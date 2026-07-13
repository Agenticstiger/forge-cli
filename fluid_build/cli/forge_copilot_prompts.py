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

"""Prompt builders for the LLM-backed Forge copilot."""

from __future__ import annotations

__all__ = [
    "PromptProfileError",
    "active_overlay_fingerprint",
    "active_overlay_names",
    "active_overlay_validator_rules",
    "available_prompt_profiles",
    "base_guidance_without_overlays",
    "build_clarification_system_prompt",
    "build_clarification_user_prompt",
    "build_system_prompt",
    "build_user_prompt",
    "get_active_domain",
    "get_active_prompt_overlays",
    "get_active_prompt_profile",
    "guidance_cache_token",
    "set_domain_prompt_fragments",
    "set_prompt_overlays",
    "set_prompt_profile",
]


import hashlib
import json
import re
from pathlib import Path
from types import MappingProxyType
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import yaml

from fluid_build.cli.forge_copilot_memory import CopilotMemorySnapshot
from fluid_build.schema_manager import FluidSchemaManager

from .forge_copilot_contract_helpers import _normalize_interview_summary


def _latest_fluid_version() -> str:
    """Return the newest bundled FLUID schema version."""
    return FluidSchemaManager.latest_bundled_version()


# Default-guidance directory under agent_specs/. Each ``.yaml`` file has
# a single top-level key ``system_prompt`` whose value is injected into
# one of the prompt builders at a labelled slot. Editing the YAML is the
# supported way to adjust the prose — no Python change needed — but the
# prompt tests lock the composed output and will fail if drift is
# unintentional.
_DEFAULTS_DIR: Path = Path(__file__).with_name("agent_specs") / "_defaults"


def _load_default_guidance() -> Mapping[str, str]:
    """Load ``_defaults/*.yaml`` into a {name: system_prompt_text} map.

    Invoked once at module import.  Missing files or missing
    ``system_prompt`` keys fall back to an empty string so that an
    incomplete install doesn't crash the CLI — the snapshot test
    catches such drift in CI.
    """
    guidance: dict[str, str] = {}
    if not _DEFAULTS_DIR.is_dir():
        return guidance
    for path in sorted(_DEFAULTS_DIR.glob("*.yaml")):
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(raw, Mapping):
            continue
        text = raw.get("system_prompt")
        if isinstance(text, str):
            guidance[path.stem] = text
    return guidance


_DEFAULT_GUIDANCE: Mapping[str, str] = _load_default_guidance()


# ---------------------------------------------------------------------------
# Per-tenant / per-domain override layers (compose UNDER prompt profiles)
# ---------------------------------------------------------------------------
# Two complementary, no-fork override mechanisms sit between the bundled
# ``_defaults/`` and an active ``--prompt-profile``:
#
#   1. **User-home shadow** — a tenant drops ``<stem>.yaml`` into
#      ``<user-home>/agent_specs/_defaults/`` (``paths.user_home()`` →
#      ``~/.fluid`` or ``$FLUID_USER_HOME``) to override the matching bundled
#      guidance block on this machine/tenant, no fork required.
#   2. **Per-domain fragments** — a domain spec (``agent_specs/<domain>.yaml``)
#      carries an optional ``system_prompt_fragments`` map that overrides the
#      matching bundled block ONLY while that domain is active (activated by
#      ``forge_domain_enrichment.enrich_context_with_domain``).
#
# Precedence, lowest → highest (mirrors git ``--system`` < ``--global`` <
# ``-c``, npm builtin < user < CLI, and XDG system < ``~/.config`` < env):
#
#   bundled ``_defaults``  <  user-home shadow  <  active domain fragments
#                                                <  active ``--prompt-profile``
#
# Rationale for the order: each layer is progressively more specific or more
# explicit. The home shadow is the tenant's persistent customisation of the
# shipped baseline; domain fragments are contextually scoped to the detected
# product domain (more specific than an always-on shadow); ``--prompt-profile``
# is a deliberate, per-invocation operator selection and is therefore
# authoritative over every ambient/auto layer. When NONE of layers 2–4 are
# present, ``_active_guidance()`` returns the exact ``_DEFAULT_GUIDANCE``
# object, so the composed prompt is byte-identical to the baseline.


def _read_guidance_dir(directory: Path) -> Dict[str, str]:
    """Parse ``directory/*.yaml`` into a ``{stem: system_prompt_text}`` map.

    Shared parse rules for the per-tenant home shadow and prompt-profile
    overlays (the bundled default read keeps its own trusted loader). Each
    file has a single top-level ``system_prompt`` string, loaded with
    ``yaml.safe_load`` ONLY — never ``load`` — so a shadow/profile file can
    never execute code. Malformed or non-mapping files are skipped so a
    partial install can't crash the CLI.

    Security: the directory is tenant-writable, so each candidate's *resolved*
    parent must equal the resolved *directory* — this rejects a symlinked file
    that would otherwise turn the shadow dir into an arbitrary-file-read
    primitive for paths outside it. ``glob('*.yaml')`` already blocks ``..`` /
    separators in the name (they can't appear in a single path component).
    """
    guidance: Dict[str, str] = {}
    if not directory.is_dir():
        return guidance
    try:
        root = directory.resolve()
    except OSError:
        return guidance
    for path in sorted(directory.glob("*.yaml")):
        try:
            if path.resolve().parent != root:
                continue  # symlink / traversal escape — ignore
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(raw, Mapping):
            continue
        text = raw.get("system_prompt")
        if isinstance(text, str):
            guidance[path.stem] = text
    return guidance


def _load_user_shadow_guidance() -> Dict[str, str]:
    """Return the per-tenant home-shadow guidance map (``{}`` when none).

    Reads ``<user-home>/agent_specs/_defaults/*.yaml`` via
    :func:`fluid_build.cli.forge_agent_specs.user_defaults_shadow_dirs`.
    Deliberately DYNAMIC (re-read on each call, not memoised at import) so
    container / test isolation via ``FLUID_USER_HOME`` takes effect without a
    module reload, and so a tenant adding or removing a shadow file between
    runs is picked up. Empty result ⇒ the no-shadow fast path in
    ``_active_guidance`` returns the bundled defaults unchanged.
    """
    # Function-local import: avoids any import cycle and keeps this module's
    # import graph unchanged for callers that never touch guidance overlays.
    from fluid_build.cli.forge_agent_specs import user_defaults_shadow_dirs

    merged: Dict[str, str] = {}
    # Lowest-priority dir first so higher-priority dirs overlay it. Today this
    # is a single directory; the loop keeps a future workspace-local shadow
    # correct with zero call-site changes.
    for directory in reversed(user_defaults_shadow_dirs()):
        merged.update(_read_guidance_dir(directory))
    return merged


