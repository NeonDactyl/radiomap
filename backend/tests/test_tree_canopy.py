"""Tests for the pure pixel-value -> obstruction-height conversion in
tree_canopy.py. Doesn't touch the network (the actual remote raster read
was validated manually against known reference points -- see the
session that built this: KWBL-area foothills ~22% -> 3.96m, rural WV
forest ~77% -> 13.86m, eastern CO plains 0% -> 0m -- these tests just pin
down the conversion formula and edge-case handling).
"""
from app.geo.tree_canopy import NODATA_VALUE, NOMINAL_TREE_HEIGHT_M, pixel_value_to_height_m


def test_zero_percent_is_zero_height():
    assert pixel_value_to_height_m(0) == 0.0


def test_full_percent_is_nominal_height():
    assert pixel_value_to_height_m(100) == NOMINAL_TREE_HEIGHT_M


def test_scales_linearly_with_percent():
    assert pixel_value_to_height_m(50) == NOMINAL_TREE_HEIGHT_M * 0.5


def test_matches_verified_real_values():
    # From manual verification against the real remote raster.
    assert pixel_value_to_height_m(22) == 22 / 100 * NOMINAL_TREE_HEIGHT_M
    assert pixel_value_to_height_m(77) == 77 / 100 * NOMINAL_TREE_HEIGHT_M


def test_nodata_sentinel_is_zero_not_a_huge_height():
    # NODATA_VALUE (255) must not be misread as "255% cover".
    assert pixel_value_to_height_m(NODATA_VALUE) == 0.0


def test_out_of_range_values_are_clamped():
    assert pixel_value_to_height_m(150) == NOMINAL_TREE_HEIGHT_M
    assert pixel_value_to_height_m(-5) == 0.0
