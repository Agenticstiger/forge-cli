# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid_build.maintenance.job``: a table's maintenance job config and its actions' statements."""

from __future__ import annotations

import copy
import json

import pytest

from fluid_build.maintenance.job import (
    STEPS,
    action_statements,
    maintenance_config,
    table_name,
)
from fluid_build.maintenance.plugins import MaintenanceActionError
from fluid_build.maintenance.spec import (
    Compaction,
    CustomAction,
    ExpireSnapshots,
    RemoveOrphanFiles,
    TableMaintenance,
)
from tests.maintenance.conftest import EXPOSE, TABLE

BUILD_CONFIG = {
    "configVersion": 1,
    "product": "commerce.orders-stream",
    "build": "ingest-orders",
    "mode": "streaming",
    "source": {"kind": "kafka"},
    "checkpoint": {"uri": "s3://x/cp"},
    "sql": "SELECT 1",
    "sink": {
        "catalog": "fluid",
        "table": "commerce.order_lines",
        "create": ["CREATE TABLE ..."],
        "format": "iceberg",
    },
}


def tm(**over):
    base = dict(expose_id=EXPOSE, backend="spark", database="commerce", table="order_lines")
    base.update(over)
    return TableMaintenance(**base)


def steps_of(table, statements=()):
    return maintenance_config(table, BUILD_CONFIG, statements)["maintenance"]["steps"]


class TestTableName:
    def test_is_catalog_then_table(self):
        assert table_name(BUILD_CONFIG) == TABLE

    def test_reads_the_sink_only(self):
        assert table_name({"sink": {"catalog": "c", "table": "d.t"}, "other": 1}) == "c.d.t"


class TestConfigShape:
    def test_top_level(self):
        config = maintenance_config(tm(compaction=Compaction()), BUILD_CONFIG)
        assert config["configVersion"] == 1
        assert config["product"] == "commerce.orders-stream"
        assert config["build"] == "ingest-orders"
        assert config["mode"] == "maintenance"
        assert config["maintenance"]["expose"] == EXPOSE
        assert set(config) == {"configVersion", "product", "build", "mode", "sink", "maintenance"}

    def test_the_sink_loses_create_and_nothing_else(self):
        config = maintenance_config(tm(compaction=Compaction()), BUILD_CONFIG)
        assert "create" not in config["sink"]
        assert config["sink"] == {
            "catalog": "fluid",
            "table": "commerce.order_lines",
            "format": "iceberg",
        }

    def test_the_build_config_is_not_changed(self):
        before = copy.deepcopy(BUILD_CONFIG)
        maintenance_config(tm(compaction=Compaction()), BUILD_CONFIG)
        assert BUILD_CONFIG == before

    def test_no_stream_fields_are_carried(self):
        config = maintenance_config(tm(compaction=Compaction()), BUILD_CONFIG)
        for key in ("source", "checkpoint", "sql", "package", "deadLetter"):
            assert key not in config

    def test_json_serialisable_and_stable(self):
        table = tm(compaction=Compaction(), expire=ExpireSnapshots(), orphans=RemoveOrphanFiles())
        one = json.dumps(maintenance_config(table, BUILD_CONFIG), sort_keys=True)
        two = json.dumps(maintenance_config(table, BUILD_CONFIG), sort_keys=True)
        assert one == two

    def test_nothing_declared_has_no_steps(self):
        assert steps_of(tm()) == []

    def test_the_steps_constant(self):
        assert STEPS == (
            "statement",
            "rewrite_data_files",
            "rewrite_manifests",
            "expire_snapshots",
            "remove_orphan_files",
        )