# Process-wide active-domain fragment overlay. ``None`` means "no domain
# override". Set by ``enrich_context_with_domain``; mirrors the prompt-profile
# state so ``build_system_prompt`` (which only sees the capability matrix)
# picks the override up through ``_active_guidance()`` without threading a
# domain argument through every call site.
_ACTIVE_DOMAIN: Optional[str] = None
_ACTIVE_DOMAIN_FRAGMENTS: Optional[Mapping[str, str]] = None


def set_domain_prompt_fragments(
    domain: Optional[str], fragments: Optional[Mapping[str, Any]]
) -> Optional[str]:
    """Activate (or clear) a per-domain ``system_prompt_fragments`` overlay.

    *fragments* is the ``system_prompt_fragments`` map parsed from the active
    domain's YAML: ``{default-stem: replacement-text}``. Only string→string
    entries are kept (defence against a malformed spec injecting non-text).
    Passing a falsy *domain* or an empty/typeless *fragments* clears the
    overlay. Process-wide and idempotent, mirroring :func:`set_prompt_profile`.
    Returns the active domain name (or ``None``).
    """
    global _ACTIVE_DOMAIN, _ACTIVE_DOMAIN_FRAGMENTS
    clean: Dict[str, str] = {}
    if isinstance(fragments, Mapping):
        for key, value in fragments.items():
            if isinstance(key, str) and key.strip() and isinstance(value, str):
                clean[key] = value
    if not domain or not clean:
        _ACTIVE_DOMAIN = None
        _ACTIVE_DOMAIN_FRAGMENTS = None
        return None
    _ACTIVE_DOMAIN = domain
    _ACTIVE_DOMAIN_FRAGMENTS = MappingProxyType(clean)
    return domain


def get_active_domain() -> Optional[str]:
    """Return the active domain-fragment overlay's domain name, or ``None``."""
    return _ACTIVE_DOMAIN


# ---------------------------------------------------------------------------
# Prompt profiles — single-name, single-swap prompt overlays
# ---------------------------------------------------------------------------
# ``fluid forge --prompt-profile <name>`` (or ``FLUID_PROMPT_PROFILE=<name>``)
# swaps the whole set of default-guidance YAML files at once. Profiles live
# under ``agent_specs/prompt_profiles/<name>/`` with the SAME file names as
# ``_defaults/`` (e.g. ``sovereignty.yaml``, ``agent_policy.yaml``).
#
# Activating a profile *overlays* its files on top of the defaults: any file
# the profile ships wins; any file it omits falls back to the default, so a
# profile author only has to override what actually differs. When NO profile
# is active the composed prompt is byte-identical to the default baseline —
# ``_active_guidance()`` returns the exact ``_DEFAULT_GUIDANCE`` object.
#
# Prior art (see PR notes): dbt's ``DBT_PROFILES_DIR`` / ``--profiles-dir``
# (a named directory swap) and Claude Code's ``--system-prompt-file`` fragment
# directories (behavioural-layer overlay). We diverge from a full-swap because
# an overlay keeps profiles DRY and can't accidentally drop a required block.
#
# No stacking, no composition — a single name selects a single directory.
_PROFILES_DIR: Path = Path(__file__).with_name("agent_specs") / "prompt_profiles"

# Profile names must be simple slugs — no path separators, no traversal, no
# leading dot/underscore. This is the first line of defence against
# ``--prompt-profile ../../etc``; ``_resolve_profile_dir`` adds a resolved-path
# containment check as the second.
_PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class PromptProfileError(ValueError):
    """Raised when an unknown or unsafe prompt profile is requested.

    Never raised for the no-profile default path; only when a caller asks
    for a profile that doesn't exist or whose name is unsafe. The CLI turns
    this into a clear, non-silent error (it does NOT fall back to defaults).
    """


# Process-wide active-profile state. ``None`` means "no profile selected".
# ``_ACTIVE_PROFILE_FILES`` holds ONLY the profile's own ``*.yaml`` (no base
# merge) so ``_active_guidance()`` can overlay it as the TOP layer on top of
# the composed bundled+shadow+domain base.
_ACTIVE_PROFILE: Optional[str] = None
_ACTIVE_PROFILE_FILES: Optional[Mapping[str, str]] = None


def available_prompt_profiles() -> List[str]:
    """Return the sorted names of bundled prompt profiles.

    A "profile" is any direct subdirectory of ``prompt_profiles/`` whose
    name doesn't start with ``.`` or ``_``. Used to build a helpful error
    message when an unknown profile is requested.
    """
    if not _PROFILES_DIR.is_dir():
        return []
    return sorted(
        entry.name
        for entry in _PROFILES_DIR.iterdir()
        if entry.is_dir() and not entry.name.startswith((".", "_"))
    )


def _resolve_profile_dir(name: str) -> Path:
    """Validate *name* and resolve ``prompt_profiles/<name>/``.

    Raises :class:`PromptProfileError` on an unsafe name (path separators /
    traversal), a directory that escapes the profiles root, or an unknown
    profile. Never silently falls back to ``_defaults/``.
    """
    if not name or not _PROFILE_NAME_RE.match(name):
        raise PromptProfileError(
            f"invalid prompt profile name {name!r}: names must match "
            r"[A-Za-z0-9][A-Za-z0-9._-]* (no path separators or traversal)"
        )
    root = _PROFILES_DIR.resolve()
    candidate = (_PROFILES_DIR / name).resolve()
    # Defence in depth: the resolved candidate must sit directly under the
    # profiles root. Rejects symlink / traversal escapes even if a name
    # somehow slipped past the slug check above.
    if candidate.parent != root:
        raise PromptProfileError(f"prompt profile {name!r} escapes the profiles directory {root}")
    if not candidate.is_dir():
        available = ", ".join(available_prompt_profiles()) or "(none bundled)"
        raise PromptProfileError(
            f"unknown prompt profile {name!r}. Available profiles: {available}. "
            f"Add one under {_PROFILES_DIR}{Path('/')}<name>{Path('/')} with the "
            "same file names as _defaults/."
        )
    return candidate


