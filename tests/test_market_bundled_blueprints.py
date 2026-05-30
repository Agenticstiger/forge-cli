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

"""Tests for bundled marketplace blueprints (``fluid market --blueprints``).

Pins three things:
  * every bundled blueprint renders a contract that passes the *real* 0.7.4
    JSON-schema validator,
  * the registry is optional — bundled blueprints are listed and instantiated
    fully offline (no HTTP), and
  * user-supplied parameters cannot inject YAML structure or be evaluated as a
    template (the two injection vectors a curated-template feature opens up).
"""

import json
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from fluid_build.cli import marketplace as marketplace_module
from fluid_build.cli._market_bundled_blueprints import (
    BUNDLED_BLUEPRINTS,
    get_bundled_blueprint,
    is_bundled,
    list_bundled_blueprints,
    render_bundled_contract,
)


def _logger() -> logging.Logger:
    return logging.getLogger("test.bundled_blueprints")


def _assert_valid_074(contract: dict) -> None:
    """Validate a rendered contract against the bundled JSON schema (offline)."""
    from fluid_build.schema_manager import FluidSchemaManager

    result = FluidSchemaManager().validate_contract(contract, offline_only=True)
    assert result.is_valid, f"contract failed validation: {result.errors}"


# ── registry / lookup helpers ────────────────────────────────────────────


def test_list_returns_every_bundled_blueprint():
    listed = list_bundled_blueprints()
    assert len(listed) == len(BUNDLED_BLUEPRINTS) >= 2
    ids = {b["id"] for b in listed}
    assert {"fluid.starter", "fluid.analytics-daily"} <= ids
    # Every bundled blueprint carries the same dict shape the registry returns.
    for b in listed:
        assert b["source"] == "bundled"
        assert {
            "id",
            "name",
            "description",
            "category",
            "version",
            "parameters",
            "contract_template",
        } <= set(b)


def test_get_and_is_bundled():
    assert is_bundled("fluid.starter") is True
    assert is_bundled("some.registry-blueprint") is False
    assert get_bundled_blueprint("fluid.starter")["id"] == "fluid.starter"
    assert get_bundled_blueprint("nope") is None


def test_returned_blueprints_are_deep_copies():
    """Mutating a returned blueprint (incl. nested lists) must not corrupt the registry."""
    bp = get_bundled_blueprint("fluid.starter")
    bp["name"] = "MUTATED"
    bp["parameters"].clear()  # nested list — must be a copy, not the shared one
    bp["labels"]["maturity"] = "CLOBBERED"

    fresh = get_bundled_blueprint("fluid.starter")
    assert fresh["name"] == "Starter Data Product"
    assert fresh["parameters"], "shared parameter list was corrupted by a caller"
    assert fresh["labels"]["maturity"] == "stable"


# ── rendering: every blueprint produces a valid 0.7.4 contract ───────────


@pytest.mark.parametrize("bp", BUNDLED_BLUEPRINTS, ids=lambda b: b["id"])
def test_render_produces_valid_074_contract(bp):
    contract = render_bundled_contract(bp, {"product_name": "My Product"})
    assert contract["fluidVersion"] == "0.7.4"
    _assert_valid_074(contract)


def test_render_applies_defaults_when_params_omitted():
    bp = get_bundled_blueprint("fluid.starter")
    contract = render_bundled_contract(bp, {"product_name": "Only Name"})
    owner = contract["metadata"]["owner"]
    # Defaults from _COMMON_PARAMS.
    assert owner["team"] == "data-team"
    assert owner["email"] == "team@example.com"
    assert contract["domain"] == "example"
    _assert_valid_074(contract)


def test_render_derives_slugified_id_from_params():
    bp = get_bundled_blueprint("fluid.starter")
    contract = render_bundled_contract(
        bp, {"product_name": "Customer Signups", "domain": "Growth Team"}
    )
    # domain_slug.name_slug, both slugified to the FLUID-safe charset.
    assert contract["id"] == "growth-team.customer-signups"
    _assert_valid_074(contract)


def test_render_enforces_required_param():
    bp = get_bundled_blueprint("fluid.starter")
    with pytest.raises(ValueError, match="product_name"):
        render_bundled_contract(bp, {"domain": "x"})
    # Blank/whitespace is treated as missing, too.
    with pytest.raises(ValueError, match="product_name"):
        render_bundled_contract(bp, {"product_name": "   "})


# ── security: parameter values are data, never YAML structure or template ──


