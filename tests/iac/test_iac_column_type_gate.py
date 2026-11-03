# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""No column type reaches AWS Glue or BigQuery silently degraded or invalid.

Every type the bundled schemas admit is classified on both clouds: written as a
valid target type, written as a fallback and warned about, or refused. The
refusal is the same one at ``fluid validate``, ``generate iac`` and ``apply``,
and ``validate`` agrees with the emitter on which contracts pass.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import pytest
import yaml

import fluid_build
from fluid_build.iac import get_iac_plugin
from fluid_build.iac.base import UnsupportedBindingError
from fluid_build.iac.column_types import EXACT, LOSSY, REFUSED
from fluid_build.iac.governance_validation import validate_governance
from fluid_build.iac.providers.aws import _hive_outcome, _hive_type, validate_aws_column_types
from fluid_build.iac.providers.gcp import _bq_outcome, validate_gcp_binding

pytestmark = [pytest.mark.unit, pytest.mark.provider]

_SCHEMAS = Path(fluid_build.__file__).parent / "schemas"

# Every type the BigQuery schema JSON accepts, standard and legacy spellings.
_BQ_VALID = {
    "STRING", "BYTES", "INTEGER", "INT64", "FLOAT", "FLOAT64", "NUMERIC", "BIGNUMERIC",
    "BOOLEAN", "BOOL", "TIMESTAMP", "DATE", "TIME", "DATETIME", "GEOGRAPHY", "JSON",
    "RECORD", "STRUCT", "INTERVAL", "RANGE",
}  # fmt: skip
# A Hive/Athena column type this emitter writes.
_HIVE_VALID = re.compile(
    r"^(string|tinyint|smallint|int|bigint|float|double|boolean|date|timestamp|binary"
    r"|decimal\((?:[1-9]|[12][0-9]|3[0-8]),\d+\))$"
)


def _enum(version: str) -> set:
    schema = json.loads((_SCHEMAS / f"fluid-schema-{version}.json").read_text(encoding="utf-8"))
    return set(schema["$defs"]["column"]["properties"]["type"]["anyOf"][0]["enum"])


_ALL_TYPES = sorted(_enum("0.7.6") | _enum("0.7.7"))

_STRUCTURED = {"array", "struct", "map", "object", "record", "row"}
_GLUE_LOSSY = {"json", "jsonb", "variant", "super", "time", "interval", "bignumeric"}
_GLUE_REFUSED = _STRUCTURED | {"geography", "geometry", "geom", "point", "hll"}
_BQ_LOSSY = {"geometry", "geom"}
_BQ_REFUSED = _STRUCTURED


def test_the_enums_are_the_79_types_this_gate_was_written_against():
    # A new enum member lands in the parametrised tests below and has to be
    # classified; this only notes the size so a change is deliberate.
    assert len(_ALL_TYPES) == 79


@pytest.mark.parametrize("fluid_type", _ALL_TYPES)
def test_every_schema_type_is_valid_warned_or_refused_on_glue(fluid_type):
    outcome = _hive_outcome(fluid_type)
    if outcome.status == REFUSED:
        assert outcome.target is None
        assert outcome.why and outcome.fix
    else:
        assert _HIVE_VALID.match(outcome.target), (fluid_type, outcome)
    assert (outcome.status == LOSSY) == (fluid_type in _GLUE_LOSSY)
    assert (outcome.status == REFUSED) == (fluid_type in _GLUE_REFUSED)


@pytest.mark.parametrize("fluid_type", _ALL_TYPES)
def test_every_schema_type_is_valid_warned_or_refused_on_bigquery(fluid_type):
    outcome = _bq_outcome(fluid_type)
    if outcome.status == REFUSED:
        assert outcome.target is None
        assert outcome.why and outcome.fix
    else:
        assert outcome.target in _BQ_VALID, (fluid_type, outcome)
    assert (outcome.status == LOSSY) == (fluid_type in _BQ_LOSSY)
    assert (outcome.status == REFUSED) == (fluid_type in _BQ_REFUSED)


