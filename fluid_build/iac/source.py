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

"""Which contract file a module is compiled from, for plugins that ship local files.

A plugin compiles a contract mapping, which does not say where it was read
from. The Databricks plugin uploads a build's local dbt project, resolved
against the contract's directory as the dbt runner resolves it, so the
commands that compile a module (``fluid apply``, ``fluid generate iac``,
``fluid diff``, ``fluid validate``, ``fluid plan``) name the source contract in
a :func:`contract_source` block. A context variable, as the loader's sinks are
(``loader.py::collect_overlay_warnings``), so a compile in another thread does
not see this one's file.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
from pathlib import Path
from typing import Any, Iterator, Optional, Union

_SOURCE: contextvars.ContextVar[Optional[Path]] = contextvars.ContextVar(
    "iac_contract_source", default=None
)


@contextlib.contextmanager
def contract_source(path: Optional[Union[str, Path]]) -> Iterator[None]:
    """Compile inside this block from the contract file ``path`` (``None``: unknown)."""
    token = _SOURCE.set(Path(path) if path else None)
    try:
        yield
    finally:
        _SOURCE.reset(token)


def current_contract_source() -> Optional[Path]:
    """The contract file the module being compiled comes from, or ``None``."""
    return _SOURCE.get()


def source_contract_file(path: Any) -> Optional[Path]:
    """The contract file a command was given: ``path`` itself, or the source contract
    a bundle or a ``plan.json`` records (``_contract_loader.resolve_source_contract``),
    so a relative ``repository`` resolves as the dbt runner resolves it. ``None``
    when ``path`` is empty or its recorded source no longer resolves."""
    if not path:
        return None
    from fluid_build._contract_loader import resolve_source_contract

    file = Path(str(path))
    plan_data = None
    if file.suffix.lower() == ".json":
        try:
            loaded = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            loaded = None
        if isinstance(loaded, dict) and "contract_metadata" in loaded:
            plan_data = loaded
    resolved, _why = resolve_source_contract(file, plan_data=plan_data)
    return resolved


__all__ = ["contract_source", "current_contract_source", "source_contract_file"]
