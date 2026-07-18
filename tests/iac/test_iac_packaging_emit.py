# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Packaging-modes PR2 — provider emitters honour isolated / shared.

RFC-packaging-modes.md files 3-5 + 7. PR1 shipped the resolver
(``iac/packaging.py``) and the LEGACY byte-parity pin
(``test_iac_packaging_default_pin.py``, still the release gate and
deliberately untouched). This file pins what the *emitters* do with the
resolution:

* ``TestAwsPackagingEmit`` / ``TestGcpPackagingEmit`` /
  ``TestSnowflakePackagingEmit`` — the owned-vs-referenced emit shapes,
  including the invariant that a shared pool container carries no
  ``force_destroy`` and is never an owned resource.
* ``TestNoDanglingReferences`` — the correctness property that makes the
  Snowflake case load-bearing: every ``${…}`` cross-reference an emitter
  produces must resolve to a block the same module declares. Dropping a
  container resource without rewriting its consumers is exactly the
  "Reference to undeclared resource" failure the RFC calls out, and it is
  checked generically across all three providers rather than per-field.
* ``TestImportAdoptionSafety`` — the load-bearing gate: a shared pool must
  never appear in ``discover_imports``, because ``_adopt_existing`` would
  ``tofu import`` it and re-own the platform's pool.
* ``TestPackagingGatedStateKey`` — the per-contract backend key, gated on
  the packaging block so LEGACY contracts keep today's shared key.
