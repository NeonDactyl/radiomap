"""Tests for the USGS 3DEP tile naming scheme (local_dem.py).

Corner-case coverage matters here more than usual: this project's stations
span all four hemisphere combinations (mainland US and Alaska in the
northwest, Guam/Northern Mariana Islands in the northeast, American Samoa
in the southwest), and a wrong tile name means either a 404 or -- worse --
a silently wrong tile whose data doesn't actually cover the point.
"""
from app.geo.local_dem import tile_name


def test_matches_verified_real_tiles():
    # Confirmed directly against the actual S3 bucket in this session.
    assert tile_name(39.73277777777778, -105.23611111111111) == "n40w106"  # KWBL-FM, Denver CO
    assert tile_name(71.29194444444444, -156.78272222222225) == "n72w157"  # KAAC-FM, Utqiagvik AK


def test_northwest_hemisphere_mainland_us():
    # A point just inside a tile's south/east edge should still belong to
    # the tile named by the NEXT integer degree up (tiles are named by
    # their NW corner and cover one degree south/east of it).
    assert tile_name(34.999, -118.001) == "n35w119"
    assert tile_name(34.001, -118.999) == "n35w119"


def test_southern_hemisphere():
    # American Samoa
    assert tile_name(-14.3, -170.7) == "s15w171"


def test_eastern_hemisphere():
    # Guam
    assert tile_name(13.4, 144.8) == "n14e145"
