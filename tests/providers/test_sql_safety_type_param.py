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

"""Pin ``validate_sql_type_param_payload`` — the ``(...)`` type-suffix
allowlist that guards DDL type-string injection.

A parameterised type-suffix payload (the text inside ``NUMBER(18,4)`` /
``VARCHAR(100)``) is interpolated into ``CREATE TABLE`` / ``CREATE
PROCEDURE`` DDL. The allowlist restricts it to ``N`` or ``N,N`` (digits +
at most one comma), so a contract cannot smuggle a statement terminator,
comment sequence, or nested DDL through a type parameter. Each test here
FAILS if that guard is removed or loosened.
"""

import pytest

from fluid_build.providers._sql_safety import SqlTypeError, validate_sql_type_param_payload


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
