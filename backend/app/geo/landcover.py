"""Tree canopy / land-cover lookup, kept behind a small interface so a real
dataset (e.g. NLCD tree canopy cover, served locally from a downloaded
raster, or a vegetation layer from a GIS service) can be dropped in later
without touching the propagation code.

No public, key-free, always-reachable canopy API was available when this
was built (the obvious USGS/MRLC service endpoints were not reachable from
here), so v1 ships a null provider that reports no canopy anywhere. The
propagation model already calls this for every terrain sample, so wiring
in real canopy heights later is a one-class change -- see
`CanopyProvider` below for the extension point.
"""
from abc import ABC, abstractmethod


class CanopyProvider(ABC):
    @abstractmethod
    def canopy_height_m(self, lat: float, lon: float) -> float:
        """Typical tree height at this point, in meters. 0 = no canopy."""
        raise NotImplementedError


class NullCanopyProvider(CanopyProvider):
    """Default stub: no tree-cover data source wired up yet."""

    def canopy_height_m(self, lat: float, lon: float) -> float:
        return 0.0


class ConstantCanopyProvider(CanopyProvider):
    """Useful for experimenting with the effect of blanket forest cover
    before a real dataset is wired in, e.g. ConstantCanopyProvider(15.0)
    to pretend the whole study area is covered in 15m trees.
    """

    def __init__(self, height_m: float):
        self.height_m = height_m

    def canopy_height_m(self, lat: float, lon: float) -> float:
        return self.height_m


canopy_provider: CanopyProvider = NullCanopyProvider()
