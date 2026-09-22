import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

# Public origin(s) this app is served from in production, e.g.
# "https://radiomap.example.com" -- comma-separated if there's more than one
# (an apex + www, say). The frontend always calls the API with a relative
# path (see frontend/app.js's `API` const), so same-origin deployment needs
# no CORS at all; this only matters if the API is ever reached from a
# different origin than the one serving it (a local frontend pointed at a
# deployed API, a preview URL, etc). Unset (the default) allows any origin,
# which is fine for local dev.
ALLOWED_ORIGINS = [
    origin.strip().rstrip("/")
    for origin in os.environ.get("RADIO_MAP_ALLOWED_ORIGINS", "").split(",")
    if origin.strip()
] or ["*"]

DB_PATH = DATA_DIR / "stations.db"

FRONTEND_DIR = BASE_DIR.parent / "frontend"

# FCC legacy query CGI endpoints. Still live on transition.fcc.gov; return
# fixed-column pipe-delimited text when list=4 is passed. www.fcc.gov blocks
# non-browser clients, so we go through the transition host.
FCC_FM_QUERY_URL = "https://transition.fcc.gov/fcc-bin/fmq"
FCC_AM_QUERY_URL = "https://transition.fcc.gov/fcc-bin/amq"

HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; radio-map-importer/1.0)"
}

# Open-Meteo elevation API: free, no key, up to 100 points per request.
# Primary elevation source -- fast (batched) but shares a rate limit across
# whoever else is hitting it from the same network.
ELEVATION_API_URL = "https://api.open-meteo.com/v1/elevation"

# USGS Elevation Point Query Service: free, no key, single point per request,
# but authoritative (3DEP) and US-only -- which matches our FCC-only station
# data. Used as a last-resort fallback when a DEM tile can't be downloaded.
USGS_EPQS_URL = "https://epqs.nationalmap.gov/v1/json"

# USGS 3DEP seamless 1-arc-second (~30m) DEM tiles, distributed as public,
# unauthenticated 1x1-degree GeoTIFFs on S3. This is the primary elevation
# source (see geo/local_dem.py): download a tile once, then every point
# lookup in that tile is a local file read -- no per-point network calls,
# no rate limits, and it covers Alaska (verified directly; the free point
# APIs below have real regional gaps there).
DEM_TILE_BASE_URL = "https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation/1/TIFF/current"
DEM_TILE_DIR = DATA_DIR / "dem_tiles"
DEM_TILE_DIR.mkdir(exist_ok=True)
