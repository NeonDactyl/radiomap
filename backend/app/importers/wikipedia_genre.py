"""Fallback genre source for stations wikidata_genre.py couldn't fill:
parse the "format" field straight out of the station's Wikipedia infobox
wikitext, for stations whose Wikidata item is linked to an English
Wikipedia article but doesn't (yet) have a structured P415 "radio format"
claim.

Wikidata's P415 is clean and deduplicated but sparse -- only ~16% of
stations. A research pass (see the "pull genre by the Wikipedia page"
conversation this came from) found ~12k more stations have a Wikidata
item + Wikipedia article but no P415, and a 200-station sample of those
had a parseable infobox format field 95% of the time. So: same
facility_id linkage as wikidata_genre.py, but instead of trusting a
missing structured claim as "no data," go read the article itself.

This is strictly a fallback -- only ever fills stations.genre where it's
still NULL after wikidata_genre.py's pass, never overwrites a value that
source already set. Infobox wikitext is free text (not a controlled
vocabulary like P415's format entities), so expect messier values --
wikilink markup, the occasional "Defunct (formerly X)" annotation, a few
formats joined by <br> for daypart splits -- cleaned up on a best-effort
basis, not guaranteed canonical.
"""
import logging
import re
import time

import requests

from ..config import HTTP_HEADERS
from ..db import db_session

log = logging.getLogger(__name__)

WIKIDATA_SPARQL_URL = "https://query.wikidata.org/sparql"
WIKIPEDIA_API_URL = "https://en.wikipedia.org/w/api.php"

# Stations with a Wikidata item + enwiki sitelink but no P415 claim --
# wikidata_genre.py already covers everything that DOES have P415, so
# excluding those here keeps this pass from redundantly re-fetching (and
# potentially clobbering with messier free text) what the structured
# source already gave us.
CANDIDATES_QUERY = """
SELECT ?facilityId ?articleTitle WHERE {
  ?station wdt:P1400 ?facilityId .
  ?article schema:about ?station ;
           schema:isPartOf <https://en.wikipedia.org/> ;
           schema:name ?articleTitle .
  FILTER NOT EXISTS { ?station wdt:P415 ?format . }
}
"""

# Matches MediaWiki API limits for logged-out/bot read requests.
TITLES_PER_BATCH = 50
BATCH_DELAY_S = 0.5

_FORMAT_FIELD_RE = re.compile(r"^[ \t]*\|[ \t]*format[ \t]*=[ \t]*([^\n]+)", re.IGNORECASE | re.MULTILINE)
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_REF_TAG_RE = re.compile(r"<ref[^>]*/>|<ref[^>]*>.*?</ref>", re.DOTALL | re.IGNORECASE)
_EXTERNAL_LINK_RE = re.compile(r"\[https?://[^\]]*\]", re.IGNORECASE)
_WIKILINK_RE = re.compile(r"\[\[(?:[^|\]]*\|)?([^\]]+)\]\]")
_BOLD_ITALIC_RE = re.compile(r"'{2,}")
_BR_TAG_RE = re.compile(r"<br[^>]*>", re.IGNORECASE)
_SMALL_TAG_RE = re.compile(r"</?small>", re.IGNORECASE)
# {{small|x}} and similar are plain inline formatting wrappers -- unwrap to
# their argument like a wikilink, not treat as list-splittable or as noise
# to reject outright.
_SIMPLE_TEMPLATE_RE = re.compile(r"\{\{\s*(?:small|smaller|nowrap)\s*\|([^{}]*)\}\}", re.IGNORECASE)
# {{hlist|...}}/{{flatlist|...}}/{{ubl|...}} are common Wikipedia
# templates for rendering a multi-value field (e.g. a station with
# separate daypart formats) as a list -- their own "|"-separated
# arguments need splitting into separate values, same idea as <br>.
_LIST_TEMPLATE_RE = re.compile(
    r"\{\{\s*(?:hlist|flatlist|ubl|unbulleted list|plainlist)\s*\|(.*?)\}\}", re.IGNORECASE | re.DOTALL,
)
# Final safety net: legitimate genre text never contains these characters,
# so if any survive every specific rule above, something wasn't actually
# handled -- a multi-line list template whose content our single-line
# field capture never saw ({{plainlist|<newline>*a<newline>*b<newline>}}),
# an unrecognized citation shorthand ({{r|Arbitron), a stray bracket typo
# in the source article, etc. Found by scanning the full ~12k-row result
# of a real bulk run, not hypothetically -- better to store nothing than a
# fragment of wikitext that looks like real data.
_UNHANDLED_MARKUP_RE = re.compile(r"[{}\[\]<>]")


