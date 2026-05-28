# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Back-compat shim for ``fluid_build.cli.opds``.

The canonical module path is now ``fluid_build.cli.odps`` — the historical
``opds`` spelling was a letter-swap of the canonical ODPS acronym. This shim
re-exports every public symbol from the new module and emits a
:class:`DeprecationWarning` on import so existing scripts and test
``@patch("fluid_build.cli.opds.*")`` decorators keep resolving while we
migrate.

The shim is intended to be removed after one release window; new code should
import from ``fluid_build.cli.odps`` directly.
"""

from __future__ import annotations

import warnings as _warnings

_warnings.warn(
    "fluid_build.cli.opds is deprecated; import fluid_build.cli.odps instead "
    "(OPDS is a letter-swap of the canonical ODPS acronym).",
    DeprecationWarning,
    stacklevel=2,
)

# Re-export *everything* from the canonical module. Star-import covers all
# public top-level names; we also rebind ``__all__`` so ``from
# fluid_build.cli.opds import *`` keeps working.
from fluid_build.cli.odps import *  # noqa: F401,F403,E402
from fluid_build.cli.odps import (  # noqa: F401,E402 — explicit re-exports for static analyzers
    BITOL_SPEC_URL,
    DEFAULT_SPEC,
    DEFAULT_VERSION,
    LEGACY_SPEC_ODPI_4_1_TOKEN,
    ODPI_4_1_SCHEMA_URL,
    ODPI_4_1_SCHEMA_URL_RAW,
    ODPI_4_1_SPEC_URL,
    ODPS_4_1_SCHEMA_URL,
    ODPS_4_1_SCHEMA_URL_RAW,
    ODPS_4_1_SPEC_URL,
    ODPS_VERSIONS,
    SPEC_BITOL_1_0_0,
    SPEC_ODPI_4_1,
    SPEC_ODPS_4_1,
    SUPPORTED_SPECS,
    cmd_opds_export,
    cmd_opds_import,
    cmd_opds_info,
    cmd_opds_validate,
    get_version_info,
    register,
    resolve_spec,
)
