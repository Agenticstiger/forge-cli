# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The profile document's envelope, canonical bytes and digest."""

from __future__ import annotations

import hashlib
import json

import jsonschema
import pytest
import rfc8785

from fluid_build.profiler.disclosure import ACRO_DEFAULT
from fluid_build.profiler.document import (
    SCHEMA_URL,
    SCHEMA_URL_1_1_0,
    SCHEMA_URL_1_2_0,
    SCHEMA_URL_1_3_0,
    SCHEMA_URL_1_4_0,
    DocumentError,
    build_document,
    canonical_bytes,
    digest_of,
    producer_uri,
    schema_url_for,
)
from tests.profiler._fake_backend import FIXTURES, SCHEMA_PATH, SCHEMA_SHA256
from tests.profiler._needs_011 import needs_flux_stats_011

pytestmark = pytest.mark.unit


def _table():
    return {
        "rowCount": 1000,
        "columns": {
            "amount": {
                "domain": "number",
                "metrics": {
                    "sum": {
                        "definition": "sum/1",
                        "status": "F",
                        "sampleSize": {"min": 998, "max": 1002},
                        "disclosure": "acro-default",
                        "value": "1234.50",
                    },
                    "min": {
                        "definition": "min/1",
                        "status": "A",
                        "sampleSize": {"min": 998, "max": 1002},
                        "disclosure": "acro-default",
                    },
                },
            }
        },
    }


def _document(**overrides):
    fields = dict(producer=producer_uri("0.31.0"), rule=ACRO_DEFAULT, tables={"orders": _table()})
    fields.update(overrides)
    return build_document(**fields)


def test_the_vendored_schema_is_the_pinned_flux_engine_file():
    assert hashlib.sha256(SCHEMA_PATH.read_bytes()).hexdigest() == SCHEMA_SHA256
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    assert schema["$id"] == SCHEMA_URL