def _clean_format(raw: str) -> str:
    # Strip comments/refs *before* touching anything else -- a <ref> often
    # contains its own {{cite ...}} template, whose "}}" would otherwise
    # be mistaken for the outer infobox's closing braces and truncate the
    # value early (e.g. "Country music<ref>{{cite web|title=x}}</ref>"
    # would cut to just "Country music{{cite web|title=x" if split first).
    text = _HTML_COMMENT_RE.sub("", raw)
    text = _REF_TAG_RE.sub("", text)

    list_match = _LIST_TEMPLATE_RE.search(text)
    if list_match:
        # Unwrap wikilinks (and their internal "|") *before* splitting the
        # list template's own arguments on "|", or a piped wikilink inside
        # a list item -- {{hlist|[[Christian radio|Christian talk]]}} --
        # would get sliced apart as if it were two separate list items.
        inner = _WIKILINK_RE.sub(r"\1", list_match.group(1))
        inner = _BOLD_ITALIC_RE.sub("", inner)
        # A <br> can appear *inside* one list item, not just between the
        # template's own "|"-separated arguments (real case: WKGC-FM's
        # {{ubl|[[Public radio]]<br>[[News radio|News]]/...|'''HD2:''' ...}}
        # has one) -- split on it the same as a "|" would be.
        inner = _BR_TAG_RE.sub("|", inner)
        text = ", ".join(item.strip() for item in inner.split("|") if item.strip())
        return "" if _UNHANDLED_MARKUP_RE.search(text) else text

    text = _EXTERNAL_LINK_RE.sub("", text)
    text = _WIKILINK_RE.sub(r"\1", text)
    text = _SIMPLE_TEMPLATE_RE.sub(r"\1", text)
    text = _SMALL_TAG_RE.sub("", text)
    # The field-capture regex grabs the rest of the line (needed so an
    # internal "|" in a piped wikilink like [[Latin pop|Latin]] doesn't
    # truncate the value) -- if format happens to be the last field on its
    # line with no newline before the template's closing braces, drop
    # those and anything after.
    text = text.split("}}", 1)[0]
    text = _BOLD_ITALIC_RE.sub("", text)
    text = _BR_TAG_RE.sub(", ", text)
    text = re.sub(r"\s+", " ", text).strip(" ,")
    return "" if _UNHANDLED_MARKUP_RE.search(text) else text


def fetch_candidates() -> dict[str, int]:
    """Returns {article_title: facility_id}."""
    resp = requests.get(
        WIKIDATA_SPARQL_URL,
        params={"query": CANDIDATES_QUERY},
        headers={**HTTP_HEADERS, "Accept": "application/sparql-results+json"},
        timeout=60,
    )
    resp.raise_for_status()
    candidates: dict[str, int] = {}
    for row in resp.json()["results"]["bindings"]:
        try:
            facility_id = int(row["facilityId"]["value"])
        except (KeyError, ValueError):
            continue
        title = row.get("articleTitle", {}).get("value")
        if title:
            candidates[title] = facility_id
    return candidates


MAX_RETRIES_PER_BATCH = 5
FALLBACK_RETRY_DELAY_S = 5.0  # used only if a 429 response has no Retry-After header


