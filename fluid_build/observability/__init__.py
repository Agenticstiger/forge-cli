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

"""
Observability module for Fluid CLI.

Provides unified logging, metrics, and Command Center integration.
"""

from typing import TYPE_CHECKING

from .config import CommandCenterConfig
from .git import get_git_info
from .secret_redactor import SecretRedactingFilter, install_secret_redacting_filter

if TYPE_CHECKING:  # import only for type checkers, never at runtime
    from .reporter import CommandCenterReporter

__all__ = [
    "CommandCenterConfig",
    "CommandCenterReporter",
    "SecretRedactingFilter",
    "get_git_info",
    "install_secret_redacting_filter",
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
