# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""
Observability module for Fluid CLI.

Provides unified logging, metrics, and Command Center integration.
"""

from typing import TYPE_CHECKING

from .config import CommandCenterConfig
from .git import get_git_info
from .secret_redactor import (
    SecretRedactingFilter,
    install_secret_redacting_filter,
    register_secret,
    register_secrets,
    register_secrets_from_environ,
    register_secrets_from_payload,
)

if TYPE_CHECKING:  # import only for type checkers, never at runtime
    from .reporter import CommandCenterReporter

__all__ = [
    "CommandCenterConfig",
    "CommandCenterReporter",
    "SecretRedactingFilter",
    "get_git_info",
    "install_secret_redacting_filter",
    "register_secret",
    "register_secrets",
    "register_secrets_from_environ",
    "register_secrets_from_payload",
]


def __getattr__(name: str):
    """Lazily resolve ``CommandCenterReporter`` (PEP 562).

    ``reporter`` pulls in ``requests`` (~68 modules) and ``build_runners``,
    none of which are needed merely to import this package — and importing
    *any* submodule (e.g. the stdlib-only ``secret_redactor`` leaf that
    ``cli/__init__`` wires up on the cold ``fluid --help`` path) runs this
    ``__init__`` first. Deferring the reporter import keeps ``requests`` off
    the startup path; it loads only when Command Center is actually used
    (``cli/bootstrap.py::get_reporter``). See the A++ Light CLI startup card.
    """
    if name == "CommandCenterReporter":
        from .reporter import CommandCenterReporter

        return CommandCenterReporter
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
