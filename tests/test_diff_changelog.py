# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Tests for the contract-vs-contract version diff (``fluid diff --baseline``).

Covers the diff engine (``api/changelog.py``), rule classifications
(``api/changelog_rules.py``), and the CLI mode-switching in ``cli/diff.py``.
"""

from __future__ import annotations

import argparse
import copy
import json
import logging
import sys
import time
from pathlib import Path

import pytest

from fluid_build.api.changelog import compare_contracts, render_markdown, render_text
from fluid_build.api.changelog_rules import (
    _classify_type_diff,
    _norm_type,
    _parse_type,
    diff_agent_policy,
    diff_columns,
    diff_consumes,
    diff_duplicates,
    diff_quality_severity,
    diff_sovereignty,
)
from fluid_build.api.changelog_types import Change, ChangelogReport
from tests.test_schema_076_preview_additions import _base_contract

FIXTURES = Path(__file__).parent / "fixtures" / "changelog"

# Characters the type tests need that look like ASCII, built with chr() so the source
# stays plain ASCII and greppable.
DOTLESS_I = chr(0x131)  # str.upper() maps it to "I"
LONG_S = chr(0x17F)  # str.upper() maps it to "S"
DOTTED_CAPITAL_I = chr(0x130)
FULLWIDTH_ONE = chr(0xFF11)
NBSP = chr(0xA0)


@pytest.fixture
def v1_v2():
    """The on-disk fixture pair loaded as parsed contracts.

    They declare 0.7.3 but keep the pre-0.7 shapes the legacy rules read (a
    flat ``schema``, ``quality.tests``, a root ``agentPolicy``), which no
    bundled schema accepts, so they are diffed with ``validate=False``.
    """
    import yaml

    v1 = yaml.safe_load((FIXTURES / "v1.fluid.yaml").read_text())
    v2 = yaml.safe_load((FIXTURES / "v2.fluid.yaml").read_text())
    return v1, v2


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_same_contract_produces_empty_diff(v1_v2):
    v1, _ = v1_v2
    report = compare_contracts(v1, v1, validate=False)
    assert report.total == 0
    assert not report.has_breaking


def test_report_total_matches_section_sums(v1_v2):
    v1, v2 = v1_v2
    report = compare_contracts(v1, v2, validate=False)
    assert report.total == (len(report.breaking) + len(report.non_breaking) + len(report.info))


# ---------------------------------------------------------------------------
# Column-level rules
# ---------------------------------------------------------------------------


def test_column_removal_is_breaking():
    expose_old = {"id": "x", "schema": [{"name": "email", "type": "STRING"}]}
    expose_new = {"id": "x", "schema": []}
    changes = diff_columns(expose_old, expose_new, "x", 0)
    assert len(changes) == 1
    assert changes[0].severity == "breaking"
    assert changes[0].kind == "column_removed"
    assert "email" in changes[0].description


def test_column_added_nullable_is_non_breaking():
    expose_old = {"id": "x", "schema": []}
    expose_new = {
        "id": "x",
        "schema": [{"name": "created_at", "type": "TIMESTAMP", "nullable": True}],
    }
    changes = diff_columns(expose_old, expose_new, "x", 0)
    assert len(changes) == 1
    assert changes[0].severity == "non_breaking"
    assert changes[0].kind == "column_added"


def test_column_added_not_null_is_breaking():
    expose_old = {"id": "x", "schema": []}
    expose_new = {
        "id": "x",
        "schema": [{"name": "external_id", "type": "STRING", "nullable": False}],
    }
    changes = diff_columns(expose_old, expose_new, "x", 0)
    assert len(changes) == 1
    assert changes[0].severity == "breaking"
    assert changes[0].kind == "column_added"


def test_type_widening_is_non_breaking():
    expose_old = {"id": "x", "schema": [{"name": "amount", "type": "INT"}]}
    expose_new = {"id": "x", "schema": [{"name": "amount", "type": "BIGINT"}]}
    changes = diff_columns(expose_old, expose_new, "x", 0)
    assert len(changes) == 1
    assert changes[0].severity == "non_breaking"
    assert changes[0].kind == "column_type_widened"


def test_type_narrowing_is_breaking():
    expose_old = {"id": "x", "schema": [{"name": "amount", "type": "BIGINT"}]}
    expose_new = {"id": "x", "schema": [{"name": "amount", "type": "INT"}]}
    changes = diff_columns(expose_old, expose_new, "x", 0)
    assert len(changes) == 1
    assert changes[0].severity == "breaking"
    assert changes[0].kind == "column_type_changed"


def test_unrelated_type_change_is_breaking():
    expose_old = {"id": "x", "schema": [{"name": "amount", "type": "STRING"}]}
    expose_new = {"id": "x", "schema": [{"name": "amount", "type": "INT"}]}
    changes = diff_columns(expose_old, expose_new, "x", 0)
    # STRING -> INT is not a widening; must be flagged as breaking.
    assert any(c.severity == "breaking" for c in changes)


def test_pk_nullability_loosened_is_breaking():
    expose_old = {
        "id": "x",
        "primaryKey": ["id"],
        "schema": [{"name": "id", "type": "BIGINT", "nullable": False}],
    }
    expose_new = {
        "id": "x",
        "primaryKey": ["id"],
        "schema": [{"name": "id", "type": "BIGINT", "nullable": True}],
    }
    changes = diff_columns(expose_old, expose_new, "x", 0)
    assert any(c.severity == "breaking" and c.kind == "primary_key_nullable" for c in changes)


def test_not_null_added_to_nullable_column_is_breaking():
    expose_old = {"id": "x", "schema": [{"name": "n", "type": "INT", "nullable": True}]}
    expose_new = {
        "id": "x",
        "schema": [{"name": "n", "type": "INT", "nullable": False}],
    }
    changes = diff_columns(expose_old, expose_new, "x", 0)
    assert any(c.kind == "column_nullable_tightened" and c.severity == "breaking" for c in changes)


def test_column_description_change_is_info():
    expose_old = {
        "id": "x",
        "schema": [{"name": "n", "type": "INT", "description": "Old desc"}],
    }
    expose_new = {
        "id": "x",
        "schema": [{"name": "n", "type": "INT", "description": "New desc"}],
    }
    changes = diff_columns(expose_old, expose_new, "x", 0)
    assert any(c.severity == "info" and c.kind == "column_description_changed" for c in changes)


# ---------------------------------------------------------------------------
# Top-level rules
# ---------------------------------------------------------------------------


def test_consume_removal_is_breaking():
    old = {"consumes": [{"ref": "upstream.a"}]}
    new = {"consumes": []}
    changes = diff_consumes(old, new)
    assert any(c.severity == "breaking" and c.kind == "consume_removed" for c in changes)


def test_consume_added_is_non_breaking():
    old = {"consumes": []}
    new = {"consumes": [{"ref": "upstream.b"}]}
    changes = diff_consumes(old, new)
    assert any(c.severity == "non_breaking" and c.kind == "consume_added" for c in changes)


def test_agent_policy_allowed_models_narrowed_is_breaking():
    old = {"agentPolicy": {"allowedModels": ["gpt-4", "claude-3-opus"]}}
    new = {"agentPolicy": {"allowedModels": ["gpt-4"]}}
    changes = diff_agent_policy(old, new)
    assert any(c.severity == "breaking" and c.kind == "agent_policy_narrowed" for c in changes)


def test_agent_policy_denied_expanded_is_breaking():
    old = {"agentPolicy": {"deniedModels": []}}
    new = {"agentPolicy": {"deniedModels": ["gpt-3.5"]}}
    changes = diff_agent_policy(old, new)
    assert any(
        c.severity == "breaking" and c.kind == "agent_policy_denied_expanded" for c in changes
    )


def test_sovereignty_regions_narrowed_is_breaking():
    old = {"sovereignty": {"allowedRegions": ["us-east-1", "eu-west-1"]}}
    new = {"sovereignty": {"allowedRegions": ["us-east-1"]}}
    changes = diff_sovereignty(old, new)
    assert any(
        c.severity == "breaking" and c.kind == "sovereignty_regions_narrowed" for c in changes
    )


def test_quality_severity_escalation_is_breaking():
    old_expose = {
        "id": "x",
        "quality": {"tests": [{"name": "t1", "severity": "warn"}]},
    }
    new_expose = {
        "id": "x",
        "quality": {"tests": [{"name": "t1", "severity": "error"}]},
    }
    changes = diff_quality_severity(old_expose, new_expose, "x", 0)
    assert len(changes) == 1
    assert changes[0].severity == "breaking"
    assert changes[0].kind == "quality_severity_escalated"


def test_quality_severity_unchanged_no_change():
    old_expose = {
        "id": "x",
        "quality": {"tests": [{"name": "t1", "severity": "error"}]},
    }
    new_expose = {
        "id": "x",
        "quality": {"tests": [{"name": "t1", "severity": "error"}]},
    }
    changes = diff_quality_severity(old_expose, new_expose, "x", 0)
    assert changes == []


# ---------------------------------------------------------------------------
# Fixture-driven golden test
# ---------------------------------------------------------------------------


def test_fixture_v1_to_v2_classification(v1_v2):
    """Full pipeline against the on-disk fixture pair."""
    v1, v2 = v1_v2
    report = compare_contracts(v1, v2, validate=False)

    # v2 vs v1 should detect:
    #  - email column removed (breaking)
    #  - amount INT -> BIGINT widening (non-breaking)
    #  - status STRING -> VARCHAR (note: VARCHAR widens to STRING, so STRING -> VARCHAR is breaking)
    #  - created_at column added, nullable (non-breaking)
    #  - upstream.users removed from consumes (breaking)
    #  - allowedModels narrowed: gemini-1.5-pro dropped (breaking)
    #  - allowedRegions narrowed: us-west-2 dropped (breaking)
    #  - quality test orders_order_id_not_null escalated warn -> error (breaking)
    #  - metadata.tags added 'pii' (info)
    #  - metadata.description changed (info)
    #  - order_id description updated (info)

    breaking_kinds = {c.kind for c in report.breaking}
    non_breaking_kinds = {c.kind for c in report.non_breaking}
    info_kinds = {c.kind for c in report.info}

    # Breaking expectations:
    assert "column_removed" in breaking_kinds  # email
    assert "consume_removed" in breaking_kinds  # upstream.users
    assert "agent_policy_narrowed" in breaking_kinds  # gemini removed
    assert "sovereignty_regions_narrowed" in breaking_kinds  # us-west-2 removed
    assert "quality_severity_escalated" in breaking_kinds

    # Non-breaking expectations:
    assert "column_added" in non_breaking_kinds  # created_at
    assert "column_type_widened" in non_breaking_kinds  # amount INT -> BIGINT

    # Info expectations:
    assert "metadata_description_changed" in info_kinds
    assert "metadata_tags_changed" in info_kinds


# ---------------------------------------------------------------------------
# Render output formats
# ---------------------------------------------------------------------------


def test_render_text_contains_summary_line(v1_v2):
    v1, v2 = v1_v2
    report = compare_contracts(v1, v2, validate=False)
    out = render_text(report)
    assert "Summary:" in out
    assert "breaking" in out.lower()


def test_render_text_empty_report_says_no_changes():
    out = render_text(ChangelogReport())
    assert "No changes detected." in out


def test_render_markdown_includes_section_headings(v1_v2):
    v1, v2 = v1_v2
    report = compare_contracts(v1, v2, validate=False)
    md = render_markdown(report)
    assert "# Contract changelog" in md
    if report.breaking:
        assert "Breaking" in md
    if report.non_breaking:
        assert "Non-breaking" in md


def test_report_to_dict_round_trips_through_json(v1_v2):
    v1, v2 = v1_v2
    report = compare_contracts(v1, v2, validate=False)
    data = report.to_dict()
    # Must be JSON-serialisable
    s = json.dumps(data)
    parsed = json.loads(s)
    assert parsed["summary"]["total"] == report.total
    assert parsed["summary"]["breaking"] == len(report.breaking)


# ---------------------------------------------------------------------------
# CLI mode-switch in cli/diff.py
# ---------------------------------------------------------------------------


def _cli_contracts(tmp_path):
    """A valid 0.7.6 pair for the CLI, which validates both: ``new`` drops a column."""
    import yaml

    old = tmp_path / "old.fluid.yaml"
    new = tmp_path / "new.fluid.yaml"
    old.write_text(yaml.safe_dump(_contract_076([_ID_COLUMN, {"name": "note", "type": "string"}])))
    new.write_text(yaml.safe_dump(_contract_076([_ID_COLUMN])))
    return str(old), str(new)


def test_cli_diff_baseline_mode_smoke(tmp_path, monkeypatch):
    """End-to-end: ``fluid diff <new> --baseline <old>`` produces JSON and exits 0 by default."""
    from fluid_build.cli import diff as diff_mod

    old, new = _cli_contracts(tmp_path)
    out_path = tmp_path / "diff.json"
    args = argparse.Namespace(
        contract=new,
        baseline=old,
        env=None,
        state=None,
        out=str(out_path),
        exit_on_drift=False,
        fail_on_breaking=False,
        format="json",
        cmd="diff",
    )
    logger = logging.getLogger("test.diff")
    rc = diff_mod.run(args, logger)
    assert rc == 0
    assert out_path.exists()
    data = json.loads(out_path.read_text())
    assert "breaking" in data
    assert "non_breaking" in data
    assert "info" in data
    assert data["summary"]["total"] > 0


def test_cli_diff_baseline_fail_on_breaking(tmp_path):
    """``--fail-on-breaking`` returns 1 when breaking changes exist."""
    from fluid_build.cli import diff as diff_mod

    old, new = _cli_contracts(tmp_path)
    args = argparse.Namespace(
        contract=new,
        baseline=old,
        env=None,
        state=None,
        out=str(tmp_path / "diff.json"),
        exit_on_drift=False,
        fail_on_breaking=True,
        format="json",
        cmd="diff",
    )
    rc = diff_mod.run(args, logging.getLogger("test.diff"))
    assert rc == 1


def test_cli_diff_baseline_idempotent(tmp_path):
    """``--fail-on-breaking`` returns 0 when comparing a contract to itself."""
    from fluid_build.cli import diff as diff_mod

    old, _ = _cli_contracts(tmp_path)
    args = argparse.Namespace(
        contract=old,
        baseline=old,
        env=None,
        state=None,
        out=str(tmp_path / "diff.json"),
        exit_on_drift=False,
        fail_on_breaking=True,
        format="json",
        cmd="diff",
    )
    rc = diff_mod.run(args, logging.getLogger("test.diff"))
    assert rc == 0


def test_cli_diff_baseline_rejects_env(tmp_path):
    """``--baseline`` and ``--env`` are mutually exclusive."""
    from fluid_build.cli import diff as diff_mod
    from fluid_build.cli._common import CLIError

    args = argparse.Namespace(
        contract=str(FIXTURES / "v2.fluid.yaml"),
        baseline=str(FIXTURES / "v1.fluid.yaml"),
        env="prod",
        state=None,
        out=str(tmp_path / "diff.json"),
        exit_on_drift=False,
        fail_on_breaking=False,
        format="text",
        cmd="diff",
    )
    with pytest.raises(CLIError) as exc_info:
        diff_mod.run(args, logging.getLogger("test.diff"))
    assert exc_info.value.event == "diff_modes_mutually_exclusive"


def test_cli_diff_baseline_rejects_state(tmp_path):
    """``--baseline`` and ``--state`` are mutually exclusive."""
    from fluid_build.cli import diff as diff_mod
    from fluid_build.cli._common import CLIError

    state_path = tmp_path / "state.json"
    state_path.write_text("{}")
    args = argparse.Namespace(
        contract=str(FIXTURES / "v2.fluid.yaml"),
        baseline=str(FIXTURES / "v1.fluid.yaml"),
        env=None,
        state=str(state_path),
        out=str(tmp_path / "diff.json"),
        exit_on_drift=False,
        fail_on_breaking=False,
        format="text",
        cmd="diff",
    )
    with pytest.raises(CLIError) as exc_info:
        diff_mod.run(args, logging.getLogger("test.diff"))
    assert exc_info.value.event == "diff_modes_mutually_exclusive"


# ---------------------------------------------------------------------------
# Precision / scale / length-aware type diff
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected_base,expected_params",
    [
        ("DECIMAL(10,2)", "DECIMAL", (10, 2)),
        ("VARCHAR(255)", "VARCHAR", (255,)),
        ("BIGINT", "BIGINT", None),
        ("DECIMAL(38, 18)", "DECIMAL", (38, 18)),
        ("", "", None),
        # Off-grammar text is its own type: it equals itself and nothing else, so a
        # change in it can never be read as "no parameters" or as whitespace.
        ("ENUM('A','B')", "ENUM('A','B')", None),
        ("VARCHAR(MAX)", "VARCHAR(MAX)", None),
        ("DECIMAL(1_0,2)", "DECIMAL(1_0,2)", None),
        ("DECIMAL(+10,2)", "DECIMAL(+10,2)", None),
        (f"DECIMAL({FULLWIDTH_ONE}0,2)", f"DECIMAL({FULLWIDTH_ONE}0,2)", None),
        (f"DECIMAL({NBSP}10,2)", f"DECIMAL({NBSP}10,2)", None),
    ],
)
def test_parse_type_extracts_base_and_params(raw, expected_base, expected_params):
    base, params = _parse_type(raw)
    assert base == expected_base
    assert params == expected_params


def test_decimal_precision_widening_is_non_breaking():
    sev, kind = _classify_type_diff("DECIMAL(10,2)", "DECIMAL(20,2)")
    assert sev == "non_breaking"
    assert kind == "column_type_widened"


def test_decimal_precision_narrowing_is_breaking():
    sev, kind = _classify_type_diff("DECIMAL(20,2)", "DECIMAL(10,2)")
    assert sev == "breaking"
    assert kind == "column_type_changed"


def test_decimal_mixed_param_change_is_breaking():
    """Precision grew but scale shrank — neither pure widening nor narrowing.

    It used to be ``info``, which ``--fail-on-breaking`` lets through; a column that
    holds fewer fractional digits than before loses data for its consumers.
    """
    sev, kind = _classify_type_diff("DECIMAL(20,4)", "DECIMAL(30,2)")
    assert sev == "breaking"
    assert kind == "column_type_param_changed"


def test_varchar_length_narrowing_is_breaking():
    sev, kind = _classify_type_diff("VARCHAR(255)", "VARCHAR(100)")
    assert sev == "breaking"
    assert kind == "column_type_changed"


def test_varchar_length_widening_is_non_breaking():
    sev, kind = _classify_type_diff("VARCHAR(50)", "VARCHAR(255)")
    assert sev == "non_breaking"
    assert kind == "column_type_widened"


def test_column_diff_uses_precision_aware_classifier():
    """End-to-end: diff_columns picks up the precision-aware kinds."""
    old = {
        "id": "x",
        "schema": [{"name": "amount", "type": "DECIMAL(10,2)"}],
    }
    new = {
        "id": "x",
        "schema": [{"name": "amount", "type": "DECIMAL(20,2)"}],
    }
    changes = diff_columns(old, new, "x", 0)
    type_changes = [c for c in changes if c.kind.startswith("column_type")]
    assert len(type_changes) == 1
    assert type_changes[0].severity == "non_breaking"
    assert type_changes[0].kind == "column_type_widened"


# ---------------------------------------------------------------------------
# Nested / struct field traversal
# ---------------------------------------------------------------------------


def test_nested_field_removal_is_breaking():
    old = {
        "id": "x",
        "schema": [
            {
                "name": "address",
                "type": "STRUCT",
                "fields": [
                    {"name": "street", "type": "STRING"},
                    {"name": "zip", "type": "STRING"},
                ],
            }
        ],
    }
    new = {
        "id": "x",
        "schema": [
            {
                "name": "address",
                "type": "STRUCT",
                "fields": [
                    {"name": "street", "type": "STRING"},
                    # zip removed
                ],
            }
        ],
    }
    changes = diff_columns(old, new, "x", 0)
    nested = [c for c in changes if c.kind == "nested_field_removed"]
    assert len(nested) == 1
    assert nested[0].severity == "breaking"
    assert "address.fields.zip" in nested[0].path


def test_nested_field_addition_nullable_is_non_breaking():
    old = {
        "id": "x",
        "schema": [
            {
                "name": "address",
                "type": "STRUCT",
                "fields": [{"name": "street", "type": "STRING"}],
            }
        ],
    }
    new = {
        "id": "x",
        "schema": [
            {
                "name": "address",
                "type": "STRUCT",
                "fields": [
                    {"name": "street", "type": "STRING"},
                    {"name": "country", "type": "STRING", "nullable": True},
                ],
            }
        ],
    }
    changes = diff_columns(old, new, "x", 0)
    added = [c for c in changes if c.kind == "nested_field_added"]
    assert len(added) == 1
    assert added[0].severity == "non_breaking"


def test_nested_struct_of_struct_recurses():
    """Deep nesting: struct.fields.struct.fields.x reaches recursion."""
    old = {
        "id": "x",
        "schema": [
            {
                "name": "outer",
                "type": "STRUCT",
                "fields": [
                    {
                        "name": "inner",
                        "type": "STRUCT",
                        "fields": [{"name": "deep_field", "type": "STRING"}],
                    }
                ],
            }
        ],
    }
    new = {
        "id": "x",
        "schema": [
            {
                "name": "outer",
                "type": "STRUCT",
                "fields": [
                    {
                        "name": "inner",
                        "type": "STRUCT",
                        "fields": [],  # deep_field removed
                    }
                ],
            }
        ],
    }
    changes = diff_columns(old, new, "x", 0)
    assert any(c.kind == "nested_field_removed" and "deep_field" in c.path for c in changes)


# ---------------------------------------------------------------------------
# PII annotation drift
# ---------------------------------------------------------------------------


def test_pii_added_is_info_signal():
    old = {"id": "x", "schema": [{"name": "email", "type": "STRING"}]}
    new = {"id": "x", "schema": [{"name": "email", "type": "STRING", "pii": True}]}
    changes = diff_columns(old, new, "x", 0)
    pii = [c for c in changes if c.kind == "column_pii_added"]
    assert len(pii) == 1
    assert pii[0].severity == "info"
    assert "PII" in pii[0].description


def test_pii_removed_is_info_signal():
    old = {"id": "x", "schema": [{"name": "email", "type": "STRING", "pii": True}]}
    new = {"id": "x", "schema": [{"name": "email", "type": "STRING"}]}
    changes = diff_columns(old, new, "x", 0)
    pii = [c for c in changes if c.kind == "column_pii_removed"]
    assert len(pii) == 1
    assert pii[0].severity == "info"


# ---------------------------------------------------------------------------
# 0.7.x contracts keep their columns under exposes[].contract.schema
# ---------------------------------------------------------------------------


def _contract_076(columns):
    """The valid 0.7.6 contract the schema tests share, with ``columns`` on its one expose.

    0.7.6 closes ``$defs.column``: it has no ``nullable``, ``primaryKey`` or
    ``pii`` key, and NOT NULL is spelled ``required: true``.
    """
    contract = _base_contract("0.7.6")
    contract["exposes"][0]["contract"]["schema"] = columns
    return contract


_ID_COLUMN = {"name": "id", "type": "string", "required": True}


def test_076_fixture_validates_against_the_bundled_schema():
    """The shape every test below diffs: if 0.7.6 stops accepting it they prove nothing."""
    from fluid_build.schema_manager import FluidSchemaManager

    columns = [_ID_COLUMN, {"name": "note", "type": "string", "required": False}]
    result = FluidSchemaManager().validate_contract(
        _contract_076(columns), schema_version="0.7.6", offline_only=True
    )
    assert result.is_valid, result.errors


def test_076_identical_contracts_have_no_changes():
    assert compare_contracts(_contract_076([_ID_COLUMN]), _contract_076([_ID_COLUMN])).total == 0


@pytest.mark.parametrize(
    "column, severity",
    [
        pytest.param({"name": "note", "type": "string"}, "non_breaking", id="plain"),
        pytest.param(
            {"name": "note", "type": "string", "required": False},
            "non_breaking",
            id="required-false",
        ),
        pytest.param(
            {"name": "note", "type": "string", "required": True}, "breaking", id="required-true"
        ),
    ],
)
def test_076_added_column_severity_follows_required(column, severity):
    report = compare_contracts(_contract_076([_ID_COLUMN]), _contract_076([_ID_COLUMN, column]))

    assert report.total == 1
    (change,) = getattr(report, severity)
    assert change.kind == "column_added"
    assert change.path == "exposes[0].contract.schema.note"
    assert ("NOT NULL" in change.description) == (severity == "breaking")


def test_076_column_removed_is_breaking():
    old = _contract_076([_ID_COLUMN, {"name": "note", "type": "string"}])

    report = compare_contracts(old, _contract_076([_ID_COLUMN]))

    assert report.total == 1
    (change,) = report.breaking
    assert change.kind == "column_removed"
    assert change.path == "exposes[0].contract.schema.note"


@pytest.mark.parametrize(
    "old_type, new_type, severity, kind",
    [
        ("string", "int", "breaking", "column_type_changed"),
        ("bigint", "int", "breaking", "column_type_changed"),
        ("int", "bigint", "non_breaking", "column_type_widened"),
        ("decimal(10,2)", "decimal(20,2)", "non_breaking", "column_type_widened"),
    ],
)
def test_076_column_type_change_is_classified(old_type, new_type, severity, kind):
    old = _contract_076([{"name": "id", "type": old_type}])
    new = _contract_076([{"name": "id", "type": new_type}])

    report = compare_contracts(old, new)

    assert report.total == 1
    (change,) = getattr(report, severity)
    assert change.kind == kind
    assert change.path == "exposes[0].contract.schema.id.type"
    assert (change.before, change.after) == (old_type.upper(), new_type.upper())


@pytest.mark.parametrize(
    "old_required, new_required, severity, kind",
    [
        (False, True, "breaking", "column_nullable_tightened"),
        (True, False, "non_breaking", "column_nullable_loosened"),
    ],
)
def test_076_required_flip_is_a_nullability_change(old_required, new_required, severity, kind):
    old = _contract_076([{"name": "id", "type": "string", "required": old_required}])
    new = _contract_076([{"name": "id", "type": "string", "required": new_required}])

    report = compare_contracts(old, new)

    assert report.total == 1
    (change,) = getattr(report, severity)
    assert change.kind == kind
    assert change.path == "exposes[0].contract.schema.id.required"
    assert (change.before, change.after) == (not old_required, not new_required)


@pytest.mark.parametrize(
    "column, severity",
    [
        pytest.param({}, "non_breaking", id="neither"),
        pytest.param({"nullable": True}, "non_breaking", id="nullable-true"),
        pytest.param({"nullable": False}, "breaking", id="nullable-false"),
        pytest.param({"required": False}, "non_breaking", id="required-false"),
        pytest.param({"required": True}, "breaking", id="required-true"),
        pytest.param({"required": 1}, "breaking", id="required-truthy"),
        pytest.param(
            {"required": True, "nullable": True}, "breaking", id="required-beats-nullable"
        ),
        pytest.param(
            {"required": False, "nullable": False}, "breaking", id="nullable-false-stands"
        ),
    ],
)
def test_a_column_is_not_null_when_required_or_nullable_false_says_so(column, severity):
    """The emitters read ``required`` alone, so a column that declares both and contradicts
    itself is read as the stricter of the two, never as nullable."""
    old = {"id": "x", "schema": []}
    new = {"id": "x", "schema": [{"name": "n", "type": "INT", **column}]}

    changes = diff_columns(old, new, "x", 0)

    assert [(c.kind, c.severity) for c in changes] == [("column_added", severity)]


@pytest.mark.parametrize(
    "old_column, new_column, primary_key, kind, key",
    [
        ({"nullable": True}, {"nullable": False}, False, "column_nullable_tightened", "nullable"),
        ({"nullable": False}, {"nullable": True}, False, "column_nullable_loosened", "nullable"),
        ({}, {"required": True}, False, "column_nullable_tightened", "required"),
        ({"required": True}, {}, False, "column_nullable_loosened", "required"),
        # Spelled differently on the two sides: the new column decides.
        ({"nullable": False}, {"required": False}, False, "column_nullable_loosened", "required"),
        ({"required": True}, {"nullable": True}, False, "column_nullable_loosened", "nullable"),
        ({"nullable": False}, {"nullable": True}, True, "primary_key_nullable", "nullable"),
        ({"required": True}, {"required": False}, True, "primary_key_nullable", "required"),
    ],
)
def test_a_nullability_change_is_reported_at_the_key_the_contract_uses(
    old_column, new_column, primary_key, kind, key
):
    def expose(column):
        columns = [{"name": "n", "type": "INT", **column}]
        return {"id": "x", "schema": columns, **({"primaryKey": ["n"]} if primary_key else {})}

    changes = diff_columns(expose(old_column), expose(new_column), "x", 0)

    assert [(c.kind, c.path) for c in changes] == [(kind, f"exposes[0].schema.n.{key}")]


def test_nested_field_added_required_is_breaking():
    old = {
        "id": "x",
        "schema": [
            {"name": "address", "type": "STRUCT", "fields": [{"name": "street", "type": "STRING"}]}
        ],
    }
    new = {
        "id": "x",
        "schema": [
            {
                "name": "address",
                "type": "STRUCT",
                "fields": [
                    {"name": "street", "type": "STRING"},
                    {"name": "country", "type": "STRING", "required": True},
                ],
            }
        ],
    }

    changes = diff_columns(old, new, "x", 0)

    assert [(c.kind, c.severity) for c in changes] == [("nested_field_added", "breaking")]


def test_flat_schema_wins_when_an_expose_carries_both_layouts():
    a, b, c = (
        {"name": "a", "type": "STRING"},
        {"name": "b", "type": "STRING"},
        {"name": "c", "type": "STRING"},
    )
    old = {"id": "x", "schema": [a], "contract": {"schema": [a, b]}}
    new = {"id": "x", "schema": [a, c], "contract": {"schema": [a, b]}}

    changes = diff_columns(old, new, "x", 0)

    assert [(ch.kind, ch.path) for ch in changes] == [("column_added", "exposes[0].schema.c")]


def _contract_flat(columns):
    """``_contract_076``'s pre-0.7 twin: columns at ``exposes[].schema``, NOT NULL as ``nullable``."""
    contract = _contract_076([])
    contract["exposes"] = [{"id": "orders", "schema": columns}]
    return contract