@pytest.mark.parametrize("field", ["owner_email", "owner_team", "description"])
def test_free_text_param_cannot_inject_yaml_keys(field):
    """A free-text value with quotes/newlines must not add sibling YAML keys."""
    bp = get_bundled_blueprint("fluid.starter")
    evil = 'a@x.io"\ninjected_top_level: PWNED\nbuilds: "clobbered'
    contract = render_bundled_contract(bp, {"product_name": "P", field: evil})
    assert "injected_top_level" not in contract
    # Core structure is intact (the payload tried to clobber `builds`).
    assert isinstance(contract["builds"], list) and contract["builds"]
    _assert_valid_074(contract)


def test_param_value_is_not_evaluated_as_template():
    """No SSTI: a Jinja expression in a value stays literal."""
    bp = get_bundled_blueprint("fluid.starter")
    contract = render_bundled_contract(bp, {"product_name": "{{ 7 * 7 }}"})
    assert contract["name"] == "{{ 7 * 7 }}"
    assert "49" not in contract["name"]


def test_quotes_and_unicode_in_name_round_trip():
    bp = get_bundled_blueprint("fluid.starter")
    contract = render_bundled_contract(bp, {"product_name": 'Acme "Pro" <Sales> & Co'})
    assert contract["name"] == 'Acme "Pro" <Sales> & Co'
    _assert_valid_074(contract)


# ── marketplace wiring: registry is optional (offline serving) ────────────


def test_search_lists_bundled_with_no_registry():
    """search_blueprints(api_url=None) serves bundled blueprints, no HTTP."""
    args = SimpleNamespace(
        query=None,
        category=None,
        tags=None,
        maturity=None,
        state="published",
        sort="downloads",
        limit=20,
    )
    with (
        patch.object(marketplace_module, "console", MagicMock()),
        patch.object(marketplace_module, "requests") as mock_requests,
    ):
        result = marketplace_module.search_blueprints(args, _logger(), None)
    assert result == 0
    mock_requests.get.assert_not_called()  # api_url=None → registry skipped


def test_info_serves_bundled_offline():
    args = SimpleNamespace(blueprint_id="fluid.starter")
    with (
        patch.object(marketplace_module, "console", MagicMock()),
        patch.object(marketplace_module, "requests") as mock_requests,
    ):
        result = marketplace_module.show_blueprint_info(args, _logger(), None)
    assert result == 0
    mock_requests.get.assert_not_called()


def test_instantiate_bundled_offline_writes_valid_contract(tmp_path):
    out = tmp_path / "contract.fluid.yaml"
    args = SimpleNamespace(
        blueprint_id="fluid.starter",
        params=json.dumps({"product_name": "Offline Product", "domain": "ops"}),
        interactive=False,
        output=str(out),
        submit=False,
    )
    with (
        patch.object(marketplace_module, "console", MagicMock()),
        patch.object(marketplace_module, "requests") as mock_requests,
    ):
        result = marketplace_module.instantiate_blueprint(args, _logger(), None)
    assert result == 0
    mock_requests.get.assert_not_called()
    mock_requests.post.assert_not_called()
    written = json.loads(out.read_text())
    assert written["id"] == "ops.offline-product"
    _assert_valid_074(written)


def test_run_resolves_bundled_id_without_touching_registry(tmp_path):
    """run() must not call get_api_url for a bundled --blueprint-id."""
    out = tmp_path / "c.json"
    args = SimpleNamespace(
        marketplace_action="instantiate",
        blueprint_id="fluid.starter",
        params=json.dumps({"product_name": "No Registry"}),
        interactive=False,
        output=str(out),
        submit=False,
    )
    sentinel = MagicMock(side_effect=AssertionError("get_api_url must not run for a bundled id"))
    with (
        patch.object(marketplace_module, "console", MagicMock()),
        patch.object(marketplace_module, "get_api_url", sentinel),
    ):
        result = marketplace_module.run(args, _logger())
    assert result == 0
    sentinel.assert_not_called()


def test_run_search_resolves_registry_as_optional():
    """run() resolves the registry URL as non-required for search."""
    args = SimpleNamespace(marketplace_action="search", blueprint_id=None)
    captured = {}

    def fake_get_api_url(logger=None, *, required=True):
        captured["required"] = required
        return None

    with (
        patch.object(marketplace_module, "console", MagicMock()),
        patch.object(marketplace_module, "get_api_url", side_effect=fake_get_api_url),
        patch.object(marketplace_module, "search_blueprints", return_value=0) as mock_search,
    ):
        result = marketplace_module.run(args, _logger())
    assert result == 0
    assert captured["required"] is False
    mock_search.assert_called_once()
