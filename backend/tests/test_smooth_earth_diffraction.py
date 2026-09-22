"""Regression tests for the ITU-R P.526 smooth-earth diffraction term
(base.py: smooth_earth_diffraction_loss_db / smooth_earth_radio_horizon_km).

This is the fix for a real, measured bug: without it, a 100kW/408m-HAAT
FM station (KWBL, Denver CO) predicted field strength above
radio-locator.com's most permissive "fringe" threshold (40 dBu) out past
300km over open terrain, while radio-locator's own fringe contour doesn't
reach even 170km in that direction. These tests pin down the physics
formula itself (pure math, no elevation/network dependency) rather than
re-deriving the real-world comparison each time.
"""
import math

from app.propagation.base import smooth_earth_diffraction_loss_db, smooth_earth_radio_horizon_km


def test_horizon_grows_with_antenna_height():
    short = smooth_earth_radio_horizon_km(h1_m=30.0, h2_m=9.0)
    tall = smooth_earth_radio_horizon_km(h1_m=408.0, h2_m=9.0)
    assert tall > short


def test_horizon_matches_standard_formula():
    horizon = smooth_earth_radio_horizon_km(h1_m=408.0, h2_m=9.0)
    expected = 4.122 * (math.sqrt(408.0) + math.sqrt(9.0))
    assert abs(horizon - expected) < 0.01


def test_diffraction_loss_increases_with_distance_beyond_horizon():
    losses = [
        smooth_earth_diffraction_loss_db(h1_m=408.0, h2_m=9.0, d_km=d, freq_mhz=106.7)
        for d in (100, 150, 200, 250, 300)
    ]
    assert losses == sorted(losses)  # strictly non-decreasing
    assert losses[-1] > losses[0] + 20  # substantial, not a rounding-level difference


def test_diffraction_loss_grows_well_beyond_the_horizon():
    """The formula is the first term of a residue series, valid near and
    beyond the horizon -- it doesn't claim to be ~0 right at the marginal
    LoS distance, just accurate there (per the Recommendation, to ~2dB).
    What matters is it keeps growing substantially further out.
    """
    horizon = smooth_earth_radio_horizon_km(h1_m=408.0, h2_m=9.0)
    loss_at_horizon = smooth_earth_diffraction_loss_db(408.0, 9.0, horizon, 106.7)
    loss_well_beyond = smooth_earth_diffraction_loss_db(408.0, 9.0, horizon * 2, 106.7)
    assert loss_well_beyond > loss_at_horizon + 20


def test_taller_tower_reduces_loss_at_a_fixed_long_distance():
    """A taller HAAT should extend usable range -- confirms the height-gain
    term (G(Y1)) is wired in with the right sign.
    """
    d = 200.0
    loss_short_tower = smooth_earth_diffraction_loss_db(30.0, 9.0, d, 106.7)
    loss_tall_tower = smooth_earth_diffraction_loss_db(408.0, 9.0, d, 106.7)
    assert loss_tall_tower < loss_short_tower


def test_matches_measured_kwbl_cheyenne_reference_point():
    """Regression pin for the real-world case that exposed the bug: KWBL-FM
    (100kW, 408m HAAT) due north toward Cheyenne, WY (~170km, open terrain).
    Before this fix, predicted field strength there was 66.2 dBu -- above
    radio-locator's fringe threshold (40 dBu), which its own map does not
    reach at that distance. After: field strength here should be well
    below 40 dBu, consistent with real-world reception.
    """
    from app.propagation.base import free_space_field_strength_dbu

    d = 170.0
    free_space = free_space_field_strength_dbu(erp_kw=100.0, distance_km=d)
    loss = smooth_earth_diffraction_loss_db(h1_m=408.0, h2_m=9.0, d_km=d, freq_mhz=106.7)
    field_strength = free_space - loss

    assert field_strength < 40.0
