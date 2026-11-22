# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""When the pushed DAG files are live in Airflow: what ``schedule-sync --airflow-url`` waits for.

A fake Airflow on 127.0.0.1 plays the server; :class:`Processor` plays its Dag processor,
one step per poll interval of the check, on the fake's simulated clock. The rules under
test, each measured on real Airflow 2.10.5 and 3.4.0 before it was written down:

* a clean parse stamps ``last_parsed_time`` (and ``bundle_version`` on Airflow 3); a failed
  one leaves both and re-stamps the file's import error on every parse;
* a parse that was already running when the files landed reads the old files, so a file
  counts as parsed once Airflow has parsed it twice after the sync started (the second
  parse began after the first ended), unless its Dags report the pushed bundle version;
* Airflow 3 shows an import error of a file with no registered Dag only to a token with
  access to all import errors, so a missing Dag is never taken for a clean file.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, List

import pytest

from fluid_build.cli import _airflow_sync_check as chk
from fluid_build.cli._airflow_api import AirflowApi, Credentials
from tests.cli._fake_airflow import FakeAirflow

pytestmark = pytest.mark.unit

_EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)
V1 = "1" * 40
V2 = "2" * 40


@pytest.fixture(autouse=True)
def _no_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_PROXY", "*")


class Processor:
    """Each sleep of the check moves the server clock on and runs the next step."""

    def __init__(self, fake: FakeAirflow, steps: List[Callable[[FakeAirflow], None]]):
        self.fake, self.steps, self.sleeps = fake, list(steps), 0

    def sleep(self, seconds: float) -> None:
        self.sleeps += 1
        self.fake.advance(seconds)
        if self.steps:
            self.steps.pop(0)(self.fake)

    def monotonic(self) -> float:
        return (self.fake.now - _EPOCH).total_seconds()


def ok(dag_id: str, rel: str, **kw) -> Callable[[FakeAirflow], None]:
    return lambda fake: fake.parse_ok(dag_id, rel, **kw)


def fail(rel: str, **kw) -> Callable[[FakeAirflow], None]:
    return lambda fake: fake.parse_fail(rel, **kw)


def idle(fake: FakeAirflow) -> None:
    pass


def both(*steps: Callable[[FakeAirflow], None]) -> Callable[[FakeAirflow], None]:
    def run(fake: FakeAirflow) -> None:
        for step in steps:
            step(fake)

    return run


ORDERS = chk.DagFile(path="sales__dev/orders.py", dag_ids=("sales__dev__orders",))


def _run(fake: FakeAirflow, steps, files=(ORDERS,), wait_s: float = 60, **kw) -> chk.CheckResult:
    processor = Processor(fake, steps)
    client = AirflowApi(fake.url, Credentials.from_password("admin", "admin"), sleep=lambda s: 0)
    client.connect("auto")
    return chk.wait_for_parse(
        client,
        list(files),
        wait_s=wait_s,
        poll_s=10,
        sleep=processor.sleep,
        monotonic=processor.monotonic,
        **kw,
    )


def _state(result: chk.CheckResult) -> List[str]:
    return [f.state for f in result.files]


# ── the two-parse rule (no bundle version) ──────────────────────────────


@pytest.mark.parametrize("major", [2, 3])
def test_two_clean_parses_after_the_sync_make_a_file_live(major: int):
    with FakeAirflow(major) as fake:
        fake.parse_ok("sales__dev__orders", ORDERS.path)  # the previous deploy
        steps = [ok("sales__dev__orders", ORDERS.path), ok("sales__dev__orders", ORDERS.path)]
        result = _run(fake, steps)
    assert result.passed, result.message
    assert _state(result) == ["clean"] and result.files[0].evidence == "parse-time"
    assert result.event is None


@pytest.mark.parametrize("major", [2, 3])
def test_one_parse_after_the_sync_is_not_proof(major: int):
    with FakeAirflow(major) as fake:
        result = _run(fake, [ok("sales__dev__orders", ORDERS.path)], wait_s=30)
    assert not result.passed and result.event == "schedule_sync_airflow_not_parsed"
    assert _state(result) == ["pending"]
    assert "once" in result.files[0].detail


