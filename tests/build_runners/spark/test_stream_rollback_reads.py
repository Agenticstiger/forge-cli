# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A failed-release marker read on an identity without ``s3:ListBucket`` (a missing key answers
AccessDenied), sink names compared without regard to case, and which superseded runs are a
config to go back to."""

from __future__ import annotations

import dataclasses
import json

import pytest

pytest.importorskip("botocore")

from fluid_build.build_runners.spark import _adapters as ad
from fluid_build.build_runners.spark import _reconcile as rc
from tests.build_runners.spark.conftest import variant
from tests.build_runners.spark.test_reconcile import WALL, info
from tests.build_runners.spark.test_stream_release import _client_error
from tests.build_runners.spark.test_stream_rollback import (
    OLD_DIGEST,
    OLD_JOB,
    Bucket,
    Rolling,
    _serverless,
    go,
    last_good,
)
from tests.build_runners.spark.test_stream_rollback_safety import moved_from, with_dead_letter


@pytest.fixture(autouse=True)
def _warn_again():
    ad._WARNED_PREFIXES.clear()


class Prefix(Bucket):
    """A bucket whose identity has s3:GetObject but not s3:ListBucket: a missing key answers
    AccessDenied. ``denied`` refuses every read (no GetObject either)."""

    def __init__(self, objects=None, denied=False):
        super().__init__(objects)
        self.denied = denied

    def get_object(self, Bucket, Key):  # noqa: N803 - boto3's names
        if self.denied or Key not in self.objects:
            raise _client_error("AccessDenied")
        return super().get_object(Bucket, Key)


def _adapter(run, markers=(), denied=False):
    """The real adapter's marker read, over a bucket holding the job config and ``markers``."""
    _, _, job_key = run.job_uri.removeprefix("s3://").partition("/")
    objects = {job_key: run.job_text.encode("utf-8")}
    for config_hash in markers:
        objects[f"{job_key.rsplit('/', 1)[0]}/releases/{config_hash[:16]}/failed"] = b"{}"
    return _serverless(run, Prefix(objects, denied))


def test_a_marker_refused_while_the_job_config_reads_is_not_read_as_missing(run):
    adapter = _adapter(run)
    with pytest.raises(ad.MarkerReadDenied) as raised:
        adapter.failed_release(run.config_hash)
    error = raised.value
    assert error.job_config_readable
    assert error.uri == ad.failed_release_uri(run.job_uri, run.config_hash)
    assert error.uri in str(error)  # it names the key that was refused


def test_no_list_bucket_and_no_marker_refuses_every_start_without_the_override(run, clock, use):
    for dry_run in (False, True):
        fake = Rolling()
        fake.failed_release = _adapter(run).failed_release
        use(fake)
        out = go(run, clock, dry_run=dry_run)
        assert (out.action, out.error, out.code) == ("refused", True, rc.READ_DENIED)
        assert fake.calls == []
        assert ad.failed_release_uri(run.job_uri, run.config_hash) in out.message
        assert rc.ACCEPT_FAILED_ENV in out.message and "nothing was started" in out.message
    ec2 = variant(run, "emr-ec2", {"clusterId": "j-1"}, restarts_per_hour=5)
    fake = Rolling([info("s-1", "FAILED", hash_=ec2.config_hash, created=WALL - 60)])
    fake.uploads_job = False
    fake.failed_release = _adapter(run).failed_release
    use(fake)
    out = go(ec2, clock, only=True)
    assert (out.action, out.code, fake.calls) == ("refused", rc.READ_DENIED, [])


def test_the_override_starts_a_config_whose_marker_is_refused_and_warns_once(
    run, clock, use, caplog
):
    override = {rc.ACCEPT_FAILED_ENV: "1"}
    with caplog.at_level("WARNING", logger=ad.__name__):
        for dry_run in (True, False):
            fake = Rolling()
            fake.failed_release = _adapter(run).failed_release
            use(fake)
            out = go(run, clock, env=override, dry_run=dry_run)
            assert (out.error, out.code) == (False, None)
            assert out.action == ("planned" if dry_run else "started")
    (warning,) = [r for r in caplog.records if r.getMessage() == "spark_marker_read_denied"]
    assert "s3:ListBucket" in warning.hint and "/_fluid/spark/" in warning.hint


