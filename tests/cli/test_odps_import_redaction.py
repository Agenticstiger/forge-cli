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

"""`fluid odps import`'s validate-before-write gate must not print a credential.

The gate reports the first schema error, and a jsonschema error quotes the
value that failed. That value comes from a document the command was handed, so
a poisoned contract could print its own credential to stderr. The message goes
through ``redact_secret_text``; before it did, a `snowflake://user:password@host`
DSN in a rejected field was printed in full while a GitHub token in the same
string was masked — the redactor knew the shape and simply was not on this path.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from fluid_build.cli.odps import _validate_imported

pytestmark = pytest.mark.unit

_PASSWORD = "S3cr3t-Pa55w0rd"
_DSN = f"snowflake://svc_etl:{_PASSWORD}@acme-prod.snowflakecomputing.com/db"


def _contract(**overrides: Any) -> Dict[str, Any]:
    """A contract that fails validation *on the field carrying the secret*.

    The id pattern is what rejects it, so the secret is what jsonschema
    quotes. A contract that fails on something else first would never reach
    the leaking branch, which is the trap this test exists to avoid.
    """
    doc: Dict[str, Any] = {
        "fluidVersion": "0.7.5",
        "kind": "DataProduct",
        "id": _DSN,
        "name": "orders",
        "metadata": {"owner": {"team": "commerce"}},
        "exposes": [
            {
                "exposeId": "orders",
                "kind": "table",
                "binding": {
                    "platform": "local",
                    "format": "parquet",
                    "location": {"path": "./orders"},
                },
                "contract": {"schema": [{"name": "id", "type": "string"}]},
            }
        ],
    }
    doc.update(overrides)
    return doc


class TestTheGateStillWorks:
    def test_a_valid_contract_passes(self):
        ok, problem = _validate_imported(_contract(id="commerce.orders"))
        assert ok is True
        assert problem == ""

    def test_an_invalid_contract_is_reported(self):
        ok, problem = _validate_imported(_contract())
        assert ok is False
        assert problem, "the gate must say what is wrong, not just refuse"
        assert "id" in problem


class TestTheCredentialIsNotPrinted:
    def test_the_password_is_masked(self):
        """The failure this test exists for: before the fix the password was
        printed verbatim in the gate's one-line problem report."""
        _, problem = _validate_imported(_contract())
        assert _PASSWORD not in problem

    def test_the_whole_dsn_is_not_reproduced(self):
        _, problem = _validate_imported(_contract())
        assert _DSN not in problem

    @pytest.mark.parametrize(
        "secret",
        [
            "postgres://admin:hunter2@db.internal:5432/warehouse",
            "mysql://root:tr0ub4dor@10.0.0.4/orders",
            "ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
            "AKIAIOSFODNN7EXAMPLE",
        ],
    )
    def test_other_credential_shapes_are_masked_too(self, secret):
        """One shape passing is luck; the redactor is meant to cover the
        family, so a future change that narrows it fails here."""
        _, problem = _validate_imported(_contract(id=secret))
        assert secret not in problem

    def test_a_harmless_value_is_still_shown(self):
        """Redaction must not swallow the diagnosis. An id that is merely
        malformed, with nothing credential-shaped in it, is still quoted so the
        user can see which value the schema rejected."""
        _, problem = _validate_imported(_contract(id="Not A Valid Id!"))
        assert "Not A Valid Id!" in problem