def test_moving_columns_from_schema_to_contract_schema_is_not_a_change():
    """A pre-0.7 baseline against its 0.7.x rewrite: ``nullable: false`` is ``required: true``."""
    old = _contract_flat([{"name": "id", "type": "STRING", "nullable": False}])
    new = _contract_076([{"name": "id", "type": "string", "required": True}])

    assert compare_contracts(old, new, validate=False).total == 0


def test_a_column_dropped_while_moving_to_contract_schema_is_still_found():
    old = _contract_flat([{"name": "id", "type": "STRING"}, {"name": "note", "type": "STRING"}])
    new = _contract_076([{"name": "id", "type": "string"}])

    report = compare_contracts(old, new, validate=False)

    assert report.total == 1
    assert [(c.kind, c.path) for c in report.breaking] == [
        ("column_removed", "exposes[0].contract.schema.note")
    ]


def _cli_diff_baseline(tmp_path, old, new, fail_on_breaking):
    """Run ``fluid diff <new> --baseline <old>`` on two contract dicts; return (rc, JSON report)."""
    import yaml

    from fluid_build.cli import diff as diff_mod

    (tmp_path / "old.fluid.yaml").write_text(yaml.safe_dump(old))
    (tmp_path / "new.fluid.yaml").write_text(yaml.safe_dump(new))
    out = tmp_path / "diff.json"
    args = argparse.Namespace(
        contract=str(tmp_path / "new.fluid.yaml"),
        baseline=str(tmp_path / "old.fluid.yaml"),
        env=None,
        state=None,
        out=str(out),
        exit_on_drift=False,
        fail_on_breaking=fail_on_breaking,
        format="json",
        cmd="diff",
    )
    rc = diff_mod.run(args, logging.getLogger("test.diff"))
    return rc, json.loads(out.read_text())


