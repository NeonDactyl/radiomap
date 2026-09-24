"""Local elevation lookups from downloaded USGS 3DEP DEM tiles -- the
primary elevation source, ahead of the network point-query APIs in
elevation.py.

USGS distributes seamless 1-arc-second (~30m) national elevation data as
public, unauthenticated 1x1-degree GeoTIFF tiles on S3 (verified
directly: https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation/1/
TIFF/current/{tile}/USGS_1_{tile}.tif, e.g. n40w106 for the tile covering
39-40N, 105-106W). Once a tile is downloaded it's cached on disk forever
(terrain doesn't change) and every point lookup inside it becomes a local
array read -- no per-point network calls, no rate limits, and no more of
the point-API regional gaps this project hit directly (confirmed: parts
of Alaska returned no response at all from USGS EPQS; the equivalent DEM
tile downloads and reads fine).

The *decoded, in-memory* copy of each tile (tens of MB apiece) is a
separate matter from the on-disk file: it's evicted after TILE_CACHE_TTL_S
unused, re-read from the already-downloaded local file (not re-fetched
over the network) on the next request. A real long-running server OOM'd
without this -- a full background-seeder pass touches a few hundred
distinct tiles nationwide, and nothing was ever bounding how many stayed
resident in RAM.

Falls back to the network-based ElevationProvider (Open-Meteo + USGS
EPQS) only for points whose tile can't be downloaded -- true gaps in
3DEP's own coverage (some remote US territories) or a transient network
failure fetching the tile itself.
"""
import logging
import math
import tempfile
import threading
import time
from pathlib import Path

import requests

from ..config import DEM_TILE_BASE_URL, DEM_TILE_DIR, HTTP_HEADERS

log = logging.getLogger(__name__)

TILE_DOWNLOAD_TIMEOUT_S = 30

# How long a *decoded, in-memory* tile array sticks around after its last
# use. The downloaded .tif on disk is still kept forever (terrain doesn't
# change -- see module docstring), but a long-running process (the
# background seeder walks every station nationwide, touching a few hundred
# distinct 1x1-degree tiles over a full pass) was never evicting the
# decoded numpy arrays from RAM, which OOM'd a real server. Each array is
# tens of MB, and nothing bounded how many stayed resident.
TILE_CACHE_TTL_S = 30 * 60
# How often an eviction sweep runs, decoupled from any single tile's
# lookup -- otherwise a provider that keeps hitting a rotating set of
# *different* tiles (exactly the seeder's access pattern) would only ever
# take the cache-miss/download path for each one and never revisit, let
# alone sweep, the entries it already has cached.
SWEEP_INTERVAL_S = 60


class TileUnavailable(RuntimeError):
    def __init__(self, message: str, confirmed_missing: bool = False):
        super().__init__(message)
        # True only for a 404 -- 3DEP has near-complete US *land* coverage,
        # so a tile confirmed not to exist in its catalog is strong
        # evidence the area is open water, not a real gap in land data.
        # False for a download failure (network error, timeout, etc.),
        # which says nothing about whether the tile exists -- that case
        # must not be treated the same way (see get_elevations below).
        self.confirmed_missing = confirmed_missing


def tile_name(lat: float, lon: float) -> str:
    """USGS 1x1-degree tile naming: named by its NW corner, so it covers
    one full degree south and east of that corner. e.g. n40w106 covers
    39-40N, 105-106W.
    """
    lat_letter = "n" if lat >= 0 else "s"
    lon_letter = "e" if lon >= 0 else "w"
    tile_lat = math.floor(abs(lat)) + 1
    tile_lon = math.floor(abs(lon)) + 1
    return f"{lat_letter}{tile_lat:02d}{lon_letter}{tile_lon:03d}"


