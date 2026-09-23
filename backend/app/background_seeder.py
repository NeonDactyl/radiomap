"""Background threads that slowly work through every imported station,
precomputing and caching coverage contours (and, as a side effect, warming
elevation_cache for that area) so the map -- and a shared link -- feels
instant once they've had time to run, without anyone needing to remember to
run the precompute CLI by hand. Started automatically with the server; see
main.py's startup hook.

Runs each precompute strategy as its own independent "lane" (own thread,
own pass over the station list, own status) rather than one combined pass,
so e.g. the much slower ITM model doesn't hold up the simple model's
progress or vice versa -- each just makes what progress it can concurrently.
WAL mode (see db.py) is what makes concurrent lanes safe to read/write
coverage_cache and elevation_cache at the same time without lock contention.

Reuses importers/precompute_coverage.py's per-station logic exactly (same
default-parameter resolution as the live endpoint), just drives it
continuously instead of as a one-shot CLI run.
"""
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field

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
# the seeder loop itself stops waiting and moves on right away. ITM is
# inherently much slower per-station (real point-to-point terrain physics,
# not a curve lookup), so it gets a longer budget.
PER_STATION_TIMEOUT_S = 120
ITM_PER_STATION_TIMEOUT_S = 300

# FM computations may need to download new DEM tiles (network-bound); space
# them out to stay polite. Already-downloaded areas are fast regardless
# (local file reads), so this mostly matters for genuinely new regions. AM is
# pure math with no terrain dependency, so there's nothing to be polite to.
# ITM reuses whatever DEM tiles the simple lane (or a prior ITM pass) has
# already pulled down for most stations, and its own compute time per
# station already paces things, so it needs less extra delay.
FM_DELAY_S = 0.75
ITM_DELAY_S = 0.1
AM_DELAY_S = 0.0
# Once a full pass finds nothing left to do, idle before checking again --
# covers new stations showing up from a later FCC import without needing a
# server restart.
IDLE_RECHECK_S = 600


@dataclass
class SeederStatus:
    name: str
    running: bool = False
    total_stations: int = 0
    processed: int = 0
    newly_computed: int = 0
    already_cached: int = 0
    errors: int = 0
    current: str | None = None
    caught_up: bool = False


@dataclass
class _Lane:
    name: str
    fm_model: str
    fm_only: bool  # ITM has no AM equivalent -- don't waste a pass on rows it can't affect
    fm_delay: float
    am_delay: float
    per_station_timeout: float
    status: SeederStatus = field(init=False)

    def __post_init__(self):
        self.status = SeederStatus(name=self.name)


_LANES = [
    _Lane("simple", fm_model="simple", fm_only=False, fm_delay=FM_DELAY_S, am_delay=AM_DELAY_S,
          per_station_timeout=PER_STATION_TIMEOUT_S),
    _Lane("itm", fm_model="itm", fm_only=True, fm_delay=ITM_DELAY_S, am_delay=AM_DELAY_S,
          per_station_timeout=ITM_PER_STATION_TIMEOUT_S),
]

# Keyed by lane name, e.g. statuses["simple"], statuses["itm"] -- what
# /api/meta/seed-status reports.
statuses: dict[str, SeederStatus] = {lane.name: lane.status for lane in _LANES}

_threads: list[threading.Thread] = []


def _precompute_with_timeout(row, fm_model: str, timeout: float) -> str:
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"seeder-{fm_model}")
    try:
        future = executor.submit(precompute_for_station, row, fm_model)
        return future.result(timeout=timeout)
    finally:
        executor.shutdown(wait=False)


def _stations_for(fm_only: bool):
    conn = db.get_conn()
    try:
        # FM first (the default service in the UI, and the slow one --
        # worth making progress on first), AM after (fast, no throttling).
        where = "WHERE service = 'FM'" if fm_only else ""
        return conn.execute(f"SELECT * FROM stations {where} ORDER BY service DESC, state, callsign").fetchall()
    finally:
        conn.close()


def _run_one_pass(lane: _Lane) -> None:
    status = lane.status
    rows = _stations_for(lane.fm_only)
    status.total_stations = len(rows)
    status.processed = 0
    status.newly_computed = 0
    status.already_cached = 0
    status.errors = 0
    status.caught_up = False

    log.info("Background seeder (%s): starting a pass over %d stations", lane.name, len(rows))
    for row in rows:
        status.current = f"{row['callsign']} ({row['service']}, {row['state']})"
        try:
            result = _precompute_with_timeout(row, lane.fm_model, lane.per_station_timeout)
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
                delay = lane.fm_delay if row["service"] == "FM" else lane.am_delay
                if delay:
                    time.sleep(delay)
        except FutureTimeoutError:
            status.errors += 1
            log.warning(
                "Background seeder (%s): %s exceeded %ds, abandoning and moving on",
                lane.name, status.current, lane.per_station_timeout,
            )
        except Exception:
            status.errors += 1
            log.exception("Background seeder (%s): failed on %s", lane.name, status.current)
        status.processed += 1

        if status.processed % 100 == 0:
            log.info(
                "Background seeder (%s): %d/%d (%d newly computed, %d already cached, %d errors)",
                lane.name, status.processed, status.total_stations,
                status.newly_computed, status.already_cached, status.errors,
            )

    status.current = None
    status.caught_up = True
    log.info(
        "Background seeder (%s): pass complete (%d newly computed, %d already cached, %d errors)",
        lane.name, status.newly_computed, status.already_cached, status.errors,
    )


def _worker(lane: _Lane) -> None:
    lane.status.running = True
    while True:
        try:
            _run_one_pass(lane)
        except Exception:
            log.exception("Background seeder (%s): pass aborted by an unexpected error, will retry", lane.name)
        time.sleep(IDLE_RECHECK_S)


def start() -> None:
    if _threads:
        return
    for lane in _LANES:
        thread = threading.Thread(target=_worker, args=(lane,), name=f"coverage-seeder-{lane.name}", daemon=True)
        thread.start()
        _threads.append(thread)
    log.info("Background seeder: started (%s)", ", ".join(lane.name for lane in _LANES))
