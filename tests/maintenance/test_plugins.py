# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid_build.maintenance.plugins``: action kinds from the entry-point group."""

from __future__ import annotations

import logging

import pytest

from fluid_build.maintenance import MaintenanceAction
from fluid_build.maintenance.plugins import (
    ACTIONS_GROUP,
    MaintenanceActionError,
    action_for,
    normalise_kind,
    reset_maintenance_plugin_cache,
)
from tests.maintenance.conftest import FakeEntryPoint


def _bare_object_factory() -> object:
    """An entry point may name a function: this one returns an object without statements."""
    return object()


class Erasure:
    """A kind that does not subclass the SDK base: forge calls the same methods on any object."""

    def statements(self, table, options):
        return [f"DELETE FROM {table} WHERE id = 1"]


class Built(Erasure):
    made = 0

    def __init__(self):
        type(self).made += 1


def refusal(name):
    with pytest.raises(MaintenanceActionError) as info:
        action_for(name)
    return str(info.value)


def test_the_group_name():
    assert ACTIONS_GROUP == "fluid_build.maintenance_actions"


class TestNormalise:
    @pytest.mark.parametrize(
        "name, key",
        [
            ("GDPR_Erasure", "gdpr-erasure"),
            ("  gdpr-erasure ", "gdpr-erasure"),
            ("Gdpr_erasure", "gdpr-erasure"),
            ("a_b-c_D", "a-b-c-d"),
        ],
    )
    def test_lower_case_and_dashes(self, name, key):
        assert normalise_kind(name) == key

    def test_a_kind_is_found_by_either_spelling(self, entry_points):
        entry_points(FakeEntryPoint("GDPR_Erasure", Erasure))
        assert isinstance(action_for("gdpr-erasure"), Erasure)
        assert isinstance(action_for("Gdpr_Erasure"), Erasure)


class TestLoading:
    def test_a_class_is_built_with_no_arguments(self, entry_points):
        Built.made = 0
        entry_points(FakeEntryPoint("built", Built))
        found = action_for("built")
        assert isinstance(found, Built)
        assert Built.made == 1

    def test_an_instance_is_used_as_it_is(self, entry_points):
        instance = Erasure()
        entry_points(FakeEntryPoint("inst", instance))
        assert action_for("inst") is instance

    def test_a_factory_function_is_called(self, entry_points):
        instance = Erasure()
        calls = []

        def factory():
            calls.append(1)
            return instance

        entry_points(FakeEntryPoint("fact", factory))
        assert action_for("fact") is instance
        assert calls == [1]

    def test_a_subclass_of_the_sdk_base_loads(self, entry_points):
        class Mine(MaintenanceAction):
            def statements(self, table, options):
                return []

        entry_points(FakeEntryPoint("mine", Mine))
        assert isinstance(action_for("mine"), Mine)

    def test_the_sdk_base_defaults(self):
        base = MaintenanceAction()
        assert base.check({"a": 1}) == ()
        with pytest.raises(NotImplementedError, match="MaintenanceAction.statements"):
            base.statements("fluid.a.b", {})

    def test_an_object_with_a_callable_statements_is_not_called_again(self, entry_points):
        class CallableAction(Erasure):
            def __call__(self):  # would replace it if forge called every callable
                return None

        instance = CallableAction()
        entry_points(FakeEntryPoint("c", instance))
        assert action_for("c") is instance

    def test_nothing_loads_until_asked(self, entry_points):
        ep = FakeEntryPoint("lazy", Erasure)
        entry_points(ep)
        assert ep.loads == 0
        action_for("lazy")
        assert ep.loads == 1

    def test_listing_other_names_loads_none(self, entry_points):
        a, b = FakeEntryPoint("a", Erasure), FakeEntryPoint("b", Erasure)
        entry_points(a, b)
        action_for("a")
        assert (a.loads, b.loads) == (1, 0)

    def test_a_loaded_kind_is_cached(self, entry_points):
        ep = FakeEntryPoint("once", Erasure)
        entry_points(ep)
        assert action_for("once") is action_for("once")
        assert ep.loads == 1

    def test_the_cache_resets(self, entry_points):
        ep = FakeEntryPoint("once", Erasure)
        entry_points(ep)
        first = action_for("once")
        reset_maintenance_plugin_cache()
        assert action_for("once") is not first
        assert ep.loads == 2

    def test_the_real_scanner_is_read_through_the_plugin_manager(self, monkeypatch):
        from fluid_build import plugin_manager

        seen = []

        def scanner(group):
            seen.append(group)
            return []

        monkeypatch.setattr(plugin_manager, "_entry_points", scanner)
        reset_maintenance_plugin_cache()
        assert "no installed package registers it" in refusal("anything")
        assert seen == [ACTIONS_GROUP]

    def test_an_index_is_rebuilt_when_the_scanner_changes(self, monkeypatch, entry_points):
        entry_points(FakeEntryPoint("old", Erasure))
        action_for("old")
        # a new scanner (a process that installed a plugin) is read without a reset
        from fluid_build import plugin_manager

        monkeypatch.setattr(
            plugin_manager,
            "_entry_points",
            lambda group: [FakeEntryPoint("new", Erasure)],
        )
        assert isinstance(action_for("new"), Erasure)