class Listing(Bucket):
    """A bucket whose identity can list: a key that is not there answers NoSuchKey."""

    def get_object(self, Bucket, Key):  # noqa: N803 - boto3's names
        if Key not in self.objects:
            raise _client_error("NoSuchKey")
        return super().get_object(Bucket, Key)


def test_a_marker_that_is_missing_on_a_listing_bucket_needs_no_override(run, clock, use):
    _, _, job_key = run.job_uri.removeprefix("s3://").partition("/")
    fake = Rolling()
    fake.failed_release = _serverless(
        run, Listing({job_key: run.job_text.encode("utf-8")})
    ).failed_release
    use(fake)
    out = go(run, clock)
    assert (out.action, out.error, out.code) == ("started", False, None)


def test_no_list_bucket_and_a_marker_that_is_there_is_refused_as_before(run, clock, use):
    fake = Rolling([info("old1", "CANCELLED", hash_=run.config_hash, created=WALL - 60)])
    fake.failed_release = _adapter(run, markers=[run.config_hash]).failed_release
    use(fake)
    out = go(run, clock)
    assert (out.action, out.code, fake.calls) == ("refused", rc.FAILED_EARLIER, [])


def test_a_prefix_that_cannot_be_read_at_all_is_refused_with_the_grants_to_give(run, clock, use):
    fake = Rolling()
    fake.failed_release = _adapter(run, denied=True).failed_release
    use(fake)
    out = go(run, clock, env={rc.ACCEPT_FAILED_ENV: "1"})  # the override does not cover this
    assert (out.action, out.error, out.code) == ("refused", True, rc.READ_DENIED)
    assert fake.calls == []
    assert "s3:GetObject" in out.message and "s3:ListBucket" in out.message
    assert "nothing was started" in out.message


class Throttled(Prefix):
    """The marker is refused; reading the job config answers a throttle."""

    def get_object(self, Bucket, Key):  # noqa: N803 - boto3's names
        if Key.endswith("job.json"):
            raise _client_error("SlowDown")
        raise _client_error("AccessDenied")


def test_a_throttled_probe_is_not_blamed_on_get_object(run):
    _, _, job_key = run.job_uri.removeprefix("s3://").partition("/")
    adapter = _serverless(run, Throttled({job_key: b"{}"}))
    with pytest.raises(ad.SparkRunError) as raised:
        adapter.failed_release(run.config_hash)
    assert not isinstance(raised.value, ad.MarkerReadDenied)
    assert "SlowDown" in str(raised.value)


def _local_root(tmp_path, monkeypatch):
    from fluid_build.build_runners.spark import _local
    from tests.build_runners.spark.conftest import runs_of
    from tests.build_runners.spark.test_local import local_contract

    monkeypatch.setenv(_local.LOCAL_ROOT_ENV, "s3://lake/art")
    monkeypatch.setenv("FLUID_SPARK_LOCAL_NAMESPACE_ALLOWLIST", "fluid/commerce")
    (local,) = runs_of(local_contract(), account=None)
    host = _local.LocalHost(tmp_path)
    return _local, host, local, _local.bound_job_uri(host, local)


@pytest.mark.parametrize("code", ["AccessDenied", "403", "Forbidden"])
def test_a_local_s3_root_refuses_a_marker_whatever_denied_code_it_answers(
    tmp_path, monkeypatch, code
):
    _local, host, local, job_uri = _local_root(tmp_path, monkeypatch)

    def read(_host, uri):
        if uri == job_uri:
            return b"{}"
        raise ad.SparkRunError(f"{uri} cannot be read: {code}")

    monkeypatch.setattr(_local, "_read_artifact", read)
    with pytest.raises(ad.MarkerReadDenied) as raised:
        _local.read_failed(host, local, local.config_hash)
    assert raised.value.job_config_readable


