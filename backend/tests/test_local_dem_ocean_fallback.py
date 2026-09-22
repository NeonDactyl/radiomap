"""Regression test for a real bug found while verifying coverage for a
high-power coastal station (KQED-FM, San Francisco): its search radius
extended offshore into the Pacific, where no DEM tile exists (confirmed
directly: 404 for the tile covering that point) -- and the network
fallback also failed for that same open-water point, so the whole
coverage computation failed with a 503 instead of just treating the
water as sea level.

3DEP has near-complete US *land* coverage, so a tile confirmed missing
(404, not a download error) is strong evidence of open water, not a real
land gap -- see LocalDemProvider.get_elevations. These tests use fake
providers so they don't depend on the network or the real dataset.
"""
from app.geo.local_dem import LocalDemProvider, TileUnavailable


class AlwaysMissingTileProvider(LocalDemProvider):
    """Simulates every tile being confirmed missing from 3DEP (404)."""
    def __init__(self, fallback_provider=None):
        super().__init__(tile_dir=None, fallback_provider=fallback_provider)

    def _get_tile_array(self, name):
        raise TileUnavailable(f"no tile {name}", confirmed_missing=True)


class AlwaysDownloadFailsProvider(LocalDemProvider):
    """Simulates a tile that exists but fails to download (network error)."""
    def __init__(self, fallback_provider=None):
        super().__init__(tile_dir=None, fallback_provider=fallback_provider)

    def _get_tile_array(self, name):
        raise TileUnavailable(f"download failed for {name}", confirmed_missing=False)


class AlwaysFailingFallback:
    def get_elevations(self, points):
        raise RuntimeError("network fallback unavailable")


def test_confirmed_missing_tile_defaults_to_sea_level_when_fallback_also_fails():
    provider = AlwaysMissingTileProvider(fallback_provider=AlwaysFailingFallback())
    result = provider.get_elevations([(35.9, -122.4), (36.0, -122.5)])
    assert result == [0.0, 0.0]


def test_confirmed_missing_tile_defaults_to_sea_level_with_no_fallback_at_all():
    provider = AlwaysMissingTileProvider(fallback_provider=None)
    result = provider.get_elevations([(35.9, -122.4)])
    assert result == [0.0]


def test_download_failure_does_not_default_to_sea_level():
    """A tile that merely failed to download (unknown land/water status)
    must NOT silently become 0m -- that could be real land whose true
    elevation we just failed to fetch. It must propagate the failure.
    """
    provider = AlwaysDownloadFailsProvider(fallback_provider=AlwaysFailingFallback())
    try:
        provider.get_elevations([(40.0, -105.0)])
        assert False, "expected an exception, got a silent default instead"
    except RuntimeError:
        pass
