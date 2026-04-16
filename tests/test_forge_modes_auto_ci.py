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

"""Tests for the auto-CI hook inside ``run_ai_copilot_mode``.

These tests exercise ``_resolve_ci_choice`` and ``_scaffold_ci_pipeline``
directly, plus the forge argparse wiring for ``--ci`` / ``--ci-complexity``
/ ``--no-ci``. Full end-to-end ``run_ai_copilot_mode`` coverage for
non-interactive silent-skip already lives in ``test_forge_modes_ext.py``.
"""

from __future__ import annotations

import argparse
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from fluid_build.cli.forge_modes import (
    _CI_COMPLEXITY_VALUES,
    _CI_PROVIDER_VALUES,
    _resolve_ci_choice,
    _scaffold_ci_pipeline,
)


def _get_cli_arg(args, name, default=None):
    return getattr(args, name, default)


def _make_args(**overrides):
    defaults = dict(
        ci=None,
        ci_complexity="standard",
        no_ci=False,
        non_interactive=True,
        dry_run=False,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


# ---------------------------------------------------------------------------
# _resolve_ci_choice — pure precedence ladder
# ---------------------------------------------------------------------------


class TestResolveCiChoice(unittest.TestCase):
    def test_killswitch_forces_skip(self):
        with patch.dict(os.environ, {"FLUID_FORGE_AUTO_CI": "0"}, clear=False):
            args = _make_args(ci="github_actions")
            prov, _ = _resolve_ci_choice(
                args,
                {},
                is_interactive=False,
                ask_dialog_question_fn=MagicMock(),
                get_cli_arg_fn=_get_cli_arg,
            )
        self.assertIsNone(prov)

    def test_no_ci_flag_forces_skip(self):
        args = _make_args(no_ci=True, ci="github_actions")
        prov, _ = _resolve_ci_choice(
            args,
            {},
            is_interactive=True,
            ask_dialog_question_fn=MagicMock(),
            get_cli_arg_fn=_get_cli_arg,
        )
        self.assertIsNone(prov)

    def test_ci_none_sentinel_skips(self):
        args = _make_args(ci="none")
        prov, _ = _resolve_ci_choice(
            args,
            {},
            is_interactive=True,
            ask_dialog_question_fn=MagicMock(),
            get_cli_arg_fn=_get_cli_arg,
        )
        self.assertIsNone(prov)

    def test_explicit_provider_non_interactive(self):
        args = _make_args(ci="github_actions", ci_complexity="advanced")
        prov, complexity = _resolve_ci_choice(
            args,
            {},
            is_interactive=False,
            ask_dialog_question_fn=MagicMock(),
            get_cli_arg_fn=_get_cli_arg,
        )
        self.assertEqual(prov, "github_actions")
        self.assertEqual(complexity, "advanced")

    def test_non_interactive_without_flag_and_no_context_skips(self):
        """Non-interactive + no flag + empty context → silent skip."""
        args = _make_args()  # ci=None, non_interactive=True
        ask_mock = MagicMock()
        prov, _ = _resolve_ci_choice(
            args,
            {},  # no ci-state / memory context either
            is_interactive=False,
            ask_dialog_question_fn=ask_mock,
            get_cli_arg_fn=_get_cli_arg,
        )
        self.assertIsNone(prov)
        ask_mock.assert_not_called()

    def test_non_interactive_with_ci_state_context_autoselects(self):
        """Slice 8: non-interactive + recorded ci-state provider → use it.

        This is what makes `fluid forge` on a teammate's clone refresh
        the committed CI files without the user typing --ci.  The
        caller (_scaffold_ci_pipeline) seeds context["ci_provider"]
        from the committed ci-state.json before calling into us.
        """
        args = _make_args()  # ci=None, non_interactive=True
        ask_mock = MagicMock()
        prov, complexity = _resolve_ci_choice(
            args,
            {"ci_provider": "github_actions", "ci_complexity": "advanced"},
            is_interactive=False,
            ask_dialog_question_fn=ask_mock,
            get_cli_arg_fn=_get_cli_arg,
        )
        self.assertEqual(prov, "github_actions")
        # complexity in args wins over context when both are set, but
        # context["ci_complexity"] is the fallback.  _make_args sets
        # ci_complexity="standard" by default, which is used here.
        self.assertEqual(complexity, "standard")
        ask_mock.assert_not_called()

    def test_non_interactive_with_unknown_provider_in_context_skips(self):
        """Unknown provider in context falls back to silent skip."""
        args = _make_args()
        prov, _ = _resolve_ci_choice(
            args,
            {"ci_provider": "not-a-real-provider"},
            is_interactive=False,
            ask_dialog_question_fn=MagicMock(),
            get_cli_arg_fn=_get_cli_arg,
        )
        self.assertIsNone(prov)

    def test_interactive_ask_opens_menu_and_uses_memory_default(self):
        args = _make_args(ci="ask", non_interactive=False)
        # Fake dialog result — user picks jenkins then keeps advanced complexity
        ask_mock = MagicMock(
            side_effect=[
                MagicMock(value="jenkins"),
                MagicMock(value="advanced"),
            ]
        )
        prov, complexity = _resolve_ci_choice(
            args,
            {"ci_provider": "github_actions"},
            is_interactive=True,
            ask_dialog_question_fn=ask_mock,
            get_cli_arg_fn=_get_cli_arg,
        )
        self.assertEqual(prov, "jenkins")
        self.assertEqual(complexity, "advanced")
        self.assertEqual(ask_mock.call_count, 2)

    def test_interactive_menu_cancel_returns_none(self):
        args = _make_args(non_interactive=False)
        ask_mock = MagicMock(return_value=MagicMock(value=None))
        prov, _ = _resolve_ci_choice(
            args,
            {},
            is_interactive=True,
            ask_dialog_question_fn=ask_mock,
            get_cli_arg_fn=_get_cli_arg,
        )
        self.assertIsNone(prov)
        # Only one call — we bail before complexity prompt
        self.assertEqual(ask_mock.call_count, 1)

    def test_unknown_provider_flag_warns_and_skips(self):
        args = _make_args(ci="not-a-real-provider")
        console = MagicMock()
        prov, _ = _resolve_ci_choice(
            args,
            {},
            is_interactive=False,
            ask_dialog_question_fn=MagicMock(),
            get_cli_arg_fn=_get_cli_arg,
            console=console,
        )
        self.assertIsNone(prov)
        console.print.assert_called()
        self.assertIn("Unknown CI provider", str(console.print.call_args))

    def test_invalid_complexity_falls_back_to_standard(self):
        args = _make_args(ci="jenkins", ci_complexity="hyperdrive")
        _, complexity = _resolve_ci_choice(
            args,
            {},
            is_interactive=False,
            ask_dialog_question_fn=MagicMock(),
            get_cli_arg_fn=_get_cli_arg,
        )
        self.assertEqual(complexity, "standard")


# ---------------------------------------------------------------------------
# _scaffold_ci_pipeline — full hook with real PipelineTemplateGenerator
# ---------------------------------------------------------------------------


class TestScaffoldCiPipeline(unittest.TestCase):
    def test_explicit_flag_writes_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            args = _make_args(ci="github_actions", ci_complexity="basic")
            prov, complexity = _scaffold_ci_pipeline(
                args,
                target,
                {},
                None,
                ask_dialog_question_fn=MagicMock(),
                get_cli_arg_fn=_get_cli_arg,
                dry_run=False,
            )
            self.assertEqual(prov, "github_actions")
            self.assertEqual(complexity, "basic")
            created = list(target.rglob("*.yml"))
            self.assertTrue(any(".github/workflows" in str(p) for p in created))

    def test_no_ci_flag_creates_no_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            args = _make_args(no_ci=True)
            prov, _ = _scaffold_ci_pipeline(
                args,
                target,
                {},
                None,
                ask_dialog_question_fn=MagicMock(),
                get_cli_arg_fn=_get_cli_arg,
                dry_run=False,
            )
            self.assertIsNone(prov)
            self.assertEqual(list(target.rglob("*.yml")), [])

    def test_non_interactive_no_flag_silent_skip(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            args = _make_args()
            ask_mock = MagicMock()
            prov, _ = _scaffold_ci_pipeline(
                args,
                target,
                {},
                None,
                ask_dialog_question_fn=ask_mock,
                get_cli_arg_fn=_get_cli_arg,
                dry_run=False,
            )
            self.assertIsNone(prov)
            ask_mock.assert_not_called()
            self.assertEqual(list(target.rglob("*")), [])

    def test_dry_run_touches_no_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            args = _make_args(ci="jenkins", dry_run=True)
            prov, _ = _scaffold_ci_pipeline(
                args,
                target,
                {},
                None,
                ask_dialog_question_fn=MagicMock(),
                get_cli_arg_fn=_get_cli_arg,
                dry_run=True,
            )
            self.assertEqual(prov, "jenkins")
            # Nothing on disk
            self.assertEqual(list(target.rglob("*")), [])

    def test_collision_refuses_to_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            wf = target / ".github" / "workflows"
            wf.mkdir(parents=True)
            (wf / "fluid-standard.yml").write_text("# existing\n")
            # Pre-create every possible filename so we definitely collide
            for name in ("fluid-basic.yml", "fluid-pipeline.yml"):
                (wf / name).write_text("# existing\n")

            args = _make_args(ci="github_actions", ci_complexity="basic")
            prov, _ = _scaffold_ci_pipeline(
                args,
                target,
                {},
                None,
                ask_dialog_question_fn=MagicMock(),
                get_cli_arg_fn=_get_cli_arg,
                dry_run=False,
            )
            self.assertIsNone(prov)
            # Existing file untouched
            self.assertEqual((wf / "fluid-standard.yml").read_text(), "# existing\n")

    def test_killswitch_blocks_even_with_explicit_flag(self):
        with patch.dict(os.environ, {"FLUID_FORGE_AUTO_CI": "0"}, clear=False):
            with tempfile.TemporaryDirectory() as tmp:
                target = Path(tmp)
                args = _make_args(ci="github_actions")
                prov, _ = _scaffold_ci_pipeline(
                    args,
                    target,
                    {},
                    None,
                    ask_dialog_question_fn=MagicMock(),
                    get_cli_arg_fn=_get_cli_arg,
                    dry_run=False,
                )
        self.assertIsNone(prov)
        self.assertEqual(list(target.rglob("*")), [])

    def test_gitlab_ci_provider_writes_gitlab_yaml(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            args = _make_args(ci="gitlab_ci", ci_complexity="standard")
            prov, _ = _scaffold_ci_pipeline(
                args,
                target,
                {},
                None,
                ask_dialog_question_fn=MagicMock(),
                get_cli_arg_fn=_get_cli_arg,
                dry_run=False,
            )
            self.assertEqual(prov, "gitlab_ci")
            self.assertTrue((target / ".gitlab-ci.yml").is_file())


# ---------------------------------------------------------------------------
# CLI parser — verify the new flags register correctly
# ---------------------------------------------------------------------------


class TestForgeCiParser(unittest.TestCase):
    def _parser(self):
        from fluid_build.cli.forge import register

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        register(sub)
        return parser

    def test_default_values(self):
        parser = self._parser()
        args = parser.parse_args(["forge"])
        self.assertIsNone(args.ci)
        self.assertEqual(args.ci_complexity, "standard")
        self.assertFalse(args.no_ci)

    def test_ci_explicit_provider(self):
        parser = self._parser()
        args = parser.parse_args(["forge", "--ci", "jenkins"])
        self.assertEqual(args.ci, "jenkins")

    def test_ci_ask_sentinel(self):
        parser = self._parser()
        args = parser.parse_args(["forge", "--ci", "ask"])
        self.assertEqual(args.ci, "ask")

    def test_ci_none_sentinel(self):
        parser = self._parser()
        args = parser.parse_args(["forge", "--ci", "none"])
        self.assertEqual(args.ci, "none")

    def test_no_ci_flag(self):
        parser = self._parser()
        args = parser.parse_args(["forge", "--no-ci"])
        self.assertTrue(args.no_ci)

    def test_rejects_unknown_ci_provider(self):
        parser = self._parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["forge", "--ci", "not-real"])

    def test_rejects_unknown_complexity(self):
        parser = self._parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["forge", "--ci-complexity", "hyperdrive"])


# ---------------------------------------------------------------------------
# Taxonomy — ci_provider / ci_complexity normalization
# ---------------------------------------------------------------------------


class TestCiTaxonomyNormalization(unittest.TestCase):
    def test_github_aliases_normalize(self):
        from fluid_build.cli.forge_copilot_taxonomy import normalize_ci_provider

        for alias in ("gh", "github", "github_actions", "github-actions", "ghactions"):
            self.assertEqual(normalize_ci_provider(alias), "github_actions", f"alias={alias!r}")

    def test_gitlab_aliases_normalize(self):
        from fluid_build.cli.forge_copilot_taxonomy import normalize_ci_provider

        for alias in ("gl", "gitlab", "gitlab_ci", "GitLab CI"):
            self.assertEqual(normalize_ci_provider(alias), "gitlab_ci")

    def test_azure_aliases_normalize(self):
        from fluid_build.cli.forge_copilot_taxonomy import normalize_ci_provider

        for alias in ("azdo", "azure", "azure-devops", "Azure DevOps"):
            self.assertEqual(normalize_ci_provider(alias), "azure_devops")

    def test_invalid_provider_returns_none(self):
        from fluid_build.cli.forge_copilot_taxonomy import normalize_ci_provider

        self.assertIsNone(normalize_ci_provider("not-a-real-ci"))
        self.assertIsNone(normalize_ci_provider(""))
        self.assertIsNone(normalize_ci_provider(None))

    def test_complexity_normalization(self):
        from fluid_build.cli.forge_copilot_taxonomy import normalize_ci_complexity

        self.assertEqual(normalize_ci_complexity("BASIC"), "basic")
        self.assertEqual(normalize_ci_complexity(" standard "), "standard")
        self.assertIsNone(normalize_ci_complexity("hyperdrive"))
        self.assertIsNone(normalize_ci_complexity(None))

    def test_normalize_copilot_context_canonicalizes_ci_fields(self):
        from fluid_build.cli.forge_copilot_taxonomy import normalize_copilot_context

        result = normalize_copilot_context({"ci_provider": "gh", "ci_complexity": "ADVANCED"})
        self.assertEqual(result["ci_provider"], "github_actions")
        self.assertEqual(result["ci_complexity"], "advanced")

    def test_normalize_copilot_context_drops_invalid_ci_values(self):
        from fluid_build.cli.forge_copilot_taxonomy import normalize_copilot_context

        result = normalize_copilot_context({"ci_provider": "bogus", "ci_complexity": "bogus"})
        self.assertNotIn("ci_provider", result)
        self.assertNotIn("ci_complexity", result)


# ---------------------------------------------------------------------------
# Module constants — sanity check stayed in sync with pipeline_templates
# ---------------------------------------------------------------------------


class TestCiConstants(unittest.TestCase):
    def test_provider_values_match_pipeline_provider_enum(self):
        """_CI_PROVIDER_VALUES uses the CLI-facing names, which differ
        from the underlying PipelineProvider enum for the CircleCI
        entry (``circleci`` vs ``circle_ci``).  Slice UX-E added an
        alias map so both spellings flow through ``_resolve_ci_choice``
        — this assertion normalises the enum side before comparing."""
        from fluid_build.cli.forge_modes import _CI_PROVIDER_ALIASES
        from fluid_build.forge.core.pipeline_templates import PipelineProvider

        def _normalise(value: str) -> str:
            return _CI_PROVIDER_ALIASES.get(value, value)

        enum_values = {_normalise(p.value) for p in PipelineProvider}
        self.assertEqual(_CI_PROVIDER_VALUES, enum_values)

    def test_complexity_values_match_pipeline_complexity_enum(self):
        from fluid_build.forge.core.pipeline_templates import PipelineComplexity

        enum_values = {c.value for c in PipelineComplexity}
        self.assertEqual(_CI_COMPLEXITY_VALUES, enum_values)


if __name__ == "__main__":
    unittest.main()
