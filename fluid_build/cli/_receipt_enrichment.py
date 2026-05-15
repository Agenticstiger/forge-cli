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

"""Pre-write receipt enrichment for the forge preview panel.

A single pure transform — ``_populate_richer_receipt`` — that fills
every preview-panel field from the interview state, the agent loop,
and the final contract. No provider state, no I/O. ``forge_modes.py``
re-imports it at module top so test patches that target
``fluid_build.cli.forge_modes._populate_richer_receipt`` keep
resolving via the namespace.
"""

from __future__ import annotations

from typing import Any, Mapping


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

        # Tools called by the agent loop
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
