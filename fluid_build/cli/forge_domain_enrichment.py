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

"""Domain auto-detection and context enrichment for the Forge Copilot.

When the copilot interview reveals a recognised industry domain the YAML
specs in ``agent_specs/`` are loaded transparently and injected into the
generation context.  The user never picks a "domain agent" -- it just
happens.

Domain keywords are maintained in ``agent_specs/domain_keywords.yaml``
so domain experts can update them without touching Python code.
"""

from __future__ import annotations

__all__ = [
    "detect_domain",
    "enrich_context_with_domain",
]

import logging
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

LOG = logging.getLogger("fluid.cli.forge.domain")

_KEYWORDS_PATH = Path(__file__).with_name("agent_specs") / "domain_keywords.yaml"


@lru_cache(maxsize=1)
def _load_domain_keywords() -> Tuple[Dict[str, List[str]], int]:
    """Load domain keywords from the built-in file + user agent specs.

    User-defined agents with a ``keywords`` field in their YAML spec
    are automatically included in the keyword map, enabling transparent
    auto-detection for custom domain agents.

    Returns ``(domain_keywords_dict, min_hits)`` or falls back to empty
    defaults if the file cannot be read.
    """
    try:
        raw = yaml.safe_load(_KEYWORDS_PATH.read_text(encoding="utf-8"))
        domains = raw.get("domains") or {}
        min_hits = int(raw.get("min_keyword_hits", 2))

        # Merge keywords from user-defined agent specs.
        try:
            from fluid_build.cli.forge_agent_specs import discover_all_agent_specs

            for name, spec in discover_all_agent_specs().items():
                if spec.keywords and name not in domains:
                    domains[name] = spec.keywords
        except Exception:  # noqa: BLE001
            pass

        LOG.debug(
            "Loaded domain keywords: %d domains, min_hits=%d",
            len(domains),
            min_hits,
        )
        return domains, min_hits
    except (FileNotFoundError, yaml.YAMLError, TypeError, ValueError) as exc:
        LOG.warning("Could not load domain keywords from %s: %s", _KEYWORDS_PATH, exc)
        return {}, 2


def detect_domain(context: Dict[str, Any]) -> Optional[str]:
    """Infer an industry domain from the copilot interview context.

    Scans ``project_goal``, ``data_sources``, ``description``, ``use_case``,
    and ``domain`` fields for domain keywords defined in
    ``agent_specs/domain_keywords.yaml``.

    Returns the domain name (``"finance"``, ``"healthcare"``, etc.) when at
    least *min_keyword_hits* keywords match, otherwise ``None``.
    """
    keywords_map, min_hits = _load_domain_keywords()
    if not keywords_map:
        return None

    # Build a single search string from relevant context fields,
    # filtering out None / empty values to avoid "None" literals.
    parts: List[str] = []
    for field in ("project_goal", "data_sources", "description", "use_case", "domain"):
        value = context.get(field)
        if value:
            parts.append(str(value))
    text = " ".join(parts).lower()

    if not text.strip():
        return None

    scores: Dict[str, int] = {}
    for domain, keywords in keywords_map.items():
        hits = sum(1 for kw in keywords if re.search(rf"\b{re.escape(kw)}\b", text))
        if hits >= min_hits:
            scores[domain] = hits

    if not scores:
        LOG.debug("No domain detected (threshold=%d)", min_hits)
        return None

    best = max(scores, key=scores.get)  # type: ignore[arg-type]
    LOG.info("Domain auto-detected: %s (score=%d, threshold=%d)", best, scores[best], min_hits)
    return best


def enrich_context_with_domain(
    context: Dict[str, Any],
    domain: str,
) -> Dict[str, Any]:
    """Inject domain expertise from the YAML spec into the copilot context.

    Loads the domain's ``AgentSpec`` and merges its ``suggestion_defaults``
    (architecture suggestions, best practices, security requirements) plus
    ``description`` into the context under the ``domain_expertise`` key.

    If the spec cannot be loaded the context is returned unchanged.
    """
    try:
        from fluid_build.cli.forge_agent_specs import AgentSpecError, load_user_or_builtin_spec

        spec = load_user_or_builtin_spec(domain)
    except (AgentSpecError, FileNotFoundError, ImportError, ValueError) as exc:
        LOG.warning("Could not load domain spec for %r: %s", domain, exc)
        return context

    expertise: Dict[str, Any] = {
        "domain": spec.domain,
        "description": spec.description,
    }

    defaults = spec.suggestion_defaults or {}
    for key in (
        "architecture_suggestions",
        "best_practices",
        "security_requirements",
        "technology_stack",
        "recommended_patterns",
    ):
        value = defaults.get(key)
        if value:
            expertise[key] = value

    if spec.next_step_tips:
        expertise["next_step_tips"] = spec.next_step_tips

    # Extract domain questions for the LLM to use during interview
    if spec.questions:
        expertise["domain_questions"] = [
            {"key": q.get("key", ""), "question": q.get("question", "")}
            for q in spec.questions[:5]
        ]

    # Load data_modeling_standards directly from YAML (not in AgentSpec dataclass).
    # Check user directories first, then fall back to built-in.
    try:
        from fluid_build.cli.forge_agent_specs import AGENT_SPECS_DIR, _user_agent_dirs

        spec_path = None
        for user_dir in _user_agent_dirs():
            candidate = user_dir / f"{domain}.yaml"
            if candidate.exists():
                spec_path = candidate
                break
        if spec_path is None:
            spec_path = AGENT_SPECS_DIR / f"{domain}.yaml"
        if spec_path.exists():
            raw = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
            modeling = raw.get("data_modeling_standards")
            if modeling and isinstance(modeling, dict):
                expertise["data_modeling_standards"] = modeling
                LOG.debug("Loaded data modeling standards for %s", domain)
    except (yaml.YAMLError, OSError):
        pass

    context["domain_expertise"] = expertise
    LOG.info("Enriched context with %s domain expertise", domain)
    return context
