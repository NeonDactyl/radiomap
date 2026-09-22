"""v1 propagation models.

FM/VHF: free-space path loss plus single-knife-edge terrain diffraction
along the tower-to-receiver path, with earth curvature accounted for via
the standard k=4/3 effective-earth-radius approximation. Tree canopy (when
a real CanopyProvider is wired in -- see geo/landcover.py) adds to the
terrain height used for the obstruction check.

AM/MW: groundwave propagation is a genuinely different physical mechanism
(a surface wave hugging the curved earth, governed by ground conductivity,
not line-of-sight) and the real FCC curves are derived from numerically
integrated Sommerfeld equations (see 47 CFR 73.190) -- not something to
approximate casually. v1 uses a simple exponential-with-distance model
calibrated to roughly match the FCC's "average ground" (5 mS/m) curve at
one reference point (1000 kHz, 1 kW, 54 dBu contour at ~60 km). It ignores
terrain and tree cover entirely, which is a reasonable approximation for
groundwave at broadcast wavelengths (hundreds of meters) but is otherwise
a placeholder for a real groundwave/ITM implementation.

Both models share the PropagationModel interface (base.py) so either can
be swapped out (e.g. for a Longley-Rice/ITM implementation) without
touching the API layer or the coverage-contour marching logic.
"""
import math

from .base import (
    PropagationModel,
    Station,
    destination_point,
    earth_curvature_bulge_m,
    free_space_field_strength_dbu,
    knife_edge_diffraction_loss_db,
)


class SimpleFmModel(PropagationModel):
    name = "simple_fm_v2"  # bump this string whenever the math below changes -- see coverage_cache

    default_haat_m = 30.0
    receiver_height_m = 9.0  # ~30ft, the FCC's standard FM receive height

    def field_strengths_along_bearing(
        self, station: Station, bearing_deg: float, distances_km: list[float]
    ) -> list[float]:
        all_distances = [0.0] + list(distances_km)
        points = [
            destination_point(station.lat, station.lon, bearing_deg, d) if d > 0 else (station.lat, station.lon)
            for d in all_distances
        ]
        elevations = self.elevation.get_elevations(points)

        canopy = [0.0] * len(points)
        if self.canopy is not None:
            canopy = [self.canopy.canopy_height_m(lat, lon) for lat, lon in points]

        haat = station.haat_m if station.haat_m and station.haat_m > 0 else self.default_haat_m
        tx_height_amsl = elevations[0] + haat
        erp = station.erp_kw if station.erp_kw and station.erp_kw > 0 else 0.05

        results = []
        for k in range(1, len(all_distances)):
            d_rx = all_distances[k]
            rx_height_amsl = elevations[k] + self.receiver_height_m

            diffraction_loss = 0.0
            for j in range(1, k):
                d1 = all_distances[j]
                d2 = d_rx - d1
                if d1 <= 0 or d2 <= 0:
                    continue
                los_height = tx_height_amsl + (rx_height_amsl - tx_height_amsl) * (d1 / d_rx)
                bulge = earth_curvature_bulge_m(d1, d2)
                terrain_height = elevations[j] + canopy[j]
                obstruction_height = terrain_height - (los_height - bulge)
                loss = knife_edge_diffraction_loss_db(obstruction_height, d1, d2, station.frequency_mhz)
                if loss > diffraction_loss:
                    diffraction_loss = loss

            free_space = free_space_field_strength_dbu(erp, d_rx)
            results.append(free_space - diffraction_loss)
        return results