def test_a_parse_running_when_the_files_landed_is_not_taken_for_them():
    """Old files parse clean once more after the sync; the new ones then fail."""
    with FakeAirflow(3) as fake:
        fake.parse_ok("sales__dev__orders", ORDERS.path)
        steps = [ok("sales__dev__orders", ORDERS.path), fail(ORDERS.path), fail(ORDERS.path)]
        result = _run(fake, steps)
    assert not result.passed and _state(result) == ["broken"]
    assert result.event == "schedule_sync_airflow_import_errors"


@pytest.mark.parametrize("major", [2, 3])
def test_a_broken_file_fails_with_the_last_line_of_its_error(major: int):
    stack = "Traceback (most recent call last):\n  File x\nModuleNotFoundError: No module named 'q'"
    with FakeAirflow(major) as fake:
        steps = [fail(ORDERS.path, stack=stack), fail(ORDERS.path, stack=stack)]
        result = _run(fake, steps)
    assert not result.passed and _state(result) == ["broken"]
    assert result.files[0].error == "ModuleNotFoundError: No module named 'q'"
    assert "sales__dev/orders.py" in result.message


def test_one_failed_parse_after_the_sync_is_not_proof_either():
    """The first failure may be the old file, parsed while the new one landed."""
    with FakeAirflow(3) as fake:
        fake.parse_fail(ORDERS.path)  # broken before the sync
        steps = [
            fail(ORDERS.path),
            ok("sales__dev__orders", ORDERS.path),
            ok("sales__dev__orders", ORDERS.path),
        ]
        result = _run(fake, steps)
    assert result.passed, result.message


def test_an_import_error_from_before_the_sync_does_not_count():
    with FakeAirflow(2) as fake:
        fake.parse_fail(ORDERS.path)
        fake.advance(30)
        steps = [ok("sales__dev__orders", ORDERS.path), ok("sales__dev__orders", ORDERS.path)]
        result = _run(fake, steps)
    assert result.passed, result.message


@pytest.mark.parametrize("major", [2, 3])
def test_a_dag_that_never_appears_is_pending(major: int):
    with FakeAirflow(major) as fake:
        result = _run(fake, [idle] * 10, wait_s=40)
    assert not result.passed and _state(result) == ["pending"]
    assert "no DAG sales__dev__orders" in result.files[0].detail
    assert result.waited_s >= 40


def test_a_file_is_live_only_when_every_dag_in_it_is():
    two = chk.DagFile(path="p/two.py", dag_ids=("p__a", "p__b"))
    with FakeAirflow(3) as fake:
        steps = [both(ok("p__a", two.path)), both(ok("p__a", two.path))]
        result = _run(fake, steps, files=[two], wait_s=30)
    assert _state(result) == ["pending"] and "p__b" in result.files[0].detail


# ── Airflow 3 bundle versions ───────────────────────────────────────────


def test_the_pushed_bundle_version_is_proof_after_one_parse():
    with FakeAirflow(3) as fake:
        fake.parse_ok("sales__dev__orders", ORDERS.path, bundle="git-dags", version=V1)
        steps = [ok("sales__dev__orders", ORDERS.path, bundle="git-dags", version=V2)]
        result = _run(fake, steps, expected_version=V2)
    assert result.passed and result.files[0].evidence == "bundle-version"
    assert result.polls == 2


def test_a_dag_still_on_the_previous_version_is_pending():
    with FakeAirflow(3) as fake:
        steps = [ok("sales__dev__orders", ORDERS.path, bundle="git-dags", version=V1)] * 5
        result = _run(fake, steps, expected_version=V2, wait_s=30)
    assert _state(result) == ["pending"]
    assert V1[:12] in result.files[0].detail and V2[:12] in result.files[0].detail


