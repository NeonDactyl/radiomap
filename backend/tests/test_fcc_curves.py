"""Tests for the FCC F(50,50) curve lookup (fcc_curves.py).

The underlying data is transcribed programmatically (not by hand) from the
FCC's own reference implementation -- see fcc_curves.py's docstring for
provenance. These tests pin down: exact grid points come back unchanged
(catches a transcription or indexing bug), interpolated points behave
sensibly, and out-of-range inputs clamp rather than extrapolating wildly.
"""
from app.propagation.fcc_curves import DISTANCE_KM, FIELD_STRENGTH_DBU_PER_KW, HAAT_M, field_strength_dbu_per_kw


def test_table_shape():
    assert len(DISTANCE_KM) == 25
    assert len(HAAT_M) == 13
    assert len(FIELD_STRENGTH_DBU_PER_KW) == 25
    assert all(len(row) == 13 for row in FIELD_STRENGTH_DBU_PER_KW)


def test_exact_grid_points_are_unchanged():
    # Corners and a middle point, checked against the source Fortran DATA
    # statements directly (not re-derived).
    assert field_strength_dbu_per_kw(DISTANCE_KM[0], HAAT_M[0]) == 92.0
    assert field_strength_dbu_per_kw(DISTANCE_KM[-1], HAAT_M[-1]) == -2.0
    assert field_strength_dbu_per_kw(DISTANCE_KM[5], HAAT_M[5]) == 72.0  # 16.09km, 304.8m


def test_field_strength_decreases_with_distance():
    values = [field_strength_dbu_per_kw(d, 300.0) for d in [5, 20, 50, 100, 200, 300]]
    assert values == sorted(values, reverse=True)


def test_field_strength_increases_with_haat():
    low = field_strength_dbu_per_kw(100.0, 30.48)
    high = field_strength_dbu_per_kw(100.0, 1524.0)
    assert high > low


def test_out_of_range_inputs_clamp_to_table_edges():
    assert field_strength_dbu_per_kw(0.01, 30.48) == field_strength_dbu_per_kw(DISTANCE_KM[0], HAAT_M[0])
    assert field_strength_dbu_per_kw(10000.0, 30.48) == field_strength_dbu_per_kw(DISTANCE_KM[-1], HAAT_M[0])
    assert field_strength_dbu_per_kw(100.0, -50.0) == field_strength_dbu_per_kw(100.0, HAAT_M[0])
    assert field_strength_dbu_per_kw(100.0, 5000.0) == field_strength_dbu_per_kw(100.0, HAAT_M[-1])


def test_interpolated_point_is_between_its_bracketing_grid_values():
    # 50km sits between D50[8]=64.37... wait, check actual brackets: 50 is
    # between D50 index for 48.28 and 64.37. Just assert monotonic bracket
    # containment generically using the nearest lower/upper grid distances.
    d = 50.0
    lower = max(x for x in DISTANCE_KM if x <= d)
    upper = min(x for x in DISTANCE_KM if x >= d)
    haat = 300.0
    value = field_strength_dbu_per_kw(d, haat)
    v_lower = field_strength_dbu_per_kw(lower, haat)
    v_upper = field_strength_dbu_per_kw(upper, haat)
    lo, hi = sorted([v_lower, v_upper])
    assert lo <= value <= hi
