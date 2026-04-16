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

"""SECURITY_REVIEW S-006 — identifier validation in
fluid_build.providers.aws.actions.redshift.ensure_schema.

The previous implementation f-stringed the caller's ``schema`` argument
directly into a ``CREATE SCHEMA {schema}`` DDL statement. Psycopg2
can't parameterize DDL identifiers, so validation at the function
boundary (``validate_ident``) is the only defense. These tests lock
that in: malicious identifiers must produce an error-status result
dict and must not reach the cursor."""

import pytest

from fluid_build.providers.aws.actions.redshift import ensure_schema


@pytest.mark.parametrize(
    "schema",
    [
        "foo; DROP SCHEMA public; --",
        "injection' OR '1'='1",
        "1leading-digit",
        "with space",
        "mixed-hyphen",
    ],
)
def test_ensure_schema_rejects_malicious_identifier(schema, monkeypatch):
    """S-006: malicious schema name returns an error status dict."""
    # Patch the importable psycopg2 so the pre-existing `import psycopg2`
    # branch doesn't error out on environments that don't have it.
    import sys
    import types

    fake_psycopg2 = types.ModuleType("psycopg2")
    monkeypatch.setitem(sys.modules, "psycopg2", fake_psycopg2)

    result = ensure_schema(
        {
            "host": "h",
            "port": 5439,
            "database": "db",
            "user": "u",
            "password": "p",
            "schema": schema,
        }
    )
    assert result["status"] == "error"
    assert "Invalid schema identifier" in result["error"]
    assert result["changed"] is False


def test_ensure_schema_empty_schema_returns_error(monkeypatch):
    """Empty schema already produced an error before this fix; kept
    for regression coverage."""
    import sys
    import types

    monkeypatch.setitem(sys.modules, "psycopg2", types.ModuleType("psycopg2"))
    result = ensure_schema({"host": "h", "schema": ""})
    assert result["status"] == "error"
    assert "schema" in result["error"].lower()
