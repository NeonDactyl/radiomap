import sqlite3

from fastapi import APIRouter, HTTPException, Query

from ..db import get_conn
from ..geo.elevation import ElevationUnavailable, elevation_provider
from ..geo.landcover import canopy_provider
from ..models import CoverageResponse, StationDetail, StationOut
from ..propagation.base import Station
from ..propagation.simple import get_model, suggest_am_search_radius_km, suggest_fm_search_radius_km

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
    limit: int = Query(2000, le=5000),
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
    sql = f"SELECT {STATION_COLUMNS} FROM stations {where} LIMIT ?"
    params.append(limit)

    conn = get_conn()
    try:
        rows = conn.execute(sql, params).fetchall()
        return [_row_to_station_out(r) for r in rows]
    finally:
        conn.close()


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
    n_bearings: int = Query(24, ge=8, le=72),
    step_km: float | None = None,
    ground_conductivity_mmho: float = Query(5.0, gt=0, description="AM only: 1=poor/rocky, 5=average, 15=rich soil"),
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

    default_threshold = 54.0
    threshold_dbu = threshold_dbu if threshold_dbu is not None else default_threshold

    if station.service == "FM":
        default_radius = suggest_fm_search_radius_km(station.erp_kw, station.haat_m)
    else:
        default_radius = suggest_am_search_radius_km(
            station.erp_kw, station.frequency_mhz, ground_conductivity_mmho, threshold_dbu,
        )
    max_radius_km = max_radius_km if max_radius_km is not None else default_radius
    # Keep the number of samples per bearing (and thus compute/elevation-call
    # cost) roughly constant regardless of how far the search radius reaches.
    default_step = max(2.0, min(max_radius_km / 35.0, 8.0))
    step_km = step_km if step_km is not None else default_step

    model = get_model(
        station.service, elevation_provider, canopy_provider,
        ground_conductivity_mmho=ground_conductivity_mmho,
    )
    try:
        contour = model.coverage_contour(
            station, threshold_dbu=threshold_dbu, max_radius_km=max_radius_km,
            step_km=step_km, n_bearings=n_bearings,
        )
    except ElevationUnavailable as exc:
        raise HTTPException(
            503,
            f"Elevation data service is temporarily unavailable/rate-limited ({exc}). "
            "Try again shortly, or retry with fewer bearings / a larger step size.",
        )

    return CoverageResponse(
        station_id=station_id,
        model=model.name,
        threshold_dbu=threshold_dbu,
        max_radius_km=max_radius_km,
        contour=[[lat, lon] for lat, lon in contour],
    )
