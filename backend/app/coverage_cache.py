"""Read/write cached coverage contours (see db.py: coverage_cache table).

Precomputing coverage ahead of time (importers/precompute_coverage.py) and
serving a cache hit on a live request are the same lookup -- both go
through here so they can't drift.
"""
import json
from datetime import datetime, timezone

from .db import get_conn
from .propagation.params import cache_key_params

_COLUMNS = ("station_id", "model", "threshold_dbu", "max_radius_km", "step_km", "n_bearings", "ground_conductivity_mmho")


def _key_values(station_id: int, model_name: str, params: dict) -> tuple:
    key = cache_key_params(params)
    return (
        station_id, model_name, key["threshold_dbu"], key["max_radius_km"],
        key["step_km"], key["n_bearings"], key["ground_conductivity_mmho"],
    )


def lookup(station_id: int, model_name: str, params: dict) -> list[list[float]] | None:
    values = _key_values(station_id, model_name, params)
    conn = get_conn()
    try:
        row = conn.execute(
            f"SELECT contour_json FROM coverage_cache WHERE {' AND '.join(c + ' = ?' for c in _COLUMNS)}",
            values,
        ).fetchone()
    finally:
        conn.close()
    return json.loads(row["contour_json"]) if row else None


def store(station_id: int, model_name: str, params: dict, contour: list[list[float]]) -> None:
    values = _key_values(station_id, model_name, params)
    conn = get_conn()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO coverage_cache "
            f"({', '.join(_COLUMNS)}, contour_json, computed_at) "
            f"VALUES ({', '.join('?' for _ in _COLUMNS)}, ?, ?)",
            values + (json.dumps(contour), datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()