class TestMissing:
    def test_no_package_registers_it(self, entry_points):
        entry_points()
        message = refusal("nothing")
        assert "no maintenance action kind 'nothing' can be used" in message
        assert "no installed package registers it" in message
        assert ACTIONS_GROUP in message

    def test_the_message_names_the_normalised_kind(self, entry_points):
        entry_points()
        assert "'some-kind'" in refusal("Some_Kind")

    def test_it_is_an_error_a_caller_catches_as_value_error(self, entry_points):
        entry_points()
        with pytest.raises(ValueError):
            action_for("x")


class TestPolicy:
    def test_blocklist_refuses_before_import(self, entry_points, monkeypatch):
        ep = FakeEntryPoint("gdpr-erasure", Erasure)
        entry_points(ep)
        monkeypatch.setenv("FLUID_PLUGINS_BLOCKLIST", "gdpr-erasure")
        assert "FLUID_PLUGINS_ALLOWLIST / FLUID_PLUGINS_BLOCKLIST refuse it" in refusal(
            "gdpr-erasure"
        )
        assert ep.loads == 0

    def test_blocklist_matches_across_dash_and_case(self, entry_points, monkeypatch):
        ep = FakeEntryPoint("Gdpr_Erasure", Erasure)
        entry_points(ep)
        monkeypatch.setenv("FLUID_PLUGINS_BLOCKLIST", "GDPR-ERASURE")
        refusal("gdpr-erasure")
        assert ep.loads == 0

    def test_allowlist_that_omits_the_kind_refuses_before_import(self, entry_points, monkeypatch):
        ep = FakeEntryPoint("gdpr-erasure", Erasure)
        entry_points(ep)
        monkeypatch.setenv("FLUID_PLUGINS_ALLOWLIST", "something-else")
        assert "refuse it" in refusal("gdpr-erasure")
        assert ep.loads == 0

    def test_allowlist_that_names_the_kind_loads_it(self, entry_points, monkeypatch):
        entry_points(FakeEntryPoint("gdpr-erasure", Erasure), FakeEntryPoint("other", Erasure))
        monkeypatch.setenv("FLUID_PLUGINS_ALLOWLIST", "gdpr_erasure")
        assert isinstance(action_for("gdpr-erasure"), Erasure)
        assert "refuse it" in refusal("other")

    def test_the_blocklist_wins_over_the_allowlist(self, entry_points, monkeypatch):
        entry_points(FakeEntryPoint("k", Erasure))
        monkeypatch.setenv("FLUID_PLUGINS_ALLOWLIST", "k")
        monkeypatch.setenv("FLUID_PLUGINS_BLOCKLIST", "k")
        assert "refuse it" in refusal("k")

    def test_the_policy_is_checked_before_the_two_distribution_check(
        self, entry_points, monkeypatch
    ):
        entry_points(
            FakeEntryPoint("k", Erasure, dist="one"), FakeEntryPoint("k", Erasure, dist="two")
        )
        monkeypatch.setenv("FLUID_PLUGINS_BLOCKLIST", "k")
        assert "refuse it" in refusal("k")


