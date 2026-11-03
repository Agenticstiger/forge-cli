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

"""``fluid diff --baseline``: what reaches the gate, how it is printed, what 0.7.x carries.

Once 0.7.x columns reached ``--fail-on-breaking`` (PR #789), its reviews found
four gaps. The diff ran on contracts no schema accepts, so a value of the wrong
type hid a change. It printed contract names raw to a terminal and to Markdown.
It ignored the quality, sensitivity and primary-key information a 0.7.x contract
keeps under ``exposes[].contract``. And it read no columns from a
``{"fields": [...]}`` schema.
"""

from __future__ import annotations

import argparse
import json
import logging

import pytest
import yaml

from fluid_build.api.changelog import (
    ContractValidationError,
    compare_contracts,
    render_markdown,
    render_text,
)
from fluid_build.api.changelog_rules import diff_columns, diff_quality_severity
from fluid_build.api.changelog_types import Change, ChangelogReport
from tests.test_diff_changelog import _ID_COLUMN as _ID
from tests.test_diff_changelog import _contract_076


def _contract(columns, **section):
    """``_contract_076`` with ``section`` (``dq``, ``quality``) beside the schema too."""
    contract = _contract_076(columns)
    contract["exposes"][0]["contract"].update(section)
    return contract


def _run_cli(tmp_path, old, new, *, fmt="json", fail_on_breaking=False):
    """``fluid diff <new> --baseline <old>`` on two contracts written to ``tmp_path``."""
    from fluid_build.cli import diff as diff_mod

    old_path, new_path = tmp_path / "old.fluid.yaml", tmp_path / "new.fluid.yaml"
    old_path.write_text(yaml.safe_dump(old), encoding="utf-8")
    new_path.write_text(yaml.safe_dump(new), encoding="utf-8")
    args = argparse.Namespace(
        contract=str(new_path),
        baseline=str(old_path),
        env=None,
        state=None,
        out=str(tmp_path / "diff.json"),
        exit_on_drift=False,
        fail_on_breaking=fail_on_breaking,
        format=fmt,
        cmd="diff",
    )
    return diff_mod.run(args, logging.getLogger("test.diff"))


# ---------------------------------------------------------------------------
# Both contracts are schema-validated before they are diffed
# ---------------------------------------------------------------------------


def test_every_contract_below_validates_against_the_bundled_schema():
    """The shapes these tests diff: if 0.7.6 stops accepting them they prove nothing."""
    from fluid_build.schema_manager import FluidSchemaManager

    shapes = [
        _contract([_ID]),
        _contract([{**_ID, "tags": ["primary-key"]}, {"name": "email", "type": "string"}]),
        _contract([{"name": "email", "type": "string", "sensitivity": "pii"}]),
        _contract(
            [
                {
                    "name": "email",
                    "type": "string",
                    "quality": [{"id": "email-valid", "type": "text", "severity": "warning"}],
                }
            ],
            dq={"rules": [{"id": "complete", "type": "completeness", "severity": "critical"}]},
            quality=[{"rule": "positive", "expression": "amount > 0", "severity": "warning"}],
        ),
    ]
    for shape in shapes:
        result = FluidSchemaManager().validate_contract(shape, offline_only=True)
        assert result.is_valid, result.errors


def test_a_value_of_the_wrong_type_cannot_hide_a_change_from_the_diff():
    """A ``type`` that is a number was never compared, so the retype passed the gate."""
    old = _contract([_ID, {"name": "amount", "type": "decimal(18,2)"}])
    new = _contract([_ID, {"name": "amount", "type": 18}])

    with pytest.raises(ContractValidationError) as excinfo:
        compare_contracts(old, new)

    assert set(excinfo.value.errors) == {"new"}
    assert any("exposes[0].contract.schema[1].type" in e for e in excinfo.value.errors["new"])


