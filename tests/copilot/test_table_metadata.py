# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Coverage for the 6-category ``TableMetadata`` schema (D4).

This schema is the Pydantic port of Model AI's ``tools/table_metadata.py``
dataclass. Tests pin:

* **Minimal construction** — the four MUST identification fields are
  enough to build a valid instance; every other MUST field has a safe
  default so stubs can grow into complete records.
* **Defaults** — match the Model AI spec exactly so tooling on either
  side reads the same values back.
* **Sub-model nesting** — SurrogateKey, ReferentialKey, Partitioning,
  Index, SLA, DataQualityCheck all round-trip cleanly inside a parent
  TableMetadata.
* **Literal enforcement** — Pydantic rejects values outside the declared
  enums (partition type, storage tier, purge policy, classification,
  load strategy/pattern/frequency).
* **JSON round-trip** — ``model_dump(mode="json")`` →
  ``model_validate_json`` returns an equal object. This is the standard
  forge-cli store/API contract and matches how every other copilot
  schema is exercised (see ``test_intent_and_models.py``).
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from fluid_build.copilot.schemas.table_metadata import (
    SLA,
    DataQualityCheck,
    Index,
    Partitioning,
    ReferentialKey,
    SurrogateKey,
    TableMetadata,
)

# ----------------------------------------------------------------------
# A. Identification + defaults (MUST fields cover every category)
# ----------------------------------------------------------------------


def _minimal() -> TableMetadata:
    """Helper — the absolute minimum to construct a TableMetadata."""
    return TableMetadata(
        table_id="dim_customer",
        table_type="dimension",
        business_domain="customer_360",
        description="Conformed customer dimension (SCD2).",
    )


class TestMinimalConstruction:
    def test_minimal_must_fields_are_enough(self):
        table = _minimal()
        assert table.table_id == "dim_customer"
        assert table.table_type == "dimension"
        assert table.business_domain == "customer_360"
        assert table.description == "Conformed customer dimension (SCD2)."

    def test_missing_identification_field_raises(self):
        """Any of the four MUST identification fields is required —
        this guards against accidental weakening of the schema."""
        with pytest.raises(ValidationError):
            TableMetadata(
                table_type="dimension",
                business_domain="customer_360",
                description="…",
            )

    def test_defaults_match_model_ai_spec(self):
        """Pin every non-None default so a downstream tool reading these
        values gets the exact same shape it would from the Model AI
        dataclass. This is the cross-tool compatibility contract."""
        table = _minimal()

        # B — Keys defaults
        assert table.business_key_columns == []
        assert table.business_key_hash is False
        assert table.business_key_hash_algo is None
        assert table.surrogate_key is None
        assert table.natural_key_present is False
        assert table.referential_keys == []

        # C — Physical/Storage defaults
        assert table.file_format == "parquet"
        assert table.partitioning is None
        assert table.clustering_keys == []
        assert table.compression == "snappy"
        assert table.encryption is False
        assert table.storage_tier == "hot"

        # D — Load Strategy defaults
        assert table.load_strategy == "append_snapshot"
        assert table.load_pattern == "batch"
        assert table.load_frequency == "daily"
        assert table.source_systems == []
        assert table.cdc_capable is False

        # E — Constraints defaults
        assert table.primary_key == []
        assert table.unique_constraints == []
        assert table.not_null_constraints == []
        assert table.indexes == []
        assert table.business_rules == []

        # F — Governance defaults
        assert table.retention_policy_days is None
        assert table.purge_policy == "soft"
        assert table.pii_flag is False
        assert table.pii_columns == []
        assert table.data_classification == "internal"
        assert table.owner is None
        assert table.steward is None
        assert table.sla is None
        assert table.data_quality_checks == []
        assert table.lineage_ref is None
        assert table.tags == []


# ----------------------------------------------------------------------
# B. Keys & Keys Strategy — sub-models
# ----------------------------------------------------------------------