class TestTwoDistributions:
    def test_both_are_refused_and_named(self, entry_points):
        a = FakeEntryPoint("erase", Erasure, dist="acme-pack")
        b = FakeEntryPoint("erase", Erasure, dist="rival-pack")
        entry_points(a, b)
        message = refusal("erase")
        assert "2 distributions ship it" in message
        assert "acme-pack" in message and "rival-pack" in message
        assert "neither was imported" in message
        assert (a.loads, b.loads) == (0, 0)

    def test_a_name_that_differs_only_in_spelling_counts_as_one_name(self, entry_points):
        entry_points(
            FakeEntryPoint("gdpr_erasure", Erasure, dist="acme-pack"),
            FakeEntryPoint("Gdpr-Erasure", Erasure, dist="rival-pack"),
        )
        assert "2 distributions ship it" in refusal("gdpr-erasure")

    def test_an_unattributable_entry_point_is_named_so(self, entry_points):
        a = FakeEntryPoint("erase", Erasure)
        a.dist = None
        entry_points(a, FakeEntryPoint("erase", Erasure, dist="rival-pack"))
        message = refusal("erase")
        assert "<unknown distribution>" in message
        assert "rival-pack" in message


class TestFailures:
    def test_an_import_error_is_named_by_type_only(self, entry_points, caplog):
        entry_points(
            FakeEntryPoint("bad", error=ImportError("secret_module_path /home/u/.token_value"))
        )
        with caplog.at_level(logging.WARNING, logger="fluid.maintenance"):
            message = refusal("bad")
        assert "importing it raised ImportError" in message
        assert "secret_module_path" not in message
        assert all("secret_module_path" not in r.getMessage() for r in caplog.records)
        assert any("ImportError" in r.getMessage() for r in caplog.records)

    def test_any_exception_while_importing_is_isolated(self, entry_points):
        entry_points(FakeEntryPoint("bad", error=RuntimeError("boom_marker")))
        message = refusal("bad")
        assert "RuntimeError" in message
        assert "boom_marker" not in message

    def test_a_constructor_error_is_named_by_type_only(self, entry_points):
        class Broken:
            def __init__(self):
                raise KeyError("ctor_marker")

            def statements(self, table, options):
                return []

        entry_points(FakeEntryPoint("broken", Broken))
        message = refusal("broken")
        assert "building it raised KeyError" in message
        assert "ctor_marker" not in message

    def test_a_factory_error_is_named_by_type_only(self, entry_points):
        def factory():
            raise OSError("factory_marker")

        entry_points(FakeEntryPoint("f", factory))
        message = refusal("f")
        assert "building it raised OSError" in message
        assert "factory_marker" not in message

    @pytest.mark.parametrize("target", [object, object(), _bare_object_factory, 5, "text", None])
    def test_an_object_without_statements_is_not_an_action(self, entry_points, target):
        entry_points(FakeEntryPoint("odd", target))
        assert "it is not a maintenance action: it has no statements(table, options)" in refusal(
            "odd"
        )

    def test_a_non_callable_statements_attribute_is_not_an_action(self, entry_points):
        class Odd:
            statements = ["DELETE"]

        entry_points(FakeEntryPoint("odd", Odd))
        assert "not a maintenance action" in refusal("odd")

    def test_a_failed_load_is_not_retried_until_reset(self, entry_points):
        ep = FakeEntryPoint("bad", error=ImportError("x"))
        entry_points(ep)
        refusal("bad")
        refusal("bad")
        assert ep.loads == 1
        # the package gets fixed
        ep.error = None
        ep.target = Erasure
        refusal("bad")
        reset_maintenance_plugin_cache()
        assert isinstance(action_for("bad"), Erasure)

    def test_a_policy_refusal_is_cached_too(self, entry_points, monkeypatch):
        entry_points(FakeEntryPoint("k", Erasure))
        monkeypatch.setenv("FLUID_PLUGINS_BLOCKLIST", "k")
        refusal("k")
        monkeypatch.delenv("FLUID_PLUGINS_BLOCKLIST")
        refusal("k")  # still refused: the operator's policy is read once per process
        reset_maintenance_plugin_cache()
        assert isinstance(action_for("k"), Erasure)

    def test_a_missing_kind_is_cached_until_reset(self, entry_points):
        entry_points()
        refusal("late")
        entry_points(FakeEntryPoint("late", Erasure))  # installs a scanner and resets
        assert isinstance(action_for("late"), Erasure)
