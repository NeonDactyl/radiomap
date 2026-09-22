"""Network-based point elevation lookups: Open-Meteo, falling back to USGS
EPQS. This is no longer the primary elevation source -- geo/local_dem.py
(downloaded USGS 3DEP DEM tiles, read locally) is, since it has no
per-point network cost and no rate limits after a tile is cached. This
module is kept as local_dem's own fallback, for points whose DEM tile
can't be downloaded (a genuine 3DEP coverage gap, or a transient network
failure fetching the tile itself) -- see the bottom of this file for how
the two are combined into the `elevation_provider` singleton everything
else imports.

Backed by a local SQLite cache so repeated point lookups through this
fallback path don't re-hit the network for the same point twice.

Two sources are used:
- Open-Meteo (SRTM/ASTER-based, ~90m resolution): primary, because it
  accepts up to 100 points per request. It's a shared free service though,
  and can return 429s under load from other traffic on the same network.
- USGS Elevation Point Query Service (3DEP, US-only): fallback when
  Open-Meteo fails. Only one point per request, so we parallelize with a
  thread pool, but it's authoritative US government data and matches our
  FCC-only station coverage, so it's a solid second source rather than a
  degraded one.
"""
import logging
import time
from concurrent.futures import ThreadPoolExecutor

import requests

from ..config import ELEVATION_API_URL, HTTP_HEADERS, USGS_EPQS_URL
from ..db import get_conn

log = logging.getLogger(__name__)

BATCH_SIZE = 100
CACHE_PRECISION = 4  # ~11m grid at the equator; plenty for terrain profiling
INTER_BATCH_DELAY_S = 0.25  # be gentle -- avoid tripping the free API's rate limiter
USGS_MAX_WORKERS = 10


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

    def _fetch_batch(self, keys: list[tuple[float, float]]) -> list[float]:
        try:
            return self._fetch_open_meteo(keys, retries=2)
        except Exception as exc:
            log.warning(
                "Open-Meteo failed for batch of %d points (%s); falling back to USGS EPQS",
                len(keys), exc,
            )
        try:
            return self._fetch_usgs(keys)
        except Exception as exc:
            log.warning("USGS EPQS fallback also failed for batch of %d points: %s", len(keys), exc)
            raise ElevationUnavailable(
                f"Both elevation sources unavailable for a batch of {len(keys)} points ({exc})"
            )

    def _fetch_open_meteo(self, keys: list[tuple[float, float]], retries: int) -> list[float]:
        lats = ",".join(str(k[0]) for k in keys)
        lons = ",".join(str(k[1]) for k in keys)
        last_exc = None
        for attempt in range(1, retries + 1):
            try:
                resp = requests.get(
                    ELEVATION_API_URL,
                    params={"latitude": lats, "longitude": lons},
                    headers=HTTP_HEADERS,
                    timeout=10,
                )
                if resp.status_code == 429:
                    retry_after = resp.headers.get("Retry-After")
                    wait = float(retry_after) if retry_after else 2.0 * attempt
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
                time.sleep(0.5 * attempt)
        raise RuntimeError(f"Open-Meteo failed after {retries} attempts: {last_exc}")

    def _fetch_usgs_point(self, key: tuple[float, float], retries: int = 2) -> float:
        lat, lon = key
        last_exc = None
        for attempt in range(1, retries + 1):
            try:
                resp = requests.get(
                    USGS_EPQS_URL,
                    params={"x": lon, "y": lat, "units": "Meters", "wkid": 4326, "includeDate": "false"},
                    headers=HTTP_HEADERS,
                    timeout=6,
                )
                resp.raise_for_status()
                value = resp.json()["value"]
                return float(value)
            except Exception as exc:
                last_exc = exc
                time.sleep(0.5 * attempt)
        raise RuntimeError(f"USGS EPQS failed for {key} after {retries} attempts: {last_exc}")

    def _fetch_usgs(self, keys: list[tuple[float, float]]) -> list[float]:
        # Canary check: USGS being unreachable isn't just a per-point thing
        # (e.g. no response at all for a whole region, observed for parts
        # of Alaska) -- without this, a single bad batch would retry every
        # one of up to 100 points independently at full cost before giving
        # up, worst case several minutes for one batch. Try the first point
        # alone first; if that fails, don't bother with the rest.
        try:
            first_result = self._fetch_usgs_point(keys[0])
        except Exception as exc:
            raise RuntimeError(f"USGS EPQS unreachable (canary point failed): {exc}")

        if len(keys) == 1:
            return [first_result]

        with ThreadPoolExecutor(max_workers=USGS_MAX_WORKERS) as pool:
            rest = list(pool.map(self._fetch_usgs_point, keys[1:]))
        return [first_result] + rest


# The public singleton everything else imports: local DEM tiles first,
# falling back to the network point APIs above only for points whose tile
# can't be downloaded. Constructed here (rather than in local_dem.py) so
# every existing `from .geo.elevation import elevation_provider` call site
# keeps working unchanged.
from ..config import DEM_TILE_DIR  # noqa: E402
from .local_dem import LocalDemProvider  # noqa: E402

_network_fallback = ElevationProvider()
elevation_provider = LocalDemProvider(DEM_TILE_DIR, fallback_provider=_network_fallback)
