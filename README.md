# Radio Map

Maps AM/FM broadcast stations and predicts their expected listening range
from tower location, transmit power, and terrain.

## What it does

- Imports real US AM/FM station data (call sign, tower location, ERP/power,
  antenna height above average terrain) from the FCC's public station
  query databases.
- Looks up real terrain elevation along the path from each tower outward
  (via a free SRTM-based elevation API), so hills and mountains actually
  block or extend a station's reach.
- Predicts a coverage contour per station using free-space path loss plus
  terrain diffraction (line-of-sight blocking), rendered as a polygon on
  a Leaflet map.
- FM and AM use different physics (FM/VHF is line-of-sight + diffraction;
  AM/MW is a groundwave that mostly ignores terrain) -- see
  `backend/app/propagation/simple.py` for details and known limitations.
- Attaches a programming genre/format where one exists in Wikidata (the FCC
  itself doesn't track this -- it's not something it regulates).
- Caches computed coverage contours in SQLite (`coverage_cache`) so a
  repeat request for the same station/params is instant, and can
  precompute them ahead of time for many stations at once (see below).

## Architecture

```
backend/
  app/
    importers/     FCC AM/FM station data -> SQLite
    geo/            elevation lookups (+ tree-canopy hook, currently a stub)
    propagation/    the coverage-prediction models
    api/            FastAPI routes
    main.py         serves the API + the static frontend
frontend/           Leaflet map, plain HTML/CSS/JS, no build step
```

The propagation model is behind a small interface
(`propagation/base.py: PropagationModel`) specifically so the v1 "simple"
model (free-space + single-knife-edge diffraction) can later be swapped
for a full Longley-Rice / ITM implementation without touching the API or
frontend. See the "Known limitations / next steps" section below.

## Setup

Requires Python 3.11+.

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Import station data

The importer pulls from the FCC's legacy AM/FM query endpoints (still
live on `transition.fcc.gov`), one US state/territory at a time, and
writes into `backend/data/stations.db` (SQLite, gitignored).

```bash
# from backend/, with the venv active
python -m app.importers.cli --service both --states CA
python -m app.importers.cli --service both --states CA,NV,OR,WA
python -m app.importers.cli --service both --states all   # every state; slow, ~2min+
```

Re-running an import updates existing stations in place (matched by FCC
facility ID) rather than duplicating them, so it's safe to re-run
periodically to pick up license changes.

To attach genre/format data (from Wikidata, keyed by the same FCC facility
ID -- see "Known limitations" below on coverage):

```bash
python -m app.importers.cli --genres --skip-stations   # genres only, no re-fetch from FCC
python -m app.importers.cli --states CA --genres        # or do both in one run
```

## Run it

```bash
cd backend
source .venv/bin/activate
uvicorn app.main:app --reload
```

Then open **http://127.0.0.1:8000/**. The frontend is served directly by
the same FastAPI process (no separate frontend server or build step) --
`--reload` picks up backend code changes; frontend HTML/CSS/JS changes
take effect on browser refresh with no restart needed.

## Using it

1. Pick FM or AM (and optionally a genre) in the top bar, then pan/zoom the
   map to the area you care about -- stations load for whatever's currently
   on screen (zoom in past a point; a whole-country view is too broad to be
   a useful station list). "Jump to" a state is a one-shot shortcut that
   flies the map there; it doesn't pin the list to that state afterward.
2. Click a station on the map or in the sidebar list to see its details
   (power, HAAT, class, licensee, genre if known, etc), or type into the
   search box to find a station by call sign or city -- search looks
   nationwide regardless of the current map view, because a station's FCC
   city of license often isn't the market it actually serves (e.g. WJLI is
   licensed to Metropolis, IL but serves Paducah, KY, so panning to KY alone
   won't surface it).
3. Pick a signal-strength threshold, then "Show coverage" to draw the
   predicted coverage polygon. Thresholds are labeled **Local / Distant /
   Fringe**, matching radio-locator.com's published definitions (60/50/40
   dBu for FM; 2.0/0.5/0.15 mV/m of groundwave for AM, per their FAQ,
   converted to dBu) --
   not an FCC standard, chosen so contours here are comparable to what
   people already expect from that site. For AM stations you can also pick
   a ground-conductivity preset (affects groundwave range a lot). Max
   radius is left on "Auto" by default -- it's picked per station from
   actual ERP/HAAT (FM) or solved directly from the groundwave model (AM),
   because a single fixed radius either clips a powerful station's real
   coverage edge or wastes time searching way past a weak one's. Set it
   manually only if you want a specific search cutoff.