@pytest.mark.parametrize("fail_on_breaking, expected_rc", [(True, 1), (False, 0)])
def test_cli_diff_baseline_sees_a_required_column_added_to_a_076_contract(
    tmp_path, fail_on_breaking, expected_rc
):
    """``--fail-on-breaking`` is the exit code CI gates on; it must see 0.7.x columns."""
    added = {"name": "region", "type": "string", "required": True}

    rc, report = _cli_diff_baseline(
        tmp_path, _contract_076([_ID_COLUMN]), _contract_076([_ID_COLUMN, added]), fail_on_breaking
    )

    assert rc == expected_rc
    assert [c["kind"] for c in report["breaking"]] == ["column_added"]


# ---------------------------------------------------------------------------
# Type comparison: type changes that used to pass ``--fail-on-breaking``
# ---------------------------------------------------------------------------


def _assert_schema_valid(*contracts):
    """Every contract below is one the bundled 0.7.6 schema accepts, so the engine must handle it."""
    from fluid_build.schema_manager import FluidSchemaManager

    for contract in contracts:
        result = FluidSchemaManager().validate_contract(
            contract, schema_version="0.7.6", offline_only=True
        )
        assert result.is_valid, result.errors


def _type_change_report(old_type, new_type):
    old = _contract_076([{"name": "c", "type": old_type}])
    new = _contract_076([{"name": "c", "type": new_type}])
    _assert_schema_valid(old, new)
    return compare_contracts(old, new)


