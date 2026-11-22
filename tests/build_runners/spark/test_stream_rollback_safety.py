# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""When a release that failed its probation is rolled back, and when it is not: a rollback that
would write records into an output a second time is refused, an interrupt in the middle of one
leaves the stream reported (and the failed config marked), and a config that failed is not
started again as it stands.

Runs against the in-memory adapters of ``test_stream_rollback.py``."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import signal

import pytest

pytest.importorskip("botocore")

from fluid_build.build_runners.spark import _adapters as ad
from fluid_build.build_runners.spark import _reconcile as rc
from fluid_build.build_runners.spark._desired import config_digest
from fluid_build.cli import _spark_apply
from tests.build_runners.spark.test_reconcile import WALL, info
from tests.build_runners.spark.test_stream_release import S3, _client_error
from tests.build_runners.spark.test_stream_rollback import (
    OLD_DIGEST,
    OLD_JOB,
    Bucket,
    Rolling,
    _serverless,
    go,
    last_good,
)

#: The checkpoint id of the last good config when the release moved it.
MOVED = "oldckpt000000000"
OLD_CHECKPOINT = "s3://lake/_checkpoints/OLD/"


def job(checkpoint, table=None, dead=None):
    """A stream's job config as the old run read it."""
    config = {
        "configVersion": 1,
        "mode": "stream",
        "checkpoint": checkpoint,
        "source": {"kind": "kafka", "topic": "commerce.orders"},
    }
    if table:
        config["sink"] = {"table": table}
    if dead:
        config["deadLetter"] = dead
    return json.dumps(config, sort_keys=True)


def moved_from(run, table, dead=None):
    """The last good run, which ran on another checkpoint, and its release record."""
    text = job(OLD_CHECKPOINT, table, dead)
    digest = config_digest(text)
    good = dataclasses.replace(last_good(run), checkpoint_id=MOVED, job_sha256=digest)
    return good, {digest: text}


def with_dead_letter(run, table):
    config = json.loads(run.job_text)
    config["deadLetter"] = {"sink": "table", "table": table}
    return dataclasses.replace(run, job_text=json.dumps(config, sort_keys=True))


def retried(fake):
    """``fake``'s run ``new`` is retried by its runtime in the probation (attempt 2)."""
    attempts = iter([1, 1, 2])

    def get(run_id, original=fake.get):
        seen = original(run_id)
        if run_id == "new" and "cancel new" not in fake.calls:
            return dataclasses.replace(seen, attempt=next(attempts, 2))
        return seen

    fake.get = get


# --- a checkpoint the release moved -------------------------------------------------------------


@pytest.mark.parametrize("how", ["failed", "retried"])
def test_a_rollback_onto_the_old_checkpoint_into_the_same_output_is_refused(run, clock, use, how):
    """The failed run wrote offsets on the new checkpoint; the old config would resume the old one
    from an earlier offset and write those records into the same table again."""
    good, records = moved_from(run, "commerce.order_lines")
    fake = Rolling(
        [good],
        answers={"old1"},
        records=records,
        start_states=("FAILED",) if how == "failed" else ("RUNNING",),
    )
    if how == "retried":
        retried(fake)
    use(fake)
    out = go(run, clock)
    assert (out.action, out.error, out.code) == ("failed", True, rc.ROLLBACK_UNSAFE)
    assert fake.rolled_back == [] and "start rollback" not in fake.calls
    assert ("cancel new" in fake.calls) == (how == "retried")  # the failed run is left cancelled
    assert out.cancelled == (["new"] if how == "retried" else [])
    assert "a second time" in out.message and "The stream is down" in out.message
    assert "apply the previous contract again" in out.message
    assert "commerce.order_lines" in out.message
    assert "was not started again" in out.message
    assert run.config_hash in fake.failed  # and the failed config is marked, so it is not restarted


def test_a_rollback_onto_the_old_checkpoint_into_the_same_dead_letters_is_refused_too(
    run, clock, use
):
    dead = {"sink": "table", "table": "commerce.rejects"}
    good, records = moved_from(run, "commerce.another_output", dead)
    fake = Rolling([good], answers={"old1"}, records=records, start_states=("FAILED",))
    use(fake)
    out = go(with_dead_letter(run, "commerce.rejects"), clock)
    assert (out.action, out.code, fake.rolled_back) == ("failed", rc.ROLLBACK_UNSAFE, [])
    assert "the dead-letter table commerce.rejects" in out.message


