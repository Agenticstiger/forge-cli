# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A stream's release, second half: the new config's run is kept on probation, and a run that
fails it is replaced by the last good config, started again from its release record.

The reconcile runs against the in-memory adapter of ``test_stream_release.py``; the records
against stand-ins for S3 and the local artifact root."""

from __future__ import annotations

import dataclasses
import json
import logging

import pytest

pytest.importorskip("botocore")

from fluid_build.build_runners.spark import _adapters as ad
from fluid_build.build_runners.spark import _reconcile as rc
from fluid_build.build_runners.spark._adapters import SparkRunError
from fluid_build.build_runners.spark._desired import config_digest
from fluid_build.cli import _spark_apply
from tests.build_runners.spark.conftest import runs_of
from tests.build_runners.spark.test_reconcile import ENV, WALL, info
from tests.build_runners.spark.test_stream_release import KEY, S3, Releasing, _client_error

#: The last good config's job config, as the run that ran it read it.
OLD_JOB = json.dumps(
    {"configVersion": 1, "mode": "stream", "checkpoint": "s3://lake/_checkpoints/p/b/",
     "source": {"kind": "kafka", "topic": "commerce.orders"}}, sort_keys=True
)  # fmt: skip
OLD_DIGEST = config_digest(OLD_JOB)


def last_good(run, **kw):
    """The running run of the build's previous config: its stop key and job config digest."""
    base = info(
        "old1", "RUNNING", hash_="stale", source=run.source_id, checkpoint=run.checkpoint_id, **kw
    )
    return dataclasses.replace(base, stop_key=KEY, job_sha256=OLD_DIGEST)


class Rolling(Releasing):
    """The release adapter with release records: ``records`` maps a job config digest to the
    bytes kept for it; the first start makes run ``new`` (``start_states``), a start after a
    rollback makes ``rb`` (``rollback_states``) on the run :meth:`sibling` was handed."""

    def __init__(self, *args, records=None, rollback_states=("RUNNING",), keep_error=None, **kw):
        super().__init__(*args, **kw)
        self.records = dict(records or {})
        self.rollback_states = list(rollback_states)
        self.keep_error = keep_error
        self.kept, self.rolled_back = [], []

    def checked(self):  # as a real adapter's: with the digest of the job config it read
        return dataclasses.replace(self.run, job_sha256=config_digest(self.run.job_text))

    def keep_release(self, run):
        if self.keep_error:
            raise SparkRunError(self.keep_error)
        self.kept.append(run.job_sha256)
        return ad.release_config_uri(run.job_uri, run.job_sha256)

    def release_config(self, run_info):
        body = self.records.get(run_info.job_sha256)
        return None if body is None else body.encode("utf-8")

    def sibling(self, run):
        self.rolled_back.append(run)
        return self

    def runs(self, since=None):  # as a runtime lists them: each run in its state now
        listed = super().runs(since)
        return [
            dataclasses.replace(h, state=self.scripts.get(h.run_id, [h.state])[0]) for h in listed
        ]

    def start(self, token, restart_of=None):
        if "start" in self.calls:  # the rollback's
            self.calls.append("start rollback")
            self.scripts["rb"] = list(self.rollback_states)
            return "rb"
        return super().start(token, restart_of)


def go(run, clock, env=None, **kw):
    kw.setdefault("wall", lambda: WALL)
    env = {**ENV, rc.STOP_GRACE_ENV: "30", rc.PROBATION_ENV: "60", **(env or {})}
    return rc.reconcile([run], sleep=clock.sleep, now=clock.now, env=env, **kw)[0]


def started(run):
    return dataclasses.replace(run, job_sha256=config_digest(run.job_text))


