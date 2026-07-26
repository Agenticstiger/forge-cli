# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Packaging-modes PR3 — planner truthfulness.

RFC-packaging-modes.md file 8. ``plan.json`` is the digest-bound artifact
a human approves; under ``mode: shared`` it used to list a create action
for the pool container the emit path refuses to own. The tofu gate kept
that safe, the review contract did not survive it.

Layers:

* ``TestPackagingSummary`` — the ownership block an approver reads.
* ``TestDroppedContainerActions`` — REFERENCED container-creation actions
  leave the plan, and are itemised rather than silently vanished.
* ``TestLegacyPlanIsUntouched`` — no ``packaging`` block ⇒ no new key and
  no action churn (the digest-stability invariant).
* ``TestOpAndActionTypeSurvive`` — the CLAUDE.md invariant: every surviving
  action still carries BOTH ``op`` and ``action_type``.
* ``TestPlanCliWiring`` — the chokepoint genuinely runs inside
  ``cli/plan.py::run``, before ``inject_digests``.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from fluid_build.iac.plan_packaging import (
    CONTAINER_CREATION_OPS,
    apply_packaging_to_plan,
    build_packaging_summary,
    filter_referenced_container_actions,
)

pytestmark = pytest.mark.unit


SHARED = {"mode": "shared", "pool": "acme-pool"}
ISOLATED = {"mode": "isolated", "pool": "acme-pool"}
HYBRID = {
    "mode": "shared",
    "pool": "sales-domain",
    "containers": {"schema": "isolated", "warehouse": "isolated"},
}


def _contract(packaging: Dict[str, Any] | None = None) -> Dict[str, Any]:
    contract: Dict[str, Any] = {
        "fluidVersion": "0.7.6",
        "id": "telemetry-sdp",
        "exposes": [
            {
                "exposeId": "telemetry",
                "binding": {
                    "platform": "aws",
                    "format": "iceberg",
                    "location": {
                        "bucket": "acme-iot-lake",
                        "path": "telemetry/",
                        "database": "iot_pool",
                        "table": "telemetry",
                    },
                },
            }
        ],
    }
    if packaging is not None:
        contract["packaging"] = packaging
    return contract


def _native_actions() -> List[Dict[str, Any]]:
    """The shape ``providers/aws/plan/planner.py`` emits."""
    return [
        {"op": "glue.ensure_database", "id": "database_iot_pool", "database": "iot_pool"},
        {"op": "s3.ensure_bucket", "id": "bucket_acme_iot_lake", "bucket": "acme-iot-lake"},
        {"op": "glue.ensure_table", "id": "table_telemetry", "table": "telemetry"},
    ]


def _abstract_actions() -> List[Dict[str, Any]]:
    """The shape ``_plan_with_provider_actions`` emits (op + action_type)."""
    return [
        {
            "step": 1,
            "action_id": "provision_telemetry",
            "op": "provisionDataset",
            "action_type": "provisionDataset",
            "provider": "aws",
            "params": {},
            "depends_on": [],
        },
        {
            "step": 2,
            "action_id": "grant_telemetry_analyst",
            "op": "grantAccess",
            "action_type": "grantAccess",
            "provider": "aws",
            "params": {},
            "depends_on": ["provision_telemetry"],
        },
    ]


class TestPackagingSummary:
    def test_shared_summary_reports_every_container_as_referenced(self):
        summary = build_packaging_summary(_contract(SHARED))
        assert summary["pool"] == "acme-pool"
        assert set(summary["containers"].values()) == {"referenced"}

    def test_isolated_summary_reports_every_container_as_owned(self):
        summary = build_packaging_summary(_contract(ISOLATED))
        assert set(summary["containers"].values()) == {"owned"}

    def test_hybrid_summary_shows_the_mixed_tier(self):
        summary = build_packaging_summary(_contract(HYBRID))
        assert summary["containers"]["database"] == "referenced"
        assert summary["containers"]["schema"] == "owned"
        assert summary["containers"]["warehouse"] == "owned"

    def test_the_summary_covers_all_six_container_kinds(self):
        from fluid_build.iac.packaging import CONTAINER_KINDS

        summary = build_packaging_summary(_contract(SHARED))
        assert set(summary["containers"]) == set(CONTAINER_KINDS)

    def test_a_per_exposure_override_is_reported_separately(self):
        contract = _contract(ISOLATED)
        contract["exposes"][0]["binding"]["packaging"] = {"mode": "shared", "pool": "other"}
        summary = build_packaging_summary(contract)
        assert summary["containers"]["bucket"] == "owned"
        assert summary["exposures"][0]["exposeId"] == "telemetry"
        assert summary["exposures"][0]["containers"]["bucket"] == "referenced"
        assert summary["exposures"][0]["pool"] == "other"

    def test_pool_manifest_is_surfaced_when_declared(self):
        summary = build_packaging_summary(
            _contract({"mode": "shared", "pool": "p", "poolManifest": "pools/sales.yaml"})
        )
        assert summary["poolManifest"] == "pools/sales.yaml"

    def test_no_manifest_key_when_undeclared(self):
        assert "poolManifest" not in build_packaging_summary(_contract(SHARED))

    def test_legacy_returns_none_not_an_empty_dict(self):
        """None so plan.json gains no key at all — digest stability."""
        assert build_packaging_summary(_contract(None)) is None


