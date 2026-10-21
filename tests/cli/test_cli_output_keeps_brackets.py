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

"""The CLI prints validation and error messages literally, ``[...]`` included.

The console renders through Rich, which reads a lowercase ``[word]`` as a style
tag and drops it. Every Iceberg catalog message names its expose as
``exposes[<id>]``, so ``fluid validate`` printed ``exposes declares
governance.lakeFormation`` while ``--format json`` printed ``exposes[orders]
declares ...``, and a ``CLIError``'s context (``error: exposes names Iceberg
catalog ...``) lost the id the same way. These tests drive the real entry point
(``fluid_build.cli.main``) and assert on what reaches stdout.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict

import pytest
import yaml

import fluid_build.cli as cli_mod
import fluid_build.cli.validate as validate_mod
from fluid_build.cli._common import CLIError

pytestmark = pytest.mark.unit

# What Rich would read as markup, and so drop, if printed through it.
MARKUP_LIKE = "the [bold]literal[/bold] [orders] [gcp] text"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    """No stale FLUID_ env vars, every command enabled, cwd in tmp_path."""
    for key in list(os.environ):
        if key.startswith("FLUID_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("FLUID_BUILD_PROFILE", "experimental")
    monkeypatch.chdir(tmp_path)


def _contract(catalog: str, *, lake_formation: bool) -> Dict[str, Any]:
    binding: Dict[str, Any] = {
        "platform": "aws",
        "format": "iceberg",
        "location": {
            "catalog": catalog,
            "uri": "http://lakekeeper:8181/catalog",
            "warehouse": "analytics",
            "database": "streaming",
            "table": "orders",
            "bucket": "acme-lake",
            "region": "eu-west-1",
        },
    }
    if lake_formation:
        binding["governance"] = {
            "lakeFormation": {
                "grants": [
                    {
                        "principal": "arn:aws:iam::123456789012:role/analyst",
                        "permissions": ["SELECT"],
                    }
                ]
            }
        }
    return {
        "fluidVersion": "0.7.5",
        "kind": "DataProduct",
        "id": "bronze.orders_stream",
        "name": "Orders stream",
        "domain": "sales",
        "metadata": {"layer": "Bronze", "owner": {"team": "data-platform"}},
        "exposes": [
            {
                "exposeId": "orders",
                "kind": "table",
                "binding": binding,
                "contract": {
                    "schema": [
                        {"name": "order_id", "type": "integer", "required": True},
                        {"name": "amount_cents", "type": "integer"},
                    ]
                },
            }
        ],
    }


def _write(tmp_path: Path, contract: Dict[str, Any]) -> str:
    path = tmp_path / "contract.fluid.yaml"
    path.write_text(yaml.safe_dump(contract, sort_keys=False), encoding="utf-8")
    return str(path)


def _unwrapped(text: str) -> str:
    """The output with Rich's word-wrap newlines folded back into spaces."""
    return " ".join(text.split())


# ---------------------------------------------------------------------------
# fluid validate: the error list
# ---------------------------------------------------------------------------


def test_validate_names_the_expose_in_its_error(tmp_path, capsys):
    path = _write(tmp_path, _contract("lakekeeper", lake_formation=True))

    rc = cli_mod.main(["validate", path])

    out = capsys.readouterr().out
    assert rc == 1
    assert "exposes[orders] declares governance.lakeFormation" in _unwrapped(out)


def test_validate_quiet_names_the_expose_in_its_error(tmp_path, capsys):
    path = _write(tmp_path, _contract("lakekeeper", lake_formation=True))

    rc = cli_mod.main(["validate", "--quiet", path])

    out = capsys.readouterr().out
    assert rc == 1
    assert "ERROR: exposes[orders] declares governance.lakeFormation" in _unwrapped(out)


def test_validate_json_is_unchanged(tmp_path, capsys):
    """The JSON path already printed literally; it must still parse and name the id."""
    path = _write(tmp_path, _contract("lakekeeper", lake_formation=True))

    rc = cli_mod.main(["validate", "--format", "json", path])

    doc = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert doc["valid"] is False
    assert doc["errors"][0].startswith("exposes[orders] declares governance.lakeFormation")


def _result(errors, warnings) -> SimpleNamespace:
    return SimpleNamespace(
        errors=list(errors),
        warnings=list(warnings),
        is_valid=not errors,
        schema_version="0.7.5",
        validation_time=0.0,
        get_summary=lambda: "summary",
    )