def test_a_release_that_runs_through_its_probation_is_kept(run, clock, use):
    fake = Rolling([last_good(run)], answers={"old1"}, records={OLD_DIGEST: OLD_JOB})
    use(fake)
    out = go(run, clock)
    assert (out.action, out.run_id, out.error) == ("restarted", "new", False)
    assert "ran through its probation (60 s)" in out.message
    assert "start rollback" not in fake.calls and fake.rolled_back == []
    assert fake.kept == [started(run).job_sha256]  # its own record, for the next release
    assert sum(clock.slept) >= 60


@pytest.mark.parametrize(
    ("states", "says"),
    [
        (("RUNNING", "RUNNING", "FAILED"), "ended FAILED"),  # in its first batches
        (("FAILED",), "ended FAILED"),  # at once, before it was seen running
    ],
)
def test_a_release_that_fails_its_probation_is_rolled_back_to_the_last_good_config(
    run, clock, use, states, says
):
    fake = Rolling(
        [last_good(run)], answers={"old1"}, start_states=states, records={OLD_DIGEST: OLD_JOB}
    )
    use(fake)
    out = go(run, clock)
    assert (out.action, out.error, out.run_id, out.state) == ("rolled-back", True, "rb", "RUNNING")
    assert "run new" in out.message and says in out.message
    assert "rolled back to config stale: run rb" in out.message
    (back,) = fake.rolled_back
    # The last good job config, with the shipped runner and the settings this apply compiled.
    assert back.job_uri == ad.release_config_uri(run.job_uri, OLD_DIGEST)
    assert (back.job_text, back.job_sha256) == (OLD_JOB, OLD_DIGEST)
    assert (back.source_id, back.checkpoint_id) == (run.source_id, run.checkpoint_id)
    # Neither config: the old job config with this apply's runner and settings.
    import hashlib

    label = hashlib.sha256(f"rollback:stale:{run.config_hash}".encode()).hexdigest()
    assert back.config_hash == label
    # The failed release's own stamps are not the rolled-back run's; this apply's run id is.
    assert (back.release_id, back.contract_digest, back.bundle_digest, back.product_version) == (
        None,
        None,
        None,
        None,
    )
    assert back.run_id == run.run_id
    assert back.checkpoint == "s3://lake/_checkpoints/p/b/"
    assert (back.runner_uri, back.role_arn, back.target) == (
        run.runner_uri,
        run.role_arn,
        run.target,
    )
    assert "cancel new" not in fake.calls  # it had ended


def test_a_service_retry_in_probation_is_a_failure_and_the_retrying_run_is_cancelled(
    run, clock, use
):
    fake = Rolling([last_good(run)], answers={"old1"}, records={OLD_DIGEST: OLD_JOB})
    use(fake)
    retried = iter([1, 1, 2])

    def get(run_id, original=fake.get):
        seen = original(run_id)
        if run_id == "new":
            return dataclasses.replace(seen, attempt=next(retried, 2))
        return seen

    fake.get = get
    out = go(run, clock)
    assert out.action == "rolled-back" and "attempt 2" in out.message
    assert fake.calls.index("cancel new") < fake.calls.index("start rollback")


def test_without_a_record_of_the_last_good_config_nothing_is_rolled_back(run, clock, use):
    fake = Rolling([last_good(run)], answers={"old1"}, start_states=("RUNNING", "FAILED"))
    use(fake)
    out = go(run, clock)
    assert (out.action, out.error, fake.rolled_back) == ("failed", True, [])
    assert "no release record" in out.message and "fluid apply" in out.message


def test_a_record_that_is_not_what_the_last_good_run_read_is_never_started(run, clock, use):
    """The record is checked against the digest the runtime recorded when forge started the old
    run (``--fluid-job-sha256``): bytes put there since run nothing."""
    planted = OLD_JOB.replace("commerce.orders", "payments.events")
    fake = Rolling(
        [last_good(run)], answers={"old1"}, start_states=("FAILED",), records={OLD_DIGEST: planted}
    )
    use(fake)
    out = go(run, clock)
    assert (out.action, out.error, fake.rolled_back) == ("failed", True, [])
    assert "is not the job config run old1 ran" in out.message


