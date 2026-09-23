import dataclasses
import sqlite3

from fastapi import APIRouter, HTTPException, Query, Response

from .. import background_seeder, coverage_cache
from ..db import get_conn
from ..geo.elevation import ElevationUnavailable, elevation_provider
from ..geo.landcover import canopy_provider
from ..models import CoverageResponse, StationDetail, StationOut
from ..propagation.base import Station
from ..propagation.params import resolve_coverage_params
from ..propagation.simple import get_model

router = APIRouter(prefix="/api")

STATION_COLUMNS = (
    "id, facility_id, callsign, service, frequency_mhz, channel, class, status, "
    "city, state, country, file_number, erp_kw, erp_v_kw, power_night_kw, "
    "haat_m, directional, lat, lon, licensee, genre"
)


def _row_to_station_out(row: sqlite3.Row) -> StationOut:
    return StationOut(
        id=row["id"],
        facility_id=row["facility_id"],
        callsign=row["callsign"],
        service=row["service"],
        frequency_mhz=row["frequency_mhz"],
        class_=row["class"],
        city=row["city"],
        state=row["state"],
        erp_kw=row["erp_kw"],
        haat_m=row["haat_m"],
        directional=bool(row["directional"]),
        lat=row["lat"],
        lon=row["lon"],
        licensee=row["licensee"],
        genre=row["genre"],
    )


def _row_to_station_detail(row: sqlite3.Row) -> StationDetail:
    base = _row_to_station_out(row)
    return StationDetail(
        **base.model_dump(),
        channel=row["channel"],
        status=row["status"],
        country=row["country"],
        file_number=row["file_number"],
        erp_v_kw=row["erp_v_kw"],
        power_night_kw=row["power_night_kw"],
    )


@router.get("/meta/states")
def list_states():
    """Per-state station counts plus a bounding box computed from the
    stations we actually have (padded a bit), used by the frontend as a
    "jump to state" shortcut -- the map itself, not the state list, is
    what drives which stations are shown.
    """
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT state, service, COUNT(*) c FROM stations GROUP BY state, service ORDER BY state"
        ).fetchall()
        bbox_rows = conn.execute(
            "SELECT state, MIN(lon) min_lon, MIN(lat) min_lat, MAX(lon) max_lon, MAX(lat) max_lat "
            "FROM stations GROUP BY state"
        ).fetchall()
    finally:
        conn.close()

    by_state: dict[str, dict[str, int]] = {}
    for r in rows:
        by_state.setdefault(r["state"], {})[r["service"]] = r["c"]

    bboxes = {}
    pad = 0.15  # degrees, so the jump doesn't crop stations right at the edge
    for r in bbox_rows:
        bboxes[r["state"]] = [
            r["min_lon"] - pad, r["min_lat"] - pad, r["max_lon"] + pad, r["max_lat"] + pad,
        ]

    return [
        {"state": s, "counts": c, "bbox": bboxes.get(s)}
        for s, c in sorted(by_state.items())
    ]


@router.get("/meta/genres")
def list_genres(service: str | None = Query(None, pattern="^(FM|AM)$")):
    clauses = ["genre IS NOT NULL"]
    params: list = []
    if service:
        clauses.append("service = ?")
        params.append(service)
    conn = get_conn()
    try:
        rows = conn.execute(
            f"SELECT genre, COUNT(*) c FROM stations WHERE {' AND '.join(clauses)} "
            "GROUP BY genre ORDER BY c DESC",
            params,
        ).fetchall()
    finally:
        conn.close()
    return [{"genre": r["genre"], "count": r["c"]} for r in rows]


@router.get("/meta/seed-status")
def seed_status():
    """One entry per precompute lane (currently "simple" and "itm" -- see
    background_seeder.py), each independently working through the station
    list so a slow ITM pass can't hold up the fast simple-model pass.
    """
    return {name: dataclasses.asdict(status) for name, status in background_seeder.statuses.items()}


