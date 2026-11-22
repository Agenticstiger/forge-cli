# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A stream's release, on forge's side: a changed stream is asked to stop between batches
before its new config starts on the same checkpoint, and is cancelled when it does not stop.

The reconcile runs against the in-memory adapter of ``test_reconcile.py``; the adapters'
stop requests against stand-ins for S3 and the local artifact root."""

from __future__ import annotations

import dataclasses
import importlib.util
import json

import pytest

pytest.importorskip("botocore")

from fluid_build.build_runners.spark import _adapters as ad
from fluid_build.build_runners.spark import _reconcile as rc
from fluid_build.build_runners.spark._adapters import RunInfo, SparkRunError
from fluid_build.build_runners.spark._desired import STOP_KEY_FLAG
from tests.build_runners.spark.conftest import runs_of, variant
from tests.build_runners.spark.test_reconcile import ENV, WALL, FakeAdapter, info
from tests.spark.conftest import RUNNER_PATH

KEY = "fluid-0123456789abcdef-0123456789abcdef0123"


def asked(run_id, state="RUNNING", *, key=KEY, **kw):
    """A run of another config, started with a stop key (``key=None``: an older forge's)."""
    kw.setdefault("hash_", "stale")
    return dataclasses.replace(info(run_id, state, **kw), stop_key=key)


class Releasing(FakeAdapter):
    """The in-memory adapter, with stop requests: ``answers`` holds the runs whose runner
    honours one (they end SUCCEEDED once asked); ``refuse`` is the error a write raises. A
    cancel ends a run (CANCELLED) on its next read."""

    def __init__(self, *args, answers=(), refuse=None, **kw):
        super().__init__(*args, **kw)
        self.answers, self.refuse = set(answers), refuse
        self.requests, self.withdrawn = [], []

    def request_stop(self, run_info):
        if not run_info.stop_key:
            return None
        if self.refuse:
            raise SparkRunError(self.refuse)
        uri = f"s3://commerce-lake/_fluid/spark/p/b/stop/{run_info.stop_key}"
        self.calls.append(f"ask {run_info.run_id}")
        self.requests.append(uri)
        if run_info.run_id in self.answers:
            self.scripts[run_info.run_id] = ["RUNNING", "RUNNING", "SUCCEEDED"]
        return uri

    def withdraw_stop(self, uri):
        self.calls.append("withdraw")
        self.withdrawn.append(uri)

    def cancel(self, run_id):
        super().cancel(run_id)
        self.scripts[run_id] = ["CANCELLED"]


def go(run, clock, env=None, **kw):
    kw.setdefault("wall", lambda: WALL)
    env = {**ENV, rc.STOP_GRACE_ENV: "30", **(env or {})}
    return rc.reconcile([run], sleep=clock.sleep, now=clock.now, env=env, **kw)[0]


def stale_of(run, run_id="old1", **kw):
    """A run of the build's previous config, on the same checkpoint and stream."""
    kw.setdefault("source", run.source_id)
    kw.setdefault("checkpoint", run.checkpoint_id)
    return asked(run_id, **kw)


# --- the reconcile -----------------------------------------------------------------------


def test_a_changed_stream_stops_between_batches_and_restarts_on_its_checkpoint(run, clock, use):
    fake = Releasing([stale_of(run)], answers={"old1"})
    use(fake)
    out = go(run, clock)
    assert (out.action, out.run_id, out.error) == ("restarted", "new", False)
    assert (out.stopped, out.cancelled) == (["old1"], [])
    assert fake.calls == ["ask old1", "withdraw", "start"]
    assert fake.withdrawn == fake.requests  # the request does not outlive the stop
    assert "run old1 stopped between batches" in out.message
    assert f"stale -> {run.config_hash[:12]}" in out.message  # the configs it moved between
    assert out.as_dict()["stopped"] == ["old1"]


def test_a_run_that_does_not_stop_within_the_grace_is_cancelled(run, clock, use):
    fake = Releasing([stale_of(run)])
    use(fake)
    out = go(run, clock)
    assert (out.action, out.error, out.stopped, out.cancelled) == (
        "restarted",
        False,
        [],
        ["old1"],
    )
    assert fake.calls == ["ask old1", "withdraw", "cancel old1", "start"]
    assert "did not stop between batches within 30 s" in out.message
    assert rc.STOP_GRACE_ENV in out.message
    assert 30 <= sum(clock.slept) < 45  # the grace, then the cancel's wait


def test_a_run_an_older_forge_started_has_no_stop_key_and_is_cancelled_at_once(run, clock, use):
    fake = Releasing([stale_of(run, key=None)])
    use(fake)
    out = go(run, clock)
    assert (out.action, out.cancelled, fake.calls) == (
        "restarted",
        ["old1"],
        ["cancel old1", "start"],
    )
    assert "started without a stop key" in out.message


def test_a_grace_of_zero_cancels_at_once(run, clock, use):
    fake = Releasing([stale_of(run)], answers={"old1"})
    use(fake)
    out = go(run, clock, env={rc.STOP_GRACE_ENV: "0"})
    assert (out.cancelled, fake.calls) == (["old1"], ["cancel old1", "start"])


def test_a_stop_request_that_cannot_be_written_falls_back_to_a_cancel(run, clock, use):
    fake = Releasing([stale_of(run)], refuse="s3://commerce-lake/...: AccessDenied")
    use(fake)
    out = go(run, clock)
    assert (out.action, out.error, out.cancelled) == ("restarted", False, ["old1"])
    assert fake.calls == ["cancel old1", "start"]
    assert "AccessDenied" in out.message and "cancelled" in out.message


def test_a_second_copy_of_the_new_config_is_cancelled_never_asked(run, clock, use):
    """Two writers on one checkpoint: the younger copy is stopped at once."""
    fake = Releasing(
        [
            asked("jr2", hash_=run.config_hash, created=5.0, key="fluid-k2"),
            asked("jr1", hash_=run.config_hash, created=1.0, key="fluid-k1"),
        ]
    )
    use(fake)
    out = go(run, clock)
    assert (out.action, out.run_id, fake.calls) == ("unchanged", "jr1", ["cancel jr2"])


def test_a_dry_run_says_which_runs_would_stop_between_batches(run, clock, use):
    fake = Releasing([stale_of(run), stale_of(run, "old2", key=None)])
    use(fake)
    out = go(run, clock, dry_run=True)
    assert (out.action, fake.calls) == ("planned", [])
    assert "would stop old1 between batches" in out.message
    assert "cancel old2" in out.message and "start the new config" in out.message


def test_ctrl_c_while_a_stop_is_awaited_withdraws_the_request(run, clock, use):
    fake = Releasing([stale_of(run)])
    use(fake)
    original = fake.request_stop

    def then_interrupted(run_info):
        uri = original(run_info)
        fake.scripts[run_info.run_id] = ["RUNNING", "INTERRUPT"]
        return uri

    fake.request_stop = then_interrupted
    with pytest.raises(KeyboardInterrupt):
        go(run, clock)
    assert fake.withdrawn == fake.requests and "start" not in fake.calls


def test_a_moved_checkpoint_is_said_to_start_from_starting_offsets(run, clock, use):
    """The contract's checkpoint is the stream's position: a new one starts the job again
    from startingOffsets (``$defs.streamSource.checkpoint``), and the old one is left."""
    fake = Releasing([stale_of(run, checkpoint="0123456789abcdef")], answers={"old1"})
    use(fake)
    out = go(run, clock)
    assert out.action == "restarted"
    assert "checkpoint moved" in out.message and "startingOffsets" in out.message


def test_a_stream_moved_to_available_now_stops_its_continuous_run_between_batches(run, clock, use):
    scheduled = dataclasses.replace(run, scheduled=True)
    fake = Releasing([stale_of(run)], answers={"old1"})
    use(fake)
    out = go(scheduled, clock)
    assert (out.action, out.stopped, out.cancelled) == ("scheduled", ["old1"], [])
    assert fake.calls == ["ask old1", "withdraw"]
    assert "the continuous run old1 stopped between batches" in out.message


@pytest.mark.parametrize(
    ("raw", "grace"),
    [(None, 300.0), ("45", 45.0), ("0", 0.0), ("-1", 300.0), ("x", 300.0), ("inf", 300.0), ("nan", 300.0)],
)  # fmt: skip
def test_the_grace_is_the_operator_s_setting_or_five_minutes(raw, grace):
    assert rc.stop_grace({} if raw is None else {rc.STOP_GRACE_ENV: raw}) == grace


def test_a_project_dotenv_may_tune_the_grace_as_it_may_the_wait():
    from fluid_build.credentials.protected_env import DOTENV_LOADABLE

    assert {rc.STOP_GRACE_ENV, rc.WAIT_ENV} <= DOTENV_LOADABLE


def test_the_plan_says_a_changed_stream_stops_between_batches(run):
    impact = run.plan_action(1)["payload"]["stateImpact"]
    assert "between batches" in impact and rc.STOP_GRACE_ENV in impact


# --- the stop key and the request ----------------------------------------------------------


def _runner():
    spec = importlib.util.spec_from_file_location("forge_spark_runner", RUNNER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_forge_and_the_runner_agree_on_where_a_request_is_and_what_names_it(run):
    runner = _runner()
    assert ad.stop_request_uri(run.job_uri, KEY) == runner.stop_request_uri(run.job_uri, KEY)
    assert ad.STOP_KEY.pattern == runner._STOP_KEY.pattern
    assert STOP_KEY_FLAG == "--fluid-stop-key"


def test_a_stream_s_start_carries_its_stop_key_and_a_batch_s_none(run):
    args = run.entry_args(stop_key=KEY)
    assert args[args.index(STOP_KEY_FLAG) + 1] == KEY
    assert ad._identity(args)["stop_key"] == KEY
    batch = dataclasses.replace(
        run, scheduled=True, mode="batch", checkpoint=None, checkpoint_id=None
    )
    assert STOP_KEY_FLAG not in batch.entry_args(stop_key=KEY)
    assert "stop_key" not in ad._identity(run.entry_args())  # an older run's arguments


class S3:
    """The artifact bucket: what was put and deleted; ``error`` fails every call."""

    def __init__(self, error=None):
        self.put, self.deleted, self.error = [], [], error

    def put_object(self, **kw):
        if self.error:
            raise self.error
        self.put.append(kw)
        return {}

    def delete_object(self, **kw):
        if self.error:
            raise self.error
        self.deleted.append(kw)
        return {}


def _client_error(code):
    from botocore.exceptions import ClientError

    return ClientError({"Error": {"Code": code, "Message": "no"}}, "PutObject")


def _serverless(run, s3):
    import boto3

    target = dataclasses.replace(run, target={"applicationId": "app1"})
    clients = {"emr-serverless": boto3.client("emr-serverless", region_name=run.region), "s3": s3}
    return ad.adapter_for(target, lambda service, region: clients[service])


def test_an_aws_request_is_written_beside_the_build_s_job_config_and_withdrawn(run):
    s3 = S3()
    adapter = _serverless(run, s3)
    uri = adapter.request_stop(asked("old1"))
    bucket, _, key = run.job_uri.removeprefix("s3://").partition("/")
    assert uri == f"s3://{bucket}/{key.rsplit('/', 1)[0]}/stop/{KEY}"
    (put,) = s3.put
    assert (put["Bucket"], put["Key"]) == (bucket, f"{key.rsplit('/', 1)[0]}/stop/{KEY}")
    body = json.loads(put["Body"])
    assert body["stop"] == "between batches" and body["configHash"] == run.config_hash
    adapter.withdraw_stop(uri)
    assert s3.deleted == [{"Bucket": bucket, "Key": put["Key"]}]


@pytest.mark.parametrize("key", [None, "", "../job.json", "a/b", "fluid.k", "-k"])
def test_a_run_without_a_usable_stop_key_is_not_asked(run, key):
    s3 = S3()
    assert _serverless(run, s3).request_stop(asked("old1", key=key)) is None
    assert s3.put == []


def test_a_request_s3_refuses_names_the_error_and_a_withdraw_that_fails_is_ignored(run):
    adapter = _serverless(run, S3(error=_client_error("AccessDenied")))
    with pytest.raises(SparkRunError, match="AccessDenied"):
        adapter.request_stop(asked("old1"))
    adapter.withdraw_stop(ad.stop_request_uri(run.job_uri, KEY))  # nothing raised


def test_every_aws_runtime_asks_through_the_same_request(run):
    for runtime, target in (
        ("glue", {"glueJobName": "fluid-job"}),
        ("emr-ec2", {"clusterId": "j-ABC"}),
        ("emr-eks", {"virtualClusterId": "vc1"}),
    ):
        s3 = S3()
        adapter = ad.adapter_for(
            variant(run, runtime, target),
            lambda service, region: s3 if service == "s3" else object(),
        )
        assert adapter.request_stop(asked("old1")) == ad.stop_request_uri(run.job_uri, KEY)
        assert len(s3.put) == 1


# --- a local run -----------------------------------------------------------------------------


def test_a_local_request_is_a_file_under_the_artifact_root_and_withdrawn(tmp_path, monkeypatch):
    from fluid_build.build_runners.spark._local import LOCAL_ROOT_ENV, LocalHost
    from tests.build_runners.spark.test_local import local_contract

    root = tmp_path / "artifacts"
    monkeypatch.setenv(LOCAL_ROOT_ENV, str(root))
    monkeypatch.setenv("FLUID_SPARK_LOCAL_NAMESPACE_ALLOWLIST", "fluid/commerce")
    (local,) = runs_of(local_contract(), account=None)
    adapter = ad.adapter_for(local, None, LocalHost(tmp_path))
    uri = adapter.request_stop(asked("20261122T000000Z-0123abcd"))
    expected = root / "_fluid" / "spark" / local.product / local.build / "stop" / KEY
    assert uri == str(expected) and json.loads(expected.read_text())["stop"] == "between batches"
    adapter.withdraw_stop(uri)
    assert not expected.exists()
    adapter.withdraw_stop(uri)  # already gone: nothing raised