def test_a_probation_of_zero_keeps_a_release_as_it_was(run, clock, use):
    fake = Rolling(
        [last_good(run)], answers={"old1"}, start_states=("RUNNING",), records={OLD_DIGEST: OLD_JOB}
    )
    use(fake)
    out = go(run, clock, env={rc.PROBATION_ENV: "0"})
    assert (out.action, out.error) == ("restarted", False) and "probation" not in out.message
    assert fake.kept == [started(run).job_sha256]


def test_a_first_start_keeps_its_record_and_a_record_that_cannot_be_kept_is_a_note(run, clock, use):
    fake = Rolling()
    use(fake)
    out = go(run, clock)
    assert (out.action, out.error, fake.kept) == ("started", False, [started(run).job_sha256])
    refused = Rolling(keep_error="s3://lake/...: AccessDenied")
    use(refused)
    out = go(run, clock)
    assert (out.action, out.error) == ("started", False)
    assert "no release record was kept" in out.message and "AccessDenied" in out.message


@pytest.mark.parametrize(
    ("raw", "window"), [(None, 300.0), ("45", 45.0), ("0", 0.0), ("-3", 300.0), ("nan", 300.0)]
)
def test_the_probation_is_the_operator_s_setting_or_five_minutes(raw, window):
    assert rc.release_probation({} if raw is None else {rc.PROBATION_ENV: raw}) == window


def test_a_project_dotenv_may_tune_the_probation():
    from fluid_build.credentials.protected_env import DOTENV_LOADABLE

    assert rc.PROBATION_ENV in DOTENV_LOADABLE


def test_a_rollback_is_its_own_event_and_fails_the_apply(run, use, caplog):
    fake = Rolling(
        [last_good(run)], answers={"old1"}, start_states=("FAILED",), records={OLD_DIGEST: OLD_JOB}
    )
    use(fake)
    monkey_env = {rc.STOP_GRACE_ENV: "1", rc.PROBATION_ENV: "1", rc.WAIT_ENV: "2"}
    log = logging.getLogger("test.spark.rollback")
    with pytest.MonkeyPatch.context() as mp, caplog.at_level(logging.INFO, logger=log.name):
        for name, value in monkey_env.items():
            mp.setenv(name, value)
        mp.setattr(rc.time, "sleep", lambda s: None)
        assert _spark_apply.reconcile_after_apply([run], log, dry_run=False) == 1
    assert "spark_release_rolled_back" in [r.getMessage() for r in caplog.records]


# --- records on AWS -----------------------------------------------------------------------


class Bucket(S3):
    """The artifact bucket: what was put, and objects to read back by key."""

    def __init__(self, objects=None, error=None):
        super().__init__(error=error)
        self.objects = dict(objects or {})

    def put_object(self, **kw):
        out = super().put_object(**kw)
        self.objects[kw["Key"]] = kw["Body"]
        return out

    def get_object(self, Bucket, Key):  # noqa: N803 - boto3's names
        import io

        if Key not in self.objects:
            raise _client_error("NoSuchKey")
        return {"Body": io.BytesIO(self.objects[Key])}


def _serverless(run, bucket):
    import boto3

    clients = {
        "emr-serverless": boto3.client("emr-serverless", region_name=run.region),
        "s3": bucket,
    }
    target = dataclasses.replace(run, target={"applicationId": "app1"})
    return ad.adapter_for(target, lambda service, region: clients[service])


def test_a_record_holds_the_bytes_the_run_read_beside_the_job_config(run):
    uploaded = run.job_text.encode("utf-8")
    _, _, job_key = run.job_uri.removeprefix("s3://").partition("/")
    bucket = Bucket({job_key: uploaded})
    adapter = _serverless(run, bucket)
    adapter._checked_body = uploaded  # what checked() read back before the start
    kept = adapter.keep_release(started(run))
    sha = started(run).job_sha256
    assert kept == f"{run.job_uri.rsplit('/', 1)[0]}/releases/{sha[:16]}/job.json"
    (put,) = bucket.put
    assert put["Key"].endswith(f"releases/{sha[:16]}/job.json") and put["Body"] == uploaded
    old = dataclasses.replace(last_good(run), job_sha256=sha)
    assert adapter.release_config(old) == uploaded
    assert adapter.release_config(dataclasses.replace(old, job_sha256="ab" * 32)) is None
    assert adapter.release_config(dataclasses.replace(old, job_sha256="../x")) is None


