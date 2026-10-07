"""Independent solves in worker processes, results in input order.

A request often needs many MILP solves that do not depend on each other (stability
perturbations, chip weeks, replacement candidates). Each is a pure function of its inputs and
the solver is deterministic (fixed seed, one thread), so the results are identical for any
number of workers — ``workers`` only changes wall time. ``workers <= 1`` runs in-process.

Processes are spawned, not forked: callers run inside forked job processes and web servers
whose threads (solver, BLAS) make ``fork`` unsafe. Task functions must be importable
module-level functions and their arguments picklable, and a spawned child re-imports the
parent's ``__main__``: entry points must guard their top-level code with
``if __name__ == "__main__"`` (console scripts such as ``fpl-worker`` and ``uvicorn`` do).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from concurrent.futures import ProcessPoolExecutor
from multiprocessing import get_context


def map_ordered[T, R](fn: Callable[[T], R], tasks: Sequence[T], workers: int = 1) -> list[R]:
    """``[fn(t) for t in tasks]``, computed by up to ``workers`` processes.

    The first exception raised by a task is re-raised here; tasks not yet started are cancelled.
    """
    n = min(workers, len(tasks))
    if n <= 1:
        return [fn(t) for t in tasks]
    ex = ProcessPoolExecutor(max_workers=n, mp_context=get_context("spawn"))
    try:
        return list(ex.map(fn, tasks))
    finally:
        ex.shutdown(wait=True, cancel_futures=True)
