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
    """The warnings besides the generic ones every platform gets, and the note that a
    user found by name or e-mail is not spoof-proof (``test_the_spoof_note``)."""
    return [w for w in warnings if w not in (_NOTHING, NO_GRANTS) and "spoof-proof" not in w]


class TestTheBinding:
    def test_a_read_grant_is_select_on_the_table(self):
        bindings, warnings = compile_policy(
            _contract({"principal": "serviceAccount:trino", "permissions": ["read"]})
        )
        assert _own(warnings) == []
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

    def test_an_unevaluable_scope_compiles_no_lakekeeper_grant_at_all(self):
        """It could cover any table: granting the others would revoke its principal."""
        exposes = [_expose("orders"), _expose("refunds")]
        grants = [
            {"principal": "group:a", "permissions": ["read"], "resources": ["$.exposes[0]"]},
            {"principal": "group:b", "permissions": ["read"], "resources": ["$.exposes[0]"]},
            {"principal": "group:c", "permissions": ["read"]},
        ]
        bindings, warnings = compile_policy(_contract(*grants, exposes=exposes))
        assert _lakekeeper(bindings) == []
        named = [w for w in warnings if "$.exposes[0]" in w]
        assert len(named) == 2  # one per grant, not one per resources text
        assert "group:a" in named[0] and "group:b" in named[1]
        assert all("leaves every Lakekeeper table's grants as they are" in w for w in named)

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
        assert NO_GRANTS in warnings and _own(warnings) == []

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
        assert any("does not map it" in w for w in warnings)

    def test_one_principal_that_cannot_map_keeps_the_whole_table_unmanaged(self):
        """With the others alone, stage 8 would revoke the unmapped principal's grants."""
        expose = _expose(principals={"group:analysts": "role:analysts", "group:eng": "domain:x"})
        grants = [
            {"principal": "group:analysts", "permissions": ["read"]},
            {"principal": "group:eng", "permissions": ["read"]},
        ]
        bindings, warnings = compile_policy(_contract(*grants, exposes=[expose]))
        assert _lakekeeper(bindings) == []
        (closed,) = [w for w in warnings if "no grant was compiled, because" in w]
        assert "['group:eng']" in closed and "leaves the table's grants as they are" in closed

    def test_another_table_still_compiles_when_one_cannot(self):
        good = _expose("orders")
        bad = _expose("refunds", principals={"group:analysts": "domain:x"})
        bindings, _ = compile_policy(
            _contract({"principal": "group:analysts", "permissions": ["read"]}, exposes=[good, bad])
        )
        assert [b["table"] for b in bindings] == ["orders"]

    @pytest.mark.parametrize("principal", ["user:Alice Smith", "role:Data Analysts"])
    def test_a_display_name_with_spaces_is_a_lakekeeper_name(self, principal):
        bindings, warnings = compile_policy(
            _contract({"principal": principal, "permissions": ["read"]})
        )
        assert _own(warnings) == []
        assert bindings[0]["identities"] == [principal]

    def test_a_group_named_like_a_role_stays_a_group(self):
        """``group:role:x`` is the group ``role:x``; only ``role:x`` written so is a role."""
        bindings, _ = compile_policy(
            _contract({"principal": "group:role:x", "permissions": ["read"]})
        )
        assert bindings[0]["principal"] == "group:role:x"
        assert bindings[0]["identities"] == ["group:role:x"]

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
        assert _own(warnings) == []
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


class TestOnACloudPlatform:
    """A Lakekeeper table on aws or snowflake: the cloud grant the compiler always
    emitted stays (the bucket is still the table's storage), and Lakekeeper gets its
    own grant instead of the old "enforce it in the catalog" warning."""

    _LOC = {"bucket": "bkt", "database": "mydb", "table": "tbl", "region": "eu-west-1"}

    @staticmethod
    def _compile(platform: str, location: Dict[str, Any]):
        expose = {"binding": {"platform": platform, "format": "iceberg", "location": location}}
        grant = {"principal": "user@example.com", "permissions": ["read"]}
        bindings, warnings = compile_policy(
            {"accessPolicy": {"grants": [grant]}, "exposes": [expose]}
        )
        return sorted(b["resource_type"] for b in bindings), [
            w for w in warnings if "cataloged in" in w
        ]

    @pytest.mark.parametrize("catalog", ["lakekeeper", "Lakekeeper"])
    def test_without_its_uri_the_warning_says_what_is_missing(self, catalog):
        types, (warning,) = self._compile("aws", dict(self._LOC, catalog=catalog))
        assert types == ["s3.bucket"]
        assert "user:user@example.com" in warning
        assert "location.uri, location.warehouse" in warning
        assert "not AWS Glue" not in warning

    def test_on_aws_the_bucket_and_a_lakekeeper_grant(self):
        loc = dict(self._LOC, catalog="lakekeeper", uri="https://lk/catalog", warehouse="wh")
        assert self._compile("aws", loc) == (["lakekeeper.table", "s3.bucket"], [])

    def test_on_snowflake_a_snowflake_grant_and_a_lakekeeper_grant(self):
        loc = {
            "database": "DB",
            "schema": "SCH",
            "table": "T",
            "catalog": "lakekeeper",
            "uri": "https://lk/catalog",
            "warehouse": "wh",
        }
        assert self._compile("snowflake", loc) == (["lakekeeper.table", "snowflake.table"], [])


def test_two_exposes_on_one_table_fail_closed_together():
    """One expose that cannot compile keeps the table unmanaged, whatever another says."""
    good = _expose("orders")
    bad = _expose("orders", principals={"group:analysts": "domain:x"})
    bad["exposeId"] = "orders_again"
    bindings, warnings = compile_policy(
        _contract({"principal": "group:analysts", "permissions": ["read"]}, exposes=[good, bad])
    )
    assert _lakekeeper(bindings) == []
    assert any("another expose naming it could not be compiled" in w for w in warnings)


@pytest.mark.parametrize(
    "principal, noted",
    [
        ("user:jane@corp.com", True),
        ("serviceAccount:service-account-trino", True),
        ("user:oidc~4f2c", False),
        ("role:analysts", False),
    ],
)
def test_the_spoof_note_names_users_found_by_name_or_e_mail(principal, noted):
    _, warnings = compile_policy(_contract({"principal": principal, "permissions": ["read"]}))
    notes = [w for w in warnings if "spoof-proof" in w]
    assert bool(notes) is noted
    if noted:
        assert principal in notes[0] and "user:oidc~<subject>" in notes[0]
