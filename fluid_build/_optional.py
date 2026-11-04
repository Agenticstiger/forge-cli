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

"""Optional-dependency gate: one call, one install line, no traceback.

``require("litellm", "litellm", "fluid ai setup")`` returns the imported module
or raises :class:`MissingOptionalDependency`, whose message names the exact
``pip install 'data-product-forge[<extra>]'`` line. The CLI entry point renders
that message and exits 1, with no traceback.

Borrowed from pandas' ``import_optional_dependency``
(``pandas/compat/_optional.py``, BSD-3-Clause): an import-name to distribution
mapping for the message, and the ``ImportError`` chained ``from err``. Two
deliberate differences:

* The package is probed with :func:`importlib.util.find_spec` BEFORE it is
  imported, so a missing package never half-imports. For a dotted name the
  top-level package is probed first, because ``find_spec("a.b")`` itself raises
  ``ModuleNotFoundError`` when ``a`` is absent (CPython bpo-30436).
* A package that IS present but fails to import re-raises the original error
  chained, instead of being misreported as "not installed".

:class:`MissingOptionalDependency` subclasses :class:`ImportError`, so existing
``except ImportError`` callers keep working.

Stdlib only and import-light: import it where a gate is needed, never at the
top of a module on the cold ``fluid --help`` path.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from types import ModuleType

__all__ = ["MissingOptionalDependency", "ensure_installed", "is_available", "require"]

# import name -> distribution name, where they differ (pandas' ``INSTALL_MAPPING``).
_DISTRIBUTION = {
    "openlineage": "openlineage-python",
    "yaml": "pyyaml",
}

# import name -> stable error code (see ``fluid_build/_error_catalog.py``).
_EVENTS = {
    "litellm": "copilot_litellm_unavailable",
    "mcp": "mcp_sdk_unavailable",
}
_GENERIC_EVENT = "optional_dependency_missing"


class MissingOptionalDependency(ImportError):
    """An optional package is not installed; ``str(exc)`` is the install hint."""

    exit_code = 1

    def __init__(self, module: str, extra: str, feature: str, alternatives: str = "") -> None:
        self.module = module
        self.extra = extra
        self.feature = feature
        self.alternatives = alternatives
        top = module.split(".", 1)[0]
        self.package = _DISTRIBUTION.get(top, top)
        self.event = _EVENTS.get(top, _GENERIC_EVENT)
        super().__init__(
            f"{feature} needs {self.package}, which is not installed.\n\n"
            f"    pip install 'data-product-forge[{extra}]'"
            + (f"\n\n{alternatives}" if alternatives else ""),
            name=module,
        )

    def __reduce__(self) -> tuple:  # type: ignore[type-arg]
        # ``__init__`` takes (module, extra, feature) but ``args`` holds the
        # message, so the default reduce could not rebuild it (pickle, copy).
        return (type(self), (self.module, self.extra, self.feature))


def _present(module: str) -> bool:
    """True when *module* can be located, without importing it."""
    if module in sys.modules:  # already imported (or deliberately injected)
        return sys.modules[module] is not None
    top = module.split(".", 1)[0]
    try:
        if importlib.util.find_spec(top) is None:
            return False
        return module == top or importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def is_available(module: str) -> bool:
    """Whether *module* is installed, without importing it.

    For a dotted name, ``find_spec`` imports the parent packages (CPython
    semantics); probe the top-level name when that cost matters.
    """
    return _present(module)


def ensure_installed(module: str, extra: str, feature: str, alternatives: str = "") -> None:
    """Raise :class:`MissingOptionalDependency` when *module* is absent; import nothing.

    The probe-only twin of :func:`require`, for guards that run before the
    package is needed (a preflight, a wizard's first prompt).
    """
    if not _present(module):
        raise MissingOptionalDependency(module, extra, feature, alternatives)


def require(module: str, extra: str, feature: str) -> ModuleType:
    """Import and return *module*, or raise :class:`MissingOptionalDependency`.

    ``extra`` is the ``data-product-forge`` extra that provides it and
    ``feature`` is the user-facing name of what needs it ("fluid ai setup").
    """
    if not _present(module):
        raise MissingOptionalDependency(module, extra, feature)
    # Present but broken (a half-installed wheel, an ABI mismatch): let the real
    # error through, chained, rather than telling the user to install it.
    try:
        return importlib.import_module(module)
    except ImportError as err:
        raise ImportError(
            f"{feature} needs {module}, which is installed but failed to import: {err}",
            name=module,
        ) from err
