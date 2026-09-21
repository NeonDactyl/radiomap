from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

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
ELEVATION_API_URL = "https://api.open-meteo.com/v1/elevation"
