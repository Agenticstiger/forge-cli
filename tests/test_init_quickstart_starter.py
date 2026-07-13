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

"""Pins for the consolidated Quickstart → starter guided flow.

Onboarding simplification: the first-run menu collapses the three redundant
"scaffold something pre-built" rows (Quickstart / template / blueprint) into a
single Quickstart row that surfaces a unified starter catalog (create-vite
framework→variant pattern). Blueprints become first-class Quickstart choices;
the rich ``customer-360`` example stays the default. All CLI flags
(``--quickstart`` / ``--template`` / ``--blueprint``) keep working unchanged.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import patch

from fluid_build.cli import _init_interactive_helpers as helpers
from fluid_build.cli import init as init_mod

_LOG = logging.getLogger("test.init.quickstart-starter")


def _menu_args(**kw):
    base = dict(
        quickstart=False,
        blank=False,
        template=None,
        blueprint=None,
        yes=False,
    )
    base.update(kw)
    return SimpleNamespace(**base)


# --- _starter_catalog -------------------------------------------------------


def test_starter_catalog_default_is_customer_360():
    catalog = helpers._starter_catalog()
    assert catalog, "starter catalog must never be empty"
    assert catalog[0] == {
        "kind": "template",
        "target": "customer-360",
        "label": "Customer 360",
        "description": catalog[0]["description"],
    }


def test_starter_catalog_includes_every_bundled_blueprint():
    from fluid_build.cli._market_bundled_blueprints import list_bundled_blueprints

    catalog = helpers._starter_catalog()
    targets = {e["target"] for e in catalog if e["kind"] == "blueprint"}
    assert {b["id"] for b in list_bundled_blueprints()} <= targets


# --- _ask_starter robustness (mirrors _ask_template_name) -------------------


def test_ask_starter_non_rich_returns_default_template():
    with patch.object(helpers, "_rich_available", return_value=False):
        assert helpers._ask_starter() == ("template", "customer-360")


def test_ask_starter_prompt_error_falls_back_to_default():
    class _PromptRaises:
        @staticmethod
        def ask(*_a, **_k):
            raise EOFError("no stdin")

    with (
        patch.object(helpers, "_rich_available", return_value=True),
        patch.object(helpers, "_get_console", return_value=_FakeConsole()),
        patch.object(helpers, "_get_prompt", return_value=_PromptRaises),
    ):
        assert helpers._ask_starter() == ("template", "customer-360")


class _FakeConsole:
    def print(self, *_a, **_k):
        pass


# --- detect_mode dispatch (the two-level guided flow) -----------------------


def test_quickstart_menu_default_starter_routes_to_template(monkeypatch, tmp_path):
    # Menu → Quickstart → starter picker defaults to customer-360 (template mode)
    # with --yes, matching the classic quickstart behaviour.
    monkeypatch.chdir(tmp_path)
    args = _menu_args()
    with (
        patch.object(init_mod, "_ask_creation_mode", return_value="quickstart"),
        patch.object(init_mod, "_ask_starter", return_value=("template", "customer-360")),
    ):
        mode = init_mod.detect_mode(args, _LOG)
    assert mode == "template"
    assert args.template == "customer-360"
    assert args.yes is True


def test_quickstart_menu_blueprint_starter_routes_to_blueprint(monkeypatch, tmp_path):
    # Picking a blueprint starter inside Quickstart dispatches through
    # blueprint_mode with the chosen id.
    monkeypatch.chdir(tmp_path)
    args = _menu_args()
    with (
        patch.object(init_mod, "_ask_creation_mode", return_value="quickstart"),
        patch.object(init_mod, "_ask_starter", return_value=("blueprint", "fluid.starter-gcp")),
    ):
        mode = init_mod.detect_mode(args, _LOG)
    assert mode == "blueprint"
    assert args.blueprint == "fluid.starter-gcp"
    assert args.yes is True


def test_quickstart_menu_empty_catalog_falls_back_to_customer_360(monkeypatch, tmp_path):
    # Defensive: if the starter picker yields nothing, Quickstart still lands on
    # the classic customer-360 template — never a dead end.
    monkeypatch.chdir(tmp_path)
    args = _menu_args()
    with (
        patch.object(init_mod, "_ask_creation_mode", return_value="quickstart"),
        patch.object(init_mod, "_ask_starter", return_value=None),
    ):
        mode = init_mod.detect_mode(args, _LOG)
    assert mode == "template"
    assert args.template == "customer-360"
    assert args.yes is True


# --- flags remain the non-interactive bypass (no menu, no picker) -----------


def test_quickstart_flag_still_bypasses_the_starter_picker():
    # `fluid init --quickstart` (local) must NOT invoke the interactive starter
    # picker — it stays a zero-question customer-360 scaffold.
    args = SimpleNamespace(
        quickstart=True, blank=False, template=None, blueprint=None, provider="local", yes=False
    )
    with patch.object(init_mod, "_ask_starter") as picker:
        mode = init_mod.detect_mode(args, _LOG)
    picker.assert_not_called()
    assert mode == "template"
    assert args.template == "customer-360"
    assert args.yes is True


def test_blueprint_flag_still_bypasses_the_menu():
    args = SimpleNamespace(quickstart=False, blank=False, template=None, blueprint="fluid.starter")
    with patch.object(init_mod, "_ask_creation_mode") as menu:
        mode = init_mod.detect_mode(args, _LOG)
    menu.assert_not_called()
    assert mode == "blueprint"
