# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Packaging-modes PR3 — the pre-plan ownership-transition guard.

RFC-packaging-modes.md file 10. Changing a container's packaging mode
changes who owns it; OpenTofu only sees a resource that left the
configuration and plans a destroy. The guard diffs ``tofu state list``
against the resolved ownership model *before* ``tofu plan`` and fails
closed.

Layers:

* ``TestParseStateAddress`` — the address grammar (``module.``, ``data.``,
  ``[0]`` / ``["k"]`` indices) the state listing emits.
* ``TestOwnedToReferenced`` — isolated → shared is always blocked, with
  copy-pasteable ``tofu state rm`` remediation.
* ``TestReferencedToOwned`` — shared → isolated needs
  ``--adopt-shared-container`` and a WARNING audit event.
* ``TestLegacyNeverTransitions`` — the compatibility invariant: a contract
  with no ``packaging`` block is a provable no-op.
* ``TestEngineWiring`` — the guard genuinely runs inside the apply engine,
  before adoption and before ``tofu plan`` (the pin is not vacuous).
"""

from __future__ import annotations

import logging
from typing import Any, Dict

import pytest

from fluid_build.iac.transition import (
    CONTAINER_RESOURCE_TYPES,
    OwnershipTransition,
    PackagingTransitionError,
    detect_ownership_transitions,
    guard_ownership_transitions,
    parse_state_address,
    state_rm_commands,
)

pytestmark = pytest.mark.unit


SHARED = {"mode": "shared", "pool": "acme-pool"}
ISOLATED = {"mode": "isolated", "pool": "acme-pool"}


def _contract(packaging: Dict[str, Any] | None = None, **extra: Any) -> Dict[str, Any]:
    contract: Dict[str, Any] = {
        "fluidVersion": "0.7.6",
        "id": "telemetry-sdp",
        "name": "Telemetry SDP",
        "metadata": {"layer": "Bronze", "productType": "SDP"},
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
    contract.update(extra)
    return contract


# A realistic `tofu state list` for a previously-isolated AWS contract.
OWNED_STATE = [
    "aws_s3_bucket.telemetry_sdp_acme_iot_lake",
    "aws_glue_catalog_database.telemetry_sdp_iot_pool",
    "aws_glue_catalog_table.telemetry_sdp_iot_pool_telemetry",
]

# The same contract after it was applied in shared mode.
REFERENCED_STATE = [
    "data.aws_s3_bucket.telemetry_sdp_acme_iot_lake",
    "aws_glue_catalog_table.telemetry_sdp_iot_pool_telemetry",
]


class TestParseStateAddress:
    @pytest.mark.parametrize(
        ("address", "expected"),
        [
            ("aws_s3_bucket.foo", (False, "aws_s3_bucket", "foo")),
            ("data.aws_s3_bucket.foo", (True, "aws_s3_bucket", "foo")),
            ("module.lake.aws_s3_bucket.foo", (False, "aws_s3_bucket", "foo")),
            ("module.a.module.b.aws_s3_bucket.foo", (False, "aws_s3_bucket", "foo")),
            ("module.lake.data.aws_s3_bucket.foo", (True, "aws_s3_bucket", "foo")),
            ("aws_s3_bucket.foo[0]", (False, "aws_s3_bucket", "foo")),
            ('aws_s3_bucket.foo["prod"]', (False, "aws_s3_bucket", "foo")),
            ("  aws_s3_bucket.foo  ", (False, "aws_s3_bucket", "foo")),
        ],
    )
    def test_grammar(self, address, expected):
        assert parse_state_address(address) == expected

    @pytest.mark.parametrize("address", ["", "   ", "aws_s3_bucket", "data", None, 42])
    def test_unparseable_is_none_not_a_guess(self, address):
        """An unrecognised shape must never be treated as a container."""
        assert parse_state_address(address) is None


class TestOwnedToReferenced:
    """isolated → shared. Always blocked; there is no flag for this."""

    def test_the_flip_is_detected(self):
        transitions = detect_ownership_transitions(_contract(SHARED), OWNED_STATE)
        assert {t.address for t in transitions} == {
            "aws_s3_bucket.telemetry_sdp_acme_iot_lake",
            "aws_glue_catalog_database.telemetry_sdp_iot_pool",
        }
        assert all(t.from_ownership == "owned" for t in transitions)
        assert all(t.to_ownership == "referenced" for t in transitions)

    def test_leaf_resources_never_transition(self):
        """Tables/objects are owned in every mode — only containers flip."""
        transitions = detect_ownership_transitions(_contract(SHARED), OWNED_STATE)
        assert not any("catalog_table" in t.address for t in transitions)

    def test_it_fails_closed(self):
        with pytest.raises(PackagingTransitionError) as excinfo:
            guard_ownership_transitions(_contract(SHARED), OWNED_STATE)
        assert excinfo.value.kind == "ownership-transition"

    def test_the_adoption_flag_does_not_wave_it_through(self):
        """--adopt-shared-container is for the OTHER direction only."""
        with pytest.raises(PackagingTransitionError) as excinfo:
            guard_ownership_transitions(_contract(SHARED), OWNED_STATE, adopt_shared_container=True)
        assert excinfo.value.kind == "ownership-transition"

    def test_the_remediation_is_copy_pasteable(self):
        with pytest.raises(PackagingTransitionError) as excinfo:
            guard_ownership_transitions(
                _contract(SHARED), OWNED_STATE, workdir="/w/.fluid/iac/aws/telemetry_sdp"
            )
        assert excinfo.value.remediation == (
            "tofu -chdir=/w/.fluid/iac/aws/telemetry_sdp state rm "
            "aws_s3_bucket.telemetry_sdp_acme_iot_lake",
            "tofu -chdir=/w/.fluid/iac/aws/telemetry_sdp state rm "
            "aws_glue_catalog_database.telemetry_sdp_iot_pool",
        )

    def test_the_message_carries_the_commands_and_says_data_is_safe(self):
        with pytest.raises(PackagingTransitionError) as excinfo:
            guard_ownership_transitions(_contract(SHARED), OWNED_STATE, workdir="/w")
        message = str(excinfo.value)
        assert "tofu -chdir=/w state rm aws_s3_bucket.telemetry_sdp_acme_iot_lake" in message
        assert "ZERO bytes" in message
        assert "DESTROY" in message

    def test_an_indexed_address_is_shell_quoted(self):
        commands = state_rm_commands(
            (OwnershipTransition('aws_s3_bucket.foo["prod"]', "bucket", "owned", "referenced"),),
            workdir="/w",
        )
        assert commands == ("tofu -chdir=/w state rm 'aws_s3_bucket.foo[\"prod\"]'",)

    def test_workdir_is_optional(self):
        commands = state_rm_commands(
            (OwnershipTransition("aws_s3_bucket.foo", "bucket", "owned", "referenced"),)
        )
        assert commands == ("tofu state rm aws_s3_bucket.foo",)

    def test_a_per_exposure_override_is_enough_to_flag(self):
        """Conservative by design — any scope declaring shared blocks."""
        contract = _contract(ISOLATED)
        contract["exposes"][0]["binding"]["packaging"] = {"mode": "shared", "pool": "p"}
        transitions = detect_ownership_transitions(contract, OWNED_STATE)
        assert transitions, "a per-exposure shared override must still be caught"


class TestReferencedToOwned:
    """shared → isolated. The dangerous direction — needs the flag."""

    def test_the_flip_is_detected_as_an_adoption(self):
        transitions = detect_ownership_transitions(_contract(ISOLATED), REFERENCED_STATE)
        assert [t.address for t in transitions] == [
            "data.aws_s3_bucket.telemetry_sdp_acme_iot_lake"
        ]
        assert transitions[0].is_adoption

    def test_it_is_blocked_without_the_flag(self):
        with pytest.raises(PackagingTransitionError) as excinfo:
            guard_ownership_transitions(_contract(ISOLATED), REFERENCED_STATE)
        assert excinfo.value.kind == "shared-adoption-requires-flag"
        assert "--adopt-shared-container" in str(excinfo.value)

    def test_the_flag_allows_it_and_returns_the_adoptions(self):
        adoptions = guard_ownership_transitions(
            _contract(ISOLATED), REFERENCED_STATE, adopt_shared_container=True
        )
        assert [t.address for t in adoptions] == ["data.aws_s3_bucket.telemetry_sdp_acme_iot_lake"]

    def test_the_override_logs_a_warning(self, caplog):
        logger = logging.getLogger("test_transition_warning")
        with caplog.at_level(logging.WARNING, logger=logger.name):
            guard_ownership_transitions(
                _contract(ISOLATED),
                REFERENCED_STATE,
                adopt_shared_container=True,
                logger=logger,
            )
        assert any(
            record.levelno == logging.WARNING and "OWNERSHIP" in record.getMessage()
            for record in caplog.records
        ), "the adoption override must leave a WARNING-level paper trail"

    def test_the_blocked_error_carries_structured_event_fields(self):
        with pytest.raises(PackagingTransitionError) as excinfo:
            guard_ownership_transitions(_contract(ISOLATED), REFERENCED_STATE)
        fields = excinfo.value.event_fields()
        assert fields["kind"] == "shared-adoption-requires-flag"
        assert fields["containers"] == [
            {
                "address": "data.aws_s3_bucket.telemetry_sdp_acme_iot_lake",
                "container": "bucket",
                "from": "referenced",
                "to": "owned",
            }
        ]


class TestLegacyNeverTransitions:
    """The compatibility invariant — no packaging block, no guard."""

    @pytest.mark.parametrize("state", [OWNED_STATE, REFERENCED_STATE, []])
    def test_a_legacy_contract_is_a_no_op(self, state):
        assert detect_ownership_transitions(_contract(None), state) == ()
        assert guard_ownership_transitions(_contract(None), state) == ()

    def test_a_steady_state_isolated_contract_is_a_no_op(self):
        assert guard_ownership_transitions(_contract(ISOLATED), OWNED_STATE) == ()

    def test_a_steady_state_shared_contract_is_a_no_op(self):
        assert guard_ownership_transitions(_contract(SHARED), REFERENCED_STATE) == ()

    def test_a_fresh_workdir_with_no_state_is_a_no_op(self):
        assert guard_ownership_transitions(_contract(SHARED), []) == ()

    def test_a_malformed_packaging_block_does_not_raise_here(self):
        """The emit path reports it as a typed error naming the real culprit."""
        assert detect_ownership_transitions(_contract({"mode": "nonsense"}), OWNED_STATE) == ()

    def test_unrelated_resource_types_are_ignored(self):
        state = ["aws_iam_role.x", "random_pet.y", "aws_lakeformation_permissions.z"]
        assert detect_ownership_transitions(_contract(SHARED), state) == ()


class TestResourceTypeMapping:
    def test_every_mapped_type_names_a_real_container_kind(self):
        from fluid_build.iac.packaging import CONTAINER_KINDS

        assert set(CONTAINER_RESOURCE_TYPES.values()) <= set(CONTAINER_KINDS)

    @pytest.mark.parametrize(
        ("resource_type", "kind"),
        [
            ("aws_s3_bucket", "bucket"),
            ("aws_glue_catalog_database", "database"),
            ("google_storage_bucket", "bucket"),
            ("google_bigquery_dataset", "dataset"),
            ("snowflake_database", "database"),
            ("snowflake_schema", "schema"),
            ("snowflake_warehouse", "warehouse"),
        ],
    )
    def test_the_rfc_mapping_is_covered(self, resource_type, kind):
        assert CONTAINER_RESOURCE_TYPES[resource_type] == kind

    def test_gcp_shared_dataset_flip_is_caught(self):
        contract = {
            "fluidVersion": "0.7.6",
            "id": "orders-adp",
            "packaging": {"mode": "shared", "pool": "sales"},
            "exposes": [
                {
                    "exposeId": "orders",
                    "binding": {
                        "platform": "gcp",
                        "format": "bigquery_table",
                        "location": {"dataset": "sales_pool", "table": "orders"},
                    },
                }
            ],
        }
        transitions = detect_ownership_transitions(
            contract, ["google_bigquery_dataset.orders_adp_sales_pool"]
        )
        assert [t.container_kind for t in transitions] == ["dataset"]

    def test_snowflake_hybrid_flips_only_the_shared_tier(self):
        contract = {
            "fluidVersion": "0.7.6",
            "id": "orders-cdp",
            "packaging": {
                "mode": "shared",
                "pool": "sales-domain",
                "containers": {"schema": "isolated", "warehouse": "isolated"},
            },
            "exposes": [
                {
                    "exposeId": "orders",
                    "binding": {
                        "platform": "snowflake",
                        "format": "table",
                        "location": {"database": "SALES_POOL", "schema": "ORDERS_CDP"},
                    },
                }
            ],
        }
        state = [
            "snowflake_database.orders_cdp_sales_pool",
            "snowflake_schema.orders_cdp_orders_cdp",
        ]
        transitions = detect_ownership_transitions(contract, state)
        # The pooled database flips; the isolated schema stays owned.
        assert [t.container_kind for t in transitions] == ["database"]


SNOWFLAKE_POOLED = {
    "fluidVersion": "0.7.6",
    "id": "silver.misc.tenant_v1",
    "exposes": [
        {
            "exposeId": "tenant_table",
            "binding": {
                "platform": "snowflake",
                "format": "snowflake_table",
                "location": {
                    "database": "FLUID_MISC_POOL",
                    "schema": "MPOOL",
                    "table": "MISC_TENANT_TABLE",
                },
            },
        }
    ],
}


def _snowflake_contract(packaging: Dict[str, Any]) -> Dict[str, Any]:
    contract = {k: v for k, v in SNOWFLAKE_POOLED.items()}
    contract["packaging"] = packaging
    return contract


# What `tofu state list` really holds after a shared-mode Snowflake apply:
# the leaf table and NOTHING else. `SnowflakeIacPlugin.emit_data` returns {},
# so the REFERENCED pool leaves no `data.` address behind.
SF_SHARED_STATE = ["snowflake_table.silver_misc_tenant_v1_FLUID_MISC_POOL_MPOOL_MISC_TENANT_TABLE"]

# What `discover_imports` yields once the block flips to `isolated` — the exact
# addresses `_adopt_existing` would `tofu import`.
SF_ISOLATED_IMPORTS = [
    "snowflake_database.silver_misc_tenant_v1_FLUID_MISC_POOL",
    "snowflake_schema.silver_misc_tenant_v1_FLUID_MISC_POOL_MPOOL",
    "snowflake_table.silver_misc_tenant_v1_FLUID_MISC_POOL_MPOOL_MISC_TENANT_TABLE",
]


class TestSnowflakeAdoptionHasNoDataAddress:
    """A REFERENCED Snowflake container leaves no state footprint at all.

    Regression pin: with the state diff as the only signal, the guard was a
    silent no-op on every Snowflake contract. A shared -> isolated flip
    imported the platform's pool database and schema into the tenant's state
    and rewrote their attributes (verified live: the platform's database and
    schema COMMENTs were erased), with the flag never consulted — a control
    run with and without ``--adopt-shared-container`` produced byte-identical
    output.
    """

    def test_state_alone_cannot_see_the_flip(self):
        """Documents the blind spot the import-candidate signal exists to cover."""
        assert detect_ownership_transitions(_snowflake_contract(ISOLATED), SF_SHARED_STATE) == ()

    def test_the_import_candidates_reveal_the_adoption(self):
        transitions = detect_ownership_transitions(
            _snowflake_contract(ISOLATED),
            SF_SHARED_STATE,
            import_candidates=SF_ISOLATED_IMPORTS,
        )
        assert [(t.container_kind, t.from_ownership, t.to_ownership) for t in transitions] == [
            ("database", "referenced", "owned"),
            ("schema", "referenced", "owned"),
        ]
        assert all(t.is_adoption for t in transitions)

    def test_it_is_blocked_without_the_flag(self):
        with pytest.raises(PackagingTransitionError) as excinfo:
            guard_ownership_transitions(
                _snowflake_contract(ISOLATED),
                SF_SHARED_STATE,
                import_candidates=SF_ISOLATED_IMPORTS,
            )
        assert excinfo.value.kind == "shared-adoption-requires-flag"

    def test_the_flag_waves_it_through(self):
        adoptions = guard_ownership_transitions(
            _snowflake_contract(ISOLATED),
            SF_SHARED_STATE,
            import_candidates=SF_ISOLATED_IMPORTS,
            adopt_shared_container=True,
        )
        assert len(adoptions) == 2

    def test_a_steady_state_shared_contract_is_still_a_no_op(self):
        """`discover_imports` skips REFERENCED containers, so nothing to adopt."""
        assert (
            guard_ownership_transitions(
                _snowflake_contract(SHARED),
                SF_SHARED_STATE,
                import_candidates=SF_SHARED_STATE,
            )
            == ()
        )

    def test_a_steady_state_isolated_contract_is_a_no_op(self):
        """Second apply of an isolated contract: the containers are already managed."""
        assert (
            guard_ownership_transitions(
                _snowflake_contract(ISOLATED),
                SF_ISOLATED_IMPORTS,
                import_candidates=SF_ISOLATED_IMPORTS,
            )
            == ()
        )

    def test_an_empty_state_never_flags_an_adoption(self):
        """Greenfield first apply — no prior state, nothing was ever referenced."""
        assert (
            detect_ownership_transitions(
                _snowflake_contract(ISOLATED), [], import_candidates=SF_ISOLATED_IMPORTS
            )
            == ()
        )

    def test_leaf_import_candidates_are_never_adoptions(self):
        """Only container types transition; a table is owned in every mode."""
        assert (
            detect_ownership_transitions(
                _snowflake_contract(ISOLATED),
                SF_SHARED_STATE,
                import_candidates=["snowflake_table.some_other_table"],
            )
            == ()
        )

    def test_a_legacy_contract_is_untouched_by_the_new_signal(self):
        contract = {k: v for k, v in SNOWFLAKE_POOLED.items()}
        assert (
            detect_ownership_transitions(
                contract, SF_SHARED_STATE, import_candidates=SF_ISOLATED_IMPORTS
            )
            == ()
        )

    def test_the_engine_feeds_the_plugin_import_candidates_to_the_guard(self, monkeypatch):
        """The wiring pin: without the plugin the guard gets no candidates."""
        from fluid_build.cli import _apply_opentofu_engine as engine
        from fluid_build.cli._common import CLIError
        from fluid_build.iac.base import ImportBlock

        monkeypatch.setattr(engine.runner, "tofu_state_list", lambda *a, **k: SF_SHARED_STATE)
        plugin = type(
            "P",
            (),
            {
                "discover_imports": staticmethod(
                    lambda contract, actions: [ImportBlock(to=a, id=a) for a in SF_ISOLATED_IMPORTS]
                )
            },
        )()
        args = type("Args", (), {"adopt_shared_container": False})()
        with pytest.raises(CLIError) as excinfo:
            engine._guard_packaging_transitions(
                _snowflake_contract(ISOLATED),
                "/w",
                {},
                args,
                logging.getLogger("t"),
                plugin=plugin,
                actions=(),
            )
        assert excinfo.value.context["kind"] == "shared-adoption-requires-flag"


class TestEngineWiring:
    """The guard must genuinely run in the apply engine — not just exist."""

    def test_the_engine_calls_the_guard_before_plan_and_before_adoption(self):
        import inspect

        from fluid_build.cli import _apply_opentofu_engine as engine

        source = inspect.getsource(engine.apply_via_opentofu)
        guard_at = source.index("_guard_packaging_transitions(")
        adopt_at = source.index("_adopt_existing(")
        plan_at = source.index("runner.tofu_plan(")
        assert guard_at < adopt_at < plan_at

    def test_the_adapter_translates_to_a_cli_error_with_remediation(self, monkeypatch):
        from fluid_build.cli import _apply_opentofu_engine as engine
        from fluid_build.cli._common import CLIError

        monkeypatch.setattr(engine.runner, "tofu_state_list", lambda *a, **k: OWNED_STATE)
        args = type("Args", (), {"adopt_shared_container": False})()
        with pytest.raises(CLIError) as excinfo:
            engine._guard_packaging_transitions(
                _contract(SHARED), "/w", {}, args, logging.getLogger("t")
            )
        context = excinfo.value.context
        assert context["kind"] == "ownership-transition"
        assert any("state rm" in line for line in context["remediation"])

    def test_the_adapter_is_a_no_op_on_an_empty_state(self, monkeypatch):
        from fluid_build.cli import _apply_opentofu_engine as engine

        monkeypatch.setattr(engine.runner, "tofu_state_list", lambda *a, **k: [])
        args = type("Args", (), {"adopt_shared_container": False})()
        assert (
            engine._guard_packaging_transitions(
                _contract(SHARED), "/w", {}, args, logging.getLogger("t")
            )
            is None
        )

    def test_the_flag_is_registered_on_the_apply_parser(self):
        import argparse

        from fluid_build.cli import apply as apply_mod

        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers(dest="command")
        apply_mod.register(subparsers)
        args = parser.parse_args(["apply", "c.fluid.yaml", "--adopt-shared-container"])
        assert args.adopt_shared_container is True

    def test_the_flag_defaults_to_false(self):
        import argparse

        from fluid_build.cli import apply as apply_mod

        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers(dest="command")
        apply_mod.register(subparsers)
        args = parser.parse_args(["apply", "c.fluid.yaml"])
        assert args.adopt_shared_container is False


class TestOverrideAuditEventsAreWarnings:
    """Safety-gate overrides must be audit-visible at WARNING, not INFO.

    Three sources of truth agreed on WARNING while the code emitted INFO:
    the ``--adopt-shared-container`` help text ("Logs a WARNING-level audit
    event"), the emitting site's own comment, and RFC-packaging-modes.md
    ("logs a WARNING-level ``packaging_adoption_override`` audit event —
    same discipline as ``--allow-data-loss``"). The precedent it cited,
    ``opentofu_destructive_gate_override``, had the same defect.

    This matters operationally: these events mean *a human deliberately
    overrode a safety gate*, which is what an audit pipeline filters for at
    WARNING and above. At INFO they are invisible to that filter.
    """

    OVERRIDE_EVENTS = ("opentofu_destructive_gate_override", "packaging_adoption_override")

    def _source(self):
        from pathlib import Path

        import fluid_build.cli._apply_opentofu_engine as engine

        return Path(engine.__file__).read_text(encoding="utf-8")

    @pytest.mark.parametrize("event", OVERRIDE_EVENTS)
    def test_override_event_is_emitted_via_warn(self, event):
        source = self._source()
        assert event in source, f"{event} no longer emitted — update this pin"
        # The emitting call is the helper invocation immediately preceding
        # the event name; assert it is `warn(`, never `info(`.
        head = source.split(event)[0]
        emitter = head.rstrip().rsplit("(", 1)[0].rsplit("\n", 1)[-1].strip()
        assert emitter.endswith("warn"), (
            f"{event} is emitted via {emitter!r}; safety-gate overrides must "
            "use warn() so audit pipelines filtering at WARNING see them"
        )

    def test_warn_helper_is_imported(self):
        assert "from ._logging import info, warn" in self._source()


class TestOneContainerIsReportedOnce:
    """AWS/GCP surface the same container through BOTH signals.

    A shared GCP dataset leaves ``data.google_bigquery_dataset.X`` in state
    (signal 1) AND appears as ``google_bigquery_dataset.X`` in the import
    candidates once the block flips to isolated (signal 2). Reporting both
    would make the blocked message read like two ownership changes.
    """

    GCP_ISOLATED = {
        "fluidVersion": "0.7.6",
        "id": "orders-adp",
        "packaging": {"mode": "isolated", "pool": "sales"},
        "exposes": [
            {
                "exposeId": "orders",
                "binding": {
                    "platform": "gcp",
                    "format": "bigquery_table",
                    "location": {"dataset": "sales_pool", "table": "orders"},
                },
            }
        ],
    }

    def test_the_same_container_is_not_double_counted(self):
        transitions = detect_ownership_transitions(
            self.GCP_ISOLATED,
            [
                "data.google_bigquery_dataset.orders_adp_sales_pool",
                "google_bigquery_table.orders_adp_orders",
            ],
            import_candidates=[
                "google_bigquery_dataset.orders_adp_sales_pool",
                "google_bigquery_table.orders_adp_orders",
            ],
        )
        assert len(transitions) == 1
        assert transitions[0].container_kind == "dataset"
        # The state address wins — it is what `tofu state rm` would name.
        assert transitions[0].address.startswith("data.")

    def test_two_distinct_containers_are_both_reported(self):
        """The dedupe must key on identity, not collapse everything to one."""
        transitions = detect_ownership_transitions(
            _snowflake_contract(ISOLATED),
            SF_SHARED_STATE,
            import_candidates=SF_ISOLATED_IMPORTS,
        )
        assert {t.container_kind for t in transitions} == {"database", "schema"}


class TestBrownfieldGrowthIsNotAnAdoption:
    """Signal 2 must detect a *transition*, not ordinary brownfield adoption.

    The first cut fired on any pre-existing container in ``discover_imports``.
    That broke the documented ``_adopt_existing`` path for contracts that were
    never shared: an ``mode: isolated`` contract, already applied once, grown
    with a second exposure pointing at a schema that already existed inside its
    OWN database was blocked — with a message asserting a shared-pool history
    the contract never had, and remediation ("declare the container `shared`")
    that would have made the product's own database referenced.

    Verified live on Snowflake before the narrowing: base build printed
    "brownfield: adopted 1 pre-existing resource(s) into state" / "+1 ~2 -0" and
    exited 0; the un-narrowed guard blocked the same apply with exit 1.
    """

    ISO_TWO_EXPOSES = {
        "fluidVersion": "0.7.6",
        "id": "silver.misc.iso_v1",
        "packaging": {"mode": "isolated"},
        "exposes": [
            {
                "exposeId": "first",
                "binding": {
                    "platform": "snowflake",
                    "format": "snowflake_table",
                    "location": {
                        "database": "FLUID_MISCP_ISO",
                        "schema": "ISOSCH",
                        "table": "T1",
                    },
                },
            },
            {
                "exposeId": "second",
                "binding": {
                    "platform": "snowflake",
                    "format": "snowflake_table",
                    "location": {
                        "database": "FLUID_MISCP_ISO",
                        "schema": "PREEXIST",
                        "table": "T2",
                    },
                },
            },
        ],
    }

    # State after the FIRST apply: the contract owns its database and its first
    # schema outright. Nothing here was ever referenced.
    ISO_STATE = [
        "snowflake_database.iso_v1_FLUID_MISCP_ISO",
        "snowflake_schema.iso_v1_FLUID_MISCP_ISO_ISOSCH",
        "snowflake_table.iso_v1_FLUID_MISCP_ISO_ISOSCH_T1",
    ]

    # `discover_imports` after the second exposure is added — PREEXIST already
    # exists in Snowflake, so it is an import candidate.
    ISO_IMPORTS = ISO_STATE + [
        "snowflake_schema.iso_v1_FLUID_MISCP_ISO_PREEXIST",
        "snowflake_table.iso_v1_FLUID_MISCP_ISO_PREEXIST_T2",
    ]

    def test_growing_into_a_pre_existing_schema_is_not_flagged(self):
        assert (
            detect_ownership_transitions(
                self.ISO_TWO_EXPOSES, self.ISO_STATE, import_candidates=self.ISO_IMPORTS
            )
            == ()
        )

    def test_the_apply_is_not_blocked(self):
        assert (
            guard_ownership_transitions(
                self.ISO_TWO_EXPOSES, self.ISO_STATE, import_candidates=self.ISO_IMPORTS
            )
            == ()
        )

    def test_the_shared_pool_flip_is_still_caught(self):
        """The narrowing must not touch the defect the signal exists for.

        Same contract shape, but the prior apply was `shared`: the state holds
        the leaf and NO container of either type. That is the shared-pool
        footprint, and it still blocks.
        """
        with pytest.raises(PackagingTransitionError) as excinfo:
            guard_ownership_transitions(
                self.ISO_TWO_EXPOSES,
                ["snowflake_table.iso_v1_FLUID_MISCP_ISO_ISOSCH_T1"],
                import_candidates=self.ISO_IMPORTS,
            )
        assert excinfo.value.kind == "shared-adoption-requires-flag"
        assert {t.container_kind for t in excinfo.value.transitions} == {"database", "schema"}

    def test_the_narrowing_is_per_resource_type_not_blanket(self):
        """A state owning the database but no schema still catches the schema.

        `containers: {database: isolated, schema: shared}` flipped to fully
        isolated: the database was owned all along, the schemas were pool
        containers. Both schemas are adoptions and both are still reported;
        the database — already managed — is not.
        """
        transitions = detect_ownership_transitions(
            self.ISO_TWO_EXPOSES,
            [
                "snowflake_database.iso_v1_FLUID_MISCP_ISO",
                "snowflake_table.iso_v1_FLUID_MISCP_ISO_ISOSCH_T1",
            ],
            import_candidates=self.ISO_IMPORTS,
        )
        assert {t.container_kind for t in transitions} == {"schema"}
        assert {t.address for t in transitions} == {
            "snowflake_schema.iso_v1_FLUID_MISCP_ISO_ISOSCH",
            "snowflake_schema.iso_v1_FLUID_MISCP_ISO_PREEXIST",
        }
        assert all(t.is_adoption for t in transitions)

    def test_the_state_signal_is_untouched_by_the_narrowing(self):
        """owned -> referenced still fires on a state that owns the container."""
        contract = {k: v for k, v in self.ISO_TWO_EXPOSES.items()}
        contract["packaging"] = {"mode": "shared", "pool": "FLUID_MISCP_POOL"}
        transitions = detect_ownership_transitions(
            contract, self.ISO_STATE, import_candidates=self.ISO_IMPORTS
        )
        assert {(t.container_kind, t.from_ownership, t.to_ownership) for t in transitions} == {
            ("database", "owned", "referenced"),
            ("schema", "owned", "referenced"),
        }
