#!/usr/bin/env python3
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

"""Auto-update the LLM model catalog from Artificial Analysis API.

This script is designed to run in CI (weekly via GitHub Actions) or
manually by a maintainer.  It fetches the latest model data, picks
flagship/balanced/routing models per provider, and writes the updated
``llm_models.json`` to the repo.  If no changes are detected, the
script exits with code 0.  If the catalog changed, it exits with
code 0 and prints a summary — the CI workflow detects changes via
``git diff``.

Usage::

    python scripts/update_model_catalog.py [--dry-run]

Environment variables:

    ARTIFICIAL_ANALYSIS_API_KEY  — API key for artificialanalysis.ai
                                  (free tier, requires attribution)

When the API is unavailable, the script keeps the existing catalog
unchanged and logs a warning.

Data source: https://artificialanalysis.ai (attribution required).
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

CATALOG_PATH = Path(__file__).resolve().parent.parent / "fluid_build" / "cli" / "llm_models.json"

# Provider mapping: our provider names → Artificial Analysis creator slugs
PROVIDER_CREATORS = {
    "openai": "openai",
    "anthropic": "anthropic",
    "gemini": "google",
}

# Models we consider for flagship/balanced per provider.
# The API returns many models; we only track the ones our adapters support.
TRACKED_MODEL_PREFIXES = {
    # Generation-agnostic on purpose. This read ["gpt-4", "gpt-3.5", "o1",
    # "o3", "o4"], which matches nothing OpenAI currently ships -- the live
    # catalogue is gpt-5.x and gpt-6.x -- so the openai provider would have
    # been silently skipped while the job reported success. An enumeration of
    # model generations is a list that goes stale by design; a family prefix
    # does not.
    "openai": ["gpt-", "o1", "o3", "o4"],
    "anthropic": ["claude"],
    "gemini": ["gemini"],
}

#: Model families that share a provider prefix but are NOT callable through
#: that provider's hosted API, so the adapters cannot use them whatever they
#: score.
#:
#: `gpt-oss-*` is the concrete case and it is not hypothetical: it is
#: open-weights, priced at $0.037/M against gpt-5.6-sol's $2.00/M, and scores
#: 12.3 against 47.1. On "intelligence per dollar" that is an efficiency of 332
#: versus 24, so it wins `balanced` and `routing` by a factor of fourteen while
#: being four times less capable and unreachable by the OpenAI adapter. Cheap
#: and uncallable beats good and callable under a pure ratio, which is why this
#: list exists rather than a smarter formula.
EXCLUDED_MODEL_MARKERS = ("gpt-oss",)

# Capability defaults when the API doesn't provide them
DEFAULT_CAPABILITIES = {
    "openai": {"structured_output": True, "tool_use": True, "streaming": True},
    "anthropic": {"structured_output": True, "tool_use": True, "streaming": True},
    "gemini": {"structured_output": False, "tool_use": True, "streaming": True},
}


#: Where the model data comes from now, and why it moved.
#:
#: ``api.artificialanalysis.ai`` is GONE. Not unauthenticated -- undeployed:
#: every path returns Vercel's ``DEPLOYMENT_NOT_FOUND`` while the product site
#: itself serves 200. So the original fetch could not have worked with a key
#: either, which is why this job never once succeeded.
#:
#: OpenRouter republishes Artificial Analysis's own index under
#: ``benchmarks.artificial_analysis``, alongside pricing, from a public
#: endpoint that needs no key at all. Same upstream numbers, same attribution
#: owed, one less credential to hold.
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"

#: OpenRouter lists priced variants of one model as separate ids --
#: ``:batch`` at half price, ``:free`` at zero. They are the SAME model, so
#: leaving them in hands "best intelligence per dollar" to whichever variant is
#: cheapest and makes ``balanced`` a billing mode rather than a model choice.
_VARIANT_SUFFIXES = (":batch", ":free", ":extended", ":thinking", ":online")


def _normalise_openrouter(raw: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Reshape OpenRouter's payload into the record ``pick_*`` already reads.

    Adapting at this boundary rather than teaching the selection logic a second
    schema: the ranking rules are the part worth keeping stable, and they are
    unchanged by this move.
    """
    out: List[Dict[str, Any]] = []
    for m in raw:
        model_id = m.get("id") or ""
        if "/" not in model_id or model_id.endswith(_VARIANT_SUFFIXES):
            continue
        creator, _, slug = model_id.partition("/")
        if any(marker in slug for marker in EXCLUDED_MODEL_MARKERS):
            continue
        index = ((m.get("benchmarks") or {}).get("artificial_analysis") or {}).get(
            "intelligence_index"
        )
        if index is None:
            # No capability score means no basis for ranking. Dropping it is
            # honest; keeping it at 0 would quietly rank it last on merit.
            continue
        # OpenRouter prices PER TOKEN as a string; the selection logic works in
        # dollars per million.
        try:
            per_million = float((m.get("pricing") or {}).get("prompt") or 0) * 1_000_000
        except (TypeError, ValueError):
            continue
        out.append(
            {
                "creator": {"slug": creator},
                "api_model_id": slug,
                "slug": slug,
                "evaluations": {"intelligence_index": index},
                "pricing": {"input_per_million_tokens": per_million},
            }
        )
    return out