def test_a_local_s3_root_that_cannot_read_the_job_config_either_says_so(tmp_path, monkeypatch):
    _local, host, local, job_uri = _local_root(tmp_path, monkeypatch)

    def read(_host, uri):
        raise ad.SparkRunError(f"{uri} cannot be read: AccessDenied")

    monkeypatch.setattr(_local, "_read_artifact", read)
    with pytest.raises(ad.MarkerReadDenied) as raised:
        _local.read_failed(host, local, local.config_hash)
    assert not raised.value.job_config_readable


def test_a_local_probe_that_fails_for_another_reason_is_not_blamed_on_get_object(
    tmp_path, monkeypatch
):
    _local, host, local, job_uri = _local_root(tmp_path, monkeypatch)

    def read(_host, uri):
        if uri == job_uri:
            raise ad.SparkRunError(f"{uri} cannot be read: SlowDown")
        raise ad.SparkRunError(f"{uri} cannot be read: AccessDenied")

    monkeypatch.setattr(_local, "_read_artifact", read)
    with pytest.raises(ad.SparkRunError) as raised:
        _local.read_failed(host, local, local.config_hash)
    assert not isinstance(raised.value, ad.MarkerReadDenied) and "SlowDown" in str(raised.value)


def test_the_read_denied_event_is_in_the_catalog_and_names_the_grants():
    from fluid_build import _error_catalog as cat

    texts = cat.suggestions_for(rc.READ_DENIED)
    assert "s3:GetObject" in " ".join(texts) and "s3:ListBucket" in " ".join(texts)


# --- sink names are compared without regard to case ------------------------------------------------


def test_a_case_only_difference_in_the_output_is_the_same_output(run, clock, use):
    table = json.loads(run.job_text)["sink"]["table"]
    good, records = moved_from(run, table.upper())
    fake = Rolling([good], answers={"old1"}, records=records, start_states=("FAILED",))
    use(fake)
    out = go(run, clock)
    assert (out.code, fake.rolled_back) == (rc.ROLLBACK_UNSAFE, [])


def test_a_case_only_difference_in_the_dead_letters_is_the_same_dead_letters(run, clock, use):
    dead = {"sink": "table", "table": "COMMERCE.Rejects"}
    good, records = moved_from(run, "commerce.another_output", dead)
    fake = Rolling([good], answers={"old1"}, records=records, start_states=("FAILED",))
    use(fake)
    out = go(with_dead_letter(run, "commerce.rejects"), clock)
    assert (out.code, fake.rolled_back) == (rc.ROLLBACK_UNSAFE, [])


# --- which superseded runs are a config to go back to ----------------------------------------------


@pytest.mark.parametrize(
    ("change", "says"),
    [
        ({"state": "PENDING", "raw_state": "PENDING"}, "was PENDING when this apply replaced"),
        ({"attempt": 3}, "on attempt 3 when this apply replaced"),
        ({"state": "CANCELLING", "raw_state": "CANCELLING"}, "was CANCELLING when this apply"),
    ],
)
def test_a_superseded_run_that_never_ran_was_retried_or_was_being_cancelled_is_no_target(
    run, clock, use, change, says
):
    previous = dataclasses.replace(last_good(run), **change)
    fake = Rolling(
        [previous], answers={"old1"}, records={OLD_DIGEST: OLD_JOB},
        start_states=("RUNNING", "RUNNING", "FAILED"),
    )  # fmt: skip
    if previous.state == "CANCELLING":  # the operator's cancel completes
        fake.scripts["old1"] = ["CANCELLING", "CANCELLED"]
    use(fake)
    out = go(run, clock)
    assert (out.action, out.code, fake.rolled_back) == ("failed", rc.NO_GOOD_CONFIG, [])
    assert says in out.message and "which is no config to go back to" in out.message
