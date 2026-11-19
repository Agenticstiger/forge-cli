# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid ship --release-dir DIR`` and the version 2 run report.

- A release is published only when every stage the run selected passed: it is
  staged beside DIR and renamed to it, with ``release.json`` written last.
- DIR must not exist, and stages 1-4 must run in the invocation; otherwise the
  run is refused (exit 2) and ``--explain`` says why.
- ``release.json``'s ``identity`` repeats across clean runs; times and the CI
  run are in ``provenance``.
- An offline release run opens no network connection and starts no process
  but ship's own stages: no provider call, package install, publish or job run.
- ``ship-report.json`` is ``fluid-ship-report/2``: six statuses, one reason,
  start and end times.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import pytest
import yaml

import fluid_build
from fluid_build.cli import _ship_release as release
from fluid_build.cli import _ship_report as report
from fluid_build.cli import _ship_stages as st
from fluid_build.cli import ship
from fluid_build.forge.core import bundle as bundle_mod

LOG = logging.getLogger("test_ship_release")
REPO_ROOT = Path(fluid_build.__file__).resolve().parent.parent
_PORTS = ("orders", "customers")


def _contract() -> Dict[str, object]:
    return {
        "fluidVersion": "0.7.5",
        "kind": "DataProduct",
        "id": "example.release",
        "name": "Release",
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
        "builds": [{"id": "external", "pattern": "hybrid-reference"}],
    }


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("FLUID_ENV", "GENERATE_EMIT", "FLUID_RUN_ID", *st.STAGE_TOGGLES.values()):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def work(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    (project / "contract.fluid.yaml").write_text(yaml.safe_dump(_contract(), sort_keys=False))
    return project


def _parse(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    ship.register(parser.add_subparsers(dest="command"))
    return parser.parse_args(["ship", *argv])


def _ship(argv: Sequence[str]) -> int:
    return ship.run(_parse(argv), LOG)


class FakeStages:
    """``subprocess.run`` for the stages: ``bundle`` copies ``bundle`` (a real one,
    since the release verifies it), ``generate artifacts`` writes one file and
    its MANIFEST; ``fail`` names a stage that exits 1."""

    def __init__(self, bundle: Optional[Path] = None, fail: str = "") -> None:
        self.bundle = bundle
        self.fail = fail
        self.calls: List[List[str]] = []

    def __call__(self, cmd: List[str], **_kw: object) -> subprocess.CompletedProcess:
        argv = cmd[3:]
        self.calls.append(argv)
        stage = argv[1] if argv[0] == "generate" else argv[0]
        if stage == "bundle":
            Path("runtime").mkdir(exist_ok=True)
            Path(st.BUNDLE_PATH).write_bytes(self.bundle.read_bytes() if self.bundle else b"x")
        elif stage == "artifacts":
            out = Path("dist/artifacts/odcs")
            out.mkdir(parents=True, exist_ok=True)
            (out / "product.odcs.orders.yaml").write_text("x")
            digest = release._sha256(out / "product.odcs.orders.yaml")
            Path(st.ARTIFACTS_MANIFEST).write_text(
                json.dumps(
                    {"digest": "sha256:m", "files": {"odcs/product.odcs.orders.yaml": digest}}
                )
            )
        elif stage == "validate":
            Path(st.VALIDATE_REPORT).write_text("{}")
        elif stage == "plan":
            bundle_digest = release._bundle_manifest(Path.cwd())["digest"] if self.bundle else ""
            plan = {"bundleDigest": bundle_digest, "planDigest": "sha256:p"}
            Path(st.PLAN_PATH).write_text(json.dumps(plan))
        return subprocess.CompletedProcess(cmd, 1 if stage == self.fail else 0)


@pytest.fixture
def bundled(work: Path) -> Path:
    """A real bundle of the contract, made once by ``fluid bundle``."""
    done = _cli(work, "bundle", "contract.fluid.yaml", "--format", "tgz", "--out", "seed.tgz")
    assert done.returncode == 0, done.stderr[-2000:]
    return work / "seed.tgz"


def _install(monkeypatch: pytest.MonkeyPatch, bundle: Path, fail: str = "") -> FakeStages:
    fake = FakeStages(bundle, fail)
    monkeypatch.setattr(ship.subprocess, "run", fake)
    return fake


_UNTIL_4 = ["--until", "validate-artifacts", "--ci-output", "plain"]


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def test_an_existing_release_dir_is_refused(
    work: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    (work / "rel").mkdir()
    fake = FakeStages()
    monkeypatch.setattr(ship.subprocess, "run", fake)
    assert _ship([*_UNTIL_4, "--release-dir", "rel"]) == 2
    assert fake.calls == [] and "already exists" in caplog.text
    assert not list(work.glob("rel.partial-*"))


@pytest.mark.parametrize(
    "argv, why",
    [
        (["--until", "validate"], "stage 3 generate-artifacts does not run (after --until 2)"),
        (["--stage", "3-6"], "stage 1 bundle does not run (not in --stage)"),
        (["--skip-stage", "validate"], "stage 2 validate does not run (--skip-stage)"),
        (["--no-generate-artifacts"], "stage 3 generate-artifacts does not run"),
    ],
)
def test_a_run_without_stages_one_to_four_is_refused_and_explained(
    work: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    argv: List[str],
    why: str,
) -> None:
    fake = FakeStages()
    monkeypatch.setattr(ship.subprocess, "run", fake)
    Path("runtime").mkdir()
    Path(st.BUNDLE_PATH).write_bytes(b"x")  # --stage 3-6 needs a bundle on disk first
    Path("dist/artifacts").mkdir(parents=True)
    assert _ship([*argv, "--release-dir", "rel"]) == 2
    assert fake.calls == [] and why in caplog.text
    capsys.readouterr()
    assert _ship([*argv, "--release-dir", "rel", "--explain"]) == 0
    assert "warning: a real run would stop: --release-dir needs stages 1-4" in (
        capsys.readouterr().out
    )
    assert not (work / "rel").exists() and not list(work.glob("rel.partial-*"))


def test_explain_names_where_the_release_goes(
    work: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _ship([*_UNTIL_4, "--release-dir", "rel", "--explain"]) == 0
    assert "release: published to " in capsys.readouterr().out
    assert not (work / "rel").exists()


# ---------------------------------------------------------------------------
# Publishing
# ---------------------------------------------------------------------------


def _strings(value: object) -> List[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    return []


@pytest.mark.parametrize("outside", [False, True], ids=["default-journal", "journal-dir"])
def test_a_release_names_its_journal_without_an_absolute_path(
    work: Path, bundled: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, outside: bool
) -> None:
    # A published release must not show the runner's layout or user name.
    _install(monkeypatch, bundled)
    flags = ["--journal-dir", str(tmp_path / "journals")] if outside else []
    assert _ship([*_UNTIL_4, "--release-dir", "../rel", *flags]) == 0
    rel = work.parent / "rel"
    index = json.loads((rel / "release.json").read_text())
    shown = json.loads((rel / "reports" / "ship-report.json").read_text())
    for block in (index["provenance"]["journal"], shown["journal"]):
        assert block["dir"] == ("--journal-dir" if outside else ".fluid/ship-journal")
        assert block["path"].endswith(".jsonl") and block["digest"].startswith("sha256:")
    for text in _strings(index["provenance"]) + _strings(shown.get("journal")):
        assert not Path(text).is_absolute(), text
        assert str(tmp_path) not in text, text


def test_a_passing_run_publishes_the_release(
    work: Path, bundled: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, bundled)
    assert _ship([*_UNTIL_4, "--release-dir", "../rel"]) == 0
    rel = work.parent / "rel"
    assert not list(work.parent.glob("rel.partial-*"))
    umask = os.umask(0)
    os.umask(umask)
    assert rel.stat().st_mode & 0o777 == 0o777 & ~umask  # readable as the umask allows
    index = json.loads((rel / "release.json").read_text())
    assert index["schema"] == release.RELEASE_SCHEMA
    assert index["identity"]["product_id"] == "example.release"
    assert index["identity"]["environment"] == "dev"
    assert index["identity"]["fluid_version"] == "0.7.5"
    assert index["identity"]["forge_version"] == fluid_build.__version__
    assert index["identity"]["emit"] == list(release._emit_set(st.ShipOptions(contract="c")))
    assert index["identity"]["artifacts"] == {"manifest_digest": "sha256:m"}
    assert "plan" not in index["identity"]  # stage 6 did not run
    assert index["identity_digest"] == release.identity_digest(index["identity"])
    assert [g["status"] for g in index["gates"]][:5] == ["passed"] * 4 + ["not-run"]
    assert set(index["provenance"]) >= {"run_id", "started_at", "ci", "stages"}
    for rel_path, digest in index["files"].items():
        assert release._sha256(rel / rel_path) == digest
    assert (rel / "bundle.tgz").read_bytes() == bundled.read_bytes()
    # The fake bundle stage writes no provenance file, so the release has none either.
    assert not (rel / f"bundle.tgz{bundle_mod.PROVENANCE_SUFFIX}").exists()
    assert (rel / "artifacts/odcs/product.odcs.orders.yaml").read_text() == "x"
    assert (rel / "reports/validate-report.json").is_file()
    shipped = json.loads((rel / "reports/ship-report.json").read_text())
    assert shipped["release"]["status"] == "published"
    assert shipped["release"]["identity_digest"] == index["identity_digest"]


def test_the_release_names_its_bundles_source_contract_from_where_it_sits(
    work: Path, bundled: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The provenance file beside runtime/bundle.tgz names the contract relative to
    runtime/. The release gets its own, relative to the release directory, so a plan
    made from the release's bundle anchors at the same contract. It is listed in
    ``files`` and stays out of ``identity``."""
    fake = _install(monkeypatch, bundled)

    def bundles_with_provenance(cmd: List[str], **kw: object) -> subprocess.CompletedProcess:
        done = fake(cmd, **kw)
        if cmd[3] == "bundle":  # as fluid bundle --out runtime/bundle.tgz writes it
            bundle_mod._write_provenance(
                Path(st.BUNDLE_PATH),
                release._bundle_manifest(Path.cwd())["digest"],
                {"contract": "../contract.fluid.yaml", "overlay": None},
            )
        return done

    monkeypatch.setattr(ship.subprocess, "run", bundles_with_provenance)
    assert _ship([*_UNTIL_4, "--release-dir", "../rel"]) == 0
    rel = work.parent / "rel"
    name = f"bundle.tgz{bundle_mod.PROVENANCE_SUFFIX}"
    index = json.loads((rel / "release.json").read_text())
    assert index["files"][name] == release._sha256(rel / name)
    assert bundle_mod.read_bundle_provenance(rel / "bundle.tgz") == {
        "contract": "../project/contract.fluid.yaml",
        "overlay": None,
    }
    contract, why = bundle_mod.resolve_bundle_source_contract(rel / "bundle.tgz")
    assert (contract, why) == ((work / "contract.fluid.yaml").resolve(), "")
    assert name not in json.dumps(index["identity"])


def test_a_report_the_checkout_held_is_never_published(
    work: Path, bundled: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A plan.html left in runtime/ (by an earlier run, or committed) is removed
    before stage 1: stage 6 writes one only with --plan-html."""
    _install(monkeypatch, bundled)
    Path("runtime").mkdir()
    Path(st.PLAN_HTML_PATH).write_text("<p>a plan nobody made</p>")
    Path("runtime/verify-report.json").write_text("{}")
    assert _ship(["--until", "plan", "--release-dir", "rel"]) == 0
    assert not Path(st.PLAN_HTML_PATH).exists()
    assert not (work / "rel/reports/plan.html").exists()
    assert not (work / "rel/reports/verify-report.json").exists()


def test_a_manifest_path_that_leads_outside_is_not_copied(work: Path) -> None:
    Path("dist/artifacts").mkdir(parents=True)
    Path(st.ARTIFACTS_MANIFEST).write_text(json.dumps({"files": {"../x": "sha256:0"}}))
    with pytest.raises(release.ReleaseError, match="not a path inside it"):
        release._copy_artifacts(work, work / "staging")


def test_a_failing_run_publishes_nothing_and_keeps_its_reports(
    work: Path, bundled: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _install(monkeypatch, bundled, fail="validate-artifacts")
    assert _ship([*_UNTIL_4, "--release-dir", "rel"]) == 1
    assert not (work / "rel").exists()
    (partial,) = work.glob("rel.partial-*")
    assert not (partial / "release.json").exists()
    assert (partial / "reports/ship-report.json").is_file()
    body = json.loads((work / "runtime/ship-report.json").read_text())
    assert body["release"]["status"] == "not-published"
    assert body["release"]["reason"] == "stage 4 validate-artifacts failed, exit 1"
    assert "no release in" in caplog.text


def test_a_tampered_bundle_publishes_nothing(
    work: Path, bundled: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install(monkeypatch, work / "tampered.tgz")
    (work / "tampered.tgz").write_bytes(b"not a bundle")
    assert _ship([*_UNTIL_4, "--release-dir", "rel"]) == 1
    assert not (work / "rel").exists()
    assert "release not published: runtime/bundle.tgz does not verify" in capsys.readouterr().out


def test_an_artifact_that_no_longer_matches_its_manifest_publishes_nothing(
    work: Path, bundled: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _install(monkeypatch, bundled)

    def edits_after(cmd: List[str], **kw: object) -> subprocess.CompletedProcess:
        done = fake(cmd, **kw)
        if cmd[3] == "validate-artifacts":
            Path("dist/artifacts/odcs/product.odcs.orders.yaml").write_text("edited")
        return done

    monkeypatch.setattr(ship.subprocess, "run", edits_after)
    assert _ship([*_UNTIL_4, "--release-dir", "rel"]) == 1
    assert not (work / "rel").exists()
    body = json.loads((work / "runtime/ship-report.json").read_text())
    assert body["release"]["status"] == "not-published"
    assert "digest its MANIFEST does not list" in body["release"]["reason"]
    (partial,) = work.glob("rel.partial-*")  # the copy kept there says the same
    kept = json.loads((partial / "reports/ship-report.json").read_text())
    assert kept["release"] == body["release"] and kept["exit_code"] == 1


def test_any_error_while_publishing_publishes_nothing_and_says_so(
    work: Path, bundled: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, bundled)

    def boom(*_a: object, **_k: object) -> None:
        raise ValueError("unexpected")

    monkeypatch.setattr(release.os, "rename", boom)
    assert _ship([*_UNTIL_4, "--release-dir", "rel"]) == 1
    assert not (work / "rel").exists()
    (partial,) = work.glob("rel.partial-*")
    assert not (partial / "release.json").exists()  # a staging dir that is no release
    for body in (
        json.loads((work / "runtime/ship-report.json").read_text()),
        json.loads((partial / "reports/ship-report.json").read_text()),
    ):
        assert body["exit_code"] == 1
        assert body["release"]["status"] == "not-published"
        assert body["release"]["reason"] == "ValueError"


def test_a_report_that_cannot_be_written_publishes_nothing(
    work: Path, bundled: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An older ship-report.json would otherwise be published as this run's."""
    _install(monkeypatch, bundled)

    def cannot_write(*_a: object, **_k: object) -> None:
        raise OSError("read-only")

    monkeypatch.setattr(report, "write_reports", cannot_write)
    assert _ship([*_UNTIL_4, "--release-dir", "rel"]) == 1
    assert not (work / "rel").exists()


def test_a_release_whose_run_applied_names_the_apply_mode(
    work: Path, bundled: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dry run that applied nothing never reads as an apply."""
    _install(monkeypatch, bundled)
    assert _ship(["--until", "apply", "--dry-run", "--yes", "--release-dir", "dry"]) == 0
    assert _ship(["--until", "apply", "--yes", "--release-dir", "amend"]) == 0
    dry, amend = (json.loads((work / d / "release.json").read_text()) for d in ("dry", "amend"))
    assert dry["identity"]["apply_mode"] == "dry-run"
    assert amend["identity"]["apply_mode"] == "amend"
    assert dry["identity_digest"] != amend["identity_digest"]
    assert _ship([*_UNTIL_4, "--release-dir", "none"]) == 0
    assert "apply_mode" not in json.loads((work / "none/release.json").read_text())["identity"]


def test_an_unreadable_earlier_report_leaves_only_the_earlier_stages_unknown(
    work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FLUID_RUN_ID", "run-1")
    Path("runtime").mkdir()
    Path(st.BUNDLE_PATH).write_bytes(b"x")
    head = {"run_id": "run-1", "env": "dev", "mode": "amend", "contract": "contract.fluid.yaml"}
    Path("runtime/ship-report.json").write_text(json.dumps({**head, "stages": [{"number": "x"}]}))
    monkeypatch.setattr(ship.subprocess, "run", FakeStages())
    assert _ship(["--stage", "diff"]) == 0
    by = {
        s["number"]: s["status"]
        for s in json.loads(Path("runtime/ship-report.json").read_text())["stages"]
    }
    assert [by[n] for n in (1, 2, 3, 4)] == ["unknown"] * 4
    assert by[5] == "passed" and by[6] == "not-run" and by[10] == "not-run"


# ---------------------------------------------------------------------------
# The version 2 run report
# ---------------------------------------------------------------------------


def test_the_report_names_its_schema_and_times_every_stage_that_ran(
    work: Path, bundled: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, bundled)
    assert _ship(["--until", "policy-apply", "--yes"]) == 0
    body = json.loads((work / "runtime/ship-report.json").read_text())
    assert body["schema"] == "fluid-ship-report/2" and body["written_at"].endswith("Z")
    by = {s["number"]: s for s in body["stages"]}
    assert by[1]["started_at"] <= by[1]["finished_at"]
    assert by[8]["status"] == "unsupported" and "no policy bindings" in by[8]["reason"]
    assert by[9]["status"] == "not-run" and by[9]["started_at"] is None
    for stage in body["stages"]:
        assert set(stage) == {
            "number",
            "name",
            "status",
            "reason",
            "argv",
            "exit_code",
            "duration_seconds",
            "started_at",
            "finished_at",
        }
        assert stage["status"] in report.STATUSES


def test_a_switch_or_skip_stage_is_skipped_by_policy(
    work: Path, bundled: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, bundled)
    monkeypatch.setenv(st.STAGE_TOGGLES[5], "false")
    assert _ship(["--until", "plan", "--skip-stage", "validate"]) == 0
    by = {
        s["number"]: s for s in json.loads(Path("runtime/ship-report.json").read_text())["stages"]
    }
    assert by[2]["status"] == "skipped-by-policy" and by[2]["reason"] == "--skip-stage"
    assert by[5]["status"] == "skipped-by-policy"
    assert by[7]["status"] == "not-run" and by[7]["reason"] == "after --until 6"


def test_a_version_one_record_merges_as_its_version_two_equivalent(
    work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FLUID_RUN_ID", "run-1")
    Path("runtime").mkdir()
    Path(st.BUNDLE_PATH).write_bytes(b"x")
    old = {
        "run_id": "run-1",
        "env": "dev",
        "mode": "amend",
        "contract": "contract.fluid.yaml",
        "exit_code": 0,
        "stages": [
            {"number": 1, "name": "bundle", "status": "passed", "argv": []},
            {"number": 3, "name": "generate-artifacts", "status": "skipped", "skip_reason": "x"},
        ],
    }
    Path("runtime/ship-report.json").write_text(json.dumps(old))
    monkeypatch.setattr(ship.subprocess, "run", FakeStages())
    assert _ship(["--stage", "validate"]) == 0
    by = {
        s["number"]: s for s in json.loads(Path("runtime/ship-report.json").read_text())["stages"]
    }
    assert by[1]["status"] == "passed"
    assert by[3]["status"] == "skipped-by-policy" and by[3]["reason"] == "x"


def test_junit_counts_unknown_as_a_failure_and_both_skips_as_skipped() -> None:
    import xml.etree.ElementTree as ET

    recs = [
        report.StageRecord(1, "bundle", status=report.UNKNOWN, reason="interrupted"),
        report.StageRecord(2, "validate", status=report.SKIPPED_BY_POLICY, reason="--skip-stage"),
        report.StageRecord(8, "policy-apply", status=report.UNSUPPORTED, reason="none"),
    ]
    suite = ET.fromstring(report.junit_xml(recs))
    assert suite.get("failures") == "1" and suite.get("skipped") == "2"
    cases = suite.findall("testcase")
    assert cases[0].find("failure").get("message") == "stage 1 bundle unknown"
    assert cases[2].find("skipped").get("message") == "unsupported: none"


# ---------------------------------------------------------------------------
# End to end: the identity repeats, and the run stays offline
# ---------------------------------------------------------------------------

_RECORDER = textwrap.dedent(
    '''
    """Test hook: record every network connection and process this interpreter starts."""
    import json, os, socket, subprocess

    _LOG = os.environ["FLUID_TEST_SIDE_EFFECTS"]

    def _note(kind, what):
        with open(_LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps([kind, what]) + "\\n")

    _note("boot", os.getpid())  # one per interpreter: ship and every stage it starts
    _connect = socket.socket.connect

    def connect(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6):
            _note("net", repr(address))
            raise OSError("network is off in this test")
        return _connect(self, address)

    socket.socket.connect = connect
    socket.socket.connect_ex = lambda self, address: connect(self, address) or 0

    def getaddrinfo(host, *args, **kwargs):
        _note("dns", repr(host))
        raise OSError("network is off in this test")

    socket.getaddrinfo = getaddrinfo
    _popen_init = subprocess.Popen.__init__

    def popen_init(self, args, *rest, **kwargs):
        _note("proc", [str(a) for a in args] if isinstance(args, (list, tuple)) else str(args))
        return _popen_init(self, args, *rest, **kwargs)

    subprocess.Popen.__init__ = popen_init
    '''
)


def _cli(cwd: Path, *args: str, hook: Optional[Path] = None) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.endswith("_API_KEY")}
    for name in ("FLUID_RUN_ID", "GENERATE_EMIT", *st.STAGE_TOGGLES.values()):
        env.pop(name, None)
    paths = [str(REPO_ROOT), env.get("PYTHONPATH", "")]
    if hook is not None:
        paths.insert(0, str(hook.parent))
        env["FLUID_TEST_SIDE_EFFECTS"] = str(hook.parent / "side-effects.jsonl")
    env["PYTHONPATH"] = os.pathsep.join(p for p in paths if p)
    env["COLUMNS"] = "200"
    return subprocess.run(
        [sys.executable, "-m", "fluid_build.cli", *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )


def _release_run(project: Path, hook: Optional[Path] = None) -> subprocess.CompletedProcess:
    return _cli(
        project,
        "ship",
        "contract.fluid.yaml",
        "--until",
        "plan",
        "--no-strict",
        "--no-exit-on-drift",
        "--ci-output",
        "plain",
        "--require-artifacts",
        "odcs,odps-bitol",
        "--release-dir",
        "rel",
        hook=hook,
    )


def _project(root: Path) -> Path:
    root.mkdir(parents=True)
    (root / "contract.fluid.yaml").write_text(yaml.safe_dump(_contract(), sort_keys=False))
    return root


@pytest.mark.integration
def test_two_clean_runs_give_the_same_release_identity(tmp_path: Path) -> None:
    """Two clean runs of one checkout. (The plan records the checkout's absolute
    paths, so a checkout elsewhere has another plan digest.)"""
    import shutil

    project = _project(tmp_path / "project")
    indexes = []
    for _ in range(2):
        for leftover in ("runtime", "dist", ".fluid", "rel"):
            shutil.rmtree(project / leftover, ignore_errors=True)
        done = _release_run(project)
        assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-3000:]
        indexes.append(json.loads((project / "rel/release.json").read_text()))
    first, second = indexes
    assert first["identity"] == second["identity"]
    assert first["identity_digest"] == second["identity_digest"]
    assert first["gates"] == second["gates"]
    assert set(first["identity"]["plan"]) == {"bundle_digest", "plan_digest"}
    assert first["provenance"]["run_id"] != second["provenance"]["run_id"]


@pytest.mark.integration
def test_an_offline_release_run_calls_no_provider_installs_nothing_and_publishes_nothing(
    tmp_path: Path,
) -> None:
    hook = tmp_path / "hook" / "sitecustomize.py"
    hook.parent.mkdir()
    hook.write_text(_RECORDER)
    project = _project(tmp_path / "project")
    done = _release_run(project, hook=hook)
    assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-3000:]
    assert (project / "rel/release.json").is_file()
    events = [
        json.loads(line)
        for line in (hook.parent / "side-effects.jsonl").read_text().splitlines()
        if line
    ]
    # the hook ran in ship and in each of its six stages, so what they did is all here
    assert len({e[1] for e in events if e[0] == "boot"}) >= 7
    assert [e for e in events if e[0] in ("net", "dns")] == []  # no connection, no lookup
    procs = [e[1] for e in events if e[0] == "proc"]
    stages = [
        p[3] if p[3] != "generate" else p[4] for p in procs if p[1:3] == ["-m", "fluid_build.cli"]
    ]
    assert stages == ["bundle", "validate", "artifacts", "validate-artifacts", "diff", "plan"]
    others = [p for p in procs if p[1:3] != ["-m", "fluid_build.cli"]]
    forbidden = ("pip", "uv", "tofu", "terraform", "dbt", "databricks", "docker", "curl", "git")
    assert [p for p in others if any(Path(str(p[0])).name.startswith(f) for f in forbidden)] == []


def _dbt_contract() -> Dict[str, object]:
    """The release contract, its ``orders`` expose built by a dbt project the bundle packs."""
    contract = _contract()
    contract["builds"] = [
        {
            "id": "transform",
            "pattern": "hybrid-reference",
            "engine": "dbt-duckdb",
            "repository": "./dbt",
            "properties": {"model": "orders"},
            "execution": {"trigger": {"type": "manual"}, "runtime": {"platform": "local"}},
            "outputs": ["orders"],
        }
    ]
    return contract


@pytest.mark.integration
def test_a_plan_made_from_the_release_applies_with_its_dbt_project(tmp_path: Path) -> None:
    """A plan made from the release's bundle records the contract the bundle was
    built from, so ``fluid apply`` finds the checkout's dbt project and checks it
    against the packed copy: an unchanged one applies (dry run), an edited one is
    refused by name. Before the release carried a provenance file, apply refused
    both, as it could not find the checkout (``apply_plan_digest_bundle_mismatch``)."""
    project = tmp_path / "project"
    (project / "dbt" / "models").mkdir(parents=True)
    (project / "dbt" / "dbt_project.yml").write_text(
        "name: orders_dbt\nversion: '1.0.0'\nprofile: orders_dbt\n"
    )
    (project / "dbt" / "models" / "orders.sql").write_text("select 1 as id\n")
    (project / "contract.fluid.yaml").write_text(yaml.safe_dump(_dbt_contract(), sort_keys=False))

    def ok(done: subprocess.CompletedProcess) -> str:
        assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-3000:]
        return done.stdout + done.stderr

    ok(
        _cli(
            project,
            "ship",
            "contract.fluid.yaml",
            "--until",
            "validate-artifacts",
            "--no-strict",
            "--ci-output",
            "plain",
            "--release-dir",
            "rel",
        )
    )
    assert "dbt project bound: transform" in ok(
        _cli(project, "plan", "rel/bundle.tgz", "--out", "plans/plan.json")
    )
    apply_argv = ("apply", "plans/plan.json", "--bundle", "rel/bundle.tgz", "--dry-run", "--yes")
    ok(_cli(project, *apply_argv))

    (project / "dbt" / "models" / "orders.sql").write_text("select 2 as id\n")
    edited = _cli(project, *apply_argv)
    assert edited.returncode == 1
    assert "changed models/orders.sql" in edited.stdout + edited.stderr
