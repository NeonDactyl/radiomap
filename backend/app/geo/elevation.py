"""Terrain elevation lookups via the Open-Meteo elevation API (free, no key,
SRTM/ASTER-based, ~90m resolution, up to 100 points per request), backed by
a local SQLite cache so repeated coverage runs over the same area don't
re-hit the network.
"""
import logging
import time

import requests

from ..config import ELEVATION_API_URL, HTTP_HEADERS
from ..db import get_conn

log = logging.getLogger(__name__)

BATCH_SIZE = 100
CACHE_PRECISION = 4  # ~11m grid at the equator; plenty for terrain profiling
INTER_BATCH_DELAY_S = 0.25  # be gentle -- avoid tripping the free API's rate limiter


class ElevationUnavailable(RuntimeError):
    """Raised when the elevation provider can't be reached after retries.

    We deliberately don't fall back to a flat (0m) terrain assumption on
    failure: silently pretending there are no hills would make the
    propagation model under-count terrain blocking and report coverage
    that isn't real. Better to surface the failure to the caller.
    """


def _round_key(lat: float, lon: float) -> tuple[float, float]:
    return (round(lat, CACHE_PRECISION), round(lon, CACHE_PRECISION))


class ElevationProvider:
    def __init__(self):
        self._mem_cache: dict[tuple[float, float], float] = {}

    def _load_from_db(self, keys: list[tuple[float, float]]) -> dict[tuple[float, float], float]:
        if not keys:
            return {}
        conn = get_conn()
        try:
            found = {}
            # SQLite has a default limit on bound params; chunk defensively.
            for i in range(0, len(keys), 500):
                chunk = keys[i:i + 500]
                placeholders = ",".join(["(?,?)"] * len(chunk))
                params = [v for pair in chunk for v in pair]
                rows = conn.execute(
                    f"SELECT lat_r, lon_r, elevation_m FROM elevation_cache "
                    f"WHERE (lat_r, lon_r) IN ({placeholders})",
                    params,
                ).fetchall()
                for r in rows:
                    found[(r["lat_r"], r["lon_r"])] = r["elevation_m"]
            return found
        finally:
            conn.close()

    def _save_to_db(self, values: dict[tuple[float, float], float]) -> None:
        if not values:
            return
        conn = get_conn()
        try:
            conn.executemany(
                "INSERT OR REPLACE INTO elevation_cache (lat_r, lon_r, elevation_m) VALUES (?,?,?)",
                [(k[0], k[1], v) for k, v in values.items()],
            )
            conn.commit()
        finally:
            conn.close()

    def get_elevations(self, points: list[tuple[float, float]]) -> list[float]:
        """Return elevation in meters for each (lat, lon) point, in order."""
        keys = [_round_key(lat, lon) for lat, lon in points]

        missing_keys = sorted({k for k in keys if k not in self._mem_cache})
        db_hits = self._load_from_db(missing_keys)
        self._mem_cache.update(db_hits)

        still_missing = [k for k in missing_keys if k not in self._mem_cache]
        fetched: dict[tuple[float, float], float] = {}
        for i in range(0, len(still_missing), BATCH_SIZE):
            if i > 0:
                time.sleep(INTER_BATCH_DELAY_S)
            batch = still_missing[i:i + BATCH_SIZE]
            elevations = self._fetch_batch(batch)
            for key, elev in zip(batch, elevations):
                fetched[key] = elev
        self._mem_cache.update(fetched)
        self._save_to_db(fetched)

        return [self._mem_cache[k] for k in keys]

    def get_elevation(self, lat: float, lon: float) -> float:
        return self.get_elevations([(lat, lon)])[0]

    def _fetch_batch(self, keys: list[tuple[float, float]], retries: int = 5) -> list[float]:
        lats = ",".join(str(k[0]) for k in keys)
        lons = ",".join(str(k[1]) for k in keys)
        last_exc = None
        for attempt in range(1, retries + 1):
            try:
                resp = requests.get(
                    ELEVATION_API_URL,
                    params={"latitude": lats, "longitude": lons},
                    headers=HTTP_HEADERS,
                    timeout=20,
                )
                if resp.status_code == 429:
                    retry_after = resp.headers.get("Retry-After")
                    wait = float(retry_after) if retry_after else 3.0 * attempt
                    last_exc = RuntimeError(f"429 rate limited (attempt {attempt}/{retries})")
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
                data = resp.json()
                elevations = data.get("elevation", [])
                if len(elevations) != len(keys):
                    raise ValueError("elevation API returned mismatched result count")
                return [float(e) for e in elevations]
            except Exception as exc:
                last_exc = exc
                time.sleep(1.0 * attempt)
        log.warning("Elevation lookup failed for batch of %d points after %d attempts: %s", len(keys), retries, last_exc)
        raise ElevationUnavailable(
            f"Elevation service unavailable after {retries} attempts ({last_exc})"
        )


# Module-level singleton; the in-memory cache is cheap and process-local.
elevation_provider = ElevationProvider()
