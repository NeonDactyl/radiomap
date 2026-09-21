"""CLI for importing FCC AM/FM station data into the local SQLite DB.

Usage (run from backend/):
    python -m app.importers.cli --service fm --states CA,NV,OR
    python -m app.importers.cli --service am --states all
    python -m app.importers.cli --service both --states all
"""
import argparse
import logging
import time

from .. import db
from . import common, fcc_am, fcc_fm

log = logging.getLogger(__name__)

UPSERT_SQL = """
INSERT INTO stations (
    facility_id, callsign, service, frequency_mhz, channel, class, status,
    city, state, country, file_number, erp_kw, erp_v_kw, power_night_kw,
    haat_m, directional, lat, lon, licensee
) VALUES (
    :facility_id, :callsign, :service, :frequency_mhz, :channel, :class, :status,
    :city, :state, :country, :file_number, :erp_kw, :erp_v_kw, :power_night_kw,
    :haat_m, :directional, :lat, :lon, :licensee
)
ON CONFLICT(facility_id, service) DO UPDATE SET
    callsign=excluded.callsign, frequency_mhz=excluded.frequency_mhz,
    channel=excluded.channel, class=excluded.class, status=excluded.status,
    city=excluded.city, state=excluded.state, country=excluded.country,
    file_number=excluded.file_number, erp_kw=excluded.erp_kw,
    erp_v_kw=excluded.erp_v_kw, power_night_kw=excluded.power_night_kw,
    haat_m=excluded.haat_m, directional=excluded.directional,
    lat=excluded.lat, lon=excluded.lon, licensee=excluded.licensee;
"""


def store_rows(rows: list[dict]) -> None:
    if not rows:
        return
    with db.db_session() as conn:
        conn.executemany(UPSERT_SQL, rows)


def run(service: str, states: list[str], delay: float) -> None:
    db.init_db()
    for i, state in enumerate(states):
        if service in ("fm", "both"):
            try:
                store_rows(fcc_fm.import_state(state))
            except common.FccFetchError as exc:
                log.warning("FM import failed for %s: %s", state, exc)
        if service in ("am", "both"):
            try:
                store_rows(fcc_am.import_state(state))
            except common.FccFetchError as exc:
                log.warning("AM import failed for %s: %s", state, exc)
        if i < len(states) - 1:
            time.sleep(delay)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Import FCC AM/FM station data")
    parser.add_argument("--service", choices=["fm", "am", "both"], default="both")
    parser.add_argument(
        "--states", default="CA",
        help="Comma-separated USPS state codes, or 'all' for every state/territory",
    )
    parser.add_argument(
        "--delay", type=float, default=1.0,
        help="Seconds to sleep between state requests (be polite to the FCC server)",
    )
    args = parser.parse_args()

    states = common.STATE_CODES if args.states.strip().lower() == "all" else [
        s.strip().upper() for s in args.states.split(",") if s.strip()
    ]
    run(args.service, states, args.delay)


if __name__ == "__main__":
    main()
