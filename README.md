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
   dBu for FM; 2.5/0.5/0.15 mV/m of groundwave for AM, converted to dBu) --
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

- **FM coverage over long, open (non-mountainous) paths is measurably
  over-predicted -- this is the single biggest known accuracy gap.**
  Direct comparison against radio-locator.com for KWBL-FM (Denver, 100kW,
  408m HAAT): due north toward Cheyenne, WY (~170km, flat terrain, no
  obstruction), this model predicts 66 dBu -- above even radio-locator's
  most permissive "fringe" threshold (40 dBu) -- and stays above 40 dBu out
  past 300km. Real-world reception (and radio-locator's fringe contour)
  doesn't reach Cheyenne at all. This isn't a threshold-labeling issue (the
  thresholds are now radio-locator's own published values, see "Using it"
  above); the model is genuinely too optimistic at long range over clear
  terrain. Cause: this v1 model checks each terrain sample against a
  straight line adjusted for earth-curvature bulge and applies loss only
  for the single worst obstruction found (see below) -- it has no separate
  term for the continuous extra attenuation that real propagation
  experiences beyond the geometric radio horizon even with zero terrain
  relief (smooth-earth diffraction, atmospheric statistics baked into the
  FCC's real F(50,50) curves, etc.). The mountain-blocking physics is
  correctly differential (confirmed: KWBL's Front-Range bearings compute
  substantially shorter than its plains bearings), so this only shows up
  as an absolute over-prediction on the *open* side of a contour, not as a
  wrong-shaped one. Fixing this properly means either implementing the
  FCC's actual F(50,50)/F(50,10) propagation curves (47 CFR 73.313/73.699)
  or a real smooth-earth-diffraction term -- both nontrivial enough that
  they're better done as a deliberate follow-up with a verified data
  source than guessed at.
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
- **Tree cover is architected but not wired to real data.**
  `backend/app/geo/landcover.py` defines a `CanopyProvider` interface that
  the FM model already calls for every terrain sample point, but it's
  currently a stub returning zero canopy everywhere -- no free, reliably
  reachable canopy-height API was found while building this. Wiring in a
  real dataset (e.g. NLCD tree canopy, served from a locally downloaded
  raster) is a self-contained change to that one file.
- **HAAT is used as if it were the tower's physical AMSL height** (ground
  elevation at the tower + HAAT). Real HAAT is technically an average over
  a ring around the tower, not the tower's own terrain-relative height --
  a reasonable stand-in for a simple model, but a source of error right
  near the tower.
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