@pytest.mark.parametrize(
    "old_type, new_type, severity, kind",
    [
        # A name that only looks like a real type once str.upper() has folded it.
        pytest.param("int", f"{DOTLESS_I}nt", "breaking", "column_type_changed", id="dotless-i"),
        pytest.param(
            "timestamp", f"time{LONG_S}tamp", "breaking", "column_type_changed", id="long-s"
        ),
        # Parameters used to be ignored when the base type changed.
        ("int", "decimal(5,0)", "breaking", "column_type_changed"),
        # Parameters present on one side only, or moving in different directions.
        ("varchar", "varchar(10)", "breaking", "column_type_param_changed"),
        ("varchar(255)", "varchar", "breaking", "column_type_param_changed"),
        ("decimal(10,2)", "decimal(12,1)", "breaking", "column_type_param_changed"),
        ("decimal(10,2)", "decimal", "breaking", "column_type_param_changed"),
        # Precision and scale both grow, the integer digits shrink (8 -> 7).
        ("decimal(10,2)", "decimal(11,4)", "breaking", "column_type_param_changed"),
        # Parameters that name something (a spatial reference id, an exact width) don't widen.
        ("geometry(4326)", "geometry(32633)", "breaking", "column_type_param_changed"),
        ("binary(16)", "binary(32)", "breaking", "column_type_param_changed"),
        # Entries that left the widening table.
        ("time", "timestamp", "breaking", "column_type_changed"),
        ("time", "datetime", "breaking", "column_type_changed"),
        ("int", "float", "breaking", "column_type_changed"),
        ("integer", "float", "breaking", "column_type_changed"),
        ("int32", "float", "breaking", "column_type_changed"),
        ("float", "decimal", "breaking", "column_type_changed"),
        ("float", "numeric", "breaking", "column_type_changed"),
        # Parameters the classifier can't order used to read as "no parameters".
        ("enum('a','b')", "enum('a')", "breaking", "column_type_changed"),
        ("varchar", "varchar(max)", "breaking", "column_type_changed"),
        ("decimal(10,2)", "decimal(1_0,2)", "breaking", "column_type_changed"),
        ("decimal(10,2)", f"decimal({NBSP}10,2)", "breaking", "column_type_changed"),
        # Controls, which pass on the code before this change too: what still widens, and
        # what is only whitespace.
        ("int", "bigint", "non_breaking", "column_type_widened"),
        ("int", "double", "non_breaking", "column_type_widened"),
        ("int", "decimal", "non_breaking", "column_type_widened"),
        ("varchar", "text", "non_breaking", "column_type_widened"),
        ("varchar(50)", "varchar(255)", "non_breaking", "column_type_widened"),
        ("date", "timestamp", "non_breaking", "column_type_widened"),
        ("float", "double", "non_breaking", "column_type_widened"),
        ("decimal(10,2)", "decimal(12,4)", "non_breaking", "column_type_widened"),
        ("char(10)", "char(20)", "non_breaking", "column_type_widened"),
        ("float(24)", "float(53)", "non_breaking", "column_type_widened"),
        ("decimal(10,2)", "decimal(10, 2)", "info", "column_type_normalized"),
        ("varchar(255)", "varchar(0255)", "info", "column_type_normalized"),
    ],
)
def test_076_type_comparison(old_type, new_type, severity, kind):
    report = _type_change_report(old_type, new_type)

    assert report.total == 1
    (change,) = getattr(report, severity)
    assert change.kind == kind
    assert change.path == "exposes[0].contract.schema.c.type"