def test_a_rollback_onto_the_old_checkpoint_with_no_output_named_is_refused(run, clock, use):
    """An output the old job config does not name cannot be shown to be another one."""
    good, records = moved_from(run, None)
    fake = Rolling([good], answers={"old1"}, records=records, start_states=("FAILED",))
    use(fake)
    out = go(run, clock)
    assert (out.code, fake.rolled_back) == (rc.ROLLBACK_UNSAFE, [])


def test_a_rollback_onto_the_old_checkpoint_into_another_output_says_the_sink_keeps_its_data(
    run, clock, use
):
    good, records = moved_from(run, "commerce.another_output", {"sink": "table", "table": "x.y"})
    fake = Rolling([good], answers={"old1"}, records=records, start_states=("FAILED",))
    use(fake)
    out = go(with_dead_letter(run, "commerce.rejects"), clock)
    assert (out.action, out.error, out.code, out.run_id) == ("rolled-back", True, None, "rb")
    (back,) = fake.rolled_back
    assert back.checkpoint_id == MOVED and back.checkpoint == OLD_CHECKPOINT
    assert "run new's sink commerce.order_lines keeps what it wrote" in out.message
    assert "the rolled-back run writes commerce.another_output" in out.message


def test_a_rollback_on_a_shared_checkpoint_says_the_failed_batches_stay_in_the_sink(
    run, clock, use
):
    fake = Rolling(
        [last_good(run)], answers={"old1"}, start_states=("FAILED",), records={OLD_DIGEST: OLD_JOB}
    )
    use(fake)
    out = go(run, clock)
    assert (out.action, out.code, out.run_id) == ("rolled-back", None, "rb")
    assert "the batches run new committed to the sink stay there" in out.message
    assert "does not read them again" in out.message


# --- an interrupt in the window between the cancel and the start -------------------------------------


def _interrupt_after_cancel(fake, on_runs):
    """``fake.runs`` calls ``on_runs`` on its second read after the failed run's start: the
    rollback's own check for another writer, after the failed run ended and before the start."""
    original, seen = fake.runs, {"n": 0}

    def runs(since=None):
        if "start" in fake.calls and "start rollback" not in fake.calls:
            seen["n"] += 1
            if seen["n"] == 2:
                on_runs()
        return original(since)

    fake.runs = runs


def _raise_interrupt():
    raise KeyboardInterrupt


def test_a_ctrl_c_before_the_rollback_starts_reports_the_stream_down_and_marks_the_config(
    run, clock, use
):
    fake = Rolling(
        [last_good(run)], answers={"old1"}, start_states=("FAILED",), records={OLD_DIGEST: OLD_JOB}
    )
    _interrupt_after_cancel(fake, _raise_interrupt)
    use(fake)
    out = go(run, clock)  # the outcome comes back; the apply raises the interrupt after saying it
    assert (out.interrupted, out.error, out.action) == (True, True, "failed")
    assert "config stale was not started" in out.message and "the stream may be down" in out.message
    assert "start rollback" not in fake.calls
    assert run.config_hash in fake.failed
    with pytest.raises(KeyboardInterrupt):
        _spark_apply._result([out])


@pytest.mark.skipif(not hasattr(signal, "SIGTERM"), reason="no SIGTERM")
def test_a_sigterm_in_the_window_is_a_ctrl_c_too(run, clock, use):
    """SIGTERM (a killed apply) ends the window in an outcome, not in silence, as ``run_once``'s
    interrupt guard does for a scheduled run."""
    fake = Rolling(
        [last_good(run)], answers={"old1"}, start_states=("FAILED",), records={OLD_DIGEST: OLD_JOB}
    )
    _interrupt_after_cancel(fake, lambda: os.kill(os.getpid(), signal.SIGTERM))
    use(fake)
    before = signal.getsignal(signal.SIGTERM)
    out = go(run, clock)
    assert signal.getsignal(signal.SIGTERM) is before  # the handler is put back
    assert (out.interrupted, out.error) == (True, True)
    assert "start rollback" not in fake.calls and run.config_hash in fake.failed


def test_a_ctrl_c_while_the_failed_run_is_cancelled_says_so(run, clock, use):
    fake = Rolling([last_good(run)], answers={"old1"}, records={OLD_DIGEST: OLD_JOB})
    retried(fake)

    def cancel(run_id):
        fake.calls.append(f"cancel {run_id}")
        fake.scripts[run_id] = ["CANCELLING", "INTERRUPT"]

    fake.cancel = cancel
    use(fake)
    out = go(run, clock)
    assert (
        out.interrupted and "run new was cancelled, and config stale was not started" in out.message
    )
    assert "marked as failed" in out.message and rc.ACCEPT_FAILED_ENV in out.message


