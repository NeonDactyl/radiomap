# Radio Map

Maps AM/FM broadcast stations and predicts their expected listening range
from tower location, transmit power, and terrain.

## What it does

- Imports real US AM/FM station data (call sign, tower location, ERP/power,
  antenna height above average terrain) from the FCC's public station
  query databases.
- Looks up real terrain elevation along the path from each tower outward,
  so hills and mountains actually block or extend a station's reach.
  Primary source is downloaded USGS 3DEP DEM tiles read locally (see
  `backend/app/geo/local_dem.py`) -- no per-point network calls or rate
  limits once a tile is cached, and it covers areas the free point APIs
  don't (confirmed: parts of Alaska). Falls back to those point APIs
  (Open-Meteo, then USGS EPQS) only for a tile that can't be downloaded.
- Looks up real tree canopy cover too, read directly (no download, no API
  key) from the USDA Forest Service's national NLCD Tree Canopy Cover
  raster over HTTP range requests -- see `backend/app/geo/tree_canopy.py`.
  Feeds into the same terrain-obstruction geometry as elevation, so dense
  forest between a tower and a receiver adds to the blocking, not just
  bare ground height.
- Predicts a coverage contour per station, rendered as a polygon on a
  Leaflet map. Two selectable FM models (pick "FM physics" in the UI, or
  `?fm_model=simple|itm` on the API): **Simple** -- the FCC's own real
  F(50,50) field-strength curve (transcribed from the FCC's own reference
  implementation -- see `backend/app/propagation/fcc_curves.py`) as a
  baseline, with real single-direction terrain obstruction (DEM- and
  canopy-based knife-edge diffraction) layered on top; fast. **ITM** --
  actual Longley-Rice point-to-point physics (multiple diffraction,
  troposcatter, the LOS regime, not just one worst obstruction) via
  `itmlogic`, a peer-reviewed Python port of NTIA's official algorithm --
  see `backend/app/propagation/itm_model.py`; a bit slower, more
  complete physics. AM uses a simpler groundwave approximation. See
  `backend/app/propagation/simple.py` and `itm_model.py` for details and
  known limitations of all three.
- Attaches a programming genre/format where one exists in Wikidata (the FCC
  itself doesn't track this -- it's not something it regulates).
- Caches computed coverage contours in SQLite (`coverage_cache`) so a
  repeat request for the same station/params is instant, and runs a
  background seeder that slowly precomputes it for every station
  automatically (see "Caching and precomputing coverage" below).

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
(`propagation/base.py: PropagationModel`) so implementations are
swappable without touching the API or frontend -- which is how FM ended
up with two: the original "Simple" model (FCC curve baseline +
single-knife-edge diffraction) and a real Longley-Rice/ITM
implementation, selectable per-request (`fm_model=simple|itm`). AM still
uses a single groundwave approximation. See "The ITM model" under
"Validation against known-real numbers" and "Known limitations / next
steps" below.

## Setup

Requires Python 3.11+.

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

`rasterio` (elevation/canopy raster reading) installs its own bundled
GDAL via prebuilt wheels on common platforms (Linux/macOS/Windows,
standard Python versions) -- no separate system GDAL install needed in
the typical case.

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
take effect on browser refresh with no restart needed. The frontend always
calls the API through a relative `/api` path, so this same-origin setup
works unmodified under any domain a reverse proxy puts in front of it. If
you ever need the API reachable from a *different* origin than the one
serving it (a local frontend pointed at a deployed API, a preview URL,
etc), set `RADIO_MAP_ALLOWED_ORIGINS` (comma-separated) before starting
uvicorn -- unset, it allows any origin, which is fine for local dev:

```bash
RADIO_MAP_ALLOWED_ORIGINS=https://radiomap.example.com uvicorn app.main:app
```

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

The current map view and selected station are kept in the page URL
(`?bbox=...&station=...`), updated via `history.replaceState` as you pan
or select -- so the URL is always a bookmarkable/shareable link back to
what's currently on screen, and reloading or opening it fresh restores
that view (and re-selects the station, if any) without touching browser
history on every pan.

