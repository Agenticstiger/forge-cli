# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid_build.maintenance.spec``: reading and checking ``exposes[].iceberg.maintenance``."""

from __future__ import annotations

import copy

import pytest

from fluid_build.maintenance.spec import (
    Compaction,
    CustomAction,
    ExpireSnapshots,
    RemoveOrphanFiles,
    declares_maintenance,
    option_problems,
    parse_duration,
    read_maintenance,
    spark_tables,
)
from tests.maintenance.conftest import ROLE_ARN, glue_block, maintained, spark_block
from tests.spark.conftest import CONTRACT


def read(block, **kw):
    return read_maintenance(maintained(block, **kw))


def one(block, **kw):
    tables, errors = read(block, **kw)
    assert errors == []
    assert len(tables) == 1
    return tables[0]


def errors_of(block, **kw):
    tables, errors = read(block, **kw)
    assert tables == []
    assert errors
    return errors


def has(errors, text):
    return any(text in e for e in errors)


class TestParseDuration:
    @pytest.mark.parametrize(
        "text, seconds",
        [
            ("P1D", 86400),
            ("P5D", 5 * 86400),
            ("P1W", 7 * 86400),
            ("PT1H", 3600),
            ("PT90M", 5400),
            ("PT1H30M", 5400),
            ("P1DT12H", 86400 + 43200),
            ("P1W2DT3H4M5S", ((9 * 24 + 3) * 60 + 4) * 60 + 5),
            (" P3D ", 3 * 86400),
        ],
    )
    def test_reads(self, text, seconds):
        assert parse_duration(text) == seconds

    @pytest.mark.parametrize(
        "value",
        [
            "",
            "P",
            "PT",
            "P1DT",
            "1D",
            "p1d",
            "P1M",
            "P1Y",
            "P-1D",
            "P1.5D",
            "PT1H ago",
            None,
            5,
            [],
        ],
    )
    def test_refuses(self, value):
        assert parse_duration(value) is None

    def test_huge_digit_runs_are_not_durations(self):
        assert parse_duration("P" + "9" * 8 + "D") is None
        assert parse_duration("PT" + "9" * 30 + "S") is None
        assert parse_duration("P9999999D") == 9999999 * 86400


class TestFullSparkBlock:
    def test_every_part_reads_to_its_dataclass(self):
        tm = one(
            {
                "backend": "spark",
                "schedule": "30 2 * * 1",
                "timezone": "Europe/Zurich",
                "build": "ingest-orders",
                "compaction": {
                    "strategy": "sort",
                    "targetFileSizeMb": 256,
                    "minInputFiles": 5,
                    "deleteFileThreshold": 3,
                    "maxCommits": 20,
                    "maxConcurrentRewrites": 4,
                },
                "rewriteManifests": {},
                "expireSnapshots": {"olderThan": "P7D", "retainLast": 3},
                "removeOrphanFiles": {"olderThan": "P2D"},
                "actions": [{"name": "erase", "kind": "gdpr-erasure", "options": {"n": 1}}],
            }
        )
        assert (tm.expose_id, tm.backend) == ("order_lines", "spark")
        assert (tm.database, tm.table) == ("commerce", "order_lines")
        assert (tm.schedule, tm.timezone) == ("30 2 * * 1", "Europe/Zurich")
        assert tm.build_id == "ingest-orders"
        assert tm.role_arn is None
        assert tm.compaction == Compaction(
            strategy="sort",
            target_file_size_mb=256,
            min_input_files=5,
            delete_file_threshold=3,
            max_commits=20,
            max_concurrent_rewrites=4,
        )
        assert tm.rewrite_manifests is True
        assert tm.expire == ExpireSnapshots(7 * 86400, 3, True)
        assert tm.orphans == RemoveOrphanFiles(2 * 86400)
        assert tm.actions == (CustomAction("erase", "gdpr-erasure", {"n": 1}),)

    def test_defaults(self):
        tm = one(
            spark_block(
                compaction={},
                rewriteManifests={},
                expireSnapshots={},
                removeOrphanFiles={},
            )
        )
        assert tm.backend == "spark"
        assert tm.timezone == "UTC"
        assert tm.compaction.strategy == "binpack"
        assert tm.compaction.max_commits == 10
        assert tm.compaction.target_file_size_mb is None
        assert tm.expire.older_than_seconds == 5 * 86400
        assert tm.expire.retain_last == 1
        assert tm.orphans.older_than_seconds == 3 * 86400
        assert tm.rewrite_manifests is True

    def test_parts_not_declared_stay_off(self):
        tm = one({"schedule": "0 3 * * *", "rewriteManifests": {}})
        assert tm.compaction is None
        assert tm.expire is None
        assert tm.orphans is None
        assert tm.actions == ()
        assert tm.rewrite_manifests is True

    def test_backend_defaults_to_spark(self):
        assert one(spark_block()).backend == "spark"

    def test_named_build_that_writes_it(self):
        assert one(spark_block(build="ingest-orders")).build_id == "ingest-orders"

    def test_weekly_cron_and_preset_schedules(self):
        assert one(spark_block(schedule="@daily")).schedule == "@daily"
        assert one(spark_block(schedule="0 3 * * MON")).schedule == "0 3 * * MON"


