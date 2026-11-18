# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid policy compile`` grants a Lakekeeper table in Lakekeeper.

An Iceberg expose cataloged in Lakekeeper used to compile to a warning ("enforce
it in the catalog's own access control"). It now compiles to one
``provider: lakekeeper`` binding per table and logical principal, from every
surface that declares access, for ``fluid policy apply`` to apply.
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List

import pytest

from fluid_build.policy.compiler import NO_GRANTS, compile_policy
from fluid_build.policy.lakekeeper import plan_changes, relations_for

pytestmark = pytest.mark.unit

_LOCATION = {
    "catalog": "lakekeeper",
    "uri": "https://lakekeeper.lab.test/catalog",
    "warehouse": "sales-wh",
    "database": "curated",
    "table": "events_daily",
}


def _expose(expose_id: str = "events_daily", **binding: Any) -> Dict[str, Any]:
    location = dict(_LOCATION, table=expose_id, **binding.pop("location", {}))
    return {
        "exposeId": expose_id,
        "binding": {"platform": "local", "format": "iceberg", "location": location, **binding},
    }


def _contract(*grants: Dict[str, Any], exposes: List[Dict[str, Any]] = ()) -> Dict[str, Any]:
    doc: Dict[str, Any] = {"exposes": list(exposes) or [_expose()]}
    if grants:
        doc["accessPolicy"] = {"grants": list(grants)}
    return doc


def _lakekeeper(bindings: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [b for b in bindings if b["provider"] == "lakekeeper"]


#: The compiler's generic warning when nothing at all compiled (any platform).
_NOTHING = "No IAM bindings generated from contract"


def _own(warnings: List[str]) -> List[str]:
    """The warnings besides the generic ones every platform gets."""
    return [w for w in warnings if w not in (_NOTHING, NO_GRANTS)]


class TestTheBinding:
    def test_a_read_grant_is_select_on_the_table(self):
        bindings, warnings = compile_policy(
            _contract({"principal": "serviceAccount:trino", "permissions": ["read"]})
        )
        assert warnings == []
        assert bindings == [
            {
                "provider": "lakekeeper",
                "resource_type": "lakekeeper.table",
                "resource_id": "sales-wh/curated.events_daily",
                "exposeId": "events_daily",
                "catalog_uri": "https://lakekeeper.lab.test/catalog",
                "warehouse": "sales-wh",
                "namespace": ["curated"],
                "table": "events_daily",
                "principal": "serviceAccount:trino",
                "identities": ["serviceAccount:trino"],
                "relations": ["select"],
            }
        ]

    def test_every_binding_carries_what_validate_artifacts_requires(self):
        """Stage 4 refuses a binding without ``provider`` or ``principal``."""
        bindings, _ = compile_policy(
            _contract({"principal": "group:analysts", "permissions": ["read", "write"]})
        )
        assert bindings and all({"provider", "principal"} <= set(b) for b in bindings)

    @pytest.mark.parametrize(
        "permissions, relations",
        [
            (["read"], ["select"]),
            (["select", "query"], ["select"]),
            (["write"], ["modify"]),
            (["insert", "update", "delete"], ["modify"]),
            (["read", "write"], ["select", "modify"]),
            (["manage"], ["modify"]),
            (["admin"], ["modify"]),
        ],
    )
    def test_permission_verbs_map_to_data_relations(self, permissions, relations):
        bindings, _ = compile_policy(
            _contract({"principal": "group:analysts", "permissions": permissions})
        )
        assert bindings[0]["relations"] == relations

    def test_create_has_no_table_relation_and_is_reported_not_widened(self):
        bindings, warnings = compile_policy(
            _contract({"principal": "group:analysts", "permissions": ["read", "create"]})
        )
        assert bindings[0]["relations"] == ["select"]
        (warning,) = warnings
        assert "['create']" in warning and "namespace" in warning

    def test_create_alone_compiles_no_binding(self):
        bindings, warnings = compile_policy(
            _contract({"principal": "group:analysts", "permissions": ["create"]})
        )
        assert _lakekeeper(bindings) == []
        assert any("['create']" in w for w in warnings)

    def test_a_dotted_database_is_a_multi_level_namespace(self):
        expose = _expose(location={"database": "sales.curated"})
        bindings, _ = compile_policy(
            _contract({"principal": "group:a", "permissions": ["read"]}, exposes=[expose])
        )
        assert bindings[0]["namespace"] == ["sales", "curated"]
        assert bindings[0]["resource_id"] == "sales-wh/sales.curated.events_daily"

    def test_principals_are_sorted_and_merged_per_table(self):
        grants = [
            {"principal": "user:zed@corp.com", "permissions": ["read"]},
            {"principal": "group:analysts", "permissions": ["read"]},
            {"principal": "group:analysts", "permissions": ["write"]},
        ]
        bindings, _ = compile_policy(_contract(*grants))
        assert [(b["principal"], b["relations"]) for b in bindings] == [
            ("group:analysts", ["select", "modify"]),
            ("user:zed@corp.com", ["select"]),
        ]

    def test_the_legacy_metadata_policies_surface_is_read_too(self):
        doc = _contract()
        doc["metadata"] = {
            "policies": {"readers": {"principals": ["a@corp.com"], "permissions": ["read"]}}
        }
        bindings, _ = compile_policy(doc)
        assert [(b["principal"], b["relations"]) for b in bindings] == [
            ("user:a@corp.com", ["select"])
        ]

    def test_compiling_twice_gives_the_same_bindings(self):
        doc = _contract(
            {"principal": "group:b", "permissions": ["read"]},
            {"principal": "group:a", "permissions": ["write"]},
        )
        assert compile_policy(copy.deepcopy(doc)) == compile_policy(copy.deepcopy(doc))


class TestScope:
    def test_a_grant_scoped_to_one_expose_reaches_only_that_table(self):
        exposes = [_expose("orders"), _expose("refunds")]
        grant = {
            "principal": "group:analysts",
            "permissions": ["read"],
            "resources": ["$.exposes[?(@.exposeId=='refunds')]"],
        }
        bindings, _ = compile_policy(_contract(grant, exposes=exposes))
        assert [b["table"] for b in bindings] == ["refunds"]

    def test_an_unevaluable_scope_is_reported_once_and_granted_nowhere(self):
        exposes = [_expose("orders"), _expose("refunds")]
        grant = {"principal": "group:a", "permissions": ["read"], "resources": ["$.exposes[0]"]}
        bindings, warnings = compile_policy(_contract(grant, exposes=exposes))
        assert _lakekeeper(bindings) == []
        assert len([w for w in warnings if "$.exposes[0]" in w]) == 1

    def test_a_table_no_one_is_granted_on_gets_no_binding(self):
        """Stage 8 leaves a table alone that the contract declares no access for."""
        exposes = [_expose("orders"), _expose("refunds")]
        grant = {
            "principal": "group:a",
            "permissions": ["read"],
            "resources": ["$.exposes[?(@.exposeId=='orders')]"],
        }
        bindings, _ = compile_policy(_contract(grant, exposes=exposes))
        assert {b["table"] for b in bindings} == {"orders"}

    def test_a_polaris_table_is_still_left_to_its_catalog(self):
        expose = _expose(location={"catalog": "polaris"})
        bindings, warnings = compile_policy(
            _contract({"principal": "group:a", "permissions": ["read"]}, exposes=[expose])
        )
        assert _lakekeeper(bindings) == []
        assert any("cataloged in 'polaris'" in w for w in warnings)

    def test_a_parquet_expose_is_not_a_lakekeeper_table(self):
        expose = _expose()
        expose["binding"]["format"] = "parquet"
        bindings, _ = compile_policy(
            _contract({"principal": "group:a", "permissions": ["read"]}, exposes=[expose])
        )
        assert _lakekeeper(bindings) == []


class TestExposeLevelAuthz:
    def test_readers_and_writers_are_granted_without_an_access_policy(self):
        expose = _expose()
        expose["policy"] = {
            "authz": {"readers": ["group:analysts"], "writers": ["serviceAccount:spark"]}
        }
        bindings, warnings = compile_policy({"exposes": [expose]})
        assert [(b["principal"], b["relations"]) for b in bindings] == [
            ("group:analysts", ["select"]),
            ("serviceAccount:spark", ["modify"]),
        ]
        assert warnings == [NO_GRANTS]

    def test_a_reader_also_granted_by_the_access_policy_is_one_binding(self):
        expose = _expose()
        expose["policy"] = {"authz": {"readers": ["group:analysts"]}}
        bindings, _ = compile_policy(
            _contract({"principal": "group:analysts", "permissions": ["write"]}, exposes=[expose])
        )
        assert [(b["principal"], b["relations"]) for b in bindings] == [
            ("group:analysts", ["select", "modify"])
        ]

    def test_column_and_row_rules_are_reported_as_not_enforced_by_lakekeeper(self):
        expose = _expose()
        expose["policy"] = {
            "authz": {"readers": ["group:a"], "columnRestrictions": [{"columns": ["ssn"]}]},
            "privacy": {"masking": [{"column": "email"}], "rowLevelPolicy": {"expression": "x"}},
        }
        _, warnings = compile_policy({"exposes": [expose]})
        (warning,) = _own(warnings)
        for name in ("columnRestrictions", "masking", "rowLevelPolicy", "query engine"):
            assert name in warning


class TestPrincipals:
    def test_binding_principals_maps_a_logical_principal_to_a_lakekeeper_role(self):
        expose = _expose(principals={"group:analysts@corp.com": "role:analysts"})
        bindings, _ = compile_policy(
            _contract(
                {"principal": "group:analysts@corp.com", "permissions": ["read"]}, exposes=[expose]
            )
        )
        assert bindings[0]["principal"] == "group:analysts@corp.com"
        assert bindings[0]["identities"] == ["role:analysts"]

    def test_a_principal_mapped_to_nothing_is_granted_nothing(self):
        expose = _expose(principals={"group:analysts": []})
        bindings, warnings = compile_policy(
            _contract({"principal": "group:analysts", "permissions": ["read"]}, exposes=[expose])
        )
        assert _lakekeeper(bindings) == []
        assert _own(warnings) == []

    def test_an_unmapped_principal_is_refused_once_the_binding_maps_principals(self):
        expose = _expose(principals={"group:other": "role:other"})
        bindings, warnings = compile_policy(
            _contract({"principal": "group:analysts", "permissions": ["read"]}, exposes=[expose])
        )
        assert _lakekeeper(bindings) == []
        assert any("does not map it" in w and "not granted" in w for w in warnings)

    @pytest.mark.parametrize(
        "principal",
        [
            "user:oidc~4f2c",
            "user:alice@corp.com",
            "serviceAccount:service-account-trino",
            "role:analysts",
        ],
    )
    def test_lakekeeper_shaped_principals_are_used_as_written(self, principal):
        bindings, warnings = compile_policy(
            _contract({"principal": principal, "permissions": ["read"]})
        )
        assert warnings == []
        assert bindings[0]["identities"] == [principal]

    @pytest.mark.parametrize(
        "principal, kind",
        [("domain:corp.com", "principal-unmapped"), ("user:x@corp.example", "placeholder")],
    )
    def test_a_principal_lakekeeper_cannot_have_is_reported(self, principal, kind):
        bindings, warnings = compile_policy(
            _contract({"principal": principal, "permissions": ["read"]})
        )
        assert _lakekeeper(bindings) == []
        assert any(principal in w for w in warnings)

    def test_a_mapped_identity_that_is_not_lakekeeper_shaped_is_reported(self):
        expose = _expose(principals={"group:a": "arn:aws:iam::123456789012:role/a"})
        bindings, warnings = compile_policy(
            _contract({"principal": "group:a", "permissions": ["read"]}, exposes=[expose])
        )
        assert _lakekeeper(bindings) == []
        assert any("not a Lakekeeper principal" in w for w in warnings)


class TestIncompleteLocation:
    @pytest.mark.parametrize("key", ["uri", "warehouse", "database", "table"])
    def test_a_missing_location_key_is_named_and_nothing_is_compiled(self, key):
        expose = _expose()
        del expose["binding"]["location"][key]
        bindings, warnings = compile_policy(
            _contract({"principal": "group:a", "permissions": ["read"]}, exposes=[expose])
        )
        assert _lakekeeper(bindings) == []
        (warning,) = _own(warnings)
        assert f"location.{key}" in warning and "group:a" in warning


class TestPlanChanges:
    def test_grants_what_is_missing_and_revokes_what_is_not_declared(self):
        desired = [("select", "user", "oidc~a"), ("modify", "role", "r1")]
        current = [("select", "user", "oidc~a"), ("select", "user", "oidc~b")]
        writes, deletes = plan_changes(desired, current)
        assert writes == [("modify", "role", "r1")]
        assert deletes == [("select", "user", "oidc~b")]

    @pytest.mark.parametrize(
        "relation", ["ownership", "pass_grants", "manage_grants", "manage_tags"]
    )
    def test_ownership_and_grant_management_are_never_revoked(self, relation):
        writes, deletes = plan_changes([], [(relation, "user", "oidc~owner")])
        assert (writes, deletes) == ([], [])

    def test_a_held_plan_is_empty(self):
        held = [("describe", "user", "u"), ("select", "role", "r")]
        assert plan_changes(held, held) == ([], [])

    def test_relations_for_orders_and_reports(self):
        assert relations_for(["write", "read", "create"]) == (("select", "modify"), ("create",))
