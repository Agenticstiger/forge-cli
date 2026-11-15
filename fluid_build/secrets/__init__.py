# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Secret references: contracts name secrets, and a value is fetched late, by the run.

The rule, for forge-cli and Command Center alike: a contract (and Command
Center) holds references, never values. A value is fetched by the stage that
sends it, with the run's own identity, through the narrowest channel the
target supports, and is never persisted.

* :mod:`.ref` -- the grammar, ``<scheme>://<locator>[#<field>][?version=<n>]``.
* :mod:`.policy` -- the operator's policy: schemes, locator prefixes, stages,
  and where each value may be sent.
* :mod:`.registry` -- a resolver per scheme: ``env``, ``file``, ``vault``
  (alias ``openbao``) and ``aws-sm`` (alias ``aws``) in core, any other store
  as a pack in the ``fluid_build.secret_resolvers`` entry-point group.
* :mod:`.resolve` -- policy, cache, the run-secrets handoff file, the
  ``SecretStr`` wrapper, redactor registration and the audit event.
* :mod:`.run` -- the run a value belongs to; its end revokes what it minted.

Names are re-exported lazily (PEP 562) so importing this package costs a
stage nothing until it resolves.
"""

from __future__ import annotations

import importlib
from typing import Any, Dict, Tuple

#: Each submodule, and the names this package re-exports from it.
_MODULES: Dict[str, Tuple[str, ...]] = {
    "ref": (
        "SecretRef",
        "parse_secret_ref",
        "looks_like_ref",
        "SCHEMA_PATTERNS",
        "CORE_SCHEMES",
        "PACK_SCHEMES",
        "KNOWN_SCHEMES",
    ),
    "errors": ("SecretError", "SecretRefError", "SecretPolicyError", "SecretResolveError"),
    "policy": ("AcquisitionSecretPolicyError", "refuse_unless_allowed"),
    "run": ("secret_run", "current_stage"),
    "resolve": ("resolve_secret", "resolve_credential", "secret_exists"),
    "registry": (
        "SecretResolver",
        "Existence",
        "ENTRY_POINT_GROUP",
        "register_resolver",
        "unregister_resolver",
        "get_resolver",
    ),
    "contract": ("lint_secret_refs", "secret_ref_sites"),
    "handoff": ("RUN_SECRETS_ENV",),
}

_EXPORTS: Dict[str, str] = {name: module for module, names in _MODULES.items() for name in names}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(f"{__name__}.{module_name}"), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_EXPORTS))