def test_a_broken_push_fails_fast_where_airflow_reports_the_bundle_version():
    """Airflow 3.4 serves /dagBundles: the bundle is at the pushed commit, so the
    errors stamped after its refresh are errors of the pushed files."""
    with FakeAirflow(3, bundles_endpoint=True) as fake:
        fake.parse_ok("sales__dev__orders", ORDERS.path, bundle="git-dags", version=V1)
        fake.set_bundle("git-dags", V1)
        steps = [
            lambda f: f.set_bundle("git-dags", V2),
            fail(ORDERS.path, bundle="git-dags"),
            fail(ORDERS.path, bundle="git-dags"),
        ]
        result = _run(fake, steps, expected_version=V2, wait_s=300)
    assert _state(result) == ["broken"] and result.waited_s < 60


def test_on_airflow_3_0_a_broken_push_is_judged_at_the_deadline():
    """No /dagBundles and no Dag at the pushed version: the errors cannot be tied to it."""
    with FakeAirflow(3, version="3.0.6", bundles_endpoint=False) as fake:
        fake.parse_ok("sales__dev__orders", ORDERS.path, bundle="git-dags", version=V1)
        steps = [fail(ORDERS.path, bundle="git-dags")] * 6
        result = _run(fake, steps, expected_version=V2, wait_s=40)
    assert not result.passed and _state(result) == ["pending"]
    assert "ModuleNotFoundError" in result.files[0].error
    assert result.event == "schedule_sync_airflow_not_parsed"


def test_another_dag_at_the_pushed_version_ties_the_errors_to_it():
    other = chk.DagFile(path="team/other.py", dag_ids=("team__other",))
    with FakeAirflow(3, version="3.0.6", bundles_endpoint=False) as fake:
        steps = [
            both(
                ok("team__other", other.path, bundle="g", version=V2), fail(ORDERS.path, bundle="g")
            ),
            fail(ORDERS.path, bundle="g"),
            fail(ORDERS.path, bundle="g"),
        ]
        result = _run(fake, steps, files=[ORDERS, other], expected_version=V2, wait_s=300)
    assert _state(result) == ["broken", "clean"]


def test_an_explicit_version_on_an_unversioned_bundle_waits_and_says_why():
    with FakeAirflow(3) as fake:
        steps = [ok("sales__dev__orders", ORDERS.path)] * 5
        result = _run(fake, steps, expected_version=V2, version_explicit=True, wait_s=30)
    assert _state(result) == ["pending"] and "no bundle version" in result.files[0].detail


@pytest.mark.parametrize("major", [2, 3])
def test_a_pushed_commit_on_an_unversioned_folder_falls_back_to_parse_time(major: int):
    """A git push that a git-sync sidecar copies into a DAG folder: no bundle version."""
    with FakeAirflow(major) as fake:
        steps = [ok("sales__dev__orders", ORDERS.path), ok("sales__dev__orders", ORDERS.path)]
        result = _run(fake, steps, expected_version=V2)
    assert result.passed and result.files[0].evidence == "parse-time"


def test_the_bundle_filter_ignores_errors_and_dags_of_other_bundles():
    with FakeAirflow(3) as fake:
        fake.parse_fail(ORDERS.path, bundle="other-bundle")
        steps = [
            both(
                fail(ORDERS.path, bundle="other-bundle"),
                ok("sales__dev__orders", ORDERS.path, bundle="mine"),
            ),
            both(
                fail(ORDERS.path, bundle="other-bundle"),
                ok("sales__dev__orders", ORDERS.path, bundle="mine"),
            ),
        ]
        result = _run(fake, steps, bundle="mine")
    assert result.passed, result.message


def test_a_dag_reported_in_another_bundle_is_a_mismatch():
    with FakeAirflow(3) as fake:
        steps = [ok("sales__dev__orders", ORDERS.path, bundle="theirs")] * 4
        result = _run(fake, steps, bundle="mine", wait_s=30)
    assert _state(result) == ["pending"] and "bundle theirs" in result.files[0].detail


# ── what the token cannot see, transient trouble ────────────────────────


def test_errors_hidden_from_the_token_leave_a_new_file_pending_with_a_hint():
    with FakeAirflow(3, sees_unregistered_errors=False) as fake:
        result = _run(fake, [fail(ORDERS.path)] * 6, wait_s=40)
    assert _state(result) == ["pending"]
    assert any("all import errors" in note for note in result.notes)