@pytest.mark.parametrize(
    "fluid_type, expected",
    [
        ("int2", "smallint"),
        ("int4", "int"),
        ("int8", "bigint"),
        ("int16", "smallint"),
        ("tinyint", "tinyint"),
        ("smallint", "smallint"),
        ("mediumint", "int"),
        ("longint", "bigint"),
        ("serial", "int"),
        ("bigserial", "bigint"),
        ("float4", "float"),
        ("float8", "double"),
        ("real", "float"),
        ("dec", "decimal(38,9)"),
        ("number", "decimal(38,9)"),
        ("number(12,2)", "decimal(12,2)"),
        ("dec(10)", "decimal(10)"),
        ("money", "decimal(19,4)"),
        ("bit", "boolean"),
        ("datetime2", "timestamp"),
        ("smalldatetime", "timestamp"),
        ("timestamptz", "timestamp"),
        ("timestamp_tz", "timestamp"),
        ("timestamp_ltz", "timestamp"),
        ("timestamp_ntz", "timestamp"),
        ("timestampntz", "timestamp"),
        ("TIMESTAMP WITH TIME ZONE", "timestamp"),
        ("double precision", "double"),
        ("varbinary", "binary"),
        ("blob", "binary"),
        ("bytea", "binary"),
        ("raw", "binary"),
        ("year", "smallint"),
        ("varchar(255)", "string"),
        ("float(53)", "double"),
        ("float(24)", "float"),
        ("decimal(10, 2)", "decimal(10,2)"),
    ],
)
def test_a_scalar_with_a_hive_form_gets_it(fluid_type, expected):
    outcome = _hive_outcome(fluid_type)
    assert (outcome.target, outcome.status) == (expected, EXACT)
    assert _hive_type(fluid_type) == expected


@pytest.mark.parametrize("fluid_type", ["json", "jsonb", "variant", "super", "time", "interval"])
def test_a_type_hive_cannot_hold_is_written_as_string_and_warned(fluid_type):
    outcome = _hive_outcome(fluid_type)
    assert (outcome.target, outcome.status) == ("string", LOSSY)


@pytest.mark.parametrize(
    "fluid_type", ["decimal(39,2)", "decimal(10,11)", "numeric(0)", "decimal(a,b)", "bit(8)"]
)
def test_a_type_hive_would_reject_is_refused(fluid_type):
    assert _hive_outcome(fluid_type).status == REFUSED


@pytest.mark.parametrize(
    "fluid_type", ["array", "array(string)", "ARRAY<string>", "struct", "map(string,int)", "row(a)"]
)
def test_a_structured_type_is_refused_whatever_its_parameters(fluid_type):
    assert _hive_outcome(fluid_type).status == REFUSED
    assert _bq_outcome(fluid_type).status == REFUSED


def test_a_type_nothing_maps_is_refused_on_both_clouds():
    assert _hive_outcome("geometryz").status == REFUSED
    assert _bq_outcome("geometryz").status == REFUSED


def test_the_lenient_views_stay_total_for_verify_and_diff():
    """``fluid verify`` and ``fluid diff`` compare with these; they never raise."""
    from fluid_build.iac.providers.gcp import _bq_type

    assert _hive_type("array") == "string"
    assert _bq_type("array") == "ARRAY"
    assert _bq_type("geometryz") == "GEOMETRYZ"
    assert _bq_type(None) == "STRING"


# ── The contracts: validate, generate and apply agree ────────────────────────

_AWS_LOC = {
    "database": "demo",
    "table": "orders",
    "bucket": "acme-demo",
    "path": "orders/",
    "region": "us-east-1",
}
_GCP_LOC = {"project": "acme", "dataset": "demo", "table": "orders"}

_BAD = [
    ("id", "string"),
    ("tags", "array"),
    ("payload", "json"),
    ("small", "smallint"),
    ("tz", "timestamptz"),
    ("addr", "struct"),
]
_GOOD = [c for c in _BAD if c[1] not in ("array", "struct")]


def _contract(platform, columns, binding=None):
    fmt, loc = ("parquet", _AWS_LOC) if platform == "aws" else ("bigquery_table", _GCP_LOC)
    loc = dict(loc)
    return {
        "fluidVersion": "0.7.6",
        "kind": "DataProduct",
        "id": "demo.types",
        "name": "Types demo",
        "description": "column type gate",
        "domain": "demo",
        "metadata": {"layer": "Silver", "owner": {"team": "data", "email": "data@example.com"}},
        "exposes": [
            {
                "exposeId": "orders",
                "version": "1.0.0",
                "kind": "table",
                "binding": binding or {"platform": platform, "format": fmt, "location": loc},
                "contract": {"schema": [{"name": n, "type": t} for n, t in columns]},
            }
        ],
    }


def _gate(contract):
    """What ``fluid validate`` reports for the contract's column types, both clouds."""
    gcp_errors, gcp_warnings = validate_gcp_binding(contract)
    gov_errors, gov_warnings = validate_governance(contract)
    return gcp_errors + gov_errors, gcp_warnings + gov_warnings


