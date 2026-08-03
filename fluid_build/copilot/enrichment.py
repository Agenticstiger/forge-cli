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

"""Post-synthesis deterministic enrichment.

Runs the four Wave 2 heuristic tools (``dbt_test_generator``,
``freshness_emitter``, ``physical_layout``, ``pii.classify_contract_schemas``)
against the synthesised contract and writes their outputs to
``.fluid/agents/<run_id>/enrichment/`` so:

* the JudgeAgent can read them via ``build_artifacts`` and score the
  ``security`` / ``performance`` / ``governance`` / ``documentation``
  axes against deterministic-tool output (not just the raw contract);
* the CLI / future builders can pick them up as a starting point for
  ``schema.yml`` / source ``freshness:`` / clustering hints / column
  PII tags without re-running heuristics.

Pure-Python helper — no LLM calls, no new deps. Fail-open: any tool
failure logs at DEBUG and the run continues. Kill-switch
``FLUID_COPILOT_ENRICHMENT=0`` for operators who want raw-contract
judge scores.

The PII pass (the H6 fix) is the 4th step; it tags columns by NAME only
(no value scanning) using the same enrichment receipt slot. See
:mod:`fluid_build.copilot.pii` for the full vocabulary and prior-art
citations (piicatcher, Presidio, GCP DLP, AWS Glue).
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from fluid_build.copilot.pii import classify_contract_schemas
from fluid_build.copilot.tools.dbt_test_generator import generate_dbt_tests
from fluid_build.copilot.tools.freshness_emitter import propose_freshness
from fluid_build.copilot.tools.physical_layout import suggest_physical_layout

LOG = logging.getLogger("fluid.copilot.enrichment")

__all__ = [
    "ENRICHMENT_DIRNAME",
    "enrich_contract",
    "enrichment_enabled",
    "extract_schemas_from_contract",
    "resolve_provider",
    "resolve_refresh_cadence",
]


ENRICHMENT_DIRNAME = "enrichment"


def enrichment_enabled() -> bool:
    """Kill-switch for the deterministic enrichment pass."""
    value = os.environ.get("FLUID_COPILOT_ENRICHMENT", "1").strip().lower()
    return value not in {"0", "false", "no", "off"}


# ---------------------------------------------------------------------------
# Contract → tool-input normalizer.
# FLUID schema columns are minimal (``{name, type}``); we extract richer
# markers when they exist but don't require them. The Wave 2 tools all
# handle missing fields gracefully so the normalizer can be lenient.
# ---------------------------------------------------------------------------


_TRUTHY = {True, "true", "True", "TRUE", "yes", "Yes", "1"}


def _is_true(val: Any) -> bool:
    return val in _TRUTHY


def _first_present(d: Dict[str, Any], *keys: str, default: Any = None) -> Any:
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


def _normalize_column(col: Dict[str, Any]) -> Dict[str, Any]:
    """Map a FLUID column dict to the Wave 2 tools' expected shape.

    Accepted aliases (any of these markers turns on the corresponding
    Wave 2 tool feature):

    * primary key: ``primary``, ``primaryKey``, ``primary_key``, ``pk``,
      ``isPrimary``
    * nullable: ``nullable``, ``required`` (inverted), ``optional``
    * foreign key: ``foreignKey``, ``foreign_key``, ``references`` (with
      ``to``/``table`` + ``field``/``column`` sub-keys)
    * enum: ``enum``, ``acceptedValues``, ``accepted_values``
    * min/max: ``min``/``max``, ``minimum``/``maximum``
    """
    name = col.get("name") or col.get("column") or ""
    type_ = col.get("type") or col.get("dataType") or col.get("data_type") or ""

    primary = any(
        _is_true(col.get(k)) for k in ("primary", "primaryKey", "primary_key", "pk", "isPrimary")
    )

    nullable: Optional[bool] = None
    if "nullable" in col:
        nullable = bool(col["nullable"])
    elif "required" in col:
        nullable = not bool(col["required"])
    elif "optional" in col:
        nullable = bool(col["optional"])

    fk_raw = _first_present(col, "foreignKey", "foreign_key", "references")
    foreign_key: Optional[Dict[str, str]] = None
    if isinstance(fk_raw, dict):
        to = fk_raw.get("to") or fk_raw.get("table") or fk_raw.get("model")
        field = fk_raw.get("field") or fk_raw.get("column") or fk_raw.get("on")
        if to and field:
            foreign_key = {"to": str(to), "field": str(field)}

    enum = _first_present(col, "enum", "acceptedValues", "accepted_values")
    if enum is not None and not isinstance(enum, list):
        enum = None

    out: Dict[str, Any] = {"name": str(name), "type": str(type_)}
    if primary:
        out["primary_key"] = True
    if nullable is not None:
        out["nullable"] = nullable
    if foreign_key:
        out["foreign_key"] = foreign_key
    if enum:
        out["enum"] = list(enum)
    for low_key, src_keys in (("min", ("min", "minimum")), ("max", ("max", "maximum"))):
        val = _first_present(col, *src_keys)
        if val is not None:
            out[low_key] = val
    description = col.get("description")
    if description:
        out["description"] = str(description)
    return out


def extract_schemas_from_contract(contract: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return one ``{model_name, columns}`` dict per exposed dataset.

    Tolerant of multiple shapes — FLUID's canonical is
    ``contract.exposes[].contract.schema`` as a list of column dicts,
    but we also accept ``contract.exposes[].schema``,
    ``contract.exposes[].columns``, and the ODCS-style
    ``contract.models[].columns``.
    """
    out: List[Dict[str, Any]] = []
    exposes = contract.get("exposes") or []
    for expose in exposes:
        if not isinstance(expose, dict):
            continue
        model_name = (
            expose.get("exposeId") or expose.get("name") or expose.get("id") or "exposed_dataset"
        )
        # Find the columns list under any of the supported shapes.
        ec = expose.get("contract") or {}
        raw_schema = (
            (ec.get("schema") if isinstance(ec, dict) else None)
            or expose.get("schema")
            or expose.get("columns")
        )
        if isinstance(raw_schema, dict):  # ODCS-style {columns: [...]}
            raw_columns = raw_schema.get("columns") or []
        elif isinstance(raw_schema, list):
            raw_columns = raw_schema
        else:
            raw_columns = []
        cols = [_normalize_column(c) for c in raw_columns if isinstance(c, dict)]
        out.append({"model_name": str(model_name), "columns": cols})

    # Fallback: ODCS / dbt-shaped ``models`` array at the top level.
    if not out:
        for model in contract.get("models") or []:
            if not isinstance(model, dict):
                continue
            cols = [
                _normalize_column(c) for c in (model.get("columns") or []) if isinstance(c, dict)
            ]
            out.append({"model_name": str(model.get("name") or "model"), "columns": cols})
    return out