def fetch_models_from_api(api_key: Optional[str] = None) -> Optional[List[Dict[str, Any]]]:
    """Fetch model data. ``api_key`` is accepted and unused -- none is needed.

    Kept in the signature so the workflow's existing env wiring and any caller
    passing one keep working rather than raising on an unexpected argument.
    """
    try:
        import httpx
    except ImportError:
        print("httpx not installed — run: pip install httpx", file=sys.stderr)
        return None

    try:
        resp = httpx.get(OPENROUTER_MODELS_URL, timeout=30)
        resp.raise_for_status()
        models = _normalise_openrouter(resp.json().get("data", []))
    except Exception as exc:
        print(f"Warning: OpenRouter model API unavailable: {exc}", file=sys.stderr)
        return None

    if not models:
        # Reachable but useless: a 200 carrying nothing rankable must not be
        # mistaken for "no models changed".
        print(
            "Warning: OpenRouter returned no models carrying an intelligence index.",
            file=sys.stderr,
        )
        return None
    return models


def pick_flagship_and_balanced(
    models: List[Dict[str, Any]], provider: str
) -> tuple[Optional[str], Optional[str]]:
    """Pick the flagship (most capable) and balanced (best value) model.

    Uses the intelligence_index score for capability ranking and
    cost-efficiency for balanced selection.
    """
    prefixes = TRACKED_MODEL_PREFIXES.get(provider, [])
    creator = PROVIDER_CREATORS.get(provider, provider)

    candidates = []
    for m in models:
        model_creator = (m.get("creator", {}).get("slug") or "").lower()
        if model_creator != creator:
            continue
        model_id = m.get("api_model_id") or m.get("slug") or ""
        if not any(model_id.lower().startswith(p) for p in prefixes):
            continue
        intelligence = m.get("evaluations", {}).get("intelligence_index") or 0
        input_price = m.get("pricing", {}).get("input_per_million_tokens") or 999
        candidates.append(
            {
                "id": model_id,
                "intelligence": intelligence,
                "input_price": input_price,
                "efficiency": intelligence / max(input_price, 0.001),
            }
        )

    if not candidates:
        return None, None

    # Flagship: highest intelligence score
    candidates.sort(key=lambda c: c["intelligence"], reverse=True)
    flagship = candidates[0]["id"]

    # Balanced: best intelligence per dollar (exclude the flagship itself
    # so balanced is always different if possible)
    non_flagship = [c for c in candidates if c["id"] != flagship]
    if non_flagship:
        non_flagship.sort(key=lambda c: c["efficiency"], reverse=True)
        balanced = non_flagship[0]["id"]
    else:
        balanced = flagship

    return flagship, balanced


def build_catalog(api_models: Optional[List[Dict[str, Any]]]) -> Dict[str, Any]:
    """Build the v2 catalog from API data, or return the existing catalog
    if the API is unavailable."""
    existing = json.loads(CATALOG_PATH.read_text(encoding="utf-8")) if CATALOG_PATH.exists() else {}

    if not api_models:
        print("No API data available — keeping existing catalog.", file=sys.stderr)
        return existing

    providers_data = existing.get("providers", {})

    for provider in ("openai", "anthropic", "gemini"):
        flagship, balanced = pick_flagship_and_balanced(api_models, provider)
        entry = providers_data.get(provider, {})

        if flagship:
            entry["flagship"] = flagship
            entry["default"] = flagship
        if balanced:
            entry["balanced"] = balanced
            # Routing model = balanced (cheap/fast) when different from flagship
            if balanced != entry.get("flagship"):
                entry["routing"] = balanced

        # Preserve existing models list + capabilities; API doesn't
        # provide per-model capability flags reliably, so we keep the
        # human-curated entries and only update flagship/balanced/routing.
        providers_data[provider] = entry

    # Ollama is local — no API data, keep as-is
    if "ollama" not in providers_data:
        providers_data["ollama"] = {
            "flagship": "llama3.1",
            "balanced": "llama3.1",
            "routing": "llama3.1:8b",
            "default": "llama3.1",
            "models": [],
        }

    return {
        "schema_version": 2,
        "updated_at": date.today().isoformat(),
        "source": "https://artificialanalysis.ai",
        "default_provider": existing.get("default_provider", "gemini"),
        "providers": providers_data,
    }


def main():
    dry_run = "--dry-run" in sys.argv
    api_key = os.environ.get("ARTIFICIAL_ANALYSIS_API_KEY")

    print("Fetching model data from Artificial Analysis API...")
    api_models = fetch_models_from_api(api_key)

    catalog = build_catalog(api_models)

    if dry_run:
        print(json.dumps(catalog, indent=2))
        return

    new_content = json.dumps(catalog, indent=2) + "\n"
    old_content = CATALOG_PATH.read_text(encoding="utf-8") if CATALOG_PATH.exists() else ""

    if new_content == old_content:
        print("No changes detected in model catalog.")
    else:
        CATALOG_PATH.write_text(new_content, encoding="utf-8")
        print(f"Updated {CATALOG_PATH}")
        # Show what changed
        for provider in ("openai", "anthropic", "gemini"):
            entry = catalog.get("providers", {}).get(provider, {})
            print(
                f"  {provider}: flagship={entry.get('flagship')}, balanced={entry.get('balanced')}"
            )


if __name__ == "__main__":
    main()