def test_the_next_apply_after_an_interrupted_rollback_does_not_start_the_failed_config_again(
    run, clock, use
):
    """The failed config is cancelled and marked; the stream is down. A plain apply of the same
    contract refuses it, and says how; the operator's go-ahead starts it, watched again."""
    first = Rolling(
        [last_good(run)], answers={"old1"}, start_states=("FAILED",), records={OLD_DIGEST: OLD_JOB}
    )
    _interrupt_after_cancel(first, _raise_interrupt)
    use(first)
    go(run, clock)
    cancelled = info(
        "new", "CANCELLED", hash_=run.config_hash, source=run.source_id,
        checkpoint=run.checkpoint_id, created=2.0,
    )  # fmt: skip
    ended = dataclasses.replace(last_good(run), state="SUCCEEDED")

    def next_apply(env=None):
        fake = Rolling([cancelled, ended], records={OLD_DIGEST: OLD_JOB})
        fake.failed = dict(first.failed)
        use(fake)
        return fake, go(run, clock, env=env)

    fake, out = next_apply()
    assert (out.action, out.error, out.code) == ("refused", True, rc.FAILED_EARLIER)
    assert fake.calls == [] and "failed its probation earlier" in out.message
    assert f"{rc.ACCEPT_FAILED_ENV}=1" in out.message
    fake, out = next_apply({rc.ACCEPT_FAILED_ENV: "1"})
    assert (out.action, out.error, out.code) == ("started", False, None)
    assert "start" in fake.calls and fake.cleared == [run.config_hash]
    assert run.config_hash not in fake.failed and "started although it failed" in out.message


def test_a_config_that_failed_its_probation_is_refused_before_a_running_config_is_stopped(
    run, clock, use
):
    """The marker of a rolled-back release: the same contract applied again leaves the stream on
    the config it was rolled back to."""
    fake = Rolling([last_good(run)], answers={"old1"}, records={OLD_DIGEST: OLD_JOB})
    fake.failed[run.config_hash] = json.dumps(
        {"runId": "r9", "failedAt": "2026-11-22T10:00:00Z"}
    ).encode()
    use(fake)
    out = go(run, clock)
    assert (out.action, out.code, fake.calls) == ("refused", rc.FAILED_EARLIER, [])
    assert "run r9 at 2026-11-22T10:00:00Z" in out.message
    assert "run old1 of another config was left running" in out.message


def test_a_dry_run_and_reconcile_only_see_the_marker_too(run, clock, use):
    from tests.build_runners.spark.conftest import variant

    cancelled = info("old1", "CANCELLED", hash_=run.config_hash, created=WALL - 60)
    fake = Rolling([cancelled])
    fake.failed[run.config_hash] = b"not json"
    use(fake)
    out = go(run, clock, dry_run=True)
    assert (out.action, out.code, fake.calls) == ("refused", rc.FAILED_EARLIER, [])
    assert "a marker without details" in out.message
    # --mode reconcile restarts a stream that died, on a runtime that has no retries of its own
    ec2 = variant(run, "emr-ec2", {"clusterId": "j-1"}, restarts_per_hour=5)
    died = info("s-1", "FAILED", hash_=ec2.config_hash, created=WALL - 60)
    fake = Rolling([died])
    fake.uploads_job = False
    fake.failed[ec2.config_hash] = b"{}"
    use(fake)
    out = go(ec2, clock, only=True)
    assert (out.action, out.code, fake.calls) == ("refused", rc.FAILED_EARLIER, [])


def test_a_failed_probation_marks_the_config_and_a_marker_that_cannot_be_kept_is_a_note(
    run, clock, use
):
    fake = Rolling(
        [last_good(run)], answers={"old1"}, start_states=("FAILED",), records={OLD_DIGEST: OLD_JOB}
    )
    use(fake)
    go(run, clock)
    marker = json.loads(fake.failed[run.config_hash])
    assert (marker["runId"], marker["configHash"]) == ("new", run.config_hash)
    assert "ended FAILED" in marker["reason"]
    refusing = Rolling(
        [last_good(run)], answers={"old1"}, start_states=("FAILED",), records={OLD_DIGEST: OLD_JOB}
    )

    def refuse(*_args):
        raise ad.SparkRunError("s3://b/releases/x/failed could not be written: AccessDenied")

    refusing.mark_failed = refuse
    use(refusing)
    out = go(run, clock)
    assert out.action == "rolled-back" and "no failed-release marker was kept" in out.message


