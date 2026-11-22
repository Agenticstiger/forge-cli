# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""What the runner stamps on every commit it makes, and what it refuses to stamp.

Iceberg keeps a ``snapshot-property.<key>`` write option as ``<key>`` in the commit's snapshot
summary, for a batch write and for a stream's (Iceberg's ``SparkWrite.commitOperation`` applies
the options, and the streaming commit reaches it), so the runner puts the product, build, job
config digest, version, contract digest, release and run on the write options of each writer it
builds, and in the job's own Spark conf.

The writers run without Spark here, against recorders. That Iceberg keeps the options as
summary entries is read off a real table by the opt-in run on Spark 3.5
(``test_runner_integration.py::test_every_commit_the_runner_makes_carries_the_runs_stamps``).
"""

from __future__ import annotations

import json
import types

import pytest

from .test_runner import config_text
from .test_stream_safety import Recorder, called

DIGEST = "sha256:" + "ab" * 32
RELEASE = "sha256:" + "cd" * 32
AIRFLOW_RUN = "scheduled__2026-11-21T00:00:00+00:00"


def flags(**given):
    """The namespace ``main`` builds from the command line: a flag not given is ``None``."""
    names = ("product_version", "contract_digest", "release_id", "run_id")
    return types.SimpleNamespace(**{f"fluid_{n}": given.get(n) for n in names})


class Session:
    """A SparkSession's conf, recording what is set on it."""

    def __init__(self):
        self.values = {}
        self.conf = types.SimpleNamespace(set=self.values.__setitem__, get=self._get)

    def _get(self, name, default=None):
        return self.values.get(name, default)


class Query(Recorder):
    id = "query-1"
    isActive = False  # noqa: N815 - PySpark's name

    def awaitTermination(self):  # noqa: N802 - PySpark's name
        return None


class Frame(Recorder):
    """The output: a batch's ``writeTo`` and a stream's ``writeStream`` record their calls."""

    @property
    def writeStream(self):  # noqa: N802 - PySpark's name
        return self

    def toTable(self, table):  # noqa: N802 - PySpark's name
        self.calls.append(("toTable", (table,), {}))
        return Query(self.calls)


def stamps_of(runner, config, text, **given):
    """What ``main`` builds: the command line's flags, checked, beside the config's own."""
    return runner.release_stamps(config, text, runner.stamp_flags(flags(**given)))


def everything(**given):
    return {
        "product_version": "1.4.0",
        "contract_digest": DIGEST,
        "release_id": RELEASE,
        "run_id": AIRFLOW_RUN,
        **given,
    }


# --- what a run is stamped with -------------------------------------------------------


def test_a_run_is_stamped_with_what_it_is(runner, contract):
    text = config_text(contract)
    stamps = stamps_of(runner, runner.parse_config(text), text, **everything())
    assert stamps == {
        "fluid.product": "commerce.orders-stream",
        "fluid.build": "ingest-orders",
        "fluid.job-sha256": runner.config_digest(text),
        "fluid.product-version": "1.4.0",
        "fluid.contract-digest": DIGEST,
        "fluid.release-id": RELEASE,
        "fluid.run-id": AIRFLOW_RUN,
    }


def test_a_flag_that_is_not_given_leaves_its_stamp_out(runner, contract):
    text = config_text(contract)
    stamps = stamps_of(runner, runner.parse_config(text), text, run_id="r-1")
    assert sorted(stamps) == [
        "fluid.build",
        "fluid.job-sha256",
        "fluid.product",
        "fluid.run-id",
    ]
    bare = stamps_of(runner, runner.parse_config(text), text)
    assert sorted(bare) == ["fluid.build", "fluid.job-sha256", "fluid.product"]


def test_the_stamp_keys_are_lower_case_and_never_one_of_icebergs_own(runner):
    keys = [key for _flag, key in runner.STAMP_FLAGS]
    keys += ["fluid.product", "fluid.build", "fluid.job-sha256"]
    assert all(key.startswith("fluid.") and key == key.lower() for key in keys)
    owned = {"spark.app.id", "spark.sql.streaming.queryId", "spark.sql.streaming.epochId"}
    assert not owned & set(keys)  # a stream's option keys are lower-cased: no key collides