@router.get("/stations", response_model=list[StationOut])
def list_stations(
    service: str | None = Query(None, pattern="^(FM|AM)$"),
    state: str | None = None,
    search: str | None = Query(
        None, min_length=2,
        description="Callsign or city substring. Searches nationwide, ignoring `state` -- "
        "a station's city of license often isn't the state of the market it actually "
        "serves (e.g. WJLI is licensed to Metropolis, IL but serves Paducah, KY), so "
        "state alone can hide the station a user is looking for.",
    ),
    bbox: str | None = Query(
        None, description="min_lon,min_lat,max_lon,max_lat -- only return stations inside this box"
    ),
    genre: str | None = Query(None, description="Exact genre string, as returned by /meta/genres"),
    limit: int = Query(
        2000, le=20000,
        description="Raised well above the total number of stations of either service "
        "nationwide (~11.4k FM, ~4.3k AM) so a viewport request can ask for everything "
        "matching and rely on client-side marker clustering, rather than the server "
        "silently truncating to an arbitrary geographic subset.",
    ),
    response: Response = None,
):
    clauses = []
    params: list = []
    if service:
        clauses.append("service = ?")
        params.append(service)
    if genre:
        clauses.append("genre = ?")
        params.append(genre)
    if search:
        clauses.append("(callsign LIKE ? OR city LIKE ?)")
        needle = f"%{search.upper()}%"
        params.extend([needle, needle])
    elif state:
        clauses.append("state = ?")
        params.append(state.upper())
    if bbox:
        try:
            min_lon, min_lat, max_lon, max_lat = (float(v) for v in bbox.split(","))
        except ValueError:
            raise HTTPException(400, "bbox must be min_lon,min_lat,max_lon,max_lat")
        clauses.append("lon BETWEEN ? AND ? AND lat BETWEEN ? AND ?")
        params.extend([min_lon, max_lon, min_lat, max_lat])

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    conn = get_conn()
    try:
        total = conn.execute(f"SELECT COUNT(*) c FROM stations {where}", params).fetchone()["c"]
        # No ORDER BY normally -- table order is fine and fastest when
        # everything fits under `limit`. But when a query matches more rows
        # than that (typically a whole-country viewport, where the table's
        # state-by-state import order would otherwise return an arbitrary
        # geographic prefix -- e.g. only whichever handful of states happen
        # to be first in the table -- and silently hide every other state),
        # order randomly so a truncated result is at least a representative
        # sample of the whole match set instead of a misleading chunk of it.
        order = "ORDER BY RANDOM()" if total > limit else ""
        sql = f"SELECT {STATION_COLUMNS} FROM stations {where} {order} LIMIT ?"
        rows = conn.execute(sql, params + [limit]).fetchall()
    finally:
        conn.close()

    if response is not None:
        response.headers["X-Total-Matching"] = str(total)
        response.headers["X-Truncated"] = "true" if total > len(rows) else "false"
    return [_row_to_station_out(r) for r in rows]


@router.get("/stations/{station_id}", response_model=StationDetail)
def get_station(station_id: int):
    conn = get_conn()
    try:
        row = conn.execute(
            f"SELECT {STATION_COLUMNS} FROM stations WHERE id = ?", (station_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(404, "Station not found")
        return _row_to_station_detail(row)
    finally:
        conn.close()


@router.get("/stations/{station_id}/coverage", response_model=CoverageResponse)
def get_coverage(
    station_id: int,
    threshold_dbu: float | None = None,
    max_radius_km: float | None = None,
    n_bearings: int | None = Query(None, ge=8, le=90),
    step_km: float | None = None,
    ground_conductivity_mmho: float = Query(5.0, gt=0, description="AM only: 1=poor/rocky, 5=average, 15=rich soil"),
    fm_model: str = Query(
        "simple", pattern="^(simple|itm)$",
        description="FM only: 'simple' (FCC curve + knife-edge, fast) or "
        "'itm' (real Longley-Rice point-to-point physics, see propagation/itm_model.py)",
    ),
    refresh: bool = Query(False, description="Recompute even if a cached contour exists for these exact params"),
):
    conn = get_conn()
    try:
        row = conn.execute(
            f"SELECT {STATION_COLUMNS} FROM stations WHERE id = ?", (station_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(404, "Station not found")
    finally:
        conn.close()

    station = Station(
        id=row["id"],
        callsign=row["callsign"],
        service=row["service"],
        frequency_mhz=row["frequency_mhz"],
        erp_kw=row["erp_kw"],
        haat_m=row["haat_m"],
        lat=row["lat"],
        lon=row["lon"],
        directional=bool(row["directional"]),
    )

    params = resolve_coverage_params(
        station, threshold_dbu=threshold_dbu, max_radius_km=max_radius_km,
        step_km=step_km, n_bearings=n_bearings, ground_conductivity_mmho=ground_conductivity_mmho,
        fm_model=fm_model,
    )
    model = get_model(station.service, elevation_provider, canopy_provider, **params)

    contour = None if refresh else coverage_cache.lookup(station_id, model.name, params)
    cached = contour is not None
    if not cached:
        try:
            contour = model.coverage_contour(
                station, threshold_dbu=params["threshold_dbu"], max_radius_km=params["max_radius_km"],
                step_km=params["step_km"], n_bearings=params["n_bearings"],
            )
        except ElevationUnavailable as exc:
            raise HTTPException(
                503,
                f"Elevation data service is temporarily unavailable/rate-limited ({exc}). "
                "Try again shortly, or retry with fewer bearings / a larger step size.",
            )
        contour = [[lat, lon] for lat, lon in contour]
        coverage_cache.store(station_id, model.name, params, contour)

    return CoverageResponse(
        station_id=station_id,
        model=model.name,
        threshold_dbu=params["threshold_dbu"],
        max_radius_km=params["max_radius_km"],
        contour=contour,
        cached=cached,
    )
