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

"""End-to-end mode-picker tests — what *actually* runs when you pick X.

Earlier picker tests asserted the right ``args`` attributes get set,
but that's only half the story: the bug we missed was that picking
``template`` or ``refine`` still landed in the AI flow because the
downstream dispatch didn't differentiate between modes. This file
patches the high-level mode handlers and asserts which one gets
called for each picker selection.
"""

from __future__ import annotations

import os
import sys
from argparse import Namespace
from pathlib import Path
from unittest import mock

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolate_home(tmp_path, monkeypatch):
    """Sandbox ~/.fluid so usage.json doesn't leak between tests."""
    fake_home = tmp_path / "_home"
    fake_home.mkdir(exist_ok=True)
    monkeypatch.setattr("fluid_build.cli._welcome_scan.Path.home", lambda: fake_home)
    yield fake_home


@pytest.fixture
def fake_tty(monkeypatch):
    """Make the picker think stdin is a TTY (interactive)."""
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)


def _bare_args(**overrides) -> Namespace:
    """Default forge args namespace as argparse would produce for bare ``fluid forge``."""
    base = dict(
        command="forge",
        help=False,
        forge_subcommand=None,
        blank=False,
        target_dir=None,
        provider=None,
        data_product_type=None,
        transform_engine=None,
        domain=None,
        non_interactive=False,
        dry_run=False,
        context=None,
        llm_provider=None,
        llm_model=None,
        llm_routing_model=None,
        llm_routing_endpoint=None,
        llm_endpoint=None,
        tiered=False,
        require_llm=False,
        no_cache=False,
        deterministic=False,
        no_llm=False,
        yes=False,
        show_work=False,
        refine=None,
        from_product=[],
        from_product_list=None,
        from_workspace=[],
        also_emit=None,
        browser=False,
        discover=True,
        discovery_path=None,
        memory=True,
        save_memory=False,
        show_memory=False,
        reset_memory=False,
        ci=None,
        ci_complexity="standard",
        no_ci=False,
        scaffold=None,
        agent_loop=False,
        no_generate=False,
        fragments=False,
        no_fragments=False,
        template=None,
    )
    base.update(overrides)
    return Namespace(**base)


def _drive_forge(picked_choice: str, args: Namespace, tmp_path: Path):
    """Run ``forge.run(args)`` with the picker patched to return *picked_choice*.

    Returns a dict of which mode handlers were called. The assertion
    is "exactly one handler ran" — not "args has the right shape" —
    so a regression where two handlers fire (e.g. picker chose
    template but AI mode also ran) shows up immediately.
    """
    called: dict = {
        "blank": 0,
        "ai": 0,
        "template": 0,
        "guided": 0,
    }

    def _stub_blank(_args, _logger):
        called["blank"] += 1
        return 0

    def _stub_ai_copilot(_args, _logger):
        called["ai"] += 1
        return 0

    def _stub_template(_args, _logger, **_kwargs):
        called["template"] += 1
        return 0

    def _stub_guided(_args, _logger):
        called["guided"] += 1
        return 0

    def _stub_pick_mode(args, *, console=None, input_fn=None, target_dir=None):
        # Apply the side-effects the real pick_mode would apply.
        if picked_choice == "blank":
            args.blank = True
        elif picked_choice == "refine":
            args.refine = "contract.fluid.yaml"
        elif picked_choice == "from_product":
            args._pick_from_product = True
        # ``ai`` and ``template`` set no flags
        return picked_choice

    def _stub_template_subpicker(_console):
        return "starter"

    def _stub_ai_setup_inline(_console):
        # Pretend AI is already configured so we don't hit the network
        from fluid_build.cli.forge_copilot_llm_providers import LlmConfig

        return LlmConfig(
            provider="openai",
            model="gpt-4o",
            endpoint="https://api.openai.com/v1/chat/completions",
            api_key="sk-test",
        )

    args.target_dir = str(tmp_path)

    # Patch every exit point so the test can assert which one fired.
    with (
        mock.patch("fluid_build.cli._forge_mode_picker.pick_mode", _stub_pick_mode),
        mock.patch("fluid_build.cli._forge_mode_picker.should_show_picker", lambda _a: True),
        mock.patch("fluid_build.cli.forge._run_blank_mode", _stub_blank),
        mock.patch("fluid_build.cli.forge.run_ai_copilot_mode", _stub_ai_copilot),
        mock.patch("fluid_build.cli.forge_modes.run_template_mode", _stub_template),
        mock.patch("fluid_build.cli.forge.run_guided_mode", _stub_guided),
        mock.patch("fluid_build.cli.forge._pick_template_subchoice", _stub_template_subpicker),
        mock.patch("fluid_build.cli.ai_setup.run_ai_setup_inline", _stub_ai_setup_inline),
        mock.patch.dict(os.environ, {"FLUID_FORGE_NO_PREVIEW": "1"}, clear=False),
    ):
        import logging

        from fluid_build.cli.forge import run as forge_run

        rc = forge_run(args, logging.getLogger("test"))

    return called, rc


# ---------------------------------------------------------------------------
# The deep test matrix — one assertion per mode
# ---------------------------------------------------------------------------


