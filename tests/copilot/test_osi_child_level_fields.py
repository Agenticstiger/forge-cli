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

"""Pin the OSI v0.1.1 child-level optional fields the plan requires.

The OSI core-spec v0.1.1 allows ``ai_context`` and ``custom_extensions``
at the per-entity level (inside ``datasets[]``, ``fields[]``,
``relationships[]``, ``metrics[]``), plus ``unique_keys`` on a dataset
and ``label`` on a field. The v1.0 port only exposed them at the
semantic-model root — that is narrower than the spec and forces all
AI context to collapse onto one pile of synonyms.

This file pins:

* Every new attribute round-trips through Pydantic on the class it
  lives on.
* Every new attribute stays **optional** (default-None or
  default-empty-list) so v1.0 sidecars keep loading without
  modification. Backward-compat regressions here would silently break
  every cached ``.model.json`` that already exists in user workspaces.
* ``unique_keys`` is modelled as ``list[list[str]]`` so a dataset can
  declare multiple disjoint composite uniqueness constraints.
* A minimal-input construction (e.g. ``OSIDataset(name="x")``) keeps
  working — the defaults are explicit so future refactors can't flip
  one to required by accident.
"""

from __future__ import annotations

from fluid_build.copilot.schemas.osi import (
    OSIAIContext,
    OSICustomExtension,
    OSIDataset,
    OSIExpression,
    OSIExpressionDialect,
    OSIField,
    OSIMetric,
    OSIRelationship,
    OSISemanticModel,
)

# ---------------------------------------------------------------------------
# Backward compatibility — minimal construction still works everywhere
# ---------------------------------------------------------------------------


def test_osi_dataset_minimal_construction_preserves_defaults() -> None:
    """A v1.0 sidecar that only supplies ``name`` must still load
    without error and produce the documented empty defaults."""
    ds = OSIDataset(name="orders")
    assert ds.primary_key == []
    assert ds.unique_keys == []
    assert ds.fields == []
    assert ds.ai_context is None
    assert ds.custom_extensions == []


def test_osi_field_minimal_construction_preserves_defaults() -> None:
    field = OSIField(name="amount")
    assert field.label is None
    assert field.ai_context is None
    assert field.custom_extensions == []


def test_osi_relationship_minimal_construction_preserves_defaults() -> None:
    rel = OSIRelationship(name="orders_to_customers", **{"from": "orders", "to": "customers"})
    assert rel.ai_context is None
    assert rel.custom_extensions == []


def test_osi_metric_minimal_construction_preserves_defaults() -> None:
    metric = OSIMetric(
        name="total_revenue",
        expression=OSIExpression(
            dialects=[OSIExpressionDialect(dialect="ANSI_SQL", expression="SUM(amount)")]
        ),
    )
    assert metric.ai_context is None
    assert metric.custom_extensions == []


# ---------------------------------------------------------------------------
# Dataset: unique_keys + ai_context + custom_extensions
# ---------------------------------------------------------------------------


def test_dataset_accepts_unique_keys_as_list_of_lists() -> None:
    """``unique_keys`` must support multiple independent composite
    uniqueness constraints on one dataset — a one-dimensional list of
    strings wouldn't be able to express that."""
    ds = OSIDataset(
        name="orders",
        primary_key=["order_id"],
        unique_keys=[
            ["customer_id", "order_date"],
            ["tenant_id", "external_order_ref"],
        ],
    )
    assert ds.unique_keys == [
        ["customer_id", "order_date"],
        ["tenant_id", "external_order_ref"],
    ]


def test_dataset_accepts_ai_context_and_custom_extensions() -> None:
    ds = OSIDataset(
        name="orders",
        ai_context=OSIAIContext(
            instructions="Order-level grain. One row per order.",
            synonyms=["purchases"],
        ),
        custom_extensions=[
            OSICustomExtension(vendor_name="DBT", data='{"meta": {"owner": "analytics"}}')
        ],
    )
    assert ds.ai_context.instructions == "Order-level grain. One row per order."
    assert ds.ai_context.synonyms == ["purchases"]
    assert ds.custom_extensions[0].vendor_name == "DBT"


