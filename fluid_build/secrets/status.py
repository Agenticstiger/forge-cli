# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""What ``fluid doctor`` reports about secrets: policy, handoff, stores. Never a value.

Every key is stable, so ``fluid doctor --json`` can be read by a machine. A
store is contacted only when one is configured (``BAO_ADDR`` / ``VAULT_ADDR``),
and then only for its health and, for an operator-supplied token, the token's
TTL: a wrapped token is never unwrapped and no JWT login is made, because
either would spend a credential meant for a run.
"""

from __future__ import annotations

import importlib.util
import os
from typing import Any, Dict

from .errors import SecretError
from .handoff import RUN_SECRETS_ENV
from .policy import (
    NEVER_RESOLVE_STAGES,
    allowed_stages,
    destination_grants,
    enabled_schemes,
    locator_prefixes,
)


def _handoff_status() -> Dict[str, Any]:
    path = os.environ.get(RUN_SECRETS_ENV, "").strip()
    if not path:
        return {"set": False}
    from .handoff import _read_run_file

    try:
        handed = _read_run_file(path)
    except SecretError as exc:
        return {"set": True, "path": path, "ok": False, "error": exc.why}
    return {"set": True, "path": path, "ok": True, "references": len(handed)}


def secrets_status() -> Dict[str, Any]:
    """The operator's secret setup, as plain data."""
    from . import _vault
    from .registry import installed_packs

    try:
        packs = installed_packs()
    except Exception as exc:  # a broken distribution must not break doctor
        packs = {"error": type(exc).__name__}
    store = os.environ.get("FLUID_CREDENTIAL_STORE", "").strip()
    return {
        "schemes_enabled": sorted(enabled_schemes()),
        "prefixes": {scheme: list(p) for scheme, p in sorted(locator_prefixes().items())},
        "stages_that_resolve": sorted(allowed_stages()),
        "stages_that_never_resolve": sorted(NEVER_RESOLVE_STAGES),
        "destination_entries": len(destination_grants()),
        "run_secrets_file": _handoff_status(),
        "credential_store": store or None,
        "vault": _vault.describe(),
        "aws_sm": {"boto3_installed": importlib.util.find_spec("boto3") is not None},
        "packs": packs,
    }
