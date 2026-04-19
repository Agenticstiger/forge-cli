# Copyright 2024-2026 Agentics Transformation Ltd
# Licensed under the Apache License, Version 2.0

"""Tests for static CI generation and Jenkins pipeline behavior.

The ``fluid generate ci`` command supports GitHub, GitLab, and Jenkins.
This module keeps the deeper Jenkins simulation coverage while also asserting
the static cross-system generation path used by BizLab acceptance tests.
"""

from __future__ import annotations

import argparse
import logging
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

from fluid_build.cli.generate_ci import run as generate_ci_run
from fluid_build.cli.pipeline_generator import _comment_prefix_for
from fluid_build.cli.scaffold_ci import _DEFAULT_PATHS, _TEMPLATES, GITHUB, GITLAB, JENKINS
from fluid_build.forge.core.pipeline_templates import (
    PipelineComplexity,
    PipelineConfig,
    PipelineProvider,
    PipelineTemplateGenerator,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_logger = logging.getLogger("test_generate_ci_jenkins")


def _make_args(system: str = "jenkins", out: Optional[str] = None) -> argparse.Namespace:
    return argparse.Namespace(system=system, out=out, contract="contract.fluid.yaml")


_STATIC_SYSTEM_CASES = (
    ("github", ".github/workflows/fluid.yml", GITHUB, ("name: FLUID", "runs-on: ubuntu-latest")),
    ("gitlab", ".gitlab-ci.yml", GITLAB, ("stages:", "validate:")),
    ("jenkins", "Jenkinsfile", JENKINS, ("pipeline {", "stage('Validate')")),
)


# ---------------------------------------------------------------------------
# 1. Static JENKINS template
# ---------------------------------------------------------------------------


class TestJenkinsStaticTemplate:
    """Validate the ``JENKINS`` constant in scaffold_ci.py."""

    def test_is_nonempty_string(self):
        assert isinstance(JENKINS, str)
        assert len(JENKINS.strip()) > 0

    def test_contains_pipeline_block(self):
        assert "pipeline {" in JENKINS

    def test_contains_stages_block(self):
        assert "stages {" in JENKINS

    def test_contains_validate_stage(self):
        assert "stage('Validate')" in JENKINS

    def test_contains_plan_stage(self):
        assert "stage('Plan')" in JENKINS

    def test_contains_apply_stage(self):
        assert "stage('Apply')" in JENKINS

    def test_contains_test_stage(self):
        assert "stage('Test')" in JENKINS

    def test_uses_groovy_comment_syntax(self):
        """Template should use ``//`` comments, not ``#`` (YAML style)."""
        first_line = JENKINS.strip().splitlines()[0]
        assert first_line.startswith("//")

    def test_references_fluid_commands(self):
        # Generated CI calls the public ``fluid`` console script rather
        # than the internal ``python -m fluid_build.cli`` path — avoids
        # assuming ``fluid_build`` is importable on the CI runner.
        assert "fluid validate" in JENKINS
        assert "fluid apply" in JENKINS
        assert "fluid generate speed-transformation" in JENKINS

    def test_has_dbt_airflow_publish_stages(self):
        """B1 demo requires these three deploy/publish hooks."""
        # dbt deployment rides on `fluid apply --build`, asserted above.
        assert "Airflow DAG Sync" in JENKINS
        assert "stage('Publish')" in JENKINS
        assert "fluid publish" in JENKINS

    def test_has_post_cleanup(self):
        assert "cleanWs()" in JENKINS

    def test_has_approval_gate(self):
        assert "input {" in JENKINS

    def test_registered_in_templates_dict(self):
        assert "jenkins" in _TEMPLATES
        assert _TEMPLATES["jenkins"] is JENKINS

    def test_default_output_path(self):
        assert _DEFAULT_PATHS["jenkins"] == "Jenkinsfile"


# ---------------------------------------------------------------------------
# 2. generate_ci CLI
# ---------------------------------------------------------------------------


class TestGenerateCIJenkins:
    """Test ``fluid generate ci --system jenkins`` via ``generate_ci.run()``."""

    def test_writes_jenkinsfile(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        rc = generate_ci_run(_make_args(), _logger)
        assert rc == 0
        assert (tmp_path / "Jenkinsfile").exists()

    def test_default_output_is_jenkinsfile(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        generate_ci_run(_make_args(), _logger)
        content = (tmp_path / "Jenkinsfile").read_text()
        assert "pipeline {" in content

    def test_custom_output_path(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        custom = str(tmp_path / "ci" / "MyJenkinsfile")
        generate_ci_run(_make_args(out=custom), _logger)
        assert Path(custom).exists()

    def test_content_matches_template(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        generate_ci_run(_make_args(), _logger)
        written = (tmp_path / "Jenkinsfile").read_text()
        assert written == JENKINS

    def test_returns_zero_on_success(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert generate_ci_run(_make_args(), _logger) == 0


class TestGenerateCIStaticSystems:
    """Cross-system coverage for ``fluid generate ci`` default paths and output."""

    @pytest.mark.parametrize(
        ("system", "default_path", "template", "tokens"),
        _STATIC_SYSTEM_CASES,
    )
    def test_registered_in_templates_dict(self, system, default_path, template, tokens):
        assert _TEMPLATES[system] is template
        assert _DEFAULT_PATHS[system] == default_path
        for token in tokens:
            assert token in template

    @pytest.mark.parametrize(
        ("system", "default_path", "template", "_tokens"),
        _STATIC_SYSTEM_CASES,
    )
    def test_generate_ci_writes_default_output(
        self, tmp_path, monkeypatch, system, default_path, template, _tokens
    ):
        monkeypatch.chdir(tmp_path)
        rc = generate_ci_run(_make_args(system=system), _logger)
        assert rc == 0
        written = tmp_path / default_path
        assert written.exists()
        assert written.read_text() == template

    @pytest.mark.parametrize(
        ("system", "_default_path", "template", "tokens"),
        _STATIC_SYSTEM_CASES,
    )
    def test_generate_ci_supports_custom_output(
        self, tmp_path, monkeypatch, system, _default_path, template, tokens
    ):
        monkeypatch.chdir(tmp_path)
        suffix = "Jenkinsfile" if system == "jenkins" else f"{system}.yml"
        custom = tmp_path / "generated" / suffix
        rc = generate_ci_run(_make_args(system=system, out=str(custom)), _logger)
        assert rc == 0
        assert custom.exists()
        content = custom.read_text()
        assert content == template
        for token in tokens:
            assert token in content


# ---------------------------------------------------------------------------
# 3. Advanced JenkinsTemplate via PipelineTemplateGenerator
# ---------------------------------------------------------------------------


class TestJenkinsPipelineTemplateGenerator:
    """Test ``JenkinsTemplate`` from pipeline_templates.py."""

    @pytest.fixture()
    def generator(self):
        return PipelineTemplateGenerator()

    def _generate(self, generator, complexity="standard", **kwargs):
        config = PipelineConfig(
            provider=PipelineProvider.JENKINS,
            complexity=PipelineComplexity(complexity),
            **kwargs,
        )
        return generator.generate_pipeline(config)

    def test_output_contains_jenkinsfile_key(self, generator):
        result = self._generate(generator)
        assert "Jenkinsfile" in result

    def test_output_contains_pipeline_block(self, generator):
        result = self._generate(generator)
        assert "pipeline {" in result["Jenkinsfile"]

    def test_output_contains_expected_stages(self, generator):
        content = self._generate(generator)["Jenkinsfile"]
        for stage in ("Setup", "Validate", "Plan", "Test"):
            assert f"stage('{stage}" in content or f'stage("{stage}' in content

    def test_output_contains_deploy_stages(self, generator):
        content = self._generate(generator, environments=["dev", "staging"])["Jenkinsfile"]
        assert "Deploy to DEV" in content
        assert "Deploy to STAGING" in content

    def test_output_contains_publish_stage(self, generator):
        content = self._generate(generator)["Jenkinsfile"]
        assert "Publish" in content

    @pytest.mark.parametrize("complexity", ["basic", "standard", "advanced", "enterprise"])
    def test_all_complexity_levels(self, generator, complexity):
        result = self._generate(generator, complexity=complexity)
        assert "Jenkinsfile" in result
        assert len(result["Jenkinsfile"]) > 0

    def test_marketplace_publishing_enabled(self, generator):
        content = self._generate(generator, enable_marketplace_publishing=True)["Jenkinsfile"]
        assert "marketplace" in content.lower() or "publish" in content.lower()

    def test_marketplace_publishing_disabled(self, generator):
        content = self._generate(generator, enable_marketplace_publishing=False)["Jenkinsfile"]
        assert "marketplace" not in content.lower() or "marketplace_publish" not in content.lower()

    def test_prod_environment_has_approval_gate(self, generator):
        content = self._generate(generator, environments=["dev", "prod"])["Jenkinsfile"]
        assert "input {" in content

    def test_comment_prefix_is_double_slash(self):
        assert _comment_prefix_for("Jenkinsfile") == "//"


# ---------------------------------------------------------------------------
# 4. Simulated Jenkins Pipeline Run
# ---------------------------------------------------------------------------


class _JenkinsStage:
    """Parsed representation of a Jenkins stage."""

    __slots__ = ("name", "sh_commands", "when_branch", "has_input", "post_actions")

    def __init__(self, name: str):
        self.name = name
        self.sh_commands: List[str] = []
        self.when_branch: Optional[str] = None  # e.g. "main"
        self.has_input: bool = False
        self.post_actions: List[str] = []  # e.g. ["archiveArtifacts ...", "junit ..."]


class _JenkinsPipeline:
    """Minimal parser for a generated Jenkinsfile — enough to extract
    stage names, ``sh`` commands, ``when`` branch guards, ``input`` gates,
    and ``post`` actions so we can *simulate* a run.
    """

    def __init__(self, content: str):
        self.stages: List[_JenkinsStage] = []
        self.global_post_always: List[str] = []
        self._parse(content)

    # -- parsing helpers ---------------------------------------------------

    def _parse(self, content: str):
        # Extract stages
        stage_pattern = re.compile(r"stage\(['\"](.+?)['\"]\)\s*\{", re.MULTILINE)
        stage_names = stage_pattern.findall(content)

        # Split content by stages for per-stage parsing
        parts = re.split(r"stage\(['\"](.+?)['\"]\)\s*\{", content)

        # parts[0] is before first stage, then alternating name/body
        for i in range(1, len(parts) - 1, 2):
            name = parts[i]
            body = parts[i + 1] if i + 1 < len(parts) else ""
            stage = _JenkinsStage(name)

            # Extract sh commands
            for m in re.finditer(r"sh\s+['\"](.+?)['\"]", body):
                stage.sh_commands.append(m.group(1))

            # Detect when { branch 'xxx' }
            when_match = re.search(r"when\s*\{[^}]*branch\s+['\"](\w+)['\"]", body)
            if when_match:
                stage.when_branch = when_match.group(1)

            # Detect input gate
            if "input {" in body or "input{" in body:
                stage.has_input = True

            # Detect post actions
            for m in re.finditer(r"(archiveArtifacts|junit|cleanWs)\b[^}\n]*", body):
                stage.post_actions.append(m.group(0).strip())

            self.stages.append(stage)

        # Detect global post { always { cleanWs() } } — search from the end
        # of the content (after all stages close).
        if re.search(r"post\s*\{[^}]*always\s*\{[^}]*cleanWs\(\)", content, re.DOTALL):
            self.global_post_always.append("cleanWs()")


class _SimulatedJenkinsRunner:
    """Simulates walking through a parsed Jenkins pipeline, executing
    ``sh`` commands against a mocked subprocess and honouring ``when``
    branch guards and ``input`` approval gates.
    """

    def __init__(
        self,
        pipeline: _JenkinsPipeline,
        *,
        branch: str = "main",
        approve_inputs: bool = True,
        fail_command: Optional[str] = None,
    ):
        self.pipeline = pipeline
        self.branch = branch
        self.approve_inputs = approve_inputs
        self.fail_command = fail_command  # substring match → exit 1

        # Results
        self.executed_commands: List[str] = []
        self.skipped_stages: List[str] = []
        self.failed_stage: Optional[str] = None
        self.post_actions_recorded: List[str] = []
        self.cleanup_ran: bool = False

    def run(self) -> bool:
        """Return True if pipeline passed, False if a stage failed."""
        success = True
        for stage in self.pipeline.stages:
            # -- branch guard --
            if stage.when_branch and stage.when_branch != self.branch:
                self.skipped_stages.append(stage.name)
                continue

            # -- approval gate --
            if stage.has_input and not self.approve_inputs:
                self.skipped_stages.append(stage.name)
                continue

            # -- execute sh commands --
            stage_ok = True
            for cmd in stage.sh_commands:
                if self.fail_command and self.fail_command in cmd:
                    self.executed_commands.append(cmd)
                    self.failed_stage = stage.name
                    stage_ok = False
                    success = False
                    break
                self.executed_commands.append(cmd)

            # -- record post actions --
            for action in stage.post_actions:
                self.post_actions_recorded.append(action)

            if not stage_ok:
                break  # Pipeline halts on first failure

        # -- global post { always } --
        if self.pipeline.global_post_always:
            self.cleanup_ran = True

        return success


class TestJenkinsSimulatedRun:
    """Simulate a real Jenkins pipeline execution by parsing the generated
    Jenkinsfile from ``PipelineTemplateGenerator`` and replaying its stages.
    """

    @pytest.fixture()
    def jenkinsfile(self) -> str:
        gen = PipelineTemplateGenerator()
        config = PipelineConfig(
            provider=PipelineProvider.JENKINS,
            complexity=PipelineComplexity.STANDARD,
            environments=["dev", "staging", "prod"],
            enable_marketplace_publishing=True,
        )
        files = gen.generate_pipeline(config)
        return files["Jenkinsfile"]

    @pytest.fixture()
    def pipeline(self, jenkinsfile) -> _JenkinsPipeline:
        return _JenkinsPipeline(jenkinsfile)

    # -- happy path --------------------------------------------------------

    def test_all_stages_execute_on_main(self, pipeline):
        runner = _SimulatedJenkinsRunner(pipeline, branch="main")
        assert runner.run() is True
        assert runner.failed_stage is None
        assert len(runner.executed_commands) > 0

    def test_fluid_commands_invoked_in_order(self, pipeline):
        runner = _SimulatedJenkinsRunner(pipeline, branch="main")
        runner.run()
        cmds = " ".join(runner.executed_commands)
        # Core fluid commands should appear
        assert "fluid" in cmds or "fluid_build" in cmds
        # Validate should come before plan/apply
        cmd_list = runner.executed_commands
        validate_idx = next((i for i, c in enumerate(cmd_list) if "validate" in c.lower()), None)
        plan_idx = next((i for i, c in enumerate(cmd_list) if "plan" in c.lower()), None)
        assert validate_idx is not None
        assert plan_idx is not None
        assert validate_idx < plan_idx

    def test_setup_stage_runs_first(self, pipeline):
        runner = _SimulatedJenkinsRunner(pipeline, branch="main")
        runner.run()
        # First executed command should be from Setup (pip install)
        assert (
            "pip install" in runner.executed_commands[0]
            or "requirements" in runner.executed_commands[0]
        )

    def test_deploy_stages_present(self, pipeline):
        runner = _SimulatedJenkinsRunner(pipeline, branch="main")
        runner.run()
        stage_names = [s.name for s in pipeline.stages]
        assert any("DEV" in n for n in stage_names)
        assert any("STAGING" in n for n in stage_names)
        assert any("PROD" in n for n in stage_names)

    def test_publish_stage_present(self, pipeline):
        stage_names = [s.name for s in pipeline.stages]
        assert any("Publish" in n for n in stage_names)

    # -- stage failure halts pipeline --------------------------------------

    def test_failure_halts_pipeline(self, pipeline):
        runner = _SimulatedJenkinsRunner(pipeline, branch="main", fail_command="validate")
        assert runner.run() is False
        assert runner.failed_stage is not None
        # Commands after the failing one should NOT have been executed
        cmds_after_failure = runner.executed_commands[
            runner.executed_commands.index(
                next(c for c in runner.executed_commands if "validate" in c)
            )
            + 1 :
        ]
        # No apply or plan commands after validate failure
        assert not any("apply" in c for c in cmds_after_failure)

    # -- branch guards -----------------------------------------------------

    def test_non_main_branch_skips_guarded_stages(self, pipeline):
        runner = _SimulatedJenkinsRunner(pipeline, branch="feature/foo")
        runner.run()
        # Stages with when { branch 'main' } should be skipped
        for stage in pipeline.stages:
            if stage.when_branch == "main":
                assert stage.name in runner.skipped_stages

    def test_main_branch_does_not_skip_guarded_stages(self, pipeline):
        runner = _SimulatedJenkinsRunner(pipeline, branch="main")
        runner.run()
        # No stage with when { branch 'main' } should be skipped
        for stage in pipeline.stages:
            if stage.when_branch == "main" and not stage.has_input:
                assert stage.name not in runner.skipped_stages

    # -- approval gate -----------------------------------------------------

    def test_approval_denied_skips_stage(self, pipeline):
        runner = _SimulatedJenkinsRunner(pipeline, branch="main", approve_inputs=False)
        runner.run()
        # Stages with input gates should be skipped
        for stage in pipeline.stages:
            if stage.has_input:
                assert stage.name in runner.skipped_stages

    def test_approval_granted_runs_stage(self, pipeline):
        runner = _SimulatedJenkinsRunner(pipeline, branch="main", approve_inputs=True)
        runner.run()
        input_stages = [s for s in pipeline.stages if s.has_input]
        for stage in input_stages:
            assert stage.name not in runner.skipped_stages

    # -- post actions & cleanup --------------------------------------------

    def test_archive_artifacts_recorded(self, pipeline):
        runner = _SimulatedJenkinsRunner(pipeline, branch="main")
        runner.run()
        all_post = runner.post_actions_recorded
        assert any("archiveArtifacts" in a for a in all_post)

    def test_junit_results_recorded(self, pipeline):
        runner = _SimulatedJenkinsRunner(pipeline, branch="main")
        runner.run()
        all_post = runner.post_actions_recorded
        assert any("junit" in a for a in all_post)

    def test_cleanup_always_runs_on_success(self, pipeline):
        runner = _SimulatedJenkinsRunner(pipeline, branch="main")
        runner.run()
        assert runner.cleanup_ran is True

    def test_cleanup_always_runs_on_failure(self, pipeline):
        runner = _SimulatedJenkinsRunner(pipeline, branch="main", fail_command="validate")
        runner.run()
        assert runner.cleanup_ran is True

    # -- marketplace publishing --------------------------------------------

    def test_marketplace_publish_command_present(self, pipeline):
        all_cmds = []
        for stage in pipeline.stages:
            all_cmds.extend(stage.sh_commands)
        assert any("marketplace" in c.lower() for c in all_cmds)

    def test_marketplace_disabled_no_publish(self):
        gen = PipelineTemplateGenerator()
        config = PipelineConfig(
            provider=PipelineProvider.JENKINS,
            complexity=PipelineComplexity.STANDARD,
            enable_marketplace_publishing=False,
        )
        content = gen.generate_pipeline(config)["Jenkinsfile"]
        pipeline = _JenkinsPipeline(content)
        all_cmds = []
        for stage in pipeline.stages:
            all_cmds.extend(stage.sh_commands)
        assert not any("marketplace" in c.lower() for c in all_cmds)


# ---------------------------------------------------------------------------
# 5. Simulated run of the STATIC Jenkins template (generate ci)
# ---------------------------------------------------------------------------


class TestStaticJenkinsSimulatedRun:
    """Simulate a run of the simpler static JENKINS template from
    ``scaffold_ci.py`` (the one produced by ``fluid generate ci --system jenkins``).
    """

    @pytest.fixture()
    def pipeline(self) -> _JenkinsPipeline:
        return _JenkinsPipeline(JENKINS)

    def test_stages_parsed(self, pipeline):
        names = [s.name for s in pipeline.stages]
        assert "Validate" in names
        assert "Plan" in names
        assert "Test" in names
        assert "Apply" in names

    def test_happy_path_on_main(self, pipeline):
        runner = _SimulatedJenkinsRunner(pipeline, branch="main")
        assert runner.run() is True
        assert len(runner.executed_commands) >= 4  # at least 4 fluid commands

    def test_apply_skipped_on_feature_branch(self, pipeline):
        runner = _SimulatedJenkinsRunner(pipeline, branch="feature/x")
        runner.run()
        # Apply stage has when { branch 'main' }
        assert "Apply" in runner.skipped_stages

    def test_apply_requires_approval(self, pipeline):
        runner = _SimulatedJenkinsRunner(pipeline, branch="main", approve_inputs=False)
        runner.run()
        assert "Apply" in runner.skipped_stages

    def test_cleanup_runs(self, pipeline):
        runner = _SimulatedJenkinsRunner(pipeline, branch="main")
        runner.run()
        assert runner.cleanup_ran is True

    def test_validate_failure_halts_pipeline(self, pipeline):
        runner = _SimulatedJenkinsRunner(pipeline, branch="main", fail_command="validate")
        assert runner.run() is False
        assert not any("apply" in c for c in runner.executed_commands)
