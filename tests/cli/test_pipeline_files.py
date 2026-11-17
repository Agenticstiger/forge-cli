# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for fluid_build.cli._pipeline_files (config coercion and the file writer)."""

import inspect
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from fluid_build.cli._pipeline_files import (
    _prepend_header,
    build_pipeline_config,
    write_pipeline_files,
)
from fluid_build.forge.core.pipeline_templates import PipelineProvider


class TestBuildPipelineConfig:
    def test_coerces_string_provider(self):
        config = build_pipeline_config(provider="github_actions")
        assert config.provider == PipelineProvider.GITHUB_ACTIONS
        assert config.add_ons == ()

    def test_circleci_alias(self):
        config = build_pipeline_config(provider="circleci")
        assert config.provider == PipelineProvider.CIRCLE_CI

    def test_unknown_provider_raises_valueerror(self):
        with pytest.raises(ValueError, match="Unknown CI provider"):
            build_pipeline_config(provider="nope")

    def test_add_ons_plumbed_through(self):
        config = build_pipeline_config(provider="github_actions", add_ons=["security", "sbom"])
        assert config.add_ons == ("security", "sbom")

    def test_cloud_auth_plumbed_through(self):
        config = build_pipeline_config(provider="github_actions", cloud_auth=["aws"])
        assert config.cloud_auth == ("aws",)

    def test_complexity_is_gone(self):
        assert "complexity" not in inspect.signature(build_pipeline_config).parameters


class TestPrependHeader:
    def test_header_comes_first(self):
        assert _prepend_header("body\n", "# h\n") == "# h\nbody\n"

    def test_missing_newline_is_added(self):
        assert _prepend_header("body\n", "# h") == "# h\nbody\n"


class TestWritePipelineFiles:
    def test_writes_files_to_output_dir(self, tmp_path: Path):
        files = {".github/workflows/a.yml": "a\n", "Jenkinsfile": "pipeline {}\n"}
        written = write_pipeline_files(files, tmp_path, inject_header=False)
        assert len(written) == 2
        assert (tmp_path / "Jenkinsfile").is_file()
        assert (tmp_path / ".github/workflows/a.yml").read_text() == "a\n"

    def test_creates_parent_dirs(self, tmp_path: Path):
        out = tmp_path / "does/not/exist/yet"
        write_pipeline_files({".github/workflows/b.yml": "b\n"}, out)
        assert (out / ".github/workflows/b.yml").is_file()

    def test_default_command_names_generate_ci(self, tmp_path: Path):
        write_pipeline_files({"a.yml": "a\n"}, tmp_path)
        assert "# Command: fluid generate ci" in (tmp_path / "a.yml").read_text()

    def test_dry_run_creates_no_files(self, tmp_path: Path):
        with patch("fluid_build.cli._pipeline_files.cprint") as mock_cprint:
            written = write_pipeline_files(
                {".github/workflows/c.yml": "c\n"}, tmp_path, dry_run=True
            )
        assert len(written) == 1
        assert not (tmp_path / ".github/workflows/c.yml").exists()
        assert "would write" in " ".join(str(c) for c in mock_cprint.call_args_list)

    def test_dry_run_with_console_uses_console_print(self, tmp_path: Path):
        console = MagicMock()
        write_pipeline_files({"d.yml": "d\n"}, tmp_path, dry_run=True, console=console)
        console.print.assert_called()
        assert "would write" in str(console.print.call_args_list[0])

    def test_returns_paths_in_insertion_order(self, tmp_path: Path):
        written = write_pipeline_files({"a.yml": "1", "b.yml": "2", "c.yml": "3"}, tmp_path)
        assert [p.name for p in written] == ["a.yml", "b.yml", "c.yml"]


def test_build_pipeline_config_follows_the_remotes_default_branch(monkeypatch) -> None:
    monkeypatch.setattr(
        "fluid_build.cli._pipeline_files.detect_default_branch",
        lambda directory=None: "develop",
    )
    assert build_pipeline_config("github_actions").default_branch == "develop"
    assert build_pipeline_config("github_actions", default_branch="trunk").default_branch == "trunk"


def test_detect_default_branch_outside_a_repository_is_main(tmp_path: Path) -> None:
    from fluid_build.cli._pipeline_files import detect_default_branch

    assert detect_default_branch(tmp_path) == "main"


def test_legacy_ci_files_names_what_the_old_tiers_left(tmp_path: Path) -> None:
    from fluid_build.cli._pipeline_files import legacy_ci_files

    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "fluid-standard.yml").write_text("on: push\n")
    (workflows / "fluid-pipeline.yml").write_text("on: push\n")
    written = [".github/workflows/fluid-pipeline.yml"]
    assert legacy_ci_files("github_actions", written, tmp_path) == [
        ".github/workflows/fluid-standard.yml"
    ]
    assert legacy_ci_files("gitlab_ci", written, tmp_path) == []
