# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A stream that died starts again within its restart budget, and ``fluid apply --mode
reconcile`` (``only``) restarts only what the last apply deployed, against the in-memory
adapter of ``test_reconcile.py`` and a fake wall clock."""

from __future__ import annotations

import dataclasses
import json

import pytest

from fluid_build.build_runners.spark import _reconcile as rc
from fluid_build.build_runners.spark._adapters import SERVICE_RETRIES, SparkRunError
from fluid_build.build_runners.spark._desired import ACCOUNT_TOKEN, RESTART_WINDOW_S
from fluid_build.spark.compiler import MSK_BROKERS_PLACEHOLDER
from tests.build_runners.spark.conftest import variant
from tests.build_runners.spark.test_reconcile import WALL, FakeAdapter, go, info

HOUR = RESTART_WINDOW_S
ACCOUNT = "123456789012"  # the account the use fixture binds a fake's run to


@pytest.fixture
def ec2(run):
    """The example stream on EMR on EC2, which no service retries: forge restarts it."""
    return variant(run, "emr-ec2", {"clusterId": "j-ABC"})


def died(run, run_id="s-dead", state="FAILED", *, created=WALL - 600, **kw):
    kw.setdefault("details", "driver exited 1")
    return info(run_id, state, hash_=run.config_hash, created=created, **kw)


def restarts(run, n, *, start=WALL - 3000, step=60):
    """``n`` restarts of this config that each died, newest first, the oldest at ``start``."""
    return [
        died(run, f"s-r{i}", created=start + i * step, restart_of=f"s-p{i}")
        for i in reversed(range(n))
    ]


def uploaded(run):
    """The job config as OpenTofu uploads it: the account and the MSK brokers filled in."""
    return run.job_text.replace(ACCOUNT_TOKEN, ACCOUNT).replace(
        MSK_BROKERS_PLACEHOLDER, "b-1.msk.example:9098,b-2.msk.example:9098"
    )


# --- the restart budget (a plain fluid apply and --mode reconcile alike) ---------------


@pytest.mark.parametrize("only", [False, True])
def test_a_failed_stream_on_emr_ec2_starts_again_marked_as_a_restart(ec2, clock, use, only):
    fake = FakeAdapter([died(ec2)], job=uploaded(ec2))
    use(fake)
    out = go(ec2, clock, only=only)
    assert (out.action, out.error, out.run_id, out.state) == ("restarted", False, "new", "RUNNING")
    assert fake.restart_of == ["s-dead"]
    assert "run s-dead ended FAILED: driver exited 1; started again on the same checkpoint" in (
        out.message
    )
    assert "(restart 1 of 5 allowed per rolling hour)" in out.message
    assert fake.since[0] == WALL - HOUR  # the history is read back one hour


@pytest.mark.parametrize(
    "ended",
    [
        dict(state="FAILED", raw="INTERRUPTED", details=""),  # an EMR step lost with its node
        dict(state="FAILED", raw="LOST", lost=True),  # a local run killed with its machine
        dict(state="FAILED", raw="STOPPED (signal 15)", lost=True),  # a local run's shutdown
        dict(state="SUCCEEDED", details=""),  # a stream that stopped by itself
    ],
)
def test_every_end_but_a_cancel_is_a_death_that_restarts(ec2, clock, use, ended):
    fake = FakeAdapter([died(ec2, **ended)])
    use(fake)
    out = go(ec2, clock)
    assert (out.action, out.error, fake.restart_of) == ("restarted", False, ["s-dead"])
    assert f"ended {ended.get('raw', ended['state'])}" in out.message


@pytest.mark.parametrize("only", [False, True])
def test_a_spent_budget_starts_nothing_and_says_when_it_frees(ec2, clock, use, only):
    fake = FakeAdapter(restarts(ec2, 5), job=uploaded(ec2))
    use(fake)
    out = go(ec2, clock, only=only)
    assert (out.action, out.error, out.run_id, fake.calls) == ("failed", True, "s-r4", [])
    assert "run s-r4 ended FAILED: driver exited 1. Not started again" in out.message
    assert (
        "budget of 5 restarts per rolling hour (execution.retries.maxAttempts, 5 when unset)"
        in (out.message)
    )
    assert f"s-r0 at {rc._utc(WALL - 3000)}" in out.message
    assert f"It can restart from {rc._utc(WALL - 3000 + HOUR)}" in out.message
    assert "fix the cause and change the build" in out.message
    assert "Earlier: run s-r3 ended FAILED" in out.message


def test_a_lowered_budget_frees_when_fewer_than_it_are_under_an_hour_old(ec2, clock, use):
    """execution.retries is not in an EMR on EC2 run's config hash: four restarts of the last
    hour can count against a budget lowered to two. The next restart is allowed once only
    one of them is under an hour old: at the third oldest's hour, not the oldest's."""
    lowered = dataclasses.replace(ec2, restarts_per_hour=2)
    fake = FakeAdapter(restarts(lowered, 4), job=uploaded(lowered))
    use(fake)
    out = go(lowered, clock)
    assert (out.action, out.error, fake.calls) == ("failed", True, [])
    assert f"It can restart from {rc._utc(WALL - 3000 + 2 * 60 + HOUR)}, when fewer than 2" in (
        out.message
    )


def test_restarts_older_than_an_hour_no_longer_count(ec2, clock, use):
    # s-r0 and s-r1 are more than an hour old; s-r2 is exactly an hour old and still counts.
    fake = FakeAdapter(restarts(ec2, 5, start=WALL - HOUR - 120))
    use(fake)
    out = go(ec2, clock)
    assert (out.action, out.error) == ("restarted", False)
    assert "restart 4 of 5" in out.message


def test_a_cancelled_restart_and_another_config_s_restarts_do_not_count(ec2, clock, use):
    two = dataclasses.replace(ec2, restarts_per_hour=2)
    history = [
        died(two, "s-x", created=WALL - 60, restart_of="s-w"),
        info("s-dup", "CANCELLED", hash_=two.config_hash, created=WALL - 61, restart_of="s-w"),
        info("s-old", "FAILED", hash_="older", created=WALL - 120, restart_of="s-v"),
    ]
    fake = FakeAdapter(history)
    use(fake)
    out = go(two, clock)
    assert (out.action, out.error) == ("restarted", False) and "restart 2 of 2" in out.message


def test_the_budget_follows_execution_retries(contract):
    from tests.build_runners.spark.conftest import runs_of

    build = contract["builds"][0]
    assert runs_of(contract)[0].restarts_per_hour == 5  # EMR Serverless's default
    build.setdefault("execution", {})["retries"] = {"maxAttempts": 2}
    two = runs_of(contract)[0]
    build["execution"]["retries"] = {"maxAttempts": 99}
    capped = runs_of(contract)[0]
    assert (two.restarts_per_hour, capped.restarts_per_hour) == (2, 10)
    build["properties"]["stream"]["trigger"] = {"availableNow": True}
    assert runs_of(contract)[0].restarts_per_hour == 0  # a run that ends is never restarted


def test_a_new_budget_on_emr_ec2_restarts_nothing(contract):
    """EMR on EC2 takes no retry setting: execution.retries is only the stream's restart
    budget there, a reconcile setting, so the config (and its hash) stays."""
    from tests.build_runners.spark.conftest import runs_of, spark_cfg

    spark_cfg(contract).update(runtime="emr-ec2", clusterId="j-ABC")
    spark_cfg(contract).pop("applicationId", None)
    before = runs_of(contract)[0]
    contract["builds"][0].setdefault("execution", {})["retries"] = {"maxAttempts": 2}
    after = runs_of(contract)[0]
    assert (after.restarts_per_hour, after.config_hash) == (2, before.config_hash)
    assert after.plan_action(1) == before.plan_action(1)


@pytest.mark.parametrize(
    "runtime, target",
    [
        ("emr-serverless", None),
        ("emr-eks", {"virtualClusterId": "vc1"}),
    ],
)
def test_a_failed_run_on_a_runtime_that_retries_runs_is_not_started_again(
    run, clock, use, runtime, target
):
    on = run if target is None else variant(run, runtime, target, retry_attempts=3)
    fake = FakeAdapter([died(on, attempt=6, details="OOM")])
    use(fake)
    out = go(on, clock)
    assert (out.action, out.error, fake.calls) == ("failed", True, [])
    assert SERVICE_RETRIES[runtime] in out.message
    assert "forge does not start it again on top of the service's own retries" in out.message
    unset = dataclasses.replace(on, retry_attempts=None)
    out = go(unset, clock)
    assert (out.action, out.error) == ("failed", True)
    assert "this build sets none, so it was not retried" in out.message
    assert "Set execution.retries" in out.message


def test_a_failed_glue_stream_restarts_as_glue_retries_nothing(run, clock, use):
    """forge emits the Glue job with MaxRetries 0: its reconcile is the one that retries."""
    glue = variant(run, "glue", {"glueJobName": "fluid-job"})
    fake = FakeAdapter([died(glue, attempt=1, details="boom")])
    use(fake)
    out = go(glue, clock)
    assert (out.action, out.error, fake.restart_of) == ("restarted", False, ["s-dead"])


def test_a_stream_that_ended_by_itself_where_the_service_retries_failures_restarts(run, clock, use):
    """EMR Serverless retries failures only: a stream that stopped without one is forge's."""
    fake = FakeAdapter([died(run, state="SUCCEEDED", details="")])
    use(fake)
    out = go(run, clock)
    assert (out.action, out.error, fake.restart_of) == ("restarted", False, ["s-dead"])


