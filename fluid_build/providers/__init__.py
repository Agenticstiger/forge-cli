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
  (comma-separated module names, e.g. "fluid_build.providers.local,fluid_build.providers.odps").
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
import traceback
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
    DISCOVERY_ERRORS.append(
        {
            "source": source,
            "modname": modname,
            "error": f"{exc.__class__.__name__}: {exc}",
            "traceback": "".join(
                traceback.format_exception(type(exc), exc, exc.__traceback__)
            ).strip(),
        }
    )


# ----------------------------- Public API ---------------------------------- #

# CLI version for protocol compatibility checks
_CLI_VERSION = "0.7.1"


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


def _check_sdk_compat(name: str, provider: Any, logger: Optional[logging.Logger]) -> None:
    """Warn if a provider's SDK version requirements don't match the running CLI.

    This is advisory — the provider is still registered.  Providers declare
    compatibility via ProviderMetadata.sdk_version (or get_provider_info()).
    """
    try:
        if not hasattr(provider, "get_provider_info"):
            return
        info = provider.get_provider_info()
        if not info:
            return
        sdk_ver = getattr(info, "sdk_version", None)
        if not sdk_ver or sdk_ver == "0.0.0":
            return  # no declared version — skip

        # Check MIN_CLI_VERSION / MAX_CLI_VERSION from the SDK package the
        # provider was built with.  We try the provider's own module first,
        # then fall back to the global SDK.
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
                import fluid_provider_sdk as _sdk

                min_v = getattr(_sdk, "MIN_CLI_VERSION", None)
                max_v = getattr(_sdk, "MAX_CLI_VERSION", None)
            except ImportError:
                return

        cli_t = _parse_version(_CLI_VERSION)
        if min_v:
            min_t = _parse_version(min_v)
            if cli_t < min_t:
                _safe_log(
                    logger,
                    logging.WARNING,
                    "provider_version_warning",
                    name=name,
                    cli_version=_CLI_VERSION,
                    min_cli_version=min_v,
                    hint=f"Provider '{name}' requires CLI >= {min_v}",
                )
        if max_v:
            max_t = _parse_version(max_v)
            if cli_t > max_t:
                _safe_log(
                    logger,
                    logging.WARNING,
                    "provider_version_warning",
                    name=name,
                    cli_version=_CLI_VERSION,
                    max_cli_version=max_v,
                    hint=f"Provider '{name}' requires CLI <= {max_v}",
                )
    except Exception:
        pass  # Version check is advisory — never block registration


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
            f"Invalid provider name '{name}'. " "Use lowercase letters, digits or underscore."
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

    # Check SDK version compatibility (outside lock — read-only, advisory)
    _check_sdk_compat(cname, provider, logger)


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
            raise KeyError(f"Unknown provider '{name}'. " f"Available: {sorted(PROVIDERS.keys())}")
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
    "fluid_build.providers.odps",
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
        # Python >=3.12 returns SelectableGroups; 3.9-3.11 returns dict
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

    for ep in eps:
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
            _safe_log(
                logger, logging.WARNING, "provider_entrypoint_failed", name=ep.name, error=str(exc)
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
    """Try the three passive strategies + a single-subclass fallback."""
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
    candidates = list(_DEFAULT_MODULES) + [
        "fluid_build.providers.opds",  # legacy alias if present
    ]
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
