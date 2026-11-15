# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Which resolver fetches each scheme: core's four, then packs from an entry-point group.

A resolver is any object with a ``scheme`` and two methods (:class:`SecretResolver`).
Core ships ``env``, ``file``, ``vault`` (alias ``openbao``) and ``aws-sm`` (alias
``aws``, on boto3 from the ``[aws]`` extra), imported only when first used. Any
other store arrives as a pack that registers a resolver in the
``fluid_build.secret_resolvers`` entry-point group, named after its scheme::

    [project.entry-points."fluid_build.secret_resolvers"]
    gcp-sm = "fluid_secrets_gcp:GcpSecretManagerResolver"

with no core edit, the shape of ``register_iac_plugin``. Packs are found the
way every other plugin is (``plugin_manager``), and pass the same allow/block
policy (``FLUID_PLUGINS_ALLOWLIST`` / ``FLUID_PLUGINS_BLOCKLIST``): a resolver
receives secret values, so a pack the operator has not allowed is not seen at
all. The group is read once per process. A pack's scheme still resolves only
once the operator enables it (``FLUID_SECRET_SCHEMES``), and a pack can never
take over a core scheme: the core resolver always wins.
"""

from __future__ import annotations

import functools
import importlib
import logging
import threading
from dataclasses import dataclass
from typing import Any, Dict, Optional, Protocol, runtime_checkable

from fluid_build._errors import doc_url

from .errors import SecretResolveError
from .ref import CORE_SCHEMES, FORMS, SecretRef
from .run import RunState

LOG = logging.getLogger("fluid.secrets")

#: The entry-point group a pack registers its resolver in.
ENTRY_POINT_GROUP = "fluid_build.secret_resolvers"

_CORE: Dict[str, str] = {
    "env": "fluid_build.secrets._env:EnvResolver",
    "file": "fluid_build.secrets._file:FileResolver",
    "vault": "fluid_build.secrets._vault:VaultResolver",
    "aws-sm": "fluid_build.secrets._aws_sm:AwsSecretsManagerResolver",
}

_LOCK = threading.Lock()
_LOADED: Dict[str, "SecretResolver"] = {}
_REGISTERED: Dict[str, "SecretResolver"] = {}


@dataclass(frozen=True)
class Existence:
    """Whether a secret exists, learned without reading its value.

    ``found`` is ``None`` when the store cannot say (for example the identity
    may read the value but not its metadata).
    """

    found: Optional[bool]
    detail: str


@runtime_checkable
class SecretResolver(Protocol):
    """Fetches one scheme's secrets. ``resolve`` returns the plain value."""

    scheme: str

    def resolve(self, ref: SecretRef, run: RunState) -> str:
        """The value of ``ref``. Raise :class:`SecretResolveError` when it cannot."""

    def exists(self, ref: SecretRef, run: RunState) -> Existence:
        """Whether ``ref`` exists, without reading its value."""


def register_resolver(scheme: str, resolver: SecretResolver) -> None:
    """Register ``resolver`` for a non-core scheme from code (tests, embedding hosts)."""
    if scheme in CORE_SCHEMES:
        raise ValueError(f"{scheme}:// is a core scheme and cannot be replaced")
    with _LOCK:
        _REGISTERED[scheme] = resolver
        _LOADED.pop(scheme, None)


def unregister_resolver(scheme: str) -> None:
    """Undo :func:`register_resolver` (tests)."""
    with _LOCK:
        _REGISTERED.pop(scheme, None)
        _LOADED.pop(scheme, None)


def _instantiate(target: Any) -> Any:
    return target() if isinstance(target, type) else target


def _load_core(scheme: str) -> SecretResolver:
    module_name, _, attr = _CORE[scheme].partition(":")
    module = importlib.import_module(module_name)
    resolver: SecretResolver = _instantiate(getattr(module, attr))
    return resolver


@functools.lru_cache(maxsize=1)
def _entry_points() -> Dict[str, Any]:
    """Installed packs the plugin policy allows, by scheme name, read once per process.

    Core scheme names are ignored. ``_entry_points.cache_clear()`` re-reads the
    group (tests). Callers must not mutate the result.
    """
    from fluid_build import plugin_manager

    out: Dict[str, Any] = {}
    for entry in plugin_manager._entry_points(ENTRY_POINT_GROUP):
        if entry.name in CORE_SCHEMES:
            LOG.warning(
                "ignoring secret-resolver pack %r for core scheme %s://",
                getattr(entry, "value", entry.name),
                entry.name,
            )
        elif not plugin_manager.is_allowed(entry.name):
            LOG.info("secret-resolver pack %r is not allowed by the plugin policy", entry.name)
        else:
            out[entry.name] = entry
    return out


def installed_packs() -> Dict[str, str]:
    """Scheme -> the entry point's target, for ``fluid doctor``."""
    return {name: str(entry.value) for name, entry in sorted(_entry_points().items())}


def _missing_pack(scheme: str) -> SecretResolveError:
    form = FORMS.get(scheme, f"{scheme}://...")
    return SecretResolveError(
        what=f"no resolver for {scheme}:// is installed",
        why=(
            f"forge core resolves env, file, vault and aws-sm; {scheme}:// ({form}) comes "
            "from a secret-resolver pack."
        ),
        fix=(
            f"Install a pack that registers {scheme} in the {ENTRY_POINT_GROUP!r} entry-point "
            "group, then enable the scheme (FLUID_SECRET_SCHEMES). Or reference the secret "
            "through a store core reads (vault://, aws-sm://, env://, file://)."
        ),
        doc=doc_url("secrets"),
        extras={"reason": "no_resolver"},
    )


def get_resolver(scheme: str) -> SecretResolver:
    """The resolver for a canonical scheme, or :class:`SecretResolveError` naming the remedy."""
    with _LOCK:
        cached = _LOADED.get(scheme)
        registered = _REGISTERED.get(scheme)
    if cached is not None:
        return cached
    resolver: SecretResolver
    if scheme in _CORE:
        resolver = _load_core(scheme)
    elif registered is not None:
        resolver = registered
    else:
        entry = _entry_points().get(scheme)
        if entry is None:
            raise _missing_pack(scheme)
        try:
            resolver = _instantiate(entry.load())
        except Exception as exc:
            raise SecretResolveError(
                what=f"the secret-resolver pack for {scheme}:// failed to load",
                why=f"{entry.value!r} raised {type(exc).__name__} on import.",
                fix="Reinstall or upgrade the pack, or disable its scheme.",
                doc=doc_url("secrets"),
                extras={"reason": "no_resolver"},
            ) from None
    if not callable(getattr(resolver, "resolve", None)):
        raise _missing_pack(scheme)
    with _LOCK:
        _LOADED[scheme] = resolver
    return resolver


def check_available(scheme: str) -> None:
    """Raise the install-a-pack error when no resolver serves a non-core ``scheme``.

    Checked before the scheme policy, so ``azure-kv://`` without its pack says
    "install a pack" rather than "enable the scheme", which alone would not help.
    It reads the entry-point metadata only: a pack's code is imported once the
    operator's policy lets its scheme resolve, never because a contract named it.
    """
    if scheme in CORE_SCHEMES:
        return
    with _LOCK:
        if scheme in _REGISTERED or scheme in _LOADED:
            return
    if scheme not in _entry_points():
        raise _missing_pack(scheme)
