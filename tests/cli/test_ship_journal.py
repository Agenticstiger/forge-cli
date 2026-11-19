# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The stage journal ``fluid ship --journal-dir`` keeps per release and target.

Every stage leaves ``started`` (fsynced before it runs) and ``finished`` with
its receipts. Before a stage that changes the target runs again, ship reads
the journal: an attempt with no known outcome blocks it until
``--acknowledge-inspected``, and an apply that did not pass needs a new plan.
Read-only stages always run. The stages are a fake ``subprocess.run``; the
journal, the gate and the reports are the real code.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import logging
import os
import subprocess
import tarfile
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import pytest
import yaml

from fluid_build.cli import _ship_journal as journal
from fluid_build.cli import _ship_stages as st
from fluid_build.cli import ship

LOG = logging.getLogger("test_ship_journal")
PRODUCT = "example.journal"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("FLUID_ENV", "APPLY_MODE", "FLUID_RUN_ID", *st.STAGE_TOGGLES.values()):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def work(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    contract = {"fluidVersion": "0.7.5", "kind": "DataProduct", "id": PRODUCT, "exposes": []}
    (project / "contract.fluid.yaml").write_text(yaml.safe_dump(contract))
    return project


def _bundle_bytes(seed: str = "one") -> bytes:
    """A bundle with what the journal reads: MANIFEST.json's digest and the
    resolved contract's id."""
    digest = "sha256:" + hashlib.sha256(seed.encode()).hexdigest()
    members = {
        "MANIFEST.json": json.dumps({"digest": digest}).encode(),
        "contract.resolved.yaml": yaml.safe_dump({"id": PRODUCT}).encode(),
    }
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return out.getvalue()


class FakeStages:
    """``subprocess.run`` for the stages. ``bundle`` writes a bundle named by
    ``seed``; ``plan`` writes a plan whose digest counts the plans written;
    ``outcome`` maps a stage to an exit code, or to ``"interrupt"``."""

    def __init__(self) -> None:
        self.seed = "one"
        self.outcome: Dict[str, object] = {}
        self.calls: List[str] = []
        self.plans = 0

    def __call__(self, cmd: List[str], **_kw: object) -> subprocess.CompletedProcess:
        argv = cmd[3:]
        stage = argv[1] if argv[0] == "generate" else argv[0]
        self.calls.append(stage)
        if stage == "bundle":
            Path("runtime").mkdir(exist_ok=True)
            Path(st.BUNDLE_PATH).write_bytes(_bundle_bytes(self.seed))
        elif stage == "plan":
            self.plans += 1
            plan = {"bundleDigest": "sha256:b", "planDigest": f"sha256:plan{self.plans}"}
            Path(st.PLAN_PATH).write_text(json.dumps(plan))
        elif stage == "apply":
            Path("runtime/apply-report.html").write_text("<p>applied</p>")
        outcome = self.outcome.get(stage, 0)
        if outcome == "interrupt":
            raise KeyboardInterrupt
        return subprocess.CompletedProcess(cmd, int(outcome))  # type: ignore[call-overload]


@pytest.fixture
def fake(work: Path, monkeypatch: pytest.MonkeyPatch) -> FakeStages:
    stages = FakeStages()
    monkeypatch.setattr(ship.subprocess, "run", stages)
    return stages


def _parse(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    ship.register(parser.add_subparsers(dest="command"))
    return parser.parse_args(["ship", *argv])


def _ship(*argv: str, journal_dir: Optional[str] = "jr") -> int:
    flags = ["--journal-dir", journal_dir] if journal_dir else []
    return ship.run(_parse([*argv, "--yes", "--ci-output", "plain", *flags]), LOG)


def _events(work: Path, seed: str = "one", env: str = "dev") -> List[dict]:
    events = []
    for line in _journal_file(work, seed, env).read_text().splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:  # a line a crash cut off
            continue
    return events


def _journal_file(work: Path, seed: str = "one", env: str = "dev") -> Path:
    digest = hashlib.sha256(seed.encode()).hexdigest()
    return work / "jr" / PRODUCT / env / f"{digest}.jsonl"


def _report(work: Path) -> dict:
    return json.loads((work / "runtime/ship-report.json").read_text())


def _stage(body: dict, num: int) -> dict:
    return next(s for s in body["stages"] if s["number"] == num)


_TO_APPLY = ("--stage", "bundle,plan,apply")


# ---------------------------------------------------------------------------
# What the journal records
# ---------------------------------------------------------------------------


def test_every_stage_leaves_started_and_finished_with_receipts(
    work: Path, fake: FakeStages
) -> None:
    state = work / ".fluid/iac/aws/x/terraform.tfstate"
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"lineage": "L-1", "serial": 4}))
    assert _ship(*_TO_APPLY) == 0
    events = _events(work)
    assert events[0]["event"] == "opened" and events[0]["schema"] == journal.JOURNAL_SCHEMA
    assert [(e["event"], e.get("stage")) for e in events[1:]] == [
        ("started", 1),
        ("finished", 1),
        ("started", 6),
        ("finished", 6),
        ("started", 7),
        ("finished", 7),
    ]
    plan_done = events[4]
    assert plan_done["status"] == "passed" and plan_done["exit_code"] == 0
    plan_sha = "sha256:" + hashlib.sha256(Path(st.PLAN_PATH).read_bytes()).hexdigest()
    assert plan_done["receipts"]["files"] == {st.PLAN_PATH: plan_sha}
    assert plan_done["receipts"]["plan"]["planDigest"] == "sha256:plan1"
    apply_started, apply_done = events[5], events[6]
    assert apply_started["side_effects"] is True
    assert apply_started["plan"]["planDigest"] == "sha256:plan1"  # the plan it is about to run
    assert apply_done["receipts"]["tofu_states"] == [
        {"path": ".fluid/iac/aws/x/terraform.tfstate", "lineage": "L-1", "serial": 4}
    ]
    assert "runtime/apply-report.html" in apply_done["receipts"]["files"]
    assert st.BUNDLE_PATH in events[2]["receipts"]["files"]