class TestLegacyDefaults:
    def test_legacy_target_size_is_the_default(self):
        tm = one(spark_block(compaction={}), legacy={"compaction": {"targetFileSizeMb": 128}})
        assert tm.compaction.target_file_size_mb == 128

    def test_legacy_snapshot_retention_is_the_default(self):
        tm = one(
            spark_block(compaction=None, expireSnapshots={}),
            legacy={
                "snapshotRetention": {"maxSnapshotAgeMs": 2 * 86400 * 1000, "minSnapshotsToKeep": 4}
            },
        )
        assert tm.expire.older_than_seconds == 2 * 86400
        assert tm.expire.retain_last == 4

    def test_the_same_value_in_both_places_is_fine(self):
        tm = one(
            spark_block(
                compaction={"targetFileSizeMb": 128},
                expireSnapshots={"olderThan": "P2D", "retainLast": 4},
            ),
            legacy={
                "compaction": {"targetFileSizeMb": 128},
                "snapshotRetention": {
                    "maxSnapshotAgeMs": 2 * 86400 * 1000,
                    "minSnapshotsToKeep": 4,
                },
            },
        )
        assert tm.compaction.target_file_size_mb == 128
        assert tm.expire.retain_last == 4

    def test_target_size_in_both_places_must_match(self):
        errors = errors_of(
            spark_block(compaction={"targetFileSizeMb": 256}),
            legacy={"compaction": {"targetFileSizeMb": 128}},
        )
        assert has(errors, "targetFileSizeMb and iceberg.compaction.targetFileSizeMb differ")

    def test_snapshot_age_in_both_places_must_match(self):
        errors = errors_of(
            spark_block(compaction=None, expireSnapshots={"olderThan": "P2D"}),
            legacy={"snapshotRetention": {"maxSnapshotAgeMs": 3 * 86400 * 1000}},
        )
        assert has(errors, "maxSnapshotAgeMs differ")

    def test_retain_last_in_both_places_must_match(self):
        errors = errors_of(
            spark_block(compaction=None, expireSnapshots={"retainLast": 2}),
            legacy={"snapshotRetention": {"minSnapshotsToKeep": 5}},
        )
        assert has(errors, "minSnapshotsToKeep differ")

    def test_legacy_age_under_an_hour_cannot_be_the_default(self):
        errors = errors_of(
            spark_block(compaction=None, expireSnapshots={}),
            legacy={"snapshotRetention": {"maxSnapshotAgeMs": 60_000}},
        )
        assert has(errors, "maxSnapshotAgeMs must be at least 3600000")

    def test_legacy_compaction_disabled_with_a_compaction_block(self):
        errors = errors_of(spark_block(compaction={}), legacy={"compaction": {"enabled": False}})
        assert has(errors, "iceberg.compaction.enabled is false")

    def test_legacy_alone_declares_no_maintenance(self):
        contract = maintained(None, legacy={"compaction": {"targetFileSizeMb": 128}})
        assert read_maintenance(contract) == ([], [])
        assert declares_maintenance(contract) is False


