lazy from concurrent.futures import ProcessPoolExecutor
lazy import multiprocessing
from contextlib import contextmanager
from contextvars import ContextVar
import os

from utils.garbage_collection import configure_garbage_collector


MAX_WORKERS = 8
MIN_JOBS = 32
_POOL = ContextVar("mmtr_worker_pool", default=None)


@contextmanager
def shared_workers():
    """Parallel work inside shares one pool of worker processes, started when first needed."""
    if _POOL.get() is not None:
        yield
        return
    pool = []
    token = _POOL.set(pool)
    try:
        yield
    finally:
        _POOL.reset(token)
        for executor in pool:
            executor.shutdown(wait=False, cancel_futures=True)


def sharing() -> bool:
    """Inside ``shared_workers``."""
    return _POOL.get() is not None


def worker_count() -> int:
    return min(MAX_WORKERS, max(1, os.process_cpu_count() or 1))


def workers() -> ProcessPoolExecutor:
    """The pool of the enclosing ``shared_workers``."""
    pool = _POOL.get()
    if pool is None:
        raise RuntimeError("Worker processes are shared only inside shared_workers().")
    if not pool:
        pool.append(ProcessPoolExecutor(max_workers=worker_count(), mp_context=multiprocessing.get_context("spawn"),
                                        initializer=configure_garbage_collector))
    return pool[0]


def _set_up() -> None:
    """A started worker's first job: loading it imports what the pool's jobs need."""


def start_workers() -> None:
    """Every worker of the enclosing ``shared_workers`` starts now and sets itself up before work arrives; outside
    it, nothing happens."""
    if not sharing():
        return
    executor = workers()
    for _ in range(worker_count()):
        executor.submit(_set_up)


class Pending:
    """One job's result, from a chunk of jobs running in the shared pool."""
    __slots__ = ("future", "position")

    def __init__(self, future, position: int):
        self.future, self.position = future, position

    def done(self) -> bool:
        return self.future.done()

    def result(self):
        return self.future.result()[self.position]


def _chunk(function, jobs):
    return [function(job) for job in jobs]


def submit(function, jobs, minimum: int = MIN_JOBS) -> list[Pending] | None:
    """``function`` over ``jobs`` started in the shared pool; None outside ``shared_workers`` or when fewer than
    ``minimum`` pay for it."""
    if len(jobs) < minimum or not sharing():
        return None
    executor, size = workers(), max(1, len(jobs) // (worker_count() * 4))
    pending = []
    for start in range(0, len(jobs), size):
        chunk = jobs[start:start + size]
        future = executor.submit(_chunk, function, chunk)
        pending.extend(Pending(future, position) for position in range(len(chunk)))
    return pending


def parallel_map(function, jobs, minimum: int = MIN_JOBS) -> list | None:
    """``function`` over ``jobs`` in worker processes, or None when fewer than ``minimum`` pay for starting them."""
    if len(jobs) < minimum:
        return None
    with shared_workers():
        return [item.result() for item in submit(function, jobs, minimum)]