def test_the_report_names_the_journal(work: Path, fake: FakeStages) -> None:
    assert _ship(*_TO_APPLY) == 0
    block = _report(work)["journal"]
    assert block["path"] == str(_journal_file(work))
    assert block["digest"] == (
        "sha256:" + hashlib.sha256(_journal_file(work).read_bytes()).hexdigest()
    )


def test_without_a_journal_flag_nothing_is_kept(work: Path, fake: FakeStages) -> None:
    """The default stays as it was (Command Center runs ``fluid ship --yes``)."""
    assert _ship(*_TO_APPLY, journal_dir=None) == 0
    assert not (work / "jr").exists() and not (work / journal.DEFAULT_JOURNAL_DIR).exists()
    assert "journal" not in _report(work)


# ---------------------------------------------------------------------------
# An unknown outcome blocks until it is acknowledged
# ---------------------------------------------------------------------------


def test_an_interrupted_apply_blocks_the_next_apply(
    work: Path, fake: FakeStages, capsys: pytest.CaptureFixture[str]
) -> None:
    fake.outcome["apply"] = "interrupt"
    assert _ship(*_TO_APPLY) == 130
    assert _events(work)[-1]["status"] == "unknown"
    fake.outcome.clear()
    fake.calls.clear()
    capsys.readouterr()
    assert _ship(*_TO_APPLY) == 1
    assert "apply" not in fake.calls
    out = " ".join(capsys.readouterr().out.split())
    assert "stage 7 apply started in this release for env dev" in out
    assert "has no known outcome (attempt 1)" in out
    assert "--acknowledge-inspected apply" in out
    rec = _stage(_report(work), 7)
    assert rec["status"] == "failed" and rec["exit_code"] == 2
    assert rec["reason"].startswith("stage journal: stage 7 apply started")


def test_a_crash_with_no_finished_event_counts_as_unknown(work: Path, fake: FakeStages) -> None:
    assert _ship(*_TO_APPLY) == 0
    jr = _open(work)
    jr.started(7, ["apply", st.PLAN_PATH, "--mode", "amend"], "crashed", work)
    jr.close()  # killed: no finished event
    fake.calls.clear()
    assert _ship(*_TO_APPLY) == 1
    assert "apply" not in fake.calls