def test_a_plain_apply_starts_a_cancelled_stream_again_not_as_a_restart(ec2, clock, use):
    fake = FakeAdapter([info("s-c", "CANCELLED", hash_=ec2.config_hash, created=WALL - 60)])
    use(fake)
    out = go(ec2, clock)
    assert (out.action, out.error, fake.restart_of) == ("started", False, [None])


def test_two_reconciles_that_restart_at_once_keep_the_older_run(ec2, clock, use):
    twin = info("s-twin", "RUNNING", hash_=ec2.config_hash, created=WALL - 5, restart_of="s-dead")
    mine = info("new", "PENDING", hash_=ec2.config_hash, created=WALL, restart_of="s-dead")
    fake = FakeAdapter([died(ec2)], later=[mine, twin, died(ec2)])
    use(fake)
    out = go(ec2, clock)
    assert fake.calls == ["start", "cancel new"]
    assert (out.action, out.run_id, out.cancelled, out.error) == (
        "unchanged",
        "s-twin",
        ["new"],
        False,
    )
    assert "started first" in out.message


def test_a_cluster_that_ended_is_an_error_and_nothing_starts(ec2, clock, use):
    fake = FakeAdapter(
        [died(ec2)], target_error="EMR cluster j-ABC is TERMINATED_WITH_ERRORS (INSTANCE_FAILURE)"
    )
    use(fake)
    out = go(ec2, clock)
    assert (out.action, out.error, fake.calls) == ("failed", True, [])
    assert "TERMINATED_WITH_ERRORS (INSTANCE_FAILURE)" in out.message