def test_the_vendored_example_validates_against_the_vendored_schema():
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    example = json.loads((FIXTURES / "profile-example.json").read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(schema).validate(example)


def test_a_built_document_is_format_1_0_0():
    document = _document()
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(schema).validate(document)
    assert document["_schemaURL"] == SCHEMA_URL
    assert document["encoding"] == {"fractionalBits": 16, "rounding": "floor"}
    assert document["disclosureRules"] == {"acro-default": ACRO_DEFAULT.to_document()}


def _with_statistic(definition):
    table = _table()
    table["columns"]["amount"]["statistics"] = [{"definition": definition, "groups": []}]
    return table


def test_a_quantile_with_bins_needs_format_1_1_0_and_nothing_else_does():
    assert _document(tables={"orders": _with_statistic("quantile/2")})["_schemaURL"] == (
        SCHEMA_URL_1_1_0
    )
    for definition in ("mean/1", "marginalHistogram/1", "levelHistogram/1", "quantile/1"):
        document = _document(tables={"orders": _with_statistic(definition)})
        assert document["_schemaURL"] == SCHEMA_URL
    assert _document()["_schemaURL"] == SCHEMA_URL


def test_a_document_with_invariants_needs_format_1_2_0():
    fact = {"name": "orders.rule", "statistics": []}
    built = build_document(
        producer="p",
        rule=ACRO_DEFAULT,
        tables={"orders": _with_statistic("quantile/2")},
        invariants=[fact],
    )
    assert built["_schemaURL"] == SCHEMA_URL_1_2_0 and built["invariants"] == [fact]
    assert "invariants" not in _document()


def _with_pair(*definitions):
    table = _table()
    table["columnPairs"] = [
        {
            "columns": ["a", "b"],
            "statistics": [{"definition": each, "groups": []} for each in definitions],
        }
    ]
    return table


def _with_grouping(**grouping):
    table = _table()
    table["columns"]["amount"]["statistics"] = [
        {"definition": "levelHistogram/1", "groups": [], **grouping}
    ]
    return table


_LINK = {"parent": {"table": "p", "columns": ["k"]}, "columns": ["k"], "column": "c"}
_BINS = {"column": "c", "bins": ["0", "1"]}


@pytest.mark.parametrize(
    ("tables", "relationships", "expected"),
    [
        ({"orders": _with_pair("correlation/1")}, [], SCHEMA_URL),
        ({"orders": _with_pair("kendallTau/1")}, [], SCHEMA_URL_1_4_0),
        ({"orders": _with_pair("correlation/1", "spearmanRho/1")}, [], SCHEMA_URL_1_4_0),
        ({"orders": _with_grouping(groupBy="c")}, [], SCHEMA_URL),
        ({"orders": _with_grouping(groupByLink=_LINK)}, [], SCHEMA_URL_1_3_0),
        ({"orders": _with_grouping(groupByBins=_BINS)}, [], SCHEMA_URL_1_4_0),
        ({"orders": _with_grouping(rowFloor=5)}, [], SCHEMA_URL_1_4_0),
        ({"orders": {**_table(), "cliqueSelection": {}}}, [], SCHEMA_URL_1_4_0),
        (
            {"orders": {**_table(), "cliques": [{"definition": "cliqueMarginal/2"}]}},
            [],
            SCHEMA_URL_1_4_0,
        ),
        (
            {"orders": {**_table(), "cliques": [{"definition": "cliqueMarginal/1"}]}},
            [],
            SCHEMA_URL,
        ),
        (
            {"orders": _table()},
            [{"statistics": [{"definition": "marginalHistogram/1"}]}],
            SCHEMA_URL,
        ),
        ({"orders": _table()}, [{"statistics": [{"groupBy": "c"}]}], SCHEMA_URL_1_4_0),
        ({"orders": _table()}, [{"statistics": [{"groupByBins": _BINS}]}], SCHEMA_URL_1_4_0),
        # Each format holds the ones before it, so the least is the latest any form needs.
        ({"orders": _with_statistic("quantile/2")}, [], SCHEMA_URL_1_1_0),
        (
            {"orders": _with_grouping(groupByLink=_LINK, definition="quantile/2")},
            [],
            SCHEMA_URL_1_3_0,
        ),
    ],
)
def test_a_document_names_the_least_format_that_holds_it(tables, relationships, expected):
    assert schema_url_for(tables, relationships=relationships) == expected
    assert schema_url_for(tables, relationships=relationships, invariants=True) == (
        expected if expected not in (SCHEMA_URL, SCHEMA_URL_1_1_0) else SCHEMA_URL_1_2_0
    )


def test_the_formats_are_the_ones_flux_stats_reads():
    profile = pytest.importorskip("flux_stats.profile")
    assert SCHEMA_URL == profile.SCHEMA_URL
    assert SCHEMA_URL_1_1_0 == profile.SCHEMA_URL_1_1_0
    assert SCHEMA_URL_1_2_0 == profile.SCHEMA_URL_1_2_0
    assert SCHEMA_URL_1_3_0 == profile.SCHEMA_URL_1_3_0


@needs_flux_stats_011
def test_the_newest_format_is_the_one_flux_stats_reads():
    profile = pytest.importorskip("flux_stats.profile")
    assert SCHEMA_URL_1_4_0 == profile.SCHEMA_URL_1_4_0


def test_the_producer_names_this_repository_at_the_release():
    assert producer_uri("0.31.0") == (
        "https://github.com/Agenticstiger/forge-cli/tree/v0.31.0/fluid_build/profiler"
    )


def test_canonical_bytes_are_rfc_8785_and_the_digest_is_their_sha256():
    document = _document()
    canonical = canonical_bytes(document)
    assert canonical == rfc8785.dumps(document)
    assert b": " not in canonical and b", " not in canonical  # no insignificant whitespace
    assert digest_of(canonical) == "sha256:" + hashlib.sha256(canonical).hexdigest()


def test_reordering_keys_keeps_the_digest_and_changing_a_value_moves_it():
    document = _document()
    shuffled = json.loads(json.dumps(document), object_pairs_hook=lambda p: dict(reversed(p)))
    assert digest_of(canonical_bytes(shuffled)) == digest_of(canonical_bytes(document))
    changed = _document()
    changed["tables"]["orders"]["rowCount"] = 1005
    assert digest_of(canonical_bytes(changed)) != digest_of(canonical_bytes(document))


@pytest.mark.parametrize(
    "mutate, where",
    [
        (lambda d: d["tables"]["orders"].__setitem__("rowCount", 12.5), "tables.orders.rowCount"),
        (lambda d: d["tables"]["orders"].__setitem__("rowCount", 2**53), "tables.orders.rowCount"),
        (lambda d: d["tables"]["orders"]["columns"].__setitem__("café", {}), "columns"),
        (lambda d: d.__setitem__("_producer", "é"), "_producer"),
        (lambda d: d["tables"]["orders"].__setitem__("rowCount", object()), "rowCount"),
    ],
)
def test_what_the_format_cannot_carry_is_refused_naming_where(mutate, where):
    document = _document()
    mutate(document)
    with pytest.raises(DocumentError) as raised:
        canonical_bytes(document)
    assert where in str(raised.value)
    assert "12.5" not in str(raised.value)


@pytest.mark.parametrize("bits", [-1, 129, True, "16"])
def test_the_grid_lies_from_0_to_128_bits(bits):
    with pytest.raises(DocumentError):
        _document(fractional_bits=bits)


def test_a_document_needs_a_table():
    with pytest.raises(DocumentError):
        _document(tables={})
