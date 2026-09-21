"""Shared helpers for pulling station data out of the FCC's legacy AM/FM
query CGI endpoints (transition.fcc.gov/fcc-bin/{fmq,amq}).

Those endpoints predate JSON APIs: called with list=4 they return one
station per line as a fixed-width set of fields separated by '|', e.g.

    |KRTM        |88.1  MHz |FM |201 |DA  |...|N |34 |2  |16.0  |W |116 |48 |51.1  |...

Field positions are stable (verified against live output), so we parse by
index rather than trying to guess a header row -- there isn't one.
"""
import time

import requests

from ..config import HTTP_HEADERS

# All US states/territories the FCC query tool recognizes.
STATE_CODES = [
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI",
    "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN",
    "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH",
    "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA",
    "WV", "WI", "WY", "PR", "VI", "GU", "AS", "MP",
]


class FccFetchError(RuntimeError):
    pass


def fetch_state(url: str, state: str, retries: int = 3, timeout: int = 30) -> str:
    """Fetch the raw list=4 response for one state. Returns decoded text."""
    params = {"state": state, "call": "", "arn": "", "list": "4"}
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, params=params, headers=HTTP_HEADERS, timeout=timeout)
            resp.raise_for_status()
            resp.encoding = "latin-1"
            return resp.text
        except requests.RequestException as exc:
            last_exc = exc
            time.sleep(1.5 * attempt)
    raise FccFetchError(f"Failed to fetch {state} from {url}: {last_exc}")


def split_rows(text: str) -> list[list[str]]:
    """Split the raw response into per-station field lists.

    Each real data line starts with '|'. We split on '|' and strip
    whitespace from every field, dropping the empty leading/trailing
    fields produced by the delimiters.
    """
    rows = []
    for line in text.splitlines():
        if not line.startswith("|"):
            continue
        fields = [f.strip() for f in line.split("|")]
        # first and last element are always '' (leading/trailing pipe)
        rows.append(fields[1:-1])
    return rows


def dms_to_decimal(direction: str, deg: str, minutes: str, seconds: str) -> float:
    d = float(deg or 0)
    m = float(minutes or 0)
    s = float(seconds or 0)
    value = d + m / 60.0 + s / 3600.0
    if direction.strip().upper() in ("S", "W"):
        value = -value
    return value


def parse_float(text: str) -> float | None:
    text = (text or "").strip()
    if not text or text == "-":
        return None
    # strip trailing unit labels like "kW", "MHz", "kHz", "m"
    for token in text.split():
        try:
            return float(token)
        except ValueError:
            continue
    return None


def parse_int(text: str) -> int | None:
    text = (text or "").strip()
    if not text or text == "-":
        return None
    try:
        return int(float(text.split()[0]))
    except (ValueError, IndexError):
        return None