class TestEnabledFalse:
    def test_disabled_blocks_run_nothing(self):
        errors = errors_of(
            {
                "schedule": "0 3 * * *",
                "compaction": {"enabled": False},
                "rewriteManifests": {"enabled": False},
                "expireSnapshots": {"enabled": False},
                "removeOrphanFiles": {"enabled": False},
            }
        )
        assert has(errors, "declares nothing to run")

    def test_a_disabled_block_is_dropped_the_rest_stay(self):
        tm = one(
            spark_block(
                compaction={"enabled": False},
                expireSnapshots={"olderThan": "P2D"},
            )
        )
        assert tm.compaction is None
        assert tm.expire.older_than_seconds == 2 * 86400

    def test_enabled_must_be_a_bool(self):
        errors = errors_of(spark_block(compaction={"enabled": "no"}))
        assert has(errors, "compaction.enabled must be true or false")

    def test_empty_actions_list_declares_nothing(self):
        assert has(errors_of({"schedule": "0 3 * * *", "actions": []}), "declares nothing")


class TestFloorsAndValues:
    @pytest.mark.parametrize("age", ["PT23H59M59S", "PT1H", "P0D", "soon", 3, "P1M"])
    def test_orphan_retention_under_a_day_or_unreadable(self, age):
        errors = errors_of(spark_block(removeOrphanFiles={"olderThan": age}))
        assert has(
            errors, "removeOrphanFiles.olderThan must be an ISO-8601 duration of at least P1D"
        )

    def test_orphan_retention_of_a_day_passes(self):
        tm = one(spark_block(removeOrphanFiles={"olderThan": "P1D"}))
        assert tm.orphans.older_than_seconds == 86400
        tm = one(spark_block(removeOrphanFiles={"olderThan": "PT24H"}))
        assert tm.orphans.older_than_seconds == 86400

    @pytest.mark.parametrize("age", ["PT59M", "PT3599S", "P0D", "forever", 7])
    def test_snapshot_expiry_under_an_hour_or_unreadable(self, age):
        errors = errors_of(spark_block(expireSnapshots={"olderThan": age}))
        assert has(
            errors, "expireSnapshots.olderThan must be an ISO-8601 duration of at least PT1H"
        )

    def test_snapshot_expiry_of_an_hour_passes(self):
        assert (
            one(spark_block(expireSnapshots={"olderThan": "PT1H"})).expire.older_than_seconds
            == 3600
        )

    def test_huge_digit_durations_are_refused(self):
        errors = errors_of(spark_block(expireSnapshots={"olderThan": "P" + "9" * 20 + "D"}))
        assert has(errors, "expireSnapshots.olderThan")

    @pytest.mark.parametrize("value", [0, -1, 1.5, True, "512", 1024 * 1024 + 1])
    def test_target_file_size_must_be_a_whole_number(self, value):
        errors = errors_of(spark_block(compaction={"targetFileSizeMb": value}))
        assert has(errors, "targetFileSizeMb must be a whole number of megabytes")

    @pytest.mark.parametrize(
        "key, bad",
        [
            ("minInputFiles", 0),
            ("deleteFileThreshold", 1_000_001),
            ("maxCommits", 1001),
            ("maxCommits", 0),
            ("maxConcurrentRewrites", "2"),
            ("minInputFiles", True),
        ],
    )
    def test_compaction_integers_are_bounded(self, key, bad):
        errors = errors_of(spark_block(compaction={key: bad}))
        assert has(errors, f"compaction.{key} must be a whole number")

    @pytest.mark.parametrize("retain", [0, -2, 100_001, 1.5, "3", True])
    def test_retain_last_is_bounded(self, retain):
        errors = errors_of(spark_block(expireSnapshots={"retainLast": retain}))
        assert has(errors, "retainLast must be a whole number from 1 to 100000")

    def test_clean_expired_files_must_be_a_bool(self):
        errors = errors_of(spark_block(expireSnapshots={"cleanExpiredFiles": "yes"}))
        assert has(errors, "cleanExpiredFiles must be true or false")

    def test_spark_deletes_expired_files_itself(self):
        errors = errors_of(spark_block(expireSnapshots={"cleanExpiredFiles": False}))
        assert has(errors, "cleanExpiredFiles")

    def test_unknown_strategy(self):
        errors = errors_of(spark_block(compaction={"strategy": "shuffle"}))
        assert has(errors, "compaction.strategy must be one of binpack, sort on backend spark")

    def test_sort_is_a_spark_strategy_and_z_order_is_not(self):
        assert one(spark_block(compaction={"strategy": "sort"})).compaction.strategy == "sort"
        assert has(errors_of(spark_block(compaction={"strategy": "z-order"})), "on backend spark")


