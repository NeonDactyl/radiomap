"""Tree canopy / land-cover lookup, kept behind a small interface so the
propagation model doesn't care which implementation is wired in.

v1 found no reachable free canopy API (the obvious USGS/MRLC service
endpoints weren't reachable from here) and shipped a null provider. That's
since been replaced: `tree_canopy.py`'s RemoteTreeCanopyProvider reads
USDA Forest Service NLCD Tree Canopy Cover data directly (lazily, via
HTTP range requests into a public, unauthenticated GeoTIFF -- no download,
no API key, no rate limit) -- see that module's docstring for how it was
found and verified. `NullCanopyProvider`/`ConstantCanopyProvider` stay
here for testing.

Batch interface (not per-point) deliberately, matching ElevationProvider:
a whole bearing's worth of profile points at once lets an implementation
batch/cache reads efficiently (e.g. GDAL's own block cache across nearby
points), the same way elevation batches across a whole bearing already
does.
"""
from abc import ABC, abstractmethod


class CanopyProvider(ABC):
    @abstractmethod
    def canopy_heights_m(self, points: list[tuple[float, float]]) -> list[float]:
        """Typical tree height (m) at each (lat, lon) point, in order. 0 = no canopy."""
        raise NotImplementedError


class NullCanopyProvider(CanopyProvider):
    """No tree-cover data -- the model behaves as if terrain were bare."""

    def canopy_heights_m(self, points: list[tuple[float, float]]) -> list[float]:
        return [0.0] * len(points)


class ConstantCanopyProvider(CanopyProvider):
    """Useful for experimenting with the effect of blanket forest cover,
    e.g. ConstantCanopyProvider(15.0) to pretend the whole study area is
    covered in 15m trees.
    """

    def __init__(self, height_m: float):
        self.height_m = height_m

    def canopy_heights_m(self, points: list[tuple[float, float]]) -> list[float]:
        return [self.height_m] * len(points)


def _default_canopy_provider() -> CanopyProvider:
    try:
        from .tree_canopy import RemoteTreeCanopyProvider
        return RemoteTreeCanopyProvider()
    except Exception:
        import logging
        logging.getLogger(__name__).exception(
            "Could not initialize the remote tree-canopy provider; falling back to no canopy data"
        )
        return NullCanopyProvider()


canopy_provider: CanopyProvider = _default_canopy_provider()
