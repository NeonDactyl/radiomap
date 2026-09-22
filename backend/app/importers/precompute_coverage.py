"""Precompute and cache coverage contours for many stations ahead of time,
so live requests hit coverage_cache instead of computing on the spot.

Usage (from backend/, with the venv active):
    python -m app.importers.precompute_coverage --states CA
    python -m app.importers.precompute_coverage --service FM --states all --delay 0.5

Uses the exact same default-parameter resolution as the live /coverage
endpoint (propagation/params.py), so a precomputed entry is guaranteed to
be found by a live request for the same station that doesn't override any
parameters.
"""
import argparse
import logging
import time

from .. import coverage_cache, db
from ..geo.elevation import ElevationUnavailable, elevation_provider
from ..geo.landcover import canopy_provider
from ..propagation.base import Station
from ..propagation.params import resolve_coverage_params
from ..propagation.simple import get_model

log = logging.getLogger(__name__)


def precompute_for_station(row) -> str:
    """Returns "computed", "cached" (already had a matching entry), or
    "failed" (elevation was unavailable -- not the same as "cached" and
    must not be treated as such by a caller, or a real, persistent failure
    would be silently indistinguishable from routine cache reuse).
    """
    station = Station(
        id=row["id"], callsign=row["callsign"], service=row["service"],
        frequency_mhz=row["frequency_mhz"], erp_kw=row["erp_kw"], haat_m=row["haat_m"],
        lat=row["lat"], lon=row["lon"], directional=bool(row["directional"]),
    )
    params = resolve_coverage_params(station)
    model = get_model(station.service, elevation_provider, canopy_provider, **params)

    if coverage_cache.lookup(station.id, model.name, params) is not None:
        return "cached"

    try:
        contour = model.coverage_contour(
            station, threshold_dbu=params["threshold_dbu"], max_radius_km=params["max_radius_km"],
            step_km=params["step_km"], n_bearings=params["n_bearings"],
        )
    except ElevationUnavailable as exc:
        log.warning("Skipping %s (%s): elevation unavailable: %s", station.callsign, station.service, exc)
        return "failed"

    coverage_cache.store(station.id, model.name, params, [[lat, lon] for lat, lon in contour])
    return "computed"


def run(service: str, states: list[str] | None, delay: float) -> None:
    db.init_db()
    conn = db.get_conn()
    try:
        clauses = []
        params: list = []
        if service != "both":
            clauses.append("service = ?")
            params.append(service.upper())
        if states:
            placeholders = ",".join("?" for _ in states)
            clauses.append(f"state IN ({placeholders})")
            params.extend(states)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = conn.execute(
            f"SELECT * FROM stations {where} ORDER BY state, service, callsign", params
        ).fetchall()
    finally:
        conn.close()

    log.info("Precomputing coverage for %d stations", len(rows))
    computed = cached = failed = 0
    for i, row in enumerate(rows):
        result = precompute_for_station(row)
        if result == "computed":
            computed += 1
            if delay:
                time.sleep(delay)
        elif result == "cached":
            cached += 1
        else:
            failed += 1
        if (i + 1) % 25 == 0:
            log.info(
                "...%d/%d done (%d newly computed, %d already cached, %d failed)",
                i + 1, len(rows), computed, cached, failed,
            )
    log.info("Finished: %d newly computed, %d already cached, %d failed", computed, cached, failed)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Precompute and cache coverage contours")
    parser.add_argument("--service", choices=["fm", "am", "both"], default="both")
    parser.add_argument(
        "--states", default="CA",
        help="Comma-separated USPS state codes, or 'all' for every state/territory in the DB",
    )
    parser.add_argument(
        "--delay", type=float, default=0.1,
        help="Seconds to sleep after each newly-computed station (politeness to the elevation API)",
    )
    args = parser.parse_args()

    states = None if args.states.strip().lower() == "all" else [
        s.strip().upper() for s in args.states.split(",") if s.strip()
    ]
    run(args.service, states, args.delay)


if __name__ == "__main__":
    main()
