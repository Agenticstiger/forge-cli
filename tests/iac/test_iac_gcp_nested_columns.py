# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A 0.7.7 contract that declares its nested columns reaches BigQuery as nested fields.

An array is a REPEATED field of its element's type, a struct a RECORD with its
fields, an array of structs a REPEATED RECORD, and a map a REPEATED RECORD of a key
and a value (BigQuery's own Avro load holds a map so). What BigQuery holds
differently is lossy, so ``fluid validate`` warns and ``--strict`` fails: a NULL
array is stored as an empty one, an array holds no NULL element, and a map held as
records does not keep its keys unique. What it cannot hold at all is refused, by
path. Glue reads the same declarations through the same parser, and its output is
pinned here as it was.
"""

from __future__ import annotations

import copy
import json
import logging

import pytest
import yaml

from fluid_build.cli import build_parser
from fluid_build.iac import get_iac_plugin
from fluid_build.iac.base import UnsupportedBindingError
from fluid_build.iac.column_types import EXACT, LOSSY, NESTED_FIX_DECLARE, REFUSED
from fluid_build.iac.providers.gcp import _bq_column_outcome, _bq_field

from .test_iac_column_type_gate import _gate

pytestmark = [pytest.mark.unit, pytest.mark.provider, pytest.mark.gcp]

#: Every array and element required: what BigQuery holds exactly.
_EXACT = [
    {"name": "id", "type": "string", "required": True},
    {
        "name": "tags",
        "type": "array",
        "required": True,
        "description": "free-form tags",
        "element": {"type": "string", "required": True},
    },
    {
        "name": "address",
        "type": "struct",
        "description": "postal address",
        "fields": [
            {"name": "street", "type": "string", "description": "street line"},
            {"name": "zip", "type": "string", "required": True},
        ],
    },
    {
        "name": "items",
        "type": "array",
        "required": True,
        "element": {
            "type": "struct",
            "required": True,
            "fields": [
                {"name": "sku", "type": "string", "required": True},
                {"name": "qty", "type": "integer"},
                {"name": "at", "type": "datetime"},
                {"name": "seen", "type": "timestamp"},
                {"name": "price", "type": "decimal(10,2)"},
            ],
        },
    },
]


def _contract(columns=None, *, platform="gcp"):
    if platform == "gcp":
        binding = {
            "platform": "gcp",
            "format": "bigquery_table",
            "location": {"project": "acme", "dataset": "demo", "table": "orders"},
        }
    else:
        binding = {
            "platform": "aws",
            "format": "parquet",
            "location": {
                "bucket": "acme-demo",
                "path": "orders/",
                "database": "demo",
                "table": "orders",
            },
        }
    return {
        "fluidVersion": "0.7.7",
        "kind": "DataProduct",
        "id": "demo.nested",
        "name": "Nested demo",
        "description": "nested BigQuery columns",
        "domain": "demo",
        "metadata": {"layer": "Silver", "owner": {"team": "data"}},
        "exposes": [
            {
                "exposeId": "orders",
                "version": "1.0.0",
                "kind": "table",
                "binding": binding,
                "contract": {"schema": copy.deepcopy(columns or _EXACT)},
            }
        ],
    }


def _table(contract):
    resources = get_iac_plugin("gcp").emit(contract)
    return next(iter(resources["google_bigquery_table"].values()))


def _fields(contract):
    return {f["name"]: f for f in json.loads(_table(contract)["schema"])}


def _column(name, **fields):
    return {"name": name, **fields}


# ── What is written ─────────────────────────────────────────────────────────


def test_declared_nested_columns_are_written_as_repeated_and_record_fields():
    contract = _contract()
    assert _gate(contract) == ([], [])
    fields = _fields(contract)
    assert fields["id"] == {
        "name": "id",
        "type": "STRING",
        "mode": "REQUIRED",
        "description": "",
    }
    assert fields["tags"] == {
        "name": "tags",
        "type": "STRING",
        "mode": "REPEATED",
        "description": "free-form tags",
    }
    assert fields["address"] == {
        "name": "address",
        "type": "RECORD",
        "mode": "NULLABLE",
        "description": "postal address",
        "fields": [
            {"name": "street", "type": "STRING", "mode": "NULLABLE", "description": "street line"},
            {"name": "zip", "type": "STRING", "mode": "REQUIRED", "description": ""},
        ],
    }
    items = fields["items"]
    assert (items["type"], items["mode"]) == ("RECORD", "REPEATED")
    assert [(f["name"], f["type"], f["mode"]) for f in items["fields"]] == [
        ("sku", "STRING", "REQUIRED"),
        ("qty", "INT64", "NULLABLE"),
        ("at", "DATETIME", "NULLABLE"),
        ("seen", "TIMESTAMP", "NULLABLE"),
        ("price", "NUMERIC", "NULLABLE"),
    ]


def test_a_nested_leaf_takes_the_type_a_top_level_column_of_its_type_gets():
    """The shared parser folds ``datetime`` into ``timestamp``; BigQuery keeps the two
    apart at the top level, so a nested field must too."""
    leaves = ["datetime", "timestamp", "timestamptz", "bigint", "float64", "bytes", "date"]
    struct = _column(
        "s",
        type="struct",
        fields=[{"name": f"f{i}", "type": t} for i, t in enumerate(leaves)],
    )
    nested = {f["name"]: f["type"] for f in _bq_field(struct)["fields"]}
    for i, leaf in enumerate(leaves):
        assert nested[f"f{i}"] == _bq_field(_column("x", type=leaf))["type"], leaf


def test_records_nest_inside_records_and_arrays_inside_records():
    column = _column(
        "order",
        type="struct",
        required=True,
        fields=[
            {
                "name": "lines",
                "type": "array",
                "required": True,
                "element": {
                    "type": "struct",
                    "required": True,
                    "fields": [
                        {
                            "name": "codes",
                            "type": "array",
                            "required": True,
                            "element": {"type": "int", "required": True},
                        }
                    ],
                },
            },
            {"name": "meta", "type": "struct", "fields": [{"name": "k", "type": "string"}]},
        ],
    )
    assert _bq_column_outcome(column).status == EXACT
    field = _bq_field(column)
    assert (field["type"], field["mode"]) == ("RECORD", "REQUIRED")
    lines, meta = field["fields"]
    assert (lines["type"], lines["mode"]) == ("RECORD", "REPEATED")
    assert lines["fields"] == [
        {"name": "codes", "type": "INT64", "mode": "REPEATED", "description": ""}
    ]
    assert (meta["type"], meta["mode"], meta["fields"][0]["type"]) == (
        "RECORD",
        "NULLABLE",
        "STRING",
    )


def test_fifteen_levels_of_record_are_held_and_sixteen_are_refused_by_path():
    def nested(levels):
        column = {"name": "d", "type": "string"}
        for _ in range(levels):
            column = {"name": "d", "type": "struct", "fields": [column]}
        return column

    assert _bq_column_outcome(nested(15)).status == EXACT
    outcome = _bq_column_outcome(nested(16))
    assert outcome.status == REFUSED
    assert "d" + ".d" * 15 + " is a RECORD 16 levels deep" in outcome.why
    assert "at most 15" in outcome.why


# ── NULL, empty and nested classification ──────────────────────────────────


def test_a_required_array_of_required_elements_is_exact_an_empty_one_included():
    """Required means never NULL; an empty array is a value BigQuery holds as is."""
    column = _column(
        "tags", type="array", required=True, element={"type": "string", "required": True}
    )
    outcome = _bq_column_outcome(column)
    assert (outcome.status, outcome.target) == (EXACT, "REPEATED STRING")


def test_a_nullable_array_is_lossy_because_bigquery_stores_null_as_empty():
    column = _column("tags", type="array", element={"type": "string", "required": True})
    outcome = _bq_column_outcome(column)
    assert (outcome.status, outcome.target) == (LOSSY, "REPEATED STRING")
    assert "tags may be NULL, which BigQuery stores as an empty array" in outcome.why
    assert "Set required: true on tags if it is never NULL." in outcome.fix
    assert "element" not in outcome.why


def test_a_nullable_element_is_lossy_because_an_array_holds_no_null():
    column = _column("tags", type="array", required=True, element={"type": "string"})
    outcome = _bq_column_outcome(column)
    assert outcome.status == LOSSY
    assert "an element of tags may be NULL, which BigQuery cannot hold in an array" in outcome.why
    assert outcome.fix == "Set required: true on tags.element if no element is NULL."


def test_a_nullable_array_inside_a_record_is_lossy_by_its_path():
    column = _column(
        "addr",
        type="struct",
        fields=[
            {"name": "lines", "type": "array", "element": {"type": "string", "required": True}}
        ],
    )
    outcome = _bq_column_outcome(column)
    assert (outcome.status, outcome.target) == (LOSSY, "RECORD")
    assert "addr.lines may be NULL" in outcome.why
    assert "Set required: true on addr.lines if it is never NULL." in outcome.fix


def test_a_nullable_struct_and_its_nullable_fields_are_exact():
    """A RECORD and its fields hold NULL as BigQuery stores it."""
    column = _column("addr", type="struct", fields=[{"name": "street", "type": "string"}])
    assert _bq_column_outcome(column).status == EXACT


def test_a_lossy_leaf_makes_its_column_lossy_and_names_the_leaf():
    column = _column("money", type="struct", fields=[{"name": "amount", "type": "decimal(38,10)"}])
    outcome = _bq_column_outcome(column)
    assert (outcome.status, outcome.target) == (LOSSY, "RECORD")
    assert "money.amount is declared 'decimal(38,10)'" in outcome.why
    assert "bignumeric" in outcome.fix


def test_every_loss_is_one_warning_per_column_at_validate():
    columns = [
        _column("id", type="string"),
        _column("tags", type="array", element={"type": "string"}),
    ]
    errors, warnings = _gate(_contract(columns))
    assert errors == []
    assert len(warnings) == 1
    assert warnings[0].startswith("expose 'orders': column 'tags' is declared 'array', where ")
    assert "it is written as 'REPEATED STRING'" in warnings[0]


# ── A map ───────────────────────────────────────────────────────────────────

_MAP = _column(
    "attrs",
    type="map",
    required=True,
    key={"type": "string", "required": True},
    value={"type": "integer"},
)


def test_a_map_is_a_repeated_record_of_key_and_value_and_is_lossy():
    field = _bq_field(_MAP)
    assert field == {
        "name": "attrs",
        "type": "RECORD",
        "mode": "REPEATED",
        "description": "",
        "fields": [
            {"name": "key", "type": "STRING", "mode": "REQUIRED", "description": ""},
            {"name": "value", "type": "INT64", "mode": "NULLABLE", "description": ""},
        ],
    }
    outcome = _bq_column_outcome(_MAP)
    assert (outcome.status, outcome.target) == (LOSSY, "REPEATED RECORD")
    assert "attrs is a map, held as an array of key and value records" in outcome.why
    assert "keys BigQuery does not keep unique" in outcome.why
    assert "Declare attrs as an array of structs with key and value fields" in outcome.fix


def test_the_array_of_key_value_structs_the_fix_names_is_the_same_field_and_exact():
    declared = _column(
        "attrs",
        type="array",
        required=True,
        element={
            "type": "struct",
            "required": True,
            "fields": [
                {"name": "key", "type": "string", "required": True},
                {"name": "value", "type": "integer"},
            ],
        },
    )
    assert _bq_column_outcome(declared).status == EXACT
    assert _bq_field(declared) == _bq_field(_MAP)


def test_a_nullable_map_is_also_lossy_for_its_null():
    column = {**_MAP, "required": False}
    assert "attrs may be NULL, which BigQuery stores as an empty array" in (
        _bq_column_outcome(column).why
    )


# ── Refusals, by path ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "element, held",
    [
        (
            {"type": "array", "required": True, "element": {"type": "int", "required": True}},
            "an array",
        ),
        (
            {
                "type": "map",
                "required": True,
                "key": {"type": "string", "required": True},
                "value": {"type": "string"},
            },
            "a map, held as an array of records",
        ),
    ],
    ids=["array-of-arrays", "array-of-maps"],
)
def test_an_array_of_arrays_is_refused_by_path(element, held):
    column = _column("m", type="array", required=True, element=element)
    outcome = _bq_column_outcome(column)
    assert outcome.status == REFUSED
    assert f"m.element is {held}, and a BigQuery array cannot hold an array" in outcome.why
    assert outcome.fix == "Declare the element as a struct with one array field, or declare json."


def test_a_nested_type_the_shared_parser_refuses_is_refused_with_its_reason():
    column = _column("s", type="struct", fields=[{"name": "amt", "type": "numeric"}])
    outcome = _bq_column_outcome(column)
    assert outcome.status == REFUSED
    assert outcome.why.startswith("whose nested types BigQuery cannot hold (s.amt:")


def test_the_validate_refusal_is_the_emit_refusal_word_for_word():
    contract = _contract(
        [
            _column(
                "m",
                type="array",
                required=True,
                element={"type": "array", "element": {"type": "int"}},
            )
        ]
    )
    errors, _ = _gate(contract)
    with pytest.raises(UnsupportedBindingError) as caught:
        get_iac_plugin("gcp").emit(contract)
    assert caught.value.kind == "column-type"
    assert f"{caught.value} {' '.join(caught.value.remediation)}" in errors
    assert (
        "'m' is declared 'array', whose nested types BigQuery cannot hold (m.element" in errors[0]
    )


@pytest.mark.parametrize("bare", ["array", "struct", "map", "array(string)"])
def test_a_bare_nested_type_is_still_refused_and_told_to_declare_its_shape(bare):
    errors, _ = _gate(_contract([_column("x", type=bare)]))
    assert len(errors) == 1 and f"'x' is declared {bare!r}" in errors[0], errors
    assert NESTED_FIX_DECLARE in errors[0]
    assert "does not write either yet" not in errors[0]


# ── Policy tags ─────────────────────────────────────────────────────────────


_PLATFORM = "group:data-platform@acme.example"
_ANALYSTS = "group:analysts@acme.example"


def _restricted(columns, restricted):
    """``restricted`` readable by the platform group only."""
    contract = _contract(columns)
    contract["accessPolicy"] = {
        "grants": [
            {"principal": _PLATFORM, "permissions": ["read"]},
            {"principal": _ANALYSTS, "permissions": ["read"]},
        ]
    }
    expose = contract["exposes"][0]
    expose["binding"]["principals"] = {
        _PLATFORM: "group:data-platform@acme.com",
        _ANALYSTS: "group:analysts@acme.com",
    }
    expose["policy"] = {
        "authz": {
            "columnRestrictions": [
                {"principal": _ANALYSTS, "columns": [restricted], "access": "deny"}
            ]
        }
    }
    return contract


def test_a_restriction_on_a_record_column_is_refused_at_validate_and_emit():
    """BigQuery attaches a policy tag to a leaf field only."""
    contract = _restricted(_EXACT, "address")
    errors, _ = _gate(contract)
    record = [e for e in errors if "holds as a RECORD" in e]
    assert record and "restricts 'address'" in record[0], errors
    with pytest.raises(UnsupportedBindingError) as caught:
        get_iac_plugin("gcp").emit(contract)
    assert caught.value.kind == "column-restriction-record"


def test_a_restriction_on_a_repeated_leaf_column_takes_its_policy_tag():
    contract = _restricted(_EXACT, "tags")
    assert _gate(contract)[0] == []
    schema = json.loads(str(_table(contract)["schema"]))
    tags = next(f for f in schema if f["name"] == "tags")
    assert tags["mode"] == "REPEATED" and tags["policyTags"]["names"]


def test_a_nested_description_cannot_inject_an_interpolation_into_the_tagged_schema():
    """The policy-tag schema is a raw expression; every contract string in it, nested
    ones included, must reach OpenTofu escaped."""
    columns = copy.deepcopy(_EXACT)
    columns[2]["fields"][0]["description"] = '${file("/etc/passwd")} %{ if true }x%{ endif }'
    schema = str(_table(_restricted(columns, "tags"))["schema"])
    assert '$${file(\\"/etc/passwd\\")}' in schema
    assert "%%{ if true }" in schema
    assert "${file" not in schema.replace("$${file", "")


def test_a_mapping_as_a_description_cannot_inject_through_its_key():
    """``generate iac`` does not run the schema check, so a description can be a
    mapping; the escape reaches its values but not its keys, so it is written as text."""
    payload = {'${file("/etc/passwd")}': 1}
    columns = copy.deepcopy(_EXACT)
    columns[0]["description"] = payload
    columns[1]["description"] = payload
    columns[2]["fields"][0]["description"] = payload
    schema = str(_table(_restricted(columns, "tags"))["schema"])
    assert "${file" not in schema.replace("$${file", "")
    assert schema.count("$${file") == 3


# ── Glue reads the same declarations as before ─────────────────────────────


def test_glue_output_for_the_same_declarations_is_unchanged():
    resources = get_iac_plugin("aws").emit(_contract(platform="aws"))
    table = next(iter(resources["aws_glue_catalog_table"].values()))
    assert [(c["name"], c["type"]) for c in table["storage_descriptor"]["columns"]] == [
        ("id", "string"),
        ("tags", "array<string>"),
        ("address", "struct<street:string,zip:string>"),
        (
            "items",
            "array<struct<sku:string,qty:int,at:timestamp,seen:timestamp,price:decimal(10,2)>>",
        ),
    ]


# ── Through the CLI ─────────────────────────────────────────────────────────


def _run(command, tmp_path, contract, *flags):
    path = tmp_path / "contract.fluid.yaml"
    path.write_text(yaml.safe_dump(contract, sort_keys=False), encoding="utf-8")
    argv = {"validate": [str(path), "--offline"], "generate": ["iac", str(path)]}[command]
    args = build_parser().parse_args([command, *argv, *flags])
    return args.func(args, logging.getLogger("test"))


def test_strict_validate_passes_exact_nested_columns_and_fails_a_nullable_array(tmp_path, capsys):
    assert _run("validate", tmp_path, _contract(), "--strict") == 0
    nullable = _contract(_EXACT + [_column("notes", type="array", element={"type": "string"})])
    assert _run("validate", tmp_path, nullable) == 0
    assert _run("validate", tmp_path, nullable, "--strict") == 1
    seen = capsys.readouterr()
    assert "notes may be NULL" in seen.out + seen.err