class TestDroppedContainerActions:
    def test_shared_drops_the_bucket_and_database_creates(self):
        kept, dropped = filter_referenced_container_actions(_contract(SHARED), _native_actions())
        assert [a["op"] for a in kept] == ["glue.ensure_table"]
        assert {d["op"] for d in dropped} == {"glue.ensure_database", "s3.ensure_bucket"}

    def test_isolated_drops_nothing(self):
        kept, dropped = filter_referenced_container_actions(_contract(ISOLATED), _native_actions())
        assert len(kept) == 3
        assert dropped == []

    def test_hybrid_drops_only_the_shared_tier(self):
        actions = [
            {"op": "sf.database.ensure", "id": "db"},
            {"op": "sf.schema.ensure", "id": "sch"},
            {"op": "sf.warehouse.ensure", "id": "wh"},
            {"op": "sf.table.ensure", "id": "tbl"},
        ]
        kept, dropped = filter_referenced_container_actions(_contract(HYBRID), actions)
        assert [a["op"] for a in kept] == [
            "sf.schema.ensure",
            "sf.warehouse.ensure",
            "sf.table.ensure",
        ]
        assert [d["op"] for d in dropped] == ["sf.database.ensure"]

    def test_leaf_actions_are_never_dropped(self):
        actions = [
            {"op": "glue.ensure_table", "id": "t"},
            {"op": "sf.grant.privilege", "id": "g"},
            {"op": "sf.task.ensure", "id": "task"},
        ]
        kept, dropped = filter_referenced_container_actions(_contract(SHARED), actions)
        assert len(kept) == 3 and dropped == []

    def test_the_abstract_provision_action_is_never_dropped(self):
        """provisionDataset is per-exposure, not per-container — dropping it
        would gut the plan rather than make it truthful."""
        kept, dropped = filter_referenced_container_actions(_contract(SHARED), _abstract_actions())
        assert len(kept) == 2 and dropped == []

    def test_dropped_actions_are_itemised_in_the_plan(self):
        plan = {"actions": _native_actions(), "total_actions": 3}
        out = apply_packaging_to_plan(plan, _contract(SHARED))
        assert out["total_actions"] == 1
        records = out["packaging"]["droppedActions"]
        assert {r["op"] for r in records} == {"glue.ensure_database", "s3.ensure_bucket"}
        assert all(r["reason"] == "referenced" for r in records)
        assert {r["actionId"] for r in records} == {"database_iot_pool", "bucket_acme_iot_lake"}

    def test_no_dropped_key_when_nothing_was_dropped(self):
        plan = {"actions": _native_actions(), "total_actions": 3}
        out = apply_packaging_to_plan(plan, _contract(ISOLATED))
        assert "droppedActions" not in out["packaging"]
        assert out["total_actions"] == 3

    def test_a_malformed_block_never_drops_actions(self):
        kept, dropped = filter_referenced_container_actions(
            _contract({"mode": "nonsense"}), _native_actions()
        )
        assert len(kept) == 3 and dropped == []

    def test_every_mapped_op_names_a_real_container_kind(self):
        from fluid_build.iac.packaging import CONTAINER_KINDS

        assert set(CONTAINER_CREATION_OPS.values()) <= set(CONTAINER_KINDS)


