# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``decimal(p,s)`` has a FLUID SQL/1 type only when 1 <= p and 0 <= s <= p, and a message
about one that has none says why (RFC-data-rules.md section 2.3). The conformance vectors
pin the boundaries; these cases cover spellings and the hint."""

from __future__ import annotations

import pytest

from fluid_build.fluid_sql._types import column_type, decimal_hint

pytestmark = pytest.mark.unit

HINT = " (a decimal is decimal(p) or decimal(p, s), with 1 <= p <= 1000 and 0 <= s <= p)"


@pytest.mark.parametrize(
    "declared, scale",
    [
        ("decimal(1)", 0),
        ("DECIMAL( 1 , 1 )", 1),
        ("Numeric(1000,1000)", 1000),
        ("number(0012, 02)", 2),
        ("bignumeric(38, 38)", 38),
    ],
)
def test_a_decimal_within_the_bounds_has_its_scale(declared, scale):
    read = column_type(declared)
    assert read is not None and read.type.scale == scale
    assert decimal_hint(declared) == ""


@pytest.mark.parametrize(
    "declared",
    [
        "decimal(0)",
        "DEC(0, 0)",
        "numeric(0,2)",
        "decimal(5,6)",
        "decimal(1000,1001)",
        "decimal(1001)",
        "decimal(99999, 2)",
        "decimal(2, 99999)",
        "decimal(1, 2, 3)",
        "decimal(-1)",
    ],
)
def test_a_decimal_outside_the_bounds_has_no_type_and_says_why(declared):
    assert column_type(declared) is None
    assert decimal_hint(declared) == HINT


@pytest.mark.parametrize("declared", ["decimal", "json", "time(3)", "array(decimal(5,6))", 42])
def test_the_hint_is_only_for_a_decimal_with_parameters(declared):
    assert decimal_hint(declared) == ""