@pytest.mark.parametrize(
    "raw, expected",
    [
        (" int ", "INT"),
        ("DeCiMaL(10,2)", "DECIMAL(10,2)"),
        (f"{DOTLESS_I}nt", f"{DOTLESS_I}nt"),
        (f"time{LONG_S}tamp", f"time{LONG_S}tamp"),
        (f"{DOTTED_CAPITAL_I}nt", f"{DOTTED_CAPITAL_I}nt"),
        (None, ""),
        (5, ""),
    ],
)
def test_norm_type_folds_case_for_ascii_names_only(raw, expected):
    assert _norm_type(raw) == expected


@pytest.mark.parametrize(
    "old, new, expected",
    [
        # Same integer digits, bigger scale: still holds every old value.
        ("DECIMAL(10,2)", "DECIMAL(12,4)", ("non_breaking", "column_type_widened")),
        ("DECIMAL(10,2)", "DECIMAL(11,4)", ("breaking", "column_type_param_changed")),
        ("NUMBER(10,2)", "NUMBER(11,4)", ("breaking", "column_type_param_changed")),
        ("NUMERIC(10,2)", "NUMERIC(12,1)", ("breaking", "column_type_param_changed")),
        ("DECIMAL(10)", "DECIMAL(10,2)", ("breaking", "column_type_param_changed")),
        ("DECIMAL(10)", "DECIMAL(12)", ("non_breaking", "column_type_widened")),
        # Types that are not fixed-point compare their parameters one by one.
        ("TIMESTAMP(3)", "TIMESTAMP(6)", ("non_breaking", "column_type_widened")),
    ],
)
def test_fixed_point_widens_only_when_scale_and_integer_digits_both_hold(old, new, expected):
    assert _classify_type_diff(old, new) == expected


