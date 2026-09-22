"""Regression tests for the coverage-contour boundary logic.

These use a fake PropagationModel with a hand-scripted field-strength
profile instead of real terrain/elevation, so they run instantly and
pin down exactly the boundary-selection behavior (not the physics).
"""
import pytest

from app.propagation.base import PropagationModel, Station


class ScriptedModel(PropagationModel):
    """Returns whatever field-strength array the test hands it, regardless
    of station or bearing -- isolates coverage_contour()'s boundary logic
    from elevation lookups and the real diffraction math.
    """
    name = "scripted"
    uses_terrain = False

    def __init__(self, strengths_by_distance: dict[float, float]):
        super().__init__(elevation_provider=None, canopy_provider=None)
        self.strengths_by_distance = strengths_by_distance

    def field_strengths_along_bearing(self, station, bearing_deg, distances_km):
        return [self.strengths_by_distance[d] for d in distances_km]


def make_station(**overrides) -> Station:
    defaults = dict(
        id=1, callsign="TEST", service="FM", frequency_mhz=100.0,
        erp_kw=100.0, haat_m=400.0, lat=39.7, lon=-105.2, directional=False,
    )
    defaults.update(overrides)
    return Station(**defaults)


def _single_bearing_distance(model, station, threshold_dbu, max_radius_km, step_km):
    contour = model.coverage_contour(
        station, threshold_dbu=threshold_dbu, max_radius_km=max_radius_km,
        step_km=step_km, n_bearings=1,
    )
    (lat, lon) = contour[0]
    # bearing 0 => straight north; degrees latitude convert to km directly
    return (lat - station.lat) * 111.0


def test_sustained_drop_ends_the_contour_even_if_signal_recovers_later():
    """This is the KWBL-FM regression: a station strong enough that its
    free-space signal briefly recovers above threshold far past a real,
    sustained mountain-range obstruction must NOT have its coverage
    boundary pulled out to that distant recovery pocket -- that produced
    near-perfect circles for high-power stations regardless of terrain.
    """
    step = 10.0
    distances = [step * i for i in range(1, 11)]  # 10..100 km
    # Strong out to 50km, blocked (sustained) 60-80km, a brief isolated
    # recovery at 90km, blocked again at 100km.
    strengths = {10: 90, 20: 85, 30: 80, 40: 75, 50: 70,
                 60: 40, 70: 35, 80: 30, 90: 60, 100: 20}
    model = ScriptedModel(strengths)
    station = make_station()

    boundary_km = _single_bearing_distance(model, station, threshold_dbu=54.0, max_radius_km=100.0, step_km=step)

    # Boundary should be the last qualifying distance BEFORE the sustained
    # drop (50km) -- not the isolated recovery at 90km.
    assert boundary_km == pytest.approx(50.0, abs=0.5)


def test_single_sample_dip_does_not_prematurely_end_the_contour():
    """A single below-threshold sample that immediately recovers (a small
    local dip, not real sustained blockage) shouldn't truncate the contour.
    """
    step = 10.0
    strengths = {10: 90, 20: 85, 30: 50, 40: 80, 50: 75, 60: 20, 70: 15, 80: 10, 90: 5, 100: 5}
    model = ScriptedModel(strengths)
    station = make_station()

    boundary_km = _single_bearing_distance(model, station, threshold_dbu=54.0, max_radius_km=100.0, step_km=step)

    # The dip at 30km is a single sample (40km recovers), so it shouldn't
    # end the contour; the real sustained drop starts at 60km, so the
    # boundary should be the last qualifying point before that: 50km.
    assert boundary_km == pytest.approx(50.0, abs=0.5)


def test_never_dropping_below_threshold_reaches_the_search_cap():
    step = 10.0
    strengths = {d: 90.0 for d in [step * i for i in range(1, 11)]}
    model = ScriptedModel(strengths)
    station = make_station()

    boundary_km = _single_bearing_distance(model, station, threshold_dbu=54.0, max_radius_km=100.0, step_km=step)

    assert boundary_km == pytest.approx(100.0, abs=0.5)
