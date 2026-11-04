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
- Single source of truth: PROVIDERS dict (name -> class or factory). Names come
  from ``fluid_build.providers`` entry-point *metadata* and are listed without
  importing anything; a provider class is imported (and cached) only when it is
  looked up by name -- the pattern of stevedore's ``entry_point_names()`` plus a
  load-one-by-name ``DriverManager`` (https://docs.openstack.org/stevedore/),
  done here with the stdlib ``importlib.metadata`` so no dependency is added.
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
from typing import Any, Dict, List, Optional, Set, Tuple, Type

#: The built-in providers and where their class lives. This table mirrors the
#: ``[project.entry-points."fluid_build.providers"]`` block of pyproject (a test
#: pins the two together). It is the fallback for a checkout whose installed
#: metadata predates an entry point (a stale editable install without
#: ``databricks``), and it is the set of names a THIRD-PARTY plugin may never
#: claim (see :func:`_claims_reserved_name`).
BUILTIN_PROVIDERS: Dict[str, str] = {
    "aws": "fluid_build.providers.aws:AwsProvider",
    "databricks": "fluid_build.providers.databricks:DatabricksProvider",
    "gcp": "fluid_build.providers.gcp:GcpProvider",
    "local": "fluid_build.providers.local:LocalProvider",
    "snowflake": "fluid_build.providers.snowflake:SnowflakeProvider",
}

#: The distribution that ships the built-ins. An entry point from any other
#: distribution is a third-party plugin.
_FIRST_PARTY_DIST = "data-product-forge"


@dataclass(frozen=True)
class _Pending:
    """A provider known by name and entry-point string, not yet imported."""

    name: str
    value: str  # "module:attr"
    source: str  # "entrypoint" (declared metadata) | "builtin" (static fallback table)
    ep: Any = None  # the importlib.metadata.EntryPoint, when there is one
    distribution: Optional[str] = None

    def load(self) -> Any:
        if self.ep is not None:
            return self.ep.load()
        modname, _, attr = self.value.partition(":")
        return getattr(importlib.import_module(modname), attr)


class _ProviderMap(dict):
    """``PROVIDERS``: a dict whose unloaded entries are entry points, not classes.

    Membership, ``len``, iteration and ``keys()`` cover loaded *and* pending
    names and import nothing. Reading a value (``[name]``, ``get``, ``values``,
    ``items``) imports the pending provider once and caches the class. Writes
    behave as on a plain dict and supersede a pending entry of the same name.
    """

    def __eq__(self, other: object) -> bool:
        # The loaded entries compare as a dict's would; comparing must not
        # import every pending provider.
        return dict.__eq__(self, other)

    __hash__ = None  # type: ignore[assignment]  # mutable, as a dict

    def __init__(self) -> None:
        super().__init__()
        self._pending: Dict[str, _Pending] = {}

    # -- names (never import) ------------------------------------------------
    def __contains__(self, name: object) -> bool:
        return dict.__contains__(self, name) or name in self._pending

    def _names(self) -> List[str]:
        return list(dict.keys(self)) + [n for n in self._pending if not dict.__contains__(self, n)]

    def keys(self):  # type: ignore[override]
        # A set-like view (``in``, ``&``, ``-``), over loaded and pending names.
        return dict.fromkeys(self._names()).keys()

    def __iter__(self):
        return iter(self._names())

    def __len__(self) -> int:
        return len(self._names())

    def __bool__(self) -> bool:
        return bool(dict.__len__(self) or self._pending)

    # -- values (import on demand) ------------------------------------------
    def _resolve(self, name: str) -> None:
        with _LOCK:
            pend = self._pending.get(name)
            if pend is None:
                return
            from fluid_build.plugin_manager import is_allowed

            if not is_allowed(name):
                # Checked again before the import: a blocklist loaded after
                # discovery (FLUID_SECRETS_FILE at apply time) must not run the
                # plugin's code, not merely leave it unserved.
                self._pending.pop(name, None)
                POLICY_BLOCKED.add(name)
                return
            _RESOLVING.add(name)
            try:
                obj = pend.load()
                # Register exactly what the declared entry point resolved to,
                # replacing whatever the import itself put in the slot, so the
                # entry point that was declared (and granted) is what is served
                # and ``provider_origin`` stays true. ``override`` lifts only the
                # duplicate check; the allow/block policy, the built-in-name
                # reservation and the compat gate still apply.
                register_provider(name, obj, source="entrypoint", override=True)
                meta = _REGISTRY_META.get(name)
                if meta is not None:
                    meta["source"] = "entrypoint"
            except Exception as exc:
                # Type-only, as in discovery: never interpolate exception text.
                who = pend.distribution or pend.value
                _log.warning(
                    "provider_entrypoint_failed: provider %r (from %r) failed to load: %s",
                    name,
                    who,
                    type(exc).__name__,
                    extra={
                        "evt": "provider_entrypoint_failed",
                        "_name": name,
                        "error": type(exc).__name__,
                    },
                )
                _add_discovery_error("entrypoint", name, exc)
                # Remembered, so every later lookup says "failed to load", not "unknown".
                LOAD_FAILURES[name] = {"error": type(exc).__name__, "distribution": who}
            finally:
                _RESOLVING.discard(name)
                self._pending.pop(name, None)

    def __getitem__(self, name):
        if not dict.__contains__(self, name):
            self._resolve(name)
        return dict.__getitem__(self, name)

    def get(self, name, default=None):
        if not dict.__contains__(self, name):
            self._resolve(name)
        return dict.get(self, name, default)

    def _resolve_all(self) -> None:
        for n in list(self._pending):
            self._resolve(n)

    def values(self):  # type: ignore[override]
        self._resolve_all()
        return dict.values(self)

    def items(self):  # type: ignore[override]
        self._resolve_all()
        return dict.items(self)

    # -- writes ---------------------------------------------------------------
    def __setitem__(self, name, value) -> None:
        self._pending.pop(name, None)
        dict.__setitem__(self, name, value)

    def __delitem__(self, name) -> None:
        if name in self._pending:
            del self._pending[name]
            if not dict.__contains__(self, name):
                return
        dict.__delitem__(self, name)

    def pop(self, name, *default):
        # As for ``dict.pop`` the entry's VALUE is returned, so a declared-but-
        # unimported provider is imported first; a provider that fails to import
        # is gone either way and yields the default (or KeyError without one).
        if name in self._pending:
            self._resolve(name)
        return dict.pop(self, name, *default)

    def clear(self) -> None:
        self._pending.clear()
        dict.clear(self)

    def update(self, *args, **kwargs) -> None:  # type: ignore[override]
        for other in args:
            if isinstance(other, _ProviderMap):
                for n, pend in other._pending.items():
                    if not dict.__contains__(self, n):
                        self._pending[n] = pend
                other = dict(dict.items(other))
            for k, v in dict(other).items():
                self[k] = v
        for k, v in kwargs.items():
            self[k] = v

    def copy(self) -> "_ProviderMap":  # type: ignore[override]
        dup = _ProviderMap()
        dup.update(self)
        return dup

    # -- introspection that must not import ------------------------------------
    def pending(self, name: str) -> Optional[_Pending]:
        return self._pending.get(name)

    def is_loaded(self, name: str) -> bool:
        return dict.__contains__(self, name)


# Public, process-wide registry (name -> provider class or factory)
PROVIDERS: Dict[str, Any] = _ProviderMap()

# Names currently being imported by ``_ProviderMap._resolve``: only that import
# may fill its own pending slot (the built-in packages self-register on import).
_RESOLVING: Set[str] = set()

#: name -> {"error": <exception type name>, "distribution": <who ships it>} for a
#: declared provider whose import failed at first use. Type names only.
LOAD_FAILURES: Dict[str, Dict[str, str]] = {}


class ProviderLoadError(KeyError):
    """A declared provider could not be imported (``get_provider`` raises this)."""

    def __init__(self, name: str, error: str, distribution: str) -> None:
        super().__init__(name)
        self.provider, self.error, self.distribution = name, error, distribution

    def __str__(self) -> str:
        return (
            f"Provider '{self.provider}' failed to load ({self.error}); "
            f"it is declared by {self.distribution}"
        )


def provider_load_failure(name: str) -> Optional[Dict[str, str]]:
    """``{"error", "distribution"}`` when ``name`` is declared but failed to import."""
    return LOAD_FAILURES.get(_normalize_name(name))


# Collect discovery errors for diagnostics/UIs
DISCOVERY_ERRORS: List[Dict[str, str]] = []

# Providers the operator's allow/block policy kept out of ``PROVIDERS``: they
# exist, so a contract naming one is told the policy removed it, not that no
# provider by that name exists (``fluid apply``'s platform check reads this).
POLICY_BLOCKED: Set[str] = set()

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


#: Names that are no longer providers -> what to do instead. Not
#: registry entries: a user who typed one gets this line (exit 2) rather than a
#: bare "unknown provider". Keys are :func:`_normalize_name` spellings.
RETIRED_PROVIDERS: Dict[str, str] = {
    "redshift": "Redshift is part of the aws provider; use --provider aws",
    "datamesh_manager": (
        "Data Mesh Manager is a catalog, not a provider; use "
        "`fluid publish --target datamesh-manager` (or `fluid datamesh-manager`)"
    ),
}


#: The provider that now serves a retired name used as a *platform* in a contract
#: (``builds[].execution.runtime.platform: redshift`` still validates). Data Mesh
#: Manager is a catalog, not a platform, so it has none.
RETIRED_SUCCESSORS: Dict[str, str] = {"redshift": "aws"}


def resolve_retired_platform(name: str) -> str:
    """The successor provider for a retired platform name; any other name unchanged."""
    if not isinstance(name, str):
        return name
    return RETIRED_SUCCESSORS.get(_normalize_name(name), name)


def _claims_reserved_name(
    name: str,
    who: str,
    logger: Optional[logging.Logger],
    *,
    third_party: bool = False,
    ep: Any = None,
    ambiguous_grant: bool = False,
) -> bool:
    """True (after a WARNING) when a plugin tries to register a reserved provider name.

    Two kinds of name are reserved against discovery:

    * a **retired** name (``RETIRED_PROVIDERS``): an entry point able to claim
      ``redshift`` would replace the retirement hint for ``--provider redshift``
      and win the platform lookup for contracts;
    * a **built-in** name (``BUILTIN_PROVIDERS``), when ``third_party`` is true:
      a plugin from any distribution other than ``data-product-forge`` (unless
      the operator grants it via ``plugin_manager.override_granted``) that
      claims ``gcp`` / ``snowflake`` / ... would otherwise capture the provider
      that emits that cloud's DDL and IaC (and its credentials).

    The check runs BEFORE the entry point is loaded, so a hostile or broken
    plugin module is never imported. Discovery calls this for entry points;
    :func:`register_provider` separately refuses a built-in name from code
    (see :func:`_builtin_registration_refused`). An explicit
    ``register_provider(..., override=True)`` call in code stays allowed.
    """
    if not isinstance(name, str):
        return False
    from fluid_build.plugin_manager import granted_distribution, override_granted

    cname = _normalize_name(name)
    hint = ""
    if cname in RETIRED_PROVIDERS:
        # No built-in to replace, so no operator grant can lift this one.
        event, what = "provider_retired_name_reserved", "the retired provider"
    elif third_party and cname in BUILTIN_PROVIDERS:
        if ep is not None and override_granted(ep) and not ambiguous_grant:
            (logger or _log).info(
                "provider_builtin_override_granted: %r overrides the built-in provider %r "
                "(FLUID_PLUGINS_ALLOWLIST)",
                who,
                cname,
                extra={"evt": "provider_builtin_override_granted", "_name": cname, "plugin": who},
            )
            return False
        event, what = "provider_builtin_name_reserved", "the built-in provider"
        if ambiguous_grant:
            hint = "; several distributions are granted this name, so none was loaded"
        else:
            dist = granted_distribution(ep) if ep is not None else None
            # A spoofed ``Name: data-product-forge`` must not be told how to grant itself.
            if dist and re.sub(r"[-_.]+", "-", dist).lower() != _FIRST_PARTY_DIST:
                # %r-formatted by the caller below: a distribution name is
                # plugin-controlled text and must not print raw into a log line.
                hint = f"; to allow it, add {f'{dist}:{cname}'!r} to FLUID_PLUGINS_ALLOWLIST"
    else:
        return False
    # The plugin goes into the message itself, repr'd so control characters are
    # escaped: the CLI's formatters print the message only, and this line is how
    # an operator learns which package to uninstall.
    (logger or _log).warning(
        "%s: %r tried to register %s %r; it was skipped%s",
        event,
        who,
        what,
        cname,
        hint,
        extra={"evt": event, "_name": cname, "plugin": who},
    )
    return True


_QUIET = logging.getLogger("fluid_build.providers.reservation_probe")
_QUIET.disabled = True


def entry_point_reserved(ep: Any) -> bool:
    """Whether discovery would refuse ``ep`` (a ``fluid_build.providers`` entry point).

    The same reservation check discovery runs before ``ep.load()`` -- a retired
    name, or a built-in name claimed by a plugin the operator has not granted --
    without logging. Inspection commands call it so they never import a plugin
    discovery would skip.
    """
    name = str(getattr(ep, "name", ""))
    return _claims_reserved_name(name, name, _QUIET, third_party=not _is_first_party(ep), ep=ep)


def _claims_retired_name(name: str, who: str, logger: Optional[logging.Logger]) -> bool:
    """Retired-name half of :func:`_claims_reserved_name` (in-tree auto-register paths)."""
    return _claims_reserved_name(name, who, logger)


def retired_provider_hint(name: Optional[str]) -> Optional[str]:
    """One-line ``--provider``/``FLUID_PROVIDER`` error for a retired name, else ``None``."""
    if not name:
        return None
    cname = _normalize_name(name)
    use = RETIRED_PROVIDERS.get(cname)
    if use is None:
        return None
    successor = RETIRED_SUCCESSORS.get(cname)
    if successor is not None:
        try:
            from fluid_build.plugin_manager import is_allowed

            blocked = not is_allowed(successor)
        except Exception:  # noqa: BLE001 - a hint never fails on a policy lookup
            blocked = False
        if blocked:
            # Naming `--provider aws` when the operator's policy removed aws would
            # send them to a second error.
            return (
                f"Provider '{name}' was retired: it is now part of the '{successor}' provider, "
                f"which the plugin allow/block policy blocks here "
                "(FLUID_PLUGINS_ALLOWLIST / FLUID_PLUGINS_BLOCKLIST), so nothing serves it"
            )
    return f"Provider '{name}' was retired: {use}"


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

    # Asked again at first use of an entry-point provider (``_resolve``): the
    # policy can change after discovery (``FLUID_SECRETS_FILE`` is loaded by
    # ``apply``), and a provider the policy now blocks must not be served.
    if not is_allowed(cname):
        _safe_log(
            logger,
            logging.DEBUG,
            "provider_name_rejected",
            name=cname,
            reason="allow_block_policy",
            source=source,
        )
        POLICY_BLOCKED.add(cname)
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
        # A built-in name is reserved in code too, whether or not its slot is
        # filled yet (it is empty before discovery, and after a granted override
        # failed to load): only a class that lives in that built-in's own package
        # is accepted. The one other way in is ``override=True``, which
        # ``_resolve`` uses for the entry point the operator granted and which
        # callers may use deliberately.
        if not override and _builtin_registration_refused(cname, provider):
            (logger or _log).warning(
                "provider_builtin_name_reserved: %r tried to register the built-in "
                "provider %r; it was skipped",
                f"{getattr(provider, '__module__', '<unknown>')}:"
                f"{getattr(provider, '__qualname__', repr(provider))}",
                cname,
                extra={"evt": "provider_builtin_name_reserved", "_name": cname},
            )
            return
        # A name an entry point already claims is taken even before its class is
        # imported: only that entry point's own import (``_RESOLVING``) may fill
        # the slot, so a plugin that self-registers at import time cannot slip in
        # ahead of a built-in that has not been loaded yet.
        exists = PROVIDERS.is_loaded(cname) or (  # type: ignore[attr-defined]
            PROVIDERS.pending(cname) is not None  # type: ignore[attr-defined]
            and cname not in _RESOLVING
        )
        if exists and not override:
            _safe_log(
                logger, logging.DEBUG, "provider_duplicate_ignored", name=cname, source=source
            )
            return

        PROVIDERS[cname] = provider  # also supersedes a pending entry of that name
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


def _builtin_registration_refused(cname: str, provider: Any) -> bool:
    """True when ``provider`` may not take the built-in name ``cname`` without ``override``.

    A built-in name accepts a class defined in that built-in's own package
    (``fluid_build.providers.gcp`` and below) and nothing else. The module name
    is checked against the loaded module too, so a class that merely *claims* an
    in-tree ``__module__`` is refused.
    """
    target = BUILTIN_PROVIDERS.get(cname)
    if target is None:
        return False
    # "fluid_build.providers.gcp:GcpProvider" -> "fluid_build.providers.gcp"
    package = target.partition(":")[0]
    mod = getattr(provider, "__module__", None)
    if not isinstance(mod, str) or not (mod == package or mod.startswith(package + ".")):
        return True
    module = sys.modules.get(mod)
    qual = getattr(provider, "__qualname__", None)
    if module is None or not isinstance(qual, str) or "<locals>" in qual:
        # A module that is not loaded, or a class it cannot name: not provably in-tree.
        return True
    obj: Any = module
    for part in qual.split("."):
        obj = getattr(obj, part, None)
    # Only the class the in-tree module really defines under that name passes.
    return obj is not provider


def provider_origin(name: str) -> Dict[str, Any]:
    """Where ``name`` comes from, without importing it.

    ``loaded`` is False for a provider declared by entry point and not yet used;
    then ``module`` is the entry point's ``module:attr`` string.
    """
    cname = _normalize_name(name)
    with _LOCK:
        pend = PROVIDERS.pending(cname)  # type: ignore[attr-defined]
        if pend is not None and not PROVIDERS.is_loaded(cname):  # type: ignore[attr-defined]
            return {
                "loaded": False,
                "source": "entrypoint",
                "module": pend.value,
                "distribution": pend.distribution,
            }
        meta = _REGISTRY_META.get(cname, {})
        return {
            "loaded": PROVIDERS.is_loaded(cname),  # type: ignore[attr-defined]
            "source": meta.get("source", "unknown"),
            "module": meta.get("module", "unknown"),
            "distribution": None,
        }


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
        found = PROVIDERS.get(cname)  # imports the provider if it is only declared
        if found is None:
            failure = LOAD_FAILURES.get(cname)
            if failure is not None:
                raise ProviderLoadError(name, failure["error"], failure["distribution"])
            raise KeyError(f"Unknown provider '{name}'. Available: {sorted(PROVIDERS.keys())}")
        return found


def clear_providers() -> None:
    """
    Clear the registry (useful in tests). Also resets discovery flags.
    """
    global _DISCOVERY_DONE, _DISCOVERY_ATTEMPTS
    with _LOCK:
        PROVIDERS.clear()
        DISCOVERY_ERRORS.clear()
        POLICY_BLOCKED.clear()
        LOAD_FAILURES.clear()
        _DISCOVERY_DONE = False
        _DISCOVERY_ATTEMPTS = 0


# -------------------------- Discovery Orchestration ------------------------ #

_DEFAULT_MODULES = (
    "fluid_build.providers.local",
    "fluid_build.providers.gcp",
    "fluid_build.providers.aws",
    "fluid_build.providers.snowflake",
    "fluid_build.providers.databricks",
    # odps is a spec-export format, not a cloud provider — intentionally excluded
    # from provider preload/discovery (see providers/opds/__init__.py).
)


def discover_providers(logger: Optional[logging.Logger] = None, *, force: bool = False) -> None:
    """
    Import provider subpackages and auto-register implementations.

    Discovery order:
      0) ``fluid_build.providers`` entry-points: the five built-ins and any
         pip-installed plugin. Recorded BY NAME from entry-point metadata;
         nothing is imported. A provider class is imported, once, when it is
         first looked up (``PROVIDERS[name]`` / ``.get(name)``).
      1) ``FLUID_PROVIDERS`` modules, when the operator names them (imported).
      2) pkgutil scan of the remaining ``fluid_build.providers.*`` PACKAGES
         (never the ``*_validation`` / ``quality_engine`` / ``_*`` helpers).
      3) Fallback best-effort (if the registry is still empty).

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

        # What the policy now allows is no longer blocked. A name it still
        # blocks stays recorded: one registered at import time (aws, by
        # the aws package) is not offered again when its module is cached.
        from fluid_build.plugin_manager import is_allowed

        POLICY_BLOCKED.difference_update({n for n in POLICY_BLOCKED if is_allowed(n)})
        # ...and a declared-but-unloaded provider the policy now blocks is dropped.
        with _LOCK:
            for n in [n for n in list(PROVIDERS._pending) if not is_allowed(n)]:  # type: ignore[attr-defined]
                PROVIDERS._pending.pop(n, None)  # type: ignore[attr-defined]
                POLICY_BLOCKED.add(n)

        # 0) entry-point plugins (third-party packages)
        _discover_entrypoints(logger)

        # 1) modules the operator named via FLUID_PROVIDERS (soft-fail)
        _preload_curated(logger)

        # 2) iterate all submodules in this package (soft-fail)
        _discover_subpackages(logger)

        # 3) If still empty, fallback (best-effort). Skipped when the operator's
        # policy blocked a name: an empty registry is then the policy working.
        if not PROVIDERS and not POLICY_BLOCKED:
            _fallback_registers(logger)

        _DISCOVERY_DONE = True
        _safe_log(
            logger,
            logging.DEBUG,
            "provider_discovery_complete",
            count=len(PROVIDERS),
            errors=len(DISCOVERY_ERRORS),
        )


def _dist_name(ep: Any) -> Optional[str]:
    """Normalised distribution name behind an entry point, or ``None`` if unattributable."""
    dist = getattr(ep, "dist", None)
    name = getattr(dist, "name", None) if dist is not None else None
    if not name:
        get = getattr(getattr(dist, "metadata", None), "get", None)
        name = get("Name") if callable(get) else None
    return re.sub(r"[-_.]+", "-", str(name)).lower() if name else None


def _is_first_party(ep: Any) -> bool:
    """True only for an entry point shipped by ``data-product-forge`` itself.

    An entry point whose distribution cannot be determined is NOT trusted: the
    reservation of built-in names fails closed. A built-in name is first-party
    only when the entry point also targets the in-tree class, because the
    distribution name in the metadata is self-declared.
    """
    if _dist_name(ep) != _FIRST_PARTY_DIST:
        return False
    # The ``Name:`` header is self-declared, so for a built-in name the entry
    # point must also be the exact in-tree target; anything else is third-party.
    target = BUILTIN_PROVIDERS.get(_normalize_name(str(getattr(ep, "name", ""))))
    return target is None or str(getattr(ep, "value", "")).strip() == target


def _entry_points_for_group(group: str) -> List[Any]:
    # Python >=3.12 returns SelectableGroups; 3.10-3.11 returns dict
    all_eps = importlib.metadata.entry_points()
    if isinstance(all_eps, dict):
        return list(all_eps.get(group, []))
    # SelectableGroups (3.12+) or importlib_metadata backport
    return list(
        all_eps.select(group=group) if hasattr(all_eps, "select") else all_eps.get(group, [])
    )


def registered_provider_names() -> List[str]:
    """Sorted provider names, from metadata, with NO provider import.

    Runs name discovery (idempotent) and returns every name that is registered or
    declared by an entry point -- what ``fluid providers`` lists and what
    ``--provider`` is validated against.
    """
    discover_providers()
    return sorted(PROVIDERS.keys())


def _discover_entrypoints(logger: Optional[logging.Logger]) -> None:
    """Record ``fluid_build.providers`` entry points by name -- without importing them.

    Uses :func:`importlib.metadata.entry_points` so any pip-installed package
    that declares::

        [project.entry-points."fluid_build.providers"]
        mycloud = "my_package.provider:MyCloudProvider"

    is listed at discovery time. ``EntryPoint.name`` / ``.value`` / ``.dist`` need
    no ``.load()``; the class is imported only when the provider is first used
    (see :class:`_ProviderMap`), which is also when the plugin<->CLI compat gate
    runs. The built-ins are entry points of ``data-product-forge`` itself, with
    :data:`BUILTIN_PROVIDERS` as the fallback for stale installed metadata.
    """
    EP_GROUP = "fluid_build.providers"
    try:
        eps = _entry_points_for_group(EP_GROUP)
    except Exception as exc:
        _safe_log(logger, logging.DEBUG, "entrypoint_discovery_unavailable", error=str(exc))
        eps = []

    # Gate provider entry-points through the unified operator allow/block policy
    # (FLUID_PLUGINS_ALLOWLIST / FLUID_PLUGINS_BLOCKLIST), so the SAME control that
    # governs validators / catalog / iac plugins also governs providers — the
    # highest-stakes role (it emits cloud DDL / IaC). Imported lazily to avoid any
    # import-time coupling during early provider bootstrap.
    from fluid_build.plugin_manager import (
        entry_point_distribution,
        is_allowed,
        override_granted,
    )

    # A built-in name more than one distribution is granted: ambiguous, load none
    # (last-one-wins must not decide which plugin takes over a built-in).
    grants: Dict[str, int] = {}
    for ep in eps:
        n = _normalize_name(ep.name)
        if (
            n in BUILTIN_PROVIDERS
            and not _is_first_party(ep)
            and is_allowed(ep.name)
            and override_granted(ep)
        ):
            grants[n] = grants.get(n, 0) + 1

    for ep in eps:
        if not is_allowed(ep.name):
            _safe_log(
                logger,
                logging.DEBUG,
                "provider_entrypoint_skipped",
                name=ep.name,
                reason="allow_block_policy",
            )
            POLICY_BLOCKED.add(_normalize_name(ep.name))
            continue
        who = entry_point_distribution(ep) or str(getattr(ep, "value", ep))
        # Before anything is imported: a plugin must not take a retired name, nor
        # (unless it ships with data-product-forge) a built-in one.
        third_party = not _is_first_party(ep)
        if _claims_reserved_name(
            ep.name,
            who,
            logger,
            third_party=third_party,
            ep=ep,
            ambiguous_grant=grants.get(_normalize_name(ep.name), 0) > 1,
        ):
            continue
        _declare_pending(
            ep.name,
            _Pending(
                name=_normalize_name(ep.name),
                value=str(getattr(ep, "value", "")),
                source="entrypoint",
                ep=ep,
                distribution=who,
            ),
            logger,
            # An operator-granted override replaces the built-in's slot.
            replace=third_party
            and _normalize_name(ep.name) in BUILTIN_PROVIDERS
            and override_granted(ep),
        )

    # Built-ins the installed metadata did not list (stale editable install).
    for name, value in BUILTIN_PROVIDERS.items():
        if name in PROVIDERS:
            continue
        if not is_allowed(name):
            POLICY_BLOCKED.add(name)
            continue
        _declare_pending(
            name,
            _Pending(
                name=name,
                value=value,
                source="builtin",
                distribution=_FIRST_PARTY_DIST,
            ),
            logger,
        )


def _declare_pending(
    name: str, pend: _Pending, logger: Optional[logging.Logger], *, replace: bool = False
) -> None:
    """Record ``name`` as declared-but-unloaded; the first declaration wins."""
    cname = _normalize_name(name)
    if not _is_valid_name(cname) or cname in _BANNED_NAMES:
        _safe_log(
            logger, logging.DEBUG, "provider_name_rejected", name=cname, reason="invalid_name"
        )
        return
    with _LOCK:
        if replace:
            PROVIDERS.pop(cname, None)
            _REGISTRY_META.pop(cname, None)
        elif cname in PROVIDERS:
            _safe_log(logger, logging.DEBUG, "provider_duplicate_ignored", name=cname)
            return
        PROVIDERS._pending[cname] = pend  # type: ignore[attr-defined]
        LOAD_FAILURES.pop(cname, None)  # a fresh declaration gets a fresh attempt
    _safe_log(
        logger,
        logging.DEBUG,
        "provider_entrypoint_declared",
        name=cname,
        entrypoint=pend.value,
    )


def _preload_curated(logger: Optional[logging.Logger]) -> None:
    """Import the modules the operator named in ``FLUID_PROVIDERS`` (and only those).

    Without the variable nothing is imported here: the built-ins are declared by
    entry point and load on first use.
    """
    env = (os.getenv("FLUID_PROVIDERS") or "").strip()
    candidates = [m.strip() for m in env.split(",") if m.strip()] if env else []
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


#: In-tree packages under ``fluid_build/providers/`` that are NOT providers: spec
#: exporters, catalog backends, shared code, and the retired ``datamesh_manager``.
#: Importing them registers nothing (``tests/providers/test_no_exporter_providers.py``)
#: but pulls ``requests`` and the AWS util chain, so the scan leaves them alone.
#: A NEW package that is in neither this set nor :data:`BUILTIN_PROVIDERS` is still
#: imported and auto-registered; a test makes the author classify it.
_NON_PROVIDER_PACKAGES = frozenset(
    {"catalogs", "common", "datamesh_manager", "odcs", "odps_standard", "opds"}
)


def _discover_subpackages(logger: Optional[logging.Logger]) -> None:
    """Import the in-tree provider PACKAGES under fluid_build.providers.* and auto-register.

    Only packages: helper modules (``aws_validation``, ``bigquery_validation``,
    ``quality_engine``, ``validation_*``, ``_*``) are not providers and some load
    a cloud SDK at import. The built-in providers are declared by entry point and
    are not imported here either.
    """
    try:
        pkg = importlib.import_module("fluid_build.providers")
    except Exception as exc:
        _safe_log(logger, logging.ERROR, "providers_package_import_failed", error=str(exc))
        _add_discovery_error("package", "fluid_build.providers", exc)
        return

    for modinfo in pkgutil.iter_modules(getattr(pkg, "__path__", []), pkg.__name__ + "."):
        modname = modinfo.name
        short = modname.rsplit(".", 1)[-1]
        if not modinfo.ispkg or short.startswith("_") or short == "base":
            continue  # helpers and non-packages are not providers
        if short in BUILTIN_PROVIDERS:
            continue  # declared by entry point; imported on first use
        if short in _NON_PROVIDER_PACKAGES:
            continue  # exporters / catalogs / shared code: nothing to register, and costly

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
            if _claims_retired_name(name, getattr(mod, "__name__", "<unknown>"), logger):
                continue
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
        if _claims_retired_name(name, getattr(mod, "__name__", "<unknown>"), logger):
            return
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
            if _claims_retired_name(inferred, getattr(mod, "__name__", "<unknown>"), logger):
                return
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