class TestShape:
    def test_unknown_keys_at_each_level(self):
        errors = errors_of(
            spark_block(
                colour="red",
                compaction={"speed": 1},
                expireSnapshots={"x": 1},
                removeOrphanFiles={"y": 1},
                rewriteManifests={"z": 1},
            )
        )
        assert has(errors, "iceberg.maintenance has keys it does not read: colour")
        assert has(errors, "compaction has keys it does not read: speed")
        assert has(errors, "expireSnapshots has keys it does not read: x")
        assert has(errors, "removeOrphanFiles has keys it does not read: y")
        assert has(errors, "rewriteManifests has keys it does not read: z")

    def test_block_that_is_not_a_mapping(self):
        assert has(errors_of("compact"), "iceberg.maintenance must be a mapping")
        assert has(errors_of([1]), "iceberg.maintenance must be a mapping")

    def test_part_that_is_not_a_mapping(self):
        assert has(errors_of(spark_block(compaction="yes")), "compaction must be a mapping")

    def test_nothing_declared(self):
        assert has(errors_of({"schedule": "0 3 * * *"}), "declares nothing to run")
        assert has(errors_of({}), "declares nothing to run")

    def test_not_an_iceberg_binding(self):
        contract = maintained(spark_block())
        contract["exposes"][0]["binding"]["format"] = "parquet"
        tables, errors = read_maintenance(contract)
        assert tables == []
        assert has(errors, "maintenance is an Iceberg table's")

    def test_iceberg_binding_without_a_table(self):
        contract = maintained(spark_block())
        del contract["exposes"][0]["binding"]["location"]["table"]
        assert has(read_maintenance(contract)[1], "location.database and location.table")

    def test_backend_unknown(self):
        errors = errors_of(spark_block(backend="flink"))
        assert has(errors, "backend must be one of spark, glue")

    def test_exposes_without_maintenance_are_skipped(self):
        contract = maintained(None)
        contract["exposes"].append("not a mapping")
        assert read_maintenance(contract) == ([], [])
        assert read_maintenance({}) == ([], [])