def test_a_rolled_back_run_is_asked_to_stop_beside_its_record(run):
    bucket = Bucket()
    adapter = _serverless(run, bucket)
    record = ad.release_config_uri(run.job_uri, OLD_DIGEST)
    uri = adapter.request_stop(dataclasses.replace(last_good(run), config_uri=record))
    assert uri == ad.stop_request_uri(record, KEY)
    with pytest.raises(SparkRunError):  # a record of another build is not this build's
        other = record.replace("/ingest-orders/", "/another-build/")
        adapter.request_stop(dataclasses.replace(last_good(run), config_uri=other))


def test_a_run_s_job_config_digest_is_read_back_from_its_arguments(run):
    assert ad._identity(started(run).entry_args())["job_sha256"] == started(run).job_sha256


# --- records on a local run ----------------------------------------------------------------


def test_a_local_record_is_a_file_under_the_artifact_root(tmp_path, monkeypatch):
    from fluid_build.build_runners.spark._local import LOCAL_ROOT_ENV, LocalHost
    from tests.build_runners.spark.test_local import local_contract

    root = tmp_path / "artifacts"
    monkeypatch.setenv(LOCAL_ROOT_ENV, str(root))
    monkeypatch.setenv("FLUID_SPARK_LOCAL_NAMESPACE_ALLOWLIST", "fluid/commerce")
    (local,) = runs_of(local_contract(), account=None)
    adapter = ad.adapter_for(local, None, LocalHost(tmp_path))
    bound = dataclasses.replace(local, job_sha256=config_digest(local.job_text))
    kept = adapter.keep_release(bound)
    path = root / "_fluid" / "spark" / local.product / local.build / "releases"
    assert kept == str(path / bound.job_sha256[:16] / "job.json")
    old = dataclasses.replace(last_good(local), job_sha256=bound.job_sha256)
    assert adapter.release_config(old).decode("utf-8") == local.job_text.replace(
        "${fluid_spark_local_root}", str(root)
    )


# --- what a probation does in the cases around it ---------------------------------------------


def _failing_reads(fake, run_id, after):
    """``fake.get`` that raises for ``run_id`` from its ``after``-th read on."""
    seen = {"n": 0}
    original = fake.get

    def get(read_id):
        if read_id == run_id:
            seen["n"] += 1
            if seen["n"] > after:
                raise RuntimeError("Throttling")
        return original(read_id)

    fake.get = get


def test_a_probation_read_that_fails_keeps_the_release_and_names_the_run(run, clock, use):
    fake = Rolling([last_good(run)], answers={"old1"}, records={OLD_DIGEST: OLD_JOB})
    use(fake)
    _failing_reads(fake, "new", after=1)  # the start's read works, the probation's do not
    out = go(run, clock)
    assert (out.action, out.run_id, out.error, out.stopped) == ("restarted", "new", True, ["old1"])
    assert "during its probation failed" in out.message and "left going" in out.message
    assert fake.rolled_back == []


def test_a_rollback_run_that_cannot_be_read_back_is_still_named(run, clock, use):
    fake = Rolling(
        [last_good(run)], answers={"old1"}, start_states=("FAILED",), records={OLD_DIGEST: OLD_JOB}
    )
    use(fake)
    _failing_reads(fake, "rb", after=0)
    out = go(run, clock)
    assert (out.action, out.run_id, out.error) == ("rolled-back", "rb", True)
    assert "run rb was started from its release record, but checking on it failed" in out.message


