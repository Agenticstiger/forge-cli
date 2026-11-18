# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid ship`` fails closed on the artifacts a run promised.

- Stage 3 runs for a reference-only contract (the transformation lives
  elsewhere; its ODCS, ODPS and policy artifacts do not), and stage 4 fails
  instead of skipping when stage 3 was to write ``dist/artifacts/`` and did not.
- ``--require-artifacts KEYS``: stage 3 fails unless this run's MANIFEST lists
  each key's files and the files on disk carry the listed digests. A switch
  that keeps stage 3 from running, or an ``--emit`` without the key, is
  refused (exit 2) and shown by ``--explain``.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import logging
import os
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import pytest
import yaml

import fluid_build
from fluid_build.cli import _ship_stages as st
from fluid_build.cli import ship

LOG = logging.getLogger("test_ship_required_artifacts")
REPO_ROOT = Path(fluid_build.__file__).resolve().parent.parent

_PRODUCT_ID = "example.ship_gate"
_PORTS = ("orders", "customers")


def _contract(*, reference_only: bool = True) -> Dict[str, object]:
    contract: Dict[str, object] = {
        "fluidVersion": "0.7.5",
        "kind": "DataProduct",
        "id": _PRODUCT_ID,
        "name": "Ship gate",
        "domain": "example",
        "metadata": {"layer": "Bronze", "owner": {"team": "data-platform"}},
        "exposes": [
            {
                "exposeId": port,
                "kind": "table",
                "binding": {
                    "platform": "local",
                    "format": "csv",
                    "location": {"path": f"out/{port}.csv"},
                },
                "contract": {"schema": [{"name": "id", "type": "string"}]},
            }
            for port in _PORTS
        ],
    }
    if reference_only:
        # An external dbt / Airflow project owns the tables.
        contract["builds"] = [{"id": "external", "pattern": "hybrid-reference"}]
    return contract


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("FLUID_ENV", "GENERATE_EMIT", "FLUID_RUN_ID", *st.STAGE_TOGGLES.values()):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def work(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "contract.fluid.yaml").write_text(yaml.safe_dump(_contract(), sort_keys=False))
    return tmp_path