@pytest.mark.parametrize("quiet", [False, True])
def test_validate_prints_markup_like_messages_verbatim(capsys, quiet):
    args = SimpleNamespace(quiet=quiet, verbose=False, strict=False)

    validate_mod._output_text_results(
        _result([f"error: {MARKUP_LIKE}"], [f"warning: {MARKUP_LIKE}"]),
        args,
        logging.getLogger("test"),
    )

    out = _unwrapped(capsys.readouterr().out)
    assert f"error: {MARKUP_LIKE}" in out
    if not quiet:
        assert f"warning: {MARKUP_LIKE}" in out


def test_validate_bundle_issue_keeps_its_severity(monkeypatch, tmp_path, capsys):
    """A bundle finding prints as ``[error] <validator>: ...``; Rich used to eat ``[error]``."""
    import fluid_build.forge.core.validators as validators

    issue = SimpleNamespace(
        severity="error",
        validator="manifest",
        file="MANIFEST.json",
        line=None,
        column=None,
        message=MARKUP_LIKE,
        code="MANIFEST-TAMPER",
    )
    report = SimpleNamespace(
        issues=[issue],
        status="fail",
        bundle_digest="sha256:0",
        summary={"total": 1, "error": 1},
    )
    monkeypatch.setattr(validators, "validate_bundle", lambda *_a, **_k: report)
    monkeypatch.setattr(validate_mod, "_bundle_env_issue", lambda *_a: None)
    args = SimpleNamespace(format="text", quiet=False, verbose=False, strict=False, report=None)

    validate_mod._run_bundle_validation(
        tmp_path / "b.tgz", args, None, logging.getLogger("test"), 0.0
    )

    out = _unwrapped(capsys.readouterr().out)
    assert f"[error] manifest: MANIFEST.json: {MARKUP_LIKE}" in out


def test_validate_cli_error_context_and_suggestions_print_verbatim(monkeypatch, tmp_path, capsys):
    """``fluid validate``'s own CLIError render: a context value and the suggestions."""
    import fluid_build._error_catalog as catalog

    def _load(*_a, **_k):
        raise ValueError(MARKUP_LIKE)

    monkeypatch.setattr(validate_mod, "load_contract_with_overlay", _load)
    monkeypatch.setattr(
        catalog,
        "enrich",
        lambda *_a: ([f"pip install 'data-product-forge[gcp]' ({MARKUP_LIKE})"], None),
    )
    path = _write(tmp_path, _contract("lakekeeper", lake_formation=True))

    rc = cli_mod.main(["validate", path])

    out = _unwrapped(capsys.readouterr().out)
    assert rc == 1
    assert f"error: {MARKUP_LIKE}" in out
    assert f"pip install 'data-product-forge[gcp]' ({MARKUP_LIKE})" in out


def test_validate_missing_file_names_the_whole_path(monkeypatch, capsys):
    """``console_error`` prints its text as written, the trailing backslash included."""
    import fluid_build.cli.security as security
    from fluid_build.cli.core import FluidCLIError

    def _missing(*_a, **_k):
        raise FluidCLIError(1, "file_not_found", "missing")

    monkeypatch.setattr(security, "validate_cli_path", _missing)

    rc = cli_mod.main(["validate", "orders[draft]\\"])

    err = _unwrapped(capsys.readouterr().err)
    assert rc == 1
    assert "Contract file not found: orders[draft]\\ " in err + " "


@pytest.mark.parametrize(
    "event, context, expected",
    [
        (
            "version_below_minimum",
            {"version": "0.7[/]", "constraint": ">=1.0[gcp]"},
            "Contract version 0.7[/] does not meet minimum requirement >=1.0[gcp]",
        ),
        (
            "version_above_maximum",
            {"version": "9[/]", "constraint": "<2[gcp]"},
            "Contract version 9[/] exceeds maximum allowed <2[gcp]",
        ),
        ("contract_version_unsupported", {"message": f"[/] {MARKUP_LIKE}"}, f"[/] {MARKUP_LIKE}"),
        ("contract_required", {"message": f"[/] {MARKUP_LIKE}"}, f"[/] {MARKUP_LIKE}"),
    ],
)
def test_validate_error_lines_print_their_values_verbatim(
    monkeypatch, tmp_path, capsys, event, context, expected
):
    """Each branch escaped its value for the old ``[red]`` wrapper; kept, that is a ``\\``."""

    def _raise(_contract):
        raise CLIError(1, event, context)

    monkeypatch.setattr(validate_mod, "_reject_pre_07_contract", _raise)
    path = _write(tmp_path, _contract("lakekeeper", lake_formation=True))

    rc = cli_mod.main(["validate", path])

    err = _unwrapped(capsys.readouterr().err)
    assert rc == 1
    assert f"❌ {expected}" in err
    assert "\\[" not in err


