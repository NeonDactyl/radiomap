"""Longley-Rice Irregular Terrain Model (ITM) -- real point-to-point
diffraction/LOS/troposcatter physics, not the single-knife-edge
approximation SimpleFmModel uses.

Built on itmlogic (github.com/edwardoughton/itmlogic), a Python port of
NTIA's official ITM v1.2.2 algorithm (Hufford 1995), published and
peer-reviewed in the Journal of Open Source Software (Oughton et al.,
2020, DOI 10.21105/joss.02266) by researchers at Oxford and Ohio State.
Verified directly, not just trusted on the paper's say-so: ran itmlogic's
own pinned test case -- the classic Longley-Rice reference path (Crystal
Palace, South London to Mursley, Buckinghamshire, traced to Stark 1967)
-- and got an exact, bit-for-bit match on all 6 of their pinned expected
transmission-loss values. See tests/test_itm_calibration.py.

Field strength convention: ITM outputs "basic transmission loss"
relative to an isotropic radiator at a given confidence/reliability, not
our dBu-above-free-space convention directly. Rather than deriving and
separately verifying a whole new absolute conversion, this uses the
*excess* loss ITM computes beyond its own free-space reference and
subtracts that from free_space_field_strength_dbu() -- the same
baseline-minus-additional-loss pattern SimpleFmModel already uses for
knife-edge diffraction, just with ITM supplying a more complete physical
model of the "additional loss" term (multiple diffraction, troposcatter,
and the LOS regime, not just a single worst obstruction).

Ground/atmosphere parameters (permittivity, conductivity, climate zone,
refractivity) are fixed at reasonable US-average defaults rather than
exposed as inputs -- refining those per-region is a real accuracy
opportunity, not implemented here (see README).

Confidence/reliability are both fixed at 50% -- the "F(50,50)" median
case, the direct real-physics analog of what SimpleFmModel's FCC curve
already represents, so the two models are answering the same question.
"""
import logging
import math

import numpy as np

from .base import PropagationModel, Station, destination_point, free_space_field_strength_dbu

log = logging.getLogger(__name__)

# "Average ground" -- the same reference condition our AM groundwave model
# and the FCC curve calibration both use, kept consistent across models.
GROUND_RELATIVE_PERMITTIVITY = 15.0
GROUND_CONDUCTIVITY_S_PER_M = 0.005

# Climate: 5 = continental temperate, a reasonable single default for most
# of the continental US (not region-specific -- see module docstring).
CLIMATE_ZONE = 5
SURFACE_REFRACTIVITY_N_UNITS = 314.0
POLARIZATION_HORIZONTAL = 0

RECEIVER_HEIGHT_M = 9.0  # matches SimpleFmModel's FCC-standard assumption
MIN_ANTENNA_HEIGHT_M = 1.0  # ITM requires a positive structural height
MAX_ANTENNA_HEIGHT_M = 3000.0  # guards against a degenerate ring-average/HAAT combination

MIN_PROFILE_POINTS = 32
MAX_PROFILE_POINTS = 300  # itmlogic's point-to-point mode supports up to 600; well under that
PROFILE_SPACING_KM = 0.5


def _itm_excess_loss_db(freq_mhz: float, distance_km: float, hg_tx_m: float, hg_rx_m: float, elevations_m: list[float]) -> float:
    """Runs itmlogic's point-to-point mode and returns how much *more*
    loss it predicts than its own free-space reference, at 50% confidence
    / 50% reliability (the median case). Positive = more loss than free
    space (the normal case); can go negative for some clear LOS
    geometries, which free_space_field_strength_dbu() - this just
    correctly treats as a small signal enhancement.
    """
    from itmlogic.misc.qerfi import qerfi
    from itmlogic.preparatory_subroutines.qlrpfl import qlrpfl
    from itmlogic.statistics.avar import avar

    n = len(elevations_m)
    prop = {
        "eps": GROUND_RELATIVE_PERMITTIVITY,
        "sgm": GROUND_CONDUCTIVITY_S_PER_M,
        "ipol": POLARIZATION_HORIZONTAL,
        "fmhz": freq_mhz,
        "hg": [hg_tx_m, hg_rx_m],
        "klim": CLIMATE_ZONE,
        "ens0": SURFACE_REFRACTIVITY_N_UNITS,
        "d": distance_km,
        "lvar": 5,
        "gma": 157e-9,
    }
    pfl = [n - 1, distance_km * 1000.0 / (n - 1)] + list(elevations_m)
    prop["pfl"] = pfl
    prop["kwx"] = 0
    prop["wn"] = prop["fmhz"] / 47.7
    prop["ens"] = prop["ens0"]
    prop["gme"] = prop["gma"] * (1 - 0.04665 * math.exp(prop["ens"] / 179.3))
    zq = complex(prop["eps"], 376.62 * prop["sgm"] / prop["wn"])
    prop["zgnd"] = np.sqrt(zq - 1)
    prop["klimx"] = 0
    prop["mdvarx"] = 11

    z50 = qerfi([0.5])[0]

    prop = qlrpfl(prop)
    db_per_neper = 8.685890
    free_space_loss_db = db_per_neper * np.log(2 * prop["wn"] * prop["dist"])
    excess_loss_db, _ = avar(z50, 0, z50, prop)
    return float(excess_loss_db)


