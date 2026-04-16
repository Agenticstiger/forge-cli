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

from __future__ import annotations

import re

_SAFE_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SAFE_EXPR_CHARS = re.compile(r"^[A-Za-z0-9_\s().,<>=!'+\-*/%|&\":\[\]]+$")
_BLOCKED_EXPR_TOKENS = re.compile(
    r"(?i)\b("
    r"alter|call|copy|create|delete|drop|execute|grant|insert|merge|put|remove|"
    r"revoke|select|show|truncate|update|use"
    r")\b"
)


def validate_ident(name: str) -> str:
    """Validate a SQL identifier to prevent injection and return it unchanged."""
    if not isinstance(name, str) or not _SAFE_IDENT.match(name):
        raise ValueError(f"Invalid SQL identifier: {name!r}")
    return name


def quote_string_literal(value: str) -> str:
    """Quote a SQL string literal by doubling embedded single quotes."""
    if not isinstance(value, str):
        raise ValueError(f"Invalid SQL string literal: {value!r}")
    return "'" + value.replace("'", "''") + "'"


def validate_sql_expression_allowlist(expr: str) -> str:
    """Allow only a narrow SQL-expression subset suitable for RLS conditions."""
    if not isinstance(expr, str):
        raise ValueError(f"Invalid SQL expression: {expr!r}")

    candidate = expr.strip()
    if not candidate:
        raise ValueError("Invalid SQL expression: empty")

    if any(token in candidate for token in (";", "--", "/*", "*/")):
        raise ValueError(f"Invalid SQL expression: {expr!r}")

    if not _SAFE_EXPR_CHARS.match(candidate):
        raise ValueError(f"Invalid SQL expression: {expr!r}")

    if _BLOCKED_EXPR_TOKENS.search(candidate):
        raise ValueError(f"Invalid SQL expression: {expr!r}")

    return candidate
