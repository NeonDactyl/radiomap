"""Shared coverage-request parameter resolution, used by both the live API
endpoint and the offline precompute CLI so the two can never drift apart
(and so a precomputed cache entry is guaranteed to be found by the live
endpoint asking for the same station with no explicit overrides).

Threshold presets match radio-locator.com's published definitions (not
ours) so contours are comparable to what people are used to seeing there:
FM local/distant/fringe = 60/50/40 dBu; AM's are given as mV/m of
groundwave (2.0/0.5/0.15 per their FAQ), converted here to dBu (dB above 1
uV/m: dBu = 20*log10(mV/m * 1000)) since dBu is the unit our whole API
already speaks in.
"""
import math

from .base import Station
from .simple import suggest_am_search_radius_km, suggest_fm_search_radius_km


def _mv_per_m_to_dbu(mv_per_m: float) -> float:
    return 20 * math.log10(mv_per_m * 1000)


THRESHOLD_PRESETS_DBU = {
    "FM": {"local": 60.0, "distant": 50.0, "fringe": 40.0},
    "AM": {
        "local": round(_mv_per_m_to_dbu(2.0), 1),
        "distant": round(_mv_per_m_to_dbu(0.5), 1),
        "fringe": round(_mv_per_m_to_dbu(0.15), 1),
    },
}

DEFAULT_TIER = "distant"

# Bearings/step resolution: more samples = a smoother, more accurate
# contour, paid for in elevation lookups -- affordable now that results are
# cached (see coverage_cache) and a precompute pass can run ahead of time.
DEFAULT_N_BEARINGS = 36


def default_threshold_dbu(service: str) -> float:
    return THRESHOLD_PRESETS_DBU[service.upper()][DEFAULT_TIER]


def resolve_coverage_params(
    station: Station,
    threshold_dbu: float | None = None,
    max_radius_km: float | None = None,
    step_km: float | None = None,
    n_bearings: int | None = None,
    ground_conductivity_mmho: float = 5.0,
) -> dict:
    resolved_threshold = threshold_dbu if threshold_dbu is not None else default_threshold_dbu(station.service)

    if max_radius_km is not None:
        resolved_radius = max_radius_km
    elif station.service == "FM":
        resolved_radius = suggest_fm_search_radius_km(station.erp_kw, station.haat_m)
    else:
        resolved_radius = suggest_am_search_radius_km(
            station.erp_kw, station.frequency_mhz, ground_conductivity_mmho, resolved_threshold,
        )

    resolved_n_bearings = n_bearings if n_bearings is not None else DEFAULT_N_BEARINGS
    # Keep samples-per-bearing (and thus elevation-call cost) roughly
    # constant regardless of how far the search radius reaches.
    resolved_step = step_km if step_km is not None else max(1.5, min(resolved_radius / 50.0, 6.0))

    return {
        "threshold_dbu": resolved_threshold,
        "max_radius_km": resolved_radius,
        "step_km": resolved_step,
        "n_bearings": resolved_n_bearings,
        "ground_conductivity_mmho": ground_conductivity_mmho,
    }


def cache_key_params(params: dict) -> dict:
    """Round resolved params before using them as a cache key so that
    float round-tripping through query-string text can't cause a
    precomputed entry to be missed by an otherwise-identical live request.
    """
    return {
        "threshold_dbu": round(params["threshold_dbu"], 1),
        "max_radius_km": round(params["max_radius_km"]),
        "step_km": round(params["step_km"], 2),
        "n_bearings": int(params["n_bearings"]),
        "ground_conductivity_mmho": round(params["ground_conductivity_mmho"], 2),
    }