@pytest.mark.parametrize(
    "old, new",
    [
        ("GEOMETRY(4326)", "GEOMETRY(32633)"),
        ("GEOMETRY(32633)", "GEOMETRY(4326)"),
        ("VECTOR(3)", "VECTOR(1536)"),
        ("BINARY(16)", "BINARY(32)"),
        ("BIT(8)", "BIT(16)"),
    ],
)
def test_parameters_that_name_something_are_never_a_widening(old, new):
    """A spatial reference id, a dimension or an exact width does not measure capacity."""
    assert _classify_type_diff(old, new)[0] == "breaking"


@pytest.mark.parametrize(
    "old, new",
    [
        ("VARCHAR(10)", "VARCHAR(20)"),
        ("CHAR(1)", "CHAR(8)"),
        ("FLOAT(24)", "FLOAT(53)"),
        ("TIME(0)", "TIME(6)"),
        ("DATETIME2(3)", "DATETIME2(7)"),
    ],
)
def test_a_bigger_length_or_precision_is_a_widening(old, new):
    assert _classify_type_diff(old, new) == ("non_breaking", "column_type_widened")


def test_parsing_a_long_run_of_spaces_or_an_unclosed_list_stays_linear():
    """A type is untrusted text: the parser must not backtrack quadratically on it."""
    started = time.perf_counter()
    _parse_type("A" + " " * 50_000 + "x")
    _parse_type("A(" + "1" * 50_000)
    assert time.perf_counter() - started < 2.0


