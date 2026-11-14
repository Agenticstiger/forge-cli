# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A 0.7.7 contract that declares its nested columns reaches Glue as typed columns.

0.7.7 lets a column declare ``fields``, ``element``, ``key`` and ``value``. Glue
holds the nested type once it has those, so the gate accepts the declared column
and the emitter writes its Hive type. A bare ``struct`` (all 0.7.5 and 0.7.6 could
write) is still refused, as is a nested type Glue cannot hold.
"""

from __future__ import annotations

import copy
import logging

import pytest
import yaml

from fluid_build.cli import build_parser
from fluid_build.iac import get_iac_plugin
from fluid_build.iac.base import UnsupportedBindingError
from fluid_build.iac.providers.aws import validate_aws_column_types
from fluid_build.streaming.types import parse_type

pytestmark = [pytest.mark.unit, pytest.mark.provider]

_COLUMNS = [
    {"name": "id", "type": "string"},
    {
        "name": "addr",
        "type": "struct",
        "fields": [
            {"name": "street", "type": "string"},
            {"name": "amount", "type": "decimal(18,2)"},
        ],
    },
    {"name": "tags", "type": "array", "element": {"type": "string"}},
    {
        "name": "m",
        "type": "map",
        "key": {"type": "string", "required": True},
        "value": {"type": "string"},
    },
]


def _contract(columns=None):
    return {
        "fluidVersion": "0.7.7",
        "kind": "DataProduct",
        "id": "demo.nested",
        "name": "Nested demo",
        "description": "nested Glue columns",
        "domain": "demo",
        "metadata": {"layer": "Silver", "owner": {"team": "data"}},
        "exposes": [
            {
                "exposeId": "orders",
                "version": "1.0.0",
                "kind": "table",
                "binding": {
                    "platform": "aws",
                    "format": "parquet",
                    "location": {
                        "bucket": "acme-demo",
                        "path": "orders/",
                        "database": "demo",
                        "table": "orders",
                    },
                },
                "contract": {"schema": copy.deepcopy(columns or _COLUMNS)},
            }
        ],
    }


def _glue_columns(contract):
    resources = get_iac_plugin("aws").emit(contract)
    table = next(iter(resources["aws_glue_catalog_table"].values()))
    return {c["name"]: c["type"] for c in table["storage_descriptor"]["columns"]}


def test_declared_nested_columns_are_accepted_and_written_as_hive_types():
    contract = _contract()
    assert validate_aws_column_types(contract) == ([], [])
    assert _glue_columns(contract) == {
        "id": "string",
        "addr": "struct<street:string,amount:decimal(18,2)>",
        "tags": "array<string>",
        "m": "map<string,string>",
    }


def test_the_hive_form_is_lowercase_and_unspaced_throughout():
    spec = {
        "type": "array",
        "element": {
            "type": "struct",
            "fields": [
                {"name": "p", "type": "decimal(10,2)"},
                {"name": "t", "type": "timestamp(3)"},
            ],
        },
    }
    assert parse_type(spec).sql("hive") == "array<struct<p:decimal(10,2),t:timestamp>>"
    assert parse_type({"type": "decimal(5,0)"}).sql("hive") == "decimal(5,0)"
    assert parse_type({"type": "decimal(5,0)"}).sql("flink") == "DECIMAL(5,0)"


def test_a_bare_nested_type_is_still_refused_with_the_existing_wording():
    for bare in ("struct", "array", "map"):
        contract = _contract([{"name": "x", "type": bare}])
        errors, _ = validate_aws_column_types(contract)
        assert len(errors) == 1 and f"'x' is declared {bare!r}" in errors[0], errors
        assert "cannot declare them yet" not in errors[0]
        assert "contract 0.7.7 or later" in errors[0]
        with pytest.raises(UnsupportedBindingError) as caught:
            get_iac_plugin("aws").emit(contract)
        assert caught.value.kind == "column-type"


def test_a_declared_nested_type_glue_cannot_hold_is_refused_with_the_reason():
    columns = [
        {
            "name": "m",
            "type": "map",
            "key": {"type": "int", "required": True},
            "value": {"type": "string"},
        }
    ]
    contract = _contract(columns)
    errors, _ = validate_aws_column_types(contract)
    assert len(errors) == 1
    assert "'m' is declared 'map'" in errors[0] and "supported map keys" in errors[0]
    with pytest.raises(UnsupportedBindingError):
        get_iac_plugin("aws").emit(contract)


def _run(command, tmp_path, contract, *flags):
    path = tmp_path / "contract.fluid.yaml"
    path.write_text(yaml.safe_dump(contract, sort_keys=False), encoding="utf-8")
    args = build_parser().parse_args([command, *command_args(command, path), *flags])
    return args.func(args, logging.getLogger("test"))


def command_args(command, path):
    return {"validate": [str(path), "--offline"], "generate": ["iac", str(path)]}[command]


def test_fluid_validate_passes_and_generate_iac_emits_the_glue_types(tmp_path, capsys):
    assert _run("validate", tmp_path, _contract(), "--strict") == 0
    capsys.readouterr()
    out = tmp_path / "iac"
    assert _run("generate", tmp_path, _contract(), "--out", str(out)) == 0
    emitted = "".join(p.read_text() for p in out.rglob("*.tf.json"))
    for glue_type in (
        "struct<street:string,amount:decimal(18,2)>",
        "array<string>",
        "map<string,string>",
    ):
        assert glue_type in emitted


def test_fluid_validate_still_refuses_a_bare_struct(tmp_path, capsys):
    contract = _contract([{"name": "addr", "type": "struct"}])
    assert _run("validate", tmp_path, contract) == 1
    seen = capsys.readouterr()
    assert "'addr' is declared 'struct'" in seen.out + seen.err


def test_verify_compares_a_nested_column_with_the_type_apply_wrote():
    from fluid_build.cli._verify_athena import _compare_schema

    glue_table = {
        "StorageDescriptor": {
            "Columns": [
                {"Name": "addr", "Type": "struct<street:string,amount:decimal(18,2)>"},
                {"Name": "tags", "Type": "array<string>"},
            ]
        }
    }
    report = _compare_schema(glue_table, _COLUMNS[1:3])
    assert report["type_mismatches"] == [] and report["missing"] == []