def test_picker_ai_routes_to_ai_copilot(fake_tty, tmp_path):
    """Picking AI must run run_ai_copilot_mode and ONLY that."""
    args = _bare_args()
    called, _ = _drive_forge("ai", args, tmp_path)
    assert called["ai"] == 1, f"expected ai=1, got {called}"
    assert called["blank"] == 0
    assert called["template"] == 0
    assert called["guided"] == 0


def test_picker_blank_routes_to_blank_mode(fake_tty, tmp_path):
    """Picking Blank must run _run_blank_mode and skip AI entirely."""
    args = _bare_args()
    called, _ = _drive_forge("blank", args, tmp_path)
    assert called["blank"] == 1, f"expected blank=1, got {called}"
    assert called["ai"] == 0, "Blank mode must not invoke AI copilot"
    assert called["template"] == 0


def test_picker_template_routes_to_template_mode(fake_tty, tmp_path):
    """Picking Template must run run_template_mode, NOT AI copilot.

    This is the bug the user caught — template silently fell through
    to AI. The test pins the fix.
    """
    args = _bare_args()
    called, _ = _drive_forge("template", args, tmp_path)
    assert called["template"] == 1, f"expected template=1, got {called}"
    assert called["ai"] == 0, (
        "Template mode must not invoke AI copilot — that was the bug "
        "every mode silently routed to AI."
    )
    assert called["blank"] == 0


def test_picker_refine_routes_to_ai_copilot_with_refine_arg(fake_tty, tmp_path):
    """Refine still uses the AI runtime (it edits an LLM-emitted contract)
    but must set ``args.refine`` so the runtime shortcuts the bootstrap
    interview and asks "what to change" instead.
    """
    args = _bare_args()
    called, _ = _drive_forge("refine", args, tmp_path)
    assert called["ai"] == 1, f"expected ai=1, got {called}"
    assert args.refine == "contract.fluid.yaml", (
        "Refine mode MUST flag the AI runtime via args.refine; otherwise "
        "the AI flow runs the full new-product interview."
    )


def test_picker_from_product_routes_to_ai_copilot(fake_tty, tmp_path):
    """From-product uses the AI runtime with composition context.

    The picker side-effect is ``_pick_from_product=True`` which signals
    the runtime to read from-product flags / context.
    """
    args = _bare_args()
    called, _ = _drive_forge("from_product", args, tmp_path)
    assert called["ai"] == 1, f"expected ai=1, got {called}"


# ---------------------------------------------------------------------------
# Negative tests — ensure flag combinations don't cross-contaminate
# ---------------------------------------------------------------------------


def test_picker_skipped_when_blank_already_set(tmp_path, fake_tty):
    """``fluid forge --blank`` should skip the picker entirely."""
    args = _bare_args(blank=True)
    called, _ = _drive_forge("ai", args, tmp_path)  # picked_choice ignored
    # Even though _stub_pick_mode would return "ai", the real
    # should_show_picker rejects blank-set runs.
    # Our stub of should_show_picker returns True unconditionally,
    # so we verify by checking that the blank flag wins downstream.
    assert called["blank"] == 1


def test_refine_flag_alone_routes_correctly(tmp_path, fake_tty):
    """``fluid forge --refine`` should also use the AI runtime + carry refine."""
    args = _bare_args(refine="contract.fluid.yaml")
    called, _ = _drive_forge("ai", args, tmp_path)  # picker would skip
    assert called["ai"] == 1
    assert args.refine == "contract.fluid.yaml"


def test_template_flag_alone_routes_to_template(tmp_path, fake_tty):
    """``fluid forge --template starter`` should route to template mode."""
    args = _bare_args(template="starter")

    # When --template is set, the picker should NOT show — but our
    # stub of should_show_picker overrides that. We verify the
    # downstream path with picked='template' anyway.
    called, _ = _drive_forge("template", args, tmp_path)
    assert called["template"] == 1
    assert called["ai"] == 0


# ---------------------------------------------------------------------------
# AI-setup wizard does not run when picker chose a non-AI mode
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("non_ai_choice", ["blank", "template"])
def test_non_ai_modes_skip_ai_setup_wizard(fake_tty, tmp_path, non_ai_choice):
    """When the user picks a non-AI mode, the inline AI setup must NOT run.

    User-visible bug: picking ``blank`` and ``template`` still triggered
    the "Local AI Available — Use it?" prompt because run_ai_setup_inline
    was called before mode dispatch.
    """
    ai_setup_calls = {"count": 0}

    def _spy_ai_setup(_console):
        ai_setup_calls["count"] += 1
        return None

    with mock.patch("fluid_build.cli.ai_setup.run_ai_setup_inline", _spy_ai_setup):
        args = _bare_args()
        called, _ = _drive_forge(non_ai_choice, args, tmp_path)

    if non_ai_choice == "blank":
        assert called["blank"] == 1
    if non_ai_choice == "template":
        assert called["template"] == 1
    assert ai_setup_calls["count"] == 0, (
        f"AI setup wizard ran for non-AI mode {non_ai_choice!r}; "
        "every non-AI path must skip the wizard entirely."
    )