class TestKeysSubModels:
    def test_surrogate_key_nesting(self):
        sk = SurrogateKey(name="customer_sk", type="BIGINT", generation_rule="identity(1,1)")
        table = TableMetadata(
            table_id="dim_customer",
            table_type="dimension",
            business_domain="customer_360",
            description="…",
            surrogate_key=sk,
        )
        assert table.surrogate_key is not None
        assert table.surrogate_key.name == "customer_sk"
        assert table.surrogate_key.type == "BIGINT"
        assert table.surrogate_key.generation_rule == "identity(1,1)"

    def test_referential_key_nesting(self):
        rk = ReferentialKey(
            target_table="hub_customer",
            target_column="customer_hk",
            cardinality="N:1",
        )
        table = TableMetadata(
            table_id="lnk_order_customer",
            table_type="link",
            business_domain="retail",
            description="…",
            referential_keys=[rk],
        )
        assert len(table.referential_keys) == 1
        assert table.referential_keys[0].cardinality == "N:1"

    def test_business_key_list_and_hash_algo_accept_strings(self):
        table = TableMetadata(
            table_id="hub_customer",
            table_type="hub",
            business_domain="retail",
            description="…",
            business_key_columns=["customer_id", "tenant_id"],
            business_key_hash=True,
            business_key_hash_algo="sha256",
        )
        assert table.business_key_columns == ["customer_id", "tenant_id"]
        assert table.business_key_hash is True
        assert table.business_key_hash_algo == "sha256"


# ----------------------------------------------------------------------
# C. Physical / Storage — Literals + Partitioning
# ----------------------------------------------------------------------


class TestPhysicalStorage:
    def test_partitioning_round_trip(self):
        part = Partitioning(type="date", column="event_date", freq="daily", retention_days=365)
        table = TableMetadata(
            table_id="fact_events",
            table_type="fact",
            business_domain="analytics",
            description="…",
            partitioning=part,
        )
        assert table.partitioning is not None
        assert table.partitioning.type == "date"
        assert table.partitioning.column == "event_date"
        assert table.partitioning.freq == "daily"
        assert table.partitioning.retention_days == 365

    def test_partitioning_none_type_allowed(self):
        """The sentinel "none" is distinct from omitting the Partitioning
        altogether — it signals intentional absence."""
        part = Partitioning(type="none")
        assert part.column is None
        assert part.freq is None

    def test_invalid_partition_type_raises(self):
        with pytest.raises(ValidationError):
            Partitioning(type="weekly")  # "weekly" isn't in PartitionType

    def test_storage_tier_literal_enforced(self):
        table = TableMetadata(
            table_id="dim_customer",
            table_type="dimension",
            business_domain="customer_360",
            description="…",
            storage_tier="cold",
        )
        assert table.storage_tier == "cold"

    def test_invalid_storage_tier_raises(self):
        with pytest.raises(ValidationError):
            TableMetadata(
                table_id="dim_customer",
                table_type="dimension",
                business_domain="customer_360",
                description="…",
                storage_tier="frozen",  # not one of hot/warm/cold
            )


# ----------------------------------------------------------------------
# D. Load Strategy — Literals
# ----------------------------------------------------------------------


class TestLoadStrategy:
    @pytest.mark.parametrize(
        "strategy",
        [
            "append_snapshot",
            "append_incremental",
            "merge_upsert",
            "truncate_reload",
            "scd_type_2",
        ],
    )
    def test_valid_load_strategies(self, strategy: str):
        table = TableMetadata(
            table_id="t",
            table_type="fact",
            business_domain="d",
            description="…",
            load_strategy=strategy,
        )
        assert table.load_strategy == strategy

    def test_invalid_load_strategy_raises(self):
        with pytest.raises(ValidationError):
            TableMetadata(
                table_id="t",
                table_type="fact",
                business_domain="d",
                description="…",
                load_strategy="teleport",  # not in enum
            )

    @pytest.mark.parametrize("pattern", ["batch", "streaming", "microbatch", "on_demand"])
    def test_valid_load_patterns(self, pattern: str):
        table = TableMetadata(
            table_id="t",
            table_type="fact",
            business_domain="d",
            description="…",
            load_pattern=pattern,
        )
        assert table.load_pattern == pattern

    @pytest.mark.parametrize(
        "freq",
        [
            "continuous",
            "hourly",
            "daily",
            "weekly",
            "monthly",
            "quarterly",
            "ad_hoc",
        ],
    )
    def test_valid_load_frequencies(self, freq: str):
        table = TableMetadata(
            table_id="t",
            table_type="fact",
            business_domain="d",
            description="…",
            load_frequency=freq,
        )
        assert table.load_frequency == freq