# --- fluid apply --mode reconcile (only) ------------------------------------------------


def test_reconcile_leaves_a_cancelled_stream_stopped(ec2, clock, use):
    fake = FakeAdapter(
        [info("s-c", "CANCELLED", hash_=ec2.config_hash, created=WALL - 60)], job=uploaded(ec2)
    )
    use(fake)
    out = go(ec2, clock, only=True)
    assert (out.action, out.error, out.run_id, fake.calls) == ("left-stopped", False, "s-c", [])
    assert "leaves a cancelled stream stopped, and fluid apply starts it again" in out.message


def test_reconcile_lets_a_run_that_is_being_stopped_stop(ec2, clock, use):
    fake = FakeAdapter([info("s-1", "CANCELLING", hash_=ec2.config_hash)], job=uploaded(ec2))
    use(fake)
    out = go(ec2, clock, only=True)
    assert (out.action, out.error, fake.calls) == ("left-stopped", False, [])


def test_reconcile_of_a_healthy_stream_changes_nothing(ec2, clock, use):
    fake = FakeAdapter([info("s-1", "RUNNING", hash_=ec2.config_hash)], job=uploaded(ec2))
    use(fake)
    out = go(ec2, clock, only=True)
    assert (out.action, out.error, out.run_id, fake.calls) == ("unchanged", False, "s-1", [])


