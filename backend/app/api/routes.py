import sqlite3

from fastapi import APIRouter, HTTPException, Query

from ..db import get_conn
from ..geo.elevation import ElevationUnavailable, elevation_provider
from ..geo.landcover import canopy_provider
from ..models import CoverageResponse, StationDetail, StationOut
from ..propagation.base import Station
from ..propagation.simple import get_model

router = APIRouter(prefix="/api")

STATION_COLUMNS = (
    "id, facility_id, callsign, service, frequency_mhz, channel, class, status, "
    "city, state, country, file_number, erp_kw, erp_v_kw, power_night_kw, "
    "haat_m, directional, lat, lon, licensee"
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
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT state, service, COUNT(*) c FROM stations GROUP BY state, service ORDER BY state"
        ).fetchall()
    finally:
        conn.close()
    by_state: dict[str, dict[str, int]] = {}
    for r in rows:
        by_state.setdefault(r["state"], {})[r["service"]] = r["c"]
    return [{"state": s, "counts": c} for s, c in sorted(by_state.items())]


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
    limit: int = Query(2000, le=5000),
):
    clauses = []
    params: list = []
    if service:
        clauses.append("service = ?")
        params.append(service)
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

    if station.service == "FM":
        default_threshold, default_radius, default_step = 54.0, 90.0, 3.0
    else:
        default_threshold, default_radius, default_step = 54.0, 150.0, 4.0

    threshold_dbu = threshold_dbu if threshold_dbu is not None else default_threshold
    max_radius_km = max_radius_km if max_radius_km is not None else default_radius
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