# ----------------------------------------------------------------------
# E. Constraints & Indexes
# ----------------------------------------------------------------------


class TestConstraintsAndIndexes:
    def test_index_preserves_column_order(self):
        """``(a, b)`` and ``(b, a)`` are different indexes — the list
        order is load-bearing and must survive the round-trip."""
        idx = Index(columns=["last_name", "first_name"], type="btree")
        table = TableMetadata(
            table_id="dim_customer",
            table_type="dimension",
            business_domain="customer_360",
            description="…",
            indexes=[idx],
        )
        assert table.indexes[0].columns == ["last_name", "first_name"]
        # Round-trip preserves order
        loaded = TableMetadata.model_validate(table.model_dump())
        assert loaded.indexes[0].columns == ["last_name", "first_name"]

    def test_unique_constraints_is_list_of_lists(self):
        """Each unique constraint is a tuple of columns (represented as
        a list) so a table can declare multiple composite uniques."""
        table = TableMetadata(
            table_id="dim_customer",
            table_type="dimension",
            business_domain="customer_360",
            description="…",
            unique_constraints=[["email"], ["tenant_id", "customer_id"]],
        )
        assert len(table.unique_constraints) == 2
        assert table.unique_constraints[1] == ["tenant_id", "customer_id"]

    def test_primary_key_and_not_null_lists(self):
        table = TableMetadata(
            table_id="dim_customer",
            table_type="dimension",
            business_domain="customer_360",
            description="…",
            primary_key=["customer_sk"],
            not_null_constraints=["customer_sk", "customer_id"],
            business_rules=[
                "customer_id must match external CRM",
                "SCD2 version never decreases",
            ],
        )
        assert table.primary_key == ["customer_sk"]
        assert "customer_sk" in table.not_null_constraints
        assert len(table.business_rules) == 2


# ----------------------------------------------------------------------
# F. Lifecycle & Governance — SLA + DQ
# ----------------------------------------------------------------------


class TestLifecycleAndGovernance:
    def test_sla_nesting(self):
        sla = SLA(RTO="4h", RPO="1h")
        table = TableMetadata(
            table_id="dim_customer",
            table_type="dimension",
            business_domain="customer_360",
            description="…",
            sla=sla,
        )
        assert table.sla is not None
        assert table.sla.RTO == "4h"
        assert table.sla.RPO == "1h"

    def test_dq_check_with_threshold_and_parameters(self):
        """``threshold`` and ``parameters`` are deliberately open-ended
        so any DQ framework (GE / dbt-utils / Soda) can round-trip."""
        check = DataQualityCheck(
            name="customer_id_not_null",
            check_type="not_null",
            threshold=0.99,
            parameters={"severity": "error", "tags": ["pii"]},
        )
        table = TableMetadata(
            table_id="dim_customer",
            table_type="dimension",
            business_domain="customer_360",
            description="…",
            data_quality_checks=[check],
        )
        assert table.data_quality_checks[0].threshold == 0.99
        assert table.data_quality_checks[0].parameters == {
            "severity": "error",
            "tags": ["pii"],
        }

    def test_dq_check_minimal_nullable_fields(self):
        """threshold and parameters are both Optional — a bare check is
        valid so we can layer complexity on later."""
        check = DataQualityCheck(name="row_count", check_type="freshness")
        assert check.threshold is None
        assert check.parameters is None

    @pytest.mark.parametrize("policy", ["soft", "hard", "none"])
    def test_valid_purge_policies(self, policy: str):
        table = TableMetadata(
            table_id="t",
            table_type="fact",
            business_domain="d",
            description="…",
            purge_policy=policy,
        )
        assert table.purge_policy == policy

    def test_invalid_purge_policy_raises(self):
        with pytest.raises(ValidationError):
            TableMetadata(
                table_id="t",
                table_type="fact",
                business_domain="d",
                description="…",
                purge_policy="sometimes",
            )

    @pytest.mark.parametrize(
        "classification",
        ["public", "internal", "confidential", "restricted"],
    )
    def test_valid_data_classifications(self, classification: str):
        table = TableMetadata(
            table_id="t",
            table_type="fact",
            business_domain="d",
            description="…",
            data_classification=classification,
        )
        assert table.data_classification == classification

    def test_invalid_data_classification_raises(self):
        with pytest.raises(ValidationError):
            TableMetadata(
                table_id="t",
                table_type="fact",
                business_domain="d",
                description="…",
                data_classification="top_secret",
            )

    def test_pii_flag_and_columns(self):
        table = TableMetadata(
            table_id="dim_customer",
            table_type="dimension",
            business_domain="customer_360",
            description="…",
            pii_flag=True,
            pii_columns=["email", "ssn", "phone"],
            data_classification="confidential",
        )
        assert table.pii_flag is True
        assert "ssn" in table.pii_columns
        assert table.data_classification == "confidential"


