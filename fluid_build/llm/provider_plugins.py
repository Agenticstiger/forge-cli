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

"""Third-party LLM provider plugins (entry-point group ``fluid_build.llm_providers``).

A third-party package advertises a provider in its ``pyproject.toml``::

    [project.entry-points."fluid_build.llm_providers"]
    azure-openai = "fluid_provider_azure_openai:AzureOpenAIProvider"

so ``pip install fluid-provider-azure-openai`` makes
``fluid forge --llm-provider azure-openai`` resolve the plugin-provided
:class:`~fluid_build.llm.providers.LlmProvider` with **no core
edit**. The entry point loads to an ``LlmProvider`` subclass, a zero-arg factory
returning one, or an instance.

Design (mirrors the existing plugin substrate — this is **not** a new mechanism):

* **Discovery** goes through :func:`fluid_build.plugin_manager.iter_plugins`, so
  the operator allow/block policy (``FLUID_PLUGINS_ALLOWLIST`` /
  ``FLUID_PLUGINS_BLOCKLIST``) and the per-plugin fail-isolation (a plugin that
  fails to load is logged **by exception type only** and skipped, never crashing
  the CLI) are shared with every other plugin role. The group is registered in
  ``plugin_manager.EXTRA_GROUPS`` so ``fluid plugins`` lists it too.
* **Laziness / startup budget.** The registry is built on first *query*
  (:func:`get_plugin_registry`), never at import time, so ``fluid --help`` /
  ``build_parser()`` never scan the ``fluid_build.llm_providers`` group. The
  argparse ``choices`` container (:class:`LlmProviderChoices`) only consults the
  registry when a value is actually tested for membership (i.e. when the user
  passes ``--llm-provider <x>``), never when iterating for ``--help``.
* **Precedence: built-ins always win.** A plugin whose name clashes with a
  built-in provider (``openai`` / ``anthropic`` / ``gemini`` / ``ollama`` /
  ``claude`` / ``github`` / ``mcp-sampling`` / the coding agents) is refused
  registration with a warning — it can never silently shadow a built-in.

Security note: an entry-point plugin runs arbitrary code in-process at host
trust — this is the same explicit ``pip install`` trust decision every Python
entry-point plugin system makes (pytest / flake8 / dbt do not sandbox plugins
either). Discovery failures are contained; the operator pins exactly which
plugins load via the allow/block policy above.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Any, Dict, Iterator, List, Optional, Tuple

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fluid_build.llm.providers import LlmProvider

LOG = logging.getLogger("fluid.cli.forge_copilot.llm.plugins")

# The entry-point group a third-party package advertises. Governed by the
# unified plugin manager (allow/block policy + fail-isolation) via its
# registration in ``plugin_manager.EXTRA_GROUPS``.
LLM_PROVIDER_GROUP = "fluid_build.llm_providers"

# The built-in provider names shown in ``--help`` and accepted eagerly by the
# argparse ``choices`` container. Kept as a single source of truth so forge.py
# and forge_data_model.py don't duplicate the literal list.
BUILTIN_PROVIDER_CHOICES: Tuple[str, ...] = (
    "openai",
    "anthropic",
    "claude",
    "gemini",
    "ollama",
    # Keyless: route the LLM through the IDE (mcp-sampling) or a local
    # coding-agent CLI instead of forge's own API key.
    "mcp-sampling",
    "claude-code",
    "codex",
    "cursor",
    "kiro",
)


_registry_lock = threading.Lock()
_registry: Optional[Dict[str, "LlmProvider"]] = None


def _reserved_builtin_names() -> frozenset:
    """Return every provider name a plugin is forbidden from shadowing.

    Derived from the live built-in registry (so a future built-in is covered
    automatically) plus ``github`` and the ``mcp-sampling`` aliases, which are
    resolved by dedicated code paths rather than the ``BUILTIN_LLM_PROVIDERS``
    dict. Reads keys only — never instantiates a provider.
    """
    from fluid_build.llm.providers import BUILTIN_LLM_PROVIDERS

    names = {str(k).strip().lower() for k in BUILTIN_LLM_PROVIDERS.keys()}
    names |= {"github", "mcp-sampling", "mcp_sampling"}
    return frozenset(names)


def _coerce_to_provider(obj: Any) -> "LlmProvider":
    """Turn a loaded entry-point object into an :class:`LlmProvider` instance.

    Accepts an ``LlmProvider`` subclass (instantiated with no args), a zero-arg
    factory callable returning one, or an already-constructed instance. Raises
    :class:`TypeError` if the result is not an ``LlmProvider`` — the caller
    fail-isolates and skips the plugin.
    """
    from fluid_build.llm.providers import LlmProvider

    if isinstance(obj, LlmProvider):
        candidate: Any = obj
    elif isinstance(obj, type):
        candidate = obj()  # class -> instantiate
    elif callable(obj):
        candidate = obj()  # factory -> call
    else:
        candidate = obj
    if not isinstance(candidate, LlmProvider):
        raise TypeError(f"{type(candidate).__name__!r} is not an LlmProvider subclass")
    return candidate


def _build_registry() -> Dict[str, "LlmProvider"]:
    """Discover + validate every allowed ``fluid_build.llm_providers`` plugin.

    Precedence: a plugin whose entry-point name (or declared ``provider.name``)
    collides with a built-in is skipped with a warning — built-ins always win.
    Per-plugin fail-isolation is provided by :func:`plugin_manager.iter_plugins`
    (load errors) plus the local coercion guard (a loaded object that isn't an
    ``LlmProvider``); one bad plugin never drops the others and this never raises.
    """
    from fluid_build.plugin_manager import iter_plugins

    reserved = _reserved_builtin_names()
    registry: Dict[str, "LlmProvider"] = {}
    for ep_name, obj in iter_plugins(LLM_PROVIDER_GROUP, logger=LOG):
        key = str(ep_name).strip().lower()
        if key in reserved:
            LOG.warning(
                "llm provider plugin %r shadows a built-in provider name; ignoring "
                "(built-ins always win)",
                ep_name,
            )
            continue
        try:
            provider = _coerce_to_provider(obj)
        except Exception as exc:  # noqa: BLE001 - isolate a bad plugin, log by type only
            LOG.warning(
                "llm provider plugin %r is not a valid LlmProvider: %s",
                ep_name,
                type(exc).__name__,
            )
            continue
        registry[key] = provider
        # Also index under the provider's declared ``.name`` when it differs from
        # the entry-point name (and doesn't clash with a built-in / existing key),
        # so a lookup by either resolves the same instance.
        pname = str(getattr(provider, "name", "") or "").strip().lower()
        if pname and pname != key and pname not in reserved and pname not in registry:
            registry[pname] = provider
    return registry


def get_plugin_registry() -> Dict[str, "LlmProvider"]:
    """Return the discovered plugin registry, building it once (thread-safe).

    Lazy: the first call triggers entry-point discovery. Returns a shallow copy
    so callers cannot mutate the cached registry.
    """
    global _registry  # noqa: PLW0603
    if _registry is None:
        with _registry_lock:
            if _registry is None:
                _registry = _build_registry()
    return dict(_registry)


def _lookup_keys(name: str) -> Iterator[str]:
    """Yield candidate registry keys for ``name`` (hyphen/underscore tolerant)."""
    key = (name or "").strip().lower()
    if not key:
        return
    yield key
    if "-" in key:
        yield key.replace("-", "_")
    if "_" in key:
        yield key.replace("_", "-")


def get_plugin_llm_provider(name: str) -> Optional["LlmProvider"]:
    """Return the plugin-provided provider for ``name``, or ``None``.

    Hyphen/underscore variants of ``name`` are tried so an entry point named
    ``azure-openai`` resolves for ``azure_openai`` too.
    """
    registry = get_plugin_registry()
    for key in _lookup_keys(name):
        provider = registry.get(key)
        if provider is not None:
            return provider
    return None


def is_plugin_provider(name: str) -> bool:
    """Return whether ``name`` resolves to a discovered plugin provider."""
    return get_plugin_llm_provider(name) is not None


def discovered_plugin_names() -> List[str]:
    """Return the sorted names of all discovered plugin providers (for doctor)."""
    return sorted(get_plugin_registry().keys())


def reset_plugin_registry() -> None:
    """Clear the cached registry so the next query re-discovers entry points.

    Wired into :func:`forge_copilot_llm_providers.reset_llm_caches`; also used by
    tests to isolate the module-level cache between cases.
    """
    global _registry  # noqa: PLW0603
    with _registry_lock:
        _registry = None


class LlmProviderChoices:
    """argparse ``choices`` accepting built-ins eagerly and plugins lazily.

    *Iteration* (used only when argparse formats ``--help`` or an
    invalid-choice error message) yields the fixed built-in names, so neither
    ``fluid --help`` nor ``fluid forge --help`` scans entry points — the
    startup-budget invariant is preserved.

    *Membership* (``in`` — argparse's ``_check_value`` calls it only when the
    user actually passes ``--llm-provider <x>``) additionally consults the
    lazily-built plugin registry, so a ``pip install``-ed provider plugin is an
    accepted value without any core edit. Built-ins always win a name clash
    (they are matched first and can never be shadowed in the registry).
    """

    __slots__ = ()

    def __iter__(self) -> Iterator[str]:
        return iter(BUILTIN_PROVIDER_CHOICES)

    def __contains__(self, value: object) -> bool:
        key = str(value or "").strip().lower()
        if key in BUILTIN_PROVIDER_CHOICES:
            return True
        try:
            return is_plugin_provider(key)
        except Exception:  # noqa: BLE001 - discovery must never break arg parsing
            return False

    def __repr__(self) -> str:
        return "LlmProviderChoices" + repr(BUILTIN_PROVIDER_CHOICES)


__all__ = [
    "LLM_PROVIDER_GROUP",
    "BUILTIN_PROVIDER_CHOICES",
    "LlmProviderChoices",
    "discovered_plugin_names",
    "get_plugin_llm_provider",
    "get_plugin_registry",
    "is_plugin_provider",
    "reset_plugin_registry",
]