class ItmFmModel(PropagationModel):
    name = "itm_fm_v1"  # bump this string whenever the math below changes -- see coverage_cache

    default_haat_m = 30.0

    # Same HAAT ring-average methodology as SimpleFmModel (FCC's real
    # definition -- see that class for the full rationale). Duplicated
    # rather than shared so this model stays a self-contained,
    # independently swappable implementation of PropagationModel.
    HAAT_RING_MIN_KM = 1.5 * 1.60934
    HAAT_RING_MAX_KM = 10.0 * 1.60934
    HAAT_RING_RADIALS = 8
    HAAT_RING_SAMPLES_PER_RADIAL = 9

    def __init__(self, elevation_provider, canopy_provider=None):
        super().__init__(elevation_provider, canopy_provider)
        self._avg_terrain_elevation_cache: dict[int, float] = {}

    def _average_terrain_elevation_m(self, station: Station) -> float:
        cached = self._avg_terrain_elevation_cache.get(station.id)
        if cached is not None:
            return cached
        points = []
        for i in range(self.HAAT_RING_RADIALS):
            bearing = (360.0 / self.HAAT_RING_RADIALS) * i
            for j in range(self.HAAT_RING_SAMPLES_PER_RADIAL):
                frac = j / (self.HAAT_RING_SAMPLES_PER_RADIAL - 1)
                d = self.HAAT_RING_MIN_KM + (self.HAAT_RING_MAX_KM - self.HAAT_RING_MIN_KM) * frac
                points.append(destination_point(station.lat, station.lon, bearing, d))
        elevations = self.elevation.get_elevations(points)
        avg = sum(elevations) / len(elevations)
        self._avg_terrain_elevation_cache[station.id] = avg
        return avg

    def _profile_point_count(self, distance_km: float) -> int:
        n = int(distance_km / PROFILE_SPACING_KM)
        return max(MIN_PROFILE_POINTS, min(MAX_PROFILE_POINTS, n))

    def _field_strength_at(self, station: Station, bearing_deg: float, distance_km: float) -> float:
        n_points = self._profile_point_count(distance_km)
        coords = [
            destination_point(station.lat, station.lon, bearing_deg, distance_km * i / (n_points - 1))
            for i in range(n_points)
        ]
        elevations = self.elevation.get_elevations(coords)

        haat = station.haat_m if station.haat_m and station.haat_m > 0 else self.default_haat_m
        tx_amsl = self._average_terrain_elevation_m(station) + haat
        hg_tx = min(max(tx_amsl - elevations[0], MIN_ANTENNA_HEIGHT_M), MAX_ANTENNA_HEIGHT_M)

        try:
            excess_loss_db = _itm_excess_loss_db(
                station.frequency_mhz, distance_km, hg_tx, RECEIVER_HEIGHT_M, elevations,
            )
        except Exception:
            log.exception(
                "ITM computation failed for %s at %.1fkm on bearing %.0f; treating as free space",
                station.callsign, distance_km, bearing_deg,
            )
            excess_loss_db = 0.0

        erp = station.erp_kw if station.erp_kw and station.erp_kw > 0 else 0.05
        return free_space_field_strength_dbu(erp, distance_km) - excess_loss_db

    def field_strengths_along_bearing(
        self, station: Station, bearing_deg: float, distances_km: list[float]
    ) -> list[float]:
        return [self._field_strength_at(station, bearing_deg, d) for d in distances_km]
