"""The FCC's actual F(50,50) field-strength curve for FM (and TV channels
2-6, "Low VHF" -- FM shares this curve set), transcribed from the FCC's own
reference implementation of 47 CFR 73.333/73.699.

This is not a derived approximation: the data in
data/fcc_f5050_low_vhf.json is parsed directly (programmatically, not by
hand, to avoid transcription errors) from curves_subroutines.f, a Fortran
program originally written by FCC engineer Gary Kalagian and distributed
by FCC senior engineer Dale Bickel (Audio Division, Media Bureau) as the
public source for the Commission's own field-strength calculator --
retrieved via https://github.com/kate-harrison/west (a research project
that archived it; the Read_Me.txt included there gives the FCC's original
distribution URL). Per that file's own comments: "There is no equation to
generate the propagation curves... determined from a table of points...
developed on the basis of measured field strengths under various
conditions, primarily in the 1930s and 1940s."

The FCC's own tool interpolates this table using Akima's 1974 bivariate
interpolation algorithm (a compact, GOTO-heavy ~700-line Fortran routine
using EQUIVALENCE-based memory aliasing -- explicitly flagged by the
distributor's own Read_Me.txt as too error-prone to hand-translate).
Rather than risk a subtly wrong manual port of that specific routine, this
uses standard bilinear interpolation over the same real table instead.
That's a deliberate, documented difference from the FCC's exact algorithm
-- the *data* is authoritative, the *interpolation* is a reasonable
standard substitute, not a faithful reproduction of Akima's smoothing.

This table is direction-agnostic (a function of distance and HAAT only),
matching how the FCC's own official contours work -- by itself it would
draw a perfect circle for a given HAAT, the same way an official FCC
protected-service contour is a simple distance-per-radial, not a
terrain-aware shape. Real-world direction-dependent blocking (e.g. a
mountain range on one side of a station) is layered on top of this as a
separate, additional loss term -- see SimpleFmModel in simple.py.
"""
import json
from pathlib import Path

_DATA_PATH = Path(__file__).parent / "data" / "fcc_f5050_low_vhf.json"
_data = json.loads(_DATA_PATH.read_text())

DISTANCE_KM = _data["distance_km"]
HAAT_M = _data["haat_m"]
FIELD_STRENGTH_DBU_PER_KW = _data["field_strength_dbu_per_kw"]


def _bracket(axis: list[float], value: float) -> tuple[int, int, float]:
    """Returns (low_index, high_index, fraction) for linear interpolation,
    clamping to the axis range for out-of-bounds values (the FCC table
    only covers 1.6-322km and 30-1524m HAAT; values outside that aren't a
    case the F(50,50) method is meant to handle, so we clamp to the edge
    rather than extrapolate into unvalidated territory).
    """
    if value <= axis[0]:
        return 0, 0, 0.0
    if value >= axis[-1]:
        last = len(axis) - 1
        return last, last, 0.0
    for i in range(len(axis) - 1):
        if axis[i] <= value <= axis[i + 1]:
            span = axis[i + 1] - axis[i]
            frac = (value - axis[i]) / span if span > 0 else 0.0
            return i, i + 1, frac
    last = len(axis) - 1  # unreachable given the bounds checks above
    return last, last, 0.0


def field_strength_dbu_per_kw(distance_km: float, haat_m: float) -> float:
    """Bilinear interpolation of the FCC F(50,50) Low-VHF/FM table."""
    d_lo, d_hi, d_frac = _bracket(DISTANCE_KM, distance_km)
    h_lo, h_hi, h_frac = _bracket(HAAT_M, haat_m)

    v_lo = FIELD_STRENGTH_DBU_PER_KW[d_lo][h_lo] * (1 - h_frac) + FIELD_STRENGTH_DBU_PER_KW[d_lo][h_hi] * h_frac
    v_hi = FIELD_STRENGTH_DBU_PER_KW[d_hi][h_lo] * (1 - h_frac) + FIELD_STRENGTH_DBU_PER_KW[d_hi][h_hi] * h_frac
    return v_lo * (1 - d_frac) + v_hi * d_frac
