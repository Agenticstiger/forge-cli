# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Why a FLUID SQL/1 text is refused: one reason code per refusal.

The codes are the ``reason`` of a refused vector in ``fluid-sql-vectors.json``,
so another implementation of the language (flux-expr/2) maps each to its own
error and reaches the same verdict. :data:`REASONS` is copied into the vectors
file, and a test keeps the two equal.
"""

from __future__ import annotations

from typing import Dict, Optional

REASONS: Dict[str, str] = {
    "too-long": "longer than 4,096 code points after NFC normalization, or a canonical text "
    "that would be",
    "comment-or-semicolon": "a ';', '--' or '/*' outside a string literal or a quoted name",
    "syntax": "a text the grammar (RFC-data-rules.md section 2.1) does not derive",
    "too-deep": "nested deeper than 64 levels",
    "round-places": "ROUND to more than 38 decimal places",
    "bad-date": "a DATE literal that is not a calendar date from 0001-01-01 to 9999-12-31 "
    "written YYYY-MM-DD",
    "multi-link-path": "a path over more than one link (rel.rel.col)",
    "unknown-column": "a name that is not a column of the expose, or of the relationship's target",
    "unknown-relationship": "rel.col where rel is not a relationship of the expose",
    "validity-window": "a path into a target that declares a validity window through a "
    "relationship that reads it neither as of a date (asOf) nor by entity, for its natural key "
    "or a column stable per entity",
    "untyped-column": "a column whose type has no FLUID SQL/1 type",
    "type-mismatch": "operands or arguments of the wrong type, or a predicate position "
    "given something other than a bool",
    "null-literal": "the NULL literal where nothing gives it a type: an IN item, an operand "
    "of a comparison or BETWEEN, an argument of a date move, or every operand of a construct",
    "enum-literal": "a string literal compared with a column whose enum does not list it",
    "derive-target": "a derive target with no FLUID SQL/1 type, a float, or a number "
    "without a declared scale",
    "derive-type": "a derived value whose type differs from its target's",
    "derive-scale": "a derived value whose static scale exceeds its target's, or is unbounded",
}


class FluidSqlError(ValueError):
    """A FLUID SQL/1 text, or its use at a site, is refused.

    ``reason`` is one of :data:`REASONS`; ``position`` is a 0-based index into
    the NFC-normalized text, when the refusal has one.
    """

    def __init__(self, reason: str, message: str, position: Optional[int] = None) -> None:
        if reason not in REASONS:  # pragma: no cover - a programming error
            raise ValueError(f"unknown FLUID SQL/1 refusal reason {reason!r}")
        self.reason = reason
        self.detail = message
        self.position = position
        where = f" (at character {position + 1})" if position is not None else ""
        super().__init__(f"{message}{where}")
