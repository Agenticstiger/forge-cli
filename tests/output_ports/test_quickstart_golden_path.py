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

"""Golden-path end-to-end test for the MCP output-port gateway.

THIS is the test that matters most. It drives the EXACT quickstart a
new user runs from ``examples/mcp-output-port/`` — the shipped contract
file, the real ``resolve_expose_paths`` the ``serve`` command uses, and
the real tool HANDLERS (``describe`` -> ``sample`` -> ``query``) — over
DuckDB, with no credentials.

Why a dedicated "golden path" file, and why it loads the shipped
example verbatim instead of a hand-built fixture:

* **Real user path > isolated components — make this the standard.**
  The gateway's headline ``query`` / ``query_sql`` tools were broken on
  *every* driver for a full release while thousands of lines of
  component tests stayed green, because those tests called the compiler
  and the driver DIRECTLY and never exercised the
  handler -> compiler -> driver wiring a real MCP call takes. The tests
  below go through ``_handlers.*`` — the same entry the server's
  dispatcher calls — so they fail when the wiring breaks. Prefer this
  shape over testing helpers in isolation.
* **The shipped example cannot silently rot.** Loading
  ``examples/mcp-output-port/contract.fluid.yaml`` directly means this
  test breaks the moment the "5-minute demo" in that README stops
  working — the docs stay honest by construction.

Keyless: runs entirely against DuckDB reading the example CSV.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")
pytest.importorskip("duckdb")

from fluid_build.output_ports.mcp import _handlers, resolve_expose_paths
from fluid_build.output_ports.mcp.policy import OutputPortPolicy
from fluid_build.output_ports.mcp.query_compiler import QueryValidationError
from fluid_build.output_ports.mcp.server import SessionState

_EXAMPLE = (
    Path(__file__).resolve().parents[2] / "examples" / "mcp-output-port" / "contract.fluid.yaml"
)


def _example_state() -> SessionState:
    """Build a SessionState from the SHIPPED quickstart contract, with
    its relative CSV path resolved exactly as ``serve`` does."""
    contract = yaml.safe_load(_EXAMPLE.read_text(encoding="utf-8"))
    expose = resolve_expose_paths(contract["exposes"][0], contract_dir=_EXAMPLE.parent)
    return SessionState(
        contract=contract,
        expose=expose,
        policy=OutputPortPolicy.from_contract_and_flags(expose=expose),
        logger=logging.getLogger("test.output_port.golden_path"),
    )


def test_example_contract_file_exists():
    assert _EXAMPLE.is_file(), f"shipped quickstart contract missing: {_EXAMPLE}"


def test_golden_path_describe_exposes_schema():
    payload = _handlers.tool_describe(_example_state())
    assert payload["exposeId"] == "customer_segments"
    names = [c["name"] for c in payload["contract"]["schema"]]
    assert "email" in names and "segment" in names
    assert payload["binding"]["dialect"] == "duckdb"


def test_golden_path_sample_masks_pii():
    payload = _handlers.tool_sample(_example_state(), {"limit": 3})
    assert payload["rowCount"] == 3
    # The email column is still ADVERTISED (the agent learns it exists) ...
    assert "email" in payload["columns"]
    # ... but every VALUE is redacted — never a real address.
    for row in payload["rows"]:
        assert row["email"] == "[REDACTED-PII]"
        assert "@" not in str(row["email"])
    # Non-PII columns pass through untouched.
    assert {r["segment"] for r in payload["rows"]} <= {"enterprise", "smb", "consumer"}


def test_golden_path_semantic_query_aggregates():
    payload = _handlers.tool_query(
        _example_state(),
        {"measure": "total_ltv_usd", "dimensions": ["segment"], "limit": 10},
    )
    assert payload["rowCount"] >= 1
    assert "total_ltv_usd" in payload["columns"]
    assert "GROUP BY" in payload["compiled"]["sql"].upper()


def test_golden_path_pii_cannot_be_aliased_away_in_free_form_sql():
    # The masking holds on the query_sql path too: aliasing the PII
    # column to dodge the row-level redactor is rejected at COMPILE time
    # (a QueryValidationError, so the agent gets a clear, self-correcting
    # message instead of an opaque failure).
    with pytest.raises(QueryValidationError, match="email"):
        _handlers.tool_query_sql(
            _example_state(),
            {"sql": "SELECT email AS contact FROM customer_segments"},
        )
