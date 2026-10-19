from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from dataclasses import dataclass
import multiprocessing
import os
import pickle


MIN_PARALLEL_JOBS = 96
MAX_WORKERS = 8
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


def initialize_native_worker(runtime_identity, configuration):
    global _WORKER_SESSION, _WORKER_IDENTITY, _WORKER_CONFIGURATION
    os.environ["REASY_DXCOMPILER_PATH"] = runtime_identity[0]
    from file_handlers.sdf import dxc
    from ..material_region_read import region_read_session
    from . import re_engine_dxil_authoring as native

    if tuple(configuration) != tuple(native._DXIL_BUILD_CONFIGURATION):
        raise RuntimeError("DXIL worker build configuration differs from its parent")
    dxc.find_dxcompiler.cache_clear()
    dxc._library.cache_clear()
    if dxc._runtime_identity(dxc._library()) != runtime_identity:
        raise RuntimeError("DXIL worker compiler/validator runtime differs from its parent")
    _WORKER_IDENTITY, _WORKER_CONFIGURATION = runtime_identity, tuple(configuration)
    _WORKER_SESSION = region_read_session()
    _WORKER_SESSION.__enter__()


def native_worker_executor(*, max_workers, runtime_identity, configuration):
    return ProcessPoolExecutor(
        max_workers=max_workers,
        mp_context=multiprocessing.get_context("spawn"),
        initializer=initialize_native_worker,
        initargs=(runtime_identity, configuration),
    )


def native_worker_count():
    return min(MAX_WORKERS, max(1, os.cpu_count() or 1))


def _execute_chunk(chunk):
    from file_handlers.sdf import dxc
    from . import re_engine_dxil_authoring as native

    if dxc._runtime_identity(dxc._library()) != _WORKER_IDENTITY:
        raise RuntimeError("DXIL worker runtime changed before compilation")
    outcomes = []
    for index, payload in chunk:
        job = pickle.loads(payload)
        if job.configuration != _WORKER_CONFIGURATION:
            raise RuntimeError("DXIL worker job configuration differs from its parent")
        before = native.native_build_activity()
        result = None
        error_type = error_message = ""
        try:
            result = native._execute_dxil_job(job)
        except Exception as exc:
            error_type, error_message = type(exc).__name__, str(exc)
        after = native.native_build_activity()
        outcomes.append(NativeWorkerOutcome(index, result, error_type, error_message,
            after["compile"] - before["compile"], after["validate"] - before["validate"],
            _WORKER_IDENTITY))
    if dxc._runtime_identity(dxc._library()) != _WORKER_IDENTITY:
        raise RuntimeError("DXIL worker runtime changed during compilation")
    return tuple(outcomes)


def _job_chunks(indexed_jobs, sequential):
    from ..native_build_cache import _immutable_size

    chunk, size = [], 0
    for index, job in indexed_jobs:
        try:
            if _immutable_size(job) > MAX_CHUNK_BYTES:
                sequential.append(index)
                continue
            payload = pickle.dumps(job, protocol=pickle.HIGHEST_PROTOCOL)
        except (TypeError, ValueError, RecursionError, pickle.PickleError):
            sequential.append(index)
            continue
        if len(payload) > MAX_CHUNK_BYTES:
            sequential.append(index)
            continue
        if chunk and (len(chunk) >= MAX_CHUNK_JOBS or size + len(payload) > MAX_CHUNK_BYTES):
            yield tuple(chunk), size
            chunk, size = [], 0
        chunk.append((index, payload))
        size += len(payload)
    if chunk:
        yield tuple(chunk), size


def run_native_workers(indexed_jobs, runtime_identity, configuration, publish):
    workers = native_worker_count()
    info = {"workers": workers, "submitted": 0, "worker_compiles": 0,
            "worker_validations": 0, "peak_pending_input_bytes": 0,
            "oversized_or_mutable": 0}
    sequential = []
    pending, pending_bytes, next_chunk = {}, 0, None
    exhausted = False
    indexed_jobs = sorted(indexed_jobs, key=lambda item: (
        item[1].source, item[1].entry_point,
        item[1].carrier_contract.target, item[1].carrier_arguments))
    chunks = iter(_job_chunks(indexed_jobs, sequential))
    with native_worker_executor(max_workers=workers, runtime_identity=runtime_identity,
                                configuration=configuration) as executor:
        while pending or not exhausted or next_chunk is not None:
            while len(pending) < workers * 2 and not exhausted:
                if next_chunk is None:
                    next_chunk = next(chunks, None)
                    if next_chunk is None:
                        exhausted = True
                        break
                chunk, size = next_chunk
                if pending_bytes + size > MAX_PENDING_BYTES:
                    break
                future = executor.submit(_execute_chunk, chunk)
                pending[future] = size, tuple(index for index, _payload in chunk)
                pending_bytes += size
                info["submitted"] += len(chunk)
                info["peak_pending_input_bytes"] = max(info["peak_pending_input_bytes"], pending_bytes)
                next_chunk = None
            if not pending:
                break
            completed, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in completed:
                size, expected_indices = pending.pop(future)
                pending_bytes -= size
                outcomes = future.result()
                if (tuple(item.index for item in outcomes) != expected_indices
                        or any(item.runtime_identity != runtime_identity for item in outcomes)):
                    raise RuntimeError("DXIL worker returned mismatched jobs or runtime identity")
                for outcome in outcomes:
                    publish(outcome)
                    info["worker_compiles"] += outcome.compiles
                    info["worker_validations"] += outcome.validations
    info["oversized_or_mutable"] = len(sequential)
    if not info["submitted"]:
        info["workers"] = 0
    return info
