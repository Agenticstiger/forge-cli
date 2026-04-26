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