class LocalDemProvider:
    def __init__(self, tile_dir: Path, fallback_provider=None):
        self.tile_dir = tile_dir
        self.fallback = fallback_provider
        self._datasets: dict[str, tuple] = {}  # tile -> (numpy array, affine transform, last_used_monotonic)
        self._lock = threading.Lock()  # guards tile download + open (rasterio datasets aren't thread-safe to share)
        self._last_sweep = time.monotonic()

    def _tile_path(self, name: str) -> Path:
        return self.tile_dir / f"USGS_1_{name}.tif"

    def _download_tile(self, name: str) -> Path:
        path = self._tile_path(name)
        if path.exists():
            return path

        url = f"{DEM_TILE_BASE_URL}/{name}/USGS_1_{name}.tif"
        try:
            resp = requests.get(url, headers=HTTP_HEADERS, timeout=TILE_DOWNLOAD_TIMEOUT_S, stream=True)
            if resp.status_code == 404:
                raise TileUnavailable(f"No DEM tile {name} (outside 3DEP coverage)", confirmed_missing=True)
            resp.raise_for_status()
            # Download to a temp file then rename atomically, so a
            # concurrent reader (or a crash mid-download) never sees a
            # partial file at the real path.
            fd, tmp_path = tempfile.mkstemp(dir=self.tile_dir, suffix=".tmp")
            try:
                with open(fd, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=1024 * 1024):
                        f.write(chunk)
                Path(tmp_path).rename(path)
            except BaseException:
                Path(tmp_path).unlink(missing_ok=True)
                raise
        except TileUnavailable:
            raise
        except Exception as exc:
            raise TileUnavailable(f"Failed to download DEM tile {name}: {exc}")

        log.info("Downloaded DEM tile %s (%.1f MB)", name, path.stat().st_size / 1e6)
        return path

    def _evict_expired_locked(self, now: float) -> None:
        """Caller must hold self._lock."""
        expired = [
            name for name, (_, _, last_used) in self._datasets.items()
            if now - last_used >= TILE_CACHE_TTL_S
        ]
        for name in expired:
            del self._datasets[name]
        if expired:
            log.info(
                "Evicted %d idle DEM tile(s) from memory (unused >%ds): %s",
                len(expired), TILE_CACHE_TTL_S, ", ".join(sorted(expired)),
            )

    def _maybe_sweep(self, now: float) -> None:
        if now - self._last_sweep < SWEEP_INTERVAL_S:
            return
        with self._lock:
            if now - self._last_sweep < SWEEP_INTERVAL_S:  # re-check: lost the race to another thread
                return
            self._evict_expired_locked(now)
            self._last_sweep = now

    def _get_tile_array(self, name: str):
        now = time.monotonic()
        self._maybe_sweep(now)

        cached = self._datasets.get(name)
        if cached is not None:
            array, transform, _ = cached
            self._datasets[name] = (array, transform, now)  # bump last-used
            return array, transform

        with self._lock:
            cached = self._datasets.get(name)
            if cached is not None:
                array, transform, _ = cached
                self._datasets[name] = (array, transform, now)
                return array, transform

            import rasterio  # deferred: keep this heavy import off the module's import-time cost

            path = self._download_tile(name)
            with rasterio.open(path) as ds:
                array = ds.read(1)
                transform = ds.transform
            self._datasets[name] = (array, transform, now)
            return array, transform

    def get_elevations(self, points: list[tuple[float, float]]) -> list[float]:
        results: list[float | None] = [None] * len(points)
        by_tile: dict[str, list[int]] = {}
        for i, (lat, lon) in enumerate(points):
            by_tile.setdefault(tile_name(lat, lon), []).append(i)

        fallback_points: list[tuple[float, float]] = []
        fallback_indices: list[int] = []

        for name, indices in by_tile.items():
            try:
                array, transform = self._get_tile_array(name)
            except TileUnavailable as exc:
                if exc.confirmed_missing:
                    # A 404 means 3DEP's own catalog -- near-complete for US
                    # *land* -- confirms this tile doesn't exist. That's
                    # strong enough evidence of open water on its own; a
                    # second source's opinion isn't needed, and asking for
                    # one is expensive (found directly: a coastal station
                    # whose search radius crossed several such tiles timed
                    # out well past 2 minutes waiting out the network
                    # fallback's own retry/timeout budget for each one,
                    # before defaulting to the same sea-level answer anyway).
                    log.info(
                        "DEM tile %s confirmed outside 3DEP coverage (open water); "
                        "assuming sea level for %d point(s)",
                        name, len(indices),
                    )
                    for i in indices:
                        results[i] = 0.0
                    continue
                if self.fallback is None:
                    raise
                log.warning("DEM tile %s unavailable (%s); falling back for %d point(s)", name, exc, len(indices))
                for i in indices:
                    fallback_points.append(points[i])
                    fallback_indices.append(i)
                continue

            inv = ~transform
            height, width = array.shape
            for i in indices:
                lat, lon = points[i]
                col, row = inv @ (lon, lat)
                col, row = int(col), int(row)
                col = min(max(col, 0), width - 1)
                row = min(max(row, 0), height - 1)
                results[i] = float(array[row, col])

        if fallback_points:
            # Everything reaching here is a download failure (network error,
            # timeout, etc.), never a confirmed-missing tile -- those are
            # handled above without involving the fallback at all. So an
            # unknown-status area (could well be real land) stays unknown
            # on failure here; propagate rather than guess.
            fallback_results = self.fallback.get_elevations(fallback_points)
            for idx, value in zip(fallback_indices, fallback_results):
                results[idx] = value

        return results  # type: ignore[return-value]

    def get_elevation(self, lat: float, lon: float) -> float:
        return self.get_elevations([(lat, lon)])[0]