def test_acknowledging_is_recorded_and_lets_the_apply_run(
    work: Path, fake: FakeStages, capsys: pytest.CaptureFixture[str]
) -> None:
    fake.outcome["apply"] = "interrupt"
    assert _ship(*_TO_APPLY) == 130
    fake.outcome.clear()
    capsys.readouterr()
    assert _ship(*_TO_APPLY, "--acknowledge-inspected", "apply") == 0
    acks = [e for e in _events(work) if e["event"] == "acknowledged"]
    assert len(acks) == 1 and acks[0]["stage"] == 7 and acks[0]["attempt"] == 1
    assert "attempt 1 of stage 7 apply in this release was inspected" in capsys.readouterr().out
    # The acknowledgement is kept: the next run needs none.
    assert _ship(*_TO_APPLY) == 0


def test_an_acknowledged_apply_still_needs_a_new_plan(work: Path, fake: FakeStages) -> None:
    fake.outcome["apply"] = "interrupt"
    assert _ship(*_TO_APPLY) == 130
    fake.outcome.clear()
    fake.calls.clear()
    assert _ship("--stage", "apply", "--acknowledge-inspected", "7") == 1
    assert fake.calls == []
    assert "the plan it ran is stale" in _stage(_report(work), 7)["reason"]


def test_a_dry_run_apply_neither_blocks_nor_hides_an_unknown_apply(
    work: Path, fake: FakeStages
) -> None:
    fake.outcome["apply"] = "interrupt"
    assert _ship(*_TO_APPLY, "--apply-mode", "dry-run") == 130
    fake.outcome.clear()
    assert _ship(*_TO_APPLY, "--apply-mode", "dry-run") == 0  # changes nothing: runs
    assert _ship(*_TO_APPLY) == 0  # the unknown attempt was a dry run
    fake.outcome["apply"] = "interrupt"
    assert _ship(*_TO_APPLY) == 130
    fake.outcome.clear()
    assert _ship(*_TO_APPLY, "--apply-mode", "dry-run") == 0
    fake.calls.clear()
    assert _ship(*_TO_APPLY) == 1  # the dry run after it did not hide it
    assert "apply" not in fake.calls


def test_an_unknown_apply_blocks_publish_too_but_never_a_read_only_stage(
    work: Path, fake: FakeStages
) -> None:
    fake.outcome["apply"] = "interrupt"
    assert _ship(*_TO_APPLY) == 130
    fake.outcome.clear()
    fake.calls.clear()
    assert _ship("--stage", "diff,publish") == 1
    assert fake.calls == ["diff"]  # diff reads only; publish would act on the target
    assert "and stage 10 publish acts on it" in _stage(_report(work), 10)["reason"]
    assert _ship("--stage", "diff,publish", "--acknowledge-inspected", "apply") == 0


def test_an_unknown_apply_in_another_release_of_the_target_blocks_too(
    work: Path, fake: FakeStages
) -> None:
    fake.outcome["apply"] = "interrupt"
    assert _ship(*_TO_APPLY) == 130
    fake.outcome.clear()
    fake.seed = "two"  # an edited contract: another release, the same product and env
    fake.calls.clear()
    assert _ship(*_TO_APPLY) == 1
    assert "apply" not in fake.calls
    assert f"in release {hashlib.sha256(b'one').hexdigest()[:12]}" in (
        _stage(_report(work), 7)["reason"]
    )
    assert _ship(*_TO_APPLY, "--acknowledge-inspected", "apply") == 0
    # The acknowledgement went into the release that holds the attempt.
    assert any(e["event"] == "acknowledged" for e in _events(work, "one"))
    # Another env is another target.
    fake.outcome["apply"] = "interrupt"
    assert _ship(*_TO_APPLY) == 130
    fake.outcome.clear()
    assert _ship(*_TO_APPLY, "--env", "prod") == 0


# ---------------------------------------------------------------------------
# A failed apply needs a new plan
# ---------------------------------------------------------------------------


def test_a_failed_apply_reruns_only_after_a_new_plan(work: Path, fake: FakeStages) -> None:
    fake.outcome["apply"] = 1
    assert _ship(*_TO_APPLY) == 1
    fake.outcome.clear()
    fake.calls.clear()
    assert _ship("--stage", "apply") == 1
    assert fake.calls == []
    reason = _stage(_report(work), 7)["reason"]
    assert "stage 7 apply failed in attempt 1" in reason and "--stage plan-apply" in reason
    assert _ship("--stage", "plan,apply") == 0
    assert fake.calls == ["plan", "apply"]


