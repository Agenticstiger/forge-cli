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


def test_menu_option_5_maps_to_blueprint():
    class _PromptReturns5:
        @staticmethod
        def ask(*_a, **_k):
            return "5"

    with (
        patch.object(helpers, "_rich_available", return_value=True),
        patch.object(helpers, "_get_console", return_value=MagicMock()),
        patch.object(helpers, "_get_panel", return_value=MagicMock()),
        patch.object(helpers, "_get_prompt", return_value=_PromptReturns5),
    ):
        assert helpers._ask_creation_mode(ai_available=True) == "blueprint"


def test_ask_blueprint_name_returns_a_bundled_id():
    # Non-rich path returns the default (first bundled) id deterministically.
    with patch.object(helpers, "_rich_available", return_value=False):
        bp_id = helpers._ask_blueprint_name()
    assert bp_id and bp_id.startswith("fluid.")