* ``TestIsolatedIsTodaysShape`` — explicit ``mode: isolated`` still emits
  owned containers (the enum's other half is not a no-op).
"""

from __future__ import annotations

import re
from typing import Any, Dict, Mapping

import pytest

from fluid_build.iac.backend import LEGACY_STATE_KEY, default_state_key, parse_backend
from fluid_build.iac.packaging import PackagingError
from fluid_build.iac.providers.aws import AwsIacPlugin
from fluid_build.iac.providers.gcp import GcpIacPlugin
from fluid_build.iac.providers.snowflake import SnowflakeIacPlugin

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Contract builders — one base contract per cloud, parameterised by packaging.
# ---------------------------------------------------------------------------


def _aws_contract(packaging: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """RFC Example 2 — shared S3 pool with prefix tenancy."""
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
                    "governance": {
                        "lakeFormation": {
                            "registerLocation": True,
                            "grants": [
                                {
                                    "principal": "arn:aws:iam::222222222222:role/consumer",
                                    "permissions": ["SELECT"],
                                }
                            ],
                        }
                    },
                },
                "contract": {"schema": [{"name": "device_id", "type": "string"}]},
            }
        ],
    }
    if packaging is not None:
        contract["packaging"] = packaging
    return contract


def _gcp_contract(packaging: Dict[str, Any] | None = None) -> Dict[str, Any]:
    contract: Dict[str, Any] = {
        "fluidVersion": "0.7.6",
        "id": "orders-adp",
        "name": "Orders ADP",
        "metadata": {
            "layer": "Silver",
            "productType": "ADP",
            "policies": {
                "analysts": {"principals": ["analytics@acme.com"], "permissions": ["read"]}
            },
        },
        "exposes": [
            {
                "exposeId": "orders",
                "binding": {
                    "platform": "gcp",
                    "format": "bigquery_table",
                    "location": {"dataset": "sales_pool", "table": "orders"},
                },
                "contract": {"schema": [{"name": "order_id", "type": "string"}]},
            },
            {
                "exposeId": "raw",
                "binding": {
                    "platform": "gcp",
                    "format": "gcs_bucket",
                    "location": {"bucket": "acme-lake", "path": "orders/"},
                },
            },
        ],
    }
    if packaging is not None:
        contract["packaging"] = packaging
    return contract


def _snowflake_contract(packaging: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """RFC Example 1 — Snowflake hybrid tier (shared lake, isolated compute)."""
    contract: Dict[str, Any] = {
        "fluidVersion": "0.7.6",
        "id": "orders-cdp",
        "name": "Orders CDP",
        "metadata": {"layer": "Gold", "productType": "CDP"},
        "exposes": [
            {
                "exposeId": "orders",
                "binding": {
                    "platform": "snowflake",
                    "format": "table",
                    "location": {
                        "database": "SALES_POOL",
                        "schema": "ORDERS_CDP",
                        "table": "ORDERS",
                        "warehouse": "ORDERS_CDP_WH",
                    },
                },
                "contract": {"schema": [{"name": "order_id", "type": "string"}]},
            }
        ],
    }
    if packaging is not None:
        contract["packaging"] = packaging
    return contract


SHARED = {"mode": "shared", "pool": "acme-pool"}
ISOLATED = {"mode": "isolated", "pool": "acme-pool"}


# ---------------------------------------------------------------------------
# AWS
# ---------------------------------------------------------------------------


class TestAwsPackagingEmit:
    def test_shared_bucket_becomes_a_data_source_and_is_not_owned(self):
        plugin = AwsIacPlugin()
        contract = _aws_contract(SHARED)
        resources = plugin.emit(contract)
        data = plugin.emit_data(contract)

        assert "aws_s3_bucket" not in resources
        assert data["aws_s3_bucket"]["telemetry_sdp_acme_iot_lake"] == {"bucket": "acme-iot-lake"}

    def test_shared_bucket_carries_no_force_destroy_anywhere(self):
        # The headline safety property: no emitted block may authorise
        # deleting a pool that other tenants share.
        rendered = repr(AwsIacPlugin().emit(_aws_contract(SHARED)))
        assert "force_destroy" not in rendered

    def test_shared_glue_database_is_inlined_and_the_table_stays_owned(self):
        # ``hashicorp/aws`` has no ``aws_glue_catalog_database`` data source
        # (``tofu validate`` rejects one), so a pooled Glue database is
        # addressed by literal name — the Snowflake treatment.
        plugin = AwsIacPlugin()
        contract = _aws_contract(SHARED)
        resources = plugin.emit(contract)
        data = plugin.emit_data(contract)

        assert "aws_glue_catalog_database" not in resources
        assert "aws_glue_catalog_database" not in data
        # The leaf inside the pool is still ours, addressing the pool by name.
        table = resources["aws_glue_catalog_table"]["telemetry_sdp_iot_pool_telemetry"]
        assert table["database_name"] == "iot_pool"

    def test_lake_formation_registers_the_prefix_not_the_pool_root(self):
        resources = AwsIacPlugin().emit(_aws_contract(SHARED))
        arns = [b["arn"] for b in resources["aws_lakeformation_resource"].values()]
        assert arns == ["arn:aws:s3:::acme-iot-lake/telemetry/"]

    def test_shared_bucket_without_a_path_fails_closed(self):
        # SECURITY REGRESSION: with no prefix to scope to, the LF
        # registration covers the pool root and the (authoritative) bucket
        # policy grants `s3:GetObject` on `arn:aws:s3:::<pool>/*` to every
        # LF principal — including cross-account ARNs — while replacing the
        # platform team's own policy. Widening a pool is the exact failure
        # shared mode exists to prevent, so this must not emit at all.
        contract = _aws_contract(SHARED)
        del contract["exposes"][0]["binding"]["location"]["path"]
        with pytest.raises(PackagingError) as excinfo:
            AwsIacPlugin().emit(contract)
        assert excinfo.value.kind == "shared-bucket-requires-path"

    def test_shared_bucket_grants_never_reach_the_whole_pool(self):
        # The property behind the regression above, asserted on the emitted
        # policy: no statement may target the bucket root or an unscoped
        # object wildcard.
        resources = AwsIacPlugin().emit(_aws_contract(SHARED))
        pool_root_wildcard = "arn:aws:s3:::acme-iot-lake/*"
        for statement in _policy_statements(next(iter(resources["aws_s3_bucket_policy"].values()))):
            resource = statement["Resource"]
            # The bucket-root wildcard reaches every tenant; the prefixed
            # form (`…/telemetry/*`) is the scoped one we want.
            assert resource != pool_root_wildcard
            if "s3:GetObject" in statement["Action"]:
                assert resource.startswith("arn:aws:s3:::acme-iot-lake/telemetry/")
            if "s3:ListBucket" in statement["Action"]:
                assert statement.get("Condition"), "ListBucket on a pool needs an s3:prefix"

    def test_an_isolated_bucket_without_a_path_is_still_fine(self):
        # Owning the whole bucket is the entire point of `isolated`, so the
        # fail-closed rule must not fire there.
        contract = _aws_contract(ISOLATED)
        del contract["exposes"][0]["binding"]["location"]["path"]
        resources = AwsIacPlugin().emit(contract)
        assert "aws_lakeformation_resource" in resources
        assert "aws_s3_bucket_policy" in resources

    def test_bucket_policy_targets_the_lookup_and_scopes_list_to_the_prefix(self):
        resources = AwsIacPlugin().emit(_aws_contract(SHARED))
        policy = next(iter(resources["aws_s3_bucket_policy"].values()))
        assert policy["bucket"] == "${data.aws_s3_bucket.telemetry_sdp_acme_iot_lake.id}"
        list_stmt = next(
            s for s in _policy_statements(policy) if s["Sid"].startswith("FluidLfBucketList")
        )
        assert list_stmt["Condition"] == {"StringLike": {"s3:prefix": ["telemetry/*"]}}

    def test_pool_id_is_stamped_into_glue_table_parameters(self):
        resources = AwsIacPlugin().emit(_aws_contract(SHARED))
        table = resources["aws_glue_catalog_table"]["telemetry_sdp_iot_pool_telemetry"]
        assert table["parameters"]["fluid_pool"] == "acme-pool"

    def test_legacy_contract_gets_no_pool_parameter_and_owns_its_bucket(self):
        plugin = AwsIacPlugin()
        contract = _aws_contract(None)
        resources = plugin.emit(contract)
        assert resources["aws_s3_bucket"]["telemetry_sdp_acme_iot_lake"]["force_destroy"] is True
        assert "aws_glue_catalog_database" in resources
        table = resources["aws_glue_catalog_table"]["telemetry_sdp_iot_pool_telemetry"]
        assert "fluid_pool" not in table["parameters"]
        # ``aws_caller_identity`` is the pre-existing Lake Formation lookup;
        # packaging adds no container data sources for a LEGACY contract.
        data = plugin.emit_data(contract)
        assert set(data) == {"aws_caller_identity"}

    def test_per_container_override_shares_the_bucket_but_owns_the_database(self):
        plugin = AwsIacPlugin()
        contract = _aws_contract(
            {"mode": "isolated", "pool": "acme-pool", "containers": {"bucket": "shared"}}
        )
        resources = plugin.emit(contract)
        data = plugin.emit_data(contract)
        assert "aws_s3_bucket" not in resources
        assert "aws_s3_bucket" in data
        assert "aws_glue_catalog_database" in resources


def _policy_statements(policy: Mapping[str, Any]) -> list:
    import json

    return json.loads(policy["policy"])["Statement"]


# ---------------------------------------------------------------------------
# GCP
# ---------------------------------------------------------------------------


class TestGcpPackagingEmit:
    def test_shared_dataset_becomes_a_data_source(self):
        plugin = GcpIacPlugin()
        contract = _gcp_contract(SHARED)
        resources = plugin.emit(contract)
        data = plugin.emit_data(contract)

        assert "google_bigquery_dataset" not in resources
        assert data["google_bigquery_dataset"]["orders_adp_sales_pool"] == {
            "dataset_id": "sales_pool"
        }
        table = resources["google_bigquery_table"]["orders_adp_orders"]
        assert (
            table["dataset_id"]
            == "${data.google_bigquery_dataset.orders_adp_sales_pool.dataset_id}"
        )

    def test_shared_dataset_drops_the_authoritative_acl_for_table_level_iam(self):
        # A dataset ``access[]`` block replaces the pool's whole ACL — a
        # tenant writing it would evict the pool's other tenants.
        plugin = GcpIacPlugin()
        resources = plugin.emit(_gcp_contract(SHARED))
        assert "google_bigquery_dataset" not in resources
        members = resources["google_bigquery_table_iam_member"]
        assert len(members) == 1
        member = next(iter(members.values()))
        assert member["role"] == "roles/bigquery.dataViewer"
        assert member["member"] == "user:analytics@acme.com"

    def test_isolated_dataset_keeps_the_dataset_acl(self):
        resources = GcpIacPlugin().emit(_gcp_contract(ISOLATED))
        dataset = resources["google_bigquery_dataset"]["orders_adp_sales_pool"]
        assert dataset["access"] == [{"role": "READER", "user_by_email": "analytics@acme.com"}]
        assert "google_bigquery_table_iam_member" not in resources

    def test_shared_bucket_is_a_data_source_with_no_force_destroy(self):
        plugin = GcpIacPlugin()
        contract = _gcp_contract(SHARED)
        resources = plugin.emit(contract)
        data = plugin.emit_data(contract)
        assert "google_storage_bucket" not in resources
        assert data["google_storage_bucket"]["orders_adp_acme_lake"] == {"name": "acme-lake"}
        assert "force_destroy" not in repr(resources)

    def test_shared_bucket_iam_is_narrowed_to_the_object_prefix(self):
        resources = GcpIacPlugin().emit(_gcp_contract(SHARED))
        member = next(iter(resources["google_storage_bucket_iam_member"].values()))
        assert member["bucket"] == "${data.google_storage_bucket.orders_adp_acme_lake.name}"
        assert member["condition"]["expression"] == (
            'resource.name.startsWith("projects/_/buckets/acme-lake/objects/orders/")'
        )

    def test_a_malicious_path_cannot_widen_the_iam_condition(self):
        # SECURITY: the condition is a CEL expression. An unescaped quote in
        # ``path`` would close the string literal and append ``|| true``,
        # widening the grant to every object in the pool — the exact
        # opposite of what the condition is for.
        contract = _gcp_contract(SHARED)
        contract["exposes"][1]["binding"]["location"]["path"] = 'x") || true || ("'
        resources = GcpIacPlugin().emit(contract)
        expression = next(iter(resources["google_storage_bucket_iam_member"].values()))[
            "condition"
        ]["expression"]
        # The injected quotes must be escaped, so they stay *inside* the
        # string literal as inert text rather than terminating it.
        assert '\\"' in expression
        # The security property: after removing the escaped quotes the
        # expression is still exactly one `startsWith("…")` call with
        # nothing appended — no second term, no trailing operator.
        assert re.fullmatch(r'resource\.name\.startsWith\("[^"]*"\)', expression.replace('\\"', ""))

    @pytest.mark.parametrize("path", [None, "", "/", "   ", "///"])
    def test_shared_bucket_grant_without_a_usable_path_fails_closed(self, path):
        # SECURITY REGRESSION: GCS bucket IAM is bucket-scoped, so with no
        # prefix condition the member reads every tenant's objects in the
        # pool. ``"/"`` and whitespace normalise away to an empty prefix and
        # would look scoped to a reviewer, so they must fail too.
        contract = _gcp_contract(SHARED)
        if path is None:
            contract["exposes"][1]["binding"]["location"].pop("path", None)
        else:
            contract["exposes"][1]["binding"]["location"]["path"] = path
        with pytest.raises(PackagingError) as excinfo:
            GcpIacPlugin().emit(contract)
        assert excinfo.value.kind == "shared-bucket-requires-path"

    def test_every_shared_bucket_member_is_conditioned(self):
        resources = GcpIacPlugin().emit(_gcp_contract(SHARED))
        for member in resources["google_storage_bucket_iam_member"].values():
            assert member.get("condition"), "a pool grant must carry a prefix condition"

    def test_an_isolated_bucket_without_a_path_is_still_fine(self):
        contract = _gcp_contract(ISOLATED)
        contract["exposes"][1]["binding"]["location"].pop("path", None)
        resources = GcpIacPlugin().emit(contract)
        assert "google_storage_bucket_iam_member" in resources

    def test_a_shared_bucket_with_no_grants_needs_no_path(self):
        # Nothing is being widened when there are no grants at all.
        contract = _gcp_contract(SHARED)
        contract["metadata"]["policies"] = {}
        contract["exposes"][1]["binding"]["location"].pop("path", None)
        resources = GcpIacPlugin().emit(contract)
        assert "google_storage_bucket_iam_member" not in resources

    def test_isolated_bucket_iam_carries_no_condition(self):
        resources = GcpIacPlugin().emit(_gcp_contract(ISOLATED))
        member = next(iter(resources["google_storage_bucket_iam_member"].values()))
        assert "condition" not in member
        assert resources["google_storage_bucket"]["orders_adp_acme_lake"]["force_destroy"] is True

    def test_pool_id_is_stamped_as_a_label(self):
        resources = GcpIacPlugin().emit(_gcp_contract(SHARED))
        table = resources["google_bigquery_table"]["orders_adp_orders"]
        assert table["labels"]["fluid_pool"] == "acme-pool"

    def test_legacy_contract_emits_no_data_block_and_no_pool_label(self):
        plugin = GcpIacPlugin()
        contract = _gcp_contract(None)
        assert plugin.emit_data(contract) == {}
        table = plugin.emit(contract)["google_bigquery_table"]["orders_adp_orders"]
        assert "fluid_pool" not in table["labels"]


# ---------------------------------------------------------------------------
# Snowflake
# ---------------------------------------------------------------------------


class TestSnowflakePackagingEmit:
    def test_rfc_example_1_hybrid_tier(self):
        # Shared database, owned schema, owned warehouse.
        plugin = SnowflakeIacPlugin()
        contract = _snowflake_contract(
            {
                "mode": "shared",
                "pool": "sales-domain",
                "containers": {"schema": "isolated", "warehouse": "isolated"},
            }
        )
        resources = plugin.emit(contract)
        assert "snowflake_database" not in resources
        assert "snowflake_schema" in resources
        assert "snowflake_warehouse" in resources
        # v1 emits no data block for the pool DB — Snowflake data sources
        # are thin (RFC file 5).
        assert plugin.emit_data(contract) == {}

    def test_referenced_database_is_inlined_as_a_literal_everywhere(self):
        # The load-bearing correctness fix: dropping the resource without
        # rewriting consumers leaves ``${snowflake_database…}`` dangling.
        # An owned schema inside the pooled DB is the case that exercises
        # *both* consumers — the schema body and the table body.
        resources = SnowflakeIacPlugin().emit(
            _snowflake_contract(
                {
                    "mode": "shared",
                    "pool": "sales-domain",
                    "containers": {"schema": "isolated"},
                }
            )
        )
        assert "snowflake_database" not in resources
        schema = resources["snowflake_schema"]["orders_cdp_SALES_POOL_ORDERS_CDP"]
        assert schema["database"] == "SALES_POOL"
        table = resources["snowflake_table"]["orders_cdp_SALES_POOL_ORDERS_CDP_ORDERS"]
        assert table["database"] == "SALES_POOL"

    def test_blanket_shared_inlines_both_database_and_schema(self):
        resources = SnowflakeIacPlugin().emit(
            _snowflake_contract({"mode": "shared", "pool": "sales-domain"})
        )
        assert "snowflake_database" not in resources
        assert "snowflake_schema" not in resources
        table = resources["snowflake_table"]["orders_cdp_SALES_POOL_ORDERS_CDP_ORDERS"]
        assert table["database"] == "SALES_POOL"
        assert table["schema"] == "ORDERS_CDP"

    def test_referenced_schema_is_inlined_too(self):
        resources = SnowflakeIacPlugin().emit(
            _snowflake_contract(
                {"mode": "shared", "pool": "sales-domain", "containers": {"database": "isolated"}}
            )
        )
        assert "snowflake_schema" not in resources
        assert "snowflake_database" in resources
        table = resources["snowflake_table"]["orders_cdp_SALES_POOL_ORDERS_CDP_ORDERS"]
        assert table["schema"] == "ORDERS_CDP"
        assert table["database"] == "${snowflake_database.orders_cdp_SALES_POOL.name}"

    def test_a_pool_name_that_is_not_an_identifier_fails_loudly(self):
        contract = _snowflake_contract({"mode": "shared", "pool": "sales-domain"})
        contract["exposes"][0]["binding"]["location"]["database"] = "SALES POOL; DROP"
        with pytest.raises(ValueError, match="Invalid SQL identifier"):
            SnowflakeIacPlugin().emit(contract)

    def test_legacy_emits_the_database_resource_and_no_warehouse(self):
        resources = SnowflakeIacPlugin().emit(_snowflake_contract(None))
        assert "snowflake_database" in resources
        assert "snowflake_warehouse" not in resources
        schema = resources["snowflake_schema"]["orders_cdp_SALES_POOL_ORDERS_CDP"]
        assert schema["database"] == "${snowflake_database.orders_cdp_SALES_POOL.name}"

    def test_shared_warehouse_is_not_provisioned(self):
        resources = SnowflakeIacPlugin().emit(
            _snowflake_contract({"mode": "shared", "pool": "sales-domain"})
        )
        assert "snowflake_warehouse" not in resources

    def test_pool_id_reaches_the_horizon_table_comment(self):
        resources = SnowflakeIacPlugin().emit(
            _snowflake_contract({"mode": "shared", "pool": "sales-domain"})
        )
        table = resources["snowflake_table"]["orders_cdp_SALES_POOL_ORDERS_CDP_ORDERS"]
        assert "- fluid_pool: sales-domain" in table["comment"]


# ---------------------------------------------------------------------------
# Cross-provider correctness: no dangling references
# ---------------------------------------------------------------------------

_REF_RE = re.compile(r"\$\{((?:data\.)?[A-Za-z0-9_]+\.[A-Za-z0-9_]+)\.")


def _declared_addresses(resources: Mapping[str, Any], data: Mapping[str, Any]) -> set:
    declared = {f"{rtype}.{name}" for rtype, block in resources.items() for name in block}
    declared |= {f"data.{dtype}.{name}" for dtype, block in data.items() for name in block}
    return declared


def _referenced_addresses(obj: Any, found: set) -> set:
    if isinstance(obj, str):
        found.update(_REF_RE.findall(obj))
    elif isinstance(obj, Mapping):
        for value in obj.values():
            _referenced_addresses(value, found)
    elif isinstance(obj, (list, tuple)):
        for value in obj:
            _referenced_addresses(value, found)
    return found


class TestNoDanglingReferences:
    """Every ``${type.name.attr}`` must resolve to a declared block.

    This is the generic form of the Snowflake bug the RFC calls out:
    dropping a container resource while a consumer still references it
    fails ``tofu validate`` with "Reference to undeclared resource". Checked
    for every provider across every packaging mode rather than field by
    field, so a future emitter cannot regress it silently.
    """

    @pytest.mark.parametrize(
        ("plugin", "builder"),
        [
            (AwsIacPlugin(), _aws_contract),
            (GcpIacPlugin(), _gcp_contract),
            (SnowflakeIacPlugin(), _snowflake_contract),
        ],
        ids=["aws", "gcp", "snowflake"],
    )
    @pytest.mark.parametrize(
        "packaging",
        [
            None,
            ISOLATED,
            SHARED,
            {"mode": "shared", "pool": "p", "containers": {"schema": "isolated"}},
            {"mode": "isolated", "pool": "p", "containers": {"bucket": "shared"}},
        ],
        ids=["legacy", "isolated", "shared", "hybrid-schema", "hybrid-bucket"],
    )
    def test_every_reference_resolves(self, plugin, builder, packaging):
        contract = builder(packaging)
        resources = plugin.emit(contract)
        data = plugin.emit_data(contract)
        declared = _declared_addresses(resources, data)
        referenced = _referenced_addresses(resources, set())
        # ``aws_caller_identity`` is emitted by ``emit_data`` on demand and
        # is already covered by ``declared``; anything else must match.
        assert referenced <= declared, f"dangling: {sorted(referenced - declared)}"

    @pytest.mark.parametrize(
        ("plugin", "builder"),
        [
            (AwsIacPlugin(), _aws_contract),
            (GcpIacPlugin(), _gcp_contract),
            (SnowflakeIacPlugin(), _snowflake_contract),
        ],
        ids=["aws", "gcp", "snowflake"],
    )
    def test_no_data_source_is_declared_but_unused(self, plugin, builder):
        # The mirror property: an orphan lookup means emit and emit_data
        # disagree about which containers exist.
        contract = builder(SHARED)
        data = plugin.emit_data(contract)
        referenced = _referenced_addresses(plugin.emit(contract), set())
        for dtype, block in data.items():
            for name in block:
                if dtype == "aws_caller_identity":
                    continue
                assert f"data.{dtype}.{name}" in referenced


# ---------------------------------------------------------------------------
# Import-adoption safety — the hazard the RFC names
# ---------------------------------------------------------------------------


class TestImportAdoptionSafety:
    """A shared pool must never be a ``tofu import`` candidate.

    ``_adopt_existing`` runs on every apply; an ungated candidate would
    adopt the platform's pool into this product's state — re-owning the
    exact container the contract declared it does not own.
    """

    def test_aws_excludes_shared_bucket_and_glue_database(self, monkeypatch):
        monkeypatch.setenv("AWS_ACCOUNT_ID", "111111111111")
        addresses = {b.to for b in AwsIacPlugin().discover_imports(_aws_contract(SHARED))}
        assert not any(a.startswith("aws_s3_bucket.") for a in addresses)
        assert not any(a.startswith("aws_glue_catalog_database.") for a in addresses)
        # The leaf inside the pool is still adoptable.
        assert "aws_glue_catalog_table.telemetry_sdp_iot_pool_telemetry" in addresses

    def test_aws_legacy_still_offers_the_containers(self, monkeypatch):
        monkeypatch.setenv("AWS_ACCOUNT_ID", "111111111111")
        addresses = {b.to for b in AwsIacPlugin().discover_imports(_aws_contract(None))}
        assert "aws_s3_bucket.telemetry_sdp_acme_iot_lake" in addresses
        assert "aws_glue_catalog_database.telemetry_sdp_iot_pool" in addresses

    def test_gcp_excludes_shared_dataset_and_bucket(self):
        addresses = {b.to for b in GcpIacPlugin().discover_imports(_gcp_contract(SHARED))}
        assert not any(a.startswith("google_bigquery_dataset.") for a in addresses)
        assert not any(a.startswith("google_storage_bucket.") for a in addresses)
        assert "google_bigquery_table.orders_adp_orders" in addresses

    def test_gcp_legacy_still_offers_the_containers(self):
        addresses = {b.to for b in GcpIacPlugin().discover_imports(_gcp_contract(None))}
        assert "google_bigquery_dataset.orders_adp_sales_pool" in addresses
        assert "google_storage_bucket.orders_adp_acme_lake" in addresses

    def test_snowflake_excludes_shared_database_and_schema(self):
        plugin = SnowflakeIacPlugin()
        addresses = {
            b.to
            for b in plugin.discover_imports(_snowflake_contract({"mode": "shared", "pool": "p"}))
        }
        assert not any(a.startswith("snowflake_database.") for a in addresses)
        assert not any(a.startswith("snowflake_schema.") for a in addresses)
        assert "snowflake_table.orders_cdp_SALES_POOL_ORDERS_CDP_ORDERS" in addresses

    def test_snowflake_hybrid_offers_only_the_owned_schema(self):
        contract = _snowflake_contract(
            {"mode": "shared", "pool": "p", "containers": {"schema": "isolated"}}
        )
        addresses = {b.to for b in SnowflakeIacPlugin().discover_imports(contract)}
        assert not any(a.startswith("snowflake_database.") for a in addresses)
        assert "snowflake_schema.orders_cdp_SALES_POOL_ORDERS_CDP" in addresses

    def test_snowflake_legacy_still_offers_the_containers(self):
        addresses = {b.to for b in SnowflakeIacPlugin().discover_imports(_snowflake_contract(None))}
        assert "snowflake_database.orders_cdp_SALES_POOL" in addresses
        assert "snowflake_schema.orders_cdp_SALES_POOL_ORDERS_CDP" in addresses


# ---------------------------------------------------------------------------
# Backend state key (RFC file 7)
# ---------------------------------------------------------------------------


class TestPackagingGatedStateKey:
    def test_legacy_contract_keeps_the_shared_key(self):
        contract = _aws_contract(None)
        assert default_state_key(contract) == LEGACY_STATE_KEY
        block = parse_backend("s3://state-bucket", contract)
        assert block["s3"]["key"] == "fluid/terraform.tfstate"

    def test_no_contract_keeps_the_shared_key(self):
        assert default_state_key(None) == LEGACY_STATE_KEY
        assert parse_backend("s3://state-bucket")["s3"]["key"] == "fluid/terraform.tfstate"

    def test_packaging_contract_gets_a_per_contract_key(self):
        contract = _aws_contract(SHARED)
        assert default_state_key(contract) == "fluid/telemetry_sdp/terraform.tfstate"
        block = parse_backend("s3://state-bucket", contract)
        assert block["s3"]["key"] == "fluid/telemetry_sdp/terraform.tfstate"

    def test_two_packaging_contracts_do_not_collide(self):
        a = parse_backend("s3://state-bucket", _aws_contract(SHARED))
        b = parse_backend("s3://state-bucket", _gcp_contract(SHARED))
        assert a["s3"]["key"] != b["s3"]["key"]

    def test_an_explicit_key_always_wins(self):
        block = parse_backend("s3://state-bucket/custom/path.tfstate", _aws_contract(SHARED))
        assert block["s3"]["key"] == "custom/path.tfstate"

    def test_gcs_prefix_isolates_the_same_way(self):
        assert parse_backend("gcs://state-bucket", _aws_contract(None)) == {
            "gcs": {"bucket": "state-bucket"}
        }
        block = parse_backend("gcs://state-bucket", _aws_contract(SHARED))
        assert block["gcs"]["prefix"] == "fluid/telemetry_sdp"

    def test_an_invalid_packaging_block_falls_back_rather_than_raising(self):
        # The typed error is reported by the emit path, which gives a far
        # better message than a traceback out of state-key derivation.
        contract = _aws_contract({"mode": "shared"})  # pool-required
        assert default_state_key(contract) == LEGACY_STATE_KEY


# ---------------------------------------------------------------------------
# Explicit `isolated` is not a no-op
# ---------------------------------------------------------------------------


class TestIsolatedIsTodaysShape:
    def test_isolated_owns_every_container(self):
        aws = AwsIacPlugin()
        assert "aws_s3_bucket" in aws.emit(_aws_contract(ISOLATED))
        assert "aws_glue_catalog_database" in aws.emit(_aws_contract(ISOLATED))
        # No container is looked up — the only data source is the
        # pre-existing Lake Formation caller identity.
        assert set(aws.emit_data(_aws_contract(ISOLATED))) == {"aws_caller_identity"}

        gcp = GcpIacPlugin()
        assert "google_bigquery_dataset" in gcp.emit(_gcp_contract(ISOLATED))
        assert gcp.emit_data(_gcp_contract(ISOLATED)) == {}

        snowflake = SnowflakeIacPlugin()
        assert "snowflake_database" in snowflake.emit(_snowflake_contract(ISOLATED))
