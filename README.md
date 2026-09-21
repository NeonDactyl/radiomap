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

1. Pick FM or AM and a state in the top bar.
2. Click a station on the map or in the sidebar list to see its details
   (power, HAAT, class, licensee, etc), or type into the search box to find
   a station by call sign or city -- search looks nationwide regardless of
   the selected state, because a station's FCC city of license often isn't
   the market it actually serves (e.g. WJLI is licensed to Metropolis, IL
   but serves Paducah, KY, so it won't show up under KY otherwise).
3. Pick a signal-strength threshold and max radius, then "Show coverage"
   to draw the predicted coverage polygon. For AM stations you can also
   pick a ground-conductivity preset (affects groundwave range a lot).

Terrain lookups hit a free public elevation API (Open-Meteo) on first use
per area and are cached afterward in `elevation_cache` inside the same
SQLite DB, so repeat coverage runs over the same region are fast. Open-Meteo
is shared/rate-limited, so `backend/app/geo/elevation.py` automatically
falls back to the USGS Elevation Point Query Service (US-only, authoritative
3DEP data, matches our FCC-only station coverage) when it gets rate-limited.
A cold "Show coverage" click on a new area can take ~5-15 seconds; if
*both* sources are unavailable you'll see an error in the coverage panel --
wait a bit and retry.

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
  with low-power rebroadcasters.