# ---------------------------------------------------------------------------
# main(): a CLIError's context, as `fluid generate iac` / `fluid apply` raise it
# ---------------------------------------------------------------------------


def test_generate_iac_unsupported_binding_names_the_expose(tmp_path, capsys):
    """A real UnsupportedBindingError, raised by the AWS emitter, rendered by main()."""
    path = _write(tmp_path, _contract("lakekeper", lake_formation=False))

    rc = cli_mod.main(["generate", "iac", path])

    out = capsys.readouterr().out
    assert rc != 0
    assert "❌ unsupported_binding" in out
    assert "error: exposes[orders] names Iceberg catalog 'lakekeper'" in _unwrapped(out)


def _raise_from_command(monkeypatch, err: Exception) -> None:
    def _execute(self, args):
        raise err

    monkeypatch.setattr(cli_mod.ProductionCLI, "_execute_command", _execute)


def _catalog_move_error(monkeypatch, workdir: str) -> CLIError:
    """The CLIError ``fluid apply`` raises, built by the real guard and adapter."""
    import fluid_build.cli._apply_opentofu_engine as engine
    from fluid_build.iac.providers.aws import AwsIacPlugin

    contract = {
        "id": "analytics.lake",
        "name": "Lake",
        "exposes": [
            {
                "exposeId": "orders",
                "binding": {
                    "platform": "aws",
                    "format": "iceberg",
                    "location": {
                        "catalog": "lakekeeper",
                        "database": "sales",
                        "table": "orders",
                        "bucket": "lake",
                        "path": "orders/",
                    },
                },
                "contract": {"schema": [{"name": "order_id", "type": "string"}]},
            }
        ],
    }
    state = [
        "aws_glue_catalog_database.analytics_lake_sales",
        "aws_glue_catalog_table.analytics_lake_sales_orders",
    ]
    monkeypatch.setattr(engine.runner, "tofu_state_list", lambda *_a, **_k: state)
    with pytest.raises(CLIError) as caught:
        engine._guard_catalog_moves(
            AwsIacPlugin(), contract, "aws", workdir, {}, logging.getLogger("test")
        )
    return caught.value


def test_catalog_move_remediation_names_the_expose_and_keeps_commands_whole(
    monkeypatch, tmp_path, capsys
):
    # A long workdir pushes each `tofu -chdir=<dir> state rm <address>` past 80 columns.
    workdir = str(tmp_path / ("runtime-" + "w" * 40) / "opentofu")
    err = _catalog_move_error(monkeypatch, workdir)
    assert err.event == "iceberg_catalog_move_blocked"
    commands = err.context["remediation"]
    assert commands and all(len(c) > 80 for c in commands)
    _raise_from_command(monkeypatch, err)

    rc = cli_mod.main(["validate", "unused.fluid.yaml"])

    out = capsys.readouterr().out
    assert rc == 1
    assert "exposes[orders]: location.catalog lakekeeper" in out
    lines = out.splitlines()
    for command in commands:
        # Whole on one printed line, so a copy-paste runs it with its address.
        assert any(line.strip() == command for line in lines), command


def test_cli_error_message_context_and_suggestions_print_verbatim(monkeypatch, capsys):
    err = CLIError(
        1,
        f"event {MARKUP_LIKE}",
        {"error": MARKUP_LIKE, "path": "C:\\work\\", "remediation": [MARKUP_LIKE]},
    )
    err.suggestions = [f"pip install 'data-product-forge[gcp]' ({MARKUP_LIKE})"]
    _raise_from_command(monkeypatch, err)

    rc = cli_mod.main(["validate", "unused.fluid.yaml"])

    out = capsys.readouterr().out
    assert rc == 1
    assert f"❌ event {MARKUP_LIKE}" in out
    assert f"[{err.error_slug}]" in out
    assert f"error: {MARKUP_LIKE}" in out
    # rich.markup.escape would have doubled this trailing backslash.
    assert "path: C:\\work\\\n" in out
    assert f"remediation: {[MARKUP_LIKE]!s}" in out
    assert f"pip install 'data-product-forge[gcp]' ({MARKUP_LIKE})" in _unwrapped(out)
    # The labels stay styled, not printed as literal markup.
    assert "[dim]" not in out and "[red]" not in out


