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

"""AI-ready metadata-enforcement core for the built-in ``ai_ready`` agent.

The ``ai_ready`` Forge agent authors / annotates a data product so a
downstream **AI or vector output port** (LLM tool, RAG retriever, agent
consumer) can consume it *safely and cleanly*. Where the domain agents
(finance / healthcare / …) shape the interview and the LLM prompt, this
module ships the deterministic **enforcement pass** that guarantees the
AI-governance surface is present on the emitted contract — no LLM, no new
dependency, fully idempotent.

What "AI-ready" means concretely, and where each fact lands in the FLUID
contract schema (all pre-existing v0.7.x fields — nothing invented):

* **AI/LLM usage governance** — every output port gets an
  ``exposes[].policy.agentPolicy`` block (``allowedModels`` /
  ``allowedUseCases`` / ``deniedUseCases`` / ``canStore`` / ``canReason`` /
  ``retentionPolicy`` / ``auditRequired``). This is the schema's
  AI-governance surface (``$defs.agentPolicy``) — the same "reporting yes,
  training no" control the ODPS 4.0 "AI-Ready" spec and Atlan / Alation's
  AI-model-governance guidance describe.
* **PII / sensitivity flags for safe AI access** — columns are tagged via
  the existing name-based classifier (:mod:`fluid_build.copilot.pii`), which
  writes ``semanticType`` / ``sensitivity`` / ``pii-*`` ``tags``. A port that
  carries any ``pii`` / ``phi`` / ``restricted`` column is treated as
  *sensitive*: training / fine-tuning are denied, storage is off, retention
  requires deletion.
* **Embedding-friendly column semantics (vector / semantic hints)** — a
  text column with a description and no sensitive signal is labelled
  ``ai-embeddable: "true"`` (schema-valid ``column.labels``) and surfaced in
  the report so a vector output port knows which columns to embed. PII text
  is deliberately excluded — you don't embed someone's email.
* **Description completeness** — embedding quality is a direct function of
  description quality (column name + description + values is the standard
  column-embedding input). Ports / columns lacking a description are
  reported; ``strict=True`` turns the report into a hard error.

Design mirrors the in-repo prior art :mod:`fluid_build.copilot.pii`:

* deterministic, name/shape-based — **no value scanning, no LLM call**;
* **conservative merge** — never stomps an operator-set ``agentPolicy``
  value or an existing ``sensitivity`` / ``semanticType`` (denied-use lists
  are unioned, everything else is fill-if-absent);
* **fail-open kill-switch** — ``FLUID_AI_READY=0`` returns the contract
  unchanged with an ``enabled=False`` report;
* **idempotent** — a second pass over an already-enforced contract is a
  no-op.

The core is a pure function so every authoring path that lands on the same
contract produces byte-identical output; the agent (:class:`AiReadyAgent` in
``fluid_build.cli.forge_agents``) is a thin registration + delegation shell.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from fluid_build.copilot.pii import classify_contract_schemas

LOG = logging.getLogger("fluid.copilot.ai_ready")

__all__ = [
    "AI_READY_ENV",
    "AiReadyError",
    "AiReadyReport",
    "ai_ready_enabled",
    "enforce_ai_ready",
    "DEFAULT_AI_USE_CASES",
    "SENSITIVE_DENIED_USE_CASES",
]

# Kill-switch env var (mirrors ``FLUID_COPILOT_PII_CLASSIFIER`` /
# ``FLUID_COPILOT_ENRICHMENT``). Set to 0/false/no/off to disable the pass.
AI_READY_ENV = "FLUID_AI_READY"

# Sensitivity levels that make an output port "sensitive" for AI access.
# Drawn from the schema's ``sensitivityLevel`` enum.
_SENSITIVE_LEVELS = frozenset({"pii", "phi", "restricted"})

# Canonical text-ish column types worth embedding. Bounded to the schema's
# string family so numerics / dates / structs are never mislabelled as
# embeddable free text.
_TEXT_TYPES = frozenset(
    {"string", "text", "varchar", "varchar2", "nvarchar", "char", "nchar", "character", "clob"}
)

# Safe default read use-cases granted to a non-sensitive AI output port.
# Every value is a member of the schema's ``agentPolicy.allowedUseCases``
# enum. Deliberately excludes ``training`` / ``fine_tuning`` — opt-in only.
DEFAULT_AI_USE_CASES = ("inference", "summarization", "qa", "rag", "search", "analysis")

# Use-cases denied on a sensitive port ("reporting yes, training no").
SENSITIVE_DENIED_USE_CASES = ("training", "fine_tuning")

# Model-name shape accepted by ``agentPolicy.allowedModels`` (schema pattern).
_MODEL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-_.]*[a-z0-9]$|^[a-z0-9]$")

# Columns that read as identifiers / keys are excluded from embedding hints —
# an ID carries no semantic text worth embedding. Mirrors the identifier check
# in ``exporters/dbt_tests.py`` (``semanticType in {identifier, primary_key}``).
_ID_SEMANTIC_TYPES = frozenset({"identifier", "primary_key", "id", "key", "uuid", "guid"})
_ID_NAME_RE = re.compile(r"(^|_)(id|uuid|guid|key|pk|sk)$")

_TRUTHY_KILL = {"0", "false", "no", "off"}


class AiReadyError(ValueError):
    """Raised in ``strict`` mode when a contract cannot be made AI-ready."""


@dataclass
class AiReadyReport:
    """Structured outcome of an :func:`enforce_ai_ready` pass.

    Suitable for persistence under
    ``.fluid/agents/<run-id>/enrichment/ai_ready.json`` and for judge /
    telemetry consumption. ``is_ai_ready`` is the single-glance verdict.
    """

    enabled: bool = True
    exposes_annotated: List[str] = field(default_factory=list)
    sensitive_exposes: List[str] = field(default_factory=list)
    embeddable_columns: List[str] = field(default_factory=list)
    missing_descriptions: List[str] = field(default_factory=list)
    allowed_models: List[str] = field(default_factory=list)
    pii_summary: Dict[str, int] = field(default_factory=dict)

    @property
    def pii_columns(self) -> int:
        """Total number of PII-classified columns across all ports."""
        return sum(self.pii_summary.values())

    @property
    def is_ai_ready(self) -> bool:
        """True when the pass ran, annotated at least one port, and every
        port / column carries a description."""
        return bool(self.enabled and self.exposes_annotated and not self.missing_descriptions)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["pii_columns"] = self.pii_columns
        d["is_ai_ready"] = self.is_ai_ready
        return d


def ai_ready_enabled() -> bool:
    """Kill-switch for the AI-ready enforcement pass (``FLUID_AI_READY``)."""
    return os.environ.get(AI_READY_ENV, "1").strip().lower() not in _TRUTHY_KILL


def _has_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _normalize_allowed_models(models: Optional[List[str]]) -> List[str]:
    """Coerce free-form model names to the schema's ``allowedModels`` shape.

    ``"GPT-4"`` / ``"Claude 3 Opus"`` → ``"gpt-4"`` / ``"claude-3-opus"``.
    Entries that still can't be coerced to the schema pattern are dropped
    (logged at DEBUG) rather than emitting an invalid contract.
    """
    if not models:
        return []
    out: List[str] = []
    for raw in models:
        if not isinstance(raw, str):
            continue
        token = re.sub(r"\s+", "-", raw.strip().lower())
        token = re.sub(r"-{2,}", "-", token).strip("-")
        if not token:
            continue
        if not _MODEL_NAME_RE.match(token):
            LOG.debug("ai_ready: dropping non-conforming model name %r", raw)
            continue
        if token not in out:
            out.append(token)
    return out


def _column_sensitivity(col: Dict[str, Any]) -> str:
    """Return the column's effective sensitivity signal (may be empty)."""
    sens = str(col.get("sensitivity") or "").strip().lower()
    if sens in _SENSITIVE_LEVELS:
        return sens
    # A ``pii-*`` tag is an equally strong signal even without ``sensitivity``.
    for tag in col.get("tags") or []:
        if isinstance(tag, str) and tag.startswith("pii-"):
            return "pii"
    return sens


