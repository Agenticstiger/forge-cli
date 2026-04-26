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

"""Load and explain BusinessIntent inputs from YAML or JSON files."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from fluid_build.copilot.schemas.intent import BusinessIntent

INTENT_EXAMPLES: dict[str, dict[str, Any]] = {
    "minimal": {
        "data_product": {
            "name": "customer_orders",
            "domain": "retail",
            "description": "Order facts and customer dimensions for analytics.",
        },
        "grain": {
            "entity": "order_line",
            "time_dimension": "order_date",
            "description": "One row per order line.",
        },
        "dimensions": {
            "entities": ["customer", "product", "store"],
            "attributes": ["name", "category"],
        },
        "metrics": [
            {
                "name": "total_revenue",
                "description": "Sum of order line revenue.",
            }
        ],
    },
    "retail": {
        "data_product": {
            "name": "retail_sales_performance",
            "domain": "retail",
            "description": "Sales, margin, and product performance by customer, store, and channel.",
            "owner": "retail-analytics",
        },
        "business_context": {
            "problem_statement": "Merchandising and store teams need consistent sales performance analytics.",
            "decision_supported": "Pricing, replenishment, promotion, and assortment decisions.",
            "consumer": "merchandising analysts",
        },
        "modeling": {"technique": "dimensional", "scd_policy_default": "type2"},
        "grain": {
            "entity": "sales_line",
            "time_dimension": "sale_date",
            "description": "One row per sold product line on a transaction.",
        },
        "dimensions": {
            "entities": ["customer", "product", "store", "channel", "promotion"],
            "attributes": ["name", "category", "status", "region"],
        },
        "metrics": [
            {"name": "gross_sales", "description": "Gross sales before returns."},
            {"name": "net_sales", "description": "Sales after returns and discounts."},
            {"name": "margin_amount", "description": "Net sales less cost of goods sold."},
        ],
        "data_sources": [
            {
                "source_name": "pos_sales",
                "source_type": "snowflake",
                "description": "Point-of-sale transaction line items.",
            }
        ],
        "business_rules": [
            "Exclude voided transactions.",
            "Use the latest active product hierarchy unless historical reporting is requested.",
        ],
    },
    "telco": {
        "data_product": {
            "name": "telco_service_health",
            "domain": "telecommunications",
            "description": "Customer service, subscription, device, and trouble ticket model.",
            "owner": "network-analytics",
        },
        "business_context": {
            "problem_statement": "Service operations need a governed model for churn, quality, and trouble ticket analytics.",
            "decision_supported": "Retention, network quality triage, and service assurance.",
            "consumer": "service operations analysts",
        },
        "modeling": {"technique": "data_vault_2", "hash_key_algorithm": "sha256"},
        "grain": {
            "entity": "service",
            "time_dimension": "service_start_date",
            "description": "One row per active service relationship.",
        },
        "dimensions": {
            "entities": ["account", "party", "subscription", "device", "trouble_ticket"],
            "attributes": ["status", "type", "segment", "region"],
        },
        "metrics": [
            {"name": "active_services", "description": "Count of active services."},
            {"name": "open_tickets", "description": "Count of unresolved trouble tickets."},
            {"name": "monthly_recurring_revenue", "description": "Recurring service revenue."},
        ],
        "data_sources": [
            {
                "source_name": "telco_stage_load",
                "source_type": "snowflake",
                "description": "Seeded telco source tables.",
            }
        ],
        "business_rules": [
            "Treat service status as the authoritative active/inactive indicator.",
            "Keep account-party relationships historized.",
        ],
    },
    "finance": {
        "data_product": {
            "name": "finance_risk_exposure",
            "domain": "finance",
            "description": "Exposure, counterparty, portfolio, and risk measure analytics.",
            "owner": "risk-analytics",
        },
        "business_context": {
            "problem_statement": "Risk teams need consistent exposure and limit monitoring.",
            "decision_supported": "Counterparty review, portfolio limits, and capital planning.",
            "consumer": "risk analysts",
        },
        "modeling": {"technique": "dimensional", "scd_policy_default": "type2"},
        "grain": {
            "entity": "exposure_position",
            "time_dimension": "as_of_date",
            "description": "One row per exposure position at the reporting date.",
        },
        "dimensions": {
            "entities": ["counterparty", "portfolio", "instrument", "region"],
            "attributes": ["name", "rating", "segment", "status"],
        },
        "metrics": [
            {"name": "exposure_amount", "description": "Current exposure amount."},
            {"name": "expected_loss", "description": "Probability-weighted expected loss."},
            {"name": "limit_utilization", "description": "Exposure divided by approved limit."},
        ],
        "data_sources": [
            {
                "source_name": "risk_positions",
                "source_type": "warehouse",
                "description": "Daily exposure positions and counterparty attributes.",
            }
        ],
        "business_rules": [
            "Report exposure in base currency.",
            "Use approved counterparty hierarchy for rollups.",
        ],
    },
}


class IntentValidationError(ValueError):
    """Friendly validation error for business intent files."""


def render_intent_example(name: str = "minimal") -> str:
    key = (name or "minimal").strip().lower()
    if key not in INTENT_EXAMPLES:
        choices = ", ".join(sorted(INTENT_EXAMPLES))
        raise IntentValidationError(f"unknown intent example {name!r}; choose one of: {choices}")
    return yaml.safe_dump(INTENT_EXAMPLES[key], sort_keys=False)


def render_intent_schema_json() -> str:
    return json.dumps(BusinessIntent.model_json_schema(), indent=2, sort_keys=True) + "\n"


def load_business_intent(path: str | Path) -> BusinessIntent:
    file_path = Path(path)
    suffix = file_path.suffix.lower()
    if suffix not in {".yaml", ".yml", ".json"}:
        raise IntentValidationError("intent files must be YAML or JSON")
    if not file_path.exists():
        raise IntentValidationError(f"intent file does not exist: {file_path}")

    raw = file_path.read_text(encoding="utf-8")
    if not raw.strip():
        raise IntentValidationError("intent file is empty")

    try:
        if suffix == ".json":
            payload = json.loads(raw)
        else:
            payload = yaml.safe_load(raw)
    except json.JSONDecodeError as exc:
        raise IntentValidationError(f"intent file is not valid JSON: {exc}") from exc
    except yaml.YAMLError as exc:
        raise IntentValidationError(f"intent file is not valid YAML: {exc}") from exc

    if payload is None:
        raise IntentValidationError("intent file is empty")

    try:
        intent = BusinessIntent.model_validate(payload)
    except ValidationError as exc:
        raise IntentValidationError(_friendly_pydantic_error(exc)) from exc

    _assert_useful_intent(intent)
    return intent


def _friendly_pydantic_error(exc: ValidationError) -> str:
    errors = exc.errors()
    for error in errors:
        loc = ".".join(str(part) for part in error.get("loc", ()))
        if loc == "data_product.name":
            return "intent file is missing data_product.name"
        if loc == "data_product.domain":
            return "intent file is missing data_product.domain"
        if loc == "data_product":
            return "intent file is missing data_product"
    if errors:
        error = errors[0]
        loc = ".".join(str(part) for part in error.get("loc", ())) or "root"
        return f"intent file has invalid {loc}: {error.get('msg', 'invalid value')}"
    return "intent file is invalid"


def _assert_useful_intent(intent: BusinessIntent) -> None:
    has_grain = bool(intent.grain and intent.grain.entity)
    has_dimensions = bool(intent.dimensions and intent.dimensions.entities)
    has_metrics = bool(intent.metrics)
    has_sources = bool(intent.data_sources)
    if not (has_grain or has_dimensions or has_metrics or has_sources):
        raise IntentValidationError(
            "intent file needs at least one grain, dimension, metric, or data source"
        )
