"""Background thread that slowly works through every imported station,
precomputing and caching its coverage contour (and, as a side effect,
warming elevation_cache for that area) so the map feels instant once it's
had time to run -- without anyone needing to remember to run the
precompute CLI by hand. Starts automatically with the server; see
main.py's startup hook.

Reuses importers/precompute_coverage.py's per-station logic exactly (same
default-parameter resolution as the live endpoint), just drives it
continuously instead of as a one-shot CLI run.
"""
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from dataclasses import dataclass

from . import db
from .importers.precompute_coverage import precompute_for_station

log = logging.getLogger(__name__)

# Defense in depth: the elevation layer has its own bounded retry/timeout
# budget, but a single hung station (e.g. a network edge case that budget
# doesn't cover) shouldn't be able to stall a seeder meant to slowly work
# through thousands of stations unattended. Each call gets a fresh
# single-task executor so a timed-out call can be abandoned immediately
# (shutdown(wait=False)) instead of blocking later stations behind it --
# Python can't force-kill a thread, so the stuck work keeps running
# orphaned in the background until it eventually finishes or errors, but
# the seeder loop itself stops waiting and moves on right away.
PER_STATION_TIMEOUT_S = 120


def _precompute_with_timeout(row) -> str:
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="seeder-station")
    try:
        future = executor.submit(precompute_for_station, row)
        return future.result(timeout=PER_STATION_TIMEOUT_S)
    finally:
        executor.shutdown(wait=False)

# FM computations involve real network-bound elevation lookups; space them
# out to stay polite to the (rate-limited, shared) elevation APIs. AM is
# pure math with no terrain dependency, so there's nothing to be polite to.
FM_DELAY_S = 0.75
AM_DELAY_S = 0.0
# Once a full pass finds nothing left to do, idle before checking again --
# covers new stations showing up from a later FCC import without needing a
# server restart.
IDLE_RECHECK_S = 600


@dataclass
class SeederStatus:
    running: bool = False
    total_stations: int = 0
    processed: int = 0
    newly_computed: int = 0
    already_cached: int = 0
    errors: int = 0
    current: str | None = None
    caught_up: bool = False


status = SeederStatus()
_thread: threading.Thread | None = None


def _all_stations():
    conn = db.get_conn()
    try:
        # FM first (the default service in the UI, and the slow one --
        # worth making progress on first), AM after (fast, no throttling).
        return conn.execute("SELECT * FROM stations ORDER BY service DESC, state, callsign").fetchall()
    finally:
        conn.close()


def _run_one_pass() -> None:
    rows = _all_stations()
    status.total_stations = len(rows)
    status.processed = 0
    status.newly_computed = 0
    status.already_cached = 0
    status.errors = 0
    status.caught_up = False

    log.info("Background seeder: starting a pass over %d stations", len(rows))
    for row in rows:
        status.current = f"{row['callsign']} ({row['service']}, {row['state']})"
        try:
            result = _precompute_with_timeout(row)
            if result == "computed":
                status.newly_computed += 1
            elif result == "cached":
                status.already_cached += 1
            else:  # "failed" -- e.g. elevation unavailable; NOT the same as cached
                status.errors += 1

            # Pace network-touching attempts (computed or failed), not
            # cache hits (instant, no network) -- otherwise a run of
            # failures (e.g. a whole region's elevation data being
            # temporarily unreachable, as seen with parts of Alaska) would
            # retry rapid-fire instead of backing off.
            if result in ("computed", "failed"):
                delay = FM_DELAY_S if row["service"] == "FM" else AM_DELAY_S
                if delay:
                    time.sleep(delay)
        except FutureTimeoutError:
            status.errors += 1
            log.warning(
                "Background seeder: %s exceeded %ds, abandoning and moving on",
                status.current, PER_STATION_TIMEOUT_S,
            )
        except Exception:
            status.errors += 1
            log.exception("Background seeder: failed on %s", status.current)
        status.processed += 1

        if status.processed % 100 == 0:
            log.info(
                "Background seeder: %d/%d (%d newly computed, %d already cached, %d errors)",
                status.processed, status.total_stations,
                status.newly_computed, status.already_cached, status.errors,
            )

    status.current = None
    status.caught_up = True
    log.info(
        "Background seeder: pass complete (%d newly computed, %d already cached, %d errors)",
        status.newly_computed, status.already_cached, status.errors,
    )


def _worker() -> None:
    status.running = True
    while True:
        try:
            _run_one_pass()
        except Exception:
            log.exception("Background seeder: pass aborted by an unexpected error, will retry")
        time.sleep(IDLE_RECHECK_S)


def start() -> None:
    global _thread
    if _thread is not None:
        return
    _thread = threading.Thread(target=_worker, name="coverage-seeder", daemon=True)
    _thread.start()
    log.info("Background seeder: started")