def _parse(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    ship.register(parser.add_subparsers(dest="command"))
    return parser.parse_args(["ship", *argv])


def _ship(argv: Sequence[str]) -> int:
    return ship.run(_parse(argv), LOG)


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


class FakeStages:
    """Stands in for ``subprocess.run``: a real bundle (stage 3's check reads
    its ports) and a ``generate artifacts`` that writes ``outputs`` (path under
    dist/artifacts/ -> bytes) with a MANIFEST listing ``listed`` of them."""

    def __init__(self, outputs: Dict[str, bytes], listed: Optional[Sequence[str]] = None) -> None:
        self.outputs = outputs
        self.listed = list(outputs) if listed is None else list(listed)
        self.calls: List[List[str]] = []

    def __call__(self, cmd: List[str], **_kw: object) -> subprocess.CompletedProcess:
        argv = cmd[3:]
        self.calls.append(argv)
        if argv[0] == "bundle":
            _write_bundle(Path("runtime/bundle.tgz"))
        elif argv[:2] == ["generate", "artifacts"]:
            out = Path("dist/artifacts")
            out.mkdir(parents=True, exist_ok=True)
            for rel, data in self.outputs.items():
                (out / rel).parent.mkdir(parents=True, exist_ok=True)
                (out / rel).write_bytes(data)
            files = {rel: _sha(self.outputs[rel]) for rel in self.listed}
            (out / "MANIFEST.json").write_text(json.dumps({"files": files}))
        return subprocess.CompletedProcess(cmd, 0)

    @property
    def stages(self) -> List[str]:
        return [c[1] if c[0] == "generate" else c[0] for c in self.calls]


def _write_bundle(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = yaml.safe_dump(_contract()).encode("utf-8")
    with tarfile.open(path, "w:gz") as tar:
        info = tarfile.TarInfo("contract.resolved.yaml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))


_ALL_OUTPUTS = {
    "odcs/product.odcs.orders.yaml": b"orders",
    "odcs/product.odcs.customers.yaml": b"customers",
    f"odps-bitol/{_PRODUCT_ID}.odps.yaml": b"product",
    f"odps-bitol/{_PRODUCT_ID}.orders.odcs.yaml": b"orders",
}


def _install(monkeypatch: pytest.MonkeyPatch, fake: FakeStages) -> FakeStages:
    monkeypatch.setattr(ship.subprocess, "run", fake)
    return fake


def _stage3(work: Path) -> Dict[str, object]:
    return json.loads((work / "runtime/ship-report.json").read_text())["stages"][2]


_REQUIRE = ["--until", "validate-artifacts", "--emit", "odcs,odps-bitol"]
_KEYS = ["--require-artifacts", "odcs,odps-bitol"]


# ---------------------------------------------------------------------------
# The default: stage 3 runs for a reference-only contract
# ---------------------------------------------------------------------------


def test_stage_three_runs_for_a_reference_only_contract(work: Path) -> None:
    opts = st.resolve_options(_parse(["--emit", "odcs,odps-bitol"]), "contract.fluid.yaml", {})
    action = st.build_stage(3, opts, st.Workspace(work))
    assert action.argv is not None and action.argv[:2] == ["generate", "artifacts"]
    assert action.argv[action.argv.index("--emit") + 1] == "odcs,odps-bitol"


def test_stage_four_fails_when_stage_three_was_to_write_and_did_not(
    work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _install(monkeypatch, FakeStages({}))
    fake_generate = fake.__call__

    def writes_nothing(cmd: List[str], **kw: object) -> subprocess.CompletedProcess:
        if cmd[3:5] == ["generate", "artifacts"]:
            fake.calls.append(cmd[3:])
            return subprocess.CompletedProcess(cmd, 0)
        return fake_generate(cmd, **kw)

    monkeypatch.setattr(ship.subprocess, "run", writes_nothing)
    assert _ship(["--until", "validate-artifacts"]) == 1
    stage4 = json.loads((work / "runtime/ship-report.json").read_text())["stages"][3]
    assert stage4["status"] == "failed"
    assert fake.stages == ["bundle", "validate", "artifacts"]


def test_stage_four_still_skips_when_generation_is_switched_off(
    work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, FakeStages({}))
    assert _ship(["--until", "validate-artifacts", "--no-generate-artifacts"]) == 0
    stage4 = json.loads((work / "runtime/ship-report.json").read_text())["stages"][3]
    assert stage4["status"] == "skipped" and "--no-generate-artifacts" in stage4["skip_reason"]


# ---------------------------------------------------------------------------
# --require-artifacts
# ---------------------------------------------------------------------------


def test_every_required_artifact_written_passes(
    work: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _install(monkeypatch, FakeStages(dict(_ALL_OUTPUTS)))
    assert _ship([*_REQUIRE, *_KEYS, "--ci-output", "plain"]) == 0
    assert fake.stages == ["bundle", "validate", "artifacts", "validate-artifacts"]
    assert _stage3(work)["status"] == "passed"
    assert "wrote every required artifact (odps-bitol,odcs)" in capsys.readouterr().out


def test_a_port_without_its_odcs_fails_and_names_the_port(
    work: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    outputs = dict(_ALL_OUTPUTS)
    del outputs["odcs/product.odcs.customers.yaml"]
    fake = _install(monkeypatch, FakeStages(outputs))
    assert _ship([*_REQUIRE, *_KEYS, "--ci-output", "plain"]) == 1
    assert fake.stages == ["bundle", "validate", "artifacts"]  # stage 4 never runs
    stage3 = _stage3(work)
    assert stage3["status"] == "failed" and stage3["exit_code"] == 0
    reason = str(stage3["failure_reason"])
    assert "port 'customers' has no ODCS" in reason and "'orders'" not in reason
    assert "required artifact missing: odcs: port 'customers'" in capsys.readouterr().out
    assert "it exited 0, but --require-artifacts was not met" in caplog.text
    junit = (work / "runtime/ship-junit.xml").read_text()
    assert "port 'customers' has no ODCS" in junit


def test_a_missing_odps_product_fails(work: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    outputs = {k: v for k, v in _ALL_OUTPUTS.items() if not k.endswith(".odps.yaml")}
    _install(monkeypatch, FakeStages(outputs))
    assert _ship([*_REQUIRE, *_KEYS]) == 1
    assert "odps-bitol: stage 3 wrote no ODPS product" in str(_stage3(work)["failure_reason"])


def test_a_file_the_manifest_does_not_list_does_not_count(
    work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    listed = [k for k in _ALL_OUTPUTS if k != "odcs/product.odcs.orders.yaml"]
    _install(monkeypatch, FakeStages(dict(_ALL_OUTPUTS), listed=listed))
    assert _ship([*_REQUIRE, *_KEYS]) == 1
    assert "port 'orders' has no ODCS" in str(_stage3(work)["failure_reason"])
    assert "not in its MANIFEST" in str(_stage3(work)["failure_reason"])


def test_a_file_whose_digest_changed_does_not_count(
    work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeStages(dict(_ALL_OUTPUTS))

    def tampers(cmd: List[str], **kw: object) -> subprocess.CompletedProcess:
        done = fake(cmd, **kw)
        if cmd[3:5] == ["generate", "artifacts"]:
            Path("dist/artifacts/odcs/product.odcs.orders.yaml").write_bytes(b"edited")
        return done

    monkeypatch.setattr(ship.subprocess, "run", tampers)
    assert _ship([*_REQUIRE, *_KEYS]) == 1
    assert "digest its MANIFEST does not list" in str(_stage3(work)["failure_reason"])


def test_a_manifest_key_that_leads_out_of_dist_artifacts_does_not_count(work: Path) -> None:
    """The MANIFEST is the checkout's: ``schedule/../..`` names nothing stage 3 wrote."""
    (work / "outside.txt").write_bytes(b"x")
    manifest = work / st.ARTIFACTS_MANIFEST
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"files": {"schedule/../../outside.txt": _sha(b"x")}}))
    opts = st.ShipOptions(contract="contract.fluid.yaml", require_artifacts=("schedule",))
    assert st.missing_required_artifacts(opts, work) == [
        "schedule: dist/artifacts/schedule/../../outside.txt outside dist/artifacts/"
    ]


def test_output_an_earlier_run_left_never_counts(
    work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An earlier run wrote every artifact; this run's stage 3 exits 0 and writes nothing.
    _install(monkeypatch, FakeStages(dict(_ALL_OUTPUTS)))
    assert _ship([*_REQUIRE, *_KEYS]) == 0
    stale = FakeStages({})

    def writes_nothing(cmd: List[str], **kw: object) -> subprocess.CompletedProcess:
        if cmd[3:5] == ["generate", "artifacts"]:
            return subprocess.CompletedProcess(cmd, 0)
        return stale(cmd, **kw)

    monkeypatch.setattr(ship.subprocess, "run", writes_nothing)
    assert _ship([*_REQUIRE, *_KEYS]) == 1
    assert "stage 3 wrote no dist/artifacts/MANIFEST.json" in str(_stage3(work)["failure_reason"])


def test_a_step_that_runs_stage_three_by_number_is_checked_too(
    work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One CI step per stage: the step that runs stage 3 carries the requirement."""
    outputs = dict(_ALL_OUTPUTS)
    del outputs["odcs/product.odcs.orders.yaml"]
    _install(monkeypatch, FakeStages(outputs))
    assert _ship(["--stage", "bundle,artifacts", *_KEYS]) == 1


@pytest.mark.parametrize(
    "argv, expected",
    [
        (["--until", "validate"], "after --until 2"),
        (["--skip-stage", "artifacts"], "--skip-stage"),
        (["--stage", "4"], "not in --stage"),
        (["--no-generate-artifacts"], "--no-generate-artifacts"),
    ],
)
def test_a_switch_that_keeps_stage_three_from_running_is_refused(
    work: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    argv: List[str],
    expected: str,
) -> None:
    fake = _install(monkeypatch, FakeStages(dict(_ALL_OUTPUTS)))
    # Stage 4 alone needs dist/artifacts/ before the requirement is even read.
    Path("dist/artifacts").mkdir(parents=True)
    assert _ship([*argv, *_KEYS]) == 2
    assert fake.calls == []
    assert "needs stage 3 generate-artifacts in this invocation" in caplog.text
    assert expected in caplog.text
    capsys.readouterr()
    # --explain shows the same refusal and runs nothing.
    assert _ship([*argv, *_KEYS, "--explain"]) == 0
    out = capsys.readouterr().out
    assert "warning: a real run would stop: --require-artifacts" in out and expected in out
    assert fake.calls == []


def test_a_run_stage_switch_that_turns_stage_three_off_is_refused(
    work: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    fake = _install(monkeypatch, FakeStages(dict(_ALL_OUTPUTS)))
    monkeypatch.setenv(st.STAGE_TOGGLES[3], "false")
    assert _ship(["--until", "artifacts", *_KEYS]) == 2
    assert fake.calls == []
    assert f"{st.STAGE_TOGGLES[3]}=false" in caplog.text


def test_a_required_key_left_out_of_emit_is_refused(
    work: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    fake = _install(monkeypatch, FakeStages(dict(_ALL_OUTPUTS)))
    assert _ship(["--emit", "odcs", *_KEYS]) == 2
    assert fake.calls == []
    assert "--require-artifacts odps-bitol is not in --emit odcs" in caplog.text
    # GENERATE_EMIT is --emit's variable and counts the same.
    monkeypatch.setenv("GENERATE_EMIT", "odps-bitol")
    assert _ship(_KEYS) == 2


def test_emit_odps_counts_as_its_canonical_opds(work: Path) -> None:
    opts = st.resolve_options(
        _parse(["--emit", "odps", "--require-artifacts", "opds"]), "contract.fluid.yaml", {}
    )
    st.check_required_artifacts([3], {}, opts)  # no refusal


@pytest.mark.parametrize("value", ["", " , ", "odcs,dbt", "odps-standard"])
def test_an_unknown_or_empty_key_is_refused(work: Path, value: str) -> None:
    with pytest.raises(st.ShipUsageError):
        st.resolve_options(_parse(["--require-artifacts", value]), "contract.fluid.yaml", {})


def test_keys_are_kept_in_canonical_order_without_duplicates(work: Path) -> None:
    opts = st.resolve_options(
        _parse(["--require-artifacts", "policies, odcs,odcs,odps-bitol"]), "contract.fluid.yaml", {}
    )
    assert opts.require_artifacts == ("odps-bitol", "odcs", "policies")


def test_without_the_flag_stage_three_keeps_an_earlier_manifest(
    work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a run that asks removes the earlier MANIFEST before stage 3."""
    manifest = work / st.ARTIFACTS_MANIFEST
    manifest.parent.mkdir(parents=True)
    manifest.write_text("{}")
    seen: List[bool] = []

    def records(cmd: List[str], **kw: object) -> subprocess.CompletedProcess:
        if cmd[3:5] == ["generate", "artifacts"]:
            seen.append(manifest.exists())
            return subprocess.CompletedProcess(cmd, 0)
        return FakeStages({})(cmd, **kw)

    monkeypatch.setattr(ship.subprocess, "run", records)
    assert _ship(["--until", "artifacts"]) == 0
    assert _ship(["--until", "artifacts", "--require-artifacts", "odcs"]) == 1
    assert seen == [True, False]


# ---------------------------------------------------------------------------
# End to end: the real CLI on a two-port reference-only contract
# ---------------------------------------------------------------------------


def _cli(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.endswith("_API_KEY")}
    for name in ("FLUID_RUN_ID", "GENERATE_EMIT", *st.STAGE_TOGGLES.values()):
        env.pop(name, None)
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env["COLUMNS"] = "200"
    return subprocess.run(
        [sys.executable, "-m", "fluid_build.cli", *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )


@pytest.mark.integration
def test_the_real_pipeline_writes_both_odcs_files_and_the_odps_product(tmp_path: Path) -> None:
    (tmp_path / "contract.fluid.yaml").write_text(yaml.safe_dump(_contract(), sort_keys=False))
    done = _cli(
        tmp_path,
        "ship",
        "contract.fluid.yaml",
        "--env",
        "dev",
        "--no-strict",
        "--ci-output",
        "plain",
        *_REQUIRE,
        *_KEYS,
    )
    assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-3000:]
    out = tmp_path / "dist/artifacts"
    assert (out / "MANIFEST.json").is_file()
    for port in _PORTS:
        assert (out / f"odcs/product.odcs.{port}.yaml").is_file()
    assert (out / f"odps-bitol/{_PRODUCT_ID}.odps.yaml").is_file()
    stages = json.loads((tmp_path / "runtime/ship-report.json").read_text())["stages"]
    assert [s["status"] for s in stages[:4]] == ["passed"] * 4

    # The same run asking for an artifact --emit leaves out is refused before stage 1.
    refused = _cli(tmp_path, "ship", "contract.fluid.yaml", "--emit", "odcs", *_KEYS)
    assert refused.returncode == 2
    assert "is not in --emit odcs" in refused.stdout + refused.stderr
