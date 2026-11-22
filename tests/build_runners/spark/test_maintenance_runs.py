# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``maintenance_runs``: one run per backend-spark table maintenance, from its writer build's run."""

from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
from typing import Any, Dict

import pytest

from fluid_build.build_runners.spark._desired import MAINTAIN_ACTION, desired_runs
from fluid_build.build_runners.spark._maintenance import maintenance_runs
from fluid_build.maintenance.plugins import MaintenanceActionError
from tests.build_runners.spark.conftest import ACCOUNT
from tests.maintenance.conftest import EXPOSE, TABLE, glue_block, maintained, spark_block

RUNNER_PATH = Path(__file__).resolve().parents[3] / "fluid_build/spark/runner/forge_spark_runner.py"
PREFIX = "s3://commerce-lake/_fluid/spark/commerce.orders-stream/ingest-orders"


@pytest.fixture(scope="module")
def runner():
    """The runner module, loaded from its file as spark-submit runs it (never imported)."""
    spec = importlib.util.spec_from_file_location("forge_spark_runner", RUNNER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Kind:
    """A maintenance action kind whose statements a test sets."""

    def __init__(self, *sql: str):
        self.sql = list(sql)

    def statements(self, table: str, options: Dict[str, Any]):
        return list(self.sql)


def loader(**kinds):
    def load(kind):
        try:
            return kinds[kind]
        except KeyError:
            raise MaintenanceActionError(f"no kind {kind!r}") from None

    return load


def full_block(**extra):
    block = spark_block(
        compaction={"targetFileSizeMb": 128},
        rewriteManifests={},
        expireSnapshots={"olderThan": "P2D", "retainLast": 2},
        removeOrphanFiles={"olderThan": "P4D"},
    )
    block.update(extra)
    return block


def one(block=None, **kwargs):
    runs = maintenance_runs(maintained(block or spark_block()), **kwargs)
    assert len(runs) == 1
    return runs[0]


def writer_of(block=None):
    return desired_runs(maintained(block or spark_block()))[0]


def two_table_contract():
    """The example plus a second Spark build and Iceberg expose (``order_totals``)."""
    contract = maintained(spark_block())
    build = copy.deepcopy(contract["builds"][0])
    build["id"] = "ingest-totals"
    build["outputs"] = ["order_totals"]
    expose = copy.deepcopy(contract["exposes"][0])
    expose["exposeId"] = "order_totals"
    expose["binding"]["location"]["table"] = "order_totals"
    expose["binding"]["location"]["path"] = "iceberg/commerce/order_totals/"
    expose["iceberg"]["maintenance"] = spark_block(
        expireSnapshots={}, compaction=None, schedule="0 4 * * *"
    )
    contract["builds"].append(build)
    contract["exposes"].append(expose)
    return contract


class TestWhichRuns:
    def test_one_run_per_backend_spark_table(self):
        run = one()
        assert run.maintains == EXPOSE
        assert run.build == "ingest-orders"

    def test_a_contract_without_maintenance_has_none(self, contract):
        assert maintenance_runs(contract) == []

    def test_glue_tables_have_no_run(self):
        assert maintenance_runs(maintained(glue_block())) == []

    def test_every_spark_table_in_contract_order(self):
        runs = maintenance_runs(two_table_contract())
        assert [r.maintains for r in runs] == ["order_lines", "order_totals"]
        assert [r.build for r in runs] == ["ingest-orders", "ingest-totals"]
        assert len({r.run_name for r in runs}) == 2
        assert len({r.job_uri for r in runs}) == 2

    def test_expose_id_picks_one(self):
        contract = two_table_contract()
        (run,) = maintenance_runs(contract, expose_id="order_totals")
        assert run.maintains == "order_totals"
        assert run.build == "ingest-totals"

    def test_an_expose_that_is_not_maintained_gives_none(self):
        assert maintenance_runs(maintained(spark_block()), expose_id="nothing") == []

    def test_a_contract_that_does_not_read_raises(self):
        with pytest.raises(ValueError, match="schedule is required"):
            maintenance_runs(maintained({"compaction": {}}))

    def test_an_invalid_glue_table_raises_too(self):
        with pytest.raises(ValueError, match="roleArn"):
            maintenance_runs(maintained({"backend": "glue", "compaction": {}}))

    def test_the_policy_still_refuses_the_writer_build(self, monkeypatch):
        from fluid_build.spark.policy import SparkPolicyError
        from fluid_build.streaming.policy import HOST_ALLOWLIST_ENV

        monkeypatch.setenv(HOST_ALLOWLIST_ENV, "nowhere.example")
        with pytest.raises(SparkPolicyError, match="may not use the Kafka bootstrap server"):
            maintenance_runs(maintained(spark_block()))
        assert maintenance_runs(maintained(spark_block()), policy=False)


class TestIdentity:
    def test_names_and_keys_differ_from_the_writer_builds(self):
        run, writer = one(), writer_of()
        assert run.run_name != writer.run_name
        assert run.run_name.endswith("-maintain")
        assert not writer.run_name.endswith("-maintain")
        assert run.job_uri != writer.job_uri
        assert run.run_key != writer.run_key
        assert writer.run_key == "ingest-orders"
        assert run.run_key == f"ingest-orders/maintenance/{EXPOSE}"
        assert run.config_hash != writer.config_hash

    def test_the_job_uri_is_under_the_writers_artifact_prefix(self):
        run, writer = one(), writer_of()
        assert writer.job_uri == f"{PREFIX}/job.json"
        assert run.job_uri == f"{PREFIX}/maintenance/{EXPOSE}/job.json"
        assert run.job_uri.startswith(PREFIX + "/")
        assert run.runner_uri == writer.runner_uri

    def test_what_a_scheduled_job_has(self):
        run = one()
        assert run.scheduled is True
        assert run.long_running is False
        assert run.mode == "maintenance"
        assert run.retry_attempts is None
        assert run.restarts_per_hour == 0

    def test_nothing_of_a_stream_is_carried(self):
        writer, run = writer_of(), one()
        assert writer.checkpoint and writer.topic and writer.source_id  # the build has them
        assert run.checkpoint is None
        assert run.checkpoint_id is None
        assert run.source_id is None
        assert run.topic is None
        assert run.job_sha256 is None
        for field in ("package_source", "package_sha256", "package_entry_point", "package_uri"):
            assert getattr(run, field) is None

    def test_runtime_target_role_and_region_are_the_writers(self):
        writer, run = writer_of(), one()
        assert (run.runtime, run.target, run.role_arn) == (
            writer.runtime,
            writer.target,
            writer.role_arn,
        )
        assert (run.region, run.release_label, run.glue_version) == (
            writer.region,
            writer.release_label,
            writer.glue_version,
        )
        assert (run.product, run.build) == (writer.product, writer.build)
        assert run.runner_sha256 == writer.runner_sha256

    def test_run_names_per_table_are_stable(self):
        assert one().run_name == one().run_name


class TestSparkConf:
    def test_iceberg_jar_and_extensions_are_there(self):
        conf = one().spark_conf
        assert conf["spark.jars"].endswith("iceberg-spark3-runtime.jar")
        assert "IcebergSparkSessionExtensions" in conf["spark.sql.extensions"]

    def test_the_streams_kafka_packages_are_not(self):
        writer, run = writer_of(), one()
        assert "kafka" in writer.spark_conf["spark.jars.packages"]
        assert "spark.jars.packages" not in run.spark_conf

    def test_the_writers_user_conf_is_kept(self, contract):
        contract = maintained(spark_block())
        spark = contract["builds"][0]["properties"]["parameters"]["spark"]
        spark["sparkConf"] = {"spark.executor.memory": "8g"}
        run = maintenance_runs(contract)[0]
        assert run.spark_conf["spark.executor.memory"] == "8g"
        assert "spark.jars.packages" not in run.spark_conf


class TestConfigHash:
    def test_stable_for_the_same_contract(self):
        assert one(full_block()).config_hash == one(full_block()).config_hash
        assert one(full_block()).job_digest == one(full_block()).job_digest

    @pytest.mark.parametrize(
        "change",
        [
            {"compaction": {"targetFileSizeMb": 256}},
            {"compaction": {"targetFileSizeMb": 128, "maxCommits": 11}},
            {"compaction": {"targetFileSizeMb": 128, "strategy": "sort"}},
            {"rewriteManifests": None},
            {"expireSnapshots": {"olderThan": "P3D", "retainLast": 2}},
            {"expireSnapshots": {"olderThan": "P2D", "retainLast": 3}},
            {"removeOrphanFiles": {"olderThan": "P5D"}},
        ],
    )
    def test_changes_when_a_step_changes(self, change):
        base = one(full_block())
        block = full_block()
        for key, value in change.items():
            if value is None:
                del block[key]
            else:
                block[key] = value
        changed = one(block)
        assert changed.config_hash != base.config_hash
        assert changed.job_digest != base.job_digest

    def test_does_not_change_with_the_schedule_or_timezone(self):
        base = one(full_block())
        other = one(full_block(schedule="0 5 * * 1", timezone="Europe/Zurich"))
        assert other.config_hash == base.config_hash
        assert other.job_digest == base.job_digest

    def test_changes_when_an_actions_statements_change(self):
        block = spark_block(actions=[{"name": "erase", "kind": "k"}])
        contract = maintained(block)
        a = maintenance_runs(contract, load=loader(k=Kind(f"DELETE FROM {TABLE} WHERE a = 1")))[0]
        again = maintenance_runs(contract, load=loader(k=Kind(f"DELETE FROM {TABLE} WHERE a = 1")))[
            0
        ]
        b = maintenance_runs(contract, load=loader(k=Kind(f"DELETE FROM {TABLE} WHERE a = 2")))[0]
        assert a.config_hash == again.config_hash
        assert a.config_hash != b.config_hash
        assert a.job_digest != b.job_digest

    def test_bind_keeps_the_hash(self):
        run = one()
        bound = one(account=ACCOUNT)
        assert bound.config_hash == run.config_hash
        assert bound.job_digest == run.job_digest


class TestAccount:
    def test_account_binds_the_token(self):
        unbound, bound = one(), one(account=ACCOUNT)
        assert unbound.needs_account is True
        assert "${aws_account_id}" in unbound.role_arn
        assert bound.needs_account is False
        assert "${aws_account_id}" not in bound.role_arn
        assert f"arn:aws:iam::{ACCOUNT}:role/" in bound.role_arn

    def test_every_run_is_bound(self):
        for run in maintenance_runs(two_table_contract(), account=ACCOUNT):
            assert run.needs_account is False


class TestPlanAction:
    def test_the_plan_action(self):
        run = one()
        action = run.plan_action(3)
        assert action["op"] == MAINTAIN_ACTION == "spark.maintain"
        assert action["action_type"] == "spark.maintain"
        assert action["action_id"] == "spark_maintain_order_lines"
        assert action["id"] == EXPOSE
        assert action["step"] == 3
        assert action["provider"] == "aws"
        assert action["depends_on"] == []

    def test_the_payload_binds_the_job_and_the_runtime(self):
        run = one(full_block())
        payload = run.plan_action(0)["payload"]
        assert payload["expose"] == EXPOSE
        assert payload["build"] == "ingest-orders"
        assert payload["product"] == run.product
        assert payload["runtime"] == run.runtime
        assert payload["artifacts"] == {"jobConfig": run.job_uri, "runner": run.runner_uri}
        assert payload["jobConfigDigest"] == run.job_digest
        assert payload["configHash"] == run.config_hash
        assert payload["runnerSha256"] == run.runner_sha256
        assert payload["executionRoleArn"] == run.role_arn
        assert f"--expose-id {EXPOSE}" in payload["stateImpact"]

    def test_the_build_plan_action_is_not_a_maintain_action(self):
        action = writer_of().plan_action(0)
        assert action["op"] == "spark.reconcile"
        assert action["action_id"] == "spark_ingest-orders"

    def test_a_changed_step_changes_the_plan_action(self):
        assert (
            one(full_block()).plan_action(0)["payload"]["configHash"]
            != one(full_block(removeOrphanFiles={"olderThan": "P9D"})).plan_action(0)["payload"][
                "configHash"
            ]
        )


class TestTags:
    def test_a_maintenance_run_is_tagged_with_its_table(self):
        tags = one().tags()
        assert tags["fluid_maintains"] == EXPOSE
        assert tags["fluid_build"] == "ingest-orders"
        assert tags["fluid_config_hash"] == one().config_hash
        assert "fluid_source" not in tags and "fluid_checkpoint" not in tags

    def test_a_builds_run_has_no_maintains_tag(self):
        assert "fluid_maintains" not in writer_of().tags()


class TestJobText:
    def test_it_is_the_maintenance_config(self):
        run = one(full_block())
        config = json.loads(run.job_text)
        assert config["mode"] == "maintenance"
        assert config["product"] == run.product
        assert config["build"] == "ingest-orders"
        assert config["maintenance"]["expose"] == EXPOSE
        assert "create" not in config["sink"]
        assert config["sink"]["table"] == "commerce.order_lines"
        assert config["sink"]["catalog"] == "fluid"
        for key in ("source", "checkpoint", "sql"):
            assert key not in config

    def test_the_digest_is_the_texts(self):
        run = one(full_block())
        # the read-back check of `fluid apply`: the text as compiled, not the build's
        assert run.config_matches(run.job_text.encode("utf-8"))
        assert not run.config_matches(writer_of().job_text.encode("utf-8"))

    def test_the_runners_own_parser_reads_it(self, runner):
        run = one(full_block())
        config = runner.parse_config(run.job_text)
        assert config["mode"] == "maintenance"
        assert [s["step"] for s in config["maintenance"]["steps"]] == [
            "rewrite_data_files",
            "rewrite_manifests",
            "expire_snapshots",
            "remove_orphan_files",
        ]
        steps = {s["step"]: s for s in config["maintenance"]["steps"]}
        assert steps["rewrite_data_files"]["options"]["target-file-size-bytes"] == str(
            128 * 1024 * 1024
        )
        assert steps["rewrite_data_files"]["options"]["partial-progress.enabled"] == "true"
        assert steps["expire_snapshots"]["olderThanSeconds"] == 2 * 86400
        assert steps["expire_snapshots"]["retainLast"] == 2
        assert steps["remove_orphan_files"]["olderThanSeconds"] == 4 * 86400

    def test_the_runner_reads_an_actions_statements_in_front(self, runner):
        sql = [f"DELETE FROM {TABLE} WHERE customer_id = 'c1'", f"UPDATE {TABLE} SET a = 1"]
        contract = maintained(full_block(actions=[{"name": "erase", "kind": "k"}]))
        run = maintenance_runs(contract, load=loader(k=Kind(*sql)))[0]
        steps = runner.parse_config(run.job_text)["maintenance"]["steps"]
        assert [s["step"] for s in steps] == [
            "statement",
            "statement",
            "rewrite_data_files",
            "rewrite_manifests",
            "expire_snapshots",
            "remove_orphan_files",
        ]
        assert [s["sql"] for s in steps[:2]] == sql
        assert {s["name"] for s in steps[:2]} == {"erase"}
        assert json.loads(run.job_text)["maintenance"]["steps"] == steps

    def test_the_runner_agrees_with_forge_on_every_step_it_takes(self, runner):
        from fluid_build.maintenance.job import STEPS

        assert tuple(runner.MAINTENANCE_STEPS) == STEPS

    def test_no_interpolation_is_left_in_the_text(self):
        assert "${" not in one(full_block()).job_text


class TestActionsAreLoaded:
    def test_an_unusable_kind_raises(self):
        contract = maintained(spark_block(actions=[{"name": "erase", "kind": "gone"}]))
        with pytest.raises(MaintenanceActionError, match="actions\\[erase\\]"):
            maintenance_runs(contract, load=loader())

    def test_statements_on_another_table_raise(self):
        contract = maintained(spark_block(actions=[{"name": "erase", "kind": "k"}]))
        with pytest.raises(MaintenanceActionError, match="own table"):
            maintenance_runs(contract, load=loader(k=Kind("DELETE FROM fluid.commerce.other")))

    def test_the_loader_is_asked_for_the_runner_table_name(self):
        seen = []

        class Spy:
            def statements(self, table, options):
                seen.append((table, options))
                return [f"DELETE FROM {table}"]

        contract = maintained(
            spark_block(actions=[{"name": "erase", "kind": "k", "options": {"n": 1}}])
        )
        maintenance_runs(contract, load=loader(k=Spy()))
        assert seen == [(TABLE, {"n": 1})]

    def test_without_actions_no_plugin_is_read(self, monkeypatch):
        from fluid_build import plugin_manager

        def boom(group):
            raise AssertionError("no entry point is read without an action")

        monkeypatch.setattr(plugin_manager, "_entry_points", boom)
        assert maintenance_runs(maintained(full_block()))