class TestSteps:
    def test_order_and_contents_of_all_of_them(self):
        table = tm(
            compaction=Compaction(
                strategy="sort",
                target_file_size_mb=512,
                min_input_files=5,
                delete_file_threshold=2,
                max_commits=7,
                max_concurrent_rewrites=3,
            ),
            rewrite_manifests=True,
            expire=ExpireSnapshots(older_than_seconds=432000, retain_last=3),
            orphans=RemoveOrphanFiles(older_than_seconds=259200),
        )
        steps = steps_of(
            table, [("erase", ["DELETE FROM a", "DELETE FROM b"]), ("fix", ["UPDATE c"])]
        )
        assert steps == [
            {"step": "statement", "name": "erase", "sql": "DELETE FROM a"},
            {"step": "statement", "name": "erase", "sql": "DELETE FROM b"},
            {"step": "statement", "name": "fix", "sql": "UPDATE c"},
            {
                "step": "rewrite_data_files",
                "strategy": "sort",
                "options": {
                    "partial-progress.enabled": "true",
                    "partial-progress.max-commits": "7",
                    "target-file-size-bytes": str(512 * 1024 * 1024),
                    "min-input-files": "5",
                    "delete-file-threshold": "2",
                    "max-concurrent-file-group-rewrites": "3",
                },
            },
            {"step": "rewrite_manifests"},
            {"step": "expire_snapshots", "olderThanSeconds": 432000, "retainLast": 3},
            {"step": "remove_orphan_files", "olderThanSeconds": 259200},
        ]
        assert [s["step"] for s in steps] == [
            "statement",
            "statement",
            "statement",
            "rewrite_data_files",
            "rewrite_manifests",
            "expire_snapshots",
            "remove_orphan_files",
        ]

    def test_compaction_defaults_commit_with_partial_progress(self):
        (step,) = steps_of(tm(compaction=Compaction()))
        assert step == {
            "step": "rewrite_data_files",
            "strategy": "binpack",
            "options": {
                "partial-progress.enabled": "true",
                "partial-progress.max-commits": "10",
                "max-concurrent-file-group-rewrites": "5",
            },
        }

    def test_option_values_are_strings(self):
        (step,) = steps_of(
            tm(
                compaction=Compaction(
                    target_file_size_mb=1,
                    min_input_files=2,
                    delete_file_threshold=3,
                    max_concurrent_rewrites=4,
                )
            )
        )
        assert all(isinstance(v, str) for v in step["options"].values())
        assert step["options"]["target-file-size-bytes"] == "1048576"

    def test_target_size_is_megabytes_not_mebibyte_off_by_one(self):
        (step,) = steps_of(tm(compaction=Compaction(target_file_size_mb=128)))
        assert step["options"]["target-file-size-bytes"] == "134217728"

    def test_optional_options_are_left_out_when_unset(self):
        (step,) = steps_of(tm(compaction=Compaction(min_input_files=5)))
        assert set(step["options"]) == {
            "partial-progress.enabled",
            "partial-progress.max-commits",
            "max-concurrent-file-group-rewrites",
            "min-input-files",
        }

    def test_each_part_on_its_own(self):
        assert steps_of(tm(rewrite_manifests=True)) == [{"step": "rewrite_manifests"}]
        assert steps_of(tm(expire=ExpireSnapshots())) == [
            {"step": "expire_snapshots", "olderThanSeconds": 5 * 86400, "retainLast": 1}
        ]
        assert steps_of(tm(orphans=RemoveOrphanFiles())) == [
            {"step": "remove_orphan_files", "olderThanSeconds": 3 * 86400}
        ]

    def test_statements_only(self):
        assert steps_of(tm(), [("a", ["DELETE FROM x"])]) == [
            {"step": "statement", "name": "a", "sql": "DELETE FROM x"}
        ]

    def test_an_action_with_no_statements_adds_no_step(self):
        assert steps_of(tm(), [("a", [])]) == []

    def test_every_step_is_one_of_the_runners(self):
        table = tm(
            compaction=Compaction(),
            rewrite_manifests=True,
            expire=ExpireSnapshots(),
            orphans=RemoveOrphanFiles(),
        )
        for step in steps_of(table, [("a", ["DELETE FROM x"])]):
            assert step["step"] in STEPS


