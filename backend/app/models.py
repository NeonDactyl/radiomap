from pydantic import BaseModel


class StationOut(BaseModel):
    id: int
    facility_id: int
    callsign: str
    service: str
    frequency_mhz: float
    class_: str | None = None
    city: str | None = None
    state: str | None = None
    erp_kw: float | None = None
    haat_m: float | None = None
    directional: bool
    lat: float
    lon: float
    licensee: str | None = None


class StationDetail(StationOut):
    channel: str | None = None
    status: str | None = None
    country: str | None = None
    file_number: str | None = None
    erp_v_kw: float | None = None
    power_night_kw: float | None = None


class CoverageResponse(BaseModel):
    station_id: int
    model: str
    threshold_dbu: float
    max_radius_km: float
    contour: list[list[float]]  # [[lat, lon], ...]
