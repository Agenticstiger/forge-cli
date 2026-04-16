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

"""Unit tests for fluid_build.cli.pipeline_generator — run() and helper functions."""

import argparse
import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from fluid_build.cli.pipeline_generator import (
    _show_next_steps,
    build_pipeline_config,
    register,
    run,
    write_pipeline_files,
)
from fluid_build.forge.core.pipeline_templates import (
    PipelineComplexity,
    PipelineConfig,
    PipelineProvider,
    PipelineTemplateGenerator,
)

LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_args(**kwargs):
    """Build a minimal argparse Namespace for pipeline-generator tests."""
    defaults = dict(
        provider="github_actions",
        complexity="standard",
        environments=["dev", "staging", "prod"],
        enable_approvals=False,
        enable_security_scan=True,
        enable_marketplace=False,
        output_dir=".",
        preview=False,
        interactive=False,
    )
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


# ---------------------------------------------------------------------------
# register
# ---------------------------------------------------------------------------


class TestRegisterPipelineGenerator(unittest.TestCase):
    def test_register_adds_subparser(self):
        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()
        register(subparsers)
        args = parser.parse_args(["generate-pipeline", "--provider", "github_actions"])
        self.assertEqual(args.provider, "github_actions")

    def test_register_default_complexity(self):
        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()
        register(subparsers)
        args = parser.parse_args(["generate-pipeline"])
        self.assertEqual(args.complexity, "standard")

    def test_register_default_environments(self):
        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()
        register(subparsers)
        args = parser.parse_args(["generate-pipeline"])
        self.assertEqual(args.environments, ["dev", "staging", "prod"])

    def test_register_preview_flag(self):
        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()
        register(subparsers)
        args = parser.parse_args(["generate-pipeline", "--preview"])
        self.assertTrue(args.preview)


# ---------------------------------------------------------------------------
# PipelineConfig / PipelineTemplateGenerator
# ---------------------------------------------------------------------------


class TestPipelineConfig(unittest.TestCase):
    def test_basic_complexity_defaults_to_dev_only(self):
        config = PipelineConfig(
            provider=PipelineProvider.GITHUB_ACTIONS,
            complexity=PipelineComplexity.BASIC,
        )
        self.assertEqual(config.environments, ["dev"])

    def test_standard_complexity_has_two_envs(self):
        config = PipelineConfig(
            provider=PipelineProvider.GITLAB_CI,
            complexity=PipelineComplexity.STANDARD,
        )
        self.assertEqual(config.environments, ["dev", "staging"])

    def test_enterprise_complexity_has_three_envs(self):
        config = PipelineConfig(
            provider=PipelineProvider.AZURE_DEVOPS,
            complexity=PipelineComplexity.ENTERPRISE,
        )
        self.assertEqual(config.environments, ["dev", "staging", "prod"])

    def test_custom_environments_preserved(self):
        config = PipelineConfig(
            provider=PipelineProvider.JENKINS,
            complexity=PipelineComplexity.ADVANCED,
            environments=["qa", "prod"],
        )
        self.assertEqual(config.environments, ["qa", "prod"])

    def test_notification_channels_defaults_to_empty(self):
        config = PipelineConfig(
            provider=PipelineProvider.GITHUB_ACTIONS,
            complexity=PipelineComplexity.BASIC,
        )
        self.assertEqual(config.notification_channels, [])