def test_reconcile_asks_before_it_restarts_and_a_no_starts_nothing(ec2, clock, use):
    asked = []
    fake = FakeAdapter([died(ec2)], job=uploaded(ec2))
    use(fake)
    out = go(ec2, clock, only=True, confirm=lambda q: asked.append(q) or False)
    assert (out.action, out.error, fake.calls) == ("declined", False, [])
    assert "run s-dead ended FAILED: driver exited 1. Start it again on its checkpoint" in asked[0]
    assert out.message == (
        "declined: not restarted, the stream stays stopped (run s-dead ended FAILED: driver "
        "exited 1)"
    )
    out = go(ec2, clock, only=True, confirm=lambda q: True)
    assert (out.action, fake.calls) == ("restarted", ["start"])


def test_reconcile_dry_run_starts_nothing(ec2, clock, use):
    fake = FakeAdapter([died(ec2)], job=uploaded(ec2))
    use(fake)
    out = go(ec2, clock, only=True, dry_run=True)
    assert (out.action, out.error, fake.calls) == ("planned", False, [])
    assert out.message.startswith(
        "would start it again on the same checkpoint: run s-dead ended FAILED"
    )


@pytest.mark.parametrize(
    "history, job, says",
    [
        # The contract changed since the last apply: the uploaded job config is the old one.
        (lambda r: [died(r)], lambda r: json.dumps({"mode": "streaming"}), "is not the one"),
        # The uploaded one is right, but the last run ran another config (another forge,
        # another FLUID_SPARK_MAVEN_REPOSITORY, a Spark setting).
        (lambda r: [info("s-old", "FAILED", hash_="older")], uploaded, "ran config older"),
        # A config the contract no longer compiles to is going: it is fluid apply's to replace.
        (lambda r: [info("s-old", "RUNNING", hash_="older")], uploaded, "of another config"),
        # Even a stream that runs: its next restart would run the job that was uploaded.
        (
            lambda r: [info("s-1", "RUNNING", hash_=r.config_hash)],
            lambda r: uploaded(r).replace('"mode"', '"mode_"'),
            "is not the one",
        ),
        (lambda r: [], uploaded, "no run of the build is recorded"),
        (lambda r: [died(r)], lambda r: None, "is not deployed"),
    ],
)
def test_reconcile_starts_and_cancels_nothing_when_the_deployment_is_not_the_contract(
    ec2, clock, use, history, job, says
):
    fake = FakeAdapter(history(ec2), job=job(ec2), uploads=True)
    use(fake)
    out = go(ec2, clock, only=True)
    assert (out.action, out.error, fake.calls) == ("refused", True, [])
    assert says in out.message and "fluid apply" in out.message


def test_reconcile_cancels_a_younger_copy_of_the_running_config(ec2, clock, use):
    fake = FakeAdapter(
        [
            info("s-2", "RUNNING", hash_=ec2.config_hash, created=WALL - 10),
            info("s-1", "RUNNING", hash_=ec2.config_hash, created=WALL - 500),
        ],
        job=uploaded(ec2),
    )
    fake.scripts["s-2"] = ["RUNNING", "CANCELLED"]
    use(fake)
    out = go(ec2, clock, only=True)
    assert (out.action, out.run_id, out.cancelled, out.error) == (
        "unchanged",
        "s-1",
        ["s-2"],
        False,
    )


def test_reconcile_never_touches_a_scheduled_build(run, clock, use):
    built = use(FakeAdapter([info("old", "RUNNING", hash_="continuous")]))
    out = go(dataclasses.replace(run, scheduled=True), clock, only=True)
    assert (out.action, out.error, built) == ("scheduled", False, [])


def test_reconcile_never_stops_a_removed_build(run, clock, use, monkeypatch):
    use(FakeAdapter([info("s-1", "RUNNING", hash_=run.config_hash)], job=uploaded(run)))
    monkeypatch.setattr(rc, "stop_removed", lambda *a, **k: pytest.fail("stopped a build"))
    rc.reconcile([run], only=True, stop_removed_of=("p", set()), wall=lambda: WALL)