class TestSparkPlacement:
    def test_schedule_is_required(self):
        errors = errors_of({"compaction": {}})
        assert has(errors, "schedule is required on backend spark")

    @pytest.mark.parametrize("value", ["", "  ", 5, None])
    def test_schedule_must_be_text(self, value):
        assert has(errors_of(spark_block(schedule=value)), "schedule is required")

    @pytest.mark.parametrize(
        "cron", ["every day", "0 3 * *", "61 3 * * *", "0 25 * * *", "0 0 3 * * *"]
    )
    def test_schedule_must_be_a_cron(self, cron):
        assert has(errors_of(spark_block(schedule=cron)), "is not a cron Airflow loads")

    def test_once_is_refused(self):
        assert has(errors_of(spark_block(schedule="@once")), "@once runs it one time")

    def test_bad_timezone(self):
        assert has(errors_of(spark_block(timezone="Mars/Base")), "timezone is not an IANA zone")
        assert has(errors_of(spark_block(timezone="not a zone!")), "timezone is not an IANA zone")

    def test_no_writer(self):
        contract = maintained(spark_block())
        contract["builds"][0]["outputs"] = ["something_else"]
        errors = read_maintenance(contract)[1]
        assert has(errors, "none does (properties.parameters.spark, outputs[0])")

    def test_two_writers_need_a_build_name(self):
        contract = maintained(spark_block())
        contract["builds"].append(copy.deepcopy(contract["builds"][0]))
        contract["builds"][1]["id"] = "ingest-orders-2"
        tables, errors = read_maintenance(contract)
        assert tables == []
        assert has(errors, "2 do; name one with build")
        contract["exposes"][0]["iceberg"]["maintenance"]["build"] = "ingest-orders-2"
        tables, errors = read_maintenance(contract)
        assert errors == []
        assert tables[0].build_id == "ingest-orders-2"

    def test_named_build_that_does_not_write_it(self):
        errors = errors_of(spark_block(build="elsewhere"))
        assert has(errors, "build must name an engine: spark build whose output is order_lines")

    def test_writer_on_glue_runtime_is_refused(self):
        contract = maintained(spark_block())
        contract["builds"][0]["properties"]["parameters"]["spark"]["runtime"] = "glue"
        errors = read_maintenance(contract)[1]
        assert has(errors, "runs on AWS Glue")
        assert has(errors, "use backend glue")

    def test_below_fluid_version_0_7_7(self):
        contract = maintained(spark_block())
        contract["fluidVersion"] = "0.7.6"
        assert has(read_maintenance(contract)[1], "fluidVersion 0.7.7 or later")

    def test_unbundled_fluid_version(self):
        contract = maintained(spark_block())
        contract["fluidVersion"] = "9.9.9"
        assert has(read_maintenance(contract)[1], "fluidVersion 0.7.7 or later")

    def test_role_arn_and_run_every_hours_are_glues(self):
        assert has(errors_of(spark_block(roleArn=ROLE_ARN)), "roleArn is backend glue's")
        assert has(errors_of(spark_block(runEveryHours=24)), "runEveryHours is backend glue's")


