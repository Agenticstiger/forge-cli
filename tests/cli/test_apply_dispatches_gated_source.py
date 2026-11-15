# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Simple-mode ``fluid apply`` dispatches what its gates checked, never a re-read.

``run()`` reads the input once: a ``plan.json`` is digest-verified and
mode-checked (``_load_verified_plan``), a contract is schema-gated
(``_load_apply_source``). Simple mode used to resolve its actions by opening
the file a second time, so a file rewritten after the gates was what ran: a
verified plan whose action list was swapped dispatched the new SQL, exit 0.

Each test here rewrites the input after every gate has passed, inside
``build_provider`` (simple mode calls it just before it resolves the
actions, the window a racing writer aims for), applies with the real local
provider, and reads back through DuckDB what was written. The rows must be
the verified ones.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List

import pytest

pytestmark = [pytest.mark.integration]

duckdb = pytest.importorskip("duckdb")

_VERIFIED = "Hello, FLUID!"
_ATTACKER_SQL = "SELECT 'TAMPERED' AS message, CURRENT_TIMESTAMP AS created_at"
_OUTPUT = Path("runtime") / "out" / "hello-world-v1.csv"
_LOG = logging.getLogger("fluid_build.cli.apply.gated_source")


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The bundled hello-world contract (one SQL build into a local CSV)."""
    src = (
        Path(__file__).resolve().parents[2] / "examples" / "01-hello-world" / "contract.fluid.yaml"
    )
    shutil.copyfile(src, tmp_path / "contract.fluid.yaml")
    monkeypatch.chdir(tmp_path)
    from fluid_build.cli import apply as apply_mod

    monkeypatch.setattr(apply_mod, "hydrate_dotenv", lambda *a, **kw: None)
    return tmp_path


def _fluid_plan(workspace: Path, *extra: str) -> Path:
    """Stage 6 for real: ``fluid plan`` writes a digest-bound ``plan.json``."""
    out = workspace / "plan.json"
    proc = subprocess.run(
        [sys.executable, "-m", "fluid_build.cli", "--provider", "local", "plan"]
        + ["contract.fluid.yaml", "--out", str(out), *extra],
        cwd=workspace,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return out


def _apply(source: Path, *extra: str) -> int:
    """Stage 7 in-process, through the real argument parser."""
    from fluid_build.cli import apply as apply_mod
    from fluid_build.cli import build_parser

    args = build_parser().parse_args(["--provider", "local", "apply", str(source), "--yes", *extra])
    return apply_mod.run(args, _LOG)


def _rewrite_after_the_gates(
    monkeypatch: pytest.MonkeyPatch, rewrite: Callable[[], None]
) -> List[int]:
    from fluid_build.cli import apply as apply_mod

    real_build_provider = apply_mod.build_provider
    rewrites: List[int] = []

    def _racing_build_provider(*args: Any, **kwargs: Any) -> Any:
        rewrite()
        rewrites.append(1)
        return real_build_provider(*args, **kwargs)

    monkeypatch.setattr(apply_mod, "build_provider", _racing_build_provider)
    return rewrites


def _swap_plan(plan_path: Path, *, contract_sql: bool) -> Callable[[], None]:
    """Replace the plan's actions with one SQL action the local provider runs;
    with ``contract_sql``, also the embedded contract's build SQL."""

    def _rewrite() -> None:
        plan: Dict[str, Any] = json.loads(plan_path.read_text(encoding="utf-8"))
        plan["actions"] = [
            {
                "op": "sql",
                "sql": _ATTACKER_SQL,
                "inputs": [],
                "outputs": [{"path": str(_OUTPUT), "format": "csv"}],
            }
        ]
        if contract_sql:
            plan["contract"]["builds"][0]["properties"]["sql"] = _ATTACKER_SQL
        plan_path.write_text(json.dumps(plan), encoding="utf-8")

    return _rewrite


def _written(workspace: Path) -> List[str]:
    rel = duckdb.read_csv(str(workspace / _OUTPUT), all_varchar=True)
    return [row[0] for row in rel.fetchall()]


def test_a_plan_rewritten_after_the_gates_dispatches_the_verified_actions(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan_path = _fluid_plan(workspace)
    rewrites = _rewrite_after_the_gates(monkeypatch, _swap_plan(plan_path, contract_sql=False))

    assert _apply(plan_path) == 0

    assert rewrites, "the rewrite never ran, so this test proved nothing"
    assert "TAMPERED" in plan_path.read_text(encoding="utf-8")
    assert _written(workspace) == [_VERIFIED]


def test_a_replace_plan_rewritten_after_the_gates_replans_the_verified_contract(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A destructive mode re-plans from the plan's embedded contract: the
    verified copy, not the one in the rewritten file."""
    plan_path = _fluid_plan(workspace, "--mode", "replace")
    rewrites = _rewrite_after_the_gates(monkeypatch, _swap_plan(plan_path, contract_sql=True))

    assert _apply(plan_path, "--mode", "replace", "--allow-data-loss") == 0

    assert rewrites
    assert _written(workspace) == [_VERIFIED]


def test_a_contract_rewritten_after_the_gate_dispatches_the_gated_contract(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    contract_path = workspace / "contract.fluid.yaml"

    def _rewrite() -> None:
        text = contract_path.read_text(encoding="utf-8")
        contract_path.write_text(text.replace(_VERIFIED, "TAMPERED"), encoding="utf-8")

    rewrites = _rewrite_after_the_gates(monkeypatch, _rewrite)

    assert _apply(contract_path) == 0

    assert rewrites
    assert "TAMPERED" in contract_path.read_text(encoding="utf-8")
    assert _written(workspace) == [_VERIFIED]


def test_a_config_override_changes_no_action_of_a_verified_plan(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--config-override`` is applied after the gates, to the contract the
    provider is chosen from. The actions of a bound plan stay the ones its
    digest covers, so an override cannot re-plan a replace either."""
    plan_path = _fluid_plan(workspace, "--mode", "replace")
    override = json.dumps(
        {
            "builds": [
                {
                    "id": "hello_transformation",
                    "pattern": "embedded-logic",
                    "engine": "sql",
                    "properties": {"sql": _ATTACKER_SQL},
                }
            ]
        }
    )

    rc = _apply(plan_path, "--mode", "replace", "--allow-data-loss", "--config-override", override)

    assert rc == 0
    assert _written(workspace) == [_VERIFIED]


@pytest.mark.parametrize("source", ["plan", "contract"])
def test_the_source_is_read_once(
    source: str, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The file is opened by the loader that gates it, and by nothing after."""
    from fluid_build.cli import apply as apply_mod

    path = _fluid_plan(workspace) if source == "plan" else workspace / "contract.fluid.yaml"
    reads: List[str] = []
    real_read_json = apply_mod.read_json
    real_load = apply_mod.load_contract_with_overlay

    def _read_json(p: Any, *a: Any, **kw: Any) -> Any:
        if Path(p).resolve() == path.resolve():
            reads.append("read_json")
        return real_read_json(p, *a, **kw)

    def _load(p: Any, *a: Any, **kw: Any) -> Any:
        if Path(p).resolve() == path.resolve():
            reads.append("load_contract_with_overlay")
        return real_load(p, *a, **kw)

    monkeypatch.setattr(apply_mod, "read_json", _read_json)
    monkeypatch.setattr(apply_mod, "load_contract_with_overlay", _load)

    assert _apply(path) == 0

    expected = ["read_json"] if source == "plan" else ["load_contract_with_overlay"]
    assert reads == expected
    assert _written(workspace) == [_VERIFIED]