class TestPipelineTemplateGenerator(unittest.TestCase):
    def test_list_available_providers_returns_all(self):
        gen = PipelineTemplateGenerator()
        providers = gen.list_available_providers()
        self.assertIn("github_actions", providers)
        self.assertIn("gitlab_ci", providers)
        self.assertEqual(len(providers), 7)

    def test_generate_github_actions_basic(self):
        config = PipelineConfig(
            provider=PipelineProvider.GITHUB_ACTIONS,
            complexity=PipelineComplexity.BASIC,
        )
        gen = PipelineTemplateGenerator()
        files = gen.generate_pipeline(config)
        self.assertGreater(len(files), 0)
        filename = next(iter(files))
        self.assertIn("github", filename.lower())

    def test_generate_gitlab_ci(self):
        config = PipelineConfig(
            provider=PipelineProvider.GITLAB_CI,
            complexity=PipelineComplexity.STANDARD,
        )
        gen = PipelineTemplateGenerator()
        files = gen.generate_pipeline(config)
        self.assertGreater(len(files), 0)

    def test_generate_unsupported_provider_raises(self):
        gen = PipelineTemplateGenerator()
        bad_provider = MagicMock()
        bad_provider.value = "unsupported"
        config = MagicMock()
        config.provider = bad_provider
        # Override templates to exclude this provider
        gen.templates = {}
        with self.assertRaises(ValueError):
            gen.generate_pipeline(config)

    def test_get_provider_features_github(self):
        gen = PipelineTemplateGenerator()
        features = gen.get_provider_features(PipelineProvider.GITHUB_ACTIONS)
        self.assertIn("multi_environment", features)
        self.assertTrue(features["multi_environment"])

    def test_get_provider_features_unknown(self):
        gen = PipelineTemplateGenerator()
        features = gen.get_provider_features(MagicMock())
        self.assertEqual(features, {})


# ---------------------------------------------------------------------------
# run()
# ---------------------------------------------------------------------------


