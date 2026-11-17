# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0
"""``fluid_build.util.column_type`` steps over quoted text when it splits a column type."""

from __future__ import annotations

import pytest

from fluid_build.util.column_type import split_param_list, split_type_params

pytestmark = pytest.mark.unit


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
        ("enum('a)", ("enum('a)", None)),
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
        ("'unclosed, x", ["'unclosed, x"]),
    ],
)
def test_split_param_list(params, expected):
    assert split_param_list(params) == expected
