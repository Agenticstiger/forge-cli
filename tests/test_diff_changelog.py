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

"""Tests for the contract-vs-contract version diff (``fluid diff --baseline``).

Covers the diff engine (``api/changelog.py``), rule classifications
(``api/changelog_rules.py``), and the CLI mode-switching in ``cli/diff.py``.
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

import pytest

from fluid_build.api.changelog import compare_contracts, render_markdown, render_text
from fluid_build.api.changelog_rules import (
    _classify_type_diff,
    _parse_type,
    diff_agent_policy,
    diff_columns,
    diff_consumes,
    diff_quality_severity,
    diff_sovereignty,
)
from fluid_build.api.changelog_types import Change, ChangelogReport

FIXTURES = Path(__file__).parent / "fixtures" / "changelog"


@pytest.fixture
def v1_v2():
    """The on-disk fixture pair loaded as parsed contracts."""
    import yaml

    v1 = yaml.safe_load((FIXTURES / "v1.fluid.yaml").read_text())
    v2 = yaml.safe_load((FIXTURES / "v2.fluid.yaml").read_text())
    return v1, v2


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_same_contract_produces_empty_diff(v1_v2):
    v1, _ = v1_v2
    report = compare_contracts(v1, v1)
    assert report.total == 0
    assert not report.has_breaking


def test_report_total_matches_section_sums(v1_v2):
    v1, v2 = v1_v2
    report = compare_contracts(v1, v2)
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
    report = compare_contracts(v1, v2)

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
    report = compare_contracts(v1, v2)
    out = render_text(report)
    assert "Summary:" in out
    assert "breaking" in out.lower()


def test_render_text_empty_report_says_no_changes():
    out = render_text(ChangelogReport())
    assert "No changes detected." in out


def test_render_markdown_includes_section_headings(v1_v2):
    v1, v2 = v1_v2
    report = compare_contracts(v1, v2)
    md = render_markdown(report)
    assert "# Contract changelog" in md
    if report.breaking:
        assert "Breaking" in md
    if report.non_breaking:
        assert "Non-breaking" in md


def test_report_to_dict_round_trips_through_json(v1_v2):
    v1, v2 = v1_v2
    report = compare_contracts(v1, v2)
    data = report.to_dict()
    # Must be JSON-serialisable
    s = json.dumps(data)
    parsed = json.loads(s)
    assert parsed["summary"]["total"] == report.total
    assert parsed["summary"]["breaking"] == len(report.breaking)


# ---------------------------------------------------------------------------
# CLI mode-switch in cli/diff.py
# ---------------------------------------------------------------------------


def test_cli_diff_baseline_mode_smoke(tmp_path, monkeypatch):
    """End-to-end: ``fluid diff <new> --baseline <old>`` produces JSON and exits 0 by default."""
    from fluid_build.cli import diff as diff_mod

    out_path = tmp_path / "diff.json"
    args = argparse.Namespace(
        contract=str(FIXTURES / "v2.fluid.yaml"),
        baseline=str(FIXTURES / "v1.fluid.yaml"),
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

    args = argparse.Namespace(
        contract=str(FIXTURES / "v2.fluid.yaml"),
        baseline=str(FIXTURES / "v1.fluid.yaml"),
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

    args = argparse.Namespace(
        contract=str(FIXTURES / "v1.fluid.yaml"),
        baseline=str(FIXTURES / "v1.fluid.yaml"),
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


def test_decimal_mixed_param_change_is_info():
    """Precision grew but scale shrank — neither pure widening nor narrowing."""
    sev, kind = _classify_type_diff("DECIMAL(20,4)", "DECIMAL(30,2)")
    assert sev == "info"
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

    args = argparse.Namespace(
        contract=str(FIXTURES / "v2.fluid.yaml"),
        baseline=str(FIXTURES / "v1.fluid.yaml"),
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
