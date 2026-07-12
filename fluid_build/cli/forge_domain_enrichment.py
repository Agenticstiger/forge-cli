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

# A domain name is used to build ``<dir>/<domain>.yaml`` paths. Restrict it to
# a simple slug before it can drive the per-domain ``system_prompt_fragments``
# override — defence in depth against a ``../``-style traversal reaching a
# fragment block in an out-of-tree YAML. Matches the profile-name guard in
# ``forge_copilot_prompts._PROFILE_NAME_RE``.
_SAFE_DOMAIN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


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

    Side effect: activates (or clears) the domain's optional
    ``system_prompt_fragments`` overlay via
    ``forge_copilot_prompts.set_domain_prompt_fragments`` so those fragments
    override the matching ``_defaults/`` block in ``build_system_prompt`` while
    this domain is active. The overlay is reset on entry so a fragment-less or
    failed enrichment can't leak a prior domain's fragments into this run.
    """
    from fluid_build.cli.forge_copilot_prompts import set_domain_prompt_fragments

    # Reset first: a previous run (or a failed load below) must not leave a
    # stale domain-fragment overlay active.
    set_domain_prompt_fragments(None, None)

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

    # Standards-modeling harmonisation: surface the domain's canonical model and
    # supporting standards from the SAME shared registry the copilot taxonomy
    # and the declarative domain agent read, and seed them onto the context
    # (without clobbering an explicit user choice) so the generative prompt and
    # the declarative recommendation stay aligned. Single source of truth:
    # ``forge_standards_modeling.domain_standard_defaults`` (reads the agent
    # spec's ``suggestion_defaults``).
    from fluid_build.cli import forge_standards_modeling as standards

    modeling_defaults = standards.domain_standard_defaults(domain)
    canonical_model = modeling_defaults["canonical_model"]
    supporting_standards = list(modeling_defaults["supporting_standards"])
    if canonical_model:
        expertise["canonical_model"] = canonical_model
        if not context.get("canonical_model"):
            context["canonical_model"] = canonical_model
    if supporting_standards:
        expertise["supporting_standards"] = supporting_standards
        existing = context.get("supporting_standards")
        if isinstance(existing, list):
            base = list(existing)
        elif existing:
            base = [str(existing)]
        else:
            base = []
        seen: set = set()
        merged: List[str] = []
        for item in base + supporting_standards:  # user's picks first
            if item not in seen:
                seen.add(item)
                merged.append(item)
        context["supporting_standards"] = merged

    if spec.next_step_tips:
        expertise["next_step_tips"] = spec.next_step_tips

    # Extract domain questions for the LLM to use during interview
    if spec.questions:
        expertise["domain_questions"] = [
            {"key": q.get("key", ""), "question": q.get("question", "")} for q in spec.questions[:5]
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

            # Per-domain system-prompt fragments override the matching
            # ``_defaults/*.yaml`` block in ``build_system_prompt`` while this
            # domain is active. Only activate for a safe domain slug so a
            # ``../``-style name can't surface fragments from an out-of-tree
            # YAML. ``set_domain_prompt_fragments`` further filters to
            # string→string entries and never touches the filesystem.
            fragments = raw.get("system_prompt_fragments")
            if isinstance(fragments, dict) and _SAFE_DOMAIN_RE.match(str(domain)):
                activated = set_domain_prompt_fragments(domain, fragments)
                if activated:
                    LOG.debug(
                        "Activated %d system_prompt_fragments for %s",
                        len(fragments),
                        domain,
                    )
    except (yaml.YAMLError, OSError):
        pass

    # Load per-technique modeling guidance when the interview has
    # settled on a technique.  The file ships two top-level keys
    # (``data_vault_2`` / ``dimensional``); we surface the matching
    # block under ``domain_expertise.modeling_technique_guidance`` so
    # ``build_user_prompt`` forwards it into the prompt automatically.
    technique = context.get("data_modeling_technique")
    if technique:
        try:
            from fluid_build.cli.forge_agent_specs import AGENT_SPECS_DIR as _AGENT_SPECS_DIR

            mt_path = _AGENT_SPECS_DIR / "modeling_techniques.yaml"
            if mt_path.exists():
                mt_raw = yaml.safe_load(mt_path.read_text(encoding="utf-8")) or {}
                block = mt_raw.get(technique)
                if isinstance(block, dict):
                    expertise.setdefault("modeling_technique_guidance", {})[technique] = block
                    LOG.debug("Loaded modeling technique guidance for %s", technique)
        except (yaml.YAMLError, OSError) as exc:
            LOG.debug("modeling_technique_load_failed: %s", exc)

    context["domain_expertise"] = expertise
    LOG.info("Enriched context with %s domain expertise", domain)
    return context
