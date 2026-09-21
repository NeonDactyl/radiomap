"""Pull radio format/genre data from Wikidata and attach it to stations we
already imported from the FCC.

The FCC doesn't track programming format at all -- that's not something it
regulates -- so there's no official source for "genre." Wikidata, though,
has a "FCC Facility ID" property (P1400) that lines up exactly with the
facility_id we already store from the FCC import, and a "radio format"
property (P415) filled in for some of those stations (sourced from
Wikipedia infoboxes). Coverage is partial -- roughly 1 in 6 US stations
has a format value in Wikidata, skewed toward larger/more notable
stations -- so most stations will still show "format unknown." That's a
real, freely-licensed data limitation, not a bug: there isn't a
comprehensive free source for this.
"""
import logging

import requests

from ..config import HTTP_HEADERS
from ..db import db_session

log = logging.getLogger(__name__)

WIKIDATA_SPARQL_URL = "https://query.wikidata.org/sparql"

QUERY = """
SELECT ?facilityId (GROUP_CONCAT(DISTINCT ?formatLabel; separator=", ") AS ?genres) WHERE {
  ?station wdt:P1400 ?facilityId .
  ?station wdt:P415 ?format .
  ?format rdfs:label ?formatLabel .
  FILTER(LANG(?formatLabel) = "en")
}
GROUP BY ?facilityId
"""


def fetch_genres() -> dict[int, str]:
    resp = requests.get(
        WIKIDATA_SPARQL_URL,
        params={"query": QUERY},
        headers={**HTTP_HEADERS, "Accept": "application/sparql-results+json"},
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()

    genres: dict[int, str] = {}
    for row in data["results"]["bindings"]:
        try:
            facility_id = int(row["facilityId"]["value"])
        except (KeyError, ValueError):
            continue
        genres[facility_id] = row["genres"]["value"]
    return genres


def import_genres() -> int:
    genres = fetch_genres()
    log.info("Wikidata: fetched genres for %d facility IDs", len(genres))

    updated = 0
    with db_session() as conn:
        for facility_id, genre in genres.items():
            cur = conn.execute(
                "UPDATE stations SET genre = ? WHERE facility_id = ?", (genre, facility_id)
            )
            updated += cur.rowcount
    log.info("Wikidata: updated genre on %d station rows", updated)
    return updated