@pytest.mark.parametrize("platform", ["aws", "gcp"])
def test_a_structured_column_is_refused_at_validate_and_at_generate(platform):
    contract = _contract(platform, _BAD)
    errors, _ = _gate(contract)
    typed = [e for e in errors if "cannot hold its columns as declared" in e]
    assert typed, errors
    message = typed[0]
    assert "expose 'orders'" in message
    assert "'tags' is declared 'array'" in message
    assert "'addr' is declared 'struct'" in message
    assert "json or string" in message and "planned" in message
    with pytest.raises(UnsupportedBindingError) as caught:
        get_iac_plugin(platform).emit(contract)
    assert caught.value.kind == "column-type"
    assert "'tags' is declared 'array'" in str(caught.value)
    assert any("json or string" in step for step in caught.value.remediation)


def test_the_validate_refusal_is_the_emit_refusal_word_for_word():
    for platform in ("aws", "gcp"):
        contract = _contract(platform, _BAD)
        errors, _ = _gate(contract)
        with pytest.raises(UnsupportedBindingError) as caught:
            get_iac_plugin(platform).emit(contract)
        raised = f"{caught.value} {' '.join(caught.value.remediation)}"
        assert raised in errors


def test_without_the_structured_columns_glue_writes_the_faithful_types_and_warns_on_json():
    contract = _contract("aws", _GOOD)
    errors, warnings = _gate(contract)
    assert errors == []
    assert len(warnings) == 1 and "'payload'" in warnings[0] and "'string'" in warnings[0]
    resources = get_iac_plugin("aws").emit(contract)
    columns = next(iter(resources["aws_glue_catalog_table"].values()))["storage_descriptor"][
        "columns"
    ]
    assert [(c["name"], c["type"]) for c in columns] == [
        ("id", "string"),
        ("payload", "string"),
        ("small", "smallint"),
        ("tz", "timestamp"),
    ]


def test_without_the_structured_columns_bigquery_gets_valid_types_and_no_warning():
    contract = _contract("gcp", _GOOD)
    assert _gate(contract) == ([], [])
    resources = get_iac_plugin("gcp").emit(contract)
    schema = json.loads(next(iter(resources["google_bigquery_table"].values()))["schema"])
    assert [(f["name"], f["type"]) for f in schema] == [
        ("id", "STRING"),
        ("payload", "JSON"),
        ("small", "INT64"),
        ("tz", "TIMESTAMP"),
    ]


def test_a_geometry_column_on_bigquery_is_warned():
    errors, warnings = _gate(_contract("gcp", [("shape", "geometry")]))
    assert errors == []
    assert len(warnings) == 1 and "planar" in warnings[0]


def test_a_geometry_column_on_glue_is_refused_not_guessed():
    errors, _ = _gate(_contract("aws", [("shape", "geometry")]))
    assert any("'shape' is declared 'geometry'" in e and "WKT" in e for e in errors)


@pytest.mark.parametrize("platform", ["aws", "gcp"])
def test_every_type_validate_lets_through_generates(platform):
    """A contract that passes validate must not fail generate on a type, and the
    reverse: one column per schema type, each expose judged by both."""
    for fluid_type in _ALL_TYPES:
        contract = _contract(platform, [("col", fluid_type)])
        errors, _ = _gate(contract)
        try:
            get_iac_plugin(platform).emit(contract)
            refused = False
        except UnsupportedBindingError as exc:
            assert exc.kind == "column-type"
            refused = True
        assert refused == bool(errors), (platform, fluid_type, errors)


def test_a_binding_with_no_glue_table_or_a_view_has_no_column_types_to_gate():
    no_table = _contract("aws", _BAD)
    del no_table["exposes"][0]["binding"]["location"]["table"]
    assert validate_aws_column_types(no_table) == ([], [])
    view = _contract("gcp", _BAD)
    view["exposes"][0]["binding"]["format"] = "bigquery_view"
    view["exposes"][0]["binding"]["location"]["query"] = "SELECT 1"
    assert not [e for e in _gate(view)[0] if "cannot hold its columns" in e]
    get_iac_plugin("gcp").emit(view)


def test_another_clouds_binding_is_not_gated_by_this_ones_types():
    contract = _contract("aws", _BAD)
    assert not [e for e in validate_gcp_binding(contract)[0] if "BigQuery" in e]


# ── fluid validate, end to end ───────────────────────────────────────────────


