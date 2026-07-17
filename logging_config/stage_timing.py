"""
Stage timing for pipeline milestone tracking.

Wrap any pipeline stage in the ``stage()`` context manager to get a
START line, a DONE line with elapsed seconds, and (on any exception,
including KeyboardInterrupt) a FAILED line identifying where the run
died. Completed stages accumulate in a module-level list;
``log_stage_summary()`` emits a sorted where-did-the-time-go table.

Usage:
    from logging_config import stage, log_stage_summary

    with stage("relay type index build", logger):
        relay_index = RelayTypeIndex.build(app)

    ...
    log_stage_summary(logger)   # at end of run / per project
"""

import logging
import threading
import time
from contextlib import contextmanager
from typing import List, Tuple

_stage_times: List[Tuple[str, float]] = []
_lock = threading.Lock()


@contextmanager
def stage(name: str, logger: logging.Logger, level: int = logging.INFO):
    """Log entry/exit/elapsed for a named pipeline stage.

    Catches BaseException (not just Exception) so that a Ctrl-C or
    process kill mid-stage still records which stage was in flight -
    the exact question the 16 July log could not answer.
    """
    logger.log(level, f"STAGE START: {name}")
    t0 = time.perf_counter()
    try:
        yield
    except BaseException:
        elapsed = time.perf_counter() - t0
        logger.error(f"STAGE FAILED: {name} after {elapsed:.1f} s")
        with _lock:
            _stage_times.append((f"{name} (failed)", elapsed))
        raise
    else:
        elapsed = time.perf_counter() - t0
        logger.log(level, f"STAGE DONE: {name} in {elapsed:.1f} s")
        with _lock:
            _stage_times.append((name, elapsed))


def log_stage_summary(logger: logging.Logger, clear: bool = True) -> None:
    """Emit recorded stages sorted by elapsed time, largest first.

    ``clear=True`` (default) resets the accumulator afterwards so the
    mastering loop gets an independent summary per project.
    """
    with _lock:
        stages = list(_stage_times)
        if clear:
            _stage_times.clear()
    if not stages:
        return
    total = sum(t for _, t in stages)
    logger.info(f"STAGE SUMMARY: {len(stages)} stages, {total:.1f} s total")
    for name, elapsed in sorted(stages, key=lambda s: -s[1]):
        pct = 100 * elapsed / total if total else 0.0
        logger.info(f"STAGE SUMMARY: {name}: {elapsed:.1f} s ({pct:.0f}%)")


def reset_stage_times() -> None:
    """Discard accumulated stage times (start of a new project/run)."""
    with _lock:
        _stage_times.clear()