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

"""FLUID_MCP_JWT_CLAIM_MAPPING must ADD to the defaults, not replace them.

The env var was parsed into a fresh dict and assigned wholesale
(``kwargs["jwt_claim_mappings"] = parsed``), so an operator who set it to map
one extra claim silently dropped every default mapping — ``sub``, ``model``,
``use_case``, ``tenant_id``.

What that costs is not cosmetic. ``model`` and ``use_case`` are the inputs to
the agentPolicy gate: without them, ``decide()`` sees ``None`` for both, so a
contract's ``allowedModels`` / ``deniedUseCases`` stop being enforceable as
intended. ``sub`` and ``tenant_id`` are what ``${caller.*}`` row filters
interpolate, so row-level security silently widens.

The trigger is the realistic one. An operator enabling a caller-jurisdiction
claim writes exactly one mapping and, by doing so, turns off two other controls
with no error, no warning, and a server that still reports healthy.

An operator who genuinely wants to drop a default can still map it away
explicitly; what they can no longer do is lose it by accident.
"""

from __future__ import annotations

from typing import Dict, Optional

import pytest

from fluid_build.output_ports.mcp.auth import AuthValidator

#: ``jurisdiction`` differs from the other four in which direction it fails.
#: Losing ``sub`` / ``model`` / ``use_case`` / ``tenant_id`` silently WIDENS
#: access; losing ``jurisdiction`` CLOSES the gate, because a contract that
#: pins one refuses every caller whose jurisdiction is unknown. Both belong in
#: the merge, for opposite reasons.
DEFAULTS = {"sub", "model", "use_case", "tenant_id", "jurisdiction"}


def _mappings(monkeypatch: pytest.MonkeyPatch, env: Optional[str]) -> Dict[str, str]:
    """Resolve the claim mappings under a JWT config; ``env`` of ``None`` unsets the var."""
    monkeypatch.setenv("FLUID_MCP_AUTH_MODE", "jwt")
    monkeypatch.setenv("FLUID_MCP_JWT_ISSUER", "https://issuer.example")
    monkeypatch.setenv("FLUID_MCP_JWT_AUDIENCE", "fluid")
    monkeypatch.setenv("FLUID_MCP_JWT_JWKS_URL", "https://issuer.example/jwks")
    if env is None:
        monkeypatch.delenv("FLUID_MCP_JWT_CLAIM_MAPPING", raising=False)
    else:
        monkeypatch.setenv("FLUID_MCP_JWT_CLAIM_MAPPING", env)
    return dict(AuthValidator.from_env().jwt_claim_mappings)


def test_defaults_survive_a_custom_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    """The regression that motivated this: one extra claim must not cost four."""
    mappings = _mappings(monkeypatch, "https://fluid/jurisdiction=jurisdiction")
    missing = DEFAULTS - set(mappings)
    assert not missing, (
        f"setting one custom claim mapping dropped the defaults {sorted(missing)}. "
        "model and use_case feed the agentPolicy gate; sub and tenant_id feed "
        "${caller.*} row filters — all four go silently inert."
    )
    assert mappings["https://fluid/jurisdiction"] == "jurisdiction"


def test_custom_mapping_can_override_a_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Merging must not make the defaults immovable — explicit still wins."""
    mappings = _mappings(monkeypatch, "https://corp/model=model")
    assert mappings["https://corp/model"] == "model"
    assert "sub" in mappings


def test_multiple_custom_mappings_all_apply(monkeypatch: pytest.MonkeyPatch) -> None:
    mappings = _mappings(
        monkeypatch, "https://fluid/jurisdiction=jurisdiction,https://fluid/region=region"
    )
    assert mappings["https://fluid/jurisdiction"] == "jurisdiction"
    assert mappings["https://fluid/region"] == "region"
    assert DEFAULTS <= set(mappings)


def test_no_env_var_leaves_defaults_exactly_as_they_were(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guard the guard: the merge must not perturb the untouched path.

    Exactly the defaults, no extras — an over-eager merge that folded in
    something else would still satisfy a subset assertion.
    """
    assert set(_mappings(monkeypatch, None)) == DEFAULTS


def test_malformed_entries_are_skipped_without_losing_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A typo'd mapping must not take the defaults down with it."""
    mappings = _mappings(monkeypatch, "no-equals-sign,https://fluid/jurisdiction=jurisdiction")
    assert DEFAULTS <= set(mappings)
    assert mappings["https://fluid/jurisdiction"] == "jurisdiction"