# ---------------------------------------------------------------------------
# Field: label + ai_context + custom_extensions
# ---------------------------------------------------------------------------


def test_field_accepts_label_for_business_glossary() -> None:
    field = OSIField(name="amount", label="Order amount (USD)")
    assert field.label == "Order amount (USD)"


def test_field_accepts_ai_context_and_custom_extensions() -> None:
    field = OSIField(
        name="amount",
        ai_context=OSIAIContext(
            instructions="Gross value in base currency.",
            examples=["SUM(orders.amount)"],
        ),
        custom_extensions=[
            OSICustomExtension(vendor_name="SNOWFLAKE", data='{"is_measure": true}')
        ],
    )
    assert field.ai_context.examples == ["SUM(orders.amount)"]
    assert field.custom_extensions[0].vendor_name == "SNOWFLAKE"


# ---------------------------------------------------------------------------
# Relationship: ai_context + custom_extensions
# ---------------------------------------------------------------------------


def test_relationship_accepts_ai_context_and_custom_extensions() -> None:
    rel = OSIRelationship(
        name="orders_to_customers",
        **{"from": "orders", "to": "customers"},
        from_columns=["customer_id"],
        to_columns=["id"],
        ai_context=OSIAIContext(instructions="Every order belongs to exactly one customer."),
        custom_extensions=[
            OSICustomExtension(vendor_name="DBT", data='{"relationship": "many_to_one"}')
        ],
    )
    assert rel.ai_context.instructions.startswith("Every order")
    assert rel.custom_extensions[0].data == '{"relationship": "many_to_one"}'


# ---------------------------------------------------------------------------
# Metric: ai_context + custom_extensions
# ---------------------------------------------------------------------------


def test_metric_accepts_ai_context_and_custom_extensions() -> None:
    metric = OSIMetric(
        name="total_revenue",
        expression=OSIExpression(
            dialects=[OSIExpressionDialect(dialect="ANSI_SQL", expression="SUM(orders.amount)")]
        ),
        ai_context=OSIAIContext(
            synonyms=["total sales", "gmv"],
            examples=["Revenue last 30 days"],
        ),
        custom_extensions=[
            OSICustomExtension(vendor_name="SALESFORCE", data='{"report": "Sales Summary"}')
        ],
    )
    assert "gmv" in metric.ai_context.synonyms
    assert metric.custom_extensions[0].vendor_name == "SALESFORCE"


# ---------------------------------------------------------------------------
# End-to-end — a full semantic model exercising every new slot round-trips
# ---------------------------------------------------------------------------


def test_full_semantic_model_with_every_new_field_roundtrips() -> None:
    model = OSISemanticModel(
        name="customer_orders",
        ai_context=OSIAIContext(instructions="Revenue analytics"),
        datasets=[
            OSIDataset(
                name="orders",
                primary_key=["order_id"],
                unique_keys=[["tenant_id", "external_order_ref"]],
                ai_context=OSIAIContext(instructions="Order-level grain"),
                custom_extensions=[OSICustomExtension(vendor_name="DBT", data="{}")],
                fields=[
                    OSIField(
                        name="amount",
                        label="Order amount (USD)",
                        ai_context=OSIAIContext(synonyms=["total"]),
                        custom_extensions=[OSICustomExtension(vendor_name="SNOWFLAKE", data="{}")],
                    ),
                ],
            ),
        ],
        relationships=[
            OSIRelationship(
                name="orders_to_customers",
                **{"from": "orders", "to": "customers"},
                ai_context=OSIAIContext(instructions="FK"),
                custom_extensions=[OSICustomExtension(vendor_name="DBT", data="{}")],
            ),
        ],
        metrics=[
            OSIMetric(
                name="total_revenue",
                expression=OSIExpression(
                    dialects=[OSIExpressionDialect(dialect="ANSI_SQL", expression="SUM(amount)")]
                ),
                ai_context=OSIAIContext(synonyms=["gmv"]),
                custom_extensions=[OSICustomExtension(vendor_name="SALESFORCE", data="{}")],
            ),
        ],
    )
    # Full JSON round-trip ensures serialisation stays lossless too.
    payload = model.model_dump_json()
    clone = OSISemanticModel.model_validate_json(payload)
    assert clone == model