def _schema_of(expose: Dict[str, Any]) -> List[Dict[str, Any]]:
    contract = expose.get("contract")
    if not isinstance(contract, dict):
        return []
    schema = contract.get("schema")
    return schema if isinstance(schema, list) else []


def _expose_is_sensitive(expose: Dict[str, Any]) -> bool:
    for col in _schema_of(expose):
        if isinstance(col, dict) and _column_sensitivity(col) in _SENSITIVE_LEVELS:
            return True
    return False


def _union_use_cases(existing: Any, additions: tuple) -> List[str]:
    merged: List[str] = list(existing) if isinstance(existing, list) else []
    for item in additions:
        if item not in merged:
            merged.append(item)
    return merged


def _enforce_agent_policy(
    expose: Dict[str, Any],
    *,
    sensitive: bool,
    allowed_models: List[str],
) -> None:
    """Fill / extend ``expose.policy.agentPolicy`` conservatively.

    Existing operator-set scalar values are preserved; denied-use lists are
    unioned; only absent keys are populated. This keeps a second pass a
    no-op and never overrides a human decision.
    """
    policy = expose.setdefault("policy", {})
    if not isinstance(policy, dict):
        policy = {}
        expose["policy"] = policy
    ap = policy.setdefault("agentPolicy", {})
    if not isinstance(ap, dict):
        ap = {}
        policy["agentPolicy"] = ap

    # Audit is always on for AI consumption.
    ap.setdefault("auditRequired", True)

    if allowed_models and not ap.get("allowedModels"):
        ap["allowedModels"] = list(allowed_models)

    if sensitive:
        # Reporting yes, training no.
        ap["deniedUseCases"] = _union_use_cases(
            ap.get("deniedUseCases"), SENSITIVE_DENIED_USE_CASES
        )
        ap.setdefault("canStore", False)
        ap.setdefault("canReason", False)
        retention = ap.setdefault("retentionPolicy", {})
        if isinstance(retention, dict):
            retention.setdefault("maxRetentionDays", 0)
            retention.setdefault("requireDeletion", True)
        ap.setdefault(
            "purposeLimitation",
            "Sensitive data — read / retrieval / reporting only; model "
            "training and fine-tuning are prohibited.",
        )
        # Grant safe read use-cases minus the denied set.
        if not ap.get("allowedUseCases"):
            ap["allowedUseCases"] = [
                uc for uc in DEFAULT_AI_USE_CASES if uc not in SENSITIVE_DENIED_USE_CASES
            ]
    else:
        ap.setdefault("canStore", True)
        ap.setdefault("canReason", True)
        if not ap.get("allowedUseCases"):
            ap["allowedUseCases"] = list(DEFAULT_AI_USE_CASES)


