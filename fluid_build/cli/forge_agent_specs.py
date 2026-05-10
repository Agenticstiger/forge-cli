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

"""Built-in declarative specs for Forge domain agents."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Mapping

import yaml

from fluid_build.cli.forge_dialogs import normalize_prompt_choices

AGENT_SPECS_DIR = Path(__file__).with_name("agent_specs")
ALLOWED_QUESTION_TYPES = {"text", "choice"}
ALLOWED_CONDITION_GROUPS = {"all", "any"}
ALLOWED_ACTIONS = {"set", "append_unique"}


class AgentSpecError(ValueError):
    """Raised when a built-in declarative agent spec is invalid."""


@dataclass(frozen=True)
class AgentSpec:
    """Validated domain-agent spec (built-in or user-defined)."""

    name: str
    domain: str
    description: str
    questions: List[Dict[str, Any]]
    resolver_defaults: Dict[str, Any] = field(default_factory=dict)
    suggestion_defaults: Dict[str, Any] = field(default_factory=dict)
    rules: List[Dict[str, Any]] = field(default_factory=list)
    next_step_tips: List[str] = field(default_factory=list)
    conditional_next_step_tips: List[Dict[str, Any]] = field(default_factory=list)
    keywords: List[str] = field(default_factory=list)
    # Phase 1.3: Data Mesh product types (SDP / ADP / CDP) this agent
    # is willing to support. Empty / missing means "all three" (the
    # interview shows the full picker). Specs can narrow to a subset
    # when the domain only makes sense for some types — e.g. a
    # streaming-CDC agent might list only ["SDP"]. Validated against
    # the canonical PRODUCT_TYPES registry on load.
    supported_data_product_types: List[str] = field(default_factory=list)


def _require_mapping(value: Any, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise AgentSpecError(f"{label} must be a mapping.")
    return value


def _normalize_questions(raw_questions: Any, *, spec_name: str) -> List[Dict[str, Any]]:
    if not isinstance(raw_questions, list) or not raw_questions:
        raise AgentSpecError(f"{spec_name}: questions must be a non-empty list.")

    normalized: List[Dict[str, Any]] = []
    for index, raw_question in enumerate(raw_questions):
        question = _require_mapping(raw_question, label=f"{spec_name}: question[{index}]")
        key = str(question.get("key") or "").strip()
        prompt = str(question.get("question") or "").strip()
        question_type = str(question.get("type") or "text").strip().lower()
        if not key or not prompt:
            raise AgentSpecError(f"{spec_name}: each question needs key and question text.")
        if question_type not in ALLOWED_QUESTION_TYPES:
            raise AgentSpecError(
                f"{spec_name}: question '{key}' has unsupported type '{question_type}'."
            )

        normalized_question: Dict[str, Any] = {
            "key": key,
            "question": prompt,
            "type": question_type,
            "required": bool(question.get("required", False)),
        }
        if "default" in question:
            normalized_question["default"] = question.get("default")
        if question_type == "choice":
            choices = normalize_prompt_choices(list(question.get("choices") or []))
            if not choices:
                raise AgentSpecError(f"{spec_name}: choice question '{key}' needs choices.")
            normalized_question["choices"] = choices
        normalized.append(normalized_question)
    return normalized


def _normalize_condition(raw_condition: Any, *, spec_name: str, label: str) -> Dict[str, Any]:
    condition = _require_mapping(raw_condition, label=f"{spec_name}: {label}")
    groups = [group for group in ALLOWED_CONDITION_GROUPS if group in condition]
    if len(groups) != 1:
        raise AgentSpecError(
            f"{spec_name}: {label} must contain exactly one of {sorted(ALLOWED_CONDITION_GROUPS)}."
        )
    group = groups[0]
    clauses = condition.get(group)
    if not isinstance(clauses, list) or not clauses:
        raise AgentSpecError(f"{spec_name}: {label}.{group} must be a non-empty list.")

    normalized_clauses: List[Dict[str, Any]] = []
    for clause_index, raw_clause in enumerate(clauses):
        clause = _require_mapping(raw_clause, label=f"{spec_name}: {label}.{group}[{clause_index}]")
        field_name = str(clause.get("field") or "").strip()
        operators = [op for op in ("equals", "in") if op in clause]
        if not field_name or len(operators) != 1:
            raise AgentSpecError(
                f"{spec_name}: each {label} clause must have a field and exactly one of equals/in."
            )
        operator = operators[0]
        normalized_clause: Dict[str, Any] = {"field": field_name}
        if operator == "equals":
            normalized_clause["equals"] = clause.get("equals")
        else:
            values = clause.get("in")
            if not isinstance(values, list) or not values:
                raise AgentSpecError(
                    f"{spec_name}: {label} clause for '{field_name}' must use a non-empty 'in' list."
                )
            normalized_clause["in"] = list(values)
        normalized_clauses.append(normalized_clause)

    return {group: normalized_clauses}


def _normalize_actions(raw_actions: Any, *, spec_name: str, label: str) -> List[Dict[str, Any]]:
    if not isinstance(raw_actions, list) or not raw_actions:
        raise AgentSpecError(f"{spec_name}: {label} must have a non-empty actions list.")

    normalized: List[Dict[str, Any]] = []
    for index, raw_action in enumerate(raw_actions):
        action = _require_mapping(raw_action, label=f"{spec_name}: {label}.actions[{index}]")
        op = str(action.get("op") or "").strip()
        path = str(action.get("path") or "").strip()
        if op not in ALLOWED_ACTIONS or not path or "value" not in action:
            raise AgentSpecError(
                f"{spec_name}: each action must include op/path/value with op in {sorted(ALLOWED_ACTIONS)}."
            )
        normalized.append({"op": op, "path": path, "value": action.get("value")})
    return normalized


def _normalize_rules(raw_rules: Any, *, spec_name: str) -> List[Dict[str, Any]]:
    if raw_rules in (None, []):
        return []
    if not isinstance(raw_rules, list):
        raise AgentSpecError(f"{spec_name}: rules must be a list.")

    normalized: List[Dict[str, Any]] = []
    for index, raw_rule in enumerate(raw_rules):
        rule = _require_mapping(raw_rule, label=f"{spec_name}: rule[{index}]")
        normalized.append(
            {
                "when": _normalize_condition(
                    rule.get("when"), spec_name=spec_name, label=f"rule[{index}].when"
                ),
                "actions": _normalize_actions(
                    rule.get("actions"), spec_name=spec_name, label=f"rule[{index}]"
                ),
            }
        )
    return normalized


def _normalize_conditional_tips(raw_tips: Any, *, spec_name: str) -> List[Dict[str, Any]]:
    if raw_tips in (None, []):
        return []
    if not isinstance(raw_tips, list):
        raise AgentSpecError(f"{spec_name}: conditional_next_step_tips must be a list.")

    normalized: List[Dict[str, Any]] = []
    for index, raw_tip in enumerate(raw_tips):
        tip = _require_mapping(raw_tip, label=f"{spec_name}: conditional_next_step_tips[{index}]")
        tips = tip.get("tips")
        if not isinstance(tips, list) or not tips:
            raise AgentSpecError(
                f"{spec_name}: conditional_next_step_tips[{index}] must include a non-empty tips list."
            )
        normalized.append(
            {
                "when": _normalize_condition(
                    tip.get("when"),
                    spec_name=spec_name,
                    label=f"conditional_next_step_tips[{index}].when",
                ),
                "tips": [str(item).strip() for item in tips if str(item or "").strip()],
            }
        )
    return normalized


def _normalize_suggestion_defaults(raw_defaults: Any, *, spec_name: str) -> Dict[str, Any]:
    defaults = _require_mapping(raw_defaults, label=f"{spec_name}: suggestion_defaults")
    if "recommended_template" not in defaults or "recommended_provider" not in defaults:
        raise AgentSpecError(
            f"{spec_name}: suggestion_defaults must include recommended_template and recommended_provider."
        )
    return dict(defaults)


def _normalize_resolver_defaults(raw_defaults: Any, *, spec_name: str) -> Dict[str, Any]:
    if raw_defaults in (None, {}):
        return {}
    defaults = _require_mapping(raw_defaults, label=f"{spec_name}: resolver_defaults")
    return dict(defaults)


def load_agent_spec_from_path(spec_path: Path) -> AgentSpec:
    """Load and validate an agent spec from a YAML file path."""
    spec_path = spec_path.resolve()
    spec_name = spec_path.stem
    if not spec_path.exists():
        raise AgentSpecError(f"Agent spec '{spec_name}' was not found at {spec_path}.")

    raw_payload = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    payload = _require_mapping(raw_payload, label=f"{spec_name}: root")

    name = str(payload.get("name") or "").strip()
    domain = str(payload.get("domain") or "").strip()
    description = str(payload.get("description") or "").strip()
    if not name or not domain or not description:
        raise AgentSpecError(f"{spec_name}: name, domain, and description are required.")

    next_step_tips = [
        str(item).strip()
        for item in (payload.get("next_step_tips") or [])
        if str(item or "").strip()
    ]

    raw_keywords = payload.get("keywords") or []
    keywords = [
        str(kw).strip().lower()
        for kw in (raw_keywords if isinstance(raw_keywords, list) else [])
        if str(kw or "").strip()
    ]

    # Phase 1.3: parse + validate ``supported_data_product_types``.
    # Each entry must resolve through ``PRODUCT_TYPES`` so a typo in the
    # YAML fails loudly at load time instead of silently filtering the
    # picker to nothing. Empty / missing means "all types" (the
    # picker is unfiltered for that domain).
    supported_types = _normalize_supported_data_product_types(
        payload.get("supported_data_product_types"), spec_name=spec_name
    )

    return AgentSpec(
        name=name,
        domain=domain,
        description=description,
        questions=_normalize_questions(payload.get("questions"), spec_name=spec_name),
        resolver_defaults=_normalize_resolver_defaults(
            payload.get("resolver_defaults"), spec_name=spec_name
        ),
        suggestion_defaults=_normalize_suggestion_defaults(
            payload.get("suggestion_defaults"), spec_name=spec_name
        ),
        rules=_normalize_rules(payload.get("rules"), spec_name=spec_name),
        next_step_tips=next_step_tips,
        conditional_next_step_tips=_normalize_conditional_tips(
            payload.get("conditional_next_step_tips"), spec_name=spec_name
        ),
        keywords=keywords,
        supported_data_product_types=supported_types,
    )


def _normalize_supported_data_product_types(raw: Any, *, spec_name: str) -> List[str]:
    """Validate the optional ``supported_data_product_types`` field.

    Returns the canonical codes (SDP / ADP / CDP) so callers don't have
    to re-resolve aliases. Empty list (the default) signals "no filter"
    — the picker shows every type. Raises ``AgentSpecError`` on any
    unknown entry so a typo in the spec fails loudly.
    """
    if raw is None or raw == []:
        return []
    if not isinstance(raw, list):
        raise AgentSpecError(
            f"{spec_name}: supported_data_product_types must be a list of "
            "type codes (SDP / ADP / CDP) or aliases (Bronze / Silver / Gold)."
        )

    # Lazy-import the registry — keeps the agent-spec loader fast for
    # the hot path that doesn't touch the field, and avoids any risk
    # of an import cycle as the product_types module grows.
    from fluid_build.forge.product_types import get_product_type

    canonical: List[str] = []
    seen: set = set()
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise AgentSpecError(
                f"{spec_name}: supported_data_product_types entries must be non-empty strings."
            )
        pt = get_product_type(item)
        if pt is None:
            raise AgentSpecError(
                f"{spec_name}: '{item}' is not a known data product type. "
                "Allowed: SDP / ADP / CDP (or Bronze / Silver / Gold)."
            )
        if pt.code not in seen:
            canonical.append(pt.code)
            seen.add(pt.code)
    return canonical


@lru_cache(maxsize=None)
def load_builtin_agent_spec(spec_name: str) -> AgentSpec:
    """Load and cache a built-in domain-agent spec by name."""
    safe_name = str(spec_name or "").strip().lower()
    if not safe_name:
        raise AgentSpecError("Built-in agent spec name cannot be empty.")
    return load_agent_spec_from_path(AGENT_SPECS_DIR / f"{safe_name}.yaml")


USER_AGENTS_DIR_NAME = "agents"

# Files in agent_specs/ that are not actual agent specs.
_NON_SPEC_FILES = {"domain_keywords.yaml", "custom.yaml.template"}

_LOG = logging.getLogger("fluid.cli.forge.agent_specs")


def _user_agent_dirs() -> List[Path]:
    """Return user agent-spec directories in priority order (workspace → global)."""
    dirs: List[Path] = []
    local = Path.cwd() / ".fluid" / USER_AGENTS_DIR_NAME
    if local.is_dir():
        dirs.append(local)
    global_dir = Path.home() / ".fluid" / USER_AGENTS_DIR_NAME
    if global_dir.is_dir() and global_dir != local:
        dirs.append(global_dir)
    return dirs


def discover_all_agent_specs() -> Dict[str, "AgentSpec"]:
    """Discover all agent specs: user (workspace + global) + built-in.

    Returns ``{name: spec}`` where workspace specs shadow global specs
    and global specs shadow built-in specs with the same name.
    """
    specs: Dict[str, AgentSpec] = {}

    # Built-in agents (lowest priority).
    for yaml_path in sorted(AGENT_SPECS_DIR.glob("*.yaml")):
        if yaml_path.name in _NON_SPEC_FILES:
            continue
        try:
            spec = load_agent_spec_from_path(yaml_path)
            specs[spec.name] = spec
        except (AgentSpecError, Exception) as exc:  # noqa: BLE001
            _LOG.debug("Skipping invalid built-in spec %s: %s", yaml_path.name, exc)

    # User agents (higher priority — iterate in reverse so workspace wins).
    for user_dir in reversed(_user_agent_dirs()):
        for yaml_path in sorted(user_dir.glob("*.yaml")):
            if yaml_path.name in _NON_SPEC_FILES:
                continue
            try:
                spec = load_agent_spec_from_path(yaml_path)
                specs[spec.name] = spec
            except (AgentSpecError, Exception) as exc:  # noqa: BLE001
                _LOG.warning("Skipping invalid user agent spec %s: %s", yaml_path, exc)

    return specs


def load_user_or_builtin_spec(name: str) -> AgentSpec:
    """Load a spec by name: user directories first, then built-in."""
    safe_name = str(name or "").strip().lower()
    if not safe_name:
        raise AgentSpecError("Agent spec name cannot be empty.")

    # Check user directories in priority order.
    for user_dir in _user_agent_dirs():
        path = user_dir / f"{safe_name}.yaml"
        if path.exists():
            return load_agent_spec_from_path(path)

    # Fall back to built-in.
    return load_builtin_agent_spec(safe_name)


def scaffold_user_agent(name: str, target_dir: Path | None = None) -> Path:
    """Scaffold a new user agent spec from the built-in template.

    Returns the path to the created file.
    """
    safe_name = str(name or "").strip().lower().replace(" ", "-")
    dest_dir = (target_dir or Path.cwd()) / ".fluid" / USER_AGENTS_DIR_NAME
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{safe_name}.yaml"

    template = AGENT_SPECS_DIR / "custom.yaml.template"
    content = template.read_text(encoding="utf-8")
    content = content.replace("my-domain", safe_name)
    content = content.replace("My Domain", safe_name.replace("-", " ").title())
    dest.write_text(content, encoding="utf-8")
    return dest


__all__ = [
    "AGENT_SPECS_DIR",
    "AgentSpec",
    "AgentSpecError",
    "discover_all_agent_specs",
    "load_agent_spec_from_path",
    "load_builtin_agent_spec",
    "load_user_or_builtin_spec",
    "scaffold_user_agent",
]