class TestGluePlacement:
    def test_full_block(self):
        tm = one(
            glue_block(
                runEveryHours=12,
                compaction={"strategy": "z-order"},
                expireSnapshots={"olderThan": "P7D", "retainLast": 5, "cleanExpiredFiles": False},
                removeOrphanFiles={"olderThan": "P4D"},
            )
        )
        assert tm.backend == "glue"
        assert tm.role_arn == ROLE_ARN
        assert tm.run_every_hours == 12
        assert tm.schedule is None and tm.build_id is None
        assert tm.compaction.strategy == "z-order"
        assert tm.expire == ExpireSnapshots(7 * 86400, 5, False)
        assert tm.orphans.older_than_seconds == 4 * 86400

    def test_run_every_hours_defaults_to_a_day(self):
        assert one(glue_block()).run_every_hours == 24

    def test_role_arn_required(self):
        block = glue_block()
        del block["roleArn"]
        assert has(errors_of(block), "roleArn is required on backend glue")

    @pytest.mark.parametrize(
        "arn",
        [
            "arn:aws:iam::123:role/x",
            "arn:aws:iam::123456789012:user/x",
            "arn:aws:iam::123456789012:role/",
            "arn:aws:iam::123456789012:role/x y",
            "arn:aws:iam::123456789012:role/x\n",
            "glue-optimizer",
            5,
        ],
    )
    def test_role_arn_pattern(self, arn):
        assert has(errors_of(glue_block(roleArn=arn)), "roleArn is required on backend glue")

    @pytest.mark.parametrize(
        "arn",
        [
            "arn:aws-cn:iam::123456789012:role/path/to/r.o_l+e=1,2@x-y",
            "arn:aws-us-gov:iam::123456789012:role/r",
        ],
    )
    def test_role_arn_other_partitions(self, arn):
        assert one(glue_block(roleArn=arn)).role_arn == arn

    @pytest.mark.parametrize("hours", [2, 169, 0, 1.5, "24", True])
    def test_run_every_hours_bounds(self, hours):
        assert has(errors_of(glue_block(runEveryHours=hours)), "runEveryHours must be a whole")

    @pytest.mark.parametrize("hours", [3, 168])
    def test_run_every_hours_edges(self, hours):
        # It paces the retention and orphan optimizers, so one of them is declared.
        block = glue_block(runEveryHours=hours, expireSnapshots={})
        assert one(block).run_every_hours == hours

    def test_whole_days_only(self):
        assert has(
            errors_of(glue_block(expireSnapshots={"olderThan": "PT36H"})),
            "whole days on backend glue",
        )
        assert has(
            errors_of(glue_block(removeOrphanFiles={"olderThan": "P1DT1H"})),
            "whole days on backend glue",
        )

    def test_z_order_is_allowed_on_glue_only(self):
        assert one(glue_block(compaction={"strategy": "z-order"})).compaction.strategy == "z-order"
        assert one(glue_block(compaction={"strategy": "sort"})).compaction.strategy == "sort"

    def test_target_file_size_is_refused(self):
        errors = errors_of(glue_block(compaction={"targetFileSizeMb": 128}))
        assert has(errors, "write.target-file-size-bytes")

    def test_the_legacy_target_file_size_is_not_glues(self):
        """The legacy field is forge's runner's default: Glue reads the table property, so the
        field is left to it rather than refused for a block that never set it."""
        table = one(glue_block(compaction={}), legacy={"compaction": {"targetFileSizeMb": 64}})
        assert table.compaction is not None and table.compaction.target_file_size_mb is None

    @pytest.mark.parametrize("key", ["maxCommits", "maxConcurrentRewrites"])
    def test_runner_only_compaction_keys_are_refused(self, key):
        errors = errors_of(glue_block(compaction={key: 3}))
        assert has(errors, f"compaction.{key} is forge's runner's")

    def test_rewrite_manifests_is_refused(self):
        errors = errors_of(glue_block(rewriteManifests={}))
        assert has(errors, "Glue's optimizers rewrite no manifests")

    def test_actions_are_refused(self):
        errors = errors_of(glue_block(actions=[{"name": "a", "kind": "k"}]))
        assert has(errors, "a plugin's actions run with forge's Spark runner")

    @pytest.mark.parametrize(
        "key, value", [("schedule", "0 3 * * *"), ("build", "ingest-orders"), ("timezone", "UTC")]
    )
    def test_spark_placement_keys_are_refused(self, key, value):
        errors = errors_of(glue_block(**{key: value}))
        assert has(errors, f"{key} is backend spark's")

    def test_another_catalog_is_refused(self):
        contract = maintained(glue_block())
        contract["exposes"][0]["binding"]["location"]["catalog"] = "lakekeeper"
        tables, errors = read_maintenance(contract)
        assert tables == []
        assert has(errors, "backend glue maintains a table of the Glue Data Catalog")

    def test_explicit_glue_catalog_is_fine(self):
        contract = maintained(glue_block())
        contract["exposes"][0]["binding"]["location"]["catalog"] = "glue"
        assert read_maintenance(contract)[1] == []

    def test_another_platform_is_refused(self):
        contract = maintained(glue_block())
        contract["exposes"][0]["binding"]["platform"] = "gcp"
        assert has(read_maintenance(contract)[1], "platform aws")

    def test_glue_needs_no_writer_build(self):
        contract = maintained(glue_block())
        contract["builds"] = []
        assert read_maintenance(contract)[1] == []


