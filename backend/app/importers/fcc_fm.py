"""Import FM broadcast stations from the FCC FM Query (list=4 format).

Field layout (0-indexed after stripping the outer '|' delimiters):

 0 callsign        1 frequency        2 service (FM/FX/FL)   3 channel
 4 DA/ND            5 (antenna pattern flag, often blank)     6 class
 7 (flag)           8 status (LIC/CP/...)                     9 city
10 state           11 country                                12 file number
13 ERP horizontal  14 ERP vertical    15 HAAT horizontal (m)  16 HAAT vertical (m)
17 facility id     18 lat dir  19 lat deg  20 lat min  21 lat sec
22 lon dir         23 lon deg  24 lon min  25 lon sec         26 licensee
"""
import logging

from ..config import FCC_FM_QUERY_URL
from . import common

log = logging.getLogger(__name__)


def parse_fm_rows(text: str) -> list[dict]:
    stations = []
    for fields in common.split_rows(text):
        if len(fields) < 27:
            continue
        if fields[2] != "FM":
            # Skip translators (FX) and LPFM boosters (FL); primary FM only.
            continue
        if fields[11] != "US":
            # The query tool also returns foreign border-coordination filings
            # (mostly Mexico); a couple even carry a garbage 2-letter "state"
            # that happens to collide with a real US state code, so this
            # must be filtered before the state field is trusted at all.
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

        stations.append({
            "facility_id": facility_id,
            "callsign": fields[0],
            "service": "FM",
            "frequency_mhz": common.parse_float(fields[1]),
            "channel": fields[3] or None,
            "class": fields[6] or None,
            "status": fields[8] or None,
            "city": fields[9] or None,
            "state": fields[10] or None,
            "country": fields[11] or None,
            "file_number": fields[12] or None,
            "erp_kw": common.parse_float(fields[13]),
            "erp_v_kw": common.parse_float(fields[14]),
            "power_night_kw": None,
            "haat_m": common.parse_float(fields[15]),
            "directional": 1 if fields[4] == "DA" else 0,
            "lat": lat,
            "lon": lon,
            "licensee": fields[26] or None,
        })
    return stations


def import_state(state: str) -> list[dict]:
    text = common.fetch_state(FCC_FM_QUERY_URL, state)
    rows = parse_fm_rows(text)
    log.info("FM %s: parsed %d stations", state, len(rows))
    return rows
