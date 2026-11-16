# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Pins the identifier-format masks of ``RFC-data-rules.md`` section 3.

``fluid_build.data_rules._masks``: the COBOL PICTURE grammar (``parse_mask`` errors, the
backslash escape), the portable regex of a column (anchored and unanchored), the literal
escaping rules, why consumers must use ``re.fullmatch``, the overlap check between
alternatives, and the capacity of an alternative.
"""

from __future__ import annotations

import re

import pytest

from fluid_build.data_rules import _masks

pytestmark = pytest.mark.unit

RFC_FORMAT = [
    {"prefixes": ["MSC", "TWR"], "mask": "99999"},
    {"prefixes": ["LEGACY-"], "mask": "A9-99AA"},
]
RFC_BODY = "((MSC|TWR)[0-9]{5}|LEGACY-[A-Z][0-9]-[0-9]{2}[A-Z]{2})"


def _alts(*entries):
    return [_masks.alternative(entry) for entry in entries]


def _mask_regex(mask: str) -> str:
    return _masks.mask_regex(_masks.parse_mask(mask))


# -- the RFC example ------------------------------------------------------------


def test_rfc_example_compiles_to_the_documented_regex():
    alternatives = _alts(*RFC_FORMAT)
    assert _masks.column_regex(alternatives) == "^" + RFC_BODY + "$"
    assert _masks.column_regex(alternatives) == (
        r"^((MSC|TWR)[0-9]{5}|LEGACY-[A-Z][0-9]-[0-9]{2}[A-Z]{2})$"
    )


def test_rfc_example_body_has_no_anchors():
    body = _masks.column_body(_alts(*RFC_FORMAT))
    assert body == RFC_BODY
    assert not body.startswith("^") and not body.endswith("$")


@pytest.mark.parametrize("value", ["MSC12345", "TWR00000", "LEGACY-A1-23BC"])
def test_body_accepts(value):
    assert re.fullmatch(RFC_BODY, value)
    assert re.fullmatch(_masks.column_regex(_alts(*RFC_FORMAT)), value)


@pytest.mark.parametrize(
    "value",
    ["MSC1234", "msc12345", "MSC12345\n", "LEGACY-a1-23BC", "MSC123456", "XXX12345", "", "MSC"],
)
def test_body_refuses(value):
    assert re.fullmatch(RFC_BODY, value) is None
    assert re.fullmatch(_masks.column_regex(_alts(*RFC_FORMAT)), value) is None


def test_match_with_the_dollar_anchor_wrongly_takes_a_trailing_newline():
    """Python's ``$`` matches before a final newline: consumers must use ``re.fullmatch``."""
    regex = _masks.column_regex(_alts(*RFC_FORMAT))
    assert re.match(regex, "MSC12345\n") is not None  # the trap
    assert re.fullmatch(regex, "MSC12345\n") is None  # why fullmatch is the rule


def test_regex_uses_only_portable_constructs():
    regex = _masks.column_regex(_alts(*RFC_FORMAT, {"prefixes": ["A.B", "$"], "mask": "X"}))
    assert not re.search(r"\\[dDwWsSbB]", regex)  # no shorthand classes
    assert "(?" not in regex  # no non-capturing groups, lookarounds or flags


# -- literal escaping ---------------------------------------------------------------


def test_dollar_becomes_a_class():
    assert _masks.literal_regex("$") == "[$]"
    assert _masks.literal_regex("US$") == "US[$]"


@pytest.mark.parametrize("char", list(".*+?()[]{}|\\^"))
def test_specials_are_backslash_escaped(char):
    assert _masks.literal_regex(char) == "\\" + char
    assert re.fullmatch(_masks.literal_regex(char), char)


@pytest.mark.parametrize("char", ["-", " ", "/", "_", ":", "#", "A", "9", "~"])
def test_plain_characters_stay_plain(char):
    assert _masks.literal_regex(char) == char


def test_escaped_literals_match_only_themselves():
    text = "a.b*c+d?(e)[f]{g}|h\\i^j$k-l m"
    pattern = _masks.literal_regex(text)
    assert re.fullmatch(pattern, text)
    assert re.fullmatch(pattern, text.replace(".", "x", 1)) is None


def test_literals_in_a_mask_are_escaped_too():
    assert _mask_regex("9.9") == "[0-9]\\.[0-9]"
    assert _mask_regex("A$9") == "[A-Z][$][0-9]"
    assert _mask_regex("9-9 9") == "[0-9]-[0-9] [0-9]"