def test_the_uploaded_job_is_compared_with_its_account_and_msk_brokers_put_back(ec2):
    arn = f"arn:aws:kafka:eu-west-1:{ACCOUNT_TOKEN}:cluster/lake/abc"
    text = json.dumps(
        {
            "mode": "streaming",
            "sink": {"bucket": f"fluid-{ACCOUNT_TOKEN}-lake"},
            "source": {
                "bootstrapServers": MSK_BROKERS_PLACEHOLDER,
                "mskClusterArn": arn,
                "topic": "orders",
            },
        }
    )
    run = dataclasses.replace(ec2, job_text=text).bind(account=ACCOUNT)
    deployed = json.loads(uploaded(run))
    assert deployed["sink"]["bucket"] == f"fluid-{ACCOUNT}-lake"
    assert not rc._job_differs(run, json.dumps(deployed, indent=2).encode())
    deployed["source"]["topic"] = "payments"
    assert rc._job_differs(run, json.dumps(deployed).encode())
    assert rc._job_differs(run, b"not json")
    other = dataclasses.replace(ec2, job_text=text).bind(account="210987654321")
    assert rc._job_differs(other, uploaded(run).encode())  # another account's
    deployed["source"].update(topic="orders", bootstrapServers="")
    assert rc._job_differs(run, json.dumps(deployed).encode())  # brokers the apply never wrote


def test_a_failure_text_never_carries_terminal_escape_sequences(ec2, clock, use):
    """A runtime's failure text reaches the console and the reconcile prompt: no ESC."""
    fake = FakeAdapter([died(ec2, details="boom \x1b[2J\x1b]0;owned\x07 end\x9b31m")])
    use(fake)
    out = go(ec2, clock)
    assert "\x1b" not in out.message and "\x07" not in out.message and "\x9b" not in out.message
    assert "boom [2J]0;owned end31m" in out.message


def test_a_cancelled_younger_copy_does_not_hide_the_death_of_the_run_it_copied(ec2, clock, use):
    """Two reconciles restarted run s-d at once; the younger copy s-y cancelled itself. When
    s-o, the one kept, dies, its death is the stream's end, not the copy's cancel."""
    history = [
        info("s-y", "CANCELLED", hash_=ec2.config_hash, created=WALL - 50, restart_of="s-d"),
        died(ec2, "s-o", created=WALL - 60, restart_of="s-d"),
        died(ec2, "s-d", created=WALL - 900),
    ]
    fake = FakeAdapter(history, job=uploaded(ec2))
    use(fake)
    out = go(ec2, clock, only=True)
    assert (out.action, out.error, fake.restart_of) == ("restarted", False, ["s-o"])
    assert "restart 2 of 5" in out.message  # s-o was a restart too; the cancelled copy is not


def test_a_cancelled_restart_is_the_stream_s_end(ec2, clock, use):
    """A restart started after the run it replaces ended, then cancelled by an operator: the
    stream was stopped on purpose."""
    history = [
        info("s-r", "CANCELLED", hash_=ec2.config_hash, created=WALL - 50, restart_of="s-o"),
        died(ec2, "s-o", created=WALL - 900),
    ]
    fake = FakeAdapter(history, job=uploaded(ec2))
    use(fake)
    out = go(ec2, clock, only=True)
    assert (out.action, out.error, fake.calls) == ("left-stopped", False, [])


def test_an_operator_s_cancel_of_a_restart_is_the_end_past_a_cancelled_copy(ec2, clock, use):
    """s-z died; two applies restarted it at once (s-o kept, s-y cancelled itself); s-o died
    and was restarted as s-b, which an operator cancelled. s-y, between s-b and the run it
    restarted, is a copy of s-o: it does not hide the operator's stop."""
    history = [
        info("s-b", "CANCELLED", hash_=ec2.config_hash, created=WALL - 50, restart_of="s-o"),
        info("s-y", "CANCELLED", hash_=ec2.config_hash, created=WALL - 590, restart_of="s-z"),
        died(ec2, "s-o", created=WALL - 600, restart_of="s-z"),
        died(ec2, "s-z", created=WALL - 900),
    ]
    fake = FakeAdapter(history, job=uploaded(ec2))
    use(fake)
    out = go(ec2, clock, only=True)
    assert (out.action, out.error, out.run_id, fake.calls) == ("left-stopped", False, "s-b", [])


