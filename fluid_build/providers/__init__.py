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

# fluid_build/providers/__init__.py
"""
Provider registry & discovery for FLUID Build (production-ready).

Key features
------------
- Single source of truth: PROVIDERS dict (name -> class or factory).
- Safe, idempotent discovery (re-entrant, thread-safe, 'force' refresh).
- Multiple auto-registration strategies per submodule:
    1) Explicit self-registration (preferred):
         from fluid_build.providers import register_provider
         register_provider("gcp", GcpProvider)
    2) PROVIDERS map exported by module: {"local": LocalProvider, ...}
    3) NAME="local" and Provider=<class>
    4) (bonus) Exactly one subclass of BaseProvider found => auto-register.
- Structured logging (no LogRecord key collisions).
- Diagnostics snapshot for scripts.

Conventions
-----------
- Provider names: lowercase letters, digits, underscore. Invalid names are rejected.
- Duplicate registration: the FIRST one wins (unless override=True).
- Scans subpackages under fluid_build.providers.*, skipping 'base' and itself.
- You can constrain discovery with FLUID_PROVIDERS env var
  (comma-separated module names, e.g. "fluid_build.providers.local,fluid_build.providers.opds").
"""

from __future__ import annotations

import importlib
import importlib.metadata
import logging
import os
import pkgutil
import re
import sys
import threading
import warnings
from dataclasses import dataclass
from inspect import isclass
from typing import Any, Dict, List, Optional, Tuple, Type

# Public, process-wide registry (name -> provider class or factory)
PROVIDERS: Dict[str, Any] = {}

# Collect discovery errors for diagnostics/UIs
DISCOVERY_ERRORS: List[Dict[str, str]] = []

# Internal guard/flags
_LOCK = threading.RLock()
_DISCOVERY_DONE = False
_DISCOVERY_ATTEMPTS = 0

# Acceptable provider key: lower, digits, underscore
_PROVIDER_NAME_RE = re.compile(r"^[a-z0-9_]+$")

# Default logger for registry-level messages if none provided by caller
_log = logging.getLogger("fluid.providers")


# ------------------------------- Utilities --------------------------------- #


def _safe_log(logger: Optional[logging.Logger], level: int, msg: str, **fields: Any) -> None:
    """
    Log with safe 'extra' keys; avoid reserved LogRecord attributes (e.g., 'module').
    """
    lg = logger or _log
    extra = {"evt": msg}  # short, fixed field to identify event
    for k, v in fields.items():
        if k in {"module", "message", "args", "levelname", "name"}:
            extra[f"_{k}"] = v
        else:
            extra[k] = v
    try:
        lg.log(level, msg, extra=extra)
    except Exception:
        lg.log(level, f"{msg} | {extra}")


def _is_valid_name(name: str) -> bool:
    return bool(_PROVIDER_NAME_RE.match(name))


def _normalize_name(name: str) -> str:
    return name.strip().lower().replace("-", "_")


def _add_discovery_error(source: str, modname: str, exc: BaseException) -> None:
    # DISCOVERY_ERRORS is surfaced to users via registry_dump() / `fluid providers
    # --debug`, so it must NOT carry raw exception text or a full traceback — either
    # can embed a secret (a credential in a message, a value off the stack). Record
    # the exception TYPE only; the full, redaction-filtered detail still reaches the
    # DEBUG logs at each call site.
    DISCOVERY_ERRORS.append(
        {
            "source": source,
            "modname": modname,
            "error": exc.__class__.__name__,
        }
    )


# ----------------------------- Public API ---------------------------------- #

# CLI version for plugin↔CLI protocol compatibility checks. Sourced from the
# installed distribution metadata (never a stale hardcoded constant); the
# fallback is used only when the package isn't installed as a distribution
# (e.g. running straight from a source tree without `pip install`).
_CLI_VERSION_FALLBACK = "0.7.1"


def _detect_cli_version() -> str:
    """Resolve the installed ``data-product-forge`` version (PEP 440)."""
    try:
        return importlib.metadata.version("data-product-forge")
    except Exception:
        return _CLI_VERSION_FALLBACK