def _load_profile_files(profile_dir: Path) -> Dict[str, str]:
    """Load ONLY ``profile_dir/*.yaml`` (no base merge).

    Returns the profile's own ``{stem: system_prompt_text}`` files so
    :func:`_active_guidance` can overlay them as the top layer. The parse
    rules mirror :func:`_load_default_guidance` — a single top-level
    ``system_prompt`` string per file — so a profile file is a drop-in
    replacement for its ``_defaults/`` counterpart. Shares the hardened,
    ``safe_load``-only, symlink-contained reader with the home shadow.
    """
    return _read_guidance_dir(profile_dir)


def set_prompt_profile(name: Optional[str]) -> Optional[str]:
    """Activate a prompt profile (or clear it with ``None`` / ``""``).

    Returns the active profile name (or ``None``). Raises
    :class:`PromptProfileError` on an unknown or unsafe name — never
    silently falls back to the defaults. Idempotent and process-wide.

    Callers that memoize the composed system prompt (see
    ``forge_copilot_runtime.build_system_prompt``) key their cache on
    :func:`guidance_cache_token`, so switching profiles (or any override
    layer) never returns a stale prompt.
    """
    global _ACTIVE_PROFILE, _ACTIVE_PROFILE_FILES
    if not name:
        _ACTIVE_PROFILE = None
        _ACTIVE_PROFILE_FILES = None
        return None
    profile_dir = _resolve_profile_dir(name)
    _ACTIVE_PROFILE_FILES = MappingProxyType(_load_profile_files(profile_dir))
    _ACTIVE_PROFILE = name
    return name


def get_active_prompt_profile() -> Optional[str]:
    """Return the active prompt profile name, or ``None`` for the defaults."""
    return _ACTIVE_PROFILE


# ---------------------------------------------------------------------------
# Prompt overlays — stackable, section-addressable patches (compose OVER
# profiles). See ``fluid_build.cli.forge_prompt_overlays`` for the loader,
# ed25519 signing, and the anchor-integrity guard. State lives HERE (next to
# the guidance composer) so ``_active_guidance`` / ``guidance_cache_token`` can
# see it without a threaded argument; the overlay module owns the heavy crypto
# and is only imported when a stack is actually active (keeps this module's
# import graph off the ``fluid --help`` cold path).
# ---------------------------------------------------------------------------
# ``_ACTIVE_OVERLAYS`` holds the validated overlay objects (duck-typed:
# ``.sections`` / ``.validator_rules``), applied left-to-right as the TOP layer.
_ACTIVE_OVERLAYS: Tuple[Any, ...] = ()
_ACTIVE_OVERLAY_FINGERPRINT: str = ""


def set_prompt_overlays(overlays: Sequence[Any], fingerprint: str) -> None:
    """Install a pre-validated overlay stack (called by ``activate_prompt_overlays``).

    Idempotent and process-wide. Pass ``((), "")`` to clear. The overlays are
    trusted here — signature policy and the anchor guard already ran during
    activation; this setter only records state so the composer and cache key
    observe it.
    """
    global _ACTIVE_OVERLAYS, _ACTIVE_OVERLAY_FINGERPRINT
    _ACTIVE_OVERLAYS = tuple(overlays or ())
    _ACTIVE_OVERLAY_FINGERPRINT = fingerprint or ""


def get_active_prompt_overlays() -> Tuple[Any, ...]:
    """Return the active overlay stack (empty tuple when none)."""
    return _ACTIVE_OVERLAYS


def active_overlay_fingerprint() -> str:
    """Return ``SHA1`` of the active overlay stack, or ``""`` when empty."""
    return _ACTIVE_OVERLAY_FINGERPRINT


def active_overlay_names() -> List[str]:
    """Return the names of the active overlays in stack order (for provenance)."""
    return [getattr(o, "name", "") for o in _ACTIVE_OVERLAYS if getattr(o, "name", "")]


def active_overlay_validator_rules() -> List[Dict[str, Any]]:
    """Return the flattened validator-rule dicts from the active overlay stack.

    Threaded into ``validate_generated_result`` so an overlay-supplied rule can
    reject a violating contract. Empty list on the no-overlay fast path.
    """
    rules: List[Dict[str, Any]] = []
    for overlay in _ACTIVE_OVERLAYS:
        for rule in getattr(overlay, "validator_rules", ()) or ():
            as_dict = getattr(rule, "as_dict", None)
            if callable(as_dict):
                rules.append(as_dict())
    return rules


def base_guidance_without_overlays() -> Dict[str, str]:
    """Compose bundled + shadow + domain + profile guidance, WITHOUT overlays.

    Used by the overlay activation path as the anchor-guard baseline (the text
    an overlay stack must not strip an anchor from). Always returns a fresh
    mutable dict so the caller can layer overlays on top.
    """
    merged: Dict[str, str] = dict(_DEFAULT_GUIDANCE)
    shadow = _load_user_shadow_guidance()
    if shadow:
        merged.update(shadow)
    if _ACTIVE_DOMAIN_FRAGMENTS:
        merged.update(_ACTIVE_DOMAIN_FRAGMENTS)
    if _ACTIVE_PROFILE_FILES:
        merged.update(_ACTIVE_PROFILE_FILES)
    return merged


def _active_guidance() -> Mapping[str, str]:
    """Compose the effective guidance map across every override layer.

    Precedence, lowest → highest (see the module-level note): bundled
    ``_defaults`` < user-home shadow < active domain fragments < active
    ``--prompt-profile``. When layers 2–4 are all absent this returns the
    exact ``_DEFAULT_GUIDANCE`` object, guaranteeing the composed prompt is
    byte-identical to the baseline (and preserving the provider prompt-cache
    fast path).
    """
    shadow = _load_user_shadow_guidance()
    domain = _ACTIVE_DOMAIN_FRAGMENTS
    profile = _ACTIVE_PROFILE_FILES
    overlays = _ACTIVE_OVERLAYS
    if not shadow and not domain and not profile and not overlays:
        return _DEFAULT_GUIDANCE
    merged: Dict[str, str] = dict(_DEFAULT_GUIDANCE)
    if shadow:
        merged.update(shadow)
    if domain:
        merged.update(domain)
    if profile:
        merged.update(profile)
    if overlays:
        # TOP layer: section-addressable replace/append/prepend patches applied
        # left-to-right. Imported lazily so the crypto/overlay module never
        # loads on the pure-default fast path. The stack was anchor-guarded at
        # activation, so compose-time application is trusted.
        from fluid_build.cli.forge_prompt_overlays import apply_overlays_to_guidance

        merged = apply_overlays_to_guidance(merged, overlays)
    return MappingProxyType(merged)


