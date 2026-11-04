# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Deterministic SQL-side tooling for the data-model forge (D5).

The LLM generates OSI ``expression.dialects[]`` per field, but it's only
*probabilistic* — on a bad day Gemini will emit ``DECIMAL(38,10)`` for
Snowflake where the rule is ``NUMBER(38,10)``, or forget BigQuery entirely.
This subpackage lands the cross-check and back-fill as a pure,
deterministic post-processor so the copilot can trust ``expression``
values that survive the mapper.

Shipped deliberately as stdlib-only Python data, not external JSON
files. forge-cli's promise is "no external artefacts required to run",
so the registry lives in code as a typed table.
"""

from fluid_build.forge_datamodel.sql.dialect_mapper import (
    DEFAULT_DIALECTS,
    DialectMapper,
    MappingResult,
    ValidationReport,
)

__all__ = [
    "DEFAULT_DIALECTS",
    "DialectMapper",
    "MappingResult",
    "ValidationReport",
]