def test_runs_of_one_symbol_become_counts():
    assert _mask_regex("999") == "[0-9]{3}"
    assert _mask_regex("9") == "[0-9]"
    assert _mask_regex("XX9A") == "[A-Z0-9]{2}[0-9][A-Z]"
    assert _mask_regex("99AA99") == "[0-9]{2}[A-Z]{2}[0-9]{2}"


# -- prefixes ----------------------------------------------------------------------------


def test_prefix_regex_single_and_several():
    assert _masks.prefix_regex(["MSC"]) == "MSC"
    assert _masks.prefix_regex(["MSC", "TWR"]) == "(MSC|TWR)"
    assert _masks.prefix_regex(["A.B", "C$"]) == "(A\\.B|C[$])"


def test_an_empty_prefix_beside_others_is_an_optional_group():
    assert _masks.prefix_regex(["MSC", "TWR", ""]) == "(MSC|TWR)?"
    assert _masks.prefix_regex(["", "MSC"]) == "(MSC)?"


def test_a_lone_empty_prefix_adds_nothing():
    assert _masks.prefix_regex([""]) == ""
    assert _masks.alternative({"mask": "99"}).regex() == "[0-9]{2}"
    assert _masks.alternative({"prefixes": [""], "mask": "99"}).regex() == "[0-9]{2}"


def test_optional_prefix_matches_with_and_without():
    regex = _masks.column_regex(_alts({"prefixes": ["MSC", ""], "mask": "99"}))
    assert regex == "^((MSC)?[0-9]{2})$"
    assert re.fullmatch(regex, "MSC12") and re.fullmatch(regex, "12")
    assert re.fullmatch(regex, "TWR12") is None


# -- parse_mask ------------------------------------------------------------------------------


def test_parse_mask_reads_symbols_and_literals():
    positions = _masks.parse_mask("A9-X")
    assert [p.symbol for p in positions] == ["A", "9", None, "X"]
    assert positions[2].literal == "-"
    assert positions[0].chars() == frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    assert positions[1].chars() == frozenset("0123456789")
    assert len(positions[3].chars()) == 36


@pytest.mark.parametrize("mask", ["", None, 5])
def test_parse_mask_refuses_an_empty_or_non_text_mask(mask):
    with pytest.raises(_masks.MaskError, match="1 to 64"):
        _masks.parse_mask(mask)  # type: ignore[arg-type]


def test_parse_mask_refuses_65_characters_and_takes_64():
    with pytest.raises(_masks.MaskError, match="65 characters"):
        _masks.parse_mask("9" * 65)
    assert len(_masks.parse_mask("9" * 64)) == 64


@pytest.mark.parametrize("mask", ["---", "a.b", "--\\9"])
def test_parse_mask_refuses_a_mask_with_no_symbol(mask):
    with pytest.raises(_masks.MaskError, match="no symbol"):
        _masks.parse_mask(mask)


def test_parse_mask_refuses_a_trailing_backslash():
    with pytest.raises(_masks.MaskError, match="ends in a backslash"):
        _masks.parse_mask("99\\")


@pytest.mark.parametrize("mask", ["9\x00", "9\t9", "9\n", "9\x7f", "9é", "9​"])
def test_parse_mask_refuses_a_non_printable_character(mask):
    with pytest.raises(_masks.MaskError, match="not printable ASCII"):
        _masks.parse_mask(mask)


def test_parse_mask_refuses_an_escaped_non_ascii_character():
    with pytest.raises(_masks.MaskError, match="non-ASCII"):
        _masks.parse_mask("9\\é")


def test_a_backslash_escaped_symbol_is_a_literal():
    positions = _masks.parse_mask("\\9A")
    assert positions[0].symbol is None and positions[0].literal == "9"
    assert positions[1].symbol == "A"
    assert _masks.mask_regex(positions) == "9[A-Z]"
    regex = _masks.column_regex(_alts({"mask": "\\9A"}))
    assert re.fullmatch(regex, "9Q") and re.fullmatch(regex, "1Q") is None


def test_an_escaped_backslash_is_a_literal_backslash():
    positions = _masks.parse_mask("9\\\\9")
    assert [p.literal for p in positions] == ["", "\\", ""]
    assert _masks.mask_regex(positions) == "[0-9]\\\\[0-9]"


def test_a_space_is_a_literal():
    assert _mask_regex("9 9") == "[0-9] [0-9]"