def _looks_like_identifier(col: Dict[str, Any]) -> bool:
    """True when the column reads as an ID / key (not embeddable free text)."""
    if str(col.get("semanticType") or "").strip().lower() in _ID_SEMANTIC_TYPES:
        return True
    return bool(_ID_NAME_RE.search(str(col.get("name") or "").strip().lower()))


def _annotate_embeddable(
    expose_id: str,
    schema: List[Dict[str, Any]],
    report: AiReadyReport,
) -> None:
    """Label embedding-friendly text columns and record them in the report."""
    for col in schema:
        if not isinstance(col, dict):
            continue
        name = str(col.get("name") or "").strip()
        col_type = str(col.get("type") or "").split("(")[0].strip().lower()
        if col_type not in _TEXT_TYPES:
            continue
        if not _has_text(col.get("description")):
            continue
        if _column_sensitivity(col) in _SENSITIVE_LEVELS:
            # Never embed PII / PHI / restricted free text.
            continue
        if _looks_like_identifier(col):
            # IDs / keys carry no semantic text worth embedding.
            continue
        labels = col.setdefault("labels", {})
        if not isinstance(labels, dict):
            labels = {}
            col["labels"] = labels
        labels.setdefault("ai-embeddable", "true")
        ref = f"{expose_id}:{name}"
        if ref not in report.embeddable_columns:
            report.embeddable_columns.append(ref)


