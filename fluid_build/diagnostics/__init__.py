# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Versioned diagnostics: each ``fluid validate`` finding at the file, line and column it was written.

``fluid validate --diagnostics`` (text, ``--format json``) and ``--format sarif``
report through this package. :mod:`.model` is the versioned model and its
tolerant reader, :mod:`.build` turns a validation result and the composition's
origins into it, :mod:`.render` writes it as text lines and SARIF 2.1.0.

Standard library only, and nothing here is imported on ``fluid --help``.
"""

from fluid_build.diagnostics.model import (
    SEVERITIES,
    VERSION,
    Diagnostic,
    DiagnosticReport,
    RelatedLocation,
    SourceLocation,
    UnsupportedDiagnosticsVersion,
)

__all__ = [
    "SEVERITIES",
    "VERSION",
    "Diagnostic",
    "DiagnosticReport",
    "RelatedLocation",
    "SourceLocation",
    "UnsupportedDiagnosticsVersion",
]