def test_a_previous_config_that_ended_succeeded_is_watched_and_rolled_back_to(run, clock, use):
    previous = dataclasses.replace(last_good(run), state="SUCCEEDED", raw_state="SUCCEEDED")
    fake = Rolling([previous], start_states=("FAILED",), records={OLD_DIGEST: OLD_JOB})
    use(fake)
    out = go(run, clock)
    assert (out.action, out.run_id, out.code) == ("rolled-back", "rb", None)
    assert len(fake.rolled_back) == 1


def test_a_previous_config_this_apply_stopped_is_rolled_back_to(run, clock, use):
    """Running when this apply replaced it (asked to stop between batches, or cancelled): the
    release's own stop does not make it a config to leave."""
    for grace, answers in (("30", {"old1"}), ("0", set())):
        fake = Rolling(
            [last_good(run)], answers=answers, start_states=("FAILED",),
            records={OLD_DIGEST: OLD_JOB},
        )  # fmt: skip
        use(fake)
        out = go(run, clock, env={rc.STOP_GRACE_ENV: grace})
        assert (out.action, out.code, len(fake.rolled_back)) == ("rolled-back", None, 1)


def test_a_previous_config_cancelled_before_this_apply_is_no_config_to_go_back_to(run, clock, use):
    """An operator may have cancelled it because it wrote bad data: the new run is watched, and
    when it fails nothing is rolled back and the stream is down."""
    gone = dataclasses.replace(last_good(run), state="CANCELLED", raw_state="CANCELLED")
    fake = Rolling(
        [gone], start_states=("RUNNING", "RUNNING", "FAILED"), records={OLD_DIGEST: OLD_JOB}
    )
    use(fake)
    out = go(run, clock)
    assert (out.action, out.error, out.code, fake.rolled_back) == (
        "failed",
        True,
        rc.NO_GOOD_CONFIG,
        [],
    )
    assert "ended CANCELLED before this apply, which is no config to go back to" in out.message
    assert "the stream is down" in out.message and run.config_hash in fake.failed


@pytest.mark.parametrize("ended", ["FAILED", "LOST"])
def test_a_previous_config_that_died_is_no_config_to_go_back_to(run, clock, use, ended):
    """A run that failed (or was lost with its machine) would otherwise be started again past
    its restart budget by a release that failed: the new run is watched, nothing is rolled
    back, and the apply fails with a typed error that says the stream is down."""
    dead = dataclasses.replace(
        last_good(run), state="FAILED", raw_state=ended, lost=ended == "LOST"
    )
    fake = Rolling(
        [dead], start_states=("RUNNING", "RUNNING", "FAILED"), records={OLD_DIGEST: OLD_JOB}
    )
    use(fake)
    out = go(run, clock)
    assert (out.action, out.error, out.code, fake.rolled_back) == (
        "failed",
        True,
        rc.NO_GOOD_CONFIG,
        [],
    )
    assert "start rollback" not in fake.calls and "cancel new" not in fake.calls
    assert f"ended {ended} before this apply, which is no config to go back to" in out.message
    assert "the stream is down" in out.message
    assert run.config_hash in fake.failed  # the config that failed is marked all the same


def test_a_previous_config_that_failed_its_own_probation_is_no_config_to_go_back_to(
    run, clock, use
):
    gone = dataclasses.replace(
        last_good(run), state="SUCCEEDED", raw_state="SUCCEEDED", config_hash="b" * 64
    )
    fake = Rolling([gone], start_states=("FAILED",), records={OLD_DIGEST: OLD_JOB})
    fake.failed["b" * 64] = b"{}"
    use(fake)
    out = go(run, clock)
    assert (out.action, out.code, fake.rolled_back) == ("failed", rc.NO_GOOD_CONFIG, [])
    assert "failed its own probation" in out.message


def test_a_first_release_of_a_stream_with_no_previous_config_is_not_watched(run, clock, use):
    fake = Rolling(start_states=("RUNNING", "RUNNING", "FAILED"))
    use(fake)
    out = go(run, clock)
    assert (out.action, out.code, out.error) == ("started", None, False)
    assert sum(clock.slept) == 0 and fake.failed == {}