def resolve_refresh_cadence(contract: Dict[str, Any]) -> Optional[str]:
    """Best-effort lookup of the contract's refresh cadence."""
    meta = contract.get("metadata") or {}
    refresh = contract.get("refresh") or {}
    for source in (meta, refresh):
        if not isinstance(source, dict):
            continue
        cad = (
            source.get("refreshCadence")
            or source.get("refresh_cadence")
            or source.get("cadence")
            or source.get("frequency")
        )
        if cad:
            return str(cad)
    return None


def resolve_provider(contract: Dict[str, Any]) -> str:
    """Best-effort provider resolution for the physical layout tool.

    Reads ``builds[0].engine`` then ``exposes[0].binding.platform``; maps
    common synonyms (``dbt`` engine isn't a provider — look at the
    binding instead). Defaults to ``"snowflake"`` so the tool still
    emits *something* — the caller can override.
    """
    builds = contract.get("builds") or []
    engine = ""
    if builds and isinstance(builds[0], dict):
        engine = str(builds[0].get("engine") or "")
    exposes = contract.get("exposes") or []
    platform = ""
    if exposes and isinstance(exposes[0], dict):
        platform = str(((exposes[0].get("binding") or {}).get("platform") or ""))
    candidate = (platform or engine).lower()
    if "snowflake" in candidate:
        return "snowflake"
    if "bigquery" in candidate or "bq" in candidate or "gcp" in candidate:
        return "bigquery"
    if "athena" in candidate or "aws" in candidate or "s3" in candidate:
        return "athena"
    if "redshift" in candidate:
        return "redshift"
    return "snowflake"


# ---------------------------------------------------------------------------
# Public entry point — runs the three tools, persists artifacts, returns
# the in-process dict the JudgeAgent consumes via ``build_artifacts``.
# ---------------------------------------------------------------------------


