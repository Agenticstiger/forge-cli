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


def _make_args(
    system: str = "jenkins",
    out: Optional[str] = None,
    install_mode: str = "pypi",
    default_publish_target: Optional[str] = None,
    verify_strict_default: Optional[bool] = None,
    publish_stage_default: Optional[bool] = None,
    publish_include_env: Optional[bool] = None,
) -> argparse.Namespace:
    return argparse.Namespace(
        system=system,
        out=out,
        contract="contract.fluid.yaml",
        install_mode=install_mode,
        default_publish_target=default_publish_target,
        verify_strict_default=verify_strict_default,
        publish_stage_default=publish_stage_default,
        publish_include_env=publish_include_env,
    )


# ``generate_ci.run`` now delegates to
# :class:`PipelineTemplateGenerator`, which emits canonical file paths
# and a richer stage set than the legacy ``scaffold_ci`` constants.
# The static constants (``GITHUB`` / ``GITLAB`` / ``JENKINS``) remain
# the output shape for the legacy ``fluid scaffold-ci`` command and
# are still covered by their own test classes below.
_GENERATE_CI_EXPECTATIONS = (
    (
        "github",
        ".github/workflows/fluid-standard.yml",
        ("name:", "jobs:", "validate:", "runs-on:"),
    ),
    (
        "gitlab",
        ".gitlab-ci.yml",
        ("stages:", "validate:", "image:", "fluid validate"),
    ),
    (
        "jenkins",
        "Jenkinsfile",
        # 11-stage parameterized template — names are prefixed with stage
        # number ("2 - validate", "6 - plan") per the perfect-pipeline design.
        ("pipeline {", "stage('2 - validate')", "stage('6 - plan')", "parameters {", "fluid"),
    ),
    (
        "azure",
        "azure-pipelines.yml",
        ("stages:", "jobs:", "fluid"),
    ),
    (
        "bitbucket",
        "bitbucket-pipelines.yml",
        ("pipelines:", "step:", "fluid"),
    ),
    (
        "circleci",
        ".circleci/config.yml",
        ("version:", "jobs:", "workflows:", "fluid"),
    ),
    (
        "tekton",
        "tekton/pipeline.yaml",
        ("apiVersion:", "kind: Pipeline", "fluid"),
    ),
)


