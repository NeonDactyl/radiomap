"""Tree canopy cover from the USDA Forest Service's NLCD Tree Canopy Cover
(TCC) product, read lazily and directly over HTTP -- no download, no API
key, no rate limit.

How this works: the Forest Service publishes a single seamless CONUS
raster (30m resolution, percent canopy cover 0-100) as a .tif inside a
.zip, e.g.
https://data.fs.usda.gov/geodata/rastergateway/treecanopycover/docs/v2025-6/nlcd_tcc_conus_2025_v2025-6_wgs84.zip
(~3.6GB; found via that page's own download-link markup, not a search
engine guess -- see the session that built this for how the FSGeodata
Clearinghouse page and the exact filenames were confirmed). The .tif
inside is stored *uncompressed* within the zip (confirmed directly), and
is itself internally tiled (512x512 blocks, deflate) and GDAL-readable
over HTTP via chained VSI handlers: `/vsizip/vsicurl/<zip-url>/<inner
name>`. GDAL only fetches the specific 512x512 tiles a read actually
touches via HTTP range requests -- confirmed directly: 40 profile points
along a real 150km bearing read in 1.5s total (most sharing tiles with
their neighbors, since a tile covers ~15km), versus 8.5s for one big
rectangular window covering the same span. So: per-point reads on a
single persistent dataset handle, relying on GDAL's own block cache for
points that land in an already-fetched tile, not a bounding-box read.

The raster's CRS is a projected Albers Equal-Area (not plain lat/lon), so
every point needs a coordinate transform before indexing -- confirmed by
inspection; an initial naive attempt indexing raw lat/lon degrees against
this CRS silently returned nothing.

Percent canopy cover isn't itself an obstruction height (what the
propagation model's knife-edge geometry needs); this scales it against a
nominal mature-tree height as a simple, explicitly approximate stand-in --
see NOMINAL_TREE_HEIGHT_M below.
"""
import logging
import threading

from ..config import HTTP_HEADERS  # noqa: F401  (kept for parity with other geo modules; GDAL does its own HTTP)

log = logging.getLogger(__name__)

CANOPY_ZIP_URL = (
    "https://data.fs.usda.gov/geodata/rastergateway/treecanopycover/"
    "docs/v2025-6/nlcd_tcc_conus_2025_v2025-6_wgs84.zip"
)
CANOPY_INNER_TIF = "nlcd_tcc_conus_wgs84_v2025-6_20250101_20251231.tif"
GDAL_PATH = f"/vsizip/vsicurl/{CANOPY_ZIP_URL}/{CANOPY_INNER_TIF}"

NODATA_VALUE = 255

# Percent cover -> obstruction height is not a physical measurement, just a
# simple, explicit approximation: treat a pixel's cover fraction as scaling
# a nominal mature-tree height. 18m is a reasonable single national
# constant for mature canopy (real heights vary a lot by species/region --
# this is a stand-in, not a vegetation-height dataset).
NOMINAL_TREE_HEIGHT_M = 18.0


def pixel_value_to_height_m(value) -> float:
    """Converts one raw raster pixel (percent canopy cover, 0-100, or the
    NoData sentinel) into an estimated obstruction height in meters.
    """
    if value == NODATA_VALUE:
        return 0.0
    pct = max(0.0, min(float(value), 100.0))
    return (pct / 100.0) * NOMINAL_TREE_HEIGHT_M


class RemoteTreeCanopyProvider:
    def __init__(self):
        self._local = threading.local()  # one GDAL dataset handle per thread -- rasterio datasets aren't safe to share across threads

    def _dataset(self):
        ds = getattr(self._local, "dataset", None)
        if ds is None:
            import rasterio  # deferred: keep this heavy import off module import time
            ds = rasterio.open(GDAL_PATH)
            self._local.dataset = ds
            self._local.transformer = None
        return ds

    def _transformer(self):
        transformer = getattr(self._local, "transformer", None)
        if transformer is None:
            from pyproj import Transformer
            ds = self._dataset()
            transformer = Transformer.from_crs("EPSG:4326", ds.crs, always_xy=True)
            self._local.transformer = transformer
        return transformer

    def canopy_heights_m(self, points: list[tuple[float, float]]) -> list[float]:
        try:
            ds = self._dataset()
            transformer = self._transformer()
        except Exception:
            log.exception("Tree canopy data unavailable; treating this batch as no canopy")
            return [0.0] * len(points)

        results = []
        for lat, lon in points:
            try:
                x, y = transformer.transform(lon, lat)
                row, col = ds.index(x, y)
                if row < 0 or col < 0 or row >= ds.height or col >= ds.width:
                    results.append(0.0)
                    continue
                value = ds.read(1, window=((row, row + 1), (col, col + 1)))[0, 0]
                results.append(pixel_value_to_height_m(value))
            except Exception:
                # A single point's read failing (e.g. a transient network
                # hiccup fetching one tile) shouldn't fail the whole batch --
                # canopy is a refinement on top of the terrain model, not a
                # required input the way elevation is.
                log.warning("Tree canopy lookup failed for (%.4f, %.4f); treating as no canopy", lat, lon)
                results.append(0.0)
        return results