Terrain lookups download USGS DEM tiles on first use per area (see
`backend/app/geo/local_dem.py`) and read them locally from then on, so
repeat coverage runs over the same region are fast even across restarts
(tiles persist on disk in `backend/data/dem_tiles/`). If a tile can't be
downloaded, it falls back to Open-Meteo, then USGS EPQS (both point
APIs); if *all three* are unavailable you'll see an error in the coverage
panel -- wait a bit and retry. A cold "Show coverage" click on a new area
downloads however many 1-degree tiles the search radius spans (each
~40-50MB, a few seconds each); a weak/local station usually needs just
one, a high-power/tall-tower station's much wider search radius (real
60 dBu contours for a 100kW station can be over 100km out) can need a
handful. Once those tiles are on disk, recomputing the same or a nearby
station's coverage is fast regardless of parameters. See "Caching and
precomputing coverage" below for how to avoid paying even the first-time
cost live.

## Validation against known-real numbers

`backend/tests/test_fcc_curve_calibration.py` checks `fcc_curves.py`'s
bilinear interpolation against the FCC's own real interpolation algorithm
(Akima's, which we deliberately didn't hand-port -- see that file's
docstring), using 75 reference points generated by extracting and running
REC Networks' public JavaScript port of the FCC's actual curves.f/
curves_subroutines.f. Result: max error 5.6% across realistic ERP
(1-300kW), HAAT (30-1000m), and all three radio-locator.com threshold
tiers, concentrated where the underlying FCC table's own distance grid is
sparsest (8km to 16.1km in one jump) -- not a flaw in bilinear
interpolation itself, just where a coarser interpolant matters most. This
is real validation of the curve data and assembly (ERP scaling,
threshold-crossing search), not a spot-check: see that test file's
docstring for exactly how the reference data was generated, including a
real JavaScript quirk it cost time to work around (`curve == ''` is
`true` in JS when `curve` is the *number* 0, due to loose-equality
coercion -- never triggered from their real HTML form since a `<select>`
value is always a string, so the reference data had to be generated by
matching that convention).

