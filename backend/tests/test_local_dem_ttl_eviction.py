"""Regression coverage for a real production issue: LocalDemProvider used
to cache every decoded DEM tile array in memory forever (never evicted),
which OOM'd a long-running server once the background seeder had walked
enough of the country to accumulate a few hundred tiles (tens of MB each)
resident in RAM at once. Fixed with a TTL on the in-memory copy -- the
on-disk .tif download itself is unaffected and still kept forever.

These tests exercise the eviction/sweep logic directly (via monkeypatched
time and pre-populated cache entries) rather than through a real tile
download, so they don't need rasterio or the network.
"""
import time

from app.geo.local_dem import SWEEP_INTERVAL_S, TILE_CACHE_TTL_S, LocalDemProvider


def _provider():
    return LocalDemProvider(tile_dir=None, fallback_provider=None)


def test_evict_expired_locked_removes_only_entries_past_the_ttl():
    provider = _provider()
    now = 10_000.0
    provider._datasets = {
        "n40w106": ("fake-array-old", "fake-transform", now - TILE_CACHE_TTL_S - 1),
        "n41w106": ("fake-array-fresh", "fake-transform", now - 5),
        "n42w106": ("fake-array-boundary", "fake-transform", now - TILE_CACHE_TTL_S),
    }

    provider._evict_expired_locked(now)

    assert set(provider._datasets.keys()) == {"n41w106"}


def test_get_tile_array_refreshes_last_used_on_a_cache_hit():
    provider = _provider()
    # Real (not monkeypatched) time.monotonic() here, so both the entry's
    # age and _last_sweep need to be relative to *now*, not an arbitrary
    # small constant -- otherwise the sweep this triggers would see the
    # entry as impossibly old (now - 1000.0 is huge on a real clock) and
    # evict it before the cache-hit path ever gets to read it back.
    now = time.monotonic()
    provider._datasets = {"n40w106": ("array", "transform", now)}
    provider._last_sweep = now  # avoid triggering a sweep for this check

    array, transform = provider._get_tile_array("n40w106")

    assert (array, transform) == ("array", "transform")
    _, _, last_used = provider._datasets["n40w106"]
    assert last_used >= now  # bumped to "now" (time.monotonic()), not left stale


def test_get_tile_array_evicts_an_idle_tile_when_a_sweep_is_due(monkeypatch):
    provider = _provider()
    t = [0.0]
    monkeypatch.setattr("app.geo.local_dem.time.monotonic", lambda: t[0])

    provider._datasets = {"n40w106": ("array", "transform", 0.0)}
    provider._last_sweep = 0.0

    # Not yet due for a sweep, and not yet past the TTL either -- must survive.
    t[0] = SWEEP_INTERVAL_S + 1
    provider._maybe_sweep(t[0])
    assert "n40w106" in provider._datasets

    # Past the TTL, and now due for another sweep -- must be evicted.
    t[0] = TILE_CACHE_TTL_S + SWEEP_INTERVAL_S * 2
    provider._maybe_sweep(t[0])
    assert "n40w106" not in provider._datasets


def test_maybe_sweep_does_nothing_before_the_sweep_interval_elapses():
    provider = _provider()
    now = 5000.0
    provider._last_sweep = now
    # An entry that's already well past its TTL...
    provider._datasets = {"n40w106": ("array", "transform", now - TILE_CACHE_TTL_S - 100)}

    # ...but a sweep isn't due yet, so it must be left alone for now.
    provider._maybe_sweep(now + 1)

    assert "n40w106" in provider._datasets
