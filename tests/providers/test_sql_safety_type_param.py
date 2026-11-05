# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Pin ``validate_sql_type_param_payload`` — the ``(...)`` type-suffix
allowlist that guards DDL type-string injection.

A parameterised type-suffix payload (the text inside ``NUMBER(18,4)`` /
``VARCHAR(100)``) is interpolated into ``CREATE TABLE`` / ``CREATE
PROCEDURE`` DDL. The allowlist restricts it to ``N`` or ``N,N`` (digits +
at most one comma), so a contract cannot smuggle a statement terminator,
comment sequence, or nested DDL through a type parameter. Each test here
FAILS if that guard is removed or loosened.

It also pins how the module's whole-string allowlists anchor. Python's ``$``
also matches just before a trailing newline, so a ``$``-anchored pattern let
``validate_ident("orders\\n")`` through.
"""

import pytest

from fluid_build.providers._sql_safety import (
    _SAFE_IDENT,
    _SQL_TYPE_NAME,
    _SQL_TYPE_PARAM_PAYLOAD,
    SqlTypeError,
    validate_ident,
    validate_sql_type_param_payload,
)


class TestValidateSqlTypeParamPayload:
    def test_accepts_precision_scale(self):
        """``NUMBER(18,4)``-style precision+scale passes through unchanged."""
        assert validate_sql_type_param_payload("18,4") == "18,4"

    def test_accepts_single_length(self):
        """``VARCHAR(100)``-style single length passes through unchanged."""
        assert validate_sql_type_param_payload("100") == "100"

    def test_rejects_injection_payload(self):
        """A DDL-injection payload smuggled into a type parameter is rejected."""
        with pytest.raises(SqlTypeError):
            validate_sql_type_param_payload("100); DROP TABLE users;--")

    def test_rejects_non_numeric_string(self):
        """Anything outside ``N``/``N,N`` (letters, extra commas, symbols)
        is rejected — the allowlist is strict, not merely escaping."""
        with pytest.raises(SqlTypeError):
            validate_sql_type_param_payload("abc")

    def test_rejects_non_str_input(self):
        """A non-``str`` input (e.g. an ``int``) fails the isinstance guard
        rather than being coerced — the ``.match`` would otherwise blow up
        or silently mis-handle it."""
        with pytest.raises(SqlTypeError):
            validate_sql_type_param_payload(100)  # type: ignore[arg-type]


class TestValidateIdentLineBreaks:
    @pytest.mark.parametrize(
        "name",
        ["orders\n", "orders\r\n", "ord\ners"],
        ids=["trailing-lf", "trailing-crlf", "embedded-lf"],
    )
    def test_rejects_line_break(self, name):
        """An identifier with a line break anywhere in it is rejected. Only the
        trailing ``\\n`` used to pass, because ``$`` matches just before it; the
        other two pin that a fix neither strips the input nor matches it line
        by line."""
        with pytest.raises(ValueError, match="Invalid SQL identifier"):
            validate_ident(name)


class TestAllowlistAnchors:
    @pytest.mark.parametrize(
        ("pattern", "value"),
        [
            (_SAFE_IDENT, "orders"),
            (_SQL_TYPE_NAME, "VARCHAR(255)"),
            (_SQL_TYPE_PARAM_PAYLOAD, "18,4"),
        ],
        ids=["ident", "type-name", "type-param"],
    )
    def test_pattern_refuses_trailing_newline(self, pattern, value):
        """Each whole-string allowlist refuses a trailing newline by itself.
        ``validate_sql_type_name`` and ``validate_sql_type_param_payload`` strip
        their input first, so the pattern is what holds if a later edit drops
        that ``.strip()``. ``_SAFE_EXPR_CHARS`` is left out: its class contains
        ``\\s``, which admits a newline anyway."""
        assert pattern.match(value)
        assert pattern.match(value + "\n") is None
