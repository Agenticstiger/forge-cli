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

"""Legacy template-contract coercion + richer-receipt enrichment.

Lifted from ``cli/forge_modes.py`` (host file was 2082 LOC). Three
functions: ``_populate_richer_receipt``, ``_coerce_dq_rules``, and
``_coerce_template_contract_to_v073``. They are pure transforms over
contract dicts — no provider state, no I/O. ``forge_modes.py``
re-imports each at module top so existing test patches that target
``fluid_build.cli.forge_modes.<helper>`` keep resolving via the
namespace.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Mapping


def _populate_richer_receipt(
    *,
    panel: Any,
    contract: Mapping[str, Any],
    generation_result: Any,
    context: Mapping[str, Any],
    logger: Any,
) -> None:
    """Populate every panel field from the interview state + agent loop +
    final contract — produces a receipt that answers "why was this
    contract shaped this way?" without re-running the agent.

    Best-effort: any exception is swallowed so receipt-richening never
    blocks a successful forge run. Caps lists at 64 entries each to
    keep the receipt under ~10 KB.
    """
    try:
        md = (contract or {}).get("metadata") or {}
        if md.get("productType"):
            panel.add_decision("data_product_type", md["productType"], source="contract")
        if md.get("layer"):
            panel.add_decision("layer", md["layer"], source="contract")
        if contract.get("domain"):
            panel.add_decision("domain", contract["domain"], source="contract")
        owner = md.get("owner") or {}
        if isinstance(owner, dict):
            if owner.get("team"):
                panel.add_decision("owner_team", owner["team"], source="contract")
            if owner.get("email"):
                panel.add_decision("owner_email", owner["email"], source="contract")
        # Every build's pattern + engine — multi-build contracts get
        # one decision per build so the receipt diff'ed across runs
        # surfaces engine swaps cleanly.
        for build in (contract.get("builds") or [])[:8]:
            if isinstance(build, dict):
                pattern = build.get("pattern", "?")
                engine = build.get("engine", "?")
                panel.add_decision(
                    f"build:{build.get('id', 'main')}",
                    f"{pattern}/{engine}",
                    source="contract",
                )
        for expose in (contract.get("exposes") or [])[:8]:
            if isinstance(expose, dict):
                expose_id = expose.get("exposeId") or "?"
                kind = expose.get("kind") or "?"
                schema_cols = (expose.get("contract") or {}).get("schema") or []
                panel.add_decision(
                    f"expose:{expose_id}",
                    f"{kind} ({len(schema_cols)} columns)",
                    source="contract",
                )
        for upstream in (contract.get("consumes") or [])[:16]:
            if isinstance(upstream, dict):
                panel.add_decision(
                    f"consumes:{upstream.get('productId', '?')}",
                    upstream.get("exposeId", "?"),
                    source="contract",
                )

        # Interview turns — every Q&A from CopilotInterviewState
        for turn in (context.get("interview_turns") or [])[:64]:
            if isinstance(turn, dict):
                panel.append_transcript(
                    {
                        "kind": "interview_turn",
                        "field": turn.get("field"),
                        "role": turn.get("role"),
                        "question_id": turn.get("question_id"),
                        "answer": turn.get("resolved_value")
                        or turn.get("content")
                        or turn.get("raw_input"),
                    }
                )

        # Every assumption the interview / runtime captured
        for note in (context.get("assumptions") or [])[:64]:
            panel.add_assumption(str(note))

        # Tools called by the agent loop (legacy registry entries)
        for tool_event in (
            (getattr(generation_result, "provenance", None) or {}).get("agent_events") or []
        )[:64]:
            if isinstance(tool_event, dict) and tool_event.get("tool_name"):
                panel.add_tool_call(str(tool_event["tool_name"]))

        # Provider + model on the cost snapshot stay surfaced
        prov = getattr(generation_result, "provenance", None) or {}
        if prov.get("llm_provider"):
            panel.add_decision("llm_provider", prov["llm_provider"], source="provenance")
        if prov.get("llm_model"):
            panel.add_decision("llm_model", prov["llm_model"], source="provenance")
        if prov.get("attempt"):
            panel.add_decision(
                "attempts_used",
                str(prov["attempt"]),
                source="provenance",
                rationale="how many LLM round-trips it took to validate",
            )
    except Exception as exc:  # noqa: BLE001 — receipt enrichment is best-effort
        try:
            logger.debug("populate_richer_receipt_failed: %s", exc)
        except Exception:  # noqa: BLE001
            pass


_V073_METADATA_ALLOWED = {
    "layer",
    "productType",
    "owner",
    "domain",
    "businessContext",
    "provenance",
    "labels",
}

_V073_BUILD_PATTERN_ALLOWED = {
    "declarative",
    "hybrid-reference",
    "embedded-logic",
    "logical-mapping",
    "acquisition",
}


def _coerce_dq_rules(legacy_rules: List[Any]) -> List[Dict[str, Any]]:
    """Drop legacy DQ-rule fields the v0.7.3 schema doesn't accept.

    Legacy templates ship ``[{name, rule, onFailure}, …]``; the schema
    expects each rule to carry ``id``, ``type``, ``selector``, etc.
    Best-effort: keep ``id`` (from name), drop the rest.
    """
    cleaned: List[Dict[str, Any]] = []
    for entry in legacy_rules:
        if not isinstance(entry, dict):
            continue
        rule_id = entry.get("id") or entry.get("name") or "rule"
        cleaned.append(
            {
                "id": str(rule_id),
                "type": "completeness",
                "selector": "id",
                "threshold": 1.0,
                "operator": ">=",
                "severity": "warn",  # v0.7.3 enum: info/warn/error/critical
            }
        )
    return cleaned


def _coerce_template_contract_to_v073(contract: Dict[str, Any]) -> Dict[str, Any]:
    """Best-effort patch of legacy template output to fluid-schema-0.7.3.

    Legacy forge templates emit contracts with v0.5-vintage shapes:

    * ``consumes[].id`` + ``ref`` instead of ``productId`` + ``exposeId``
    * ``metadata.tags`` / ``status`` / ``created`` / ``template`` /
      ``forge_version`` (none of these are 0.7.3 metadata fields)
    * ``builds[].transformation: {pattern, engine, properties}`` nested
      instead of flat ``builds[].pattern`` / ``engine`` / ``properties``
    * ``exposes[].schema`` / ``description`` / ``quality`` instead of
      ``exposes[].contract.schema``
    * ``binding.dataset`` / ``binding.table`` / ``binding.format=table``

    This function maps each known legacy shape to its v0.7.3 equivalent
    so ``fluid validate`` accepts the output. Unknown extras are
    dropped rather than passed through (fail-safe — the schema
    rejects them anyway).
    """
    if not isinstance(contract, dict):
        return contract
    # Fast path: contracts produced by the v0.7.3 builder declare their
    # version up front — skip the legacy coercion entirely so we don't
    # round-trip a clean payload through fixups designed for v0.5
    # vintage. The strict schema validator still runs downstream.
    if contract.get("fluidVersion", "").startswith(("0.7.", "0.8.")):
        return dict(contract)
    out = dict(contract)

    # Drop top-level fields the v0.7.3 schema doesn't recognise
    # (legacy templates ship ``ml_config`` / ``slo`` / domain-specific
    # blocks at the root). The schema is closed at the top level.
    _LEGAL_TOP_LEVEL = {
        "fluidVersion",
        "kind",
        "id",
        "name",
        "description",
        "domain",
        "metadata",
        "consumes",
        "builds",
        "build",
        "exposes",
        "lineage",
        "schemaEvolution",
        "machineLearning",
        "environments",
        "sovereignty",
        "tags",
        "labels",
    }
    for stray_root in [k for k in list(out.keys()) if k not in _LEGAL_TOP_LEVEL]:
        out.pop(stray_root, None)

    # Pin to the current fluid version. Templates emit 0.7.3 directly via the v0.7.3 builder.
    fv = str(out.get("fluidVersion") or "")
    if not fv or fv.startswith(("0.4", "0.5", "0.6")):
        try:
            from fluid_build.schema_manager import FluidSchemaManager

            out["fluidVersion"] = FluidSchemaManager.latest_bundled_version()
        except Exception:  # noqa: BLE001
            out["fluidVersion"] = "0.7.3"

    # ── id / name fall-backs ────────────────────────────────────────
    if not out.get("id"):
        out["id"] = "generated.product.placeholder_v1"
    if not out.get("name"):
        out["name"] = out["id"].split(".")[-1].replace("_", " ").title()
    # IDs must match identifier pattern; coerce empty/leading-dash to safe.
    if isinstance(out.get("id"), str):
        cleaned = out["id"].strip().strip("-_.").lower() or "generated_product"
        out["id"] = cleaned

    # Top-level fields the schema doesn't allow — drop or move.
    for stray in ("tags", "labels"):
        if stray in out and stray not in {"labels"}:
            out.pop(stray, None)

    # ── metadata: only canonical fields ────────────────────────────
    md = out.get("metadata")
    if isinstance(md, dict):
        clean_md = {k: v for k, v in md.items() if k in _V073_METADATA_ALLOWED}
        # Pull labels from md.tags into md.labels.tags if needed for round-trip
        out["metadata"] = clean_md
        try:
            from fluid_build.forge.product_types import normalize_metadata_in_place

            normalize_metadata_in_place(out["metadata"])
        except Exception:  # noqa: BLE001 — never block on coercion
            pass

    # ── consumes: id/ref → productId/exposeId ───────────────────────
    consumes = out.get("consumes")
    if isinstance(consumes, list):
        new_consumes: List[Dict[str, str]] = []
        for entry in consumes:
            if not isinstance(entry, dict):
                continue
            product_id = entry.get("productId") or entry.get("id") or ""
            expose_id = entry.get("exposeId")
            if not expose_id and isinstance(entry.get("ref"), str):
                # ``urn:fluid:foo:v1`` → ``foo`` is a reasonable expose default.
                ref = entry["ref"]
                if ":" in ref:
                    expose_id = (
                        ref.split(":")[-2] if len(ref.split(":")) >= 4 else ref.split(":")[-1]
                    )
                else:
                    expose_id = ref
            if not expose_id:
                expose_id = "main"
            row = {"productId": str(product_id), "exposeId": str(expose_id)}
            new_consumes.append(row)
        out["consumes"] = new_consumes

    # ── builds: flatten nested transformation block ──────────────────
    builds = out.get("builds")
    if isinstance(builds, list):
        new_builds: List[Dict[str, Any]] = []
        for build in builds:
            if not isinstance(build, dict):
                continue
            new = dict(build)
            tr = new.pop("transformation", None)
            if isinstance(tr, dict):
                new["pattern"] = tr.get("pattern", new.get("pattern", "embedded-logic"))
                new["engine"] = tr.get("engine", new.get("engine", "sql"))
                new["properties"] = tr.get("properties", new.get("properties", {}))
            new.setdefault("id", "main_build")
            new.setdefault("pattern", "embedded-logic")
            if new["pattern"] not in _V073_BUILD_PATTERN_ALLOWED:
                new["pattern"] = "embedded-logic"
            # Coerce engines to the closed v0.7.3 enum.
            engine_now = str(new.get("engine") or "").lower()
            _ENGINE_FALLBACK = {
                "beam": "spark",
                "flink": "spark",
                "dataflow": "spark",
                "kafka_streams": "kafka-connect",
                "kafka-streams": "kafka-connect",
            }
            if engine_now in _ENGINE_FALLBACK:
                new["engine"] = _ENGINE_FALLBACK[engine_now]
            elif engine_now and engine_now not in {
                "dbt",
                "sql",
                "python",
                "spark",
                "custom",
                "duckdb",
                "airbyte",
                "meltano",
                "dlt",
                "kafka-connect",
                "debezium",
            }:
                new["engine"] = "custom"
            # embedded-logic requires properties.sql; python builds
            # require properties.model + repository. hybrid-reference
            # (dbt-style) requires ``properties.model`` (model name).
            engine = str(new.get("engine") or "").lower()
            if new["pattern"] == "hybrid-reference":
                props = new.setdefault("properties", {})
                if not props.get("model"):
                    # Promote a legacy ``model_path`` to ``model`` if present.
                    if isinstance(props.get("model_path"), str):
                        props["model"] = (
                            props["model_path"].rsplit("/", 1)[-1].rsplit(".", 1)[0] or "main_model"
                        )
                    else:
                        props["model"] = "main_model"
                # hybrid-reference allows: model, vars. Drop everything else.
                _ALLOWED_HYBRID = {"model", "vars"}
                for stray in [k for k in list(props.keys()) if k not in _ALLOWED_HYBRID]:
                    props.pop(stray, None)
            elif new["pattern"] == "embedded-logic":
                props = new.setdefault("properties", {})
                if engine == "python":
                    # Python builds ride on embedded-logic in v0.7.3 with
                    # a properties.model entry-point and a repository path.
                    if not props.get("model"):
                        # Best-effort guess from a script field if templates
                        # set one, otherwise fall back to a placeholder.
                        script = props.pop("script", None)
                        if isinstance(script, str) and script:
                            module_path = script.rsplit(".py", 1)[0].replace("/", ".")
                            props["model"] = f"{module_path}:main"
                        else:
                            props["model"] = "src.main:build"
                    new.setdefault("repository", "src/main.py")
                    # Drop python-specific keys that aren't in the schema
                    for stray in ("environment", "requirements"):
                        props.pop(stray, None)
                else:
                    if not props.get("sql"):
                        props["sql"] = "SELECT 1 AS id"
                    # embedded-logic only allows: sql, language. Drop everything else.
                    _ALLOWED_EMBEDDED = {"sql", "language"}
                    for stray in [k for k in list(props.keys()) if k not in _ALLOWED_EMBEDDED]:
                        props.pop(stray, None)
                    props.pop("language", None)  # we don't set language by default
            # Strip any free-form description that the schema rejects
            new.pop("description", None)
            # Coerce trigger.type to canonical 'schedule'
            ex = new.get("execution")
            if isinstance(ex, dict):
                tr_block = ex.get("trigger")
                if isinstance(tr_block, dict):
                    legacy_to_canonical = {
                        "scheduled": "schedule",
                        "cron": "schedule",
                        "continuous": "schedule",
                        "streaming": "schedule",
                        "kafka": "event",
                    }
                    if tr_block.get("type") in legacy_to_canonical:
                        tr_block["type"] = legacy_to_canonical[tr_block["type"]]
                    if tr_block.get("type") not in {
                        "schedule",
                        "event",
                        "manual",
                        "dependency",
                        "dataset",
                        "schedule_and_dataset",
                        "timetable",
                    }:
                        tr_block["type"] = "manual"
            new_builds.append(new)
        out["builds"] = new_builds

    # ── exposes: schema → contract.schema; binding fixes ─────────────
    exposes = out.get("exposes")
    if isinstance(exposes, list):
        new_exposes: List[Dict[str, Any]] = []
        for ex in exposes:
            if not isinstance(ex, dict):
                continue
            new = dict(ex)
            new.pop("description", None)
            # Legacy field-name mapping: id→exposeId, type→kind, location→binding.location
            if "exposeId" not in new and isinstance(new.get("id"), str):
                new["exposeId"] = new.pop("id")
            elif "id" in new and "exposeId" in new:
                new.pop("id", None)
            if "kind" not in new and isinstance(new.get("type"), str):
                new["kind"] = new.pop("type")
            elif "type" in new and "kind" in new:
                new.pop("type", None)
            # Top-level ``location`` (not under binding) — synthesize binding.
            stray_location = new.pop("location", None)
            if stray_location is not None and "binding" not in new:
                # Strip legacy format/properties off the location dict —
                # v0.7.3's location schema is closed (path/bucket/etc only).
                if isinstance(stray_location, dict):
                    clean_loc = {
                        k: v
                        for k, v in stray_location.items()
                        if k not in {"format", "properties", "type"}
                    }
                else:
                    clean_loc = {"path": str(stray_location)}
                new["binding"] = {
                    "platform": "local",
                    "format": "csv",
                    "location": clean_loc or {"path": "runtime/out/main.csv"},
                }
            # Move ``schema`` and ``quality`` under ``contract``
            if "contract" not in new:
                contract_block: Dict[str, Any] = {}
                if isinstance(new.get("schema"), list):
                    contract_block["schema"] = new.pop("schema")
                if isinstance(new.get("quality"), dict):
                    contract_block["dq"] = new.pop("quality")
                if contract_block:
                    new["contract"] = contract_block
            else:
                # Ensure `contract.schema` exists if a top-level `schema` was
                # set alongside an existing contract block.
                if isinstance(new.get("schema"), list) and "schema" not in new["contract"]:
                    new["contract"]["schema"] = new.pop("schema")
                # Same for top-level quality: move it under contract.dq
                if isinstance(new.get("quality"), dict):
                    new["contract"].setdefault("dq", new.pop("quality"))
            # Top-level legacy ``quality`` block on the expose itself
            if "quality" in new:
                if "contract" not in new:
                    new["contract"] = {}
                new["contract"].setdefault("dq", new.pop("quality"))
            # ``schema[].nullable`` isn't a recognised field — translate
            # to ``required = not nullable`` and drop the legacy key.
            contract_block = new.get("contract") or {}
            schema_block = contract_block.get("schema")
            if isinstance(schema_block, list):
                clean_cols = []
                for col in schema_block:
                    if not isinstance(col, dict):
                        continue
                    nullable = col.pop("nullable", None)
                    if nullable is not None and "required" not in col:
                        col["required"] = not bool(nullable)
                    clean_cols.append(col)
                contract_block["schema"] = clean_cols
            # ``contract.dq`` legacy shape: list of rules. v0.7.3 expects
            # ``{rules: [...]}``. Drop legacy ``onFailure`` / ``rule``
            # nesting and just wrap so it validates.
            dq_block = contract_block.get("dq")
            if isinstance(dq_block, list):
                contract_block["dq"] = {"rules": _coerce_dq_rules(dq_block)}
            elif isinstance(dq_block, dict) and "rules" not in dq_block:
                contract_block["dq"] = {"rules": []}
            # Always render contract block back so the dq fix sticks.
            if contract_block:
                new["contract"] = contract_block
            # Synthesize a default binding when missing — required by schema.
            if not new.get("binding"):
                expose_id = new.get("exposeId", "main_output")
                new["binding"] = {
                    "platform": "local",
                    "format": "csv",
                    "location": {"path": f"runtime/out/{expose_id}.csv"},
                }
            new.setdefault("exposeId", "main_output")
            new.setdefault("kind", "table")
            # binding fixes: format='table' isn't valid; default by platform
            binding = new.get("binding")
            if isinstance(binding, dict):
                # Move dataset/table/database/schema-style keys into a
                # provider-shaped location dict so the schema's binding
                # validator sees ``platform`` + ``location``.
                platform = binding.get("platform", "local")
                location = binding.get("location") or {}
                fmt = binding.get("format", "")
                # Drop legacy direct keys
                for legacy in ("dataset", "table", "database", "schema", "key"):
                    if legacy in binding:
                        location[legacy] = binding.pop(legacy)
                # Coerce format to a v0.7.3 enum value
                _FORMAT_FALLBACK = {
                    "table": "csv",
                    "stream": "kafka_topic",
                    "topic": "kafka_topic",
                    "kafka": "kafka_topic",
                    "pubsub": "pubsub_topic",
                    "api": "http_api",
                    "rest": "http_api",
                    "parquet": "parquet",
                    "csv": "csv",
                    "json": "json",
                    "delta": "delta_table",
                    "delta_table": "delta_table",
                    "bigquery_table": "bigquery_table",
                    "snowflake_table": "snowflake_table",
                    "gcs_file": "gcs_file",
                    "s3_file": "s3_file",
                    "http_api": "http_api",
                    "grpc_api": "grpc_api",
                    "kafka_topic": "kafka_topic",
                    "pubsub_topic": "pubsub_topic",
                }
                fmt = _FORMAT_FALLBACK.get(fmt.lower(), "")
                if not fmt and platform == "snowflake":
                    fmt = "snowflake_table"
                elif not fmt and platform == "gcp":
                    fmt = "bigquery_table"
                elif not fmt:
                    fmt = "csv"
                if not location:
                    location = {"path": "runtime/out/main_output.csv"}
                new["binding"] = {
                    "platform": platform,
                    "format": fmt,
                    "location": location,
                }
            new_exposes.append(new)
        out["exposes"] = new_exposes

    return out


# ── Template-mode imports ────────────────────────────────────────