def test_an_operator_s_cancel_of_a_start_after_another_config_is_the_end(ec2, clock, use):
    """Config X, then Y, then X again: s-a, started with no restart mark because another
    config ran before it, was cancelled by an operator. It is no copy of s-z: Y ran between."""
    history = [
        info("s-a", "CANCELLED", hash_=ec2.config_hash, created=WALL - 50),
        info("s-y", "CANCELLED", hash_="other", created=WALL - 300),
        died(ec2, "s-z", created=WALL - 900),
    ]
    fake = FakeAdapter(history, job=uploaded(ec2))
    use(fake)
    out = go(ec2, clock, only=True)
    assert (out.action, out.error, out.run_id, fake.calls) == ("left-stopped", False, "s-a", [])


def test_a_cancelled_step_on_a_cluster_that_ended_is_the_cluster_s_error(ec2, clock, use):
    """A cluster that terminates cancels the steps still pending on it: that is no
    operator's stop, and the reconcile says the cluster ended."""
    ended = "EMR cluster j-ABC is TERMINATED_WITH_ERRORS (INSTANCE_FAILURE) and takes no new step"
    history = [
        info("s-p", "CANCELLED", hash_=ec2.config_hash, created=WALL - 50, restart_of="s-o"),
        died(ec2, "s-o", created=WALL - 900),
    ]
    fake = FakeAdapter(history, job=uploaded(ec2), target_error=ended)
    use(fake)
    out = go(ec2, clock, only=True)
    assert (out.action, out.error, fake.calls) == ("failed", True, [])
    assert ended in out.message


def test_a_cancelled_copy_started_by_hand_does_not_hide_a_death(ec2, clock, use):
    history = [
        info("s-copy", "CANCELLED", hash_=ec2.config_hash, created=WALL - 50),
        died(ec2, "s-o", created=WALL - 900),
    ]
    fake = FakeAdapter(history)
    use(fake)
    out = go(ec2, clock)
    assert (out.action, fake.restart_of) == ("restarted", ["s-o"])


def test_a_read_that_fails_after_the_start_still_names_the_run_that_is_going(ec2, clock, use):
    class Throttled(FakeAdapter):
        def runs(self, since=None):
            if "start" in self.calls:
                raise SparkRunError("ThrottlingException: Rate exceeded")
            return super().runs(since)

    fake = Throttled([died(ec2)])
    use(fake)
    out = go(ec2, clock)
    assert (out.action, out.run_id, out.error) == ("restarted", "new", True)
    assert "run new was started, but checking on it failed: ThrottlingException" in out.message
    assert "It is left going" in out.message


def test_a_cancel_that_fails_names_what_was_already_cancelled(ec2, clock, use):
    class Refusing(FakeAdapter):
        def cancel(self, run_id):
            if run_id == "s-2":
                raise SparkRunError("step s-2 on j-ABC was not cancelled: gone")
            super().cancel(run_id)

    fake = Refusing(
        [
            info("s-3", "RUNNING", hash_=ec2.config_hash, created=WALL - 5),
            info("s-2", "RUNNING", hash_=ec2.config_hash, created=WALL - 10),
            info("s-1", "RUNNING", hash_=ec2.config_hash, created=WALL - 500),
        ]
    )
    use(fake)
    out = go(ec2, clock)
    assert (out.action, out.error, fake.calls) == ("failed", True, ["cancel s-3"])
    assert "cancelled s-3 before it, and started nothing" in out.message


def test_a_restart_that_fails_at_once_keeps_its_restart_count_in_the_outcome(ec2, clock, use):
    fake = FakeAdapter([died(ec2)], start_states=("FAILED",), start_details="bad conf")
    use(fake)
    out = go(ec2, clock)
    assert (out.action, out.error, out.run_id) == ("restarted", True, "new")
    assert "restart 1 of 5 allowed per rolling hour" in out.message and "bad conf" in out.message