def test_a_release_that_changed_only_settings_or_the_runner_rolls_nothing_back(run, clock, use):
    same = dataclasses.replace(last_good(run), job_sha256=started(run).job_sha256)
    fake = Rolling([same], answers={"old1"}, start_states=("FAILED",), records={})
    use(fake)
    out = go(run, clock)
    assert (out.action, out.error, fake.rolled_back) == ("failed", True, [])
    assert "changed only the run's settings or runner" in out.message


@pytest.mark.parametrize("stop", ["CANCELLED", "SUCCEEDED"])
def test_a_run_stopped_from_outside_during_its_probation_is_not_rolled_back(run, clock, use, stop):
    fake = Rolling(
        [last_good(run)],
        answers={"old1"},
        start_states=("RUNNING", "RUNNING", stop),
        records={OLD_DIGEST: OLD_JOB},
    )
    use(fake)
    out = go(run, clock)
    assert (out.action, out.state, fake.rolled_back) == ("restarted", stop, [])
    assert "stopped during its probation" in out.message


def test_no_rollback_starts_beside_a_run_another_apply_started(run, clock, use):
    other = info("c1", "RUNNING", hash_="config-c", created=WALL)
    fake = Rolling(
        [last_good(run)],
        answers={"old1"},
        start_states=("FAILED",),
        records={OLD_DIGEST: OLD_JOB},
        later=[other, last_good(run)],
    )
    use(fake)
    out = go(run, clock)
    assert (out.action, fake.rolled_back) == ("failed", [])
    assert "run c1 is going on the checkpoint" in out.message


def test_an_unchanged_run_gets_the_record_a_pending_start_did_not_keep(run, clock, use):
    fake = Rolling([info("jr1", "RUNNING", hash_=run.config_hash)])
    use(fake)
    out = go(run, clock)
    assert (out.action, fake.kept) == ("unchanged", [started(run).job_sha256])
    fake = Rolling([info("jr1", "RUNNING", hash_=run.config_hash)])
    use(fake)
    go(run, clock, only=True)  # --mode reconcile keeps none: its role may not write one
    assert fake.kept == []


def test_a_reconcile_restart_keeps_no_record(run, clock, use):
    from tests.build_runners.spark.conftest import variant

    ec2 = variant(run, "emr-ec2", {"clusterId": "j-1"}, restarts_per_hour=5)
    died = info("s-1", "FAILED", hash_=ec2.config_hash, created=WALL - 60)
    fake = Rolling([died])
    fake.uploads_job = False
    use(fake)
    out = go(ec2, clock, only=True)
    assert out.action == "restarted" and fake.kept == []


@pytest.mark.parametrize(
    ("raw", "seconds"), [("86400", 3600.0), ("1e12", 3600.0), ("3599", 3599.0)]
)
def test_a_grace_or_probation_from_a_project_file_is_capped_at_an_hour(raw, seconds):
    assert rc.stop_grace({rc.STOP_GRACE_ENV: raw}) == seconds
    assert rc.release_probation({rc.PROBATION_ENV: raw}) == seconds


def test_a_scheduled_run_never_starts_beside_a_continuous_run_still_stopping(run, clock, use):
    """A stream moved to trigger.availableNow: while fluid apply waits for the old continuous run
    to stop between batches, its scheduler's run must not become a second writer."""
    available_now = dataclasses.replace(run, scheduled=True)
    continuous = info("old1", "RUNNING", hash_="stale", checkpoint=run.checkpoint_id)
    fake = Rolling([continuous])
    use(fake)
    out = rc.run_once(available_now, sleep=clock.sleep, now=clock.now, say=lambda _t: None)
    assert (out.action, out.error, out.run_id, fake.calls) == ("refused", True, "old1", [])
    assert "still going on the checkpoint" in out.message
