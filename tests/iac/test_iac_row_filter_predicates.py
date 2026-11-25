# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A row-filter predicate is one boolean expression over the expose's own columns.

``exposes[].policy.authz.rowFilters[].where`` is written into the platform's policy
(a BigQuery row access policy, a Lake Formation data cells filter) as it stands, so
it is parsed rather than trusted. The parse lives in
``fluid_build.providers._row_filters``, shared with the MCP output-port gateway, which
applies the same filters itself.
"""

from __future__ import annotations

import pytest

from fluid_build.iac.base import UnsupportedBindingError
from fluid_build.iac.row_access import row_filters_for

ANALYSTS = "group:analysts@northwind.example"


def _expose(where):
    return {
        "exposeId": "customers",
        "contract": {"schema": [{"name": "region"}, {"name": "consent"}]},
        "policy": {"authz": {"rowFilters": [{"principal": ANALYSTS, "where": where}]}},
    }


@pytest.mark.parametrize(
    "where",
    [
        pytest.param("EXISTS (SELECT 1 FROM t)", id="exists-select"),
        pytest.param("EXISTS (FROM customers)", id="exists-from"),
        pytest.param("region = 'EU' INTERSECT SELECT 1", id="set-operation"),
        pytest.param("region = 'EU' HAVING 1 = 1", id="having"),
        pytest.param("region = 'EU' QUALIFY 1 = 1", id="qualify"),
        pytest.param("region = 'EU' WINDOW w AS (PARTITION BY consent)", id="window"),
        pytest.param("region = 'EU' ORDER BY consent", id="order-by"),
        pytest.param("region = ?", id="positional-placeholder"),
        pytest.param("region = :region", id="named-placeholder"),
        pytest.param("region = 'EU", id="unterminated-quote"),
    ],
)
def test_a_predicate_that_is_not_one_expression_is_refused(where):
    with pytest.raises(UnsupportedBindingError) as caught:
        row_filters_for(_expose(where))
    assert caught.value.kind == "row-filter"
    assert "rowFilters[0]" in str(caught.value)


@pytest.mark.parametrize(
    "raw", [pytest.param({}, id="mapping"), pytest.param("region = 'EU'", id="string")]
)
def test_row_filters_that_are_not_a_list_are_refused(raw):
    expose = _expose("region = 'EU'")
    expose["policy"]["authz"]["rowFilters"] = raw
    with pytest.raises(UnsupportedBindingError, match="must be a list of filters"):
        row_filters_for(expose)


def test_a_plain_predicate_still_parses_with_its_columns():
    (flt,) = row_filters_for(_expose("region = 'EU' AND consent = true"))
    assert flt.principal == ANALYSTS
    assert flt.columns == ("region", "consent")
    assert flt.name == "analysts_rows"
    assert row_filters_for({"exposeId": "x"}) == ()
    assert row_filters_for(_expose("region = 'EU'") | {"policy": {"authz": {}}}) == ()