def test_a_dag_flagged_with_an_import_error_is_named_when_its_error_is_hidden():
    with FakeAirflow(3, hide_errors=True) as fake:
        fake.parse_ok("sales__dev__orders", ORDERS.path)
        result = _run(fake, [fail(ORDERS.path)] * 5, wait_s=30)
    assert _state(result) == ["pending"]
    assert "import error for the file of DAG sales__dev__orders" in result.files[0].detail


def test_a_transient_outage_during_the_wait_is_ridden_out():
    with FakeAirflow(3) as fake:

        def outage(f: FakeAirflow) -> None:
            f.answer_next = [(503, {}, "restarting")]
            f.parse_ok("sales__dev__orders", ORDERS.path)

        # The parse during the outage is never seen, so two more are needed.
        steps = [
            outage,
            ok("sales__dev__orders", ORDERS.path),
            ok("sales__dev__orders", ORDERS.path),
        ]
        result = _run(fake, steps)
    assert result.passed, result.message
    assert result.polls == 4


def test_an_airflow_that_never_answers_again_is_unreachable():
    with FakeAirflow(3) as fake:

        def down(f: FakeAirflow) -> None:
            f.answer_next = [(503, {}, "down")] * 50

        result = _run(fake, [down] * 10, wait_s=30)
    assert result.event == "schedule_sync_airflow_unreachable"
    assert "503" in result.message


def test_a_token_refused_during_the_wait_stops_the_wait():
    with FakeAirflow(3, static_tokens=("only-once",)) as fake:
        client = AirflowApi(fake.url, Credentials.from_token("only-once"), sleep=lambda s: 0)
        client.connect("auto")
        fake.static_tokens = ()
        processor = Processor(fake, [idle] * 10)
        result = chk.wait_for_parse(
            client,
            [ORDERS],
            wait_s=300,
            poll_s=10,
            sleep=processor.sleep,
            monotonic=processor.monotonic,
        )
    assert result.event == "schedule_sync_airflow_auth_failed" and processor.sleeps == 0


# ── files whose Dag ids forge cannot read ───────────────────────────────


def test_a_dynamic_file_is_judged_by_the_dags_airflow_lists_for_it():
    dyn = chk.DagFile(path="factory/made.py", dag_ids=(), dynamic=True)
    with FakeAirflow(3) as fake:
        steps = [ok("made__1", dyn.path), ok("made__1", dyn.path)]
        result = _run(fake, steps, files=[dyn])
    assert result.passed and result.files[0].dag_ids == ("made__1",)


def test_a_dynamic_file_airflow_lists_nothing_for_is_unchecked_not_failed():
    dyn = chk.DagFile(path="common/helpers.py", dag_ids=(), dynamic=True)
    with FakeAirflow(2) as fake:
        steps = [ok("sales__dev__orders", ORDERS.path), ok("sales__dev__orders", ORDERS.path)]
        result = _run(fake, steps, files=[ORDERS, dyn])
    assert result.passed and _state(result) == ["clean", "unchecked"]
    assert any("common/helpers.py" in note for note in result.notes)


def test_when_no_file_can_be_checked_the_check_fails_rather_than_pass_blind():
    dyn = chk.DagFile(path="common/helpers.py", dag_ids=(), dynamic=True)
    with FakeAirflow(2) as fake:
        result = _run(fake, [idle] * 5, files=[dyn], wait_s=20)
    assert not result.passed and result.event == "schedule_sync_airflow_not_parsed"
    assert "nothing" in result.message


def test_a_dynamic_file_with_an_import_error_is_broken():
    dyn = chk.DagFile(path="factory/made.py", dag_ids=(), dynamic=True)
    with FakeAirflow(2) as fake:
        steps = [
            both(ok("sales__dev__orders", ORDERS.path), fail(dyn.path)),
            both(ok("sales__dev__orders", ORDERS.path), fail(dyn.path)),
        ]
        result = _run(fake, steps, files=[ORDERS, dyn])
    assert _state(result) == ["clean", "broken"]