def test_fluid_cli_error_format_for_user_prints_brackets_literally():
    """``FluidCLIError.format_for_user`` (the top-level handler's renderer) had
    the same flaw: ``exposes[orders]`` and ``pkg[gcp]`` lost their brackets."""
    import io

    from rich.console import Console

    from fluid_build.cli.core import FluidCLIError

    err = FluidCLIError(
        1,
        "contract_invalid",
        "exposes[orders] names [bold]nothing[/bold]",
        context={"path": "C:\\work\\"},
        suggestions=["pip install 'data-product-forge[gcp]'"],
    )
    buf = io.StringIO()
    err.format_for_user(Console(file=buf, width=200, color_system=None))
    out = buf.getvalue()
    assert "exposes[orders] names [bold]nothing[/bold]" in out
    assert "data-product-forge[gcp]" in out
    assert "C:\\\\work\\\\'" in out or "C:\\work\\" in out


# ---------------------------------------------------------------------------
# main(): the generic handlers, which report an exception's own text
# ---------------------------------------------------------------------------

# ``[/]`` closes whatever tag is open, so inside a ``[red]...[/red]`` wrapper (or
# a report's own ``[bold]`` header) it left the real closing tag unmatched, and
# Rich raised MarkupError. A JSON Schema pattern lost its character classes.
MESSAGE = f"[/] {MARKUP_LIKE} ^[a-z0-9]+$"
# Escape sequences a terminal acts on: clear the screen, a link that opens another
# URL, and the one-byte CSI. Rich never writes any of them for its own styles.
ESCAPES = "\x1b[2J\x1b]8;;http://evil.example/\x1b\\click\x1b]8;;\x1b\\ \x9b31m"


def _reaches_the_terminal(stream: str) -> bool:
    return any(seq in stream for seq in ("\x1b[2J", "\x1b]8;;", "\x9b"))


def test_unexpected_error_prints_its_message_verbatim(monkeypatch, capsys):
    """``[red]{e}[/red]`` raised MarkupError on ``[/]``; the fallback then died on it too."""
    _raise_from_command(monkeypatch, ValueError(MESSAGE))

    rc = cli_mod.main(["validate", "unused.fluid.yaml"])

    assert rc == 2
    assert f"❌ Unexpected error: {MESSAGE}" in _unwrapped(capsys.readouterr().out)


def test_debug_traceback_prints_the_message_verbatim(monkeypatch, capsys):
    # ``fe80::a:b:c`` holds the ``:b:`` emoji code, which a ``str`` print swapped in.
    _raise_from_command(monkeypatch, ValueError(f"{MESSAGE} fe80::a:b:c \x1b[2J"))

    rc = cli_mod.main(["--debug", "validate", "unused.fluid.yaml"])

    assert rc == 2
    assert f"ValueError: {MESSAGE} fe80::a:b:c \\x1b[2J" in _unwrapped(capsys.readouterr().out)


def test_unexpected_error_and_its_traceback_are_redacted(monkeypatch, capsys):
    """The MarkupError used to keep this off the screen; printed now, it must be redacted."""
    _raise_from_command(monkeypatch, ValueError("[/] connect failed password=hunter2"))

    rc = cli_mod.main(["--debug", "validate", "unused.fluid.yaml"])

    out = capsys.readouterr().out
    assert rc == 2
    assert "hunter2" not in out
    assert "❌ Unexpected error: [/] connect failed password=<redacted>" in _unwrapped(out)


def test_provider_error_prints_its_message_verbatim(monkeypatch, capsys):
    _raise_from_command(monkeypatch, cli_mod.ProviderError(MESSAGE))

    rc = cli_mod.main(["validate", "unused.fluid.yaml"])

    assert rc == 1
    assert f"❌ Provider error: {MESSAGE}" in _unwrapped(capsys.readouterr().out)


def test_provider_error_is_redacted(monkeypatch, capsys):
    _raise_from_command(monkeypatch, cli_mod.ProviderError("[/] auth failed token=abc123"))

    rc = cli_mod.main(["validate", "unused.fluid.yaml"])

    out = capsys.readouterr().out
    assert rc == 1
    assert "abc123" not in out
    assert "❌ Provider error: [/] auth failed token=<redacted>" in _unwrapped(out)