# Back-compat alias for the scaffold-ci legacy-constant tests below.
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

    def test_content_has_expected_structure(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        generate_ci_run(_make_args(), _logger)
        written = (tmp_path / "Jenkinsfile").read_text()
        # Post-11-stage-rewrite, the Jenkins template emits a
        # parameterized declarative pipeline with every stage named
        # ``<n> -<stage-name>``. Assert structural markers rather than
        # byte equality so prose tweaks don't break the suite.
        assert "pipeline {" in written
        assert "stage('2 - validate')" in written
        assert "fluid" in written
        assert "parameters {" in written
        # Default install mode is pypi — stable PyPI.
        assert "stage('0 — Bootstrap FLUID [pypi]')" in written

    def test_install_mode_dev_source_flag(self, tmp_path, monkeypatch):
        """``--install-mode dev-source`` emits the bind-mount install
        branch + fail-loud check. Default path (pypi) still covered by
        test_content_has_expected_structure above."""
        monkeypatch.chdir(tmp_path)
        rc = generate_ci_run(_make_args(install_mode="dev-source"), _logger)
        assert rc == 0
        written = (tmp_path / "Jenkinsfile").read_text()
        assert "stage('0 — Bootstrap FLUID [dev-source]')" in written
        assert "/forge-cli-src" in written
        # Fail-loud ERROR message must be present in the generated Setup.
        assert "install-mode=dev-source but /forge-cli-src" in written
        # pypi's Jenkins parameters must NOT leak into dev-source mode.
        assert "name: 'FLUID_PIP_INDEX_URL'" not in written

    def test_returns_zero_on_success(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert generate_ci_run(_make_args(), _logger) == 0

    def test_default_publish_target_omitted_emits_bare_form(self, tmp_path, monkeypatch):
        """Without ``--default-publish-target``, Stage 10's shell uses
        the bare ``${PUBLISH_TARGETS}`` form — backwards-compatible with
        every Jenkinsfile generated before the flag existed.

        We check the actual shell-loop line rather than a bare substring
        because the surrounding groovy comment block legitimately
        documents the opt-in ``${PUBLISH_TARGETS:-X}`` example."""
        monkeypatch.chdir(tmp_path)
        assert generate_ci_run(_make_args(), _logger) == 0
        content = (tmp_path / "Jenkinsfile").read_text()
        assert "for t in ${PUBLISH_TARGETS}" in content
        assert "for t in ${PUBLISH_TARGETS:-" not in content

    def test_default_publish_target_opt_in_emits_shell_fallback(self, tmp_path, monkeypatch):
        """With ``--default-publish-target datamesh-manager``, Stage 10's
        shell emits ``${PUBLISH_TARGETS:-datamesh-manager}`` so the
        first Pipeline-from-SCM build Jenkins auto-triggers (before
        the parameters block is exported as env vars) still publishes
        to the intended catalog."""
        monkeypatch.chdir(tmp_path)
        assert generate_ci_run(_make_args(default_publish_target="datamesh-manager"), _logger) == 0
        content = (tmp_path / "Jenkinsfile").read_text()
        assert "for t in ${PUBLISH_TARGETS:-datamesh-manager}" in content

    def test_default_publish_target_accepts_arbitrary_catalog(self, tmp_path, monkeypatch):
        """The flag isn't hard-coded to datamesh-manager — operators on
        Horizon / DataHub / Collibra pick their own primary catalog."""
        monkeypatch.chdir(tmp_path)
        assert generate_ci_run(_make_args(default_publish_target="horizon"), _logger) == 0
        content = (tmp_path / "Jenkinsfile").read_text()
        assert "for t in ${PUBLISH_TARGETS:-horizon}" in content

    def test_default_publish_target_empty_string_treated_as_unset(self, tmp_path, monkeypatch):
        """An empty or whitespace-only value is treated the same as
        omitting the flag — bare ``${PUBLISH_TARGETS}`` form, no
        surprise ``:-`` expansion with nothing on the right side.

        Again we check the actual shell-loop line rather than a bare
        substring because the template's doc-comment legitimately
        mentions ``${PUBLISH_TARGETS:-X}`` as an example."""
        monkeypatch.chdir(tmp_path)
        assert generate_ci_run(_make_args(default_publish_target="   "), _logger) == 0
        content = (tmp_path / "Jenkinsfile").read_text()
        assert "for t in ${PUBLISH_TARGETS}" in content
        assert "for t in ${PUBLISH_TARGETS:-" not in content

    def test_verify_strict_default_override_flips_parameter_default(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert generate_ci_run(_make_args(verify_strict_default=False), _logger) == 0
        content = (tmp_path / "Jenkinsfile").read_text()
        assert "name: 'VERIFY_STRICT',      defaultValue: false" in content

    def test_publish_stage_default_override_flips_parameter_default(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert generate_ci_run(_make_args(publish_stage_default=True), _logger) == 0
        content = (tmp_path / "Jenkinsfile").read_text()
        assert "name: 'RUN_STAGE_10_PUBLISH', defaultValue: true" in content

    def test_publish_include_env_override_omits_stage_10_env_flag(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert generate_ci_run(_make_args(publish_include_env=False), _logger) == 0
        content = (tmp_path / "Jenkinsfile").read_text()
        assert 'fluid publish "${CONTRACT:-contract.fluid.yaml}" ${TARGET_FLAGS}' in content
        assert 'fluid publish "${CONTRACT:-contract.fluid.yaml}" ${TARGET_FLAGS} \\' not in content
        stage_10 = content[content.index("stage('10 - publish')") :]
        stage_10 = stage_10[: stage_10.index("stage('11 - schedule sync')")]
        assert '--env "${FLUID_ENV:-dev}"' not in stage_10


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
        ("system", "default_path", "tokens"),
        _GENERATE_CI_EXPECTATIONS,
    )
    def test_generate_ci_writes_default_output(
        self, tmp_path, monkeypatch, system, default_path, tokens
    ):
        monkeypatch.chdir(tmp_path)
        rc = generate_ci_run(_make_args(system=system), _logger)
        assert rc == 0
        written = tmp_path / default_path
        assert written.exists(), f"missing {default_path} for system={system}"
        content = written.read_text()
        for token in tokens:
            assert token in content, (
                f"expected token {token!r} missing from generated {default_path}"
            )

    @pytest.mark.parametrize(
        ("system", "_default_path", "tokens"),
        [
            case
            for case in _GENERATE_CI_EXPECTATIONS
            # Skip multi-file systems (tekton); --out is a no-op for them.
            if case[0] != "tekton"
        ],
    )
    def test_generate_ci_supports_custom_output(
        self, tmp_path, monkeypatch, system, _default_path, tokens
    ):
        monkeypatch.chdir(tmp_path)
        suffix = (
            "Jenkinsfile"
            if system == "jenkins"
            else f"{system}.yml"
            if system != "circleci"
            else "circleci-config.yml"
        )
        custom = tmp_path / "generated" / suffix
        rc = generate_ci_run(_make_args(system=system, out=str(custom)), _logger)
        assert rc == 0
        assert custom.exists()
        content = custom.read_text()
        for token in tokens:
            assert token in content, (
                f"expected token {token!r} missing from custom output for {system}"
            )


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

    def test_output_contains_all_11_stages(self, generator):
        """11-stage template must emit all 11 named stages in order.
        Setup stage's name includes the install-mode marker —
        ``stage('0 — Bootstrap FLUID [pypi]')`` by default, or
        ``stage('0 — Bootstrap FLUID [dev-source]')`` when generated
        with ``--install-mode dev-source``."""
        content = self._generate(generator)["Jenkinsfile"]
        expected = [
            "stage('0 — Bootstrap FLUID [pypi]')",
            "stage('1 - bundle')",
            "stage('2 - validate')",
            "stage('3 - generate artifacts')",
            "stage('4 - validate artifacts')",
            "stage('5 - diff (drift gate)')",
            "stage('6 - plan')",
            "stage('7 - apply')",
            "stage('8 - policy apply')",
            "stage('9 - verify')",
            "stage('10 - publish')",
            "stage('11 - schedule sync')",
        ]
        for s in expected:
            assert s in content, f"missing stage: {s}"
        # Stage order must match the HTML design — validate before plan,
        # plan before apply, apply before verify, verify before publish.
        idx = {s: content.index(s) for s in expected}
        ordered = sorted(expected, key=lambda s: idx[s])
        assert ordered == expected, f"stage order wrong: {ordered}"

    def test_output_contains_apply_mode_choice(self, generator):
        """Stage 7 apply exposes --mode as a Jenkins choice parameter
        with all 6 canonical modes from the HTML design."""
        content = self._generate(generator)["Jenkinsfile"]
        assert "name: 'APPLY_MODE'" in content
        for mode in [
            "dry-run",
            "amend",
            "create-only",
            "amend-and-build",
            "replace",
            "replace-and-build",
        ]:
            assert f"'{mode}'" in content, f"APPLY_MODE choice missing: {mode}"

    def test_output_contains_publish_targets_parameter(self, generator):
        """Stage 10 publish uses --target (repeatable) — exposed via a
        space-separated PUBLISH_TARGETS string parameter."""
        content = self._generate(generator)["Jenkinsfile"]
        assert "name: 'PUBLISH_TARGETS'" in content
        assert "--target" in content

    def test_output_contains_scheduler_choice(self, generator):
        """Stage 11 schedule-sync exposes --scheduler as a choice with
        all supported scheduler targets."""
        content = self._generate(generator)["Jenkinsfile"]
        assert "name: 'SCHEDULER'" in content
        for sch in ["airflow", "mwaa", "composer", "astronomer", "prefect", "dagster"]:
            assert f"'{sch}'" in content, f"SCHEDULER choice missing: {sch}"

    def test_output_contains_per_stage_toggles(self, generator):
        """Every stage (1-11) must have a RUN_STAGE_N_* boolean toggle."""
        content = self._generate(generator)["Jenkinsfile"]
        for n in range(1, 12):
            assert f"RUN_STAGE_{n}_" in content, (
                f"RUN_STAGE_{n}_* toggle missing — stage {n} not parameterized"
            )

    def test_output_contains_allow_data_loss_gate(self, generator):
        """Stage 7 destructive-mode gate must be exposed as a param."""
        content = self._generate(generator)["Jenkinsfile"]
        assert "name: 'ALLOW_DATA_LOSS'" in content
        assert "--allow-data-loss" in content

    def test_output_contains_no_verify_digest_escape_hatch(self, generator):
        """Stage 7 emergency plan-binding waiver flag."""
        content = self._generate(generator)["Jenkinsfile"]
        assert "name: 'NO_VERIFY_DIGEST'" in content
        assert "--no-verify-digest" in content

    def test_output_contains_diff_drift_gate_toggle(self, generator):
        """Stage 5 --exit-on-drift behavior must be toggleable."""
        content = self._generate(generator)["Jenkinsfile"]
        assert "name: 'DIFF_EXIT_ON_DRIFT'" in content
        assert "--exit-on-drift" in content

    def test_output_contains_policy_apply_mode(self, generator):
        """Stage 8 must expose check | enforce as a choice."""
        content = self._generate(generator)["Jenkinsfile"]
        assert "name: 'POLICY_APPLY_MODE'" in content
        assert "'enforce'" in content
        assert "'check'" in content

    def test_stage1_bundle_format_is_tgz_and_not_parameterized(self, generator):
        """Stage 1 hardcodes ``--format tgz``.

        The pipeline's downstream stages (4: validate artifacts, 6: plan
        bundleDigest, 7: apply plan-binding verification) all require the
        tgz MANIFEST.json. A ``yaml`` / ``json`` bundle would break every
        stage after 1, so making format a pipeline parameter is a footgun —
        the previous template exposed ``BUNDLE_FORMAT`` with an invalid
        ``'yaml-single-file'`` choice that ``fluid bundle`` would reject
        outright (valid choices are ``{yaml, json, tgz}``). Keep the
        format pinned to tgz here; operators who need yaml/json bundles
        run ``fluid bundle`` out-of-band.
        """
        content = self._generate(generator)["Jenkinsfile"]
        # The BUNDLE_FORMAT choice parameter was removed — the pipeline now
        # hardcodes tgz.
        assert "name: 'BUNDLE_FORMAT'" not in content, (
            "BUNDLE_FORMAT should no longer be a pipeline parameter; Stages 4/6/7 require tgz."
        )
        # The obsolete, invalid choice must not reappear.
        assert "'yaml-single-file'" not in content, (
            "'yaml-single-file' is not a valid `fluid bundle --format` choice "
            "(valid: {yaml, json, tgz}). Template regressed."
        )
        # And the bundle step itself passes an explicit --format tgz.
        assert "--format tgz --out runtime/bundle.tgz" in content

    @pytest.mark.parametrize("complexity", ["basic", "standard", "advanced", "enterprise"])
    def test_all_complexity_levels(self, generator, complexity):
        result = self._generate(generator, complexity=complexity)
        assert "Jenkinsfile" in result
        assert len(result["Jenkinsfile"]) > 0

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

            # Extract sh commands — handle all 4 Groovy string forms in
            # ONE regex with alternation so matches stay in file order
            # (order matters for test_setup_stage_runs_first):
            #   sh '''...'''  (triple-single, multi-line, no interpolation)
            #   sh """..."""  (triple-double, multi-line, with interpolation)
            #   sh '...'      (single-single, single-line)
            #   sh "..."      (single-double, single-line)
            # The 11-stage template uses triple-single predominantly so env
            # vars ${VAR} flow at shell runtime rather than Groovy-eval time.
            sh_pattern = re.compile(
                r"""sh\s+(?:'''(?P<tsq>.+?)'''|\"\"\"(?P<tdq>.+?)\"\"\"|'(?P<ssq>[^']+)'|"(?P<sdq>[^"]+)")""",
                re.DOTALL,
            )
            for m in sh_pattern.finditer(body):
                stage.sh_commands.append(
                    m.group("tsq") or m.group("tdq") or m.group("ssq") or m.group("sdq")
                )

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

    def test_11_stages_present(self, pipeline):
        """Replaces the old per-env deploy stages. The 11-stage design
        has a single parameterized ``7 - apply`` stage controlled by
        FLUID_ENV + APPLY_MODE params, not N environment-named stages.
        Every structural stage (1-11) must be present."""
        stage_names = [s.name for s in pipeline.stages]
        for marker in [
            "1 - bundle",
            "2 - validate",
            "3 - generate artifacts",
            "4 - validate artifacts",
            "5 - diff",
            "6 - plan",
            "7 - apply",
            "8 - policy apply",
            "9 - verify",
            "10 - publish",
            "11 - schedule sync",
        ]:
            assert any(marker in n for n in stage_names), (
                f"stage containing {marker!r} missing from {stage_names}"
            )

    def test_publish_stage_present(self, pipeline):
        """Stage 10 is named ``10 - publish`` in the parameterized
        template — matches the HTML design's naming scheme."""
        stage_names = [s.name for s in pipeline.stages]
        assert any("publish" in n.lower() for n in stage_names)

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

    # Junit post-action dropped — the 11-stage design treats ``fluid test``
    # as a separate tool, not a structural pipeline stage. Contract tests
    # run via stage 9 verify which emits a JSON report (not junit XML).

    def test_cleanup_always_runs_on_success(self, pipeline):
        runner = _SimulatedJenkinsRunner(pipeline, branch="main")
        runner.run()
        assert runner.cleanup_ran is True

    def test_cleanup_always_runs_on_failure(self, pipeline):
        runner = _SimulatedJenkinsRunner(pipeline, branch="main", fail_command="validate")
        runner.run()
        assert runner.cleanup_ran is True

    # Marketplace is not a structural stage in the 11-stage design —
    # it's a --target value of stage 10 publish (fluid publish <contract>
    # --target marketplace). The previous test_marketplace_publish_command_present
    # was dropped along with the standalone marketplace stage.

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


# ---------------------------------------------------------------------------
# 6. Cross-system regression: generated pipelines must not emit bare $CONTRACT
# ---------------------------------------------------------------------------


class TestGeneratedContractEnvAcrossSystems:
    """Every generated CI pipeline calls ``fluid validate / plan / apply /
    contract-tests / publish`` with a contract-path argument. Before the
    fix, the argument was a bare ``$CONTRACT`` and no CI template's env
    block injected the var — so Build Now failed on the first shell
    step. The fix uses ``${CONTRACT:-contract.fluid.yaml}`` in
    ``_get_fluid_commands()``. This test locks that shape in across all
    seven supported providers so a regression in any one template is
    caught.
    """

    _DEFAULT = "${CONTRACT:-contract.fluid.yaml}"
    _PRIMARY_FILE = {
        PipelineProvider.GITHUB_ACTIONS: ".github/workflows/fluid-standard.yml",
        PipelineProvider.GITLAB_CI: ".gitlab-ci.yml",
        PipelineProvider.AZURE_DEVOPS: "azure-pipelines.yml",
        PipelineProvider.JENKINS: "Jenkinsfile",
        PipelineProvider.BITBUCKET: "bitbucket-pipelines.yml",
        PipelineProvider.CIRCLE_CI: ".circleci/config.yml",
        PipelineProvider.TEKTON: "tekton/tasks.yaml",
    }

    @pytest.mark.parametrize("provider", list(_PRIMARY_FILE.keys()))
    def test_primary_file_uses_default_expansion_not_bare_var(self, provider):
        cfg = PipelineConfig(
            provider=provider,
            complexity=PipelineComplexity.STANDARD,
            environments=["dev", "prod"],
        )
        files = PipelineTemplateGenerator().generate_pipeline(cfg)
        primary = files[self._PRIMARY_FILE[provider]]

        # Must contain the default-expansion form at least once.
        assert self._DEFAULT in primary, (
            f"{provider.value}: expected {self._DEFAULT!r} in generated "
            f"{self._PRIMARY_FILE[provider]}"
        )

        # Must not contain any bare $CONTRACT references (i.e. any
        # occurrence that is not part of the default-expansion form).
        without_default = primary.replace(self._DEFAULT, "")
        assert "$CONTRACT" not in without_default, (
            f"{provider.value}: generated {self._PRIMARY_FILE[provider]} "
            f"contains a bare $CONTRACT reference — regression of the "
            f"A1 Jenkins Build-Now gap"
        )


# ---------------------------------------------------------------------------
# Reference-only contract detection + git-prefix workdir resolution
# ---------------------------------------------------------------------------


class TestContractIsReferenceOnly:
    """`_contract_is_reference_only` gates whether `fluid generate ci` emits the
    Generate Artifacts stage. A contract with any ``builds[].pattern`` in
    ``{hybrid-reference, reference, external-reference}`` is owned externally
    (team's own dbt project / Airflow DAG), so asking fluid to regenerate those
    artifacts surfaces only spurious failures. Edge cases are the point.
    """

    def _write(self, tmp_path: Path, yaml_content: str) -> Path:
        p = tmp_path / "contract.fluid.yaml"
        p.write_text(yaml_content)
        return p

    def test_missing_file_returns_false(self, tmp_path):
        from fluid_build.cli.generate_ci import _contract_is_reference_only

        missing = tmp_path / "nope.yaml"
        assert _contract_is_reference_only(str(missing)) is False

    def test_broken_yaml_returns_false(self, tmp_path):
        from fluid_build.cli.generate_ci import _contract_is_reference_only

        p = self._write(tmp_path, "not: valid: yaml: [\n")
        # Defensive: parse failure must not crash; returns False so the
        # generate-artifacts stage stays on by default.
        assert _contract_is_reference_only(str(p)) is False

    def test_no_builds_key_returns_false(self, tmp_path):
        from fluid_build.cli.generate_ci import _contract_is_reference_only

        p = self._write(tmp_path, "dataProduct:\n  name: foo\n")
        assert _contract_is_reference_only(str(p)) is False

    def test_builds_is_dict_not_list_returns_false(self, tmp_path):
        from fluid_build.cli.generate_ci import _contract_is_reference_only

        p = self._write(tmp_path, "builds:\n  not_a_list: true\n")
        assert _contract_is_reference_only(str(p)) is False

    def test_build_without_pattern_returns_false(self, tmp_path):
        from fluid_build.cli.generate_ci import _contract_is_reference_only

        p = self._write(tmp_path, "builds:\n  - id: foo\n    engine: python\n")
        assert _contract_is_reference_only(str(p)) is False

    @pytest.mark.parametrize("pattern", ["hybrid-reference", "reference", "external-reference"])
    def test_recognised_reference_patterns_return_true(self, tmp_path, pattern):
        from fluid_build.cli.generate_ci import _contract_is_reference_only

        p = self._write(tmp_path, f"builds:\n  - id: foo\n    pattern: {pattern}\n")
        assert _contract_is_reference_only(str(p)) is True

    def test_unrecognised_pattern_returns_false(self, tmp_path):
        from fluid_build.cli.generate_ci import _contract_is_reference_only

        p = self._write(tmp_path, "builds:\n  - id: foo\n    pattern: something-else\n")
        assert _contract_is_reference_only(str(p)) is False

    def test_any_build_with_reference_pattern_wins(self, tmp_path):
        """Mixed contracts (one normal build, one reference build) must be
        treated as reference-only — the reference build's external ownership
        still blocks fluid from regenerating anything for that build."""
        from fluid_build.cli.generate_ci import _contract_is_reference_only

        p = self._write(
            tmp_path,
            "builds:\n"
            "  - id: normal\n"
            "    pattern: declarative\n"
            "  - id: ref\n"
            "    pattern: hybrid-reference\n",
        )
        assert _contract_is_reference_only(str(p)) is True

    def test_non_dict_build_entry_skipped(self, tmp_path):
        from fluid_build.cli.generate_ci import _contract_is_reference_only

        p = self._write(
            tmp_path,
            "builds:\n  - just a string\n  - id: foo\n    pattern: reference\n",
        )
        # Must not raise on the bare-string entry; must still detect the
        # dict entry's reference pattern.
        assert _contract_is_reference_only(str(p)) is True


class TestGitPrefix:
    """`_git_prefix` returns the current directory's path relative to the git
    repo root — used to generate ``cd "<workdir>" && ...`` wrappers for
    Jenkins when ``fluid generate ci`` runs in a subfolder of the checkout.
    """

    @patch("fluid_build.cli.generate_ci.subprocess.run")
    def test_root_of_repo_returns_none(self, mock_run):
        """git rev-parse --show-prefix returns '' at the repo root;
        _git_prefix must normalise that to None so no cd wrapper is injected."""
        from fluid_build.cli.generate_ci import _git_prefix

        mock_run.return_value = MagicMock(returncode=0, stdout="\n")
        assert _git_prefix() is None

    @patch("fluid_build.cli.generate_ci.subprocess.run")
    def test_subfolder_returns_stripped_path(self, mock_run):
        from fluid_build.cli.generate_ci import _git_prefix

        mock_run.return_value = MagicMock(returncode=0, stdout="examples/demo/\n")
        assert _git_prefix() == "examples/demo"

    @patch("fluid_build.cli.generate_ci.subprocess.run")
    def test_non_zero_returncode_returns_none(self, mock_run):
        """`git rev-parse` fails outside a repo → exit 128 → return None."""
        from fluid_build.cli.generate_ci import _git_prefix

        mock_run.return_value = MagicMock(returncode=128, stdout="")
        assert _git_prefix() is None

    @patch("fluid_build.cli.generate_ci.subprocess.run", side_effect=FileNotFoundError)
    def test_git_not_installed_returns_none(self, _mock_run):
        from fluid_build.cli.generate_ci import _git_prefix

        assert _git_prefix() is None

    @patch("fluid_build.cli.generate_ci.subprocess.run", side_effect=OSError("boom"))
    def test_oserror_returns_none(self, _mock_run):
        from fluid_build.cli.generate_ci import _git_prefix

        assert _git_prefix() is None


class TestNoGenerateArtifactsFlag:
    """`--no-generate-artifacts` on `fluid generate ci` must propagate to
    ``PipelineConfig.generates_artifacts=False`` so the Generate Artifacts
    stage is omitted from the emitted pipeline. Auto-detection (via
    ``_contract_is_reference_only``) must achieve the same result without
    the flag.
    """

    # CRITICAL: every test here must `monkeypatch.chdir(tmp_path)` because
    # `generate_ci.run()` writes files via `atomic_write(rel_path, ...)`
    # regardless of whether the PipelineTemplateGenerator is mocked. Without
    # chdir, the test clobbers the repo's real Jenkinsfile at the repo root.

    @patch("fluid_build.cli.generate_ci._contract_is_reference_only", return_value=False)
    @patch("fluid_build.cli.generate_ci._git_prefix", return_value=None)
    @patch("fluid_build.forge.core.pipeline_templates.PipelineTemplateGenerator")
    def test_flag_forces_generates_artifacts_false(
        self, mock_gen, _mock_prefix, _mock_ref_only, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        from fluid_build.cli.generate_ci import run as generate_ci_run

        mock_gen.return_value.generate_pipeline.return_value = {"Jenkinsfile": "pipeline {}\n"}

        args = argparse.Namespace(
            system="jenkins",
            out=None,
            contract="contract.fluid.yaml",
            no_generate_artifacts=True,
            provider=None,
            complexity="basic",
        )

        generate_ci_run(args, _logger)
        cfg = mock_gen.return_value.generate_pipeline.call_args[0][0]
        assert cfg.generates_artifacts is False

    @patch("fluid_build.cli.generate_ci._contract_is_reference_only", return_value=True)
    @patch("fluid_build.cli.generate_ci._git_prefix", return_value=None)
    @patch("fluid_build.forge.core.pipeline_templates.PipelineTemplateGenerator")
    def test_reference_only_contract_auto_disables_generate(
        self, mock_gen, _mock_prefix, _mock_ref_only, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        from fluid_build.cli.generate_ci import run as generate_ci_run

        mock_gen.return_value.generate_pipeline.return_value = {"Jenkinsfile": "pipeline {}\n"}

        args = argparse.Namespace(
            system="jenkins",
            out=None,
            contract="contract.fluid.yaml",
            no_generate_artifacts=False,  # flag NOT set
            provider=None,
            complexity="basic",
        )

        generate_ci_run(args, _logger)
        cfg = mock_gen.return_value.generate_pipeline.call_args[0][0]
        # Auto-detection wins even without the flag.
        assert cfg.generates_artifacts is False

    @patch("fluid_build.cli.generate_ci._contract_is_reference_only", return_value=False)
    @patch("fluid_build.cli.generate_ci._git_prefix", return_value="examples/demo")
    @patch("fluid_build.forge.core.pipeline_templates.PipelineTemplateGenerator")
    def test_git_prefix_propagates_to_workdir(
        self, mock_gen, _mock_prefix, _mock_ref_only, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        from fluid_build.cli.generate_ci import run as generate_ci_run

        mock_gen.return_value.generate_pipeline.return_value = {"Jenkinsfile": "pipeline {}\n"}

        args = argparse.Namespace(
            system="jenkins",
            out=None,
            contract="contract.fluid.yaml",
            no_generate_artifacts=False,
            provider=None,
            complexity="basic",
        )

        generate_ci_run(args, _logger)
        cfg = mock_gen.return_value.generate_pipeline.call_args[0][0]
        assert cfg.workdir == "examples/demo"
