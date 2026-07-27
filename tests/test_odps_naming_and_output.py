# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""ODPS/OPDS naming residue and silent-write regressions (#308 / #381).

Four separate ways the CLI contradicted itself about the ODPS family:

* `fluid odps validate --spec odps-4.1` raised TypeError on *every* input —
  the CLI passed a ``schema_url`` kwarg the validator has never accepted, and a
  blanket ``except Exception`` reported the signature bug as a validation error;
* `fluid odps export --spec odps-4.1` wrapped the document in a FLUID envelope
  (``opds_version: "1.0"``) while printing an LF/ODPI conformance claim, so it
  and `generate standard --format odps-v4.1` emitted different documents for
  the same named standard;
* three of five `generate standard` formats wrote a file and printed nothing —
  including the documented default — which is indistinguishable from a no-op;
* the deprecated `fluid export-opds` minted a deprecated *filename*, and the
  `fluid opds` command alias was the only deprecated spelling in the family
  that never warned.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from fluid_build.cli import odps as odps_cli
from fluid_build.cli.generate_standard import DEFAULT_OUTPUTS, SUPPORTED_FORMATS

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTRACT = REPO_ROOT / "examples" / "snowflake" / "smoke" / "contract.fluid.yaml"


@pytest.fixture()
def contract() -> dict:
    """The contract as ``cmd_opds_export`` hands it to the v4.1 exporter.

    The CLI resolves ``{{ env.* }}`` before rendering, so a test that feeds
    ``_export_odps_v4_1`` a raw dict is not exercising the CLI path — and would
    compare an unresolved document against a resolved one.
    """
    from fluid_build.cli._export_env import resolve_for_export

    with open(CONTRACT) as handle:
        return resolve_for_export(yaml.safe_load(handle))


# ---------------------------------------------------------------------------
# `fluid odps validate --spec odps-4.1`
# ---------------------------------------------------------------------------


def test_v41_validator_signature_matches_the_cli_call() -> None:
    """The CLI passed ``schema_url=``; ``validate_opds_structure`` has no such
    parameter, so the v4.1 branch had a 0% success rate — it could not validate
    even FLUID's own v4.1 output."""
    import inspect

    from fluid_build.providers.opds.validator import validate_opds_structure

    params = set(inspect.signature(validate_opds_structure).parameters)
    assert "schema_url" not in params
    source = inspect.getsource(odps_cli.cmd_opds_validate)
    assert "schema_url=" not in source


def test_v41_export_then_validate_round_trips(tmp_path: Path, contract: dict) -> None:
    """End to end through the real CLI entry points: what the exporter emits,
    the validator must accept."""
    out = tmp_path / "product.odps-v4.1.json"
    export_args = _ns(contract=str(CONTRACT), spec="odps-4.1", out=str(out), pretty=True)
    assert odps_cli._export_odps_v4_1(export_args, contract, _logger()) == 0

    validate_args = _ns(file=str(out), spec="odps-4.1", full_schema=True)
    assert odps_cli.cmd_opds_validate(validate_args, _logger()) == 0


# ---------------------------------------------------------------------------
# `fluid odps export --spec odps-4.1` conformance
# ---------------------------------------------------------------------------


def test_v41_export_is_the_bare_lf_odpi_document(tmp_path: Path, contract: dict) -> None:
    out = tmp_path / "v41.json"
    assert odps_cli._export_odps_v4_1(_ns(out=str(out), pretty=True), contract, _logger()) == 0
    doc = json.loads(out.read_text())
    assert set(doc) == {"schema", "version", "product"}, sorted(doc)
    # The FLUID envelope keys — and the letter-swapped, wrong-versioned
    # ``opds_version: "1.0"`` in particular — must be gone.
    for key in ("opds_version", "artifacts", "generator", "export_config", "target_platform"):
        assert key not in doc


def test_v41_export_satisfies_the_vendored_lf_odpi_schema(tmp_path: Path, contract: dict) -> None:
    """The wrapper failed the published schema on all four root constraints."""
    jsonschema = pytest.importorskip("jsonschema")
    import fluid_build

    schema_path = (
        Path(fluid_build.__file__).parent / "providers" / "opds" / "opds-schema-v4.1.0.json"
    )
    schema = json.loads(schema_path.read_text())

    out = tmp_path / "v41.json"
    odps_cli._export_odps_v4_1(_ns(out=str(out), pretty=True), contract, _logger())
    doc = json.loads(out.read_text())

    validator = jsonschema.Draft202012Validator(schema)
    root_errors = [e for e in validator.iter_errors(doc) if not list(e.path)]
    assert not root_errors, [e.message for e in root_errors]


def test_both_v41_entry_points_emit_the_same_document(tmp_path: Path, contract: dict) -> None:
    """Two commands advertised as producing the same standard must not produce
    structurally different documents."""
    from fluid_build.cli import generate_standard

    via_odps = tmp_path / "a.json"
    odps_cli._export_odps_v4_1(_ns(out=str(via_odps), pretty=True), contract, _logger())

    via_generate = tmp_path / "b.json"
    generate_standard._export_odps_v4_1(str(CONTRACT), None, str(via_generate), _logger())

    assert json.loads(via_odps.read_text()) == json.loads(via_generate.read_text())


# ---------------------------------------------------------------------------
# Silent writes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fmt", ["odps", "odps-bitol", "odcs", "odps-v4.1", "opds"])
def test_every_format_reports_what_it_wrote(
    tmp_path: Path, capsys: pytest.CaptureFixture, fmt: str
) -> None:
    from fluid_build.cli import generate_standard

    out = tmp_path / f"out-{fmt.replace('.', '_')}"
    args = _ns(
        contract=str(CONTRACT),
        standard_format=fmt,
        out=str(out),
        env=None,
        list_formats=False,
    )
    assert generate_standard.run(args, _logger()) == 0
    # The console wraps at the terminal width, so compare with newlines removed.
    stdout = capsys.readouterr().out.replace("\n", "")
    assert "Wrote" in stdout, f"--format {fmt} wrote {out} but printed nothing"
    assert str(out) in stdout, f"--format {fmt} did not name the path it wrote"


# ---------------------------------------------------------------------------
# Deprecated spellings
# ---------------------------------------------------------------------------


def test_the_deprecated_command_does_not_mint_a_deprecated_filename() -> None:
    """`fluid export-opds` defaulted to ``product.opds.json`` — a repo ended up
    with that and a byte-identical ``product.odps-v4.1.json``."""
    import argparse

    from fluid_build.cli import export_odps

    parser = argparse.ArgumentParser()
    export_odps.register(parser.add_subparsers())
    args = parser.parse_args(["export-opds", "contract.fluid.yaml"])
    assert args.out.endswith("product.odps-v4.1.json")
    assert "opds" not in Path(args.out).name

    assert not any("product.opds.json" in v for v in DEFAULT_OUTPUTS.values())


def test_the_opds_command_alias_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every other deprecated spelling in the family warns; this one did not."""
    monkeypatch.setattr("sys.argv", ["fluid", "opds", "export", "c.fluid.yaml"])
    assert odps_cli.warn_if_opds_alias() is True

    monkeypatch.setattr("sys.argv", ["fluid", "odps", "export", "c.fluid.yaml"])
    assert odps_cli.warn_if_opds_alias() is False


def test_deprecated_format_alias_is_still_accepted() -> None:
    """Deprecating a spelling must not break it."""
    assert "opds" in SUPPORTED_FORMATS


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _ns(**kwargs):
    import argparse

    return argparse.Namespace(**kwargs)


def _logger():
    import logging

    return logging.getLogger("test.odps")