def test_every_invalid_side_is_reported_at_once():
    old = _contract([_ID])
    del old["metadata"]["owner"]
    new = _contract([{"name": "id"}])

    with pytest.raises(ContractValidationError) as excinfo:
        compare_contracts(old, new)

    assert set(excinfo.value.errors) == {"baseline", "new"}
    assert "baseline contract" in str(excinfo.value) and "new contract" in str(excinfo.value)


@pytest.mark.parametrize("version", ["0.6.0", "0.5.7", "0.4"])
def test_a_pre_07_contract_is_refused_with_the_upgrade_message(version):
    old = _contract([_ID])
    old["fluidVersion"] = version

    with pytest.raises(ContractValidationError) as excinfo:
        compare_contracts(old, _contract([_ID]))

    (message,) = excinfo.value.errors["baseline"]
    assert "no longer supported" in message and "0.7.x" in message


def test_a_contract_without_a_fluid_version_is_refused():
    new = _contract([_ID])
    del new["fluidVersion"]

    with pytest.raises(ContractValidationError) as excinfo:
        compare_contracts(_contract([_ID]), new)

    assert set(excinfo.value.errors) == {"new"}


def test_validate_false_diffs_what_it_is_given():
    """The refine preview's opt-out: an advisory diff of a contract still being repaired."""
    old = {"exposes": [{"id": "x", "schema": [{"name": "a", "type": "STRING"}]}]}
    new = {"exposes": [{"id": "x", "schema": []}]}

    report = compare_contracts(old, new, validate=False)

    assert [c.kind for c in report.breaking] == ["column_removed"]


@pytest.mark.parametrize("fail_on_breaking", [True, False])
def test_the_cli_exits_2_on_an_invalid_contract_whatever_fail_on_breaking_says(
    tmp_path, fail_on_breaking
):
    """diff(1)'s 2 is "trouble": an invalid contract never reads as 0 (clean) or 1 (breaking)."""
    from fluid_build.cli._common import CLIError

    old = _contract([_ID, {"name": "amount", "type": "decimal(18,2)"}])
    new = _contract([_ID, {"name": "amount", "type": 18}])

    with pytest.raises(CLIError) as excinfo:
        _run_cli(tmp_path, old, new, fail_on_breaking=fail_on_breaking)

    assert excinfo.value.exit_code == 2
    assert excinfo.value.event == "diff_contract_invalid"
    context = excinfo.value.context
    assert context["invalid"] == ["new"]
    assert str(tmp_path / "new.fluid.yaml") in context["message"]
    assert "fluid validate" in context["message"]
    assert not (tmp_path / "diff.json").exists()


# ---------------------------------------------------------------------------
# Names are printed as data: escaped for a terminal and inert in Markdown
# ---------------------------------------------------------------------------


def _report(*names):
    """A report naming each of ``names`` as an added column, as ``diff_columns`` words it."""
    report = ChangelogReport()
    for name in names:
        report.add(
            Change(
                path=f"exposes[0].contract.schema.{name}",
                kind="column_added",
                severity="non_breaking",
                description=f"column '{name}' added to expose 'orders'",
            )
        )
    return report


@pytest.mark.parametrize(
    "name, spelled",
    [
        pytest.param(
            "x\x1b]8;;https://evil.example\x07click\x1b]8;;\x07",
            "x\\x1b]8;;https://evil.example\\x07click\\x1b]8;;\\x07",
            id="osc8-hyperlink",
        ),
        pytest.param("x\x1b[2J", "x\\x1b[2J", id="clear-screen"),
        pytest.param("x\x9b31m", "x\\x9b31m", id="c1-csi"),
        pytest.param("a\rb", "a\\x0db", id="carriage-return"),
        pytest.param("a\tb", "a\\x09b", id="tab"),
        pytest.param("admin\u202egnp.exe", "admin\\u202egnp.exe", id="bidi-override"),
        pytest.param("e\u200bmail", "e\\u200bmail", id="zero-width-space"),
        pytest.param("e\ufe0fmail", "e\\ufe0fmail", id="variation-selector"),
        pytest.param("\u3164", "\\u3164", id="hangul-filler"),
        pytest.param("x\U000e0041", "x\\U000e0041", id="tag-character"),
        pytest.param("a\\x1b", "a\\\\x1b", id="literal-backslash-stays-distinct"),
        pytest.param("straße_日本", "straße_日本", id="printable-unicode-is-kept"),
    ],
)
def test_render_text_spells_out_what_a_terminal_would_act_on(name, spelled):
    out = render_text(_report(name))

    assert f"column '{spelled}' added" in out
    assert f"exposes[0].contract.schema.{spelled}:" in out
    assert all(ch == "\n" or ch.isprintable() for ch in out)