def guidance_cache_token() -> str:
    """Return a stable token summarising the active guidance layers.

    Folded into the memoised system-prompt cache key
    (``forge_copilot_runtime._system_prompt_cache_key``) so the cache is
    invalidated whenever ANY override layer changes — profile name, active
    domain, or the *content* of the dynamic home-shadow / domain-fragment
    layers (the latter two aren't fully captured by a name alone). Returns a
    cheap constant on the pure-default path so the common case stays fast.
    """
    shadow = _load_user_shadow_guidance()
    domain_frag = dict(_ACTIVE_DOMAIN_FRAGMENTS or {})
    overlay_fp = _ACTIVE_OVERLAY_FINGERPRINT
    if not shadow and not domain_frag and _ACTIVE_PROFILE is None and not overlay_fp:
        return "::0"
    blob = json.dumps({"shadow": shadow, "domain_frag": domain_frag}, sort_keys=True)
    digest = hashlib.sha1(blob.encode("utf-8"), usedforsecurity=False).hexdigest()[:16]
    token = f"{_ACTIVE_PROFILE or ''}:{_ACTIVE_DOMAIN or ''}:{digest}"
    # Append the overlay fingerprint ONLY when a stack is active, so the token
    # is byte-identical to the legacy value whenever the overlay stack is empty
    # (no cache invalidation for existing users — even with a profile active).
    if overlay_fp:
        token = f"{token}:ov={overlay_fp}"
    return token


# Per-agent "voice" fragments moved to the tier-0 shared leaf
# ``fluid_build._agent_voice`` so ``copilot.agents.base`` can prepend a
# stage's voice without importing this ``cli`` module (the ``copilot -> cli``
# edge the ``[tool.importlinter]`` contracts forbid). Re-exported here under the
# same names so existing call sites and test patches on
# ``fluid_build.cli.forge_copilot_prompts.<name>`` keep resolving.
from fluid_build._agent_voice import (  # noqa: E402,F401
    _AGENT_VOICES,
    _load_agent_voices,
    agent_voice,
)

_AUXILIARY_PROMPT_NAMES = frozenset({"clarification", "evaluation"})
# ``MappingProxyType`` makes the auxiliary prompt map actually immutable post-import,
# matching the ``Mapping[str, str]`` annotation rather than a mutable ``dict`` that
# only appears immutable to type checkers. Mirrors stdlib's defensive idiom for
# class ``__dict__`` and similar import-time-frozen singletons.
_AUXILIARY_PROMPTS: Mapping[str, str] = MappingProxyType(
    {name: _DEFAULT_GUIDANCE.get(name, "") for name in _AUXILIARY_PROMPT_NAMES}
)


def _render_auxiliary_prompt(name: str, replacements: Mapping[str, str]) -> str:
    # Resolve through the active profile so a prompt profile can override the
    # clarification / evaluation prose too. Falls back to ``_AUXILIARY_PROMPTS``
    # (the frozen default) when the active guidance lacks the key — which is
    # exactly the no-profile path, keeping output byte-identical.
    text = _active_guidance().get(name, _AUXILIARY_PROMPTS.get(name, ""))
    for key, value in replacements.items():
        text = text.replace("${" + key + "}", value)
    return text


def _evaluation_prompt_spec() -> Mapping[str, Any]:
    raw = _active_guidance().get("evaluation", _AUXILIARY_PROMPTS.get("evaluation", "{}"))
    try:
        spec = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return spec if isinstance(spec, Mapping) else {}


# Per-technique rule sheets injected into the user prompt. Keyed by the
# canonical value of ``context["data_modeling_technique"]`` (produced by
# :func:`fluid_build.cli.forge_copilot_interview.normalize_interview_value`).
# The LLM reads this alongside ``upstream_products`` and must follow the
# named conventions in every ``additional_files`` SQL file it emits.
_MODELING_GUIDANCE: Mapping[str, Mapping[str, Any]] = {
    "data_vault_2": {
        "label": "Data Vault 2.0",
        "naming_conventions": {
            "hub": "hub_<entity>",
            "link": "lnk_<relation>",
            "satellite": "sat_<entity>_<source_system>",
            "point_in_time": "pit_<entity>",
            "bridge": "br_<relation>",
        },
        "key_strategy": (
            "Business keys are hashed to 32-hex surrogate keys via "
            "md5(upper(trim(<business_key>))). Parent keys in links and "
            "satellites reference hub hash keys only — never raw business keys."
        ),
        "load_metadata": [
            "load_dts TIMESTAMP — insert time of the record",
            "record_source VARCHAR — short identifier of the upstream source system",
            "hash_diff VARCHAR (satellites only) — md5 of all descriptive attributes",
        ],
        "layer_structure": (
            "Staging view per upstream source (one per consume). Raw vault: hubs + "
            "links + satellites. Business vault (optional) may derive PITs/bridges."
        ),
        "insert_only": True,
        "anti_patterns": [
            "Do NOT update or delete raw-vault rows — all history is insert-only.",
            "Do NOT mix sources in one satellite; one source = one satellite.",
            "Do NOT fabricate business keys when a natural key exists upstream.",
        ],
    },
    "dimensional": {
        "label": "Dimensional (Kimball)",
        "naming_conventions": {
            "staging": "stg_<source>",
            "dimension": "dim_<entity>",
            "fact": "fct_<grain>",
            "conformed_dimension": "dim_<conformed_entity>",
        },
        "key_strategy": (
            "Dimensions have integer surrogate keys (<entity>_key) generated via "
            "dbt_utils.generate_surrogate_key() from the natural key + scd columns. "
            "Facts reference dimension surrogate keys only — never natural keys."
        ),
        "load_metadata": [
            "valid_from TIMESTAMP — SCD type-2 effective start",
            "valid_to TIMESTAMP — SCD type-2 effective end (null for current row)",
            "is_current BOOLEAN — true for the live row of each natural key",
        ],
        "layer_structure": (
            "Staging view per upstream source, then conformed dimensions (shared "
            "across facts), then fact tables one per business process / grain."
        ),
        "scd_handling": (
            "Type-2 for dimensions with historical attributes; type-1 for rapidly "
            "changing dimensions where history isn't required."
        ),
        "anti_patterns": [
            "Do NOT reference natural keys from fact tables — only surrogate keys.",
            "Do NOT duplicate conformed dimensions per fact; reuse the shared dim.",
            "Do NOT store additive measures on dimensions; measures belong on facts.",
        ],
    },
}