def _validate(tmp_path, contract, *flags):
    from fluid_build.cli import build_parser
    from fluid_build.cli.validate import run

    path = tmp_path / "contract.fluid.yaml"
    path.write_text(yaml.safe_dump(contract, sort_keys=False), encoding="utf-8")
    args = build_parser().parse_args(["validate", str(path), "--offline", *flags])
    return run(args, logging.getLogger("test"))


@pytest.mark.parametrize("platform", ["aws", "gcp"])
def test_validate_strict_exits_1_for_a_structured_column(tmp_path, platform, capsys):
    assert _validate(tmp_path, _contract(platform, _BAD), "--strict") == 1
    out = capsys.readouterr()
    assert "cannot hold its columns as declared" in out.out + out.err


def test_validate_strict_exits_1_on_the_glue_json_warning_and_0_without_it(tmp_path, capsys):
    assert _validate(tmp_path, _contract("aws", _GOOD), "--strict") == 1
    assert _validate(tmp_path, _contract("aws", _GOOD)) == 0
    assert (
        _validate(tmp_path, _contract("aws", [c for c in _GOOD if c[1] != "json"]), "--strict") == 0
    )
    assert _validate(tmp_path, _contract("gcp", _GOOD), "--strict") == 0
    capsys.readouterr()


# ── The plan notice for a column type that changes in place ──────────────────


def _glue_change(actions, before_types, after_types):
    def table(types):
        if types is None:
            return None
        columns = [{"name": n, "type": t, "comment": ""} for n, t in types]
        return {
            "database_name": "demo",
            "name": "orders",
            "storage_descriptor": [{"columns": columns}],
        }

    return {
        "type": "aws_glue_catalog_table",
        "address": "aws_glue_catalog_table.t",
        "change": {"actions": actions, "before": table(before_types), "after": table(after_types)},
    }


def test_a_glue_update_that_changes_a_column_type_is_announced_once_per_table():
    plan = {
        "resource_changes": [
            _glue_change(
                ["update"],
                [("id", "string"), ("small", "string"), ("tz", "string")],
                [("id", "string"), ("small", "smallint"), ("tz", "timestamp")],
            )
        ]
    }
    notices = get_iac_plugin("aws").plan_update_notices(plan)
    assert len(notices) == 1
    assert notices[0].startswith("Glue table demo.orders: column types change in place")
    assert "small: string -> smallint; tz: string -> timestamp" in notices[0]
    assert "id:" not in notices[0]
    assert "data files are untouched" in notices[0]


def test_no_notice_without_a_type_change_or_for_a_create_or_another_resource():
    plugin = get_iac_plugin("aws")
    same = [("id", "string"), ("small", "smallint")]
    assert (
        plugin.plan_update_notices({"resource_changes": [_glue_change(["update"], same, same)]})
        == []
    )
    added = same + [("new", "int")]
    assert (
        plugin.plan_update_notices({"resource_changes": [_glue_change(["update"], same, added)]})
        == []
    )
    assert (
        plugin.plan_update_notices({"resource_changes": [_glue_change(["create"], None, same)]})
        == []
    )
    other = _glue_change(["update"], [("a", "string")], [("a", "int")])
    other["type"] = "aws_s3_bucket"
    assert plugin.plan_update_notices({"resource_changes": [other]}) == []
    assert plugin.plan_update_notices({}) == []


def test_the_engine_prints_the_notice_on_a_dry_run_and_never_fails_on_it(monkeypatch, capsys):
    from fluid_build.cli import _apply_opentofu_engine as engine

    plan = {
        "resource_changes": [
            _glue_change(["update"], [("small", "string")], [("small", "smallint")])
        ]
    }
    monkeypatch.setattr(engine.runner, "tofu_show_plan", lambda *a, **k: plan)
    log = logging.getLogger("test")
    plugin = get_iac_plugin("aws")
    engine._report_plan_update_notices(plugin, {"change": 1}, "wd", {}, log)
    assert "note: Glue table demo.orders" in capsys.readouterr().out
    engine._report_plan_update_notices(plugin, {"change": 0}, "wd", {}, log)
    assert capsys.readouterr().out == ""
    monkeypatch.setattr(engine.runner, "tofu_show_plan", lambda *a, **k: None)
    engine._report_plan_update_notices(plugin, {"change": 1}, "wd", {}, log)
    assert capsys.readouterr().out == ""

    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(engine.runner, "tofu_show_plan", boom)
    engine._report_plan_update_notices(plugin, {"change": 1}, "wd", {}, log)
    assert capsys.readouterr().out == ""