class TestActions:
    def actions(self, *items, **kw):
        return errors_of(spark_block(actions=list(items)), **kw)

    def good(self, **over):
        item = {"name": "erase", "kind": "gdpr-erasure", "options": {"requests": "gdpr.requests"}}
        item.update(over)
        return item

    def test_read(self):
        tm = one(
            spark_block(
                compaction=None,
                actions=[
                    self.good(),
                    {"name": "second_1", "kind": "other_kind"},
                ],
            )
        )
        assert [(a.name, a.kind) for a in tm.actions] == [
            ("erase", "gdpr-erasure"),
            ("second_1", "other_kind"),
        ]
        assert tm.actions[0].options == {"requests": "gdpr.requests"}
        assert tm.actions[1].options == {}

    @pytest.mark.parametrize("name", ["", "1erase", "has space", "a" * 65, "x;y", 5, None, "é"])
    def test_name_grammar(self, name):
        assert has(self.actions(self.good(name=name)), ".name must be a plain name")

    @pytest.mark.parametrize("kind", ["", "Erase", "1k", "a" * 65, "has space", 5, None])
    def test_kind_grammar(self, kind):
        assert has(self.actions(self.good(kind=kind)), ".kind must name a plugin's kind")

    def test_name_of_64_characters_is_fine(self):
        assert one(spark_block(actions=[self.good(name="a" * 64)])).actions[0].name == "a" * 64

    def test_duplicate_names(self):
        errors = self.actions(self.good(), self.good(kind="other"))
        assert has(errors, "is an earlier action's too")

    def test_more_than_twenty(self):
        items = [self.good(name=f"a{i}") for i in range(21)]
        assert has(self.actions(*items), "holds more than 20 actions")
        assert len(one(spark_block(actions=items[:20])).actions) == 20

    def test_actions_must_be_a_list(self):
        assert has(errors_of(spark_block(actions={"name": "a"})), "actions must be a list")

    def test_item_must_be_a_mapping(self):
        assert has(self.actions("erase"), "actions[0] must be a mapping")

    def test_item_unknown_key(self):
        assert has(self.actions(self.good(color="red")), "has keys it does not read: color")

    def test_options_must_be_a_mapping(self):
        assert has(self.actions(self.good(options=["a"])), ".options must be a mapping")

    def test_json_values_in_options(self):
        tm = one(
            spark_block(
                actions=[
                    self.good(
                        options={
                            "a": None,
                            "b": True,
                            "c": 3,
                            "d": 1.5,
                            "e": "text",
                            "f": [1, "x", {"g": 2}],
                            "h": {"i": {"j": 1}},
                        }
                    )
                ]
            )
        )
        assert tm.actions[0].options["h"] == {"i": {"j": 1}}