def test_a_newline_in_a_name_cannot_forge_a_line():
    out = render_text(_report("a\nSummary: 0 breaking, 0 non-breaking, 0 info"))

    assert [line for line in out.splitlines() if line.startswith("Summary:")] == [
        "Summary: 0 breaking, 1 non-breaking, 0 info"
    ]
    assert "a\\x0aSummary: 0 breaking" in out


def _html(markdown):
    from markdown_it import MarkdownIt

    return MarkdownIt("commonmark").enable(["table", "strikethrough"]).render(markdown)


@pytest.mark.parametrize(
    "name",
    [
        pytest.param("<!-- everything below is hidden", id="html-comment"),
        pytest.param("<img src=https://tracker.example/p.gif>", id="html-image"),
        pytest.param("[click](https://evil.example)", id="link"),
        pytest.param("![x](https://tracker.example/p.gif)", id="image"),
        pytest.param("**bold** _em_ ~~gone~~", id="emphasis"),
        pytest.param("a\n# Summary: 0 breaking", id="newline-heading"),
        pytest.param("a\n- forged entry", id="newline-list-item"),
        pytest.param("a`b", id="one-backtick"),
        pytest.param("a``b```c", id="backtick-runs"),
        pytest.param("`edge`", id="backtick-at-both-ends"),
        pytest.param(" spaced ", id="space-at-both-ends"),
        pytest.param("@octocat #1 :smile: GH-1 foo@example.com", id="host-references"),
        pytest.param("a\\", id="trailing-backslash"),
    ],
)
def test_render_markdown_keeps_every_name_inside_a_code_span(name):
    """A code span is the one inline construct nothing is read out of.

    Outside one, a backslash escape stops CommonMark markup but not what GitHub
    adds after rendering: ``\\@user`` still notifies and ``\\#1`` still links.
    """
    report = _report(name)
    html = _html(render_markdown(report))

    assert html.count("<li>") == 1
    assert html.count("<h1>") == 1  # the "Contract changelog" title
    for tag in ("<a ", "<img", "<!--", "<em>", "<strong>bold", "<s>", "<h2>Summary"):
        assert tag not in html, (tag, html)
    # Both data fields arrive intact as code: the path and the description.
    from html import escape

    for code in (report.non_breaking[0].path, report.non_breaking[0].description):
        spelled = code.replace("\\", "\\\\").replace("\n", "\\x0a")
        assert f"<code>{escape(spelled, quote=False)}</code>" in html, html


def test_render_markdown_reads_as_before_for_an_ordinary_name():
    md = render_markdown(_report("email"))

    assert (
        "- **column_added** `exposes[0].contract.schema.email` — "
        "`column 'email' added to expose 'orders'`"
    ) in md


def test_the_cli_prints_text_without_reading_it_as_rich_markup(tmp_path, capsys):
    """Rich read ``[bold]`` as a style, swapped ``:smile:`` for an emoji and raised on ``[/]``."""
    names = ["[bold]x[/bold]", "[/]", ":smile:"]
    old = _contract([_ID])
    new = _contract([_ID] + [{"name": n, "type": "string"} for n in names])

    assert _run_cli(tmp_path, old, new, fmt="text") == 0

    lines = capsys.readouterr().out.splitlines()
    for name in names:
        # One line per entry, as render_text wrote it: Rich does not wrap it either.
        (line,) = [line for line in lines if f".schema.{name}:" in line]
        assert line.endswith(f"column '{name}' added to expose 'orders'")