def _fetch_formats(titles: list[str]) -> dict[str, str]:
    """Returns {title: cleaned_format} for whichever of `titles` (<=50)
    have a parseable infobox format field. `redirects=1` follows Wikipedia
    redirects (a Wikidata sitelink occasionally points at one) back to the
    real article automatically.

    Retries on 429 (honoring Retry-After when present) instead of just
    failing the batch -- a real bulk run against ~12k candidates got 429'd
    on the vast majority of batches once the old, browser-spoofing
    User-Agent triggered Wikimedia's rate limiter (see config.py), so this
    isn't a hypothetical edge case.
    """
    for attempt in range(MAX_RETRIES_PER_BATCH):
        resp = requests.get(
            WIKIPEDIA_API_URL,
            params={
                "action": "query", "prop": "revisions", "rvprop": "content", "rvslots": "main",
                "redirects": 1, "format": "json", "formatversion": 2, "titles": "|".join(titles),
            },
            headers=HTTP_HEADERS,
            timeout=30,
        )
        if resp.status_code == 429 and attempt < MAX_RETRIES_PER_BATCH - 1:
            delay = float(resp.headers.get("Retry-After", FALLBACK_RETRY_DELAY_S))
            log.warning("Wikipedia: rate-limited, retrying in %.0fs (attempt %d/%d)",
                        delay, attempt + 1, MAX_RETRIES_PER_BATCH)
            time.sleep(delay)
            continue
        resp.raise_for_status()
        data = resp.json()
        break

    # Redirects map the *requested* title to the resolved one -- results
    # come back keyed by the resolved title, so without this we'd silently
    # drop every redirected page.
    resolved_to_requested = {title: title for title in titles}
    for r in data.get("query", {}).get("redirects", []):
        resolved_to_requested[r["to"]] = r["from"]

    results: dict[str, str] = {}
    for page in data.get("query", {}).get("pages", []):
        if "revisions" not in page:
            continue
        text = page["revisions"][0]["slots"]["main"]["content"]
        m = _FORMAT_FIELD_RE.search(text)
        if not m:
            continue
        cleaned = _clean_format(m.group(1))
        if cleaned:
            requested_title = resolved_to_requested.get(page["title"], page["title"])
            results[requested_title] = cleaned
    return results


def fetch_genres() -> dict[int, str]:
    candidates = fetch_candidates()
    log.info("Wikipedia: %d candidate stations (Wikidata item + article, no P415)", len(candidates))

    titles = list(candidates.keys())
    genres: dict[int, str] = {}
    for i in range(0, len(titles), TITLES_PER_BATCH):
        batch = titles[i:i + TITLES_PER_BATCH]
        try:
            formats = _fetch_formats(batch)
        except requests.RequestException:
            log.warning("Wikipedia: batch starting at %d failed, skipping", i, exc_info=True)
            continue
        for title, fmt in formats.items():
            genres[candidates[title]] = fmt
        if i + TITLES_PER_BATCH < len(titles):
            time.sleep(BATCH_DELAY_S)
        if (i // TITLES_PER_BATCH) % 20 == 0:
            log.info("Wikipedia: %d/%d titles checked, %d formats found so far", i, len(titles), len(genres))

    log.info("Wikipedia: found formats for %d/%d candidates", len(genres), len(candidates))
    return genres


def import_genres() -> int:
    genres = fetch_genres()

    updated = 0
    with db_session() as conn:
        for facility_id, genre in genres.items():
            # Only a fallback: never override a genre wikidata_genre.py
            # (or a previous run of this importer) already set.
            cur = conn.execute(
                "UPDATE stations SET genre = ? WHERE facility_id = ? AND genre IS NULL",
                (genre, facility_id),
            )
            updated += cur.rowcount
    log.info("Wikipedia: updated genre on %d station rows", updated)
    return updated