_CLI_VERSION = _detect_cli_version()


def _strict_compat() -> bool:
    """True when ``FLUID_PLUGIN_STRICT_COMPAT`` opts into hard-failing on a mismatch."""
    return os.environ.get("FLUID_PLUGIN_STRICT_COMPAT", "").strip().lower() in {"1", "true", "yes"}


def _spec_satisfied(cli_version: str, specifier: Optional[str]) -> Optional[bool]:
    """Whether ``cli_version`` satisfies a PEP 440 ``specifier``; ``None`` if uncheckable.

    Uses ``packaging`` when available (the precise, canonical check) and returns
    ``None`` (defer to legacy bounds) when it isn't installed or the specifier
    is malformed — so a missing optional dependency never blocks a plugin.
    """
    if not specifier:
        return None
    try:
        from packaging.specifiers import SpecifierSet
        from packaging.version import Version

        # prereleases=True: a dev/rc build of the CLI (e.g. "0.9.1.dev3") still
        # counts as satisfying ">=0.7" — its own pre-release status must not
        # disqualify it from a plugin's compatibility window.
        return SpecifierSet(specifier).contains(Version(cli_version), prereleases=True)
    except Exception:
        return None


def _parse_version(v: str) -> Tuple[int, ...]:
    """Parse a dotted version string into a tuple of ints.  Handles '1.x' as (1, 999).
    Always returns at least 3 components, padding with 0."""
    parts = []
    for p in v.strip().split(".")[:3]:
        if p.lower() == "x":
            parts.append(999)
        else:
            try:
                parts.append(int(p))
            except ValueError:
                parts.append(0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


def _check_sdk_compat(name: str, provider: Any, logger: Optional[logging.Logger]) -> bool:
    """Return True if ``provider`` is compatible with the running CLI (or unknown).

    Declare-and-gate (the SDK declares, the CLI gates), honouring two styles:

    * **New SDK** (``data-product-forge-sdk`` / import ``fluid_sdk``): a plugin's
      ``get_plugin_info().requires_cli`` is a PEP 440 specifier; the running CLI
      version is checked against it (``packaging.SpecifierSet`` when available).
    * **Legacy**: ``get_provider_info().sdk_version`` plus ``MIN_CLI_VERSION`` /
      ``MAX_CLI_VERSION`` read off the provider's own SDK module (or ``fluid_sdk``).

    Advisory by default — an incompatible provider still registers and a
    ``provider_version_warning`` is logged. ``FLUID_PLUGIN_STRICT_COMPAT=1`` makes
    :func:`register_provider` reject incompatible plugins instead. Never raises;
    fails open (returns True) on any internal error so the check can't block a
    plugin because of its own bug.
    """
    try:
        # ── New-SDK declaration: PluginMetadata.requires_cli (PEP 440) ──
        info_fn = getattr(provider, "get_plugin_info", None)
        if callable(info_fn):
            try:
                requires = getattr(info_fn(), "requires_cli", None)
            except Exception:
                requires = None
            sat = _spec_satisfied(_CLI_VERSION, requires)
            if sat is False:
                _safe_log(
                    logger,
                    logging.WARNING,
                    "provider_version_warning",
                    name=name,
                    cli_version=_CLI_VERSION,
                    requires_cli=requires,
                    hint=f"Provider '{name}' requires CLI {requires}",
                )
                return False
            if sat is True:
                return True
            # sat is None → specifier absent/uncheckable; fall through to legacy.

        # ── Legacy declaration: ProviderMetadata.sdk_version + MIN/MAX ──
        if not hasattr(provider, "get_provider_info"):
            return True
        info = provider.get_provider_info()
        if not info:
            return True
        sdk_ver = getattr(info, "sdk_version", None)
        if not sdk_ver or sdk_ver == "0.0.0":
            return True  # no declared version — nothing to gate

        provider_mod = getattr(provider, "__module__", "") or ""
        min_v = max_v = None
        try:
            provider_pkg = provider_mod.rsplit(".", 1)[0] if "." in provider_mod else provider_mod
            sdk_mod = importlib.import_module(provider_pkg)
            min_v = getattr(sdk_mod, "MIN_CLI_VERSION", None)
            max_v = getattr(sdk_mod, "MAX_CLI_VERSION", None)
        except Exception:
            pass
        if not min_v:
            try:
                import fluid_sdk as _sdk  # the live SDK (was the renamed-away fluid_provider_sdk)

                min_v = getattr(_sdk, "MIN_CLI_VERSION", None)
                max_v = getattr(_sdk, "MAX_CLI_VERSION", None)
            except ImportError:
                return True  # SDK not installed alongside the CLI — can't gate

        cli_t = _parse_version(_CLI_VERSION)
        compatible = True
        if min_v and cli_t < _parse_version(min_v):
            compatible = False
            _safe_log(
                logger,
                logging.WARNING,
                "provider_version_warning",
                name=name,
                cli_version=_CLI_VERSION,
                min_cli_version=min_v,
                hint=f"Provider '{name}' requires CLI >= {min_v}",
            )
        if max_v and cli_t > _parse_version(max_v):
            compatible = False
            _safe_log(
                logger,
                logging.WARNING,
                "provider_version_warning",
                name=name,
                cli_version=_CLI_VERSION,
                max_cli_version=max_v,
                hint=f"Provider '{name}' requires CLI <= {max_v}",
            )
        return compatible
    except Exception:
        return True  # fail open — the compat check never blocks on its own bug


# Track simple meta for debugging: name -> {module, qualname, source}
_REGISTRY_META: Dict[str, Dict[str, str]] = {}
_BANNED_NAMES = {"unknown", "stub", ""}


def register_provider(
    name: str,
    provider: Any,
    *,
    override: bool = False,
    logger: Optional[logging.Logger] = None,
    source: str = "explicit",
) -> None:
    """
    Register a provider implementation under a canonical name.

    - Reject ambiguous names ('unknown', 'stub', empty)
    - Store module/class meta to aid 'fluid providers --debug'
    """
    if provider is None:
        raise ValueError("provider must not be None")

    cname = _normalize_name(name)
    if not _is_valid_name(cname):
        raise ValueError(
            f"Invalid provider name '{name}'. Use lowercase letters, digits or underscore."
        )
    if cname in _BANNED_NAMES:
        _safe_log(
            logger,
            logging.DEBUG,
            "provider_name_rejected",
            name=cname,
            reason="banned_name",
            source=source,
        )
        return

    # Operator allow/block policy, enforced at the single registration
    # chokepoint rather than only in ``_discover_entrypoints``. The
    # entry-point walk gated correctly, but ``_preload_curated`` and
    # ``_discover_subpackages`` import the in-tree provider modules directly
    # and those self-register here — so a built-in slipped past the policy.
    # ``fluid plugins --role provider`` meanwhile rendered it as
    # ``BLOCKED (allow/block policy)`` off the same name, i.e. the control
    # displayed as enforced while being inert: with
    # ``FLUID_PLUGINS_BLOCKLIST=snowflake`` set, a real Snowflake apply
    # provisioned tables. A security control that renders as enforced while
    # doing nothing is worse than no control, so the gate now holds for
    # every registration source.
    from fluid_build.plugin_manager import is_allowed

    if not is_allowed(cname):
        _safe_log(
            logger,
            logging.DEBUG,
            "provider_name_rejected",
            name=cname,
            reason="allow_block_policy",
            source=source,
        )
        return

    # Plugin↔CLI compatibility gate (read-only, outside the lock). Advisory by
    # default; FLUID_PLUGIN_STRICT_COMPAT=1 rejects an incompatible plugin.
    compatible = _check_sdk_compat(cname, provider, logger)
    if not compatible and _strict_compat():
        _safe_log(
            logger,
            logging.ERROR,
            "provider_compat_rejected",
            name=cname,
            source=source,
            cli_version=_CLI_VERSION,
        )
        return

    with _LOCK:
        exists = cname in PROVIDERS
        if exists and not override:
            _safe_log(
                logger, logging.DEBUG, "provider_duplicate_ignored", name=cname, source=source
            )
            return

        PROVIDERS[cname] = provider
        # capture meta
        mod = getattr(provider, "__module__", "<unknown>")
        qual = getattr(provider, "__qualname__", repr(provider))
        _REGISTRY_META[cname] = {"module": mod, "qualname": qual, "source": source}
        _safe_log(
            logger,
            logging.DEBUG,
            "provider_registered_explicit",
            name=cname,
            provider=f"{mod}:{qual}",
            source=source,
        )


def registry_dump() -> Dict[str, Any]:
    """Return registry + meta for diagnostics."""
    with _LOCK:
        return {
            "providers": list_providers(),
            "meta": dict(_REGISTRY_META),
            "discovery_errors": list(DISCOVERY_ERRORS),
            "discovery_done": _DISCOVERY_DONE,
            "discovery_attempts": _DISCOVERY_ATTEMPTS,
        }


def list_providers() -> List[str]:
    """Return a sorted list of registered provider names."""
    with _LOCK:
        return sorted(PROVIDERS.keys())


def get_provider(name: str) -> Any:
    """
    Lookup a provider by name. Raises KeyError if not registered.
    """
    cname = _normalize_name(name)
    with _LOCK:
        if cname not in PROVIDERS:
            raise KeyError(f"Unknown provider '{name}'. Available: {sorted(PROVIDERS.keys())}")
        return PROVIDERS[cname]


def clear_providers() -> None:
    """
    Clear the registry (useful in tests). Also resets discovery flags.
    """
    global _DISCOVERY_DONE, _DISCOVERY_ATTEMPTS
    with _LOCK:
        PROVIDERS.clear()
        DISCOVERY_ERRORS.clear()
        _DISCOVERY_DONE = False
        _DISCOVERY_ATTEMPTS = 0


# -------------------------- Discovery Orchestration ------------------------ #

_DEFAULT_MODULES = (
    "fluid_build.providers.local",
    "fluid_build.providers.gcp",
    "fluid_build.providers.aws",
    "fluid_build.providers.snowflake",
    # odps is a spec-export format, not a cloud provider — intentionally excluded
    # from provider preload/discovery (see providers/opds/__init__.py).
)


def discover_providers(logger: Optional[logging.Logger] = None, *, force: bool = False) -> None:
    """
    Import provider subpackages and auto-register implementations.

    Discovery order:
      0) ``fluid_build.providers`` entry-points (pip-installed plugins).
      1) Curated / default built-in modules.
      2) pkgutil scan of ``fluid_build.providers.*`` subpackages.
      3) Fallback best-effort (if registry still empty).

    Idempotent. If force=True, re-attempts even if discovery was previously marked done.
    Honors FLUID_PROVIDERS="mod1,mod2" to constrain imports.
    """
    global _DISCOVERY_DONE, _DISCOVERY_ATTEMPTS

    with _LOCK:
        _DISCOVERY_ATTEMPTS += 1

        if _DISCOVERY_DONE and PROVIDERS and not force:
            _safe_log(
                logger,
                logging.DEBUG,
                "provider_discovery_short_circuit",
                attempts=_DISCOVERY_ATTEMPTS,
                count=len(PROVIDERS),
            )
            return

        # 0) entry-point plugins (third-party packages)
        _discover_entrypoints(logger)

        # 1) preload curated/default modules (soft-fail)
        _preload_curated(logger)

        # 2) iterate all submodules in this package (soft-fail)
        _discover_subpackages(logger)

        # 3) If still empty, fallback (best-effort)
        if not PROVIDERS:
            _fallback_registers(logger)

        _DISCOVERY_DONE = True
        _safe_log(
            logger,
            logging.DEBUG,
            "provider_discovery_complete",
            count=len(PROVIDERS),
            errors=len(DISCOVERY_ERRORS),
        )


def _discover_entrypoints(logger: Optional[logging.Logger]) -> None:
    """Discover third-party providers via ``fluid_build.providers`` entry-points.

    Uses :func:`importlib.metadata.entry_points` so any pip-installed package
    that declares::

        [project.entry-points."fluid_build.providers"]
        mycloud = "my_package.provider:MyCloudProvider"

    will be picked up automatically at discovery time.
    """
    EP_GROUP = "fluid_build.providers"
    try:
        # Python >=3.12 returns SelectableGroups; 3.10-3.11 returns dict
        all_eps = importlib.metadata.entry_points()
        if isinstance(all_eps, dict):
            eps = all_eps.get(EP_GROUP, [])
        else:
            # SelectableGroups (3.12+) or importlib_metadata backport
            eps = (
                all_eps.select(group=EP_GROUP)
                if hasattr(all_eps, "select")
                else all_eps.get(EP_GROUP, [])
            )
    except Exception as exc:
        _safe_log(logger, logging.DEBUG, "entrypoint_discovery_unavailable", error=str(exc))
        return

    # Gate provider entry-points through the unified operator allow/block policy
    # (FLUID_PLUGINS_ALLOWLIST / FLUID_PLUGINS_BLOCKLIST), so the SAME control that
    # governs validators / catalog / iac plugins also governs providers — the
    # highest-stakes role (it emits cloud DDL / IaC). Imported lazily to avoid any
    # import-time coupling during early provider bootstrap.
    from fluid_build.plugin_manager import is_allowed

    for ep in eps:
        if not is_allowed(ep.name):
            _safe_log(
                logger,
                logging.DEBUG,
                "provider_entrypoint_skipped",
                name=ep.name,
                reason="allow_block_policy",
            )
            continue
        try:
            provider_cls = ep.load()
            register_provider(
                ep.name, provider_cls, override=False, logger=logger, source="entrypoint"
            )
            _safe_log(
                logger,
                logging.DEBUG,
                "provider_entrypoint_loaded",
                name=ep.name,
                entrypoint=str(ep),
            )
        except Exception as exc:
            # Type-only — never interpolate the raw exception text (matches the
            # unified manager's posture and the DISCOVERY_ERRORS redaction above).
            _safe_log(
                logger,
                logging.WARNING,
                "provider_entrypoint_failed",
                name=ep.name,
                error=type(exc).__name__,
            )
            _add_discovery_error("entrypoint", ep.name, exc)


def _preload_curated(logger: Optional[logging.Logger]) -> None:
    env = (os.getenv("FLUID_PROVIDERS") or "").strip()
    candidates = [m.strip() for m in env.split(",") if m.strip()] if env else list(_DEFAULT_MODULES)
    for modname in candidates:
        try:
            importlib.import_module(modname)
            _safe_log(logger, logging.DEBUG, "provider_module_imported", modname=modname)
        except Exception as exc:
            _safe_log(
                logger,
                logging.DEBUG,
                "provider_module_import_failed",
                modname=modname,
                error=str(exc),
            )
            _add_discovery_error("default", modname, exc)


def _discover_subpackages(logger: Optional[logging.Logger]) -> None:
    """Import all subpackages under fluid_build.providers.* and try auto-registration."""
    try:
        pkg = importlib.import_module("fluid_build.providers")
    except Exception as exc:
        _safe_log(logger, logging.ERROR, "providers_package_import_failed", error=str(exc))
        _add_discovery_error("package", "fluid_build.providers", exc)
        return

    for modinfo in pkgutil.iter_modules(getattr(pkg, "__path__", []), pkg.__name__ + "."):
        modname = modinfo.name
        short = modname.rsplit(".", 1)[-1]
        if short in {"__init__", "base"}:
            continue  # skip non-providers

        try:
            mod = importlib.import_module(modname)
            _safe_log(logger, logging.DEBUG, "provider_module_imported", modname=modname)
        except Exception as exc:
            _safe_log(
                logger,
                logging.DEBUG,
                "provider_module_import_failed",
                modname=modname,
                error=str(exc),
            )
            _add_discovery_error("subpackage", modname, exc)
            continue

        # If module already self-registered in import, we still attempt auto paths,
        # but duplicates will be ignored (with a warning) unless override=True.
        _auto_register_from_module(mod, logger)


def _auto_register_from_module(mod, logger: Optional[logging.Logger]) -> None:
    """Try the three passive strategies + a single-subclass fallback.

    A module can opt OUT of all passive auto-registration by setting
    ``__fluid_no_autoregister__ = True``. This is how a package that exposes a
    ``BaseProvider`` subclass for direct import — but is a spec EXPORTER, not a
    deployment provider (e.g. odcs) — keeps the class importable without the
    single-subclass fallback silently re-registering it in the provider registry.
    """
    if getattr(mod, "__fluid_no_autoregister__", False):
        return

    # Strategy 1: PROVIDERS dict
    providers_map = getattr(mod, "PROVIDERS", None)
    if isinstance(providers_map, dict) and providers_map:
        for name, prov in list(providers_map.items()):
            try:
                register_provider(name, prov, override=False, logger=logger)
            except Exception as exc:
                _safe_log(
                    logger,
                    logging.WARNING,
                    "provider_auto_register_failed",
                    modname=getattr(mod, "__name__", "<unknown>"),
                    name=str(name),
                    error=str(exc),
                )
                _add_discovery_error("auto_map", getattr(mod, "__name__", "?"), exc)

    # Strategy 2: NAME + Provider
    name = getattr(mod, "NAME", None)
    prov = getattr(mod, "Provider", None)
    if isinstance(name, str) and prov is not None:
        try:
            register_provider(name, prov, override=False, logger=logger)
            _safe_log(
                logger,
                logging.DEBUG,
                "provider_registered_auto",
                modname=getattr(mod, "__name__", "<unknown>"),
                name=_normalize_name(name),
            )
            return
        except Exception as exc:
            _safe_log(
                logger,
                logging.WARNING,
                "provider_auto_register_failed",
                modname=getattr(mod, "__name__", "<unknown>"),
                name=str(name),
                error=str(exc),
            )
            _add_discovery_error("auto_name", getattr(mod, "__name__", "?"), exc)

    # Strategy 3: scan for exactly one subclass of BaseProvider
    try:
        from .base import BaseProvider  # local import to avoid circulars

        discovered: List[Type[BaseProvider]] = []
        for attr_name in dir(mod):
            attr = getattr(mod, attr_name, None)
            if isclass(attr) and issubclass(attr, BaseProvider) and attr is not BaseProvider:
                discovered.append(attr)  # type: ignore[misc]
        if len(discovered) == 1:
            cls = discovered[0]
            inferred = _normalize_name(getattr(cls, "name", cls.__name__).replace("Provider", ""))
            if not _is_valid_name(inferred):
                inferred = _normalize_name(getattr(mod, "__name__", "provider").rsplit(".", 1)[-1])
            register_provider(inferred, cls, override=False, logger=logger)
            _safe_log(
                logger,
                logging.DEBUG,
                "provider_registered_by_subclass",
                modname=getattr(mod, "__name__", "<unknown>"),
                name=inferred,
                provider=cls.__name__,
            )
    except Exception as exc:
        _safe_log(
            logger,
            logging.DEBUG,
            "provider_single_subclass_scan_failed",
            modname=getattr(mod, "__name__", "<unknown>"),
            error=str(exc),
        )


def _fallback_registers(logger: Optional[logging.Logger]) -> None:
    """Best-effort fallback imports if discovery yielded nothing."""
    candidates = list(_DEFAULT_MODULES)
    for modname in candidates:
        try:
            mod = importlib.import_module(modname)
            _safe_log(logger, logging.DEBUG, "provider_module_imported", modname=modname)
            _auto_register_from_module(mod, logger)
        except Exception as exc:
            _safe_log(
                logger,
                logging.DEBUG,
                "provider_candidate_import_failed",
                modname=modname,
                error=str(exc),
            )


# ------------------------------ Diagnostics -------------------------------- #


def diagnostics() -> Dict[str, Any]:
    """Return a structured diagnostic snapshot for scripts."""
    with _LOCK:
        return {
            "providers": list_providers(),
            "discovery_errors": list(DISCOVERY_ERRORS),
            "discovery_done": _DISCOVERY_DONE,
            "discovery_attempts": _DISCOVERY_ATTEMPTS,
        }