@pytest.mark.parametrize("fmt", ["markdown", "json"])
def test_the_cli_prints_markdown_and_json_verbatim(tmp_path, capsys, fmt):
    """Rich wrapped a long line at 80 columns, which put a line break inside a JSON string
    and could start a Markdown heading inside a code span."""
    long_name = "customer_lifetime_value_estimate_in_reporting_currency_v2 # [bold]x[/bold] :smile:"
    old = _contract([_ID])
    new = _contract([_ID, {"name": long_name, "type": "string"}])

    assert _run_cli(tmp_path, old, new, fmt=fmt) == 0

    out = capsys.readouterr().out
    report = compare_contracts(old, new)
    if fmt == "json":
        assert json.loads(out) == report.to_dict()
    else:
        assert out == render_markdown(report) + "\n"


def test_the_refine_preview_prints_names_literally():
    from rich.console import Console

    from fluid_build.cli._preview_panel import render_refine_diff

    console = Console(record=True, width=200)
    old = _contract([_ID])
    new = _contract([_ID, {"name": "[bold]x[/bold]", "type": "string"}])

    assert render_refine_diff(old, new, console=console) is True

    assert "column '[bold]x[/bold]' added" in console.export_text()


# ---------------------------------------------------------------------------
# 0.7.x keeps quality, sensitivity and the primary key under exposes[].contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "old_severity, new_severity, escalated",
    [
        ("warn", "error", True),
        ("warn", "critical", True),
        ("info", "error", True),
        ("critical", "error", False),
        ("error", "critical", False),
        ("error", "warn", False),
    ],
)
def test_a_dq_rule_escalated_to_a_failing_severity_is_breaking(
    old_severity, new_severity, escalated
):
    def rules(severity):
        return {"rules": [{"id": "orders_complete", "type": "completeness", "severity": severity}]}

    report = compare_contracts(
        _contract([_ID], dq=rules(old_severity)), _contract([_ID], dq=rules(new_severity))
    )

    if not escalated:
        assert report.total == 0
        return
    (change,) = report.breaking
    assert change.kind == "quality_severity_escalated"
    assert change.path == "exposes[0].contract.dq.rules.orders_complete.severity"
    assert (change.before, change.after) == (old_severity, new_severity)
    assert f"severity={new_severity}" in change.description


def test_a_contract_quality_rule_escalated_to_error_is_breaking():
    def quality(severity):
        return [{"rule": "positive_amount", "expression": "amount > 0", "severity": severity}]

    report = compare_contracts(
        _contract([_ID], quality=quality("warning")), _contract([_ID], quality=quality("error"))
    )

    (change,) = report.breaking
    assert change.kind == "quality_severity_escalated"
    assert change.path == "exposes[0].contract.quality.positive_amount.severity"


def test_a_column_quality_rule_escalated_to_error_is_breaking():
    def email(severity):
        rule = {"id": "email-valid", "type": "text", "severity": severity}
        return {"name": "email", "type": "string", "quality": [rule]}

    report = compare_contracts(_contract([email("warning")]), _contract([email("error")]))

    (change,) = report.breaking
    assert change.kind == "quality_severity_escalated"
    assert change.path == "exposes[0].contract.schema.email.quality.email-valid.severity"
    assert "column 'email'" in change.description


@pytest.mark.parametrize(
    "old_severity, new_severity, escalated",
    [("warn", "critical", True), ("critical", "error", False)],
)
def test_a_pre_07_quality_test_is_judged_by_the_same_severity_scale(
    old_severity, new_severity, escalated
):
    """``critical`` fails a run as ``error`` does (``fluid_build.severity``)."""

    def expose(severity):
        return {"id": "x", "quality": {"tests": [{"name": "t1", "severity": severity}]}}

    changes = diff_quality_severity(expose(old_severity), expose(new_severity), "x", 0)

    assert [c.kind for c in changes] == (["quality_severity_escalated"] if escalated else [])