def test_the_report_names_each_file_and_no_credential():
    stack = "Traceback\nKeyError: password=hunter2"
    with FakeAirflow(3) as fake:
        steps = [fail(ORDERS.path, stack=stack), fail(ORDERS.path, stack=stack)]
        report = _run(fake, steps).to_report()
    assert report["result"] == "failed" and report["event"] == "schedule_sync_airflow_import_errors"
    (entry,) = report["files"]
    assert entry["path"] == ORDERS.path and entry["state"] == "broken"
    assert "hunter2" not in str(report)


def test_an_indirect_transport_is_named_in_the_notes():
    with FakeAirflow(2) as fake:
        steps = [ok("sales__dev__orders", ORDERS.path), ok("sales__dev__orders", ORDERS.path)]
        result = _run(fake, steps, indirect=True)
    assert result.passed
    assert any("own schedule" in note for note in result.notes)


# ── reading Dag ids from the files ──────────────────────────────────────


def _write(tmp_path: Path, rel: str, text: str) -> None:
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_dag_ids_are_read_from_every_spelling_forge_and_people_use(tmp_path: Path):
    _write(
        tmp_path, "a/kw.py", "from airflow import DAG\nwith DAG(dag_id='a__kw') as dag:\n    pass\n"
    )
    _write(tmp_path, "a/pos.py", "from airflow import DAG\ndag = DAG('a__pos')\n")
    _write(tmp_path, "a/mod.py", 'from airflow import models\nd = models.DAG(dag_id="a__mod")\n')
    _write(tmp_path, "a/const.py", "from airflow import DAG\nNAME = 'a__const'\nDAG(dag_id=NAME)\n")
    _write(
        tmp_path,
        "a/fstr.py",
        "from airflow import DAG\nP = 'a'\nE = 'dev'\nDAG(dag_id=f'{P}__{E}__f')\nDAG(dag_id=P + '__plus')\n",
    )
    _write(
        tmp_path,
        "a/deco.py",
        "from airflow.sdk import dag\n@dag(dag_id='a__deco')\ndef x():\n    pass\n"
        "@dag\ndef a__bare():\n    pass\n@dag()\ndef a__call():\n    pass\n",
    )
    files = {f.path: f for f in chk.dag_files(tmp_path)}
    assert files["a/kw.py"].dag_ids == ("a__kw",)
    assert files["a/pos.py"].dag_ids == ("a__pos",)
    assert files["a/mod.py"].dag_ids == ("a__mod",)
    assert files["a/const.py"].dag_ids == ("a__const",)
    assert files["a/fstr.py"].dag_ids == ("a__dev__f", "a__plus")
    assert files["a/deco.py"].dag_ids == ("a__deco", "a__bare", "a__call")
    assert not any(f.dynamic for f in files.values())


def test_files_airflow_would_skip_are_not_expected_to_hold_dags(tmp_path: Path):
    _write(tmp_path, "p/util.py", "def helper():\n    return 1\n")  # no "airflow" / "dag"
    _write(tmp_path, "p/__pycache__/x.py", "from airflow import DAG\nDAG('x')\n")
    _write(tmp_path, "p/.hidden.py", "from airflow import DAG\nDAG('x')\n")
    _write(tmp_path, "p/readme.md", "airflow dag")
    _write(tmp_path, "p/d.py", "from airflow import DAG\nDAG('p__d')\n")
    assert [f.path for f in chk.dag_files(tmp_path)] == ["p/d.py"]


def test_a_dag_id_forge_cannot_read_makes_the_file_dynamic(tmp_path: Path):
    _write(
        tmp_path,
        "f/factory.py",
        "from airflow import DAG\nfor n in ['a', 'b']:\n    globals()[n] = DAG(dag_id=n + suffix())\n",
    )
    _write(tmp_path, "f/broken.py", "from airflow import DAG\nDAG('x'\n")
    files = {f.path: f for f in chk.dag_files(tmp_path)}
    assert files["f/factory.py"].dynamic and files["f/factory.py"].dag_ids == ()
    assert files["f/broken.py"].dynamic


