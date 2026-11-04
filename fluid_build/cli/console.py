# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Backwards-compat shim for the centralised console renderer.

The renderer moved to the tier-0 shared leaf :mod:`fluid_build._console` so
``fluid_build.build_runners`` can emit through it without inducing a
``build_runners → cli`` edge (enforced by the ``[tool.importlinter]``
contracts). This module re-exports the whole surface so the ~50 existing
``from ...cli.console import cprint`` / ``from .console import ...`` sites keep
working unchanged.

Note on test seams: helpers like :func:`cprint` read the module-global
``console`` sentinel of their *defining* module. To toggle Rich off in a test,
patch it at its canonical home — ``fluid_build._console.console`` — not on this
shim; patching the shim attribute rebinds only this namespace, which the leaf
functions never read.
"""

from __future__ import annotations

from fluid_build._console import (
    _RICH_TAG_PATTERN,
    _SECRET_OUTPUT_PATTERN,
    RICH_AVAILABLE,
    _redact_sensitive_output,
    _redact_str,
    console,
    cprint,
    cprint_json,
    cprint_literal,
    detail,
    error,
    heading,
    hint,
    info,
    literal_text,
    success,
    warning,
)

__all__ = [
    "_RICH_TAG_PATTERN",
    "_SECRET_OUTPUT_PATTERN",
    "RICH_AVAILABLE",
    "_redact_sensitive_output",
    "_redact_str",
    "console",
    "cprint",
    "cprint_json",
    "cprint_literal",
    "detail",
    "error",
    "heading",
    "hint",
    "info",
    "literal_text",
    "success",
    "warning",
]