@pytest.mark.parametrize("name", ["product_version", "contract_digest", "release_id", "run_id"])
@pytest.mark.parametrize(
    "value",
    [
        "two words", "line\nbreak", "tab\tsep", "semi;colon", "caf" + chr(0xE9), "{braces}", "q'uote",
        "x" * 257, "-x", "--config=s3://other/job.json", ".dot", "_under",
    ],  # fmt: skip
)
def test_a_value_a_stamp_may_not_hold_is_refused_before_spark_starts(runner, name, value):
    with pytest.raises(runner.JobConfigError, match="fluid\\.") as refused:
        runner.stamp_flags(flags(**{name: value}))
    assert "the job was not run" in str(refused.value)
    assert "x" * 100 not in str(refused.value)  # a long value is not echoed whole


@pytest.mark.parametrize("field", ["product", "build"])
def test_a_product_or_build_a_stamp_may_not_hold_is_refused(runner, contract, field):
    text = config_text(contract)
    config = runner.parse_config(text)
    config[field] = "two words"
    with pytest.raises(runner.JobConfigError, match=f"fluid\\.{field}"):
        runner.release_stamps(config, text)


@pytest.mark.parametrize(
    "value",
    [
        "1.4.0",
        "1.5.0-rc.1+build.7",
        DIGEST,
        "3f9a1c0b" + "7e2d",
        AIRFLOW_RUN,
        "manual__2026-11-21T10:05:00+00:00",
    ],
)
def test_the_ids_and_versions_the_pipeline_makes_are_accepted(runner, contract, value):
    text = config_text(contract)
    stamps = stamps_of(runner, runner.parse_config(text), text, run_id=value)
    assert stamps["fluid.run-id"] == value


def test_a_product_or_build_id_may_start_with_an_underscore_a_flag_value_may_not(runner, contract):
    text = config_text(contract)
    config = runner.parse_config(text)
    config.update(product="_internal.orders", build="_ingest")
    stamps = stamps_of(runner, config, text)
    assert stamps["fluid.product"] == "_internal.orders" and stamps["fluid.build"] == "_ingest"
    with pytest.raises(runner.JobConfigError, match="starts with a letter or a digit"):
        runner.stamp_flags(flags(run_id="_internal"))


# --- where the stamps go --------------------------------------------------------------


def test_each_stamp_becomes_a_snapshot_property_write_option(runner):
    calls = []
    writer = runner.stamped(Recorder(calls), {"fluid.product": "p", "fluid.run-id": "r-1"})
    assert isinstance(writer, Recorder)
    assert called(calls, "option") == [
        (("snapshot-property.fluid.product", "p"), {}),
        (("snapshot-property.fluid.run-id", "r-1"), {}),
    ]


@pytest.mark.parametrize("stamps", [None, {}])
def test_no_stamps_add_no_options(runner, stamps):
    calls = []
    writer = Recorder(calls)
    assert runner.stamped(writer, stamps) is writer and calls == []


def stamped_run(runner, contract, monkeypatch, **config_changes):
    text = config_text(contract)
    config = runner.parse_config(text)
    config.update(maskedColumns=[], maskKey=None, **config_changes)
    calls = []
    frame = Frame(calls)
    monkeypatch.setattr(runner, "build_output", lambda spark, cfg: frame)
    monkeypatch.setattr(runner, "_names", lambda spark, directory: [])  # a new checkpoint
    session = Session()
    stamps = stamps_of(runner, config, text, **everything())
    runner.run(session, config, stamps)
    return calls, session, stamps


def test_a_batch_overwrite_carries_the_stamps(runner, contract, monkeypatch):
    calls, _session, stamps = stamped_run(runner, contract, monkeypatch, mode="batch", source=None)
    options = dict(args for args, _ in called(calls, "option"))
    for key, value in stamps.items():
        assert options[f"snapshot-property.{key}"] == value
    assert options["check-nullability"] == "false"
    steps = [name for name, _args, _kwargs in calls]
    assert steps.index("overwritePartitions") == len(steps) - 1  # the stamps come before it


def test_a_streams_sink_carries_the_stamps(runner, contract, monkeypatch):
    calls, _session, stamps = stamped_run(runner, contract, monkeypatch)
    options = dict(args for args, _ in called(calls, "option"))
    for key, value in stamps.items():
        assert options[f"snapshot-property.{key}"] == value
    assert options["checkpointLocation"].startswith("s3://commerce-lake/_checkpoints/")
    assert called(calls, "toTable") == [(("fluid.commerce.order_lines",), {})]


def test_the_jobs_spark_conf_holds_the_stamps_too(runner, contract, monkeypatch):
    _calls, session, stamps = stamped_run(runner, contract, monkeypatch)
    for key, value in stamps.items():
        assert session.values[f"spark.{key}"] == value
    assert session.values["spark.fluid.run-id"] == AIRFLOW_RUN


