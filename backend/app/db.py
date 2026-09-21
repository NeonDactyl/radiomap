import sqlite3
from contextlib import contextmanager

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS stations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    facility_id     INTEGER NOT NULL,
    callsign        TEXT NOT NULL,
    service         TEXT NOT NULL,          -- 'FM' or 'AM'
    frequency_mhz   REAL NOT NULL,          -- AM stored as kHz/1000 for a common unit
    channel         TEXT,
    class           TEXT,
    status          TEXT,
    city            TEXT,
    state           TEXT,
    country         TEXT,
    file_number     TEXT,
    erp_kw          REAL,                  -- FM: ERP horizontal. AM: daytime power.
    erp_v_kw        REAL,                  -- FM: ERP vertical (nullable)
    power_night_kw  REAL,                  -- AM only
    haat_m          REAL,                  -- FM only
    directional     INTEGER NOT NULL DEFAULT 0,
    lat             REAL NOT NULL,
    lon             REAL NOT NULL,
    licensee        TEXT,
    UNIQUE(facility_id, service)
);

CREATE INDEX IF NOT EXISTS idx_stations_service ON stations(service);
CREATE INDEX IF NOT EXISTS idx_stations_state ON stations(state);
CREATE INDEX IF NOT EXISTS idx_stations_latlon ON stations(lat, lon);

CREATE TABLE IF NOT EXISTS elevation_cache (
    lat_r       REAL NOT NULL,
    lon_r       REAL NOT NULL,
    elevation_m REAL NOT NULL,
    PRIMARY KEY (lat_r, lon_r)
);
"""


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(SCHEMA)


@contextmanager
def db_session():
    conn = get_conn()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()
