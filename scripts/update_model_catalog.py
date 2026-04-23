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
    "openai": ["gpt-4", "gpt-3.5", "o1", "o3", "o4"],
    "anthropic": ["claude"],
    "gemini": ["gemini"],
}

# Capability defaults when the API doesn't provide them
DEFAULT_CAPABILITIES = {
    "openai": {"structured_output": True, "tool_use": True, "streaming": True},
    "anthropic": {"structured_output": True, "tool_use": True, "streaming": True},
    "gemini": {"structured_output": False, "tool_use": True, "streaming": True},
}


def fetch_models_from_api(api_key: Optional[str] = None) -> Optional[List[Dict[str, Any]]]:
    """Fetch model data from the Artificial Analysis API."""
    try:
        import httpx
    except ImportError:
        print("httpx not installed — run: pip install httpx", file=sys.stderr)
        return None

    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        resp = httpx.get(
            "https://api.artificialanalysis.ai/v0/models",
            headers=headers,
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json().get("data", [])
    except Exception as exc:
        print(f"Warning: Artificial Analysis API unavailable: {exc}", file=sys.stderr)
        return None


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
        candidates.append({
            "id": model_id,
            "intelligence": intelligence,
            "input_price": input_price,
            "efficiency": intelligence / max(input_price, 0.001),
        })

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

    print(f"Fetching model data from Artificial Analysis API...")
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
            print(f"  {provider}: flagship={entry.get('flagship')}, balanced={entry.get('balanced')}")


if __name__ == "__main__":
    main()