class TestPlanBookkeepingStaysConsistent:
    def test_steps_are_renumbered_without_holes(self):
        actions = [
            {"step": 1, "action_id": "b", "op": "s3.ensure_bucket", "action_type": "x"},
            {"step": 2, "action_id": "t", "op": "glue.ensure_table", "action_type": "x"},
        ]
        out = apply_packaging_to_plan({"actions": actions, "total_actions": 2}, _contract(SHARED))
        assert [a["step"] for a in out["actions"]] == [1]

    def test_dangling_dependencies_on_dropped_actions_are_pruned(self):
        actions = [
            {"action_id": "bucket", "op": "s3.ensure_bucket"},
            {"action_id": "table", "op": "glue.ensure_table", "depends_on": ["bucket"]},
        ]
        out = apply_packaging_to_plan({"actions": actions, "total_actions": 2}, _contract(SHARED))
        assert out["actions"][0]["depends_on"] == []

    def test_the_dependency_graph_is_rebuilt(self):
        actions = [
            {"action_id": "bucket", "op": "s3.ensure_bucket"},
            {"action_id": "table", "op": "glue.ensure_table", "depends_on": ["bucket"]},
        ]
        plan = {
            "actions": actions,
            "total_actions": 2,
            "has_dependencies": True,
            "dependency_graph": {"nodes": ["bucket", "table"], "edges": [("table", "bucket")]},
        }
        out = apply_packaging_to_plan(plan, _contract(SHARED))
        assert out["dependency_graph"] == {"nodes": ["table"], "edges": []}
        assert out["has_dependencies"] is False


class TestOpAndActionTypeSurvive:
    """CLAUDE.md: dropping either field silently breaks the pipeline."""

    @pytest.mark.parametrize("packaging", [SHARED, ISOLATED, HYBRID, None])
    def test_both_fields_survive_every_mode(self, packaging):
        plan = {"actions": _abstract_actions(), "total_actions": 2}
        out = apply_packaging_to_plan(plan, _contract(packaging))
        for action in out["actions"]:
            assert action["op"] == action["action_type"]
            assert action["op"]


class TestLegacyPlanIsUntouched:
    def test_no_packaging_key_is_added(self):
        plan = {"actions": _native_actions(), "total_actions": 3}
        out = apply_packaging_to_plan(plan, _contract(None))
        assert "packaging" not in out

    def test_actions_are_identical(self):
        original = _native_actions()
        plan = {"actions": list(original), "total_actions": 3}
        out = apply_packaging_to_plan(plan, _contract(None))
        assert out["actions"] == original
        assert out["total_actions"] == 3

    def test_the_plan_object_is_returned_unchanged(self):
        plan = {"actions": [], "total_actions": 0}
        assert apply_packaging_to_plan(plan, _contract(None)) is plan


class TestPlanCliWiring:
    """The chokepoint must genuinely run in cli/plan.py — not just exist."""

    def test_run_calls_the_chokepoint_before_inject_digests(self):
        import inspect

        from fluid_build.cli import plan as plan_mod

        source = inspect.getsource(plan_mod.run)
        applied_at = source.index("apply_packaging_to_plan(plan, contract)")
        digests_at = source.index("inject_digests(plan")
        assert applied_at < digests_at

    def test_the_import_is_function_local_for_the_help_cold_path(self):
        """`iac` pulls every provider plugin — a module-scope import here
        would land it on `fluid --help`."""
        import inspect

        from fluid_build.cli import plan as plan_mod

        module_source = inspect.getsource(plan_mod)
        head = module_source.split("def register(", 1)[0]
        assert "iac.plan_packaging" not in head
        assert "iac.plan_packaging" in inspect.getsource(plan_mod.run)


class TestTheSummaryIsActuallyRendered:
    """RFC file 8 stamps ownership into plan.json "so approvers see effective
    ownership without recomputing precedence" — but nothing rendered it.

    Two twin contracts differing only in ``packaging.mode`` (one owning a
    database + schema + warehouse, the other owning nothing but a leaf table)
    produced BYTE-IDENTICAL ``fluid plan`` terminal output. The signal existed
    and was invisible to the only human in the loop.
    """

    def _plan(self, contract):
        from fluid_build.iac.plan_packaging import apply_packaging_to_plan

        actions = _native_actions()
        base = {
            "total_actions": len(actions),
            "actions": actions,
            "contract": {"name": "Telemetry SDP", "version": "0.7.6"},
        }
        return apply_packaging_to_plan(base, contract)

    def test_shared_and_isolated_no_longer_render_identically(self):
        from fluid_build.cli.plan import _packaging_summary_lines

        shared = "\n".join(_packaging_summary_lines(self._plan(_contract(SHARED))))
        isolated = "\n".join(_packaging_summary_lines(self._plan(_contract(ISOLATED))))
        assert shared and isolated
        assert shared != isolated

    def test_the_shared_summary_names_the_pool(self):
        from fluid_build.cli.plan import _packaging_summary_lines

        text = "\n".join(_packaging_summary_lines(self._plan(_contract(SHARED))))
        assert "acme-pool" in text
        assert "owned by this product:  none" in text

    def test_the_isolated_summary_lists_what_is_owned(self):
        from fluid_build.cli.plan import _packaging_summary_lines

        text = "\n".join(_packaging_summary_lines(self._plan(_contract(ISOLATED))))
        assert "referenced (not owned): none" in text
        assert "database" in text

    def test_dropped_container_actions_are_named(self):
        from fluid_build.cli.plan import _packaging_summary_lines

        plan = self._plan(_contract(SHARED))
        if not plan["packaging"].get("droppedActions"):
            pytest.skip("this action fixture has no droppable container ops")
        text = "\n".join(_packaging_summary_lines(plan))
        assert "dropped" in text

    def test_a_legacy_plan_renders_nothing_new(self):
        """No packaging block ⇒ no key in plan.json ⇒ no extra output."""
        from fluid_build.cli.plan import _packaging_summary_lines

        assert _packaging_summary_lines(self._plan(_contract(None))) == []
        assert _packaging_summary_lines({"total_actions": 0, "actions": []}) == []