def _scan_descriptions(
    expose_id: str,
    expose: Dict[str, Any],
    schema: List[Dict[str, Any]],
    report: AiReadyReport,
) -> None:
    if not _has_text(expose.get("description")):
        report.missing_descriptions.append(f"{expose_id}: (port description)")
    for col in schema:
        if not isinstance(col, dict):
            continue
        if not _has_text(col.get("description")):
            report.missing_descriptions.append(f"{expose_id}:{col.get('name') or '?'}")


def enforce_ai_ready(
    contract: Dict[str, Any],
    *,
    overwrite: bool = False,
    strict: bool = False,
    allowed_models: Optional[List[str]] = None,
) -> AiReadyReport:
    """Annotate *contract* in place so it is safe for AI / vector consumption.

    Parameters
    ----------
    contract
        A FLUID contract dict (v0.7.x shape — ``exposes[].contract.schema``).
        Mutated in place.
    overwrite
        Forwarded to the PII classifier: when True, existing
        ``sensitivity`` / ``semanticType`` are recomputed. ``agentPolicy`` and
        embedding labels are *always* merged conservatively regardless.
    strict
        When True, raise :class:`AiReadyError` if any port or column lacks a
        description (embedding quality gate). Default False → report only.
    allowed_models
        Optional whitelist of AI models permitted to consume the data. Free
        text is normalised to the schema's model-name shape. When omitted the
        ``allowedModels`` key is left for the operator to set (empty = no AI
        access, the schema's conservative default).

    Returns
    -------
    AiReadyReport
        Structured summary of what was annotated.
    """
    report = AiReadyReport()

    if not ai_ready_enabled():
        report.enabled = False
        return report

    report.allowed_models = _normalize_allowed_models(allowed_models)

    if not isinstance(contract, dict):
        return report

    # 1) Column PII / sensitivity / semanticType — reuse the name-based
    #    classifier so the "safe AI access" flags are consistent with the
    #    rest of the enrichment stack.
    try:
        report.pii_summary = dict(
            classify_contract_schemas(contract, overwrite=overwrite).get("totals", {})
        )
    except Exception as exc:  # noqa: BLE001 — fail-open, annotation continues
        LOG.debug("ai_ready: PII classification skipped: %s", exc)

    exposes = contract.get("exposes")
    if not isinstance(exposes, list):
        exposes = []

    # 2) Per-port agentPolicy + embedding hints + description scan.
    for expose in exposes:
        if not isinstance(expose, dict):
            continue
        expose_id = str(expose.get("exposeId") or expose.get("id") or expose.get("name") or "port")
        schema = _schema_of(expose)

        sensitive = _expose_is_sensitive(expose)
        _enforce_agent_policy(expose, sensitive=sensitive, allowed_models=report.allowed_models)
        _annotate_embeddable(expose_id, schema, report)
        _scan_descriptions(expose_id, expose, schema, report)

        report.exposes_annotated.append(expose_id)
        if sensitive:
            report.sensitive_exposes.append(expose_id)

    # 3) Root discovery marker so marketplace / catalog can facet on it.
    if report.exposes_annotated:
        labels = contract.setdefault("labels", {})
        if isinstance(labels, dict):
            labels.setdefault("ai-ready", "true")

    if strict and report.missing_descriptions:
        raise AiReadyError(
            "Contract is not AI-ready: "
            f"{len(report.missing_descriptions)} description(s) missing — "
            f"{', '.join(report.missing_descriptions[:5])}"
            + (" …" if len(report.missing_descriptions) > 5 else "")
        )

    return report
