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

"""Back-compat shim for ``fluid_build.cli.export_opds``.

The canonical module path is now ``fluid_build.cli.export_odps``. This shim
re-exports every public symbol from the new module and emits a
:class:`DeprecationWarning` on import so existing scripts and tests keep
resolving while we migrate. The CLI subcommand name ``export-opds`` is
still registered (as a deprecated alias) by the canonical module.
"""

from __future__ import annotations

import warnings as _warnings

_warnings.warn(
    "fluid_build.cli.export_opds is deprecated; import "
    "fluid_build.cli.export_odps instead (OPDS is a letter-swap of the "
    "canonical ODPS acronym).",
    DeprecationWarning,
    stacklevel=2,
)

from fluid_build.cli.export_odps import *  # noqa: F401,F403,E402
from fluid_build.cli.export_odps import (  # noqa: F401,E402
    COMMAND,
    register,
    run,
)
