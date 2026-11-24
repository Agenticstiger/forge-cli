# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for ``fluid_build.util.excerpt``: a value as a message quotes it."""

from __future__ import annotations

import pytest

from fluid_build.util.excerpt import EXCERPT_CHARS, excerpt, spelled


@pytest.mark.parametrize("text", ["", "./x.yaml#/a", "naïve 名前 'quoted' \\ back", "a" * 200])
def test_a_short_printable_value_is_quoted_as_written(text: str) -> None:
    assert excerpt(text) == text


def test_a_long_value_is_cut_naming_what_was_left_out() -> None:
    assert excerpt("k" * 50_000) == f"{'k' * EXCERPT_CHARS}... (49,800 more characters)"
    assert excerpt("abcdef", limit=4) == "abcd... (2 more characters)"


@pytest.mark.parametrize(
    ("text", "shown"),
    [
        ("a\n2026-10-29 INFO forged", "a\\n2026-10-29 INFO forged"),
        ("x\r\ny\tz", "x\\r\\ny\\tz"),
        ("\x1b[2K\x1b]0;owned\x07", "\\x1b[2K\\x1b]0;owned\\x07"),
        ("\x00\x7f\x85\x9b", "\\x00\\x7f\\x85\\x9b"),
        ("ab\u202ecd\u2066\u2028\ufeff", "ab\\u202ecd\\u2066\\u2028\\ufeff"),
        ("\ud800 \xa0", "\\ud800 \\xa0"),
    ],
    ids=["line-feed", "crlf-tab", "escape-sequences", "c0-c1", "bidi-and-separators", "other"],
)
def test_every_character_that_is_not_printable_is_spelled_as_repr_writes_it(
    text: str, shown: str
) -> None:
    """A line feed would start a forged log line; an escape sequence or a bell reaches the
    terminal; a bidi control turns the text around it. ``str.isprintable`` refuses each."""
    assert excerpt(text) == shown
    assert excerpt(text).isprintable()
    assert spelled(text) == shown


def test_spelled_characters_count_toward_the_cut() -> None:
    """The quoted text stays bounded however the value is made: 200 escape characters are
    spelled in 800, so only the first 50 fit."""
    quoted = excerpt("\x1b" * 200)

    assert quoted == "\\x1b" * 50 + "... (150 more characters)"
    assert quoted.isprintable()


def test_a_path_is_spelled_whole_however_long() -> None:
    """A file path is bounded by the operating system, and its end is the file's name: an
    excerpt of a deep path kept its first 200 characters and lost the name. ``spelled``
    spells what ``excerpt`` spells, and cuts nothing."""
    path = "/" + "/".join(["d" * 100] * 5) + "/settings.conf"
    deep = "/" + "d" * 300 + "/t\n2026-10-29 INFO forged\x1b]0;x\x07.conf"

    assert len(path) > EXCERPT_CHARS and spelled(path) == path
    assert spelled(deep) == "/" + "d" * 300 + "/t\\n2026-10-29 INFO forged\\x1b]0;x\\x07.conf"
    assert spelled(deep).isprintable()
