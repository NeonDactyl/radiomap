"""Shared building blocks for propagation models.

The design goal is a clean seam between "how do we walk the map and build a
coverage contour" (here) and "how do we predict field strength along one
path" (implemented per model in simple.py, and later e.g. itm.py for a full
Longley-Rice / ITM implementation). Swapping models should mean writing a
new class that implements `field_strength_dbu` -- nothing else changes.
"""
import math
from abc import ABC, abstractmethod
from dataclasses import dataclass

EARTH_RADIUS_KM = 6371.0
K_FACTOR = 4.0 / 3.0  # standard effective-earth-radius factor for radio LOS
EFFECTIVE_EARTH_RADIUS_KM = EARTH_RADIUS_KM * K_FACTOR
SPEED_OF_LIGHT_M_S = 299_792_458.0


@dataclass
class Station:
    id: int
    callsign: str
    service: str  # "FM" or "AM"
    frequency_mhz: float
    erp_kw: float | None
    haat_m: float | None
    lat: float
    lon: float
    directional: bool


def destination_point(lat: float, lon: float, bearing_deg: float, distance_km: float) -> tuple[float, float]:
    """Great-circle destination point given a start, bearing, and distance."""
    lat1 = math.radians(lat)
    lon1 = math.radians(lon)
    brng = math.radians(bearing_deg)
    d_r = distance_km / EARTH_RADIUS_KM

    lat2 = math.asin(
        math.sin(lat1) * math.cos(d_r) + math.cos(lat1) * math.sin(d_r) * math.cos(brng)
    )
    lon2 = lon1 + math.atan2(
        math.sin(brng) * math.sin(d_r) * math.cos(lat1),
        math.cos(d_r) - math.sin(lat1) * math.sin(lat2),
    )
    return math.degrees(lat2), math.degrees(lon2)


def earth_curvature_bulge_m(d1_km: float, d2_km: float) -> float:
    """Height (m) that the curved earth blocks of the straight line between
    two points d1_km and d2_km from an obstruction, under the effective
    earth radius (k=4/3) approximation for standard atmospheric refraction.
    """
    return (d1_km * d2_km) / (2 * EFFECTIVE_EARTH_RADIUS_KM) * 1000.0


def knife_edge_diffraction_loss_db(obstruction_height_m: float, d1_km: float, d2_km: float, freq_mhz: float) -> float:
    """Single knife-edge diffraction loss (ITU-R P.526 approximation).

    obstruction_height_m: height of the obstruction ABOVE the direct
    line-of-sight path (positive = blocking, negative = clearance).
    Returns extra path loss in dB (0 if fully clear).
    """
    if obstruction_height_m <= 0:
        # Still some loss creeping in as clearance shrinks toward the line;
        # ignore for simplicity when there's clean, positive clearance.
        return 0.0

    d1_m = d1_km * 1000.0
    d2_m = d2_km * 1000.0
    wavelength_m = SPEED_OF_LIGHT_M_S / (freq_mhz * 1e6)

    if d1_m <= 0 or d2_m <= 0:
        return 0.0

    v = obstruction_height_m * math.sqrt(2 * (d1_m + d2_m) / (wavelength_m * d1_m * d2_m))

    if v <= -0.78:
        return 0.0
    loss = 6.9 + 20 * math.log10(math.sqrt((v - 0.1) ** 2 + 1) + v - 0.1)
    return max(loss, 0.0)


def free_space_field_strength_dbu(erp_kw: float, distance_km: float) -> float:
    """Free-space field strength (dBu, i.e. dB above 1 microvolt/meter) at
    `distance_km` from a transmitter radiating `erp_kw` kW ERP.

    Standard broadcast-engineering approximation:
        E(dBu) = 10*log10(ERP_kW) + 106.92 - 20*log10(distance_km)
    """
    distance_km = max(distance_km, 0.01)
    erp_kw = max(erp_kw, 0.001)
    return 10 * math.log10(erp_kw) + 106.92 - 20 * math.log10(distance_km)


class PropagationModel(ABC):
    name: str = "base"
    uses_terrain: bool = True

    def __init__(self, elevation_provider, canopy_provider=None):
        self.elevation = elevation_provider
        self.canopy = canopy_provider

    @abstractmethod
    def field_strengths_along_bearing(
        self, station: Station, bearing_deg: float, distances_km: list[float]
    ) -> list[float]:
        """Predicted field strength (dBu) at each distance in `distances_km`
        along one radial from the tower. Implementations should fetch the
        whole terrain profile for the bearing in one batched elevation call
        rather than one call per distance -- coverage_contour below relies
        on this being cheap to call once per bearing, not once per sample.
        """
        raise NotImplementedError

    def field_strength_dbu(self, station: Station, distance_km: float, bearing_deg: float) -> float:
        """Convenience single-point wrapper, mainly for tests/debugging."""
        return self.field_strengths_along_bearing(station, bearing_deg, [distance_km])[0]

    def coverage_contour(
        self,
        station: Station,
        threshold_dbu: float,
        max_radius_km: float,
        step_km: float = 2.0,
        n_bearings: int = 36,
    ) -> list[tuple[float, float]]:
        """Sample field strength outward along `n_bearings` evenly spaced
        radials and return, for each bearing, the boundary where the signal
        sustainedly drops below `threshold_dbu`. This traces a single closed
        polygon approximating the "expected coverage" area.

        The boundary is the last qualifying distance before a *sustained*
        drop (the next couple of samples also below threshold) -- a single
        anomalous sample doesn't end the contour early, but a real mountain
        range does. An earlier version took the outermost qualifying
        distance anywhere on the bearing, which let a station "see past" a
        genuinely blocked stretch to a distant recovery pocket beyond it,
        producing near-perfect circles for strong stations instead of the
        terrain-shaped contour the diffraction model was actually computing
        -- see the regression test in test_propagation.py.
        """
        n_steps = max(int(max_radius_km / step_km), 1)
        distances = [s * step_km for s in range(1, n_steps + 1)]
        bearings = [(360.0 / n_bearings) * i for i in range(n_bearings)]

        if self.uses_terrain and self.elevation is not None:
            # Warm the elevation cache with every sample point across every
            # bearing in as few batched HTTP calls as possible, instead of
            # letting each bearing's model call trigger its own round trip
            # (public elevation APIs are rate-limited; this is the
            # difference between ~10 requests and ~n_bearings requests).
            all_points = [(station.lat, station.lon)]
            for bearing in bearings:
                all_points.extend(destination_point(station.lat, station.lon, bearing, d) for d in distances)
            self.elevation.get_elevations(all_points)

        sustain_samples = 2  # tolerate a single-sample dip; require it to persist to count as the boundary

        contour = []
        for bearing in bearings:
            strengths = self.field_strengths_along_bearing(station, bearing, distances)
            boundary_distance = distances[-1]  # signal never sustainedly drops within the search radius
            for i, strength in enumerate(strengths):
                if strength >= threshold_dbu:
                    continue
                window = strengths[i:i + sustain_samples]
                if all(w < threshold_dbu for w in window):
                    boundary_distance = distances[i - 1] if i > 0 else step_km * 0.5
                    break
            lat, lon = destination_point(station.lat, station.lon, bearing, boundary_distance)
            contour.append((lat, lon))
        return contour