def test_a_spark_conf_that_is_refused_does_not_stop_the_run(runner, contract, monkeypatch, capsys):
    text = config_text(contract)
    config = runner.parse_config(text)
    config.update(mode="batch", source=None, maskedColumns=[], maskKey=None)
    calls = []
    monkeypatch.setattr(runner, "build_output", lambda spark, cfg: Frame(calls))
    session = Session()
    accept = session.conf.set

    def refuse(name, value):
        if name.startswith("spark.fluid."):
            raise RuntimeError("Cannot modify the value of a static config: " + name)
        accept(name, value)

    session.conf.set = refuse
    runner.run(session, config, {"fluid.run-id": "r-1"})
    out = capsys.readouterr().out
    assert '"stamp_conf_not_set"' in out and "fluid.run-id" in out and "static config" not in out
    options = dict(args for args, _ in called(calls, "option"))
    assert options["snapshot-property.fluid.run-id"] == "r-1"  # the commit still carries it


def test_the_start_line_names_the_stamps_and_nothing_secret(runner, contract, monkeypatch, capsys):
    stamped_run(runner, contract, monkeypatch)
    start = next(
        json.loads(line) for line in capsys.readouterr().out.splitlines() if '"start"' in line
    )
    assert start["stamps"]["fluid.release-id"] == RELEASE
    assert start["stamps"]["fluid.run-id"] == AIRFLOW_RUN


def test_a_run_without_stamps_writes_what_it_always_wrote(runner, contract, monkeypatch):
    text = config_text(contract)
    config = runner.parse_config(text)
    config.update(mode="batch", source=None, maskedColumns=[], maskKey=None)
    calls = []
    monkeypatch.setattr(runner, "build_output", lambda spark, cfg: Frame(calls))
    session = Session()
    runner.run(session, config)
    assert called(calls, "option") == [(("check-nullability", "false"), {})]
    assert not any(key.startswith("spark.fluid.") for key in session.values)


# --- the command line -----------------------------------------------------------------


def main_with(runner, monkeypatch, contract, argv, events=None):
    """``main`` on stand-ins; ``events`` records the steps it takes, in order."""
    import sys

    seen = []
    events = [] if events is None else events
    session = types.SimpleNamespace(
        builder=types.SimpleNamespace(getOrCreate=lambda: events.append("spark") or "spark")
    )
    monkeypatch.setitem(sys.modules, "pyspark", types.ModuleType("pyspark"))
    monkeypatch.setitem(sys.modules, "pyspark.sql", types.SimpleNamespace(SparkSession=session))
    text = config_text(contract)
    monkeypatch.setattr(runner, "read_text", lambda spark, path: events.append("read") or text)
    monkeypatch.setattr(
        runner,
        "run",
        lambda spark, config, stamps, stop=None: events.append("run") or seen.append(stamps),
    )
    code = runner.main(["--config", "s3://b/_fluid/job.json", *argv])
    return code, seen, text


def test_the_flags_fluid_apply_passes_become_stamps(runner, contract, monkeypatch):
    argv = [
        "--fluid-product-version", "1.4.0",
        "--fluid-contract-digest", DIGEST,
        "--fluid-release-id", RELEASE,
        "--fluid-run-id", AIRFLOW_RUN,
        "--JOB_NAME", "glue-owns-this",
    ]  # fmt: skip
    code, seen, text = main_with(runner, monkeypatch, contract, argv)
    assert code == 0 and len(seen) == 1
    assert seen[0]["fluid.run-id"] == AIRFLOW_RUN
    assert seen[0]["fluid.contract-digest"] == DIGEST
    assert seen[0]["fluid.job-sha256"] == runner.config_digest(text)


def test_a_plain_spark_submit_still_runs_with_the_three_stamps_it_can_make(
    runner, contract, monkeypatch
):
    code, seen, _text = main_with(runner, monkeypatch, contract, [])
    assert code == 0
    assert sorted(seen[0]) == ["fluid.build", "fluid.job-sha256", "fluid.product"]


def test_a_flag_with_a_bad_value_stops_the_run_before_spark_starts(runner, contract, monkeypatch):
    events = []
    with pytest.raises(runner.JobConfigError, match="fluid.run-id"):
        main_with(runner, monkeypatch, contract, ["--fluid-run-id", "two words"], events)
    assert events == []  # no session, no config read, no job
