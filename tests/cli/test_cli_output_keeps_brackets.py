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
from fluid_build.cli import main
from fluid_build.cli._common import CLIError
from fluid_build.cli.validate import _output_text_results

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

    rc = main(["validate", path])

    out = capsys.readouterr().out
    assert rc == 1
    assert "exposes[orders] declares governance.lakeFormation" in _unwrapped(out)


def test_validate_quiet_names_the_expose_in_its_error(tmp_path, capsys):
    path = _write(tmp_path, _contract("lakekeeper", lake_formation=True))

    rc = main(["validate", "--quiet", path])

    out = capsys.readouterr().out
    assert rc == 1
    assert "ERROR: exposes[orders] declares governance.lakeFormation" in _unwrapped(out)


def test_validate_json_is_unchanged(tmp_path, capsys):
    """The JSON path already printed literally; it must still parse and name the id."""
    path = _write(tmp_path, _contract("lakekeeper", lake_formation=True))

    rc = main(["validate", "--format", "json", path])

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

    _output_text_results(
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
    import fluid_build.cli.validate as validate_mod
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
    import fluid_build.cli.validate as validate_mod

    def _load(*_a, **_k):
        raise ValueError(MARKUP_LIKE)

    monkeypatch.setattr(validate_mod, "load_contract_with_overlay", _load)
    monkeypatch.setattr(
        catalog,
        "enrich",
        lambda *_a: ([f"pip install 'data-product-forge[gcp]' ({MARKUP_LIKE})"], None),
    )
    path = _write(tmp_path, _contract("lakekeeper", lake_formation=True))

    rc = main(["validate", path])

    out = _unwrapped(capsys.readouterr().out)
    assert rc == 1
    assert f"error: {MARKUP_LIKE}" in out
    assert f"pip install 'data-product-forge[gcp]' ({MARKUP_LIKE})" in out


def test_validate_missing_file_names_the_whole_path(monkeypatch, capsys):
    """The ``console_error`` lines wrap their text in ``[red]`` markup; the value is escaped."""
    import fluid_build.cli.security as security
    from fluid_build.cli.core import FluidCLIError

    def _missing(*_a, **_k):
        raise FluidCLIError(1, "file_not_found", "missing")

    monkeypatch.setattr(security, "validate_cli_path", _missing)

    rc = main(["validate", "orders[draft]\\"])

    err = _unwrapped(capsys.readouterr().err)
    assert rc == 1
    assert "Contract file not found: orders[draft]\\ " in err + " "


# ---------------------------------------------------------------------------
# main(): a CLIError's context, as `fluid generate iac` / `fluid apply` raise it
# ---------------------------------------------------------------------------


def test_generate_iac_unsupported_binding_names_the_expose(tmp_path, capsys):
    """A real UnsupportedBindingError, raised by the AWS emitter, rendered by main()."""
    path = _write(tmp_path, _contract("lakekeper", lake_formation=False))

    rc = main(["generate", "iac", path])

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
    from fluid_build.cli import _apply_opentofu_engine as engine
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

    rc = main(["validate", "unused.fluid.yaml"])

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

    rc = main(["validate", "unused.fluid.yaml"])

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
