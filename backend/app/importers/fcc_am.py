"""Import AM broadcast stations from the FCC AM Query (list=4 format).

AM stations often have separate daytime/nighttime power rows (many AM
stations must reduce power or go silent at night to protect other
stations on the same channel). We merge those rows into one station
record per facility id.

Field layout (0-indexed after stripping the outer '|' delimiters):

 0 callsign   1 frequency   2 service (AM)   3 (NCE/blank)
 4 DAY/NIG    5 label       6 class          7 (flag)         8 status
 9 city      10 state      11 country       12 file number   13 power
14 Directional/blank       15 pattern       16 (flag)        17 facility id
18 lat dir  19 lat deg  20 lat min  21 lat sec
22 lon dir  23 lon deg  24 lon min  25 lon sec                26 licensee
"""
import logging

from ..config import FCC_AM_QUERY_URL
from . import common

log = logging.getLogger(__name__)


def parse_am_rows(text: str) -> list[dict]:
    by_facility: dict[int, dict] = {}

    for fields in common.split_rows(text):
        if len(fields) < 27:
            continue
        if fields[2] != "AM":
            continue
        try:
            lat = common.dms_to_decimal(fields[18], fields[19], fields[20], fields[21])
            lon = common.dms_to_decimal(fields[22], fields[23], fields[24], fields[25])
        except ValueError:
            continue
        if lat == 0.0 and lon == 0.0:
            continue

        facility_id = common.parse_int(fields[17])
        if facility_id is None:
            continue

        power_kw = common.parse_float(fields[13])
        is_night = fields[4].strip().upper().startswith("NIG")

        station = by_facility.get(facility_id)
        if station is None:
            station = {
                "facility_id": facility_id,
                "callsign": fields[0],
                "service": "AM",
                "frequency_mhz": (common.parse_float(fields[1]) or 0) / 1000.0,
                "channel": None,
                "class": fields[6] or None,
                "status": fields[8] or None,
                "city": fields[9] or None,
                "state": fields[10] or None,
                "country": fields[11] or None,
                "file_number": fields[12] or None,
                "erp_kw": None,
                "erp_v_kw": None,
                "power_night_kw": None,
                "haat_m": None,
                "directional": 1 if fields[14].strip().upper() == "DIRECTIONAL" else 0,
                "lat": lat,
                "lon": lon,
                "licensee": fields[26] or None,
            }
            by_facility[facility_id] = station

        if fields[14].strip().upper() == "DIRECTIONAL":
            station["directional"] = 1

        if is_night:
            station["power_night_kw"] = power_kw
        else:
            # Daytime (or only) power row; prefer the first non-null value.
            if station["erp_kw"] is None:
                station["erp_kw"] = power_kw

    rows = list(by_facility.values())
    return rows


def import_state(state: str) -> list[dict]:
    text = common.fetch_state(FCC_AM_QUERY_URL, state)
    rows = parse_am_rows(text)
    log.info("AM %s: parsed %d stations", state, len(rows))
    return rows