def test_only_the_plan_ship_wrote_since_counts_as_new(work: Path, fake: FakeStages) -> None:
    """A plan written outside ship, or an older plan put back after the new one,
    is not a plan the journal saw made against the target as it is now."""
    fake.outcome["apply"] = 1
    assert _ship(*_TO_APPLY) == 1
    fake.outcome.clear()
    Path(st.PLAN_PATH).write_text(json.dumps({"planDigest": "sha256:other"}))
    assert _ship("--stage", "apply") == 1
    old = Path(st.PLAN_PATH).read_text()
    assert _ship("--stage", "plan") == 0
    Path(st.PLAN_PATH).write_text(old)
    assert _ship("--stage", "apply") == 1
    assert "runtime/plan.json is not the plan stage 6 wrote since" in (
        _stage(_report(work), 7)["reason"]
    )
    assert _ship("--stage", "plan,apply") == 0


def test_a_stage_killed_by_a_signal_has_an_unknown_outcome(
    work: Path, fake: FakeStages, caplog: pytest.LogCaptureFixture
) -> None:
    fake.outcome["publish"] = -9  # the OOM killer
    assert _ship("--stage", "bundle,publish") == 1
    rec = _stage(_report(work), 10)
    assert rec["status"] == "unknown" and rec["reason"] == "killed by signal 9"
    assert rec["exit_code"] == 1
    assert "(exit 1, killed by signal)" in caplog.text
    assert _events(work)[-1]["status"] == "unknown"
    fake.outcome.clear()
    fake.calls.clear()
    assert _ship("--stage", "bundle,publish") == 1
    assert fake.calls == ["bundle"]
    assert _ship("--stage", "bundle,publish", "--acknowledge-inspected", "publish") == 0


def test_the_journal_records_argv_with_credentials_masked(work: Path) -> None:
    jr = _open(work)
    try:
        raw = ["schedule-sync", "--client-secret", "s3cr3t-value", "--dry-run"]
        jr.started(11, raw, "r", work, shown=ship._safe_argv(raw))
    finally:
        jr.close()
    text = _journal_file(work).read_text()
    assert "s3cr3t-value" not in text and "--client-secret" in text


def test_a_failed_publish_reruns(work: Path, fake: FakeStages) -> None:
    """A failure with a known outcome elsewhere than apply simply runs again."""
    fake.outcome["publish"] = 1
    assert _ship("--stage", "bundle,publish") == 1
    fake.outcome.clear()
    assert _ship("--stage", "bundle,publish") == 0


# ---------------------------------------------------------------------------
# The file itself: torn lines, the lock
# ---------------------------------------------------------------------------


def test_a_torn_last_line_is_ignored_and_the_next_event_starts_its_own(
    work: Path, fake: FakeStages
) -> None:
    assert _ship(*_TO_APPLY) == 0
    path = _journal_file(work)
    with path.open("ab") as fh:
        fh.write(b'{"event": "started", "stage": 7, "attempt": 2')  # a crash mid-write
    jr = _open(work)
    assert len(jr.attempts[7]) == 1  # the torn line never committed an attempt
    jr.close()
    assert _ship(*_TO_APPLY) == 0
    lines = path.read_bytes().split(b"\n")
    assert lines[-1] == b""  # the file ends on a whole line
    unreadable = []
    for line in lines[:-1]:
        try:
            json.loads(line)
        except ValueError:
            unreadable.append(line)
    assert unreadable == [b'{"event": "started", "stage": 7, "attempt": 2']
    assert [a.outcome for a in _read(work).attempts[7]] == ["passed", "passed"]


def test_one_run_at_a_time_holds_a_target(
    work: Path, fake: FakeStages, caplog: pytest.LogCaptureFixture
) -> None:
    assert _ship(*_TO_APPLY) == 0
    held = _open(work)
    try:
        with pytest.raises(journal.JournalError, match="another fluid ship run holds"):
            _open(work)
        fake.calls.clear()
        assert _ship(*_TO_APPLY) == 2  # refused before stage 1 rebundles anything
        assert fake.calls == []
        assert "another fluid ship run holds" in caplog.text
    finally:
        held.close()
    assert _ship(*_TO_APPLY) == 0