class TestTheApplyPathFiltersToo:
    """``apply_packaging_to_plan`` was wired into cli/plan.py only.

    ``fluid apply`` on a contract owning exactly one leaf table announced three
    actions (``sf.database.ensure`` + ``sf.schema.ensure`` + the table) while
    ``tofu`` correctly planned ``+1``. Verified live against Snowflake.
    """

    _SF_ACTIONS = [
        {"op": "sf.database.ensure", "action_id": "database_POOL"},
        {"op": "sf.schema.ensure", "action_id": "schema_POOL_S"},
        {"op": "sf.table.ensure", "action_id": "table_POOL_S_T"},
    ]

    def _contract_sf(self, packaging):
        contract = {
            "fluidVersion": "0.7.6",
            "id": "silver.misc.tenant_v1",
            "exposes": [
                {
                    "exposeId": "t",
                    "binding": {
                        "platform": "snowflake",
                        "format": "snowflake_table",
                        "location": {"database": "POOL", "schema": "S", "table": "T"},
                    },
                }
            ],
        }
        if packaging is not None:
            contract["packaging"] = packaging
        return contract

    def test_referenced_container_ops_are_dropped(self, caplog):
        import logging as _logging

        from fluid_build.cli.generate_iac import _drop_referenced_container_actions

        kept = _drop_referenced_container_actions(
            self._contract_sf(SHARED), list(self._SF_ACTIONS), _logging.getLogger("t")
        )
        assert [a["op"] for a in kept] == ["sf.table.ensure"]

    def test_an_isolated_contract_keeps_every_action(self):
        import logging as _logging

        from fluid_build.cli.generate_iac import _drop_referenced_container_actions

        kept = _drop_referenced_container_actions(
            self._contract_sf(ISOLATED), list(self._SF_ACTIONS), _logging.getLogger("t")
        )
        assert kept == self._SF_ACTIONS

    def test_a_legacy_contract_is_untouched_by_identity(self):
        import logging as _logging

        from fluid_build.cli.generate_iac import _drop_referenced_container_actions

        actions = list(self._SF_ACTIONS)
        assert (
            _drop_referenced_container_actions(
                self._contract_sf(None), actions, _logging.getLogger("t")
            )
            is actions
        )

    def test_the_effective_count_is_emitted_for_ci(self, caplog):
        import json as _json
        import logging as _logging

        from fluid_build.cli.generate_iac import _drop_referenced_container_actions

        logger = _logging.getLogger("test_packaging_filter_event")
        with caplog.at_level(_logging.DEBUG, logger=logger.name):
            _drop_referenced_container_actions(
                self._contract_sf(SHARED), list(self._SF_ACTIONS), logger
            )
        events = [
            _json.loads(r.getMessage())
            for r in caplog.records
            if r.getMessage().startswith("{") and "packaging_actions_filtered" in r.getMessage()
        ]
        assert events, "the apply path must report the effective action count"
        assert events[0]["planner_actions_count"] == 3
        assert events[0]["actions_count"] == 1

    def test_the_apply_path_really_calls_the_filter(self):
        """Wiring pin — the fix is in native_actions, not only in cli/plan.py."""
        import inspect

        from fluid_build.cli import generate_iac

        assert "_drop_referenced_container_actions(" in inspect.getsource(
            generate_iac.native_actions
        )
