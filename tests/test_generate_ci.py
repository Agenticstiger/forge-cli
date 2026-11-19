# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

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
import yaml

from fluid_build.cli._pipeline_files import _comment_prefix_for
from fluid_build.cli.generate_ci import run as generate_ci_run
from fluid_build.forge.core.pipeline_templates import (
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
    **extra: Any,
) -> argparse.Namespace:
    return argparse.Namespace(
        system=system,
        out=out,
        contract="contract.fluid.yaml",
        install_mode=install_mode,
        default_publish_target=default_publish_target,
        verify_strict_default=verify_strict_default,
        publish_stage_default=publish_stage_default,
        **extra,
    )


# ``generate_ci.run`` delegates to :class:`PipelineTemplateGenerator`,
# which emits the canonical file path for each system.
_GENERATE_CI_EXPECTATIONS = (
    (
        "github",
        ".github/workflows/fluid-pipeline.yml",
        ("name:", "jobs:", "plan:", "apply:", "runs-on:", "fluid ship"),
    ),
    (
        "gitlab",
        ".gitlab-ci.yml",
        ("stages:", "fluid-plan:", "fluid-apply:", "image:", "fluid ship"),
    ),
    (
        "jenkins",
        "Jenkinsfile",
        # One stage per pipeline stage, numbered ("2 - validate", "6 - plan"),
        # each running `fluid ship --stage N`.
        ("pipeline {", "stage('2 - validate')", "stage('6 - plan')", "parameters {", "fluid ship"),
    ),
    (
        "azure",
        "azure-pipelines.yml",
        ("stages:", "jobs:", "deployment:", "fluid ship"),
    ),
    (
        "bitbucket",
        "bitbucket-pipelines.yml",
        ("pipelines:", "step:", "trigger: manual", "fluid ship"),
    ),
    (
        "circleci",
        ".circleci/config.yml",
        ("version:", "jobs:", "workflows:", "type: approval", "fluid ship"),
    ),
    (
        "tekton",
        "tekton/pipeline.yaml",
        ("apiVersion:", "kind: Pipeline", "name: fluid-apply", "taskRef:"),
    ),
)


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

    def test_default_publish_target_omitted_keeps_datamesh_manager(self, tmp_path, monkeypatch):
        """Without ``--default-publish-target``, PUBLISH_TARGETS defaults to
        ``datamesh-manager``, and the environment {} block falls back to the same
        value when a build carries no parameters (a job's first build, or its
        first after a restart re-seeded it)."""
        monkeypatch.chdir(tmp_path)
        assert generate_ci_run(_make_args(), _logger) == 0
        content = (tmp_path / "Jenkinsfile").read_text()
        assert "params.PUBLISH_TARGETS == null ? 'datamesh-manager'" in content
        assert "name: 'PUBLISH_TARGETS', defaultValue: 'datamesh-manager'" in content

    def test_default_publish_target_opt_in_reaches_the_parameter_and_its_fallback(
        self, tmp_path, monkeypatch
    ):
        """With ``--default-publish-target fluid-command-center`` the parameter
        default and the environment fallback ship reads are both that value, so
        the first build Jenkins auto-triggers (before the parameters block is
        exported) still publishes to the intended catalog."""
        monkeypatch.chdir(tmp_path)
        assert (
            generate_ci_run(_make_args(default_publish_target="fluid-command-center"), _logger) == 0
        )
        content = (tmp_path / "Jenkinsfile").read_text()
        assert "params.PUBLISH_TARGETS == null ? 'fluid-command-center'" in content
        assert "name: 'PUBLISH_TARGETS', defaultValue: 'fluid-command-center'" in content

    def test_default_publish_target_accepts_arbitrary_catalog(self, tmp_path, monkeypatch):
        """The flag isn't hard-coded to datamesh-manager: operators on
        Horizon / DataHub / Collibra pick their own primary catalog."""
        monkeypatch.chdir(tmp_path)
        assert generate_ci_run(_make_args(default_publish_target="horizon"), _logger) == 0
        content = (tmp_path / "Jenkinsfile").read_text()
        assert "params.PUBLISH_TARGETS == null ? 'horizon'" in content

    def test_default_publish_target_empty_string_treated_as_unset(self, tmp_path, monkeypatch):
        """An empty or whitespace-only value is treated the same as omitting the flag."""
        monkeypatch.chdir(tmp_path)
        assert generate_ci_run(_make_args(default_publish_target="   "), _logger) == 0
        content = (tmp_path / "Jenkinsfile").read_text()
        assert "params.PUBLISH_TARGETS == null ? 'datamesh-manager'" in content

    def test_verify_strict_default_override_flips_parameter_default(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert generate_ci_run(_make_args(verify_strict_default=False), _logger) == 0
        content = (tmp_path / "Jenkinsfile").read_text()
        assert "name: 'VERIFY_STRICT', defaultValue: false" in content

    def test_publish_stage_default_override_flips_parameter_default(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert generate_ci_run(_make_args(publish_stage_default=True), _logger) == 0
        content = (tmp_path / "Jenkinsfile").read_text()
        assert "name: 'RUN_STAGE_10_PUBLISH', defaultValue: true" in content


class TestGenerateCIStaticSystems:
    """Cross-system coverage for ``fluid generate ci`` default paths and output."""

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
            assert (
                token in content
            ), f"expected token {token!r} missing from generated {default_path}"

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
            else f"{system}.yml" if system != "circleci" else "circleci-config.yml"
        )
        custom = tmp_path / "generated" / suffix
        rc = generate_ci_run(_make_args(system=system, out=str(custom)), _logger)
        assert rc == 0
        assert custom.exists()
        content = custom.read_text()
        for token in tokens:
            assert (
                token in content
            ), f"expected token {token!r} missing from custom output for {system}"


class TestGenerateCIOptions:
    """The options ``fluid generate ci`` takes now that every system gets one
    pipeline shape: ``--add`` for the jobs the tiers carried, ``--default-branch``
    for the apply trigger, and the removed ``--complexity``."""

    @staticmethod
    def _run(argv, monkeypatch, tmp_path, capsys):
        from fluid_build.cli import main as fluid_main

        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("COLUMNS", "200")
        rc = fluid_main(["generate", "ci", "contract.fluid.yaml", *argv])
        captured = capsys.readouterr()
        return rc, captured.out + captured.err

    def test_complexity_is_refused_and_names_add(self, tmp_path, monkeypatch, capsys):
        rc, output = self._run(
            ["--system", "github", "--complexity", "advanced"], monkeypatch, tmp_path, capsys
        )
        assert rc == 2
        assert "--complexity was removed" in output
        assert "--add" in output
        assert not (tmp_path / ".github").exists()  # nothing was written

    def test_add_is_repeatable_and_writes_the_github_add_on_files(
        self, tmp_path, monkeypatch, capsys
    ):
        rc, _ = self._run(
            ["--system", "github", "--add", "security", "--add", "sbom"],
            monkeypatch,
            tmp_path,
            capsys,
        )
        assert rc == 0
        workflows = sorted(p.name for p in (tmp_path / ".github" / "workflows").iterdir())
        assert workflows == ["fluid-pipeline.yml", "fluid-sbom.yml", "fluid-security.yml"]

    def test_an_unknown_add_on_is_refused_by_the_parser(self, tmp_path, monkeypatch, capsys):
        with pytest.raises(SystemExit):
            self._run(["--system", "github", "--add", "nope"], monkeypatch, tmp_path, capsys)

    def test_a_leftover_tier_file_is_named_in_the_output(self, tmp_path, monkeypatch, capsys):
        leftover = tmp_path / ".github" / "workflows" / "fluid-standard.yml"
        leftover.parent.mkdir(parents=True)
        leftover.write_text("name: old\n", encoding="utf-8")
        rc, output = self._run(["--system", "github"], monkeypatch, tmp_path, capsys)
        assert rc == 0
        assert ".github/workflows/fluid-standard.yml" in output
        assert leftover.exists()  # named, never deleted for the user

    def test_default_branch_reaches_the_triggers(self, tmp_path, monkeypatch, capsys):
        rc, _ = self._run(
            ["--system", "github", "--default-branch", "release"], monkeypatch, tmp_path, capsys
        )
        assert rc == 0
        text = (tmp_path / ".github" / "workflows" / "fluid-pipeline.yml").read_text()
        triggers = yaml.safe_load(text)
        triggers = triggers.get("on", triggers.get(True))
        assert triggers["push"] == {"branches": ["release"]}
        assert triggers["pull_request"] == {"branches": ["release"]}
        assert "refs/heads/release" in text and "refs/heads/main" not in text

    def test_default_branch_is_origin_head_else_main(self, tmp_path, monkeypatch, capsys):
        """Without the option: ``main`` outside a repository with a remote."""
        rc, _ = self._run(["--system", "github"], monkeypatch, tmp_path, capsys)
        assert rc == 0
        text = (tmp_path / ".github" / "workflows" / "fluid-pipeline.yml").read_text()
        assert "refs/heads/main" in text

    def test_a_branch_name_that_would_break_the_file_is_refused(
        self, tmp_path, monkeypatch, capsys
    ):
        rc, _ = self._run(
            ["--system", "github", "--default-branch", "a b"], monkeypatch, tmp_path, capsys
        )
        assert rc != 0
        assert not (tmp_path / ".github").exists()


# ---------------------------------------------------------------------------
# 3. Advanced JenkinsTemplate via PipelineTemplateGenerator
# ---------------------------------------------------------------------------


class TestJenkinsPipelineTemplateGenerator:
    """Test ``JenkinsTemplate`` from pipeline_templates.py."""

    @pytest.fixture()
    def generator(self):
        return PipelineTemplateGenerator()

    def _generate(self, generator, **kwargs):
        config = PipelineConfig(provider=PipelineProvider.JENKINS, **kwargs)
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
        # (the --target flags are ship's: pinned in tests/cli/test_ship_pipeline.py)

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
            assert (
                f"RUN_STAGE_{n}_" in content
            ), f"RUN_STAGE_{n}_* toggle missing — stage {n} not parameterized"

    def test_output_contains_allow_data_loss_gate(self, generator):
        """Stage 7 destructive-mode gate must be exposed as a param."""
        content = self._generate(generator)["Jenkinsfile"]
        assert "name: 'ALLOW_DATA_LOSS'" in content
        assert "ALLOW_DATA_LOSS = " in content  # ship turns it into --allow-data-loss

    def test_output_contains_no_verify_digest_escape_hatch(self, generator):
        """Stage 7 emergency digest-gate waiver: one ``NO_VERIFY_DIGEST`` param,
        which ship turns into BOTH narrowly-scoped flags (the plan-binding gate
        and the federation upstream-digest gate)."""
        content = self._generate(generator)["Jenkinsfile"]
        assert "name: 'NO_VERIFY_DIGEST'" in content
        assert "NO_VERIFY_DIGEST = " in content
        # Old single flag must be fully retired from generated output.
        assert "--no-verify-digest" not in content

    def test_output_contains_diff_drift_gate_toggle(self, generator):
        """Stage 5 --exit-on-drift behavior must be toggleable."""
        content = self._generate(generator)["Jenkinsfile"]
        assert "name: 'DIFF_EXIT_ON_DRIFT'" in content
        assert "DIFF_EXIT_ON_DRIFT = " in content

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
        assert (
            "name: 'BUNDLE_FORMAT'" not in content
        ), "BUNDLE_FORMAT should no longer be a pipeline parameter; Stages 4/6/7 require tgz."
        # The obsolete, invalid choice must not reappear.
        assert "'yaml-single-file'" not in content, (
            "'yaml-single-file' is not a valid `fluid bundle --format` choice "
            "(valid: {yaml, json, tgz}). Template regressed."
        )
        # (the bundle step's explicit --format tgz is ship's: tests/cli/test_ship_pipeline.py)

    @pytest.mark.parametrize(
        "add_ons",
        [(), ("security",), ("compliance",), ("sbom",), ("security", "compliance", "sbom")],
        ids=lambda a: "+".join(a) or "plain",
    )
    def test_every_add_on_combination_generates(self, generator, add_ons):
        result = self._generate(generator, add_ons=add_ons)
        assert "Jenkinsfile" in result
        assert len(result["Jenkinsfile"]) > 0

    def test_comment_prefix_is_double_slash(self):
        assert _comment_prefix_for("Jenkinsfile") == "//"


# ---------------------------------------------------------------------------
# 4. Simulated Jenkins Pipeline Run
# ---------------------------------------------------------------------------


class _JenkinsStage:
    """Parsed representation of a Jenkins stage."""

    __slots__ = ("name", "sh_commands", "post_actions")

    def __init__(self, name: str):
        self.name = name
        self.sh_commands: List[str] = []
        self.post_actions: List[str] = []  # e.g. ["archiveArtifacts ...", "junit ..."]


class _JenkinsPipeline:
    """Minimal parser for a generated Jenkinsfile — enough to extract
    stage names, ``sh`` commands and ``post`` actions so we can *simulate* a
    run. The apply gate (``Approve apply``, ``lock``, ``changeRequest()``) is
    pinned structurally in tests/forge/core/pipeline_systems/test_thin_wrappers.py.
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

            # Detect post actions
            for m in re.finditer(r"(archiveArtifacts|junit|cleanWs)\b[^}\n]*", body):
                stage.post_actions.append(m.group(0).strip())

            self.stages.append(stage)

        # Detect the global workspace cleanup: the legacy
        # ``post { always { cleanWs() } }`` (ws-cleanup plugin), or the
        # core ``deleteDir()`` in ``post { cleanup { } }``, which runs after
        # every other post condition.
        post = content[content.rfind("post {") :] if "post {" in content else ""
        if re.search(r"post\s*\{[^}]*always\s*\{[^}]*cleanWs\(\)", content, re.DOTALL):
            self.global_post_always.append("cleanWs()")
        elif re.search(r"cleanup\s*\{[^}]*deleteDir\(\)", post, re.DOTALL):
            self.global_post_always.append("deleteDir()")


class _SimulatedJenkinsRunner:
    """Simulates walking through a parsed Jenkins pipeline, executing ``sh``
    commands in file order against a mocked subprocess."""

    def __init__(self, pipeline: _JenkinsPipeline, *, fail_command: Optional[str] = None):
        self.pipeline = pipeline
        self.fail_command = fail_command  # substring match -> exit 1

        # Results
        self.executed_commands: List[str] = []
        self.failed_stage: Optional[str] = None
        self.post_actions_recorded: List[str] = []
        self.cleanup_ran: bool = False

    def run(self) -> bool:
        """Return True if pipeline passed, False if a stage failed."""
        success = True
        for stage in self.pipeline.stages:
            stage_ok = True
            for cmd in stage.sh_commands:
                self.executed_commands.append(cmd)
                if self.fail_command and self.fail_command in cmd:
                    self.failed_stage = stage.name
                    stage_ok = False
                    success = False
                    break

            for action in stage.post_actions:
                self.post_actions_recorded.append(action)

            if not stage_ok:
                break  # Pipeline halts on first failure

        # -- global post { cleanup } --
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
        config = PipelineConfig(provider=PipelineProvider.JENKINS)
        files = gen.generate_pipeline(config)
        return files["Jenkinsfile"]

    @pytest.fixture()
    def pipeline(self, jenkinsfile) -> _JenkinsPipeline:
        return _JenkinsPipeline(jenkinsfile)

    # -- happy path --------------------------------------------------------

    def test_all_stages_execute_on_main(self, pipeline):
        runner = _SimulatedJenkinsRunner(pipeline)
        assert runner.run() is True
        assert runner.failed_stage is None
        assert len(runner.executed_commands) > 0

    def test_fluid_commands_invoked_in_order(self, pipeline):
        runner = _SimulatedJenkinsRunner(pipeline)
        runner.run()
        ships = [
            int(m.group(1))
            for c in runner.executed_commands
            for m in re.finditer(r"fluid ship \"\$CONTRACT\" --stage (\d+)", c)
        ]
        # Each pipeline stage ships exactly once, in order: validate (2) before
        # plan (6) before apply (7), apply before verify (9).
        assert ships == list(range(1, 12))

    def test_setup_stage_runs_first(self, pipeline):
        runner = _SimulatedJenkinsRunner(pipeline)
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
            assert any(
                marker in n for n in stage_names
            ), f"stage containing {marker!r} missing from {stage_names}"

    def test_publish_stage_present(self, pipeline):
        """Stage 10 is named ``10 - publish`` in the parameterized
        template — matches the HTML design's naming scheme."""
        stage_names = [s.name for s in pipeline.stages]
        assert any("publish" in n.lower() for n in stage_names)

    # -- stage failure halts pipeline --------------------------------------

    def test_failure_halts_pipeline(self, pipeline):
        runner = _SimulatedJenkinsRunner(pipeline, fail_command="--stage 2")
        assert runner.run() is False
        assert runner.failed_stage is not None
        failed = next(c for c in runner.executed_commands if "--stage 2" in c)
        cmds_after_failure = runner.executed_commands[runner.executed_commands.index(failed) + 1 :]
        # Nothing after validate ran: no plan, no apply.
        assert not any("--stage" in c for c in cmds_after_failure)

    # -- post actions & cleanup --------------------------------------------

    def test_archive_artifacts_recorded(self, pipeline):
        runner = _SimulatedJenkinsRunner(pipeline)
        runner.run()
        all_post = runner.post_actions_recorded
        assert any("archiveArtifacts" in a for a in all_post)

    # Junit post-action dropped — the 11-stage design treats ``fluid test``
    # as a separate tool, not a structural pipeline stage. Contract tests
    # run via stage 9 verify which emits a JSON report (not junit XML).

    def test_cleanup_always_runs_on_success(self, pipeline):
        runner = _SimulatedJenkinsRunner(pipeline)
        runner.run()
        assert runner.cleanup_ran is True

    def test_cleanup_always_runs_on_failure(self, pipeline):
        runner = _SimulatedJenkinsRunner(pipeline, fail_command="--stage 2")
        runner.run()
        assert runner.cleanup_ran is True

    # Marketplace is not a structural stage in the 11-stage design —
    # it's a --target value of stage 10 publish (fluid publish <contract>
    # --target marketplace). The previous test_marketplace_publish_command_present
    # was dropped along with the standalone marketplace stage.

    def test_no_marketplace_command_in_the_default_pipeline(self):
        """Marketplace is a ``--target`` value of ship's stage 10, not a stage."""
        gen = PipelineTemplateGenerator()
        config = PipelineConfig(provider=PipelineProvider.JENKINS)
        content = gen.generate_pipeline(config)["Jenkinsfile"]
        pipeline = _JenkinsPipeline(content)
        all_cmds = []
        for stage in pipeline.stages:
            all_cmds.extend(stage.sh_commands)
        assert not any("marketplace" in c.lower() for c in all_cmds)


# ---------------------------------------------------------------------------
# 6. Cross-system regression: generated pipelines must not emit bare $CONTRACT
# ---------------------------------------------------------------------------


class TestGeneratedContractEnvAcrossSystems:
    """Every generated CI pipeline names the contract in each ``fluid ship``
    command. Where the CI system exports parameters to the shell (Jenkins), the
    command reads ``"$CONTRACT"`` and the ``environment {}`` block defines it
    with a fallback, so Build Now on a job that has no parameters yet still
    works; everywhere else the path is written into the command, never read
    from a variable that no environment block sets. Locked in across all seven
    providers so a regression in any one template is caught.
    """

    _PATH = "contracts/p/contract.fluid.yaml"

    @pytest.mark.parametrize("provider", list(PipelineProvider), ids=lambda p: p.name)
    def test_the_contract_is_in_every_ship_command_and_never_bare(self, provider):
        cfg = PipelineConfig(provider=provider, contract_path=self._PATH)
        files = PipelineTemplateGenerator().generate_pipeline(cfg)
        text = "\n".join(files.values())
        if provider is PipelineProvider.JENKINS:
            assert 'fluid ship "$CONTRACT" --stage' in text
            assert (
                f"CONTRACT = \"${{params.CONTRACT == null ? '{self._PATH}' : params.CONTRACT}}\""
                in text
            )
        else:
            assert f"fluid ship {self._PATH} --stage" in text
            for variable in ("$CONTRACT", "${CONTRACT", "$(params.CONTRACT"):
                assert variable not in text, f"{provider.value}: {variable}"


class TestFluidEnvDefaultAcrossSystems:
    """``--fluid-env-default`` reaches every system the way Jenkins gets it: the
    FLUID_ENV parameter's default (the shared ``_eleven_stage_parameters``
    table) and, where the system has one, its fallback. Nothing else changes."""

    @staticmethod
    def _files(provider, **kwargs):
        cfg = PipelineConfig(provider=provider, **kwargs)
        return PipelineTemplateGenerator().generate_pipeline(cfg)

    @pytest.mark.parametrize("provider", list(PipelineProvider), ids=lambda p: p.name)
    def test_only_the_fluid_env_default_moves(self, provider):
        default = self._files(provider)
        staging = self._files(provider, fluid_env_default="staging")
        assert set(staging) == set(default)
        changed = 0
        for path, content in staging.items():
            before_lines, after_lines = default[path].splitlines(), content.splitlines()
            assert len(before_lines) == len(after_lines), path
            for before, after in zip(before_lines, after_lines, strict=True):
                if before != after:
                    changed += 1
                    # The value only: the line is otherwise the one the default writes.
                    # The list of overlay names in a description is not the default,
                    # and Bitbucket's environment follows FLUID_ENV (dev -> test).
                    envs = "(dev | staging | prod)"
                    expected = (
                        after.replace(envs, "<envs>")
                        .replace("staging", "dev")
                        .replace("<envs>", envs)
                    )
                    if path == "bitbucket-pipelines.yml":
                        expected = expected.replace("`dev`", "`test`")
                        expected = expected.replace("deployment: dev", "deployment: test")
                        if " gives " in after:  # the line that explains the mapping
                            expected = after.replace("(staging)", "(dev)")
                    assert before == expected, (path, before, after)
        assert changed, f"{provider.value}: --fluid-env-default changed nothing"

    @pytest.mark.parametrize("provider", list(PipelineProvider), ids=lambda p: p.name)
    def test_the_parameter_table_declares_the_default(self, provider):
        cfg = PipelineConfig(provider=provider, fluid_env_default="staging")
        template = PipelineTemplateGenerator().templates[provider]
        declared = {
            name: default for name, _k, default, _d in template._eleven_stage_parameters(cfg)
        }
        assert declared["FLUID_ENV"] == "staging"

    @pytest.mark.parametrize("bad", ["_prod", "p" * 65], ids=["leading-underscore", "65-chars"])
    @pytest.mark.parametrize("provider", list(PipelineProvider), ids=lambda p: p.name)
    def test_a_default_fluid_publish_refuses_is_refused(self, provider, bad):
        """One config backs every system, and so does the rule: a default
        ``fluid publish --env`` refuses (a leading underscore, more than 64
        characters) fails generation for each, instead of the publish stage of
        every build that runs on the defaults, after the apply."""
        with pytest.raises(ValueError, match="fluid publish --env"):
            self._files(provider, fluid_env_default=bad)
        assert self._files(provider, fluid_env_default="p" * 64)


# ---------------------------------------------------------------------------
# Stage 3 is never inferred from builds[].pattern
# ---------------------------------------------------------------------------


def _stage_3_default(system: str, root: Path) -> str:
    """The generated default of ``RUN_STAGE_3_GENERATE_ARTIFACTS``."""
    if system == "github":
        workflow = yaml.safe_load((root / ".github/workflows/fluid-pipeline.yml").read_text())
        value = workflow["env"]["RUN_STAGE_3_GENERATE_ARTIFACTS"]
        return re.search(r"\|\| '(true|false)' \}\}$", value).group(1)
    for doc in yaml.safe_load_all((root / "tekton/tasks.yaml").read_text()):
        if doc and doc["kind"] == "Task" and doc["metadata"]["name"] == "fluid-ship":
            return {p["name"]: p.get("default") for p in doc["spec"]["params"]}[
                "RUN_STAGE_3_GENERATE_ARTIFACTS"
            ]
    raise AssertionError("no fluid-ship Task")


_SPARK = {"id": "spark_{n}", "engine": "spark", "properties": {"model": "m"}}
_REFERENCE = {"id": "dbt_ref", "engine": "dbt", "pattern": "hybrid-reference"}


class TestStageThreeIsNotInferred:
    """Stage 3 writes the policy bindings and the schedule that stages 4, 8 and
    11 read, and ``fluid generate artifacts`` writes them for reference builds
    too, so only ``--no-generate-artifacts`` turns it off. It used to be off for
    a contract with any reference build, which left a mixed contract's stages
    4, 8 and 11 without their inputs."""

    def _generate(self, tmp_path, monkeypatch, system, builds, **extra) -> str:
        contract = {
            "fluidVersion": "0.7.5",
            "kind": "DataProduct",
            "id": "gold.mixed",
            "name": "mixed",
            "builds": builds,
        }
        (tmp_path / "contract.fluid.yaml").write_text(yaml.safe_dump(contract))
        monkeypatch.chdir(tmp_path)
        assert generate_ci_run(_make_args(system=system, **extra), _logger) == 0
        return _stage_3_default(system, tmp_path)

    @pytest.mark.parametrize("system", ["github", "tekton"])
    def test_a_mixed_contract_runs_stage_3(self, tmp_path, monkeypatch, system):
        builds = [
            {**_SPARK, "id": "spark_1"},
            {**_SPARK, "id": "spark_2"},
            _REFERENCE,
        ]
        assert self._generate(tmp_path, monkeypatch, system, builds) == "true"

    @pytest.mark.parametrize("pattern", ["hybrid-reference", "reference", "external-reference"])
    def test_a_contract_of_reference_builds_runs_stage_3(self, tmp_path, monkeypatch, pattern):
        builds = [{**_REFERENCE, "pattern": pattern}]
        assert self._generate(tmp_path, monkeypatch, "github", builds) == "true"

    @pytest.mark.parametrize("system", ["github", "tekton"])
    def test_the_flag_turns_stage_3_off(self, tmp_path, monkeypatch, system):
        default = self._generate(
            tmp_path, monkeypatch, system, [_REFERENCE], no_generate_artifacts=True
        )
        assert default == "false"

    def test_the_detector_is_gone(self):
        from fluid_build.cli import generate_ci

        assert not hasattr(generate_ci, "_contract_is_reference_only")


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
    stage is off by default in the emitted pipeline. Nothing else turns it
    off (``TestStageThreeIsNotInferred``).
    """

    # CRITICAL: every test here must `monkeypatch.chdir(tmp_path)` because
    # `generate_ci.run()` writes files via `atomic_write(rel_path, ...)`
    # regardless of whether the PipelineTemplateGenerator is mocked. Without
    # chdir, the test clobbers the repo's real Jenkinsfile at the repo root.

    @patch("fluid_build.cli.generate_ci._git_prefix", return_value=None)
    @patch("fluid_build.forge.core.pipeline_templates.PipelineTemplateGenerator")
    def test_flag_forces_generates_artifacts_false(
        self, mock_gen, _mock_prefix, tmp_path, monkeypatch
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
        )

        generate_ci_run(args, _logger)
        cfg = mock_gen.return_value.generate_pipeline.call_args[0][0]
        assert cfg.generates_artifacts is False

    @patch("fluid_build.cli.generate_ci._git_prefix", return_value="examples/demo")
    @patch("fluid_build.forge.core.pipeline_templates.PipelineTemplateGenerator")
    def test_git_prefix_propagates_to_workdir(self, mock_gen, _mock_prefix, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        from fluid_build.cli.generate_ci import run as generate_ci_run

        mock_gen.return_value.generate_pipeline.return_value = {"Jenkinsfile": "pipeline {}\n"}

        args = argparse.Namespace(
            system="jenkins",
            out=None,
            contract="contract.fluid.yaml",
            no_generate_artifacts=False,
            provider=None,
        )

        generate_ci_run(args, _logger)
        cfg = mock_gen.return_value.generate_pipeline.call_args[0][0]
        assert cfg.workdir == "examples/demo"