def build_system_prompt(
    capability_matrix: Mapping[str, Any], known_build_engines: Sequence[str]
) -> str:
    """System prompt for structured FLUID contract generation."""
    providers = ", ".join(capability_matrix.get("providers") or [])
    engines = ", ".join(capability_matrix.get("build_engines") or list(known_build_engines))
    fv = _latest_fluid_version()
    # Resolve the injected guidance blocks through the active prompt profile
    # (defaults when none is active — byte-identical to the baseline).
    guidance = _active_guidance()
    return (
        # Security lint: this function constructs static prompt prose, not executable SQL.
        f"You are FLUID Forge Copilot. Generate a production-ready FLUID {fv} contract and README "  # noqa: S608
        "that only use locally supported templates, providers, and build engines.\n"
        # Load-bearing strict-JSON + no-secrets directives. Relocated out of
        # inline prose into an overlay-addressable ``response_contract`` section
        # (agent_specs/_defaults/response_contract.yaml) so tenant overlays can
        # reinforce them and the anchor-integrity guard can prove no overlay
        # drops them. Byte-identical to the previous prose (the block scalar's
        # trailing newline reproduces the original two ``\n``-terminated lines).
        + guidance.get("response_contract", "") + f"ALWAYS use fluidVersion '{fv}'.\n"
        "Treat project_memory as a soft preference layer only. Explicit user context and the current "
        "discovery report take precedence.\n"
        "Use interview_summary as the authoritative statement of current user intent.\n\n"
        "TEAM MEMORY: If team_memory is provided, treat it as authoritative team conventions:\n"
        "- Use team vocabulary (entities, measures, dimensions) as preferred names in the contract.\n"
        "- Apply team naming conventions (product_prefix, layer_convention, column_style).\n"
        "- Respect team defaults (provider, build_engine, domain, owner_team) unless the user overrides.\n"
        "- Honour team decisions — do not contradict architectural decisions the team has made.\n"
        "Team memory takes precedence over project_memory and personal defaults.\n\n"
        "If interview_summary includes canonical_model or supporting_standards, use them as the authoritative "
        "semantic modeling guidance for entity names, measures, dimensions, and descriptions.\n"
        "Prefer canonical business vocabulary from those standards over source-table or file-specific names.\n\n"
        # --- Chain-of-thought reasoning ---
        "REASONING: Before generating the contract, think through these steps in order:\n"
        "1. Analyze the data sources — what schema shape, column types, and relationships are implied?\n"
        "2. Evaluate which template best matches the use case and why.\n"
        "3. Select the provider and build engine based on the capability matrix and compatibility rules.\n"
        "4. Design entity modeling — identify primary keys, measures, dimensions, and time grains.\n"
        "5. Determine if sovereignty or agentPolicy blocks are needed based on compliance context.\n"
        "Include your reasoning in a top-level 'reasoning' key (string) in the response JSON.\n\n"
        "The JSON object must contain keys: reasoning, recommended_template, recommended_provider, "
        "recommended_patterns, architecture_suggestions, best_practices, technology_stack, "
        "description, domain, owner, readme_markdown, contract, additional_files.\n\n"
        f"CRITICAL: The contract value must be a JSON object that strictly conforms to the FLUID {fv} schema.\n"
        "The ONLY allowed top-level keys in the contract object are: "
        "fluidVersion, kind, id, name, description, domain, metadata, consumes, builds, exposes, sovereignty.\n"
        "Only include 'sovereignty' when the user specifies jurisdiction, compliance, or data residency requirements.\n"
        "DO NOT add 'quality', 'governance', 'owner', or any other top-level key.\n\n"
        "metadata must be an object with: owner (object with team and email) and layer.\n\n"
        "Each build must have: id, pattern (one of: 'embedded-logic', 'hybrid-reference', "
        "'multi-stage', 'acquisition'), "
        "engine (one of: " + engines + "), properties, execution.\n"
        "The 'engine' value MUST be exactly one of the short names above. "
        "Do NOT invent provider-suffixed variants like 'dbt-snowflake', 'dbt-bigquery', 'dbt-athena', "
        "'dbt-redshift', 'dataform', or 'glue' — those are NOT valid. "
        "For Snowflake/BigQuery/Athena dbt projects, use engine='dbt' and declare the target platform "
        "via binding.platform on each expose.\n"
        # --- Engine ↔ productType mapping (Data Mesh axiom) ---
        # Strong nudge: SDPs ingest, ADP/CDP transform. The wrong engine
        # for a given productType produces a contract that's syntactically
        # valid but architecturally incoherent — e.g. a dbt-engine SDP
        # implies the SDP IS a dbt model, which contradicts SDP's role
        # as the raw, source-aligned product. Picking the right engine
        # up-front saves a refine round-trip.
        "ENGINE × productType MAPPING (pick the engine that matches the role):\n"
        "- SDP (source-aligned, Bronze): role = INGESTION. Prefer ingestion engines:\n"
        "  * 'duckdb' for filesystem / JDBC sources (CSV / Parquet / Postgres / MySQL / SQLite, zero infra)\n"
        "  * 'dlt' for Python-native incremental loads (REST APIs, GitHub, Stripe, custom auth flows)\n"
        "  * 'airbyte' for 350+ pre-built SaaS connectors (Salesforce / Hubspot / Stripe / GitHub)\n"
        "  * 'meltano' for Singer-tap ecosystem (600+ taps, when you want config-driven Singer)\n"
        "  * 'kafka-connect' for streaming source connectors\n"
        "  * 'debezium' for CDC from operational databases (Postgres / MySQL / Mongo / Oracle)\n"
        "  * 'python' for fully custom ingestion (rare — usually one of the above fits)\n"
        "  * 'sql' for SDP only when the source IS a SQL warehouse and you're snapshotting a query\n"
        "  AVOID 'dbt' for SDP — dbt is a TRANSFORM engine; using it for SDP implies the SDP IS a dbt model,\n"
        "  which contradicts SDP's source-aligned role.\n"
        "- ADP (aggregate, Silver): role = TRANSFORM. Prefer 'dbt' (most common), 'sql', or 'python'.\n"
        "  AVOID ingestion engines (duckdb/dlt/airbyte/meltano/kafka-connect/debezium) for ADP.\n"
        "- CDP (consumer-aligned, Gold): role = TRANSFORM + SHAPE FOR SERVING. Prefer 'dbt' or 'sql'.\n"
        "  AVOID ingestion engines for CDP.\n"
        "When the user's data_sources mention an external system (REST API, OAuth, SaaS, files in S3/GCS,\n"
        "a Postgres database that's not the warehouse) AND productType='SDP', you are almost certainly\n"
        "supposed to pick 'dlt', 'duckdb', or 'airbyte' — NOT 'dbt'.\n"
        "BUILD PROPERTIES SHAPE (strict — additionalProperties is false per pattern):\n"
        "- pattern='hybrid-reference' (the common dbt case): properties = {model (required, string), "
        "vars? (object), materializations? (object, keys->{table|view|incremental|ephemeral}), "
        "tags?, labels?}. DO NOT add 'profile', 'projectDir', 'target', 'schema', 'database', or any "
        "other key to properties — those are resolved at apply time from the provider config, not "
        "declared in the contract.\n"
        "- pattern='embedded-logic' (engine='sql' for inline SQL): properties = {sql (required, string), "
        "language? (one of: sql, flink_sql, pyspark, scala, python, r), parameters? (object), "
        "tags?, labels?}. ``sql`` is required even when language=python — pass the Python code in "
        "``sql`` and set ``language: python``.\n"
        "- pattern='multi-stage': properties = {stages (array of objects with name, pattern, "
        "properties, dependsOn)}.\n"
        "- pattern='acquisition' (NEW in 0.7.3, REQUIRED for engines duckdb/dlt/airbyte/meltano/"
        "kafka-connect/debezium): properties = {\n"
        "    source (REQUIRED object, additionalProperties=false; allowed keys: "
        "kind, connection?, mode, cursor_field?, watermark?, streams?, reader?),\n"
        "    sink? (object, additionalProperties=false; allowed keys: format, catalog?, "
        "partitionBy?),\n"
        "    delivery? (object: trigger/scheduler/replay/ordering/slo),\n"
        "    schemaEvolution? (object),\n"
        "    preLand? (array, allowed values: 'dlp_scan'|'tokenize_pii'|'quality_gate'|"
        "'emit_lineage_input'),\n"
        "    quality?, cost?, catalog?, concurrency?, lineage?,\n"
        "    duckdb? (engine-specific config: extensions[]),\n"
        "    dlt? ({source_module, pipeline_name}),\n"
        "    airbyte? ({connector_image, version, normalization, ...}),\n"
        "    meltano? ({tap, project_dir, deployment}),\n"
        "    'kafka-connect'? (engine-specific),\n"
        "    debezium? (engine-specific)\n"
        "  }.\n"
        "  source.kind is engine-specific: filesystem/postgres/mysql/sqlite/http/salesforce/stripe/"
        "github/kafka — pick the value the engine documents. source.mode MUST be one of: "
        "'full_refresh', 'incremental_append', 'incremental_dedup', 'incremental_merge', 'cdc', "
        "'streaming'. sink.format MUST be one of: 'iceberg', 'delta', 'parquet', 'csv', 'json', "
        "'snowflake_table', 'bigquery_table', 'redshift_table', 'duckdb_table'. "
        "DO NOT add 'format'/'schema'/'datasets' under source — those go elsewhere "
        "(sink.format, expose.contract.schema, source.streams). DO NOT add 'datasetRef' or "
        "'writeMode' under sink — those are not part of the schema. NEVER inline credentials — "
        "use ${ENV_VAR} placeholders or connection.secretRef.\n"
        "For engine='sql', use pattern='embedded-logic' and properties must contain 'sql' with "
        "a SQL string.\n"
        "For engine='python', ALWAYS use pattern='hybrid-reference'. Required fields: "
        "build.repository (git URL or local path string) AND properties.model (dotted module "
        "path like 'src.weather:fetch'). DO NOT use pattern='embedded-logic' for python — the "
        "validator rejects python builds without repository+model. Use this shape:\n"
        "    builds:\n"
        "    - id: <build_id>\n"
        "      pattern: hybrid-reference\n"
        "      engine: python\n"
        "      repository: <git url or local path>\n"
        "      properties:\n"
        "        model: <module>:<entrypoint>\n"
        "      execution: {trigger: {...}, runtime: {platform, resources}}\n"
        "For engines duckdb/dlt/airbyte/meltano/kafka-connect/debezium, ALWAYS use "
        "pattern='acquisition' and the acquisition properties shape above. Do NOT use "
        "embedded-logic/hybrid-reference/multi-stage for those engines — schema validation will "
        "reject the contract.\n"
        "execution must have trigger (object with type and iterations) and runtime (object with platform and resources).\n"
        "trigger.type MUST be EXACTLY one of: 'schedule' (time-based, e.g. daily at 2am — set "
        "trigger.schedule to a cron string like '0 2 * * *'), 'event' (data-arrival or webhook — "
        "set trigger.event), 'manual' (on-demand), 'dependency' (run when an upstream completes), "
        "'dataset' (run when a dataset arrives — set trigger.datasets), 'schedule_and_dataset' "
        "(both gates required), 'timetable' (custom timetable). trigger.iterations is usually 1 "
        "for batch, -1 for streaming. DO NOT use 'cron' or 'streaming' as trigger.type — those "
        "are NOT in the schema enum and will fail validation.\n"
        "If the user asked for scheduling, set trigger.type='schedule' and a sensible cron in "
        "trigger.schedule (cron syntax like '0 2 * * *').\n"
        "DO NOT add 'consumes' or 'produces' inside a build object.\n\n"
        "Each consume must have: productId (string) and exposeId (string). No other keys.\n\n"
        "Each expose must have: exposeId (string), kind (string), binding (object with platform, format, location), "
        "contract (object with schema as array of column objects with name, type, required).\n"
        "binding.platform is REQUIRED and must be one of: " + providers + ".\n"
        "binding.format is REQUIRED and must be one of: 'bigquery_table', 'snowflake_table', "
        "'gcs_file', 's3_file', 'http_api', 'grpc_api', 'pubsub_topic', 'kafka_topic', "
        "'delta_table', 'iceberg', 'parquet', 'csv', 'json', 'other'. "
        "Match the format to the platform: snowflake->'snowflake_table', gcp->'bigquery_table', "
        "aws->'s3_file' or 'delta_table' or 'iceberg', local->'parquet' or 'csv' or 'json'. "
        "Do NOT use generic values like 'table', 'view', or 'dataset'.\n"
        "DO NOT put 'platform' inside binding.location.\n\n"
        f"NEW IN {fv} — SEMANTICS BLOCK (required on each expose):\n"
        "Each expose MUST include a 'semantics' object with the following structure:\n"
        "- name (string): Human-readable name for this semantic model\n"
        "- description (string): Business context for what this model represents\n"
        "- entities (array): Join keys with type annotations. Each entity has: name (string), "
        "type (one of: 'primary', 'foreign', 'unique', 'natural'), and optional expr and description.\n"
        "- measures (array): Aggregatable expressions. Each measure has: name (string, required), "
        "agg (one of: 'sum', 'avg', 'count', 'count_distinct', 'min', 'max', 'median', 'percentile', required), "
        "and optional expr, description, createMetric (boolean).\n"
        "- dimensions (array): Grouping axes. Each dimension has: name (string, required), "
        "type (one of: 'categorical', 'time', required), and optional expr, description, "
        "typeParams (object with timeGranularity for time dimensions).\n"
        "- metrics (array): KPI definitions. Each metric has: name (string, required), "
        "type (one of: 'simple', 'derived', 'ratio', required), "
        "and optional measure (for simple), filter, inputMetrics (array of strings for derived/ratio), "
        "expr (for derived), numerator/denominator (for ratio), description.\n"
        "The semantics block enables AI agents and BI tools to generate correct queries without hallucination.\n\n"
        # --- Default guidance loaded from agent_specs/_defaults/ ---
        # These blocks are expanded from YAML at import time so editing
        # the prose doesn't require a Python change. The mid-prompt YAML
        # blocks end with one trailing newline from YAML ``|``; we add
        # one more newline per block to reproduce the original ``\n\n`` gap.
        + guidance.get("sovereignty", "")
        + "\n"
        + guidance.get("agent_policy", "")
        + "\n"
        + "Follow the seed_contract structure exactly as a reference for the correct schema shape.\n"  # noqa: S608  # nosec B608
        f"Allowed providers: {providers}.\n"
        "Only use build engines from the provided capability matrix.\n\n"
        # --- Upstream-driven transformation SQL ---
        + guidance.get("upstream_sql", "") + "\n"
        # --- Engine-owned files: do NOT recreate ---
        "ENGINE-OWNED FILES (do NOT write these to additional_files):\n"
        "- dbt_project/models/sources.yml — emitted by the engine. NEVER include a "
        "  `sources:` block in any YAML you ship; duplicating source declarations makes "
        '  dbt fail with "two sources with the same name". If you emit a schema.yml '
        "  file, it must contain ONLY a `models:` top-level key.\n"
        "- dbt_project/profiles.yml — emitted by the engine.\n"
        "- dbt_project/dbt_project.yml — emitted by the engine.\n"
        "When a modeling technique is active the engine does NOT emit any per-model "
        "schema.yml either, so you SHOULD ship exactly one schema.yml at "
        "`additional_files['dbt_project/models/schema.yml']` listing every staging + "
        "mart model you authored, with per-column tests.\n\n"
        # --- Modeling-technique mandate ---
        + guidance.get("technique_mandate", "") + "\n"
    )


