"""Pure-logic tests for the Wikipedia-infobox format-field scraper --
regression coverage for a real bug found while building this: the format
field's captured value was truncated at the first "|", which cut it off
mid-value whenever the format itself was a piped wikilink like
[[Latin pop|Latin]] (common -- Wikipedia prefers piping a specific genre
article to a shorter display name).
"""
from app.importers.wikipedia_genre import _FORMAT_FIELD_RE, _clean_format


def _extract(wikitext: str) -> str:
    m = _FORMAT_FIELD_RE.search(wikitext)
    assert m, f"format field not found in: {wikitext!r}"
    return _clean_format(m.group(1))


def test_plain_wikilink():
    assert _extract("| format           = [[Classic hits]]\n| erp = 5000") == "Classic hits"


def test_piped_wikilink_does_not_truncate_at_the_internal_pipe():
    assert _extract("| format = [[Latin pop|Latin]]\n| erp = 5000") == "Latin"


def test_multiple_formats_joined_by_br():
    assert _extract(
        "| format = [[Brokered Time]] Ethnic (days)<br>[[Adult Standards]] (nights)\n| erp = 5000"
    ) == "Brokered Time Ethnic (days), Adult Standards (nights)"


def test_plain_text_no_markup():
    assert _extract("| format = Talk radio\n| erp = 5000") == "Talk radio"


def test_ref_tag_and_html_comment_stripped():
    assert _extract(
        "| format = [[Country music]]<ref>{{cite web|title=x}}</ref><!-- as of 2020 -->\n| erp = 5000"
    ) == "Country music"


def test_bold_markup_stripped():
    assert _extract("| format = '''Defunct'''\n| erp = 5000") == "Defunct"


def test_format_as_last_field_before_closing_braces_on_the_same_line():
    assert _extract("| format = Classic rock}}") == "Classic rock"


def test_hlist_template_with_piped_wikilinks():
    # Real false positive found against live data (KCBI (AM)): the naive
    # pipeline unwrapped the wikilinks but never stripped the outer
    # {{hlist|...}} wrapper itself, leaving "{{hlist|Christian talk|brokered"
    # (missing closing braces, since those get truncated by the generic
    # "}}"-drop step) instead of a clean joined value.
    assert _extract(
        "| format = {{hlist|[[Christian radio|Christian talk]]|[[Brokered programming|brokered]]}} "
        "(To become Christian Teaching by Fall 2026)\n| language = English"
    ) == "Christian talk, brokered"


def test_ubl_template():
    assert _extract("| format = {{ubl|Top 40|Rhythmic}}\n| erp = 5000") == "Top 40, Rhythmic"


def test_br_inside_a_single_list_item_still_splits():
    # Real case found against live data (WKGC-FM): a <br> can appear
    # *inside* one {{ubl|...}} argument, not just between them -- must
    # still act as a separator there, not survive into the item's text
    # (where it would trip the final unhandled-markup rejection guard).
    assert _extract(
        "| format = {{ubl|[[Public radio]]<br>[[News radio|News]]|'''HD2:''' [[Classical music]]}}\n"
        "| erp = 5000"
    ) == "Public radio, News, HD2: Classical music"


def test_ignores_format_parameter_of_a_nested_coord_template():
    # A real false positive found against live data (KALI-FM): {{coord}}
    # takes its own "format" parameter (dms/dm/d, for how the coordinates
    # display) that has nothing to do with programming format, and often
    # appears inline inside the infobox's "coordinates" field. Must only
    # match "format" as an actual top-level infobox field (starting its
    # own line), not that unrelated same-named nested parameter.
    text = (
        "| coordinates = {{coord|33.75|-117.85|format=dms|type:landmark}}\n"
        "| erp = 5000\n"
    )
    assert _FORMAT_FIELD_RE.search(text) is None


def test_small_template_unwrapped_to_its_argument():
    # {{small|x}} is a plain inline formatting wrapper (renders smaller
    # text), not a list -- unwrap it like a wikilink rather than either
    # list-splitting it or truncating at its "}}".
    assert _extract(
        "| format = Spanish Tropical, {{small|(simulcast of WXDJ)}}\n| erp = 5000"
    ) == "Spanish Tropical, (simulcast of WXDJ)"


def test_html_small_tag_stripped():
    assert _extract(
        "| format = Full-service, <small>(News Talk Information, oldies)</small>\n| erp = 5000"
    ) == "Full-service, (News Talk Information, oldies)"


def test_external_link_stripped():
    assert _extract(
        "| format = Community/Public Radio ([http://example.org/sched Program Schedule])\n| erp = 5000"
    ) == "Community/Public Radio ()"


def test_unrecognized_template_rejected_rather_than_left_as_garbage():
    # Real garbage found against live data (WINC): an unrecognized
    # citation-shorthand template ({{r|...}}) isn't in the small
    # list of specifically-unwrapped templates, so it hits the generic
    # "}}"-truncation fallback and leaves a dangling "{{r|Arbitron"
    # fragment. Storing that verbatim would be worse than storing nothing.
    assert _extract("| format = News/Talk/Sports{{r|Arbitron}}\n| erp = 5000") == ""


def test_multiline_list_template_our_single_line_capture_cant_see_is_rejected():
    # Real garbage found against live data (KCRY, KCRU): {{plainlist|...}}
    # formatted across multiple lines (one bullet per line) has its
    # closing "}}" past the newline our field-capture regex stops at, so
    # all that's ever captured is the bare opening "{{plainlist|" -- must
    # be recognized as unparseable, not stored as literal wikitext syntax.
    assert _extract("| format = {{plainlist|\n* Classic hits\n}}\n| erp = 5000") == ""


def test_stray_bracket_typo_in_source_article_rejected():
    # Real garbage found against live data (KMRS): the Wikipedia article
    # itself has a typo mixing bracket styles ("[{...}]"). Neither a
    # wikilink nor a template, so nothing unwraps it -- must be rejected
    # rather than stored as-is.
    assert _extract("| format = [{Classic hits}]\n| erp = 5000") == ""


def test_no_format_field_returns_no_match():
    assert _FORMAT_FIELD_RE.search("| erp = 5000\n| class = A") is None


def test_empty_format_value_never_leaks_the_next_field():
    # Whether the regex matches an empty capture or doesn't match at all
    # here isn't the point -- what must never happen is the next infobox
    # field's text (e.g. "erp = 5000") leaking in as if it were the format.
    text = "| format = \n| erp = 5000"
    m = _FORMAT_FIELD_RE.search(text)
    cleaned = _clean_format(m.group(1)) if m else ""
    assert cleaned == ""
