# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The ``fluid apply`` run in progress in this context, for code below the CLI.

``cli/_apply_cc_report.py`` opens one Command Center run report per ``fluid
apply`` and sets it here. ``build_runners`` records each build on it
(``record_build`` / ``build_failed``) without importing the CLI, which the
layering in ``tests/observability/test_import_hygiene.py`` forbids.
"""

from __future__ import annotations

import contextvars
from typing import Any, Optional

_CURRENT: contextvars.ContextVar[Optional[Any]] = contextvars.ContextVar(
    "fluid_apply_run", default=None
)


def current_apply_run() -> Optional[Any]:
    """The report of the ``fluid apply`` running in this context, or ``None``."""
    return _CURRENT.get()


def set_current_apply_run(report: Optional[Any]) -> "contextvars.Token[Optional[Any]]":
    """Make ``report`` the current run; pass the token to :func:`reset_current_apply_run`."""
    return _CURRENT.set(report)


def reset_current_apply_run(token: "contextvars.Token[Optional[Any]]") -> None:
    _CURRENT.reset(token)


__all__ = ["current_apply_run", "reset_current_apply_run", "set_current_apply_run"]