Terrain lookups hit a free public elevation API (Open-Meteo) on first use
per area and are cached afterward in `elevation_cache` inside the same
SQLite DB, so repeat coverage runs over the same region are fast. Open-Meteo
is shared/rate-limited, so `backend/app/geo/elevation.py` automatically
falls back to the USGS Elevation Point Query Service (US-only, authoritative
3DEP data, matches our FCC-only station coverage) when it gets rate-limited;
if *both* sources are unavailable you'll see an error in the coverage panel
-- wait a bit and retry. A cold "Show coverage" click on a new, weak/local
station can take ~5-15 seconds; a high-power/tall-tower FM station searches
a much wider radius (real 60 dBu contours for a 100kW station can be over
100km out) and can take up to ~40 seconds -- most of that is the per-bearing
terrain lookups, not the propagation math itself. See "Caching and
precomputing coverage" below for how to avoid paying that cost live.

## Caching and precomputing coverage

Every computed coverage contour is cached in SQLite (`coverage_cache`),
keyed by station + every parameter that affects the result (model,
threshold, radius, step, bearings, AM conductivity). A live request for
params that match an existing cache row returns instantly (the API
response has a `cached: true/false` field, and the UI shows "(cached)" in
the status line); a request with different params computes fresh and adds
a new cache row alongside the old one.

To avoid paying the first-computation cost live (e.g. so the map feels
instant for anyone browsing after setup), precompute coverage for many
stations ahead of time using the same default parameters the live endpoint
would pick:

```bash
# from backend/, with the venv active
python -m app.importers.precompute_coverage --states CA
python -m app.importers.precompute_coverage --service FM --states all --delay 0.5
```

It skips anything already cached, so it's safe to re-run (e.g. after
importing more states) without redoing work. `propagation/params.py` is
the single place both the live endpoint and this script resolve default
parameters from, so they can't drift apart -- a precomputed entry for a
station is guaranteed to be what a live request for that station (with no
overrides) would compute.

Changing the propagation model's math doesn't automatically invalidate old
cache rows -- the cache key includes `model.name`, not a hash of the code,
so a stale row from before a math change would otherwise look identical to
a fresh one. `SimpleFmModel`/`SimpleAmModel` version their `name` (e.g.
`simple_fm_v2`) specifically so this can't happen silently: bump the
version string in `propagation/simple.py` whenever you change the actual
calculations (not just default parameters), and old rows simply become
unreachable dead entries rather than wrong answers -- `DELETE FROM
coverage_cache;` cleans those up if you want the space back, but isn't
required for correctness.

## Known limitations / next steps