_EVAL_MAX_SCHEMA_COLUMNS = 3


def _truncate_contract_for_eval(contract: Mapping[str, Any]) -> dict:
    """Return a lightweight copy of the contract for evaluation.

    Large schema arrays are truncated to the first few columns to keep
    the evaluation prompt small enough for the routing model.
    """
    c = dict(contract)
    exposes = c.get("exposes")
    if isinstance(exposes, list):
        trimmed = []
        for expose in exposes:
            expose = dict(expose)
            schema = (expose.get("contract") or {}).get("schema")
            if isinstance(schema, list) and len(schema) > _EVAL_MAX_SCHEMA_COLUMNS:
                expose = dict(expose)
                expose["contract"] = dict(expose.get("contract") or {})
                expose["contract"]["schema"] = schema[:_EVAL_MAX_SCHEMA_COLUMNS]
                expose["contract"]["_truncated_columns"] = len(schema)
            trimmed.append(expose)
        c["exposes"] = trimmed
    return c


def build_evaluation_prompt(
    context: Mapping[str, Any],
    contract: Mapping[str, Any],
) -> str:
    """Build a prompt that asks the LLM to evaluate a generated contract.

    Used for self-evaluation after schema validation passes — checks
    semantic quality, not just structural correctness.  The response
    is a small JSON: ``{"score": int, "issues": [str], "suggestions": [str]}``.
    """
    goal = context.get("project_goal") or context.get("description") or ""
    use_case = context.get("use_case") or ""
    data_sources = context.get("data_sources") or ""
    prompt_spec = _evaluation_prompt_spec()
    return json.dumps(
        {
            "task": prompt_spec.get("task", ""),
            "user_requirements": {
                "project_goal": goal,
                "use_case": use_case,
                "data_sources": data_sources,
            },
            "contract": _truncate_contract_for_eval(contract),
            "evaluation_criteria": prompt_spec.get("evaluation_criteria", []),
            "response_format": prompt_spec.get("response_format", {}),
        },
        indent=2,
        sort_keys=True,
    )