# -- alternative() ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "entry",
    [
        "99",
        {"prefixes": [], "mask": "99"},
        {"prefixes": "MSC", "mask": "99"},
        {"prefixes": [1], "mask": "99"},
    ],
)
def test_alternative_refuses_a_bad_entry_or_prefixes(entry):
    with pytest.raises(_masks.MaskError):
        _masks.alternative(entry)


def test_alternative_refuses_a_prefix_that_is_not_printable_ascii():
    with pytest.raises(_masks.MaskError, match="not printable ASCII"):
        _masks.alternative({"prefixes": ["é"], "mask": "99"})


def test_alternative_refuses_a_missing_mask():
    with pytest.raises(_masks.MaskError):
        _masks.alternative({"prefixes": ["A"]})


# -- overlap ----------------------------------------------------------------------------------


def test_overlap_names_an_example_both_take():
    first = _masks.alternative({"prefixes": ["T"], "mask": "999"})
    second = _masks.alternative({"mask": "A999"})  # the default prefix: one empty prefix
    assert _masks.overlap([first, second]) == (0, 1, "T000")


def test_overlap_example_is_taken_by_both_alternatives():
    alternatives = _alts({"prefixes": ["T"], "mask": "999"}, {"mask": "A999"})
    _, _, example = _masks.overlap(alternatives)
    assert all(re.fullmatch(a.regex(), example) for a in alternatives)


def test_values_of_different_lengths_never_overlap():
    assert _masks.overlap(_alts({"mask": "99"}, {"mask": "999"})) is None
    assert _masks.overlap(_alts({"prefixes": ["A"], "mask": "9"}, {"mask": "999"})) is None


def test_different_prefixes_with_the_same_mask_do_not_overlap():
    first = _masks.alternative({"prefixes": ["MSC", "TWR"], "mask": "99999"})
    second = _masks.alternative({"prefixes": ["MSD"], "mask": "99999"})
    assert _masks.overlap([first, second]) is None


def test_the_rfc_example_has_no_overlap():
    assert _masks.overlap(_alts(*RFC_FORMAT)) is None


def test_overlap_reports_the_first_pair_in_order():
    alternatives = _alts({"mask": "99"}, {"mask": "AA"}, {"mask": "9X"}, {"mask": "X9"})
    assert _masks.overlap(alternatives)[:2] == (0, 2)


def test_digit_and_letter_symbols_do_not_overlap_but_x_overlaps_both():
    assert _masks.overlap(_alts({"mask": "99"}, {"mask": "AA"})) is None
    assert _masks.overlap(_alts({"mask": "AA"}, {"mask": "XX"})) == (0, 1, "AA")
    assert _masks.overlap(_alts({"mask": "99"}, {"mask": "XX"})) == (0, 1, "00")


def test_a_literal_in_one_and_a_symbol_in_the_other_overlap_when_the_set_contains_it():
    assert _masks.overlap(_alts({"mask": "A-9"}, {"mask": "A99"})) is None
    assert _masks.overlap(_alts({"mask": "A\\5"}, {"mask": "A9"})) == (0, 1, "A5")


def test_a_single_alternative_never_overlaps_itself():
    assert _masks.overlap(_alts({"prefixes": ["A", "AB"], "mask": "99"})) is None


# -- capacity ----------------------------------------------------------------------------------


def test_capacity_counts_prefixes_times_symbol_choices():
    assert _masks.alternative({"prefixes": ["MSC", "TWR"], "mask": "99999"}).capacity() == 200_000
    assert _masks.alternative({"mask": "AX"}).capacity() == 26 * 36 == 936
    assert _masks.alternative({"mask": "9"}).capacity() == 10
    assert _masks.alternative({"mask": "99\\-9"}).capacity() == 1000


def test_capacity_ignores_literals_in_the_mask():
    assert _masks.alternative({"prefixes": ["LEGACY-"], "mask": "A9-99AA"}).capacity() == (
        26 * 10 * 10 * 10 * 26 * 26
    )


def test_overlap_of_thousands_of_prefixes_is_fast():
    import time

    tail = "A" * 30 + "9" * 30
    alternatives = [
        _masks.alternative({"prefixes": [f"{s}{i:04d}" for i in range(2000)], "mask": tail})
        for s in "PQ"
    ]
    started = time.monotonic()
    assert _masks.overlap(alternatives) is None
    assert time.monotonic() - started < 5


def test_overlap_finds_a_prefix_that_a_symbol_covers():
    alternatives = [
        _masks.alternative({"prefixes": ["T"], "mask": "999"}),
        _masks.alternative({"mask": "A999"}),
    ]
    assert _masks.overlap(alternatives) == (0, 1, "T000")