@pytest.mark.parametrize(
    "old, new, kind, before, after",
    [
        ("none", "pii", "column_pii_added", False, True),
        (None, "phi", "column_pii_added", False, True),
        ("pii", "anonymized", "column_pii_removed", True, False),
        ("internal", "restricted", "column_sensitivity_changed", "internal", "restricted"),
        ("pii", "phi", "column_sensitivity_changed", "pii", "phi"),
        (None, "confidential", "column_sensitivity_changed", None, "confidential"),
    ],
)
def test_a_sensitivity_change_is_an_info_signal(old, new, kind, before, after):
    def email(sensitivity):
        column = {"name": "email", "type": "string"}
        return {**column, "sensitivity": sensitivity} if sensitivity else column

    report = compare_contracts(_contract([email(old)]), _contract([email(new)]))

    (change,) = report.info
    assert report.total == 1
    assert (change.kind, change.path) == (kind, "exposes[0].contract.schema.email.sensitivity")
    assert (change.before, change.after) == (before, after)


@pytest.mark.parametrize(
    "tag, validate",
    [
        ("primary-key", True),
        # The ODCS exporter's other spelling: ``$defs.tags`` is lower-kebab-case,
        # so only an unvalidated diff meets it.
        ("primaryKey", False),
    ],
)
def test_a_tagged_primary_key_column_made_nullable_is_breaking(tag, validate):
    def key(required):
        return {"name": "id", "type": "string", "required": required, "tags": [tag]}

    report = compare_contracts(_contract([key(True)]), _contract([key(False)]), validate=validate)

    (change,) = report.breaking
    assert report.total == 1
    assert change.kind == "primary_key_nullable"
    assert change.path == "exposes[0].contract.schema.id.required"


def test_the_cli_gate_fails_on_a_primary_key_made_nullable(tmp_path):
    key = {"name": "id", "type": "string", "required": True, "tags": ["primary-key"]}

    rc = _run_cli(
        tmp_path, _contract([key]), _contract([{**key, "required": False}]), fail_on_breaking=True
    )

    assert rc == 1


# ---------------------------------------------------------------------------
# A schema may wrap its column list as {"fields": [...]}
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "layout, at",
    [
        pytest.param(lambda cols: {"schema": {"fields": cols}}, "schema.fields", id="flat"),
        pytest.param(
            lambda cols: {"contract": {"schema": {"fields": cols}}},
            "contract.schema.fields",
            id="contract",
        ),
    ],
)
def test_a_wrapped_schema_is_read(layout, at):
    email = {"name": "email", "type": "STRING"}
    old = {"id": "x", **layout([_ID, email])}
    new = {"id": "x", **layout([_ID])}

    changes = diff_columns(old, new, "x", 0)

    assert [(c.kind, c.path) for c in changes] == [("column_removed", f"exposes[0].{at}.email")]


def test_wrapping_a_schema_is_not_a_change_to_its_columns():
    columns = [_ID, {"name": "email", "type": "STRING"}]

    assert diff_columns({"schema": columns}, {"schema": {"fields": columns}}, "x", 0) == []


def test_a_wrapped_schema_is_read_when_validation_is_off_and_refused_when_it_is_on():
    """No bundled schema accepts the wrapper, so only an unvalidated diff ever meets it."""
    email = {"name": "email", "type": "string"}
    old, new = _contract([_ID, email]), _contract([_ID])
    for contract in (old, new):
        expose = contract["exposes"][0]
        expose["contract"]["schema"] = {"fields": expose["contract"]["schema"]}

    report = compare_contracts(old, new, validate=False)
    assert [(c.kind, c.path) for c in report.breaking] == [
        ("column_removed", "exposes[0].contract.schema.fields.email")
    ]
    with pytest.raises(ContractValidationError):
        compare_contracts(old, new)