def test_a_helper_that_never_builds_a_dag_is_dynamic_not_expected(tmp_path: Path):
    _write(tmp_path, "c/common.py", "from airflow.models import Variable  # dag helpers\n")
    (only,) = chk.dag_files(tmp_path)
    assert only.dynamic and only.dag_ids == ()


# ── where the files landed: another directory's file of the same name is not ours ──


@pytest.mark.parametrize("major", [2, 3])
def test_an_error_of_a_same_named_file_elsewhere_is_not_ours_once_ours_is_placed(major: int):
    """A shared DAG folder: team-b has its own ``sales__dev/orders.py``, broken."""
    other = "team-b/" + ORDERS.path
    with FakeAirflow(major) as fake:
        steps = [
            both(ok("sales__dev__orders", ORDERS.path), fail(other)),
            both(ok("sales__dev__orders", ORDERS.path), fail(other)),
        ]
        result = _run(fake, steps)
    assert result.passed, result.message


def test_a_dynamic_file_is_not_given_the_dags_of_a_same_named_file_elsewhere():
    dyn = chk.DagFile(path="factory/made.py", dag_ids=(), dynamic=True)
    with FakeAirflow(3) as fake:
        steps = [
            both(ok("sales__dev__orders", ORDERS.path), ok("theirs__1", "team-b/" + dyn.path)),
            both(ok("sales__dev__orders", ORDERS.path), ok("theirs__1", "team-b/" + dyn.path)),
        ]
        result = _run(fake, steps, files=[ORDERS, dyn])
    assert _state(result) == ["clean", "unchecked"] and result.files[1].dag_ids == ()


# ── what the scan of the first version turned up ────────────────────────


GEN = chk.DagFile(path="factory/gen.py", dag_ids=(), dynamic=True)
STATIC = chk.DagFile(path="team/s.py", dag_ids=("team__s",))


def test_a_dynamic_file_with_a_visible_import_error_holds_the_wait():
    """Versioned: the static file is clean after one poll; the factory file is broken."""
    with FakeAirflow(3) as fake:
        fake.set_bundle("dags-folder", V2)
        fake.advance(5)
        fake.parse_ok("team__s", STATIC.path, version=V2)
        fake.parse_fail(GEN.path)
        steps = [fail(GEN.path), fail(GEN.path), fail(GEN.path)]
        result = _run(fake, steps, files=[STATIC, GEN], expected_version=V2, version_explicit=True)
    assert not result.passed and _state(result) == ["clean", "broken"]


def test_a_dynamic_file_whose_dags_go_stale_still_holds_the_wait():
    """Airflow 3 lists no stale Dag; the factory's Dag drops out when its file breaks."""
    with FakeAirflow(3) as fake:
        fake.parse_ok("gen__1", GEN.path)
        steps = [
            both(ok("team__s", STATIC.path), fail(GEN.path)),
            both(ok("team__s", STATIC.path), fail(GEN.path)),
            both(ok("team__s", STATIC.path), fail(GEN.path)),
        ]
        result = _run(fake, steps, files=[STATIC, GEN])
    assert not result.passed and _state(result) == ["clean", "broken"]
    assert result.files[1].dag_ids == ("gen__1",)


def test_the_fake_lists_no_stale_dag_like_airflow_3():
    with FakeAirflow(3) as fake:
        fake.parse_ok("gen__1", GEN.path)
        fake.parse_fail(GEN.path)
        client = AirflowApi(
            fake.url, Credentials.from_password("admin", "admin"), sleep=lambda s: 0
        )
        client.connect("auto")
        assert client.dags() == [] and client.dag("gen__1") is not None


@pytest.mark.parametrize("major", [2, 3])
def test_a_dag_parsed_from_another_file_does_not_count_for_the_pushed_one(major: int):
    """The same dag_id in a copy left at an old path: Airflow keeps parsing that one."""
    with FakeAirflow(major) as fake:
        fake.parse_ok("sales__dev__orders", "old/orders.py")
        steps = [ok("sales__dev__orders", "old/orders.py")] * 5
        result = _run(fake, steps, wait_s=40)
    assert not result.passed and _state(result) == ["pending"]
    assert "old/orders.py" in result.files[0].detail