def test_an_oversized_integer_parameter_is_unparseable_text():
    if not hasattr(sys, "set_int_max_str_digits"):
        pytest.skip("this interpreter has no integer-string length limit")
    previous = sys.get_int_max_str_digits()
    sys.set_int_max_str_digits(640)
    try:
        raw = "VARCHAR(" + "9" * 700 + ")"
        assert _parse_type(raw) == (raw, None)
    finally:
        sys.set_int_max_str_digits(previous)


def test_a_non_ascii_type_name_says_why_it_is_not_the_same_type():
    report = _type_change_report("int", f"{DOTLESS_I}nt")

    (change,) = report.breaking
    assert "non-ASCII" in change.description


@pytest.mark.parametrize("fail_on_breaking, expected_rc", [(True, 1), (False, 0)])
def test_cli_diff_baseline_fails_on_a_type_change_that_used_to_pass(
    tmp_path, fail_on_breaking, expected_rc
):
    """``int`` -> ``decimal(5,0)`` was reported as a widening, so the CI gate stayed green."""
    old = _contract_076([{"name": "id", "type": "int"}])
    new = _contract_076([{"name": "id", "type": "decimal(5,0)"}])
    _assert_schema_valid(old, new)

    rc, report = _cli_diff_baseline(tmp_path, old, new, fail_on_breaking)

    assert rc == expected_rc
    assert [c["kind"] for c in report["breaking"]] == ["column_type_changed"]


def _contract_with_exposes(*exposes):
    """A 0.7.6 contract with one expose per ``(expose_id, columns)`` pair."""
    contract = _contract_076([])
    template = contract["exposes"][0]
    contract["exposes"] = []
    for expose_id, columns in exposes:
        expose = copy.deepcopy(template)
        expose["exposeId"] = expose_id
        expose["contract"]["schema"] = columns
        contract["exposes"].append(expose)
    return contract


@pytest.mark.parametrize(
    "declared, kinds",
    [
        pytest.param(["int", "string"], {"column_duplicate_name"}, id="last-matches-baseline"),
        pytest.param(
            ["string", "int"], {"column_duplicate_name", "column_type_changed"}, id="last-differs"
        ),
        pytest.param(["string", "string"], {"column_duplicate_name"}, id="identical"),
        pytest.param(["string"] * 3, {"column_duplicate_name"}, id="three-times"),
    ],
)
def test_076_a_column_declared_twice_in_the_new_contract_is_breaking(declared, kinds):
    """The index keeps the last declaration of a name, so the diff never saw the other one."""
    old = _contract_076([_ID_COLUMN, {"name": "note", "type": "string"}])
    new = _contract_076([_ID_COLUMN] + [{"name": "note", "type": t} for t in declared])
    _assert_schema_valid(old, new)

    report = compare_contracts(old, new)

    assert {c.kind for c in report.breaking} == kinds
    assert report.total == len(kinds)
    (duplicate,) = [c for c in report.breaking if c.kind == "column_duplicate_name"]
    assert duplicate.path == "exposes[0].contract.schema.note"
    assert [c["type"] for c in duplicate.after] == declared


_ZIP = {"name": "zip", "type": "STRING"}


def _address(*fields):
    return {"name": "addr", "type": "STRUCT", "fields": list(fields)}


