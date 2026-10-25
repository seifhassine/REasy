from concurrent.futures import FIRST_COMPLETED, wait
from dataclasses import dataclass
from functools import partial
import os
import pickle

lazy from file_handlers.sdf import dxc
lazy from ..native_build_cache import _immutable_size
lazy from ..worker_pool import worker_count, workers
lazy from . import re_engine_dxil_authoring as native


MIN_PARALLEL_JOBS = 32
MAX_CHUNK_JOBS = 16
MAX_CHUNK_BYTES = 4 * 1024 * 1024
MAX_PENDING_BYTES = 32 * 1024 * 1024
_WORKER_SESSION = None
_WORKER_IDENTITY = None
_WORKER_CONFIGURATION = None


@dataclass(frozen=True, slots=True)
class NativeWorkerOutcome:
    index: int
    result: object
    error_type: str
    error_message: str
    compiles: int
    validations: int
    runtime_identity: tuple
    size: int | None = None
    carrier: bytes | None = None


def initialize_native_worker(runtime_identity, configuration):
    global _WORKER_SESSION, _WORKER_IDENTITY, _WORKER_CONFIGURATION
    os.environ["REASY_DXCOMPILER_PATH"] = runtime_identity[0]
    from ..material_region_read import region_read_session

    if tuple(configuration) != tuple(native._DXIL_BUILD_CONFIGURATION):
        raise RuntimeError("DXIL worker build configuration differs from its parent")
    dxc.find_dxcompiler.cache_clear()
    dxc._library.cache_clear()
    if dxc._runtime_identity(dxc._library()) != runtime_identity:
        raise RuntimeError("DXIL worker compiler/validator runtime differs from its parent")
    _WORKER_IDENTITY, _WORKER_CONFIGURATION = runtime_identity, tuple(configuration)
    _WORKER_SESSION = region_read_session()
    _WORKER_SESSION.__enter__()


def _execute_chunk(runtime_identity, configuration, chunk, carriers=()):
    if _WORKER_IDENTITY is None:
        initialize_native_worker(runtime_identity, configuration)
    if (_WORKER_IDENTITY, _WORKER_CONFIGURATION) != (runtime_identity, tuple(configuration)):
        raise RuntimeError("DXIL worker runtime differs from its parent")
    if dxc._runtime_identity(dxc._library()) != _WORKER_IDENTITY:
        raise RuntimeError("DXIL worker runtime changed before compilation")
    for blob in carriers:
        native.remember_carrier(blob)
    outcomes = []
    for index, payload in chunk:
        job = pickle.loads(payload)
        if job.configuration != _WORKER_CONFIGURATION:
            raise RuntimeError("DXIL worker job configuration differs from its parent")
        before = native.native_build_activity()
        result = size = None
        error_type = error_message = ""
        try:
            result = native._execute_dxil_job(job)
        except Exception as exc:
            error_type, error_message = type(exc).__name__, str(exc)
        else:
            try:
                size = _immutable_size(result)
            except (TypeError, ValueError, RecursionError):
                pass
        after = native.native_build_activity()
        compiles = after["compile"] - before["compile"]
        outcomes.append(NativeWorkerOutcome(index, result, error_type, error_message,
            compiles, after["validate"] - before["validate"],
            _WORKER_IDENTITY, size, native.carrier_blob(job) if compiles else None))
    if dxc._runtime_identity(dxc._library()) != _WORKER_IDENTITY:
        raise RuntimeError("DXIL worker runtime changed during compilation")
    return tuple(outcomes)


def _carrier(job) -> tuple:
    return job.source, job.entry_point, job.carrier_contract.target, job.carrier_arguments


class NativeWorkers:
    """Hands DXIL jobs to the shared worker processes as they come, those sharing a carrier in one chunk where several
    wait, with the carriers other workers already compiled, and publishes their outcomes. Made inside
    ``shared_workers``."""

    def __init__(self, runtime_identity, configuration, publish):
        self.identity, self.configuration, self.publish = runtime_identity, tuple(configuration), publish
        self.executor, self.count = workers(), worker_count()
        self.info = {"workers": self.count, "submitted": 0, "worker_compiles": 0, "worker_validations": 0,
                     "peak_pending_input_bytes": 0, "oversized_or_mutable": 0}
        self.queue, self.sequential, self.next_chunk = [], [], None
        self.pending, self.pending_bytes = {}, 0
        self.keys, self.carriers = {}, {}

    def feed(self, index, job):
        """A job too large or not picklable is left to the parent: ``finish`` names it."""
        try:
            payload = pickle.dumps(job, protocol=pickle.HIGHEST_PROTOCOL)
        except (TypeError, ValueError, RecursionError, pickle.PickleError):
            payload = None
        if payload is None or len(payload) > MAX_CHUNK_BYTES:
            self.sequential.append(index)
        else:
            self.keys[index] = key = _carrier(job)
            self.queue.append((key, index, payload))

    def pump(self, *, final=False):
        """Hands queued jobs over while the workers have room; ``final`` waits for every outcome."""
        while True:
            for future in [future for future in self.pending if future.done()]:
                self._publish(future)
            while len(self.pending) < self.count * 2:
                if self.next_chunk is None:
                    if not self.queue:
                        break
                    self.next_chunk = self._chunk()
                chunk, size = self.next_chunk
                carriers = tuple({key: self.carriers[key] for index, _payload in chunk
                                  if (key := self.keys[index]) in self.carriers}.values())
                size += sum(map(len, carriers))
                if self.pending_bytes + size > MAX_PENDING_BYTES:
                    break
                future = self.executor.submit(partial(_execute_chunk, carriers=carriers), self.identity,
                                              self.configuration, chunk)
                self.pending[future] = size, tuple(index for index, _payload in chunk)
                self.pending_bytes += size
                self.info["submitted"] += len(chunk)
                self.info["peak_pending_input_bytes"] = max(self.info["peak_pending_input_bytes"], self.pending_bytes)
                self.next_chunk = None
            if not final or not self.pending:
                return
            wait(self.pending, return_when=FIRST_COMPLETED)

    def finish(self) -> tuple[dict, list]:
        """Every job's outcome published; the info and the jobs left to the parent."""
        self.pump(final=True)
        self.info["oversized_or_mutable"] = len(self.sequential)
        if not self.info["submitted"]:
            self.info["workers"] = 0
        return self.info, self.sequential

    def _chunk(self) -> tuple[tuple, int]:
        self.queue.sort(key=lambda item: item[0])
        chunk, size = [], 0
        while self.queue and len(chunk) < MAX_CHUNK_JOBS and (not chunk or size + len(self.queue[0][2]) <= MAX_CHUNK_BYTES):
            _carrier_key, index, payload = self.queue.pop(0)
            chunk.append((index, payload))
            size += len(payload)
        return tuple(chunk), size

    def _publish(self, future):
        size, expected_indices = self.pending.pop(future)
        self.pending_bytes -= size
        outcomes = future.result()
        if (tuple(item.index for item in outcomes) != expected_indices
                or any(item.runtime_identity != self.identity for item in outcomes)):
            raise RuntimeError("DXIL worker returned mismatched jobs or runtime identity")
        for outcome in outcomes:
            self.publish(outcome)
            self.info["worker_compiles"] += outcome.compiles
            self.info["worker_validations"] += outcome.validations
            if outcome.carrier is not None:
                self.carriers.setdefault(self.keys[outcome.index], outcome.carrier)
