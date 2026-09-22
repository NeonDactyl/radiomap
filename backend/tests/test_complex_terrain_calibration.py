"""Validates the terrain-specific knife-edge diffraction piece of the
model (the part test_fcc_curve_calibration.py's flat-Kansas comparison
against the FCC's own curve can't exercise, since that curve is
direction-agnostic by construction) against real, complex mountain/canyon
terrain in Colorado and eastern Utah.

There's no precisely-numeric, independently-published ground truth for
mountain-terrain coverage the way there was for the flat-terrain FCC
curve case (radio-locator.com's maps are images, not extractable data).
What's used instead is real, independently-stated geography:

KBCO-FM (Boulder, CO, 100kW, 469m HAAT) is radio-locator.com's own FAQ
example of terrain asymmetry -- verbatim: "has relatively flat land to
the east, but mountains to the west. This means that it can transmit
much farther to the east than to the west." That's an independently
authored claim about this exact station, not something derived from our
own model, so reproducing the same pattern is real corroboration.

KCYN-FM (Moab, UT, 29kW, 394m HAAT) has no such published statement, so
this instead checks the result against the actual downloaded terrain
data directly (not just "looks plausible"): confirmed the bearing with
the shortest predicted range climbs 768m within 6km of the tower (2739m
to 3507m -- the La Sal Mountains, which rise abruptly immediately
east/southeast of Moab, among the most dramatic close-in mountain ranges
near any US town), while a long-range bearing's profile drops to 1200-
1800m of genuinely open canyon/desert terrain.

These are integration tests (they hit the real elevation/terrain
pipeline, no mocking) and will be slow and network-dependent on a cold
cache. Marked so they can be skipped in fast test runs.
"""
import pytest

from app.geo.elevation import elevation_provider
from app.geo.landcover import canopy_provider
from app.propagation.simple import SimpleFmModel
from app.propagation.base import Station

pytestmark = pytest.mark.integration


def _field_strength_at(station, bearing_deg, target_distance_km, step_km=2.0):
    """field_strengths_along_bearing()'s knife-edge check only inspects
    *intermediate* points between the tower and each requested distance --
    it doesn't independently re-sample the profile per call. Requesting
    only the single far-end distance (as an earlier version of this test
    mistakenly did) gives it nothing in between to find an obstruction
    with, silently producing a free-space-only answer regardless of real
    terrain. This samples the whole profile up to the target, matching
    how coverage_contour() actually drives the model.
    """
    model = SimpleFmModel(elevation_provider, canopy_provider)
    n_steps = max(int(target_distance_km / step_km), 1)
    distances = [step_km * i for i in range(1, n_steps + 1)]
    return model.field_strengths_along_bearing(station, bearing_deg, distances)[-1]


def test_kbco_reaches_much_further_east_than_west():
    """Reproduces radio-locator.com's own stated claim about this exact
    station: much farther east (flat) than west (mountains).
    """
    station = Station(
        id=3385, callsign="KBCO", service="FM", frequency_mhz=97.3,
        erp_kw=100.0, haat_m=469.0, lat=39.91330555555555, lon=-105.29275, directional=False,
    )
    distance = 60.0  # within range in both directions; a fair strength comparison

    east = _field_strength_at(station, 90.0, distance)
    west = _field_strength_at(station, 270.0, distance)

    # East (flat) must be a clearly, substantially stronger signal than
    # west (mountains) at the same distance -- not just numerically
    # greater, since real terrain noise could produce a marginal edge
    # either way by chance.
    assert east > west + 15, f"expected east >> west at {distance}km, got east={east:.1f} west={west:.1f}"


def test_kcyn_moab_shortest_bearing_faces_the_real_la_sal_mountains():
    """The bearing with the shortest predicted range should point toward
    real, confirmed steep terrain (La Sal Mountains, verified directly
    against downloaded elevation data -- see module docstring), not some
    arbitrary/wrong direction, and should be dramatically shorter than a
    bearing toward open terrain.
    """
    station = Station(
        id=15547, callsign="KCYN", service="FM", frequency_mhz=100.7,
        erp_kw=29.0, haat_m=394.0, lat=38.526916666666665, lon=-109.3065, directional=False,
    )
    distance = 25.0  # well within the La Sal Mountains' immediate blocking zone

    la_sal_direction = _field_strength_at(station, 90.0, distance)  # due east: confirmed steep rise nearby
    open_direction = _field_strength_at(station, 270.0, distance)  # due west: confirmed open terrain

    assert la_sal_direction < open_direction - 15, (
        f"expected the La Sal (east) direction to be much weaker than the open (west) one "
        f"at {distance}km, got east={la_sal_direction:.1f} west={open_direction:.1f}"
    )


def test_kcyn_terrain_profile_confirms_the_la_sal_mountains_are_really_there():
    """Pins the actual elevation data this whole test file's reasoning
    depends on, independent of the propagation model -- if this ever
    fails, the terrain data pipeline changed, not just the physics.
    """
    from app.propagation.base import destination_point

    lat0, lon0 = 38.526916666666665, -109.3065
    tower_elev = elevation_provider.get_elevation(lat0, lon0)

    lat6, lon6 = destination_point(lat0, lon0, 90.0, 6.0)
    elev_6km_east = elevation_provider.get_elevation(lat6, lon6)

    assert elev_6km_east - tower_elev > 500, (
        f"expected a dramatic rise toward the La Sal Mountains within 6km east of Moab, "
        f"got tower={tower_elev:.0f}m, +6km east={elev_6km_east:.0f}m"
    )