- **FM beyond-horizon over-prediction (fixed).** Was: this model checked
  each terrain sample against a straight line adjusted for earth-curvature
  bulge and applied loss only for the single worst obstruction found --
  with no separate term for the continuous extra attenuation real
  propagation experiences beyond the geometric radio horizon even with
  zero terrain relief. Measured effect: for KWBL-FM (Denver, 100kW, 408m
  HAAT) due north toward Cheyenne, WY (~170km, open terrain), the model
  predicted 66 dBu -- above even radio-locator.com's most permissive
  "fringe" threshold (40 dBu), which its own map doesn't reach at that
  distance. Fixed by adding the ITU-R P.526-14 §3.1.1 smooth-earth
  diffraction formula (equations 13-18b; ground conductivity/polarization
  factor β taken as 1, which the Recommendation gives as exact for
  horizontal polarization at all frequencies) as a second loss term beyond
  `smooth_earth_radio_horizon_km()`, summed with (not max()'d against) the
  existing terrain-specific knife-edge loss -- max() was tried first and
  discarded because it let the horizon term completely swamp real,
  already-confirmed mountain blocking past ~100km, making a mountain-facing
  and a clear bearing converge to identical numbers. Summing does mean the
  two terms likely double-count some shared geometry near the horizon
  (they're not perfectly independent), which is a known imprecision of this
  approximation, not a bug -- see `base.py: smooth_earth_diffraction_loss_db`
  and `tests/test_smooth_earth_diffraction.py` for the derivation, sourced
  formula, and the regression test pinned to the KWBL/Cheyenne measurement.
  This is still a simplified model, not the FCC's actual F(50,50) curves
  (47 CFR 73.313/73.699, empirically measured, not purely physics-derived)
  -- a real next step if more accuracy is needed, since those aren't a
  closed-form formula and would need a verified digitized source.
- **Propagation model is deliberately simple (v1) in general.** FM/VHF
  coverage uses free-space path loss plus a single worst-case knife-edge
  diffraction obstruction per path -- a legitimate but simplified physical
  model. It does not model multiple/cascaded diffraction or troposcatter.
  The natural next step is a full **Longley-Rice / ITM** implementation
  behind the same `PropagationModel` interface -- see
  `backend/app/propagation/base.py` and `simple.py` for the seam.
- **Coverage boundary per bearing is the last point before a *sustained*
  drop below threshold** (the next couple of samples also below it) --
  not the first drop, and not the farthest qualifying point anywhere on
  the bearing. The former tolerates a single anomalous low sample without
  prematurely ending the contour; an earlier version used "farthest
  qualifying point anywhere," which let a strong station's signal "see
  past" a genuinely blocked mountain stretch to a distant recovery pocket,
  producing near-perfect circles instead of the terrain-shaped contour the
  diffraction model was actually computing. See
  `backend/tests/test_propagation.py` for the regression tests.
- **AM groundwave is a rough approximation**, not the FCC's official
  curves (47 CFR 73.190, derived from Sommerfeld/GRWAVE integration). It's
  calibrated to one reference point and ignores terrain and night skywave
  interference entirely. Treat AM contours as a much rougher estimate than
  FM.
- **Directional antenna patterns aren't modeled -- every station is treated
  as radiating uniformly in all directions.** We import the FCC's
  `directional` flag for both AM and FM but don't use it: AM stations with
  multiple towers can have precisely shaped patterns (cardioid, multi-lobe,
  etc., from tower count/spacing/phasing), and directional FM stations
  radiate less than their nameplate ERP off-axis. Real coverage for a
  directional station will be smaller than predicted in its pattern's null
  directions and is otherwise accurately predicted in this model only
  along the bearing of *maximum* radiation. Fixing this means importing
  actual antenna pattern data (relative field vs. azimuth, typically ~360
  points) from the FCC's separate antenna pattern tables -- a real,
  scoped follow-up (new importer + a per-bearing ERP multiplier in the
  propagation models), not implemented here.
- **Tree cover is architected but not wired to real data.**
  `backend/app/geo/landcover.py` defines a `CanopyProvider` interface that
  the FM model already calls for every terrain sample point, but it's
  currently a stub returning zero canopy everywhere -- no free, reliably
  reachable canopy-height API was found while building this. Wiring in a
  real dataset (e.g. NLCD tree canopy, served from a locally downloaded
  raster) is a self-contained change to that one file.
- **HAAT reference point (fixed).** Was: the antenna's AMSL height was
  approximated as (ground elevation *at the tower* + HAAT). Per radio-locator's
  own FAQ (and the actual FCC definition), HAAT is antenna height above the
  *average* ground elevation 1.5-10 miles from the tower in each direction
  -- not the tower's own local elevation, which can differ substantially
  (confirmed for KWBL: the tower site sits 185m higher than the ring
  average, because it's built on a foothill slope with lower plains pulling
  the wider average down). `SimpleFmModel._average_terrain_elevation_m()`
  now computes the real ring average from actual elevation data (8 radials
  x 9 samples between 1.5-10mi) and uses that as the AMSL reference instead.
- **Elevation data depends on a free public API** (Open-Meteo, SRTM-based,
  ~90m resolution). It's rate-limited; heavy use (e.g. computing coverage
  for many stations back-to-back, or a nationwide import) may hit 429s.
  Swapping to locally downloaded SRTM tiles would remove this dependency
  and the rate-limit risk entirely -- `backend/app/geo/elevation.py` is
  the place to do that.
- Only primary FM/AM licensed stations are imported -- FM translators/
  boosters (FX/FL service codes) are skipped to avoid cluttering the map
  with low-power rebroadcasters. Non-US filings (the FCC query tool also
  returns foreign border-coordination stations, mostly Mexico) are filtered
  by `country == 'US'` -- a couple of them carried a garbage 2-letter
  "state" value that happened to collide with a real US state code, so
  this filter runs before the state field is trusted for anything.
- **Genre/format coverage is partial (~16% of stations).** Wikidata's
  "radio format" property (P415) is filled in for some US stations,
  skewed toward larger/more notable ones -- there's no comprehensive free
  source for this since the FCC doesn't track it. Most stations will show
  "format unknown," which is an honest reflection of the data, not a bug.