def test_on_an_indirect_destination_a_fix_that_lands_late_is_not_called_broken():
    """S3 or Composer: the previous, broken file is parsed twice before the fix lands."""
    with FakeAirflow(2) as fake:
        fake.parse_ok("sales__dev__orders", ORDERS.path)
        fake.parse_fail(ORDERS.path)
        steps = [
            fail(ORDERS.path),
            fail(ORDERS.path),
            ok("sales__dev__orders", ORDERS.path),
            ok("sales__dev__orders", ORDERS.path),
        ]
        result = _run(fake, steps, indirect=True, wait_s=120)
    assert result.passed, result.message


def test_on_an_indirect_destination_a_file_still_broken_at_the_deadline_fails():
    with FakeAirflow(2) as fake:
        result = _run(fake, [fail(ORDERS.path)] * 10, indirect=True, wait_s=60)
    assert not result.passed and _state(result) == ["broken"]
    assert result.waited_s >= 60


def test_an_abbreviated_commit_matches_the_full_bundle_version():
    with FakeAirflow(3) as fake:
        steps = [
            ok("sales__dev__orders", ORDERS.path, bundle="git-dags", version="abc1234" + "0" * 33)
        ]
        result = _run(fake, steps, expected_version="ABC1234", version_explicit=True)
    assert result.passed and result.files[0].evidence == "bundle-version"


def test_a_short_prefix_is_not_a_commit():
    with FakeAirflow(3) as fake:
        steps = [
            ok("sales__dev__orders", ORDERS.path, bundle="git-dags", version="abc1234" + "0" * 33)
        ] * 3
        result = _run(fake, steps, expected_version="abc", version_explicit=True, wait_s=20)
    assert not result.passed


def test_a_long_wait_says_what_it_still_waits_for():
    lines: List[str] = []
    with FakeAirflow(2) as fake:
        processor = Processor(fake, [idle] * 20)
        client = AirflowApi(
            fake.url, Credentials.from_password("admin", "admin"), sleep=lambda s: 0
        )
        client.connect("auto")
        chk.wait_for_parse(
            client,
            [ORDERS],
            wait_s=150,
            poll_s=10,
            sleep=processor.sleep,
            monotonic=processor.monotonic,
            on_progress=lines.append,
        )
    assert len(lines) == 2 and all(ORDERS.path in line for line in lines)
    assert "still waiting" in lines[0]


def test_server_text_with_a_newline_cannot_forge_a_console_line():
    with FakeAirflow(3) as fake:
        steps = [ok("sales__dev__orders", ORDERS.path, bundle="b\n[schedule-sync] airflow: x")] * 3
        result = _run(fake, steps, bundle="mine", wait_s=20)
    assert "\n" not in result.files[0].detail and "\n" not in result.message


def test_a_dag_id_airflow_would_refuse_is_not_read(tmp_path: Path):
    _write(tmp_path, "n/bad.py", "from airflow import DAG\nDAG(dag_id='x\\n[schedule-sync] ok')\n")
    _write(tmp_path, "n/long.py", f"from airflow import DAG\nDAG(dag_id='{'a' * 251}')\n")
    files = {f.path: f for f in chk.dag_files(tmp_path)}
    assert files["n/bad.py"].dag_ids == () and files["n/bad.py"].dynamic
    assert files["n/long.py"].dag_ids == ()


@pytest.mark.parametrize(
    "source",
    [
        b"from airflow import DAG\nX = " + b" + ".join([b'"a"'] * 2000) + b"\nDAG('x')\n",
        b"from airflow import DAG\nX = " + b"a+" * 30000 + b"a\nDAG('x')\n",
        b"from airflow import DAG\n" + b"lambda: " * 3000 + b"1\n",
    ],
    ids=["long-sum", "deep-sum", "nested-lambdas"],
)
def test_a_dag_file_too_deep_to_read_does_not_crash_the_check(tmp_path: Path, source: bytes):
    """Read, or left to Airflow when too deep: how deep depends on the recursion limit."""
    (tmp_path / "deep.py").write_bytes(source)
    (only,) = chk.dag_files(tmp_path)
    assert only.dynamic or only.dag_ids == ("x",)
