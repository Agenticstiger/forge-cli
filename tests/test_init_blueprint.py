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

"""Pins for `fluid init --blueprint` / the "Start from a blueprint" menu path.

The non-breaking "merge blueprints into init" follow-up: surfaces the bundled
marketplace blueprints inside `fluid init`, rendered client-side into a valid
contract (no network, no AI key).
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import yaml

from fluid_build.cli import _init_interactive_helpers as helpers
from fluid_build.cli import init as init_mod
from fluid_build.cli._init_modes import blueprint_mode

_LOG = logging.getLogger("test.init.blueprint")


def _args(**kw):
    base = dict(
        name="demo",
        blueprint="fluid.starter",
        provider="local",
        dry_run=False,
        non_interactive=True,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_blueprint_mode_renders_a_valid_contract(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rc = blueprint_mode(_args(), _LOG)
    assert rc == 0
    contract = tmp_path / "demo" / "contract.fluid.yaml"
    assert contract.exists()
    # The rendered contract must pass the real validator.
    from fluid_build.cli.validate import run_on_contract_dict

    data = yaml.safe_load(contract.read_text(encoding="utf-8"))
    _result, code = run_on_contract_dict(data, strict=False, logger=_LOG)
    assert code == 0, data


def test_blueprint_mode_defaults_to_first_when_unset(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rc = blueprint_mode(_args(blueprint=None), _LOG)
    assert rc == 0
    assert (tmp_path / "demo" / "contract.fluid.yaml").exists()


def test_blueprint_mode_unknown_blueprint_returns_1(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert blueprint_mode(_args(blueprint="nope.does-not-exist"), _LOG) == 1


def test_blueprint_mode_dry_run_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert blueprint_mode(_args(dry_run=True), _LOG) == 0
    assert not (tmp_path / "demo").exists()


def test_detect_mode_routes_blueprint_flag():
    args = SimpleNamespace(quickstart=False, blank=False, template=None, blueprint="fluid.starter")
    assert init_mod.detect_mode(args, _LOG) == "blueprint"


def test_blueprints_surface_via_quickstart_starter_catalog():
    # Consolidation: blueprints are no longer a standalone top-level menu row.
    # They are first-class Quickstart choices in the unified starter catalog,
    # alongside the rich customer-360 template.
    catalog = helpers._starter_catalog()
    kinds = {(e["kind"], e["target"]) for e in catalog}
    # The rich local example is the default (first) entry.
    assert catalog[0]["kind"] == "template"
    assert catalog[0]["target"] == "customer-360"
    # Every bundled blueprint is reachable through the picker.
    assert ("blueprint", "fluid.starter") in kinds
    assert ("blueprint", "fluid.starter-gcp") in kinds
    assert ("blueprint", "fluid.starter-snowflake") in kinds


def test_quickstart_starter_picker_returns_a_blueprint():
    # Picking a blueprint row in the second-level starter picker returns
    # ("blueprint", <id>) so the caller dispatches through blueprint_mode.
    class _PromptReturns2:
        @staticmethod
        def ask(*_a, **_k):
            return "2"  # second catalog entry — the first bundled blueprint

    with (
        patch.object(helpers, "_rich_available", return_value=True),
        patch.object(helpers, "_get_console", return_value=MagicMock()),
        patch.object(helpers, "_get_prompt", return_value=_PromptReturns2),
    ):
        kind, target = helpers._ask_starter()
    assert kind == "blueprint"
    assert target.startswith("fluid.")


def test_menu_option_3_maps_to_blank():
    # The consolidated top-level menu is Quickstart / AI / Empty (3 rows).
    class _PromptReturns3:
        @staticmethod
        def ask(*_a, **_k):
            return "3"

    with (
        patch.object(helpers, "_rich_available", return_value=True),
        patch.object(helpers, "_get_console", return_value=MagicMock()),
        patch.object(helpers, "_get_panel", return_value=MagicMock()),
        patch.object(helpers, "_get_prompt", return_value=_PromptReturns3),
    ):
        assert helpers._ask_creation_mode(ai_available=True) == "blank"


def test_ask_blueprint_name_returns_a_bundled_id():
    # Non-rich path returns the default (first bundled) id deterministically.
    with patch.object(helpers, "_rich_available", return_value=False):
        bp_id = helpers._ask_blueprint_name()
    assert bp_id and bp_id.startswith("fluid.")


# --- inspection follow-ups to #318 -----------------------------------------


def test_blueprint_mode_writes_forge_receipt(tmp_path, monkeypatch):
    # Parity with blank_mode / template_mode: the blueprint scaffold must drop a
    # .fluid/forge-receipt.json so `fluid status` + drift see the same shape.
    import json

    monkeypatch.chdir(tmp_path)
    rc = blueprint_mode(_args(), _LOG)
    assert rc == 0
    receipt = tmp_path / "demo" / ".fluid" / "forge-receipt.json"
    assert receipt.exists(), "blueprint_mode must write a forge receipt"
    doc = json.loads(receipt.read_text(encoding="utf-8"))
    # Envelope-wrapped ForgeReceipt; the raw text records the authoring flow +
    # the blueprint id so downstream tooling can correlate the product.
    raw = receipt.read_text(encoding="utf-8")
    assert "init-blueprint" in raw
    assert "fluid.starter" in raw
    assert isinstance(doc, dict)


def test_init_parser_registers_metadata_flags():
    # Card 3: --domain / --owner-team / --owner-email must be wired into argparse
    # (blueprint_mode reads args.domain / args.owner_team / args.owner_email).
    import argparse

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    init_mod.register(sub)
    args = parser.parse_args(
        [
            "init",
            "demo",
            "--blueprint",
            "fluid.starter",
            "--domain",
            "marketing",
            "--owner-team",
            "growth",
            "--owner-email",
            "growth@example.com",
        ]
    )
    assert args.domain == "marketing"
    assert args.owner_team == "growth"
    assert args.owner_email == "growth@example.com"


def test_blueprint_flags_flow_into_contract(tmp_path, monkeypatch):
    # Card 3: the metadata flags must reach the rendered contract (the bundled
    # templates interpolate domain / owner_team / owner_email).
    monkeypatch.chdir(tmp_path)
    rc = blueprint_mode(
        _args(domain="marketing", owner_team="growth", owner_email="growth@example.com"),
        _LOG,
    )
    assert rc == 0
    contract = (tmp_path / "demo" / "contract.fluid.yaml").read_text(encoding="utf-8")
    data = yaml.safe_load(contract)
    assert data.get("domain") == "marketing"
    owner = (data.get("metadata") or {}).get("owner") or {}
    assert owner.get("team") == "growth"
    assert owner.get("email") == "growth@example.com"