class TestRunPipelineGenerator(unittest.TestCase):
    def test_run_with_provider_returns_zero(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            args = _make_args(
                provider="github_actions",
                complexity="basic",
                output_dir=tmpdir,
            )
            result = run(args, LOG)
            self.assertEqual(result, 0)

    def test_run_creates_files_in_output_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            args = _make_args(
                provider="github_actions",
                complexity="basic",
                output_dir=tmpdir,
            )
            run(args, LOG)
            created = list(Path(tmpdir).rglob("*.yml")) + list(Path(tmpdir).rglob("*.yaml"))
            self.assertGreater(len(created), 0)

    def test_run_preview_mode_returns_zero(self):
        args = _make_args(
            provider="github_actions",
            complexity="basic",
            preview=True,
        )
        with patch("fluid_build.cli.pipeline_generator.cprint"):
            result = run(args, LOG)
        self.assertEqual(result, 0)

    def test_run_returns_one_on_exception(self):
        args = _make_args(provider="github_actions")
        with patch(
            "fluid_build.cli.pipeline_generator.PipelineTemplateGenerator",
            side_effect=RuntimeError("boom"),
        ):
            result = run(args, LOG)
        self.assertEqual(result, 1)

    def test_run_gitlab_ci_provider(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            args = _make_args(
                provider="gitlab_ci",
                complexity="standard",
                output_dir=tmpdir,
            )
            result = run(args, LOG)
            self.assertEqual(result, 0)


# ---------------------------------------------------------------------------
# _show_next_steps
# ---------------------------------------------------------------------------


class TestShowNextSteps(unittest.TestCase):
    @patch("fluid_build.cli.pipeline_generator.cprint")
    def test_show_next_steps_github(self, mock_cprint):
        _show_next_steps("github_actions", Path("/tmp/out"))
        calls = " ".join(str(c) for c in mock_cprint.call_args_list)
        self.assertIn("secrets", calls.lower())

    @patch("fluid_build.cli.pipeline_generator.cprint")
    def test_show_next_steps_gitlab(self, mock_cprint):
        _show_next_steps("gitlab_ci", Path("/tmp/out"))
        calls = " ".join(str(c) for c in mock_cprint.call_args_list)
        self.assertIn("gitlab", calls.lower())

    @patch("fluid_build.cli.pipeline_generator.cprint")
    def test_show_next_steps_azure(self, mock_cprint):
        _show_next_steps("azure_devops", Path("/tmp/out"))
        calls = " ".join(str(c) for c in mock_cprint.call_args_list)
        self.assertIn("azure", calls.lower())

    @patch("fluid_build.cli.pipeline_generator.cprint")
    def test_show_next_steps_jenkins(self, mock_cprint):
        _show_next_steps("jenkins", Path("/tmp/out"))
        calls = " ".join(str(c) for c in mock_cprint.call_args_list)
        self.assertIn("jenkins", calls.lower())

    @patch("fluid_build.cli.pipeline_generator.cprint")
    def test_show_next_steps_generic(self, mock_cprint):
        _show_next_steps("circleci", Path("/tmp/out"))
        # Generic branch — should still print something
        self.assertTrue(mock_cprint.called)


# ---------------------------------------------------------------------------
# build_pipeline_config — extracted factory helper
# ---------------------------------------------------------------------------


class TestBuildPipelineConfig(unittest.TestCase):
    def test_coerces_string_provider_and_complexity(self):
        config = build_pipeline_config(provider="github_actions", complexity="basic")
        self.assertEqual(config.provider, PipelineProvider.GITHUB_ACTIONS)
        self.assertEqual(config.complexity, PipelineComplexity.BASIC)

    def test_none_environments_delegates_to_dataclass_defaults(self):
        config = build_pipeline_config(provider="jenkins", complexity="basic")
        # __post_init__ picks dev-only for BASIC
        self.assertEqual(config.environments, ["dev"])

    def test_explicit_environments_preserved(self):
        config = build_pipeline_config(
            provider="gitlab_ci",
            complexity="advanced",
            environments=["qa", "prod"],
        )
        self.assertEqual(config.environments, ["qa", "prod"])

    def test_unknown_provider_raises_valueerror(self):
        with self.assertRaises(ValueError) as ctx:
            build_pipeline_config(provider="nope", complexity="standard")
        self.assertIn("Unknown CI provider", str(ctx.exception))

    def test_unknown_complexity_raises_valueerror(self):
        with self.assertRaises(ValueError) as ctx:
            build_pipeline_config(provider="jenkins", complexity="nope")
        self.assertIn("Unknown pipeline complexity", str(ctx.exception))

    def test_flags_plumbed_through(self):
        config = build_pipeline_config(
            provider="github_actions",
            complexity="enterprise",
            enable_approvals=True,
            enable_security_scan=False,
            enable_marketplace_publishing=True,
        )
        self.assertTrue(config.enable_approvals)
        self.assertFalse(config.enable_security_scan)
        self.assertTrue(config.enable_marketplace_publishing)


# ---------------------------------------------------------------------------
# write_pipeline_files — extracted writer helper
# ---------------------------------------------------------------------------


class TestWritePipelineFiles(unittest.TestCase):
    def test_writes_files_to_output_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            files = {
                ".github/workflows/a.yml": "a\n",
                "Jenkinsfile": "pipeline {}\n",
            }
            # Slice 7 injects a DO-NOT-EDIT header by default.  This
            # legacy test asserts raw body contents, so opt out.
            written = write_pipeline_files(files, out, inject_header=False)
            self.assertEqual(len(written), 2)
            self.assertTrue((out / ".github/workflows/a.yml").is_file())
            self.assertTrue((out / "Jenkinsfile").is_file())
            self.assertEqual((out / ".github/workflows/a.yml").read_text(), "a\n")

    def test_creates_parent_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "does/not/exist/yet"
            files = {".github/workflows/b.yml": "b\n"}
            write_pipeline_files(files, out)
            self.assertTrue((out / ".github/workflows/b.yml").is_file())

    def test_dry_run_creates_no_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            files = {".github/workflows/c.yml": "c\n"}
            with patch("fluid_build.cli.pipeline_generator.cprint") as mock_cprint:
                written = write_pipeline_files(files, out, dry_run=True)
            # Path returned but not created
            self.assertEqual(len(written), 1)
            self.assertFalse((out / ".github/workflows/c.yml").exists())
            # "would write" message emitted
            calls = " ".join(str(c) for c in mock_cprint.call_args_list)
            self.assertIn("would write", calls)

    def test_dry_run_with_console_uses_console_print(self):
        console = MagicMock()
        with tempfile.TemporaryDirectory() as tmp:
            write_pipeline_files(
                {".github/workflows/d.yml": "d\n"},
                Path(tmp),
                dry_run=True,
                console=console,
            )
        console.print.assert_called()
        self.assertIn("would write", str(console.print.call_args_list[0]))

    def test_returns_paths_in_insertion_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            files = {"a.yml": "1", "b.yml": "2", "c.yml": "3"}
            written = write_pipeline_files(files, out)
            names = [p.name for p in written]
            self.assertEqual(names, ["a.yml", "b.yml", "c.yml"])


if __name__ == "__main__":
    unittest.main()