def build_clarification_system_prompt(capability_matrix: Mapping[str, Any]) -> str:
    """System prompt for interview planning before contract generation."""
    providers = ", ".join(capability_matrix.get("providers") or [])
    templates = ", ".join(sorted((capability_matrix.get("templates") or {}).keys()))
    fv = _latest_fluid_version()
    return _render_auxiliary_prompt(
        "clarification",
        {
            "fluid_version": fv,
            "providers": providers,
            "templates": templates,
        },
    )


def build_clarification_user_prompt(
    *,
    interview_state: Mapping[str, Any],
    discovery_report: Any,
    capability_matrix: Mapping[str, Any],
    project_memory: Optional[CopilotMemorySnapshot] = None,
    team_memory: Optional[Mapping[str, Any]] = None,
    previous_failure: Sequence[str] | None = None,
) -> str:
    """Build the adaptive interview prompt payload."""
    payload: dict[str, Any] = {
        "interview_state": interview_state,
        "discovery_report": discovery_report.to_prompt_payload(),
        "capability_matrix": capability_matrix,
        "target_slots": [
            "project_goal",
            "use_case",
            "data_sources",
            "provider_hint",
            "domain",
            "canonical_model",
            "supporting_standards",
            "owner_team",
            "build_engine",
            "output_kind",
            "primary_entity",
            "primary_measures",
            "primary_dimensions",
            "time_dimension",
            "time_granularity",
            "refresh_cadence",
            "schedule_engine",
            "trigger_type",
            "consumes",
            "jurisdiction",
            "regulatory_framework",
            "data_sensitivity",
            "ai_access_policy",
        ],
        "priorities": [
            "Ask nothing if current context and discovery are already sufficient.",
            "Prefer semantic intent questions over generic project-management questions.",
            "If use_case is ambiguous, prefer the canonical taxonomy with an Other / Not sure option.",
            "Prefer inferring canonical_model and supporting_standards from domain-specific wording before asking an extra question.",
            "Assume the user may answer with fuzzy wording and use transcript raw_input plus resolved values together.",
            "If there was a generation failure, only ask questions that directly reduce that ambiguity.",
            (
                "If existing_products are listed and the user's project_goal is semantically similar to an existing product, "
                "flag it in your reason field and ask: 'This looks similar to <existing_id>. Are you extending it or creating something new?'"
            ),
            (
                "If the user explicitly wants scheduling or mentions DAGs, infer schedule_engine and trigger_type. "
                "Available schedulers: airflow, dagster, prefect. Default trigger_type is 'cron' for batch workloads. "
                "Do not ask an orchestration question after schedule_engine has already been answered."
            ),
            (
                "If the domain is healthcare, finance, or the user mentions compliance, GDPR, HIPAA, CCPA, or data residency, "
                "ask about jurisdiction and regulatory requirements. Canonical jurisdiction values: EU, US, UK, CA, AU, JP, Global."
            ),
            (
                "If data involves PII, PHI, or financial records, infer data_sensitivity as confidential or restricted "
                "and suggest agentPolicy constraints (canStore=false, deniedUseCases=[training, fine_tuning])."
            ),
        ],
    }
    if team_memory:
        payload["team_memory"] = team_memory
    if project_memory:
        payload["project_memory"] = project_memory.to_prompt_payload()
    if previous_failure:
        payload["previous_failure"] = list(previous_failure)

    # Inject domain-specific context if available in interview state
    interview_ctx = interview_state.get("normalized_context") or interview_state
    domain_expertise = (
        interview_ctx.get("domain_expertise") if isinstance(interview_ctx, dict) else None
    )
    if domain_expertise:
        payload["domain_expertise"] = domain_expertise
        # Surface domain questions as suggested topics
        domain_questions = domain_expertise.get("domain_questions")
        if domain_questions:
            payload["suggested_domain_questions"] = domain_questions

    return json.dumps(payload, indent=2, sort_keys=True)