class SimpleAmModel(PropagationModel):
    name = "simple_am_v2"  # bump this string whenever the math below changes -- see coverage_cache
    uses_terrain = False

    # Calibration reference: ~54 dBu (0.5 mV/m) contour at 60km for a 1kW
    # station at 1000kHz over "average" ground (5 mS/m). See module docstring.
    _BASE_ALPHA_DB_PER_KM = 0.29
    _REF_FREQ_KHZ = 1000.0
    _REF_CONDUCTIVITY_MMHO = 5.0

    def __init__(self, elevation_provider, canopy_provider=None, ground_conductivity_mmho: float = 5.0):
        super().__init__(elevation_provider, canopy_provider)
        self.ground_conductivity_mmho = max(ground_conductivity_mmho, 0.1)

    def _alpha_db_per_km(self, freq_mhz: float) -> float:
        freq_khz = freq_mhz * 1000.0
        return (
            self._BASE_ALPHA_DB_PER_KM
            * (freq_khz / self._REF_FREQ_KHZ)
            * (self._REF_CONDUCTIVITY_MMHO / self.ground_conductivity_mmho)
        )

    def field_strengths_along_bearing(
        self, station: Station, bearing_deg: float, distances_km: list[float]
    ) -> list[float]:
        power = station.erp_kw if station.erp_kw and station.erp_kw > 0 else 0.25
        alpha = self._alpha_db_per_km(station.frequency_mhz)
        results = []
        for d in distances_km:
            free_space = free_space_field_strength_dbu(power, d)
            results.append(free_space - alpha * d)
        return results


def get_model(service: str, elevation_provider, canopy_provider=None, **kwargs) -> PropagationModel:
    service = service.upper()
    if service == "FM":
        return SimpleFmModel(elevation_provider, canopy_provider)
    if service == "AM":
        return SimpleAmModel(
            elevation_provider,
            canopy_provider,
            ground_conductivity_mmho=kwargs.get("ground_conductivity_mmho", 5.0),
        )
    raise ValueError(f"Unknown service: {service}")


def suggest_fm_search_radius_km(erp_kw: float | None, haat_m: float | None) -> float:
    """A flat default search radius doesn't work across FM stations: a
    100kW/400m-HAAT station's real 54 dBu contour can be 150-250km out,
    while a 17W translator's is a few km. Undershooting the radius doesn't
    just clip the map -- it makes coverage_contour() report every bearing
    at the search cap, since the real threshold crossing was never reached
    (this was the bug behind an all-directions-identical, obviously-wrong
    circle for high-power stations: the terrain diffraction was computing
    correctly, the search radius just never got far enough to see it drop
    below threshold in any direction).

    Estimates via the standard VHF radio-horizon formula (4.12*sqrt(h) in
    km, receiver assumed at 9m) plus a log-scaled ERP term and a fixed
    margin for diffraction extending real coverage past the pure horizon.
    """
    haat = max(haat_m if haat_m and haat_m > 0 else 30.0, 1.0)
    erp = max(erp_kw if erp_kw and erp_kw > 0 else 0.05, 0.01)
    horizon_km = 4.12 * (math.sqrt(haat) + math.sqrt(9.0))
    radius = horizon_km * 1.8 + 15 * math.log10(erp + 1)
    return max(40.0, min(radius, 300.0))


def suggest_am_search_radius_km(
    erp_kw: float | None, frequency_mhz: float, ground_conductivity_mmho: float, threshold_dbu: float
) -> float:
    """AM has no terrain dependency, so unlike FM we can just solve for the
    actual threshold crossing directly (cheap: no network/elevation calls)
    instead of guessing at a search radius.
    """
    station = Station(
        id=0, callsign="", service="AM", frequency_mhz=frequency_mhz,
        erp_kw=erp_kw, haat_m=None, lat=0.0, lon=0.0, directional=False,
    )
    model = SimpleAmModel(elevation_provider=None, ground_conductivity_mmho=ground_conductivity_mmho)
    probe_distances = [d for d in range(5, 505, 5)]
    strengths = model.field_strengths_along_bearing(station, 0.0, probe_distances)
    for d, s in zip(probe_distances, strengths):
        if s < threshold_dbu:
            return min(d * 1.15, 400.0)  # small margin past the crossing
    return 400.0