# --- the reconcile's results become the apply's --------------------------------------------------


def test_an_outcome_with_a_catalog_event_is_raised_as_a_typed_error(run, clock, use):
    from fluid_build.cli._common import CLIError

    good, records = moved_from(run, "commerce.order_lines")
    fake = Rolling([good], answers={"old1"}, records=records, start_states=("FAILED",))
    use(fake)
    out = go(run, clock)
    with pytest.raises(CLIError) as caught:
        _spark_apply._result([out])
    assert caught.value.event == rc.ROLLBACK_UNSAFE
    assert caught.value.error_slug == "ERR_APPLY_SPARK_ROLLBACK_UNSAFE"
    assert any("apply the previous contract again" in s for s in caught.value.suggestions)
    assert caught.value.docs_url
    assert _spark_apply._result([dataclasses.replace(out, code=None)]) == 1


@pytest.mark.parametrize("event", [rc.ROLLBACK_UNSAFE, rc.NO_GOOD_CONFIG, rc.FAILED_EARLIER])
def test_every_event_a_release_can_end_with_is_in_the_error_catalog(event):
    from fluid_build import _error_catalog as cat

    assert event in cat.catalogued_events() and cat.suggestions_for(event)


# --- an unchanged stream's record -------------------------------------------------------------------


def _unchanged(run, record):
    """A running stream of this config with its digest, and ``record`` kept (or not)."""
    sha = config_digest(run.job_text)
    running = dataclasses.replace(info("jr1", "RUNNING", hash_=run.config_hash), job_sha256=sha)
    return Rolling([running], records={} if record is None else {sha: record})


@pytest.mark.parametrize(("record", "writes"), [("same", 0), (None, 1), ("other", 1), ("bytes", 1)])
def test_an_unchanged_stream_writes_its_record_only_when_it_is_missing_or_different(
    run, clock, use, record, writes
):
    text = {"same": run.job_text, "other": '{"another": 1}'}.get(record)
    fake = _unchanged(run, text)
    if record == "bytes":  # bytes that are not UTF-8
        fake.release_config = lambda _info: b"\xff\xfe"
    use(fake)
    out = go(run, clock)
    assert (out.action, out.error) == ("unchanged", False)
    assert len(fake.kept) == writes


def test_an_unchanged_stream_with_no_digest_of_its_own_is_judged_by_the_job_config_compiled(
    run, clock, use
):
    """A run an older forge started carries no digest: its record is the one of the job config
    this apply compiled, and it is kept once, not on every apply."""
    digest = config_digest(run.job_text)
    kept = Rolling([info("jr1", "RUNNING", hash_=run.config_hash)], records={digest: run.job_text})
    use(kept)
    go(run, clock)
    assert kept.kept == []
    missing = Rolling([info("jr1", "RUNNING", hash_=run.config_hash)])
    use(missing)
    go(run, clock)
    assert len(missing.kept) == 1


# --- the duplicate-writer check after a rollback starts ------------------------------------------


def test_two_applies_that_roll_back_at_once_keep_the_older_rollback_run(run, clock, use):
    label = hashlib.sha256(f"rollback:stale:{run.config_hash}".encode()).hexdigest()
    other = info("other-rb", "RUNNING", hash_=label, created=1.0)

    class Twice(Rolling):
        def runs(self, since=None):
            listed = super().runs(since)
            if "start rollback" in self.calls:
                self.scripts.setdefault("other-rb", ["RUNNING"])
                listed = [*listed, other]
            return listed

    fake = Twice(
        [last_good(run)], answers={"old1"}, start_states=("FAILED",), records={OLD_DIGEST: OLD_JOB}
    )
    use(fake)
    out = go(run, clock)
    assert (out.action, out.run_id, out.error) == ("rolled-back", "other-rb", True)
    assert "cancel rb" in fake.calls and "rb" in out.cancelled
    assert "run other-rb of it started first" in out.message


# --- reads that fail once -----------------------------------------------------------------------------