def build_user_prompt(
    *,
    context: Mapping[str, Any],
    discovery_report: Any,
    capability_matrix: Mapping[str, Any],
    seed_contract: Mapping[str, Any],
    seed_template: str,
    seed_provider: str,
    attempt_index: int,
    previous_errors: Sequence[str],
    previous_payload: Optional[Mapping[str, Any]],
    project_memory: Optional[CopilotMemorySnapshot] = None,
    team_memory: Optional[Mapping[str, Any]] = None,
) -> str:
    """Build the attempt-specific user prompt."""
    interview_summary = _normalize_interview_summary(context)

    # Inject industry skills into the prompt when available.
    # Slice UX-J: prefer the pre-compiled payload (already contains
    # only prompt-relevant fields).  Fall back to the legacy
    # on-the-fly extraction from the raw skills dict for backward
    # compatibility.
    compiled_skills = context.get("compiled_skills")
    skills = context.get("industry_skills")
    if compiled_skills:
        # Pre-compiled payload — already in the right shape for the prompt.
        interview_summary["industry_skills"] = compiled_skills
    elif skills:
        skills_hint: dict[str, Any] = {}
        ind = skills.get("industry", {})
        if ind.get("label"):
            skills_hint["industry"] = ind["label"]
        cm = skills.get("canonical_model", {})
        if cm.get("label"):
            skills_hint["canonical_model"] = cm["label"]
        domains = skills.get("domains")
        if domains:
            skills_hint["domains"] = [d.get("label", d.get("name")) for d in domains]
        compliance = skills.get("compliance")
        if compliance:
            skills_hint["compliance"] = compliance
            skills_hint["requires_sovereignty"] = True
        if skills_hint:
            interview_summary["industry_skills"] = skills_hint

    prompt: dict[str, Any] = {
        "attempt": attempt_index,
        "interview_summary": interview_summary,
        "capability_matrix": capability_matrix,
        "discovery_report": discovery_report.to_prompt_payload(),
        "seed_template": seed_template,
        "seed_provider": seed_provider,
        "seed_contract": seed_contract,
        "response_requirements": {
            "metadata_only_discovery": True,
            "include_additional_files_only_if_needed": True,
            "use_placeholder_env_vars_for_credentials": True,
            "prefer_manual_trigger_for_execute_compatibility": True,
            "generate_sovereignty_when_compliance_detected": True,
            "generate_agent_policy_for_sensitive_data": True,
        },
    }
    # Inject domain expertise (architecture, security, modeling standards) if detected
    domain_expertise = context.get("domain_expertise")
    if domain_expertise:
        prompt["domain_expertise"] = domain_expertise

    # Inject data modeling flag — tells LLM to generate richer semantic blocks + dbt models
    if context.get("data_modeling"):
        prompt["data_modeling_requested"] = True
        prompt["dbt_generation_instructions"] = (
            "Generate dbt model SQL files in additional_files. "
            "Use staging models (stg_ prefix) for source cleanup, "
            "fact tables (fct_ prefix) for events/transactions, "
            "and dimension tables (dim_ prefix) for entities. "
            "Include a schema.yml with column descriptions."
        )

    # Inject upstream product schemas — lets the LLM emit real dbt SQL
    # with correct source identifiers, join keys, and column projections
    # instead of TODO skeletons. Present only when upstream contracts
    # were discovered by ``_create_project_minimal``.
    upstream_products = context.get("upstream_products")
    if upstream_products:
        prompt["upstream_products"] = upstream_products

    # Data-modeling technique guidance. The interview bootstrap always
    # resolves this field to a canonical value (default ``data_vault_2``)
    # so the LLM prompt can unconditionally include the matching naming,
    # key-strategy and load-metadata rules. The guidance text is kept in
    # ``_MODELING_GUIDANCE`` rather than inside the system prompt so it
    # ships under the user prompt where it belongs (per-run context).
    technique = context.get("data_modeling_technique")
    if technique and technique in _MODELING_GUIDANCE:
        prompt["data_modeling_technique"] = technique
        prompt["data_modeling_guidance"] = _MODELING_GUIDANCE[technique]

    if team_memory:
        prompt["team_memory"] = team_memory
    if project_memory:
        prompt["project_memory"] = project_memory.to_prompt_payload()
    if previous_errors:
        prompt["repair_feedback"] = list(previous_errors)
    if previous_payload:
        prompt["previous_response_summary"] = {
            key: value
            for key, value in previous_payload.items()
            if key in {"recommended_template", "recommended_provider", "contract"}
        }
    return json.dumps(prompt, indent=2, sort_keys=True)
