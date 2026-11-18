# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid_build.util.column_type`` steps over quoted text when it splits a column type."""

from __future__ import annotations

import random
import time

import pytest

from fluid_build.util.column_type import (
    read_enum_list,
    split_param_list,
    split_type_params,
    write_enum_list,
)

pytestmark = pytest.mark.unit

TAB = chr(9)


@pytest.mark.parametrize(
    "text, expected",
    [
        ("decimal(18,4)", ("decimal", "18,4")),
        ("  varchar ( 255 )  ", ("varchar", "255")),
        ("string", ("string", None)),
        ("varchar()", ("varchar", "")),
        ("enum('a)b', 'c')", ("enum", "'a)b', 'c'")),
        ("enum('O''Brien')", ("enum", "'O''Brien'")),
        ('enum("a)""b")', ("enum", '"a)""b"')),
        ("enum('(')", ("enum", "'('")),
        ("enum('a'", ("enum('a'", None)),
        ("enum('a)", ("enum", "'a")),
        ("varchar(a'b)", ("varchar", "a'b")),
        ("varchar(255')", ("varchar", "255'")),
        ("enum(a", ("enum(a", None)),
        ("map(string, array(int))", ("map(string, array(int))", None)),
        ("decimal(10,2) extra", ("decimal(10,2) extra", None)),
    ],
)
def test_split_type_params(text, expected):
    assert split_type_params(text) == expected


@pytest.mark.parametrize(
    "params, expected",
    [
        ("18, 4", ["18", "4"]),
        ("'a, b', 'c'", ["'a, b'", "'c'"]),
        ("'O''Brien', \"x,y\"", ["'O''Brien'", '"x,y"']),
        ("a,,b", ["a", "", "b"]),
        ("", [""]),
        ("'unclosed, x", ["'unclosed", "x"]),
        ("a'b, c", ["a'b", "c"]),
    ],
)
def test_split_param_list(params, expected):
    assert split_param_list(params) == expected


@pytest.mark.parametrize(
    "text",
    [
        "x(" + "'" * 100001,
        "x(" + "'\"" * 50000,
        "x(" + "a''" * 30000 + "'",
        "x(" + "''" * 50000,
        "x('" + "a, " * 50000,
        "x('" + "a''" * 30000,
        "x(" + "'a', " * 30000 + "'",
        "x(" + "', \"" * 30000,
        "x(" + "'a, '' " * 20000 + "x",
        "x(" + "''' , " * 30000,
        "x('" + "''" * 50000 + "x, y",
    ],
    ids=[
        "unclosed-run",
        "alternating-quotes",
        "doubled-then-closed",
        "doubled-run",
        "list-unclosed-before-commas",
        "list-doubled-never-closed",
        "list-quoted-values",
        "list-alternating-quotes",
        "list-doubled-inside-values",
        "list-odd-quote-runs",
        "list-text-after-doubled-run",
    ],
)
def test_hostile_quotes_split_in_linear_time(text):
    started = time.perf_counter()
    split_type_params(text)
    split_param_list(text[2:])
    read_enum_list(text[2:])
    read_enum_list(text[2:], "|,")
    assert time.perf_counter() - started < 1.0


@pytest.mark.parametrize(
    "text, expected",
    [
        ("'a, b', 'c'", ["a, b", "c"]),
        ("'O''Brien', 'x'", ["O'Brien", "x"]),
        ('"a, b", "say ""hi"""', ["a, b", 'say "hi"']),
        ("'Dr.', '>10000', 'a (b)'", ["Dr.", ">10000", "a (b)"]),
        ("IE, United Kingdom", ["IE", "United Kingdom"]),
        ("'a, b', United Kingdom", ["a, b", "United Kingdom"]),
        ("' a ', ''", [" a ", ""]),
        ("  'a'  ,  'b'  ", ["a", "b"]),
        ("'a',", ["a"]),
        # A quote that does not open a value is an ordinary character, as before.
        ("O'Brien, D'Arcy", ["O'Brien", "D'Arcy"]),
        ("'80s, '90s", ["'80s", "'90s"]),
        ("'a'b, c", ["'a'b", "c"]),
        ("'unclosed, x", ["'unclosed", "x"]),
        ("x 'a, b'", ["x 'a", "b'"]),
        # A quoted string may not hold a control character (the 0.7.7 pattern).
        ("'a" + TAB + "b', c", ["'a" + TAB + "b'", "c"]),
        ("", []),
        (" , ,", []),
    ],
)
def test_read_enum_list(text, expected):
    assert read_enum_list(text) == expected


@pytest.mark.parametrize(
    "text, expected",
    [
        ("A | B", ["A", "B"]),
        ("'a|b' | c", ["a|b", "c"]),
        ("'a, b' | 'c'", ["a, b", "c"]),
        ("a|b, c", ["a", "b", "c"]),
    ],
)
def test_read_enum_list_at_pipes_too(text, expected):
    assert read_enum_list(text, "|,") == expected


@pytest.mark.parametrize(
    "values, text",
    [
        (["base_fee", "usage"], "base_fee, usage"),
        (["base_fee", "fee, late", "O'Brien"], "'base_fee', 'fee, late', 'O''Brien'"),
        (["", "a"], "'', 'a'"),
        ([" a", "b"], "' a', 'b'"),
        (['"a"', "b"], "'\"a\"', 'b'"),
        (["'a", "b'"], "'''a', 'b'''"),
        (["O'Brien", "b"], "O'Brien, b"),
    ],
)
def test_write_enum_list_reads_back(values, text):
    assert write_enum_list(values) == text
    assert read_enum_list(text) == values


def test_any_list_without_a_control_character_reads_back():
    rng = random.Random(20261118)
    alphabet = ("a", "B", " ", ",", "|", "'", '"', "-")
    for _ in range(20000):
        values = [
            "".join(rng.choices(alphabet, k=rng.randint(0, 5))) for _ in range(rng.randint(1, 4))
        ]
        assert read_enum_list(write_enum_list(values)) == values, values
