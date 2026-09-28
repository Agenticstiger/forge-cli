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