def test_a_journal_dir_that_cannot_be_made_is_refused_up_front(
    work: Path, fake: FakeStages, caplog: pytest.LogCaptureFixture
) -> None:
    Path("jr").write_text("a file")
    assert _ship(*_TO_APPLY) == 2
    assert fake.calls == [] and "cannot create the stage journal directory" in caplog.text


def test_no_link_below_the_journal_directory_is_followed(
    work: Path, fake: FakeStages, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (work / "jr").mkdir()
    (work / "jr" / PRODUCT).symlink_to(outside, target_is_directory=True)
    assert _ship(*_TO_APPLY) == 2
    assert "is not a directory (a symbolic link is never followed" in caplog.text
    assert fake.calls == [] and list(outside.iterdir()) == []


def test_the_default_journal_in_the_checkout_follows_no_link(
    work: Path, fake: FakeStages, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    outside = tmp_path / "planted"
    outside.mkdir()
    (work / ".fluid").mkdir()
    (work / ".fluid" / "ship-journal").symlink_to(outside, target_is_directory=True)
    assert ship.run(_parse(["--release-dir", "rel", "--yes"]), LOG) == 2
    assert "is not a directory" in caplog.text
    assert fake.calls == [] and list(outside.iterdir()) == []


def _refused_apply(work: Path) -> str:
    """Stages 1 and 6 change nothing, so they run unrecorded; apply is refused."""
    rec = _stage(_report(work), 7)
    assert rec["status"] == "failed" and rec["exit_code"] == 2
    return str(rec["reason"])


def test_a_journal_file_that_is_a_link_is_refused(
    work: Path, fake: FakeStages, tmp_path: Path
) -> None:
    victim = tmp_path / "victim_rc"  # a dangling link: it must not be created
    _journal_file(work).parent.mkdir(parents=True)
    _journal_file(work).symlink_to(victim)
    assert _ship(*_TO_APPLY) == 1
    assert fake.calls == ["bundle", "plan"]
    assert "is a symbolic link" in _refused_apply(work)
    assert not victim.exists()


def test_a_hard_link_and_an_oversized_file_are_refused(
    work: Path, fake: FakeStages, tmp_path: Path
) -> None:
    assert _ship(*_TO_APPLY) == 0
    other = _journal_file(work, "two")
    elsewhere = tmp_path / "elsewhere.jsonl"
    elsewhere.write_text("")
    os.link(elsewhere, other)
    assert _ship(*_TO_APPLY) == 1
    assert "is not a regular file ship wrote" in _refused_apply(work)
    other.unlink()
    with other.open("wb") as fh:
        fh.truncate(journal.MAX_FILE_BYTES + 1)
    assert _ship(*_TO_APPLY) == 1
    assert "more than a stage journal ship writes" in _refused_apply(work)


def test_a_stage_that_changes_nothing_runs_without_a_bundle(
    work: Path, fake: FakeStages, capsys: pytest.CaptureFixture[str]
) -> None:
    Path(st.POLICY_BINDINGS).parent.mkdir(parents=True)
    Path(st.POLICY_BINDINGS).write_text("{}")
    assert _ship("--stage", "policy-apply", "--policy-mode", "check") == 0
    assert fake.calls == ["policy-apply"]
    assert "runs without a journal entry" in capsys.readouterr().out
    assert _ship("--stage", "policy-apply") == 1  # enforce changes the target
    assert fake.calls == ["policy-apply"]
    assert "no readable runtime/bundle.tgz" in _stage(_report(work), 8)["reason"]


def test_a_truncated_bundle_is_refused_without_a_traceback(
    work: Path, fake: FakeStages, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _ship(*_TO_APPLY) == 0
    data = Path(st.BUNDLE_PATH).read_bytes()
    Path(st.BUNDLE_PATH).write_bytes(data[: len(data) // 2])
    assert journal.bundle_digest(work) is None
    assert _ship("--stage", "apply") == 1
    assert "no readable runtime/bundle.tgz" in _refused_apply(work)
    assert _ship("--stage", "apply", "--explain") == 0
    assert "the release is named once stage 1 has bundled" in capsys.readouterr().out


def test_an_overlay_that_renames_the_product_journals_under_the_bundled_id(
    work: Path, fake: FakeStages
) -> None:
    """The contract's file says one id and the bundle (with its env overlay)
    another: the run holds both targets and journals where the bundle says."""
    body = yaml.safe_load(Path("contract.fluid.yaml").read_text())
    body["id"] = "example.base"
    Path("contract.fluid.yaml").write_text(yaml.safe_dump(body))
    fake.outcome["apply"] = "interrupt"
    assert _ship(*_TO_APPLY) == 130
    fake.outcome.clear()
    assert _journal_file(work).is_file()  # under the bundled id
    assert (work / "jr" / "example.base" / "dev" / journal.LOCK_NAME).is_file()
    fake.calls.clear()
    assert _ship(*_TO_APPLY) == 1  # the unknown apply is found again
    assert "apply" not in fake.calls
    assert _ship(*_TO_APPLY, "--acknowledge-inspected", "apply") == 0


def test_a_contract_id_ship_cannot_read_holds_the_shared_target(work: Path) -> None:
    Path("bad.yaml").write_text("id: x\ncreated: 2026-02-30\n")  # not a date
    assert ship._contract_id("bad.yaml") == "product"
    Path("tree.yaml").write_text("id: {a: 1}\n")
    assert ship._contract_id("tree.yaml") == "product"
    assert ship._contract_id("contract.fluid.yaml") == PRODUCT


def test_a_child_that_exits_as_a_signal_would_has_an_unknown_outcome(
    work: Path, fake: FakeStages
) -> None:
    """``fluid apply`` turns Ctrl-C into exit 130."""
    fake.outcome["apply"] = 130
    assert _ship(*_TO_APPLY) == 1
    rec = _stage(_report(work), 7)
    assert rec["status"] == "unknown" and rec["reason"] == "killed by signal 2 (it exited 130)"
    fake.outcome.clear()
    fake.calls.clear()
    assert _ship(*_TO_APPLY) == 1
    assert "apply" not in fake.calls


def test_a_planted_temporary_name_cannot_keep_an_old_report(work: Path, fake: FakeStages) -> None:
    Path("runtime").mkdir()
    Path("runtime/ship-report.json").write_text('{"planted": true}')
    for pid in range(os.getpid() - 2, os.getpid() + 3):
        Path(f"runtime/.ship-report.json.{pid}.tmp").write_text("x")
    assert _ship(*_TO_APPLY, journal_dir=None) == 0
    assert "planted" not in _report(work)


def test_an_acknowledgement_in_another_release_survives_its_torn_tail(
    work: Path, fake: FakeStages, capsys: pytest.CaptureFixture[str]
) -> None:
    fake.outcome["apply"] = "interrupt"
    assert _ship(*_TO_APPLY) == 130
    fake.outcome.clear()
    with _journal_file(work).open("ab") as fh:
        fh.write(b'{"event": "fini')  # a crash mid-write in release one
    fake.seed = "two"
    capsys.readouterr()
    assert _ship(*_TO_APPLY, "--acknowledge-inspected", "apply") == 0
    assert f"in release {hashlib.sha256(b'one').hexdigest()[:12]} was inspected" in (
        capsys.readouterr().out
    )
    assert any(e["event"] == "acknowledged" for e in _events(work, "one"))
    assert _ship(*_TO_APPLY) == 0  # the acknowledgement was kept


def test_the_run_report_replaces_a_planted_link(
    work: Path, fake: FakeStages, tmp_path: Path
) -> None:
    victim = tmp_path / "victim.json"
    victim.write_text("keep me")
    Path("runtime").mkdir()
    Path("runtime/ship-report.json").symlink_to(victim)
    assert _ship(*_TO_APPLY, journal_dir=None) == 0
    assert victim.read_text() == "keep me"
    assert not Path("runtime/ship-report.json").is_symlink()


def test_the_journal_file_is_private(work: Path, fake: FakeStages) -> None:
    assert _ship(*_TO_APPLY) == 0
    assert _journal_file(work).stat().st_mode & 0o077 == 0


def _read(work: Path, seed: str = "one") -> journal.Journal:
    return journal.Journal.read(
        work / "jr",
        product_id=PRODUCT,
        environment="dev",
        digest="sha256:" + hashlib.sha256(seed.encode()).hexdigest(),
    )


def _open(work: Path, seed: str = "one") -> journal.Journal:
    return journal.Journal.open(
        work / "jr",
        product_id=PRODUCT,
        environment="dev",
        digest="sha256:" + hashlib.sha256(seed.encode()).hexdigest(),
        run_id="test",
    )


# ---------------------------------------------------------------------------
# Flags and --explain
# ---------------------------------------------------------------------------


def test_acknowledging_needs_a_journal_and_a_stage_that_changes_the_target(
    work: Path, fake: FakeStages, caplog: pytest.LogCaptureFixture
) -> None:
    assert _ship(*_TO_APPLY, "--acknowledge-inspected", "apply", journal_dir=None) == 2
    assert "needs a stage journal" in caplog.text
    assert _ship(*_TO_APPLY, "--acknowledge-inspected", "verify") == 2
    assert "9 verify changes nothing in the target" in caplog.text
    assert _ship(*_TO_APPLY, "--acknowledge-inspected", "nope") == 2
    assert fake.calls == []


def test_explain_shows_the_journal_and_what_it_would_refuse(
    work: Path, fake: FakeStages, capsys: pytest.CaptureFixture[str]
) -> None:
    fake.outcome["apply"] = "interrupt"
    assert _ship(*_TO_APPLY) == 130
    before = _journal_file(work).read_bytes()
    capsys.readouterr()
    assert _ship("--stage", "apply", "--explain") == 0
    out = capsys.readouterr().out
    assert f"stage journal: {_journal_file(work)}" in out
    assert "refused by the stage journal: stage 7 apply started in this release" in out
    assert "warning: a real run would stop: stage journal:" in out
    assert _journal_file(work).read_bytes() == before  # explain writes nothing


def test_explain_knows_stage_six_plans_again_before_apply(
    work: Path, fake: FakeStages, capsys: pytest.CaptureFixture[str]
) -> None:
    fake.outcome["apply"] = 1
    assert _ship(*_TO_APPLY) == 1
    fake.outcome.clear()
    capsys.readouterr()
    assert _ship("--stage", "plan,apply", "--explain") == 0
    assert "refused by the stage journal" not in capsys.readouterr().out
    assert _ship("--stage", "apply", "--explain") == 0
    assert "the plan it ran is stale" in capsys.readouterr().out
    assert _ship("--stage", "plan,apply") == 0


def test_the_abort_line_says_how_to_go_on_after_a_refusal(
    work: Path, fake: FakeStages, caplog: pytest.LogCaptureFixture
) -> None:
    fake.outcome["apply"] = "interrupt"
    assert _ship(*_TO_APPLY) == 130
    fake.outcome.clear()
    caplog.clear()
    assert _ship(*_TO_APPLY) == 1
    assert "(exit 2, refused by the stage journal)" in caplog.text
    assert "pass --acknowledge-inspected, or plan again), then run again" in caplog.text


def test_a_release_keeps_its_journal_in_the_project(
    work: Path, fake: FakeStages, capsys: pytest.CaptureFixture[str]
) -> None:
    assert ship.run(_parse(["--release-dir", "rel", "--explain"]), LOG) == 0
    assert f"stage journal: {work / journal.DEFAULT_JOURNAL_DIR}" in capsys.readouterr().out


def test_ids_that_differ_only_in_case_have_their_own_targets() -> None:
    assert journal.target_stem("orders") == "orders"
    upper = journal.target_stem("Orders")
    assert upper.startswith("Orders-") and upper.lower() != "orders"


def test_side_effects_by_stage_and_mode() -> None:
    se = journal.side_effects
    assert se(7, ["apply", "p", "--mode", "amend"]) and not se(7, ["apply", "--mode", "dry-run"])
    assert se(8, ["policy-apply", "b", "--mode", "enforce"])
    assert not se(8, ["policy-apply", "b", "--mode", "check"])
    assert se(10, ["publish"]) and se(11, ["schedule-sync"])
    assert not se(11, ["schedule-sync", "--dry-run"])
    assert not any(se(n, ["x"]) for n in (1, 2, 3, 4, 5, 6, 9))
