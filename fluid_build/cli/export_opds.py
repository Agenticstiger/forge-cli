# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

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