One genuine, nuanced finding from this work, not swept under the rug:
comparing the *full* model (FCC curve + real terrain) against real,
verifiably flat-terrain stations (100kW Class C FM stations across
Kansas) shows the full model predicting **shorter** range than the pure
curve alone -- e.g. KCFN-FM (Wichita, KS): curve-only says ~81km to 50
dBu, the full model with real terrain says ~60-75km depending on
direction. Traced this to two compounding, individually legitimate
effects: (1) even "flat" Kansas has real, gentle elevation relief
(confirmed directly: ~416-470m over a 90km profile, not perfectly flat),
and (2) earth-curvature bulge grows large enough by ~50-60km (roughly
119m at the midpoint of a 90km path for this station's HAAT) that even
that modest real relief starts registering as measurable knife-edge loss.
Both are real, explainable physics -- but there's a genuine, unresolved
methodological question worth flagging: the FCC curve is itself an
empirical average across a great deal of mixed real US terrain, so it
likely already encodes *some* implicit long-range rolloff on average: our
added terrain-specific loss at long range could be partially
double-counting what the curve already represents, similar in spirit to
the earlier documented tradeoff between the (now-removed) ITU smooth-earth
term and knife-edge diffraction. Not fixed here because there isn't a
rigorous way to cleanly decompose "the curve's own average implicit
terrain effect" from "this specific path's real terrain effect" with the
data available -- flagged honestly rather than patched without a solid
basis.

That Kansas comparison only exercises the flat case -- the FCC curve is
direction-agnostic by construction, so it can't validate the
terrain-specific knife-edge diffraction piece at all.
`backend/tests/test_complex_terrain_calibration.py` (marked
`integration`; skipped by default, see below) does that instead, against
real Colorado and Utah mountain terrain:

- **KBCO-FM** (Boulder, CO) is radio-locator.com's own FAQ example of
  terrain asymmetry, independently authored, not derived from anything
  in this project: "has relatively flat land to the east, but mountains
  to the west... can transmit much farther to the east than to the
  west." The model reproduces that pattern -- east (~85-105km at 50 dBu)
  roughly double west (~35-55km) across a full 24-bearing sweep.
- **KCYN-FM** (Moab, UT) has no equivalent published claim, so this
  checks the result against the actual downloaded terrain data directly:
  confirmed the bearing with the shortest predicted range (21-30km, vs.
  75-105km in open directions) points toward a profile that climbs 768m
  within 6km of the tower -- the La Sal Mountains, which rise abruptly
  immediately east/southeast of Moab, among the most dramatic close-in
  ranges near any US town. The model's shortest-range bearing landing
  exactly there, not some arbitrary direction, is the actual test.

### The ITM model: real diffraction physics, not an approximation of it

Everything above validates the *Simple* model (FCC curve + single-worst-
obstruction knife-edge) as a good implementation of what it is -- a
deliberately simplified approximation. It's still an approximation,
documented as such throughout this README. The **ITM** model
(`backend/app/propagation/itm_model.py`) is the actual alternative:
Longley-Rice, the real industry-standard terrain propagation physics
(multiple diffraction, troposcatter, the LOS regime), via `itmlogic`
(github.com/edwardoughton/itmlogic) -- a Python port of NTIA's official
ITM v1.2.2 algorithm, published and peer-reviewed in the Journal of Open
Source Software (Oughton et al., 2020, DOI 10.21105/joss.02266).

Verified directly, not trusted on the paper's say-so: ran itmlogic's own
pinned test case -- the classic Longley-Rice reference path (Crystal
Palace, South London to Mursley, Buckinghamshire, traced to Stark 1967,
not just self-referential) -- and got an exact, bit-for-bit match on all
6 of their pinned expected transmission-loss values (see
`backend/tests/test_itm_calibration.py`). Also caught and fixed a real
off-by-one bug introduced while writing that test (range-step computed as
`distance/n` instead of `distance/(n-1)`) precisely because the pinned
reference values caught it -- the kind of error a "looks plausible"
spot-check would have missed entirely.

Compared directly against the Simple model on KBCO-FM (the same
radio-locator-cited station above): ITM predicts **consistently higher**
field strength on the open (east) side -- up to +25 dB at 80km -- and
more complex, non-monotonic behavior on the mountain (west) side (real
diffraction/multipath over specific terrain, not a smooth curve). The
east-side gap is independent corroboration of the "possible
double-counting" concern flagged in the Kansas section above: Simple's
extra terrain-specific loss stacked on top of a curve that may already
represent some average terrain effect looks like it *is* somewhat
overcautious on open ground, exactly where ITM (real physics, no such
stacking) predicts more range.

Performance: ITM's own computation is cheap (~10-25ms per point); the
per-point terrain profile fetch (same elevation pipeline as Simple)
dominates. A full 24-bearing sweep on a completely cold cache took ~20s
in testing -- comparable to Simple, not meaningfully slower in practice.

Not (yet) the default: this is a substantial, newly-built integration,
and its results differ meaningfully from Simple's (see above) -- exposed
as an explicit, selectable choice (`fm_model=simple|itm`, defaulting to
`simple`) rather than silently changing what every existing user sees.
Ground/atmosphere parameters (permittivity, conductivity, climate zone,
refractivity) are fixed at reasonable US-average defaults, not
region-specific -- a real, documented simplification, same spirit as the
AM model's single "average ground" constant.

## Running the integration tests

Most of the test suite (`pytest tests/`) is fast, pure-logic, and has no
network dependency, by design (see each test file's docstring for how
its real-world reference data was generated ahead of time). A few tests
are marked `integration` and excluded by default (`pytest.ini`'s
`addopts`) because they hit the real elevation/terrain pipeline directly
-- slow, and dependent on whatever's actually reachable when you run
them. Run them explicitly with:

```bash
python -m pytest tests/ -m integration -v
```

## Caching and precomputing coverage

Every computed coverage contour is cached in SQLite (`coverage_cache`),
keyed by station + every parameter that affects the result (model,
threshold, radius, step, bearings, AM conductivity). A live request for
params that match an existing cache row returns instantly (the API
response has a `cached: true/false` field, and the UI shows "(cached)" in
the status line); a request with different params computes fresh and adds
a new cache row alongside the old one.

To avoid paying the first-computation cost live (e.g. so the map feels
instant for anyone browsing after setup), precompute coverage ahead of
time -- either automatically, or on demand:

**Automatic**: `backend/app/background_seeder.py` starts a daemon thread
with the server (set `RADIO_MAP_DISABLE_SEEDER=1` to turn it off) that
slowly works through every imported station in the background, pacing
itself (0.75s between FM attempts -- AM needs no throttling, it's pure
math with no terrain dependency) so it doesn't hammer the free elevation
APIs. Check progress at `GET /api/meta/seed-status`
(`{running, total_stations, processed, newly_computed, already_cached,
errors, current, caught_up}`). It idles 10 minutes between full passes so
newly-imported stations eventually get picked up without a restart. Real
regional data gaps (confirmed: parts of Alaska return no response at all
from either elevation source) will show up as `errors`, not silently
hang -- see "Known limitations" for the circuit-breaker that makes that
possible instead of a multi-minute stall per bad station.

**On demand**: the same underlying logic as a one-shot CLI run, useful
for prioritizing specific states instead of waiting for the seeder to
reach them:

```bash
# from backend/, with the venv active
python -m app.importers.precompute_coverage --states CA
python -m app.importers.precompute_coverage --service FM --states all --delay 0.5
```

Both skip anything already cached, so either is safe to re-run (e.g.
after importing more states) without redoing work. `propagation/params.py`
is the single place the live endpoint, the seeder, and the CLI script all
resolve default parameters from, so none of them can drift apart -- a
precomputed entry for a station is guaranteed to be what a live request
for that station (with no overrides) would compute.

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

- **FM beyond-horizon over-prediction (fixed, twice).** Originally: this
  model checked each terrain sample against a straight line adjusted for
  earth-curvature bulge and penalized only the single worst obstruction
  found, with nothing accounting for the continuous extra attenuation real
  propagation experiences beyond the radio horizon even over zero terrain
  relief. Measured for KWBL-FM (Denver, 100kW, 408m HAAT) due north toward
  Cheyenne, WY (~170km, open terrain): predicted 66 dBu, above even
  radio-locator.com's most permissive "fringe" threshold (40 dBu), which
  its own map doesn't reach that far. First fix added an ITU-R P.526
  smooth-earth diffraction term (still in `base.py` and covered by
  `tests/test_smooth_earth_diffraction.py`, though no longer used by
  `SimpleFmModel` -- see below). That was superseded by a better fix: the
  FCC's actual F(50,50) field-strength curve (see next bullet) replaced
  the free-space+smooth-earth-diffraction baseline entirely, since it's
  the real, empirically-measured data rather than a physics approximation
  of it. Real terrain-specific knife-edge diffraction is layered on top of
  that curve, unchanged in approach. Current result for the same
  reference point: 31.8 dBu at Cheyenne, below the fringe threshold.
- **FM propagation baseline is the FCC's own F(50,50) curve, not a derived
  approximation.** `backend/app/propagation/fcc_curves.py` embeds data
  transcribed (programmatically, not by hand) from the FCC's own
  reference Fortran implementation of 47 CFR 73.333/73.699 -- see that
  file's docstring for exact provenance and a link. Two honest caveats:
  (1) the FCC's tool interpolates this table with a specific 1974 Akima
  bivariate algorithm; porting that exact ~700-line routine (dense,
  GOTO-heavy, using Fortran `EQUIVALENCE` memory aliasing -- explicitly
  flagged by the code's own distributor as too risky to hand-translate)
  seemed like a worse trade than using the same real data with standard
  bilinear interpolation, which is what this does instead -- a deliberate,
  documented difference from the FCC's exact algorithm, not a full
  reproduction of it. (2) The FCC curve itself is direction-agnostic (a
  function of distance and HAAT only) -- exactly like an official FCC
  protected-service contour, which is a simple per-radial distance, not a
  terrain-aware shape. Real single-direction blocking (why a station
  reaches further over open plains than into a mountain range) still
  comes entirely from the separate real-terrain knife-edge diffraction
  layered on top, which only checks the single worst obstruction per path
  and doesn't model multiple/cascaded diffraction or troposcatter. **This
  is now addressed by the ITM model** (`fm_model=itm`) -- real
  Longley-Rice physics behind the same `PropagationModel` interface, not
  a future item anymore -- see "The ITM model" section above for what it
  is and how it was validated. Simple remains the default (fast,
  well-calibrated for the flat case) since ITM is a newer, less
  battle-tested integration; not yet promoted to default pending more
  real-world comparison.
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
- **Tree cover (fixed).** Was: `backend/app/geo/landcover.py`'s
  `CanopyProvider` interface existed and the FM model already called it
  for every terrain sample point, but it was a stub returning zero canopy
  everywhere -- no free, reliably reachable canopy-height API had been
  found. Fixed: `backend/app/geo/tree_canopy.py` reads the USDA Forest
  Service's national NLCD Tree Canopy Cover raster (percent cover, 30m,
  most recent year) directly over HTTP -- no download, no API key. The
  raster is published as a ~3.6GB zip with the GeoTIFF stored
  *uncompressed* inside it (confirmed directly), which means GDAL can
  fetch just the specific internal tiles a read touches via HTTP range
  requests (`/vsizip/vsicurl/...`) instead of downloading the whole file.
  Verified against known geography: rural West Virginia forest reads
  ~77% cover, eastern Colorado plains reads 0%. Percent cover isn't
  itself an obstruction height, so it's scaled against a nominal 18m
  mature-tree height as an explicit, documented approximation -- see
  `NOMINAL_TREE_HEIGHT_M` in that file. Per-point reads (not one big
  bounding-box read) turned out much faster in practice: 40 profile
  points along a real 150km bearing read in 1.5s relying on GDAL's own
  block cache for points sharing a tile, versus 8.5s for one rectangular
  window covering the same span.
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
- **Elevation now comes from local USGS 3DEP DEM tiles, not point APIs
  (fixed).** Was: every point lookup was a network call to Open-Meteo
  (rate-limited under heavy use) falling back to USGS EPQS -- and USGS
  EPQS turned out to have real regional gaps, not just slowness: confirmed
  directly, parts of Alaska returned no response at all. A batch of up to
  100 points retried every one individually at full cost before giving
  up, worst case several minutes for one bad batch. Fixed properly:
  `backend/app/geo/local_dem.py` downloads USGS's own public,
  unauthenticated 1x1-degree DEM tiles (1 arc-second / ~30m resolution,
  actually finer than Open-Meteo's 90m) from S3
  (`prd-tnm.s3.amazonaws.com`, verified directly, including the exact
  Alaska tile that the point APIs couldn't serve) and reads elevation
  locally from then on -- no per-point network calls, no rate limits,
  after the one-time tile download. The old point-API code
  (`geo/elevation.py`) is kept as a fallback for a tile that can't be
  downloaded (a genuine 3DEP coverage gap, or a transient failure
  fetching the tile itself), circuit-breaker included. Tradeoff: tiles
  are ~40-50MB each and accumulate in `backend/data/dem_tiles/`
  (gitignored) as new areas get queried -- expect multiple GB over time
  for nationwide use, which is the point (local, permanent, fast) but
  worth knowing about if disk space is tight.
- **Offshore points (fixed).** A high-power coastal station's search
  radius often extends out over open water, where 3DEP has no tile at
  all (confirmed directly: 404 for the specific tile off the coast near
  KQED-FM, San Francisco). Originally this fell through to the network
  fallback, which *also* has no data there -- and every such tile paid
  the fallback's full retry/timeout budget before giving up, so a station
  whose radius crossed several offshore tiles timed out past 2 minutes
  instead of failing fast. Fixed: a 404 (3DEP's catalog confirming the
  tile doesn't exist, not just a failed download) is treated as strong
  enough evidence of open water on its own -- 3DEP's *land* coverage is
  near-complete, so a confirmed absence very likely means water, not a
  real gap. Skips the fallback entirely and assumes sea level (0m) for
  that tile. A tile that merely *fails to download* (network error,
  unknown status -- could well be real land) is not treated this way and
  still goes through the fallback / raises normally; see
  `LocalDemProvider.get_elevations` and
  `tests/test_local_dem_ocean_fallback.py` for the distinction and the
  regression tests pinning it down.
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