@pytest.mark.parametrize(
    "old_columns, new_columns, non_breaking",
    [
        pytest.param(
            [_address(_ZIP)],
            [_address({"name": "zip", "type": "INT"}, _ZIP)],
            [],
            id="column-in-both-contracts",
        ),
        pytest.param(
            [{"name": "id", "type": "STRING"}],
            [{"name": "id", "type": "STRING"}, _address(_ZIP, _ZIP)],
            ["column_added"],
            id="column-is-new",
        ),
    ],
)
def test_a_nested_field_declared_twice_in_the_new_contract_is_breaking(
    old_columns, new_columns, non_breaking
):
    report = compare_contracts(
        _contract_flat(old_columns), _contract_flat(new_columns), validate=False
    )

    assert [c.kind for c in report.non_breaking] == non_breaking
    assert [(c.kind, c.path) for c in report.breaking] == [
        ("nested_field_duplicate_name", "exposes[0].schema.addr.fields.zip")
    ]


def test_076_an_expose_id_declared_twice_in_the_new_contract_is_breaking():
    as_string = [{"name": "a", "type": "string"}]
    as_int = [{"name": "a", "type": "int"}]
    old = _contract_with_exposes(("orders", as_string))
    new = _contract_with_exposes(("orders", as_int), ("orders", as_string))
    _assert_schema_valid(old, new)

    report = compare_contracts(old, new)

    assert report.total == 1
    (change,) = report.breaking
    assert (change.kind, change.path) == ("expose_duplicate_id", "exposes[1]")
    assert change.after == {"id": "orders", "indexes": [0, 1]}


def test_076_distinct_names_and_ids_are_not_duplicates():
    columns = [_ID_COLUMN, {"name": "note", "type": "string"}]
    contract = _contract_with_exposes(("orders", columns), ("customers", columns))
    _assert_schema_valid(contract)

    assert compare_contracts(contract, copy.deepcopy(contract)).total == 0


def test_a_duplicate_in_an_expose_that_is_new_to_the_contract_is_breaking():
    """A new expose has no baseline to hide a change from, but it is just as ambiguous."""
    customers = ("customers", [_ID_COLUMN])
    note = {"name": "note", "type": "string"}
    old = _contract_with_exposes(customers)
    new = _contract_with_exposes(customers, ("orders", [_ID_COLUMN, note, note]))
    _assert_schema_valid(old, new)

    report = compare_contracts(old, new)

    assert [c.kind for c in report.non_breaking] == ["expose_added"]
    assert [(c.kind, c.path) for c in report.breaking] == [
        ("column_duplicate_name", "exposes[1].contract.schema.note")
    ]


def test_a_nested_field_declared_twice_below_the_first_level_is_breaking():
    lat = {"name": "lat", "type": "DOUBLE"}
    geo = {"name": "geo", "type": "STRUCT", "fields": [lat, lat]}
    contract = _contract_flat([{"name": "addr", "type": "STRUCT", "fields": [geo]}])

    assert [(c.kind, c.path) for c in diff_duplicates(contract)] == [
        ("nested_field_duplicate_name", "exposes[0].schema.addr.fields.geo.fields.lat")
    ]


def test_076_a_duplicate_both_contracts_declare_is_still_reported():
    """The documented exception to idempotence: the earlier declaration is hidden in both."""
    note = {"name": "note", "type": "string"}
    contract = _contract_076([_ID_COLUMN, note, note])
    _assert_schema_valid(contract)

    report = compare_contracts(contract, copy.deepcopy(contract))

    assert [c.kind for c in report.breaking] == ["column_duplicate_name"]
    assert report.total == 1


def test_076_a_duplicate_only_the_baseline_declares_is_not_reported():
    note = {"name": "note", "type": "string"}
    old = _contract_076([_ID_COLUMN, note, note])
    new = _contract_076([_ID_COLUMN, note])

    assert compare_contracts(old, new).total == 0


def test_an_expose_is_named_by_id_or_exposeid_and_an_unnamed_one_by_position():
    exposes = [{"id": "a"}, {"exposeId": "a"}, {"id": "b"}, {"schema": []}, {"id": "_pos_3"}]

    found = diff_duplicates({"exposes": exposes})

    assert [(c.kind, c.path) for c in found] == [("expose_duplicate_id", "exposes[1]")]


@pytest.mark.parametrize("schema", [5, True, 3.5, "text", {"x": 1}])
def test_a_malformed_schema_in_an_expose_that_is_new_does_not_crash(schema):
    """The duplicate walk reads exposes the comparison used to skip as merely added.

    A schema-invalid contract only gets here with ``validate=False``.
    """
    old = {"exposes": [{"id": "a", "schema": [{"name": "x", "type": "INT"}]}]}
    new = {"exposes": [old["exposes"][0], {"id": "b", "schema": schema}]}

    report = compare_contracts(old, new, validate=False)

    assert [c.kind for c in report.non_breaking] == ["expose_added"]
    assert report.total == 1


def test_the_duplicate_findings_render():
    note = {"name": "note", "type": "string"}
    report = compare_contracts(_contract_076([_ID_COLUMN]), _contract_076([_ID_COLUMN, note, note]))

    assert "column_duplicate_name" in render_text(report)
    assert "column_duplicate_name" in render_markdown(report)
    assert json.loads(json.dumps(report.to_dict()))["breaking"][0]["after"] == [note, note]


# ---------------------------------------------------------------------------
# OTel span attributes on the diff sub-mode
# ---------------------------------------------------------------------------


def test_cli_diff_baseline_sets_otel_attributes(tmp_path, monkeypatch):
    """Version-diff mode should open a child span with mode/path/count attributes."""
    from fluid_build.cli import diff as diff_mod

    captured: list[tuple[str, object]] = []

    class _FakeSpan:
        def set_attribute(self, k, v):
            captured.append((k, v))

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def _fake_traced_span(name, attributes=None):
        if attributes:
            for k, v in attributes.items():
                captured.append((k, v))
        return _FakeSpan()

    monkeypatch.setattr(diff_mod, "_traced_span", _fake_traced_span)

    old, new = _cli_contracts(tmp_path)
    args = argparse.Namespace(
        contract=new,
        baseline=old,
        env=None,
        state=None,
        out=str(tmp_path / "diff.json"),
        exit_on_drift=False,
        fail_on_breaking=False,
        format="json",
        cmd="diff",
    )
    rc = diff_mod.run(args, logging.getLogger("test.diff"))
    assert rc == 0
    keys = {k for k, _ in captured}
    assert "fluid.diff.mode" in keys
    assert "fluid.diff.baseline_path" in keys
    assert "fluid.diff.new_path" in keys
    assert "fluid.diff.breaking_count" in keys
    assert "fluid.diff.non_breaking_count" in keys
    mode_value = next(v for k, v in captured if k == "fluid.diff.mode")
    assert mode_value == "version"
