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

"""Mission spec — declarative goal + deterministic success criteria.

The YAML surface (see ``builtin/gdpr_clean.yaml`` for the canonical
example) follows the ``cli/agent_specs/*.yaml`` conventions: snake_case
keys, ``name``/``description`` at the root, and a loader that fails
loudly with a typed error (:class:`MissionSpecError`) on any shape
problem — a typo in a mission must never silently weaken its criteria.

Resolution order for ``resolve_mission_spec`` mirrors
``forge_agent_specs.load_user_or_builtin_spec``: workspace
(``.fluid/missions/``) → user-global (``~/.fluid/missions/``) →
built-in (``builtin/`` package data). Workspace specs are subject to
the direnv-style trust gate in :mod:`trust`; the loader itself only
parses and validates.

The ``predicate`` criterion's mini-language is deliberately **frozen**:
dotted paths, ``[*]`` array fan-out, and the ops
``{eq, ne, lt, lte, gt, gte, exists, contains}``. No filters, no
functions, no extensibility hooks — mini-languages never stay lite, so
this one is not allowed to grow (RFC-deep-agents.md).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

import yaml

BUILTIN_MISSIONS_DIR = Path(__file__).with_name("builtin")

USER_MISSIONS_DIR_NAME = "missions"

#: The v1 check types. ``judge`` / ``agent_policy`` / live-state ``verify``
#: are explicitly v2 (RFC-deep-agents.md, "v1 scope").
MISSION_CHECK_TYPES = ("validate", "ai_ready", "predicate")

#: Frozen predicate operators. Requests for more get pointed at v2's
#: plugin checks — this set does not grow.
PREDICATE_OPS = frozenset({"eq", "ne", "lt", "lte", "gt", "gte", "exists", "contains"})

GATE_MODES = frozenset({"ask", "deny"})

#: ``ai_ready`` criteria may only require these keys (frozen; both map onto
#: :class:`fluid_build.copilot.agents.ai_ready_agent.AiReadyReport` fields).
AI_READY_REQUIRE_KEYS = frozenset({"sensitive_exposes_annotated", "missing_descriptions"})

_ROOT_KEYS = frozenset(
    {"name", "description", "goal", "success_criteria", "budgets", "gates", "tools", "plan_hint"}
)
_CRITERION_KEYS = frozenset({"check", "advisory", "require", "path", "op", "value"})
_BUDGET_KEYS = frozenset({"max_usd", "max_iterations", "max_wall_seconds"})

#: One dotted-path segment: a key, optionally fanned out over an array.
_SEGMENT_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*)(\[\*\])?$")

_MISSION_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


class MissionSpecError(ValueError):
    """Raised when a mission spec fails to load or validate."""


@dataclass(frozen=True)
class CriterionSpec:
    """One validated entry of ``success_criteria``.

    ``value_provided`` disambiguates ``value: null`` /
    ``op: exists`` with no value from an explicit value — ``exists``
    defaults to requiring presence when no value is given.
    """

    check: str
    advisory: bool = False
    # ai_ready only
    require: Dict[str, Any] = field(default_factory=dict)
    # predicate only
    path: str = ""
    op: str = ""
    value: Any = None
    value_provided: bool = False

    def describe(self) -> str:
        """Short human-readable label for scorecard rendering."""
        if self.check == "predicate":
            if self.op == "exists":
                want = self.value if self.value_provided else True
                return f"{self.path} {'exists' if want else 'is absent'}"
            return f"{self.path} {self.op} {self.value!r}"
        if self.check == "ai_ready" and self.require:
            parts = ", ".join(f"{k}={v}" for k, v in sorted(self.require.items()))
            return f"ai_ready ({parts})"
        return self.check


@dataclass(frozen=True)
class MissionBudgets:
    """Hard ceilings for the (PR-2) autonomous runner. Optional in PR 1."""

    max_usd: Optional[float] = None
    max_iterations: Optional[int] = None
    max_wall_seconds: Optional[int] = None


@dataclass(frozen=True)
class MissionGates:
    """Gate configuration. ``destructive`` is ``ask`` or ``deny``."""

    destructive: str = "ask"


@dataclass(frozen=True)
class MissionSpec:
    """Validated mission spec (built-in or user-defined)."""

    name: str
    description: str
    goal: str
    success_criteria: Tuple[CriterionSpec, ...]
    budgets: MissionBudgets = MissionBudgets()
    gates: MissionGates = MissionGates()
    tools_allow: Tuple[str, ...] = ()
    plan_hint: Tuple[str, ...] = ()
    #: Resolved absolute path of the YAML file this spec was loaded from.
    source_path: Optional[Path] = None
    #: sha256 of the raw file bytes — the trust-pinning identity (direnv-style).
    content_sha256: str = ""
    #: True when loaded from the shipped ``builtin/`` package data.
    builtin: bool = False


def _require_mapping(value: Any, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise MissionSpecError(f"{label} must be a mapping.")
    return value


def _reject_unknown_keys(payload: Mapping[str, Any], allowed: frozenset, *, label: str) -> None:
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise MissionSpecError(
            f"{label} has unknown key(s) {unknown}; allowed keys are {sorted(allowed)}."
        )


def validate_predicate_path(path: str, *, label: str) -> None:
    """Validate a predicate dotted path against the frozen grammar.

    ``segment ::= key | key"[*]"`` joined by ``.`` — nothing else. No
    numeric indexes, no filters, no functions.
    """
    if not path:
        raise MissionSpecError(f"{label}: predicate needs a non-empty path.")
    for segment in path.split("."):
        if not _SEGMENT_RE.match(segment):
            raise MissionSpecError(
                f"{label}: invalid path segment '{segment}' in '{path}'. The predicate "
                "grammar is frozen: dotted keys with optional [*] array fan-out "
                "(e.g. exposes[*].contract.dq.rules)."
            )


def _normalize_predicate(criterion: Mapping[str, Any], *, label: str) -> Dict[str, Any]:
    path = str(criterion.get("path") or "").strip()
    op = str(criterion.get("op") or "").strip().lower()
    validate_predicate_path(path, label=label)
    if op not in PREDICATE_OPS:
        raise MissionSpecError(
            f"{label}: unknown predicate op '{op}'. Allowed (frozen): {sorted(PREDICATE_OPS)}."
        )
    value_provided = "value" in criterion
    value = criterion.get("value")
    if op == "exists":
        if value_provided and not isinstance(value, bool):
            raise MissionSpecError(
                f"{label}: 'exists' takes an optional boolean value (default true)."
            )
    else:
        if not value_provided:
            raise MissionSpecError(f"{label}: op '{op}' requires a value.")
        if isinstance(value, (dict, list)):
            raise MissionSpecError(f"{label}: predicate values must be scalars.")
    return {"path": path, "op": op, "value": value, "value_provided": value_provided}


def _normalize_ai_ready_require(raw: Any, *, label: str) -> Dict[str, Any]:
    if raw is None:
        return {}
    require = dict(_require_mapping(raw, label=f"{label}.require"))
    unknown = sorted(set(require) - AI_READY_REQUIRE_KEYS)
    if unknown:
        raise MissionSpecError(
            f"{label}.require has unknown key(s) {unknown}; "
            f"allowed keys are {sorted(AI_READY_REQUIRE_KEYS)}."
        )
    if (
        "sensitive_exposes_annotated" in require
        and require["sensitive_exposes_annotated"] is not True
    ):
        raise MissionSpecError(
            f"{label}.require.sensitive_exposes_annotated only accepts true "
            "(criteria assert desired state; drop the key to skip the assertion)."
        )
    if "missing_descriptions" in require:
        allowed = require["missing_descriptions"]
        if not isinstance(allowed, int) or isinstance(allowed, bool) or allowed < 0:
            raise MissionSpecError(
                f"{label}.require.missing_descriptions must be a non-negative integer "
                "(the maximum number of missing descriptions tolerated)."
            )
    return require


def _normalize_criteria(raw: Any, *, spec_name: str) -> Tuple[CriterionSpec, ...]:
    if not isinstance(raw, list) or not raw:
        raise MissionSpecError(f"{spec_name}: success_criteria must be a non-empty list.")

    criteria: List[CriterionSpec] = []
    for index, raw_criterion in enumerate(raw):
        label = f"{spec_name}: success_criteria[{index}]"
        criterion = _require_mapping(raw_criterion, label=label)
        _reject_unknown_keys(criterion, _CRITERION_KEYS, label=label)

        check = str(criterion.get("check") or "").strip().lower()
        if check not in MISSION_CHECK_TYPES:
            raise MissionSpecError(
                f"{label}: unknown check '{check}'. v1 ships exactly {list(MISSION_CHECK_TYPES)}."
            )
        advisory = criterion.get("advisory", False)
        if not isinstance(advisory, bool):
            raise MissionSpecError(f"{label}: advisory must be a boolean.")

        kwargs: Dict[str, Any] = {"check": check, "advisory": advisory}
        if check == "predicate":
            if "require" in criterion:
                raise MissionSpecError(f"{label}: 'require' only applies to ai_ready checks.")
            kwargs.update(_normalize_predicate(criterion, label=label))
        else:
            for key in ("path", "op", "value"):
                if key in criterion:
                    raise MissionSpecError(f"{label}: '{key}' only applies to predicate checks.")
            if check == "ai_ready":
                kwargs["require"] = _normalize_ai_ready_require(
                    criterion.get("require"), label=label
                )
            elif "require" in criterion:
                raise MissionSpecError(f"{label}: 'require' only applies to ai_ready checks.")
        criteria.append(CriterionSpec(**kwargs))

    if all(criterion.advisory for criterion in criteria):
        raise MissionSpecError(
            f"{spec_name}: at least one non-advisory criterion is required — "
            "a mission must have a deterministic gate (advisory checks never gate)."
        )
    return tuple(criteria)


def _normalize_budgets(raw: Any, *, spec_name: str) -> MissionBudgets:
    if raw is None:
        return MissionBudgets()
    budgets = _require_mapping(raw, label=f"{spec_name}: budgets")
    _reject_unknown_keys(budgets, _BUDGET_KEYS, label=f"{spec_name}: budgets")

    def _positive_number(key: str, *, integer: bool) -> Optional[float]:
        if key not in budgets or budgets[key] is None:
            return None
        value = budgets[key]
        ok_type = (
            isinstance(value, int) if integer else isinstance(value, (int, float))
        ) and not isinstance(value, bool)
        if not ok_type or value <= 0:
            kind = "positive integer" if integer else "positive number"
            raise MissionSpecError(f"{spec_name}: budgets.{key} must be a {kind}.")
        return value

    max_usd = _positive_number("max_usd", integer=False)
    max_iterations = _positive_number("max_iterations", integer=True)
    max_wall_seconds = _positive_number("max_wall_seconds", integer=True)
    return MissionBudgets(
        max_usd=float(max_usd) if max_usd is not None else None,
        max_iterations=int(max_iterations) if max_iterations is not None else None,
        max_wall_seconds=int(max_wall_seconds) if max_wall_seconds is not None else None,
    )


def _normalize_gates(raw: Any, *, spec_name: str) -> MissionGates:
    if raw is None:
        return MissionGates()
    gates = _require_mapping(raw, label=f"{spec_name}: gates")
    _reject_unknown_keys(gates, frozenset({"destructive"}), label=f"{spec_name}: gates")
    destructive = str(gates.get("destructive") or "ask").strip().lower()
    if destructive not in GATE_MODES:
        raise MissionSpecError(
            f"{spec_name}: gates.destructive must be one of {sorted(GATE_MODES)}."
        )
    return MissionGates(destructive=destructive)


def _normalize_string_list(raw: Any, *, spec_name: str, key: str) -> Tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise MissionSpecError(f"{spec_name}: {key} must be a list of strings.")
    items: List[str] = []
    for entry in raw:
        if not isinstance(entry, str) or not entry.strip():
            raise MissionSpecError(f"{spec_name}: {key} entries must be non-empty strings.")
        items.append(entry.strip())
    return tuple(items)


def _normalize_tools(raw: Any, *, spec_name: str) -> Tuple[str, ...]:
    if raw is None:
        return ()
    tools = _require_mapping(raw, label=f"{spec_name}: tools")
    _reject_unknown_keys(tools, frozenset({"allow"}), label=f"{spec_name}: tools")
    return _normalize_string_list(tools.get("allow"), spec_name=spec_name, key="tools.allow")


def load_mission_spec_from_path(spec_path: Path) -> MissionSpec:
    """Load and validate a mission spec from a YAML file path.

    Reads the raw bytes exactly once and stamps their sha256 onto the
    returned spec — this is the identity the trust gate pins, so the
    hash always reflects the bytes that were parsed (no TOCTOU window
    between hashing and loading).
    """
    spec_path = spec_path.resolve()
    spec_name = spec_path.stem
    if not spec_path.is_file():
        raise MissionSpecError(f"Mission spec '{spec_name}' was not found at {spec_path}.")

    raw_bytes = spec_path.read_bytes()
    content_sha256 = hashlib.sha256(raw_bytes).hexdigest()
    try:
        raw_payload = yaml.safe_load(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise MissionSpecError(
            f"{spec_name}: not parseable as YAML ({type(exc).__name__})."
        ) from exc

    payload = _require_mapping(raw_payload, label=f"{spec_name}: root")
    _reject_unknown_keys(payload, _ROOT_KEYS, label=f"{spec_name}: root")

    name = str(payload.get("name") or "").strip().lower()
    description = str(payload.get("description") or "").strip()
    goal = str(payload.get("goal") or "").strip()
    if not name or not description or not goal:
        raise MissionSpecError(f"{spec_name}: name, description, and goal are required.")
    if not _MISSION_NAME_RE.match(name):
        raise MissionSpecError(
            f"{spec_name}: name '{name}' must be lowercase alphanumeric with - or _."
        )

    return MissionSpec(
        name=name,
        description=description,
        goal=goal,
        success_criteria=_normalize_criteria(payload.get("success_criteria"), spec_name=spec_name),
        budgets=_normalize_budgets(payload.get("budgets"), spec_name=spec_name),
        gates=_normalize_gates(payload.get("gates"), spec_name=spec_name),
        tools_allow=_normalize_tools(payload.get("tools"), spec_name=spec_name),
        plan_hint=_normalize_string_list(
            payload.get("plan_hint"), spec_name=spec_name, key="plan_hint"
        ),
        source_path=spec_path,
        content_sha256=content_sha256,
        builtin=spec_path.parent == BUILTIN_MISSIONS_DIR.resolve(),
    )


def _name_candidates(name: str) -> List[str]:
    """File-stem candidates for a mission name (``gdpr-clean`` → ``gdpr_clean``)."""
    safe = str(name or "").strip().lower()
    candidates = [safe]
    swapped = safe.replace("-", "_")
    if swapped != safe:
        candidates.append(swapped)
    return candidates


@lru_cache(maxsize=None)
def load_builtin_mission_spec(name: str) -> MissionSpec:
    """Load and cache a built-in mission spec by name."""
    for stem in _name_candidates(name):
        if not stem:
            break
        path = BUILTIN_MISSIONS_DIR / f"{stem}.yaml"
        if path.is_file():
            return load_mission_spec_from_path(path)
    raise MissionSpecError(
        f"No built-in mission named '{name}'. "
        f"Available: {', '.join(sorted(builtin_mission_names())) or '(none)'}."
    )


def builtin_mission_names() -> List[str]:
    """Names of the shipped built-in missions."""
    names = []
    for path in sorted(BUILTIN_MISSIONS_DIR.glob("*.yaml")):
        try:
            names.append(load_mission_spec_from_path(path).name)
        except MissionSpecError:  # pragma: no cover — shipped specs are test-pinned
            continue
    return names


def user_mission_dirs() -> List[Path]:
    """User mission-spec directories in priority order (workspace → global).

    The workspace directory (``.fluid/missions/``) is the direnv-style
    trust boundary — specs there require content-hash pinning before any
    autonomous use. The user-global directory lives under
    :func:`fluid_build.paths.user_home` (``~/.fluid/missions/`` by
    default, ``$FLUID_USER_HOME`` aware) and is implicitly trusted —
    it is user-authored, outside any cloned repo.
    """
    from fluid_build.paths import user_home

    dirs: List[Path] = []
    local = Path.cwd() / ".fluid" / USER_MISSIONS_DIR_NAME
    if local.is_dir():
        dirs.append(local)
    global_dir = user_home() / USER_MISSIONS_DIR_NAME
    if global_dir.is_dir() and global_dir != local:
        dirs.append(global_dir)
    return dirs


def resolve_mission_spec(ref: str) -> MissionSpec:
    """Resolve *ref* — a mission name or a YAML path — to a loaded spec.

    Path-looking refs (existing file, or ``.yaml``/``.yml`` suffix, or a
    path separator) load directly. Names resolve workspace → user-global
    → built-in, mirroring ``forge_agent_specs``.
    """
    ref = str(ref or "").strip()
    if not ref:
        raise MissionSpecError("Mission reference cannot be empty.")

    candidate = Path(ref).expanduser()
    looks_like_path = candidate.suffix.lower() in {".yaml", ".yml"} or "/" in ref or "\\" in ref
    if looks_like_path or candidate.is_file():
        return load_mission_spec_from_path(candidate)

    for user_dir in user_mission_dirs():
        for stem in _name_candidates(ref):
            path = user_dir / f"{stem}.yaml"
            if path.is_file():
                return load_mission_spec_from_path(path)

    try:
        return load_builtin_mission_spec(ref)
    except MissionSpecError:
        available = sorted(discover_all_mission_specs())
        raise MissionSpecError(
            f"No mission named '{ref}' (looked in .fluid/missions/, the user-global "
            f"missions directory, and the built-ins). Available: "
            f"{', '.join(available) or '(none)'}."
        ) from None


def discover_all_mission_specs() -> Dict[str, MissionSpec]:
    """Discover all mission specs: built-in + user (global + workspace).

    Returns ``{name: spec}`` where workspace specs shadow user-global
    specs and user-global specs shadow built-ins with the same name.
    Invalid files are skipped (logged by callers that care); discovery
    must never crash ``fluid mission list``.
    """
    specs: Dict[str, MissionSpec] = {}
    search: List[Path] = [BUILTIN_MISSIONS_DIR, *reversed(user_mission_dirs())]
    for directory in search:
        for path in sorted(directory.glob("*.yaml")):
            try:
                spec = load_mission_spec_from_path(path)
            except MissionSpecError:
                continue
            specs[spec.name] = spec
    return specs


__all__ = [
    "AI_READY_REQUIRE_KEYS",
    "BUILTIN_MISSIONS_DIR",
    "CriterionSpec",
    "GATE_MODES",
    "MISSION_CHECK_TYPES",
    "MissionBudgets",
    "MissionGates",
    "MissionSpec",
    "MissionSpecError",
    "PREDICATE_OPS",
    "builtin_mission_names",
    "discover_all_mission_specs",
    "load_builtin_mission_spec",
    "load_mission_spec_from_path",
    "resolve_mission_spec",
    "user_mission_dirs",
    "validate_predicate_path",
]