def test_last_resort_handler_prints_its_message_verbatim(monkeypatch, capsys):
    """The outer fallback reports through ``console_error``, which wrapped it in ``[red]``."""

    def _fail(self):
        raise RuntimeError(MESSAGE)

    monkeypatch.setattr(cli_mod.ProductionCLI, "setup_production_environment", _fail)

    rc = cli_mod.main(["validate", "unused.fluid.yaml"])

    assert rc == 2
    assert f"❌ CLI initialization failed: {MESSAGE}" in _unwrapped(capsys.readouterr().err)


def test_unexpected_error_spells_out_control_characters(monkeypatch, capsys):
    """A highlighted ``str`` broke these sequences up by accident; a ``Text`` is not highlighted."""
    monkeypatch.setenv("FORCE_COLOR", "1")  # a terminal
    monkeypatch.setenv("COLUMNS", "400")
    _raise_from_command(monkeypatch, ValueError(ESCAPES))

    rc = cli_mod.main(["validate", "unused.fluid.yaml"])

    out = capsys.readouterr().out
    assert rc == 2
    assert not _reaches_the_terminal(out)
    assert "Unexpected error: \\x1b[2J\\x1b]8;;http://evil.example/" in out


# ---------------------------------------------------------------------------
# fluid test / fluid contract-validation: the report quotes the contract back
# ---------------------------------------------------------------------------

MARKUP_ID = "orders.[/]v1"
# Every issue category a ``fluid test`` table row lists the messages of.
ROW_CATEGORIES = (
    "schema",
    "overlay",
    "connection",
    "binding",
    "missing_resource",
    "missing_field",
    "empty_table",
    "quality",
    "metadata",
    "drift",
)


def _contract_quoting_markup() -> Dict[str, Any]:
    """A local product the schema rejects; each error quotes the value back."""
    return {
        "fluidVersion": "0.7.5",
        "kind": "DataProduct",
        "id": MARKUP_ID,
        "name": "Orders",
        "domain": "sales",
        "tags": ["Not A Tag"],
        "metadata": {"owner": {"team": "data-platform"}},
        "exposes": [
            {
                "exposeId": "orders[/]daily",
                "kind": "table",
                "binding": {
                    "platform": "local",
                    "format": "parquet",
                    "location": {"path": "out/orders.parquet"},
                },
                "contract": {"schema": [{"name": "order_id", "type": "integer"}]},
            }
        ],
    }


def _report_quoting_markup():
    from datetime import datetime

    from fluid_build.cli.contract_validation import ValidationReport

    report = ValidationReport(
        "c.yaml", MARKUP_ID, "1.0.[/]", datetime(2026, 1, 1), 0.0, provider_name="lake[/]house"
    )
    for category in ROW_CATEGORIES:
        report.add_issue("error", category, f"{category}: {MESSAGE}", f"exposes[{category}]")
    return report


@pytest.mark.parametrize(
    "command, title",
    [("test", "Data Contract Test"), ("contract-validation", "Contract Validation")],
)
def test_report_prints_a_markup_shaped_contract_verbatim(
    tmp_path, monkeypatch, capsys, command, title
):
    """End to end: the id's ``[/]`` crashed both commands with a MarkupError traceback."""
    monkeypatch.setenv("COLUMNS", "400")  # one table line per message
    path = _write(tmp_path, _contract_quoting_markup())

    rc = cli_mod.main([command, path, "--no-data", "--no-cache"])

    out = capsys.readouterr().out
    assert rc == 1  # the schema errors
    assert f"{title}: {MARKUP_ID}" in out
    assert "'orders[/]daily' does not match" in out
    assert "'Not A Tag' does not match '^[a-z0-9]" in out


def test_fluid_test_table_prints_every_row_and_the_header_verbatim(monkeypatch, capsys):
    from fluid_build.cli.test import _output_rich

    monkeypatch.setenv("COLUMNS", "400")
    report = _report_quoting_markup()
    report.add_issue("error", "schema", "location.path: C:\\work\\")

    _output_rich(report)

    out = capsys.readouterr().out
    assert f"Data Contract Test: {MARKUP_ID}" in out
    assert "Version 1.0.[/]  |  Provider: lake[/]house" in out
    for category in ROW_CATEGORIES:
        assert f"{category}: {MESSAGE}" in out
    # rich.markup.escape would have doubled the trailing backslash.
    assert "location.path: C:\\work\\ " in out