# ----------------------------------------------------------------------
# Round-trip — the forge-cli store/API contract
# ----------------------------------------------------------------------


class TestJsonRoundTrip:
    def test_minimal_round_trip(self):
        original = _minimal()
        as_json = original.model_dump_json()
        loaded = TableMetadata.model_validate_json(as_json)
        assert loaded == original

    def test_full_fidelity_round_trip(self):
        """One table that exercises every category — verifies that
        model_dump(mode="json") / model_validate_json() is lossless
        across sub-models, Literals, and optional fields."""
        original = TableMetadata(
            table_id="sat_customer_profile",
            table_type="satellite",
            business_domain="customer_360",
            description="SCD2 satellite for customer profile attributes.",
            business_key_columns=["customer_hk"],
            business_key_hash=True,
            business_key_hash_algo="md5",
            surrogate_key=SurrogateKey(
                name="sat_sk", type="BIGINT", generation_rule="identity(1,1)"
            ),
            natural_key_present=False,
            referential_keys=[
                ReferentialKey(
                    target_table="hub_customer",
                    target_column="customer_hk",
                    cardinality="N:1",
                )
            ],
            file_format="parquet",
            partitioning=Partitioning(
                type="date",
                column="load_date",
                freq="daily",
                retention_days=2555,
            ),
            clustering_keys=["customer_hk", "load_date"],
            compression="zstd",
            encryption=True,
            storage_tier="warm",
            load_strategy="scd_type_2",
            load_pattern="microbatch",
            load_frequency="hourly",
            source_systems=["crm", "web_events"],
            cdc_capable=True,
            primary_key=["sat_sk"],
            unique_constraints=[["customer_hk", "load_date"]],
            not_null_constraints=["customer_hk", "load_date", "hash_diff"],
            indexes=[
                Index(columns=["customer_hk"], type="btree"),
                Index(columns=["load_date"], type="btree"),
            ],
            business_rules=["SCD2 end-date can never precede start-date"],
            retention_policy_days=2555,
            purge_policy="soft",
            pii_flag=True,
            pii_columns=["email", "phone"],
            data_classification="confidential",
            owner="data-platform@example.com",
            steward="retail-domain-team@example.com",
            sla=SLA(RTO="4h", RPO="15m"),
            data_quality_checks=[
                DataQualityCheck(
                    name="hash_diff_unique",
                    check_type="unique",
                    threshold=1.0,
                    parameters={"columns": ["customer_hk", "hash_diff"]},
                )
            ],
            lineage_ref="lineage://domain/customer/sat_profile",
            tags=["customer", "scd2", "pii"],
        )
        as_json = original.model_dump_json()
        # Ensure it's actually JSON and round-trips to an equal object.
        parsed = json.loads(as_json)
        assert parsed["storage_tier"] == "warm"
        assert parsed["partitioning"]["type"] == "date"
        assert parsed["sla"]["RTO"] == "4h"

        loaded = TableMetadata.model_validate_json(as_json)
        assert loaded == original

    def test_dump_mode_json_uses_plain_types(self):
        """``model_dump(mode="json")`` must return JSON-serializable
        plain types (no Enum/datetime objects) — store backends rely on
        this for their JSON-on-disk layout."""
        original = _minimal()
        dumped = original.model_dump(mode="json")
        # Sanity: it survives json.dumps without a custom encoder.
        serialized = json.dumps(dumped)
        assert "dim_customer" in serialized
