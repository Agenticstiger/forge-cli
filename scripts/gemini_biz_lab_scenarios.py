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

"""Multi-scenario smoke runner for the forge data-model pipeline on Gemini.

Goal: before a live biz-lab demo, exercise the ``forge data-model
from-intent`` pipeline against real Gemini across the four
industry × technique combos that ship with seed skeletons:

* ``telecommunications`` × ``data_vault_2`` (TMF SID)
* ``retail``             × ``dimensional`` (NRF ARTS)
* ``healthcare``         × ``data_vault_2`` (HL7 FHIR)
* ``finance``            × ``dimensional`` (ISO 20022)

For each scenario the runner captures:

* Wall-clock latency
* Number of produced hubs/links/sats (DV2) or facts/dims (dimensional)
* Naming-convention compliance rate (hub_* / sat_* / fact_* / dim_*)
* SCD2 default survivability (DV2 only)
* Validator outcome: passes_schema + warning list
* Canonical-coverage render

Usage::

    GEMINI_API_KEY=... python scripts/gemini_biz_lab_scenarios.py

The runner is standalone (no pytest); it prints a markdown summary
table at the end so the results can be pasted directly into a demo
intent. Exits non-zero if any scenario failed outright (network, JSON
parse, schema fail) — warnings and partial coverage are **not** fails.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from fluid_build.cli.forge_copilot_llm_providers import (
    BUILTIN_LLM_PROVIDERS,
    LlmConfig,
)
from fluid_build.copilot.agents.base import StageSession
from fluid_build.copilot.industry.compiler import IndustryPackCompiler
from fluid_build.copilot.schemas.intent import (
    BusinessIntent,
    DataProduct,
    Dimensions,
    Grain,
    Metric,
)
from fluid_build.copilot.store.backends.null import NullBackend
from fluid_build.forge_datamodel.emit.coverage import compute_canonical_coverage
from fluid_build.forge_datamodel.from_intent.pipeline import run_from_intent

HUB_NAME = re.compile(r"^hub_[a-z][a-z0-9_]*$")
SAT_NAME = re.compile(r"^sat_[a-z][a-z0-9_]*$")
LNK_NAME = re.compile(r"^lnk_[a-z][a-z0-9_]*$")
FACT_NAME = re.compile(r"^fact_[a-z][a-z0-9_]*$")
DIM_NAME = re.compile(r"^dim_[a-z][a-z0-9_]*$")


@dataclass
class Scenario:
    industry: str
    technique: str
    intent: BusinessIntent


@dataclass
class ScenarioResult:
    industry: str
    technique: str
    ok: bool
    latency_s: float
    entity_counts: Dict[str, int] = field(default_factory=dict)
    naming_ok: Dict[str, int] = field(default_factory=dict)
    naming_total: Dict[str, int] = field(default_factory=dict)
    scd_distribution: Dict[str, int] = field(default_factory=dict)
    passes_schema: bool = False
    warning_count: int = 0
    error_count: int = 0
    coverage_lines: List[str] = field(default_factory=list)
    failure: Optional[str] = None


# ---------------------------------------------------------------------------
# Scenario intents — one per industry, realistic but minimal.
# ---------------------------------------------------------------------------


def _telco_intent() -> BusinessIntent:
    return BusinessIntent(
        data_product=DataProduct(
            name="customer_subscriptions",
            domain="telecommunications",
            description=(
                "Customer subscription analytics for a telco operator. "
                "Tracks parties, products, services, and subscription events "
                "across the customer lifecycle."
            ),
        ),
        grain=Grain(
            entity="subscription_event",
            time_dimension="event_date",
            description="One row per subscription state change per customer per service.",
        ),
        metrics=[
            Metric(
                name="active_subscribers", description="Distinct parties with an active service"
            ),
            Metric(name="churn_rate", description="Churned / active at period start"),
        ],
        dimensions=Dimensions(
            entities=["party", "service", "product_offering", "resource"],
            attributes=["event_type", "channel", "geography"],
        ),
    )


def _retail_intent() -> BusinessIntent:
    return BusinessIntent(
        data_product=DataProduct(
            name="sales_analytics",
            domain="retail",
            description=(
                "Omnichannel sales analytics for a specialty retailer. "
                "Captures transaction lines, customers, products, and stores "
                "for a Kimball-style star schema."
            ),
        ),
        grain=Grain(
            entity="sales_line",
            time_dimension="transaction_date",
            description="One row per line item per transaction.",
        ),
        metrics=[
            Metric(name="gross_revenue", description="Sum of line extended amount"),
            Metric(name="units_sold", description="Sum of line quantity"),
            Metric(name="basket_size", description="Units per transaction"),
        ],
        dimensions=Dimensions(
            entities=["customer", "product", "store", "date"],
            attributes=["channel", "promotion", "loyalty_tier"],
        ),
    )


def _healthcare_intent() -> BusinessIntent:
    return BusinessIntent(
        data_product=DataProduct(
            name="patient_encounters",
            domain="healthcare",
            description=(
                "Patient encounter analytics across a hospital network. "
                "Captures encounters, observations, and procedures aligned "
                "to HL7 FHIR resources."
            ),
        ),
        grain=Grain(
            entity="encounter_observation",
            time_dimension="observation_datetime",
            description="One row per observation per encounter.",
        ),
        metrics=[
            Metric(name="avg_length_of_stay", description="Mean encounter duration in days"),
            Metric(name="readmission_rate", description="30-day readmissions / total discharges"),
        ],
        dimensions=Dimensions(
            entities=["patient", "encounter", "observation", "practitioner"],
            attributes=["department", "diagnosis_code", "encounter_class"],
        ),
    )


def _finance_intent() -> BusinessIntent:
    return BusinessIntent(
        data_product=DataProduct(
            name="payment_analytics",
            domain="finance",
            description=(
                "Payment-transaction analytics aligned to ISO 20022 messages. "
                "Captures instructions, settlements, and counterparties."
            ),
        ),
        grain=Grain(
            entity="payment_instruction",
            time_dimension="value_date",
            description="One row per payment instruction.",
        ),
        metrics=[
            Metric(name="total_settled", description="Sum of settled amount"),
            Metric(name="fx_margin", description="Average FX spread vs. mid-market"),
            Metric(name="reject_rate", description="Rejected / submitted"),
        ],
        dimensions=Dimensions(
            entities=["counterparty", "currency", "instrument", "channel"],
            attributes=["message_type", "status", "region"],
        ),
    )


def _scenarios() -> List[Scenario]:
    return [
        Scenario("telecommunications", "data_vault_2", _telco_intent()),
        Scenario("retail", "dimensional", _retail_intent()),
        Scenario("healthcare", "data_vault_2", _healthcare_intent()),
        Scenario("finance", "dimensional", _finance_intent()),
    ]


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def _build_session(
    api_key: str, pack_industry: str, technique: str, workspace: Path
) -> tuple[StageSession, Any]:
    provider = BUILTIN_LLM_PROVIDERS["gemini"]
    model = provider.default_model
    config = LlmConfig(
        provider="gemini",
        model=model,
        endpoint=provider.default_endpoint(model, {"GEMINI_API_KEY": api_key}),
        api_key=api_key,
        timeout_seconds=120,
        streaming=False,
    )
    pack = IndustryPackCompiler().compile(pack_industry, technique=technique)
    session = StageSession(
        store=NullBackend(),
        workspace_root=workspace,
        llm_config=config,
        active_provider="gemini",
        no_cache=True,
        industry_pack=pack,
        require_llm=True,
    )
    return session, pack


def _naming_check(names: List[str], pattern: re.Pattern) -> tuple[int, int]:
    ok = sum(1 for n in names if pattern.match(n))
    return ok, len(names)


def _run_scenario(s: Scenario, api_key: str, workspace: Path) -> ScenarioResult:
    result = ScenarioResult(industry=s.industry, technique=s.technique, ok=False, latency_s=0.0)
    session, pack = _build_session(api_key, s.industry, s.technique, workspace)

    started = time.time()
    try:
        pipeline = run_from_intent(session, intent=s.intent, technique=s.technique)
    except Exception:
        result.latency_s = round(time.time() - started, 2)
        result.failure = traceback.format_exc(limit=6)
        return result
    result.latency_s = round(time.time() - started, 2)

    logical = pipeline.coordinator.logical
    validation = pipeline.validation

    if s.technique == "data_vault_2":
        assert logical.dv2 is not None, "dv2 branch must be populated for data_vault_2"
        hubs = [h.hub_table_name for h in logical.dv2.hubs]
        links = [l.link_table_name for l in logical.dv2.links]
        sats = [sa.satellite_table_name for sa in logical.dv2.satellites]
        result.entity_counts = {"hubs": len(hubs), "links": len(links), "satellites": len(sats)}
        for key, names, pat in [
            ("hub", hubs, HUB_NAME),
            ("link", links, LNK_NAME),
            ("sat", sats, SAT_NAME),
        ]:
            ok, total = _naming_check(names, pat)
            result.naming_ok[key] = ok
            result.naming_total[key] = total
        # SCD distribution across satellites — defaults to type2 in the schema,
        # so an absence of type1/append_only is expected for a vanilla run.
        for sa in logical.dv2.satellites:
            result.scd_distribution[sa.change_tracking] = (
                result.scd_distribution.get(sa.change_tracking, 0) + 1
            )
    else:  # dimensional
        assert (
            logical.dimensional is not None
        ), "dimensional branch must be populated for dimensional"
        facts = [f.name for f in logical.dimensional.facts]
        dims = [d.name for d in logical.dimensional.dimensions]
        result.entity_counts = {"facts": len(facts), "dimensions": len(dims)}
        for key, names, pat in [("fact", facts, FACT_NAME), ("dim", dims, DIM_NAME)]:
            ok, total = _naming_check(names, pat)
            result.naming_ok[key] = ok
            result.naming_total[key] = total

    result.passes_schema = bool(getattr(validation, "passes_schema", False))
    issues = list(getattr(validation, "issues", []) or [])
    result.warning_count = sum(1 for i in issues if getattr(i, "severity", "warning") == "warning")
    result.error_count = sum(1 for i in issues if getattr(i, "severity", "warning") == "error")

    coverage = compute_canonical_coverage(logical, pack)
    if coverage is not None:
        result.coverage_lines = coverage.render().splitlines()

    result.ok = result.passes_schema
    return result


def _print_report(results: List[ScenarioResult]) -> None:
    print()
    print("=" * 78)
    print("Forge data-model · Gemini biz-lab scenarios · report")
    print("=" * 78)
    header = "| Industry | Technique | OK | Latency | Counts | Naming | SCD | Schema | Warns |"
    sep = "|---|---|---|---|---|---|---|---|---|"
    print(header)
    print(sep)
    for r in results:
        status = "✓" if r.ok else "✗"
        counts = ", ".join(f"{k}={v}" for k, v in r.entity_counts.items()) or "—"
        naming = (
            ", ".join(
                f"{k}={r.naming_ok.get(k,0)}/{r.naming_total.get(k,0)}" for k in r.naming_total
            )
            or "—"
        )
        scd = ", ".join(f"{k}={v}" for k, v in r.scd_distribution.items()) or "—"
        schema = "✓" if r.passes_schema else "✗"
        print(
            f"| {r.industry} | {r.technique} | {status} | {r.latency_s}s | {counts} | {naming} | {scd} | {schema} | {r.warning_count} |"
        )
    print()
    for r in results:
        print(f"### {r.industry} × {r.technique}")
        if r.failure:
            print("FAILURE:")
            print(r.failure)
            continue
        for line in r.coverage_lines:
            print(line)
        print()


def _serialize_results(results: List[ScenarioResult]) -> str:
    """Return the JSON representation of a (possibly partial) results list."""
    return json.dumps(
        [
            {
                "industry": r.industry,
                "technique": r.technique,
                "ok": r.ok,
                "latency_s": r.latency_s,
                "entity_counts": r.entity_counts,
                "naming_ok": r.naming_ok,
                "naming_total": r.naming_total,
                "scd_distribution": r.scd_distribution,
                "passes_schema": r.passes_schema,
                "warning_count": r.warning_count,
                "error_count": r.error_count,
                "coverage": r.coverage_lines,
                "failure": r.failure,
            }
            for r in results
        ],
        indent=2,
    )


def _flush_report(results: List[ScenarioResult], out_path: Path) -> None:
    """Write the JSON report atomically so a mid-run kill still leaves a valid file."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp_path.write_text(_serialize_results(results))
    tmp_path.replace(out_path)


def main() -> int:
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        print("GEMINI_API_KEY not set — aborting.", file=sys.stderr)
        return 2

    workspace = Path.cwd()
    scenarios = _scenarios()
    results: List[ScenarioResult] = []
    out_path = Path(".fluid") / "gemini_biz_lab_report.json"

    for s in scenarios:
        print(f"→ running {s.industry} × {s.technique} …", flush=True)
        results.append(_run_scenario(s, api_key, workspace))
        # Persist incrementally so a timeout / SIGTERM preserves completed scenarios.
        _flush_report(results, out_path)
        print(
            f"  ↳ partial report flushed ({len(results)}/{len(scenarios)}) to {out_path}",
            flush=True,
        )

    _print_report(results)
    _flush_report(results, out_path)
    print(f"JSON report written to {out_path}")

    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