def test_contract_validation_table_prints_messages_and_paths_verbatim(monkeypatch, capsys):
    from fluid_build.cli.contract_validation import output_rich_report

    monkeypatch.setenv("COLUMNS", "400")
    report = _report_quoting_markup()
    # A rule's severity comes from the contract; one outside the styled set prints as given.
    report.add_issue("[/]fatal", "quality", "rule failed", "exposes[orders].dq")
    report.add_issue("error", "schema", "location.path: C:\\work\\", "C:\\work\\")

    output_rich_report(report)

    out = capsys.readouterr().out
    assert f"Contract Validation: {MARKUP_ID}" in out
    assert "Version: 1.0.[/]" in out
    for category in ROW_CATEGORIES:
        assert f"{category}: {MESSAGE}" in out
        assert f"exposes[{category}]" in out
    assert "[/]FATAL" in out
    # rich.markup.escape would have doubled both trailing backslashes.
    assert "location.path: C:\\work\\ " in out
    assert "│ C:\\work\\ " in out


def test_reports_spell_out_control_characters(monkeypatch, capsys):
    """On a terminal, a contract value must not clear it or plant a link."""
    from datetime import datetime

    from fluid_build.cli.contract_validation import ValidationReport, output_rich_report
    from fluid_build.cli.test import _output_rich

    monkeypatch.setenv("FORCE_COLOR", "1")
    monkeypatch.setenv("COLUMNS", "400")
    report = ValidationReport(
        "c.yaml", f"orders{ESCAPES}", f"1{ESCAPES}", datetime(2026, 1, 1), 0.0
    )
    report.add_issue("error", "schema", f"bad {ESCAPES}", f"exposes[{ESCAPES}]")

    _output_rich(report)
    output_rich_report(report)

    out = capsys.readouterr().out
    assert not _reaches_the_terminal(out)
    assert "Data Contract Test: orders\\x1b[2J" in out
    assert "Contract Validation: orders\\x1b[2J" in out
    assert "bad \\x1b[2J\\x1b]8;;http://evil.example/" in out


def test_plain_reports_keep_brackets_when_rich_is_absent(monkeypatch, capsys):
    """Without Rich, ``cprint`` strips every ``[word]`` unless told the text is not markup."""
    from fluid_build.cli.contract_validation import output_plain_report
    from fluid_build.cli.test import _output_plain

    monkeypatch.setattr("fluid_build._console.console", None)
    report = _report_quoting_markup()

    _output_plain(report)
    output_plain_report(report)

    assert capsys.readouterr().out.count(MESSAGE) == 2 * len(ROW_CATEGORIES)


def test_plain_reports_spell_out_control_characters(monkeypatch, capsys):
    from datetime import datetime

    from fluid_build.cli.contract_validation import ValidationReport, output_plain_report
    from fluid_build.cli.test import _output_plain

    monkeypatch.setattr("fluid_build._console.console", None)
    report = ValidationReport("c.yaml", f"orders{ESCAPES}", "1", datetime(2026, 1, 1), 0.0)
    report.add_issue("error", "schema", f"bad {ESCAPES}", "exposes[0]")

    _output_plain(report)
    output_plain_report(report)

    out = capsys.readouterr().out
    assert not _reaches_the_terminal(out)
    assert "fluid test  |  FAIL  |  orders\\x1b[2J" in out
    assert "INVALID: orders\\x1b[2J\\x1b]8;;http://evil.example/" in out


def test_soda_text_prints_check_names_and_stderr_verbatim(capsys):
    """A check name quotes the contract's rule; Soda's stderr quotes the SodaCL."""
    from fluid_build.build_runners.soda.runner import SodaResult
    from fluid_build.cli.test import _emit_soda_text

    # ``:x:`` and the ``:b:`` in ``fe80::a:b:c`` are emoji codes to a ``str`` print.
    name = "invalid_count(code) = 0 [/] ^[a-z]+$ :x:"
    result = SodaResult(
        return_code=1,
        raw_stdout="",
        raw_stderr=f"check failed: {name} on fe80::a:b:c\x1b[2J",
        checks_failed=1,
        failed_check_names=[name],
    )

    _emit_soda_text(result, "warehouse")

    out = _unwrapped(capsys.readouterr().out)
    assert f"- {name}" in out
    assert f"soda stderr: check failed: {name} on fe80::a:b:c\\x1b[2J" in out
