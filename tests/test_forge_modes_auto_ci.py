# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Tests for the auto-CI hook inside ``run_ai_copilot_mode``.

These tests exercise ``_resolve_ci_choice`` and ``_scaffold_ci_pipeline``
directly, plus the forge argparse wiring for ``--ci`` / ``--no-ci``. Full end-to-end ``run_ai_copilot_mode`` coverage for
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
    _CI_PROVIDER_VALUES,
    _resolve_ci_choice,
    _scaffold_ci_pipeline,
)


def _get_cli_arg(args, name, default=None):
    return getattr(args, name, default)


def _make_args(**overrides):
    defaults = dict(
        ci=None,
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
            prov = _resolve_ci_choice(
                args,
                {},
                is_interactive=False,
                ask_dialog_question_fn=MagicMock(),
                get_cli_arg_fn=_get_cli_arg,
            )
        self.assertIsNone(prov)

    def test_no_ci_flag_forces_skip(self):
        args = _make_args(no_ci=True, ci="github_actions")
        prov = _resolve_ci_choice(
            args,
            {},
            is_interactive=True,
            ask_dialog_question_fn=MagicMock(),
            get_cli_arg_fn=_get_cli_arg,
        )
        self.assertIsNone(prov)

    def test_ci_none_sentinel_skips(self):
        args = _make_args(ci="none")
        prov = _resolve_ci_choice(
            args,
            {},
            is_interactive=True,
            ask_dialog_question_fn=MagicMock(),
            get_cli_arg_fn=_get_cli_arg,
        )
        self.assertIsNone(prov)

    def test_explicit_provider_non_interactive(self):
        args = _make_args(ci="github_actions")
        prov = _resolve_ci_choice(
            args,
            {},
            is_interactive=False,
            ask_dialog_question_fn=MagicMock(),
            get_cli_arg_fn=_get_cli_arg,
        )
        self.assertEqual(prov, "github_actions")

    def test_non_interactive_without_flag_and_no_context_skips(self):
        """Non-interactive + no flag + empty context → silent skip."""
        args = _make_args()  # ci=None, non_interactive=True
        ask_mock = MagicMock()
        prov = _resolve_ci_choice(
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
        prov = _resolve_ci_choice(
            args,
            {"ci_provider": "github_actions"},
            is_interactive=False,
            ask_dialog_question_fn=ask_mock,
            get_cli_arg_fn=_get_cli_arg,
        )
        self.assertEqual(prov, "github_actions")
        ask_mock.assert_not_called()

    def test_non_interactive_with_unknown_provider_in_context_skips(self):
        """Unknown provider in context falls back to silent skip."""
        args = _make_args()
        prov = _resolve_ci_choice(
            args,
            {"ci_provider": "not-a-real-provider"},
            is_interactive=False,
            ask_dialog_question_fn=MagicMock(),
            get_cli_arg_fn=_get_cli_arg,
        )
        self.assertIsNone(prov)

    def test_interactive_ask_opens_menu_and_uses_memory_default(self):
        args = _make_args(ci="ask", non_interactive=False)
        # Fake dialog result: the user picks jenkins
        ask_mock = MagicMock(return_value=MagicMock(value="jenkins"))
        prov = _resolve_ci_choice(
            args,
            {"ci_provider": "github_actions"},
            is_interactive=True,
            ask_dialog_question_fn=ask_mock,
            get_cli_arg_fn=_get_cli_arg,
        )
        self.assertEqual(prov, "jenkins")
        # The provider is the only question.
        self.assertEqual(ask_mock.call_count, 1)

    def test_interactive_menu_cancel_returns_none(self):
        args = _make_args(non_interactive=False)
        ask_mock = MagicMock(return_value=MagicMock(value=None))
        prov = _resolve_ci_choice(
            args,
            {},
            is_interactive=True,
            ask_dialog_question_fn=ask_mock,
            get_cli_arg_fn=_get_cli_arg,
        )
        self.assertIsNone(prov)
        self.assertEqual(ask_mock.call_count, 1)

    def test_unknown_provider_flag_warns_and_skips(self):
        args = _make_args(ci="not-a-real-provider")
        console = MagicMock()
        prov = _resolve_ci_choice(
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


# ---------------------------------------------------------------------------
# _scaffold_ci_pipeline — full hook with real PipelineTemplateGenerator
# ---------------------------------------------------------------------------


class TestScaffoldCiPipeline(unittest.TestCase):
    def test_explicit_flag_writes_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            args = _make_args(ci="github_actions")
            prov = _scaffold_ci_pipeline(
                args,
                target,
                {},
                None,
                ask_dialog_question_fn=MagicMock(),
                get_cli_arg_fn=_get_cli_arg,
                dry_run=False,
            )
            self.assertEqual(prov, "github_actions")
            created = list(target.rglob("*.yml"))
            self.assertTrue(any(".github/workflows" in str(p) for p in created))

    def test_no_ci_flag_creates_no_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            args = _make_args(no_ci=True)
            prov = _scaffold_ci_pipeline(
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
            prov = _scaffold_ci_pipeline(
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
            prov = _scaffold_ci_pipeline(
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

            args = _make_args(ci="github_actions")
            prov = _scaffold_ci_pipeline(
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
                prov = _scaffold_ci_pipeline(
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
            args = _make_args(ci="gitlab_ci")
            prov = _scaffold_ci_pipeline(
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
        self.assertFalse(hasattr(args, "ci_complexity"))
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

    def test_ci_complexity_flag_is_gone(self):
        parser = self._parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["forge", "--ci-complexity", "standard"])


# ---------------------------------------------------------------------------
# Taxonomy: ci_provider normalization
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

    def test_normalize_copilot_context_canonicalizes_ci_provider_and_drops_complexity(self):
        from fluid_build.cli.forge_copilot_taxonomy import normalize_copilot_context

        result = normalize_copilot_context({"ci_provider": "gh", "ci_complexity": "advanced"})
        self.assertEqual(result["ci_provider"], "github_actions")
        self.assertNotIn("ci_complexity", result)

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


if __name__ == "__main__":
    unittest.main()