class TestOptionProblems:
    """A refusal never shows the value that caused it."""

    SECRETS = [
        "AKIAIOSFODNN7EXAMPLE",  # pragma: allowlist secret
        "password=hunter2hunter2",  # pragma: allowlist secret
        "sk_live_" + "a1B2c3D4e5F6g7H8i9J0k1L2",  # pragma: allowlist secret
        "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8",  # pragma: allowlist secret
    ]

    def test_clean_values_pass(self):
        assert option_problems({"a": [1, 2, {"b": "plain text"}], "c": None}, "o") == []

    @pytest.mark.parametrize("secret", SECRETS)
    def test_secret_shaped_values_are_refused_without_the_value(self, secret):
        problems = option_problems({"token": secret}, "o")
        assert len(problems) == 1
        assert "looks like a secret" in problems[0]
        assert secret not in problems[0]
        for part in secret.split("=")[-1:]:
            assert part not in problems[0]

    @pytest.mark.parametrize("secret", SECRETS)
    def test_a_secret_never_reaches_a_read_error(self, secret):
        errors = errors_of(
            spark_block(actions=[{"name": "a", "kind": "k", "options": {"deep": [{"x": secret}]}}])
        )
        assert has(errors, "looks like a secret")
        for message in errors:
            assert secret not in message
            assert secret.split("=")[-1] not in message

    def test_variable_syntax_is_refused_without_the_value(self):
        value = "${env:SOME_UNIQUE_VALUE_42}"
        problems = option_problems({"k": value}, "o")
        assert problems and "'${'" in problems[0]
        assert "SOME_UNIQUE_VALUE_42" not in problems[0]
        errors = errors_of(
            spark_block(actions=[{"name": "a", "kind": "k", "options": {"k": value}}])
        )
        assert not any("SOME_UNIQUE_VALUE_42" in e for e in errors)

    @pytest.mark.parametrize("char", ["\x00", "\x07", "\n", "\t", "\x1b", "\x7f"])
    def test_control_characters_are_refused_without_the_value(self, char):
        value = f"before{char}after_marker"
        problems = option_problems({"k": value}, "o")
        assert problems and "control character" in problems[0]
        assert "after_marker" not in problems[0]

    def test_string_length_limit(self):
        assert option_problems({"k": "x" * 1000}, "o") == []
        assert "longer than 1000" in option_problems({"k": "x" * 1001}, "o")[0]

    def test_nesting_limit(self):
        ok = {"a": {"b": {"c": {"d": 1}}}}  # root is depth 0, value at depth 4
        assert option_problems(ok, "o") == []
        too_deep = {"a": {"b": {"c": {"d": {"e": 1}}}}}
        assert "nested deeper than 4 levels" in option_problems(too_deep, "o")[0]
        assert option_problems([[[[1]]]], "o") == []
        assert "nested deeper" in option_problems([[[[[1]]]]], "o")[0]

    def test_item_and_key_count_limits(self):
        assert option_problems(list(range(50)), "o") == []
        assert "more than 50 items" in option_problems(list(range(51)), "o")[0]
        assert option_problems({f"k{i}": i for i in range(50)}, "o") == []
        assert "more than 50 keys" in option_problems({f"k{i}": i for i in range(51)}, "o")[0]

    @pytest.mark.parametrize("key", ["1a", "has space", "a-b", "", "a.b", "k" * 65, 5])
    def test_keys_must_be_plain_names_and_are_not_shown(self, key):
        problems = option_problems({key: 1}, "o")
        assert problems and "not a plain name (key not shown)" in problems[0]

    @pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
    def test_non_finite_floats(self, value):
        assert "not a finite number" in option_problems({"k": value}, "o")[0]

    @pytest.mark.parametrize("value", [b"bytes", object(), {1, 2}, 1 + 2j])
    def test_non_json_values(self, value):
        assert "not a JSON value" in option_problems({"k": value}, "o")[0]

    def test_where_names_the_position(self):
        problems = option_problems({"a": [1, {"b": object()}]}, "w")
        assert problems[0].startswith("w.a[1].b is a object")


class TestDeclaresAndSparkTables:
    def test_declares_maintenance(self):
        assert declares_maintenance(maintained(spark_block())) is True
        assert declares_maintenance(maintained(None)) is False
        assert declares_maintenance(maintained({})) is True  # declared, however empty
        assert declares_maintenance({}) is False
        assert declares_maintenance({"exposes": ["x", {"iceberg": "no"}]}) is False
        assert declares_maintenance(copy.deepcopy(CONTRACT)) is False

    def test_spark_tables_returns_the_spark_ones(self):
        tables = spark_tables(maintained(spark_block()))
        assert [t.expose_id for t in tables] == ["order_lines"]

    def test_spark_tables_skips_glue_tables(self):
        assert spark_tables(maintained(glue_block())) == []

    def test_spark_tables_without_maintenance(self):
        assert spark_tables(maintained(None)) == []

    def test_spark_tables_raises_the_first_error(self):
        with pytest.raises(ValueError, match="schedule is required"):
            spark_tables(maintained({"compaction": {}}))

    def test_spark_tables_raises_even_when_only_a_glue_table_is_wrong(self):
        with pytest.raises(ValueError, match="roleArn"):
            spark_tables(maintained({"backend": "glue", "compaction": {}}))
