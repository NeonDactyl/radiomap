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
   predicted coverage polygon. For AM stations you can also pick a
   ground-conductivity preset (affects groundwave range a lot). Max radius
   is left on "Auto" by default -- it's picked per station from actual
   ERP/HAAT (FM) or solved directly from the groundwave model (AM), because
   a single fixed radius either clips a powerful station's real coverage
   edge or wastes time searching way past a weak one's. Set it manually
   only if you want a specific search cutoff.

Terrain lookups hit a free public elevation API (Open-Meteo) on first use
per area and are cached afterward in `elevation_cache` inside the same
SQLite DB, so repeat coverage runs over the same region are fast. Open-Meteo
is shared/rate-limited, so `backend/app/geo/elevation.py` automatically
falls back to the USGS Elevation Point Query Service (US-only, authoritative
3DEP data, matches our FCC-only station coverage) when it gets rate-limited;
if *both* sources are unavailable you'll see an error in the coverage panel
-- wait a bit and retry. A cold "Show coverage" click on a new, weak/local
station can take ~5-15 seconds; a high-power/tall-tower FM station searches
a much wider radius (real 54 dBu contours for a 100kW station can be
150-250km out) and can take up to ~40 seconds -- most of that is the
per-bearing terrain lookups, not the propagation math itself.

## Known limitations / next steps

- **Propagation model is deliberately simple (v1).** FM/VHF coverage uses
  free-space path loss plus a single worst-case knife-edge diffraction
  obstruction per path -- a legitimate but simplified physical model. It
  does not model multiple diffraction, troposcatter, or receiver noise
  floor, so very weak stations over flat/open terrain can show
  unrealistically large contours at low dBu thresholds (raise the
  threshold to compensate). The natural next step is a full **Longley-Rice
  / ITM** implementation behind the same `PropagationModel` interface --
  see `backend/app/propagation/base.py` and `simple.py` for the seam.
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