class StubKind:
    def __init__(self, statements=None, check=None, raises=None, check_raises=None):
        self._statements = statements
        self._check = check
        self.raises = raises
        self.check_raises = check_raises
        self.calls = []

    def check(self, options):
        self.calls.append(("check", options))
        if self.check_raises is not None:
            raise self.check_raises
        return self._check if self._check is not None else ()

    def statements(self, table, options):
        self.calls.append(("statements", table, options))
        if self.raises is not None:
            raise self.raises
        return self._statements


class NoCheck:
    def statements(self, table, options):
        return [f"DELETE FROM {table}"]


def with_actions(*actions):
    return tm(actions=tuple(actions))


def loader_of(**kinds):
    seen = []

    def load(kind):
        seen.append(kind)
        if kind not in kinds:
            raise MaintenanceActionError(f"no maintenance action kind {kind!r} can be used: gone")
        return kinds[kind]

    load.seen = seen
    return load


class TestActionStatements:
    def test_each_action_in_order(self):
        a = StubKind([f"DELETE FROM {TABLE} WHERE a = 1;", f"UPDATE {TABLE} SET a = 2"])
        b = StubKind([f"DELETE FROM {TABLE} WHERE b = 1"])
        table = with_actions(
            CustomAction("first", "kind-a", {"x": 1}), CustomAction("second", "kind-b")
        )
        load = loader_of(**{"kind-a": a, "kind-b": b})
        assert action_statements(table, TABLE, load=load) == [
            ("first", [f"DELETE FROM {TABLE} WHERE a = 1", f"UPDATE {TABLE} SET a = 2"]),
            ("second", [f"DELETE FROM {TABLE} WHERE b = 1"]),
        ]
        assert load.seen == ["kind-a", "kind-b"]

    def test_options_and_table_reach_the_kind_as_plain_copies(self):
        kind = StubKind([f"DELETE FROM {TABLE}"])
        options = {"requests": "gdpr.requests", "n": [1, 2]}
        table = with_actions(CustomAction("erase", "k", options))
        action_statements(table, TABLE, load=loader_of(k=kind))
        assert kind.calls == [
            ("check", options),
            ("statements", TABLE, options),
        ]
        assert kind.calls[0][1] is not table.actions[0].options

    def test_no_actions_no_statements(self):
        assert action_statements(tm(), TABLE, load=loader_of()) == []

    def test_a_kind_without_check_is_fine(self):
        table = with_actions(CustomAction("erase", "k"))
        assert action_statements(table, TABLE, load=loader_of(k=NoCheck())) == [
            ("erase", [f"DELETE FROM {TABLE}"])
        ]

    def test_the_default_loader_is_the_plugin_loader(self, entry_points):
        from tests.maintenance.conftest import FakeEntryPoint

        entry_points(FakeEntryPoint("gdpr-erasure", NoCheck))
        table = with_actions(CustomAction("erase", "gdpr-erasure"))
        assert action_statements(table, TABLE) == [("erase", [f"DELETE FROM {TABLE}"])]

    def test_an_unusable_kind_names_the_action(self):
        table = with_actions(CustomAction("erase", "missing"))
        with pytest.raises(MaintenanceActionError) as info:
            action_statements(table, TABLE, load=loader_of())
        message = str(info.value)
        assert message.startswith(f"exposes[{EXPOSE}].iceberg.maintenance.actions[erase]: ")
        assert "'missing'" in message

    def test_check_problems_raise_naming_the_action(self):
        kind = StubKind([f"DELETE FROM {TABLE}"], check=["requests must be a table", "second"])
        table = with_actions(CustomAction("erase", "k"))
        with pytest.raises(MaintenanceActionError) as info:
            action_statements(table, TABLE, load=loader_of(k=kind))
        assert str(info.value) == (
            f"exposes[{EXPOSE}].iceberg.maintenance.actions[erase]: requests must be a table"
        )
        assert not any(call[0] == "statements" for call in kind.calls)

    def test_a_long_check_problem_is_cut(self):
        kind = StubKind([], check=["x" * 1000])
        with pytest.raises(MaintenanceActionError) as info:
            action_statements(with_actions(CustomAction("a", "k")), TABLE, load=loader_of(k=kind))
        assert len(str(info.value)) < 400

    @pytest.mark.parametrize("problems", ["a problem as text", {"a": 1}, 5])
    def test_check_that_returns_no_list_is_refused(self, problems):
        kind = StubKind([], check=problems)
        with pytest.raises(MaintenanceActionError, match="check\\(\\) returned no list"):
            action_statements(with_actions(CustomAction("a", "k")), TABLE, load=loader_of(k=kind))

    def test_a_raising_check_is_named_by_type_only(self):
        kind = StubKind([], check_raises=RuntimeError("check_marker"))
        with pytest.raises(MaintenanceActionError) as info:
            action_statements(with_actions(CustomAction("a", "k")), TABLE, load=loader_of(k=kind))
        assert "its check() raised RuntimeError" in str(info.value)
        assert "check_marker" not in str(info.value)
        assert info.value.__cause__ is None

    def test_a_raising_statements_is_named_by_type_only(self):
        kind = StubKind(raises=KeyError("statements_marker"))
        with pytest.raises(MaintenanceActionError) as info:
            action_statements(
                with_actions(CustomAction("erase", "k")), TABLE, load=loader_of(k=kind)
            )
        message = str(info.value)
        assert message == (
            f"exposes[{EXPOSE}].iceberg.maintenance.actions[erase]: its statements() raised KeyError"
        )
        assert "statements_marker" not in message
        assert info.value.__cause__ is None

    def test_bad_sql_is_refused_and_not_quoted(self):
        kind = StubKind(["DROP TABLE marker_table_xyz"])
        with pytest.raises(MaintenanceActionError) as info:
            action_statements(
                with_actions(CustomAction("erase", "k")), TABLE, load=loader_of(k=kind)
            )
        message = str(info.value)
        assert "statement 0 must be one of DELETE, UPDATE, MERGE" in message
        assert "marker_table_xyz" not in message

    def test_a_statement_on_another_table_is_refused(self):
        kind = StubKind(["DELETE FROM fluid.commerce.other"])
        with pytest.raises(MaintenanceActionError, match="own table"):
            action_statements(with_actions(CustomAction("e", "k")), TABLE, load=loader_of(k=kind))

    @pytest.mark.parametrize("value", [None, "DELETE FROM x", 5])
    def test_statements_that_are_not_a_list(self, value):
        kind = StubKind(value)
        with pytest.raises(MaintenanceActionError, match="not a list of SQL statements"):
            action_statements(with_actions(CustomAction("e", "k")), TABLE, load=loader_of(k=kind))

    def test_a_later_action_is_not_asked_once_an_earlier_one_fails(self):
        bad = StubKind(raises=ValueError("x"))
        later = StubKind([f"DELETE FROM {TABLE}"])
        table = with_actions(CustomAction("a", "bad"), CustomAction("b", "later"))
        with pytest.raises(MaintenanceActionError):
            action_statements(table, TABLE, load=loader_of(bad=bad, later=later))
        assert later.calls == []

    def test_a_kind_object_without_statements_is_refused(self):
        class Empty:
            pass

        with pytest.raises(MaintenanceActionError, match="the kind has no statements\\(\\)"):
            action_statements(
                with_actions(CustomAction("e", "k")), TABLE, load=loader_of(k=Empty())
            )

    def test_the_error_is_a_value_error(self):
        with pytest.raises(ValueError):
            action_statements(with_actions(CustomAction("e", "nope")), TABLE, load=loader_of())
