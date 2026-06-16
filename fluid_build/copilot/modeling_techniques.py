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

"""Pluggable modeling-technique registry (issue #248).

``--modeling-technique`` used to be a closed argparse ``choices`` enum
(``data_vault_2`` / ``data-vault-2`` / ``dimensional``) with the dispatch
hardcoded as ``if technique == "data_vault_2" ... else dimensional`` inside the
modeler agent — so a curated logical model could not be driven through
verbatim, source-aligned products were forced into a vault/dimensional shape,
and an organisation-specific technique (e.g. anchor modeling) couldn't be added
without forking.

This registry makes techniques pluggable, mirroring ``fluid_build.providers``
and ``fluid_build.source_adapters``: built-ins merge with plugins discovered
from the ``fluid_build.modeling_techniques`` entry-point group. A
:class:`ModelingTechnique` is a small declarative spec — the modeler agent and
the contract emitter read its fields instead of branching on the name string:

* ``branch`` — which ``LogicalDraft`` branch the technique fills:
  ``"dv2"`` (Data Vault 2.0), ``"dimensional"`` (Kimball), or ``None``
  (source-aligned ``flat`` / bring-your-own ``custom`` — neither branch).
* ``requires_logical_model`` — ``True`` for ``custom``: the modeler is skipped
  and a user-supplied logical model (``--logical-model <path>``) is used
  verbatim.
* ``llm_fragment`` — the prompt fragment for the LLM modeling path, or ``None``
  for deterministic techniques (``flat`` / ``custom``) that skip the LLM.

Design constraints match the other plugin surfaces: lightweight (stdlib only at
import, so the ``LogicalDraft`` schema validator can import it without a
cycle), fail-open discovery, built-ins win on name collision, lazy plugin load.

A plugin registers in its own ``pyproject.toml``::

    [project.entry-points."fluid_build.modeling_techniques"]
    anchor = "my_pkg.techniques:ANCHOR_MODELING"

where ``ANCHOR_MODELING`` is a :class:`ModelingTechnique` instance.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional

LOG = logging.getLogger("fluid.modeling_techniques")

EP_GROUP = "fluid_build.modeling_techniques"


@dataclass(frozen=True)
class ModelingTechnique:
    """Declarative spec for one modeling technique.

    ``name`` is the canonical value stored on ``LogicalDraft.technique`` and
    accepted by ``--modeling-technique``. ``aliases`` are extra accepted spellings
    (e.g. ``data-vault-2``) normalized to ``name``.
    """

    name: str
    description: str = ""
    aliases: tuple = ()
    branch: Optional[str] = None  # "dv2" | "dimensional" | None
    requires_logical_model: bool = False
    llm_fragment: Optional[str] = None
    origin: str = "builtin"

    @property
    def uses_llm(self) -> bool:
        """Deterministic techniques (no LLM fragment) skip the LLM modeling path."""
        return self.llm_fragment is not None


# ── Built-in techniques ──────────────────────────────────────────────────
_BUILTINS: tuple = (
    ModelingTechnique(
        name="data_vault_2",
        description="Data Vault 2.0 (hubs, links, satellites).",
        aliases=("data-vault-2", "datavault2", "dv2"),
        branch="dv2",
        llm_fragment="fragments/dv2.yaml",
    ),
    ModelingTechnique(
        name="dimensional",
        description="Dimensional / Kimball (facts + dimensions).",
        aliases=("kimball", "star"),
        branch="dimensional",
        llm_fragment="fragments/dimensional.yaml",
    ),
    ModelingTechnique(
        name="flat",
        description="Source-aligned 1:1 — one dataset per source table, no reshaping.",
        aliases=("source-aligned", "source_aligned", "raw", "passthrough"),
        branch=None,
        llm_fragment=None,  # deterministic — no LLM reshaping
    ),
    ModelingTechnique(
        name="custom",
        description="Bring-your-own logical model — used verbatim via --logical-model.",
        aliases=("byo", "as-is", "as_is"),
        branch=None,
        requires_logical_model=True,
        llm_fragment=None,
    ),
)

_REGISTRY: Dict[str, ModelingTechnique] = {}
_ALIASES: Dict[str, str] = {}
_discovered = False


def _index(tech: ModelingTechnique) -> None:
    _REGISTRY[tech.name] = tech
    for alias in tech.aliases:
        _ALIASES[alias.replace("-", "_").lower()] = tech.name


def _seed_builtins() -> None:
    for tech in _BUILTINS:
        _index(tech)


def _discover_entrypoints(logger: Optional[logging.Logger]) -> None:
    """Merge ``fluid_build.modeling_techniques`` plugins. Mirrors the other
    plugin surfaces: tolerant of the <3.10 / >=3.10 entry_points APIs, fail-open
    on discovery errors, per-plugin try/except, built-ins not overridden."""
    log = logger or LOG
    try:
        import importlib.metadata as md

        all_eps = md.entry_points()
        eps = (
            all_eps.select(group=EP_GROUP)
            if hasattr(all_eps, "select")
            else all_eps.get(EP_GROUP, [])
        )
    except Exception as exc:  # noqa: BLE001 — fail open
        log.warning("modeling-technique discovery failed: %s", exc)
        return

    for ep in eps:
        name = getattr(ep, "name", None)
        if not name:
            continue
        if name in _REGISTRY and _REGISTRY[name].origin == "builtin":
            log.warning(
                "modeling-technique plugin %r shadows a built-in; keeping the built-in", name
            )
            continue
        try:
            obj = ep.load()
        except Exception as exc:  # noqa: BLE001 — one broken plugin must not break the CLI
            log.warning("modeling-technique plugin %r failed to load: %s", name, exc)
            continue
        if not isinstance(obj, ModelingTechnique):
            log.warning(
                "modeling-technique plugin %r did not return a ModelingTechnique; skipping", name
            )
            continue
        # Force name/origin to the registered ep name so a plugin can't lie.
        tech = ModelingTechnique(
            name=name,
            description=obj.description,
            aliases=obj.aliases,
            branch=obj.branch,
            requires_logical_model=obj.requires_logical_model,
            llm_fragment=obj.llm_fragment,
            origin="entrypoint",
        )
        _index(tech)


def discover_modeling_techniques(
    logger: Optional[logging.Logger] = None, *, force: bool = False
) -> None:
    """Populate the registry: built-ins first, then entry-point plugins.
    Idempotent unless ``force=True``."""
    global _discovered
    if _discovered and not force:
        return
    _REGISTRY.clear()
    _ALIASES.clear()
    _seed_builtins()
    _discover_entrypoints(logger)
    _discovered = True


def _ensure_discovered() -> None:
    if not _discovered:
        discover_modeling_techniques()


def normalize_technique(value: Optional[str]) -> Optional[str]:
    """Resolve an alias/spelling to the canonical technique name, or ``None``
    for a falsy input. Unknown values pass through unchanged so the caller's
    own validation produces the error."""
    if not value:
        return None
    _ensure_discovered()
    key = str(value).replace("-", "_").strip().lower()
    if key in _REGISTRY:
        return key
    return _ALIASES.get(key, value)


def list_modeling_techniques() -> List[str]:
    """Canonical technique names (built-in + plugin), sorted."""
    _ensure_discovered()
    return sorted(_REGISTRY)


def list_technique_choices() -> List[str]:
    """Canonical names + aliases, for argparse ``choices`` (so the documented
    ``data-vault-2`` spelling keeps working)."""
    _ensure_discovered()
    return sorted(set(_REGISTRY) | set(_ALIASES))


def get_modeling_technique(name: Optional[str]) -> Optional[ModelingTechnique]:
    """Look up a technique by canonical name or alias; ``None`` if unknown."""
    if not name:
        return None
    _ensure_discovered()
    canonical = normalize_technique(name)
    return _REGISTRY.get(canonical) if canonical else None