def enrich_contract(
    contract: Dict[str, Any],
    *,
    run_id: Optional[str] = None,
    workspace_root: Optional[Path] = None,
    logger: Optional[logging.Logger] = None,
) -> Optional[Dict[str, Any]]:
    """Run the four Wave 2 tools, write artifacts, return the dict.

    Returns ``None`` when the enrichment pass is disabled. Otherwise
    returns ``{"dbt_tests": [...], "freshness": {...},
    "physical_layout": [...], "pii_tags": {...}}``.

    Best-effort: any tool failure logs at DEBUG and the slot is set to
    ``None`` / empty in the returned dict.

    The PII pass mutates the contract dict **in place** — column
    schemas under ``exposes[].contract.schema`` gain ``tags`` /
    ``sensitivity`` / ``semanticType`` for matched PII columns. The
    other three tools are read-only.
    """
    if not enrichment_enabled():
        return None
    log = logger or LOG

    schemas = extract_schemas_from_contract(contract)
    provider = resolve_provider(contract)
    cadence = resolve_refresh_cadence(contract)

    # dbt tests — one schema.yml dict per exposed dataset.
    dbt_tests: List[Dict[str, Any]] = []
    for schema in schemas:
        try:
            dbt_tests.append(generate_dbt_tests(schema, dialect=provider))
        except Exception as exc:  # noqa: BLE001 — fail-open per tool
            log.debug(
                "enrichment_dbt_tests_failed model=%s error=%r", schema.get("model_name"), exc
            )

    # Freshness — one block per contract (FLUID has at most one refresh
    # cadence per product; multi-source CDC is handled via source_type).
    freshness: Dict[str, Any] = {}
    if cadence:
        try:
            freshness = propose_freshness(cadence)
        except Exception as exc:  # noqa: BLE001 — fail-open
            log.debug("enrichment_freshness_failed cadence=%s error=%r", cadence, exc)

    # Physical layout — one suggestion per exposed dataset.
    physical_layout: List[Dict[str, Any]] = []
    for schema in schemas:
        try:
            physical_layout.append(suggest_physical_layout(schema, provider=provider))
        except Exception as exc:  # noqa: BLE001 — fail-open
            log.debug("enrichment_layout_failed model=%s error=%r", schema.get("model_name"), exc)

    # PII tags — name-based pre-classifier. Mutates ``contract`` in
    # place (attaches ``tags`` / ``sensitivity`` / ``semanticType`` on
    # matched columns) and returns a per-model summary for the receipt.
    # H6 fix — previously the security axis silently missed obvious
    # PII columns. Kill-switch FLUID_COPILOT_PII_CLASSIFIER=0 disables.
    pii_tags: Dict[str, Any] = {"models": [], "totals": {}}
    try:
        pii_tags = classify_contract_schemas(contract)
    except Exception as exc:  # noqa: BLE001 — fail-open
        log.debug("enrichment_pii_failed error=%r", exc)

    artifacts: Dict[str, Any] = {
        "provider": provider,
        "refresh_cadence": cadence,
        "dbt_tests": dbt_tests,
        "freshness": freshness,
        "physical_layout": physical_layout,
        "pii_tags": pii_tags,
    }

    # Persistence — best-effort. The receipt dir is .fluid/agents/<run-id>/enrichment/
    # under the workspace root (CWD when not supplied), matching every
    # other receipt-emitting agent.
    try:
        _persist_artifacts(artifacts, run_id=run_id, workspace_root=workspace_root)
    except Exception as exc:  # noqa: BLE001 — observability-only
        log.debug("enrichment_persist_failed error=%r", exc)

    pii_total = sum((pii_tags.get("totals") or {}).values())
    log.info(
        "enrichment summary: %d test set(s), freshness=%s, %d layout suggestion(s), "
        "%d PII column(s) tagged",
        len(dbt_tests),
        "yes" if freshness else "no",
        len(physical_layout),
        pii_total,
    )

    return artifacts


def _persist_artifacts(
    artifacts: Dict[str, Any],
    *,
    run_id: Optional[str],
    workspace_root: Optional[Path],
) -> None:
    """Write artifacts to .fluid/agents/<run_id>/enrichment/."""
    # Resolve run_id from env / file if not provided; do NOT create a
    # persisted file (enrichment is out-of-loop, never first stage).
    if not run_id:
        try:
            from fluid_build.observability.run_id import get_or_create_run_id

            run_id = get_or_create_run_id(create_persisted_file=False)
        except Exception:  # noqa: BLE001 — keep going without persistence
            return
    if not run_id:
        return

    root = Path(workspace_root or Path.cwd()).resolve()
    out_dir = root / ".fluid" / "agents" / run_id / ENRICHMENT_DIRNAME
    out_dir.mkdir(parents=True, exist_ok=True)

    # dbt schema.yml-style — multi-doc YAML keeps one model per file
    # under a single dir, but a single combined file is enough for the
    # judge and avoids per-model name collisions on disk.
    if artifacts.get("dbt_tests"):
        (out_dir / "tests.yml").write_text(
            yaml.safe_dump_all(artifacts["dbt_tests"], sort_keys=False),
            encoding="utf-8",
        )
    if artifacts.get("freshness"):
        (out_dir / "freshness.yml").write_text(
            yaml.safe_dump(artifacts["freshness"], sort_keys=False),
            encoding="utf-8",
        )
    if artifacts.get("physical_layout"):
        (out_dir / "layout.json").write_text(
            json.dumps(artifacts["physical_layout"], indent=2, sort_keys=False),
            encoding="utf-8",
        )
    pii_artifacts = artifacts.get("pii_tags") or {}
    pii_has_content = bool(pii_artifacts.get("models")) or bool(pii_artifacts.get("totals"))
    if pii_has_content:
        (out_dir / "pii.json").write_text(
            json.dumps(pii_artifacts, indent=2, sort_keys=False),
            encoding="utf-8",
        )
    # Summary index so downstream consumers can find everything.
    (out_dir / "index.json").write_text(
        json.dumps(
            {
                "provider": artifacts.get("provider"),
                "refresh_cadence": artifacts.get("refresh_cadence"),
                "files": {
                    "dbt_tests": "tests.yml" if artifacts.get("dbt_tests") else None,
                    "freshness": "freshness.yml" if artifacts.get("freshness") else None,
                    "physical_layout": "layout.json" if artifacts.get("physical_layout") else None,
                    "pii_tags": "pii.json" if pii_has_content else None,
                },
            },
            indent=2,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
