# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid verify --require-every-port``: one verified port never stands for all.

``--strict`` fails only a run that verified nothing, so a run that verifies one
port passes while its others are skipped (no verifier for the binding, or a
reference-only table not built yet). With ``--require-every-port`` each skipped
port fails the run and is named with why. ``--warn-only`` does not downgrade it,
and ``--expose`` (one port) is refused beside it. ``fluid ship --release-dir``
passes it to stage 9.

Every test runs ``verify.run`` on a real contract file through the real
argument parser; none needs cloud credentials.
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Any, Dict, List

import pytest
import yaml

from fluid_build.cli import _ship_stages as st
from fluid_build.cli import build_parser
from fluid_build.cli import verify as verify_mod
from fluid_build.cli._common import CLIError

_LOG = logging.getLogger("test_verify_require_every_port")
_SCHEMA = [{"name": "id", "type": "string"}]


def _aws_parquet(expose_id: str) -> Dict[str, Any]:
    return {
        "exposeId": expose_id,
        "kind": "table",
        "binding": {
            "platform": "aws",
            "format": "parquet",
            "location": {"bucket": "my-bucket", "path": f"bronze/{expose_id}/"},
        },
        "contract": {"schema": _SCHEMA},
    }


def _local_csv(expose_id: str) -> Dict[str, Any]:
    return {
        "exposeId": expose_id,
        "kind": "table",
        "binding": {
            "platform": "local",
            "format": "csv",
            "location": {"path": f"runtime/out/{expose_id}.csv"},
        },
        "contract": {"schema": _SCHEMA},
    }


def _write(tmp_path: Path, exposes: List[Dict[str, Any]], *, reference_only: bool = False) -> Path:
    contract: Dict[str, Any] = {
        "fluidVersion": "0.7.5",
        "kind": "DataProduct",
        "id": "example.every_port",
        "name": "Every port",
        "domain": "example",
        "metadata": {"layer": "Bronze", "owner": {"team": "t"}},
        "exposes": exposes,
    }
    if reference_only:
        contract["builds"] = [{"id": "external", "pattern": "hybrid-reference"}]
    path = tmp_path / "contract.fluid.yaml"
    path.write_text(yaml.safe_dump(contract, sort_keys=False), encoding="utf-8")
    return path


def _built(tmp_path: Path, expose_id: str) -> None:
    out = tmp_path / "runtime" / "out" / f"{expose_id}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("id\na\nb\n", encoding="utf-8")


def _verify(contract: Path, *flags: str) -> int:
    args = build_parser().parse_args(["verify", str(contract), *flags])
    return verify_mod.run(args, _LOG)


def _printed(capsys: pytest.CaptureFixture[str]) -> str:
    out = capsys.readouterr()
    return " ".join((out.out + out.err).split())


@pytest.fixture(autouse=True)
def _workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def mixed(tmp_path: Path) -> Path:
    """One local CSV that verifies, one AWS parquet port forge cannot verify."""
    pytest.importorskip("duckdb")
    _built(tmp_path, "ids")
    return _write(tmp_path, [_local_csv("ids"), _aws_parquet("orders")])


def test_one_verified_port_passes_strict_but_not_every_port(
    mixed: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _verify(mixed, "--strict") == 0  # today's rule: the skip is only reported
    capsys.readouterr()
    assert _verify(mixed, "--strict", "--require-every-port") == 1
    text = _printed(capsys)
    assert (
        "--require-every-port: 1 expose(s) were not verified: orders (no verifier for the "
        "binding). Every expose has to be verified for this run to pass."
    ) in text


def test_it_needs_no_strict_and_warn_only_does_not_downgrade_it(mixed: Path) -> None:
    assert _verify(mixed, "--require-every-port") == 1
    assert _verify(mixed, "--require-every-port", "--warn-only") == 1


def test_every_port_verified_passes(tmp_path: Path) -> None:
    pytest.importorskip("duckdb")
    _built(tmp_path, "ids")
    _built(tmp_path, "more")
    contract = _write(tmp_path, [_local_csv("ids"), _local_csv("more")])
    assert _verify(contract, "--strict", "--require-every-port") == 0


def test_a_reference_only_port_not_built_yet_is_named(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pytest.importorskip("duckdb")
    _built(tmp_path, "ids")
    contract = _write(tmp_path, [_local_csv("ids"), _local_csv("later")], reference_only=True)
    assert _verify(contract, "--strict") == 0
    capsys.readouterr()
    assert _verify(contract, "--require-every-port") == 1
    assert "later (reference-only, not built yet)" in _printed(capsys)


def test_the_report_lists_the_unverified_ports(mixed: Path, tmp_path: Path) -> None:
    out = tmp_path / "verify-report.json"
    assert _verify(mixed, "--require-every-port", "--out", str(out)) == 1
    report = json.loads(out.read_text())
    assert report["require_every_port"] == {"unverified": ["orders (no verifier for the binding)"]}
    assert _verify(mixed, "--out", str(out)) == 0
    assert "require_every_port" not in json.loads(out.read_text())


def test_expose_beside_it_is_refused(mixed: Path) -> None:
    with pytest.raises(CLIError) as err:
        _verify(mixed, "--require-every-port", "--expose", "ids")
    assert err.value.exit_code == 2
    assert err.value.event == "verify_require_every_port_with_expose"


def test_ship_passes_it_to_stage_nine_only_for_a_release(tmp_path: Path) -> None:
    def verify_argv(*argv: str) -> List[str]:
        parser = argparse.ArgumentParser()
        from fluid_build.cli import ship

        ship.register(parser.add_subparsers(dest="command"))
        args = parser.parse_args(["ship", *argv])
        opts = st.resolve_options(args, "contract.fluid.yaml", {})
        return st.build_stage(9, opts, st.Workspace(tmp_path)).argv or []

    assert "--require-every-port" not in verify_argv("--yes")
    release = verify_argv("--yes", "--release-dir", "rel")
    assert release[:2] == ["verify", st.BUNDLE_PATH] and "--require-every-port" in release