def test_a_clock_retries_the_first_read_like_the_others(clock):
    reads = []

    def read():
        reads.append(1)
        if len(reads) < 3:
            raise RuntimeError("Throttling")
        return info("r", "RUNNING")

    seen = rc._Clock(clock.sleep, clock.now).until(read, lambda i: True, 0.0)
    assert (seen.run_id, len(reads), clock.slept) == ("r", 3, [1.0, 2.0])


def test_a_clock_gives_up_on_a_first_read_that_never_works(clock):
    def read():
        raise RuntimeError("Throttling")

    with pytest.raises(RuntimeError):
        rc._Clock(clock.sleep, clock.now).until(read, lambda i: True, 0.0)
    assert len(clock.slept) == rc.MAX_FAILED_READS - 1


def test_a_probation_whose_first_read_is_throttled_is_still_watched(run, clock, use):
    fake = Rolling([last_good(run)], answers={"old1"}, records={OLD_DIGEST: OLD_JOB})
    use(fake)
    original, seen = fake.get, {"n": 0}

    def get(run_id):
        if run_id == "new":
            seen["n"] += 1
            if seen["n"] == 2:  # the start's read works; the probation's first does not
                raise RuntimeError("Throttling")
        return original(run_id)

    fake.get = get
    out = go(run, clock)
    assert (out.action, out.error) == ("restarted", False)
    assert "ran through its probation" in out.message and fake.rolled_back == []


# --- the markers, on AWS and on a local run --------------------------------------------------------


def test_a_marker_is_an_object_beside_the_job_config_and_a_hash_that_is_none_is_not_read(run):
    bucket = Bucket()
    adapter = _serverless(run, bucket)
    config_hash = run.config_hash
    uri = adapter.mark_failed(config_hash, "r1", "ended FAILED")
    assert uri == f"{run.job_uri.rsplit('/', 1)[0]}/releases/{config_hash[:16]}/failed"
    (put,) = bucket.put
    assert put["Key"].endswith(f"releases/{config_hash[:16]}/failed")
    assert json.loads(put["Body"])["runId"] == "r1"
    assert json.loads(adapter.failed_release(config_hash))["configHash"] == config_hash
    assert adapter.failed_release("c" * 64) is None  # no marker there
    for bad in (None, "", "../../x", "A" * 64, "a" * 15):
        assert adapter.failed_release(bad) is None
    adapter.clear_failed(config_hash)
    assert [d["Key"] for d in bucket.deleted] == [put["Key"]]
    adapter.clear_failed("../x")  # not a hash: nothing is deleted
    assert len(bucket.deleted) == 1


def test_a_marker_that_cannot_be_written_or_read_names_the_object_and_the_code(run):
    adapter = _serverless(run, S3(error=_client_error("AccessDenied")))
    with pytest.raises(ad.SparkRunError, match="could not be written: AccessDenied"):
        adapter.mark_failed(run.config_hash, "r1", "x")

    class Broken(Bucket):
        def get_object(self, Bucket, Key):  # noqa: N803 - boto3's names
            raise _client_error("InternalError")

    with pytest.raises(ad.SparkRunError, match="cannot be read: InternalError"):
        _serverless(run, Broken()).failed_release(run.config_hash)


def test_a_local_marker_is_a_file_under_the_artifact_root(tmp_path, monkeypatch):
    from fluid_build.build_runners.spark._local import LOCAL_ROOT_ENV, LocalHost
    from tests.build_runners.spark.conftest import runs_of
    from tests.build_runners.spark.test_local import local_contract

    root = tmp_path / "artifacts"
    monkeypatch.setenv(LOCAL_ROOT_ENV, str(root))
    monkeypatch.setenv("FLUID_SPARK_LOCAL_NAMESPACE_ALLOWLIST", "fluid/commerce")
    (local,) = runs_of(local_contract(), account=None)
    adapter = ad.adapter_for(local, None, LocalHost(tmp_path))
    config_hash = local.config_hash
    assert adapter.failed_release(config_hash) is None
    kept = adapter.mark_failed(config_hash, "r1", "ended FAILED")
    path = root / "_fluid" / "spark" / local.product / local.build / "releases"
    assert kept == str(path / config_hash[:16] / "failed")
    assert json.loads(adapter.failed_release(config_hash))["runId"] == "r1"
    assert adapter.failed_release("../x") is None
    adapter.clear_failed(config_hash)
    assert (
        adapter.failed_release(config_hash) is None
        and not (path / config_hash[:16] / "failed").exists()
    )
