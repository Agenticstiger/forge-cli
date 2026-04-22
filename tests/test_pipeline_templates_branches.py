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

"""Branch-coverage tests for fluid_build.forge.core.pipeline_templates"""

import pytest

from fluid_build.forge.core.pipeline_templates import (
    BasePipelineTemplate,
    GitHubActionsTemplate,
    PipelineComplexity,
    PipelineConfig,
    PipelineProvider,
    PipelineTemplateGenerator,
)

# ── Enum tests ──────────────────────────────────────────────────────


class TestPipelineProvider:
    @pytest.mark.parametrize(
        "member,value",
        [
            ("GITHUB_ACTIONS", "github_actions"),
            ("GITLAB_CI", "gitlab_ci"),
            ("AZURE_DEVOPS", "azure_devops"),
            ("JENKINS", "jenkins"),
            ("BITBUCKET", "bitbucket"),
            ("CIRCLE_CI", "circle_ci"),
            ("TEKTON", "tekton"),
        ],
    )
    def test_values(self, member, value):
        assert PipelineProvider[member].value == value


class TestPipelineComplexity:
    @pytest.mark.parametrize(
        "member,value",
        [
            ("BASIC", "basic"),
            ("STANDARD", "standard"),
            ("ADVANCED", "advanced"),
            ("ENTERPRISE", "enterprise"),
        ],
    )
    def test_values(self, member, value):
        assert PipelineComplexity[member].value == value


# ── PipelineConfig tests ────────────────────────────────────────────


class TestPipelineConfig:
    def test_basic_sets_dev_only(self):
        cfg = PipelineConfig(
            provider=PipelineProvider.GITHUB_ACTIONS,
            complexity=PipelineComplexity.BASIC,
        )
        assert cfg.environments == ["dev"]

    def test_standard_sets_dev_staging(self):
        cfg = PipelineConfig(
            provider=PipelineProvider.GITHUB_ACTIONS,
            complexity=PipelineComplexity.STANDARD,
        )
        assert cfg.environments == ["dev", "staging"]

    def test_advanced_sets_all_envs(self):
        cfg = PipelineConfig(
            provider=PipelineProvider.GITHUB_ACTIONS,
            complexity=PipelineComplexity.ADVANCED,
        )
        assert cfg.environments == ["dev", "staging", "prod"]

    def test_enterprise_sets_all_envs(self):
        cfg = PipelineConfig(
            provider=PipelineProvider.GITHUB_ACTIONS,
            complexity=PipelineComplexity.ENTERPRISE,
        )
        assert cfg.environments == ["dev", "staging", "prod"]

    def test_custom_environments_preserved(self):
        cfg = PipelineConfig(
            provider=PipelineProvider.JENKINS,
            complexity=PipelineComplexity.BASIC,
            environments=["qa", "prod"],
        )
        assert cfg.environments == ["qa", "prod"]

    def test_notification_channels_default_empty(self):
        cfg = PipelineConfig(
            provider=PipelineProvider.JENKINS,
            complexity=PipelineComplexity.BASIC,
        )
        assert cfg.notification_channels == []

    def test_custom_steps_default_empty(self):
        cfg = PipelineConfig(
            provider=PipelineProvider.JENKINS,
            complexity=PipelineComplexity.BASIC,
        )
        assert cfg.custom_steps == []

    def test_enable_flags_defaults(self):
        cfg = PipelineConfig(
            provider=PipelineProvider.JENKINS,
            complexity=PipelineComplexity.BASIC,
        )
        assert cfg.enable_approvals is False
        assert cfg.enable_security_scan is True
        assert cfg.enable_performance_monitoring is True
        assert cfg.enable_marketplace_publishing is False


# ── BasePipelineTemplate tests ──────────────────────────────────────


class TestBasePipelineTemplate:
    def test_init_defaults(self):
        t = BasePipelineTemplate()
        assert t.provider_name == "unknown"
        assert t.file_extensions == [".yml"]

    def test_generate_raises_not_implemented(self):
        t = BasePipelineTemplate()
        config = PipelineConfig(
            provider=PipelineProvider.JENKINS,
            complexity=PipelineComplexity.BASIC,
        )
        with pytest.raises(NotImplementedError):
            t.generate(config)

    def test_get_features(self):
        t = BasePipelineTemplate()
        features = t.get_features()
        assert features["multi_environment"] is True
        assert features["approvals"] is True
        assert features["security_scanning"] is True
        assert features["artifact_management"] is True
        assert features["notifications"] is True
        assert features["parallel_execution"] is True
        assert features["matrix_builds"] is True

    def test_get_fluid_commands(self):
        t = BasePipelineTemplate()
        cmds = t._get_fluid_commands()
        assert "validate" in cmds
        assert "plan" in cmds
        assert "apply" in cmds
        assert "test" in cmds
        assert "contract_test" in cmds
        assert "visualize" in cmds
        assert "publish_opds" in cmds
        assert "marketplace_publish" in cmds
        assert "doctor" in cmds
        assert "fluid" in cmds["validate"]

    def test_get_common_environment_vars(self):
        t = BasePipelineTemplate()
        env = t._get_common_environment_vars()
        assert "FLUID_LOG_LEVEL" in env
        assert "FLUID_CONFIG_PATH" in env
        assert "PYTHONPATH" in env
        assert "PIP_CACHE_DIR" in env


# ── PipelineTemplateGenerator tests ─────────────────────────────────


class TestPipelineTemplateGenerator:
    def test_init_populates_templates(self):
        gen = PipelineTemplateGenerator()
        assert PipelineProvider.GITHUB_ACTIONS in gen.templates
        assert PipelineProvider.GITLAB_CI in gen.templates
        assert PipelineProvider.AZURE_DEVOPS in gen.templates
        assert PipelineProvider.JENKINS in gen.templates
        assert PipelineProvider.BITBUCKET in gen.templates
        assert PipelineProvider.CIRCLE_CI in gen.templates
        assert PipelineProvider.TEKTON in gen.templates

    def test_list_available_providers(self):
        gen = PipelineTemplateGenerator()
        providers = gen.list_available_providers()
        assert "github_actions" in providers
        assert "jenkins" in providers

    def test_generate_unsupported_provider(self):
        gen = PipelineTemplateGenerator()
        # Remove a provider to trigger error
        del gen.templates[PipelineProvider.TEKTON]
        config = PipelineConfig(
            provider=PipelineProvider.TEKTON,
            complexity=PipelineComplexity.BASIC,
        )
        with pytest.raises(ValueError, match="Unsupported provider"):
            gen.generate_pipeline(config)

    def test_get_provider_features_valid(self):
        gen = PipelineTemplateGenerator()
        features = gen.get_provider_features(PipelineProvider.GITHUB_ACTIONS)
        assert "multi_environment" in features

    def test_get_provider_features_invalid(self):
        gen = PipelineTemplateGenerator()
        del gen.templates[PipelineProvider.TEKTON]
        features = gen.get_provider_features(PipelineProvider.TEKTON)
        assert features == {}


# ── GitHubActionsTemplate generate branches ─────────────────────────


class TestGitHubActionsGenerate:
    def test_init(self):
        t = GitHubActionsTemplate()
        assert t.provider_name == "GitHub Actions"
        assert ".yml" in t.file_extensions

    def test_basic_workflow(self):
        t = GitHubActionsTemplate()
        config = PipelineConfig(
            provider=PipelineProvider.GITHUB_ACTIONS,
            complexity=PipelineComplexity.BASIC,
        )
        result = t.generate(config)
        assert isinstance(result, dict)
        assert any("github/workflows" in k for k in result)

    def test_standard_workflow(self):
        t = GitHubActionsTemplate()
        config = PipelineConfig(
            provider=PipelineProvider.GITHUB_ACTIONS,
            complexity=PipelineComplexity.STANDARD,
        )
        result = t.generate(config)
        assert isinstance(result, dict)

    def test_advanced_workflow(self):
        t = GitHubActionsTemplate()
        config = PipelineConfig(
            provider=PipelineProvider.GITHUB_ACTIONS,
            complexity=PipelineComplexity.ADVANCED,
        )
        result = t.generate(config)
        assert isinstance(result, dict)

    def test_enterprise_workflow(self):
        t = GitHubActionsTemplate()
        config = PipelineConfig(
            provider=PipelineProvider.GITHUB_ACTIONS,
            complexity=PipelineComplexity.ENTERPRISE,
        )
        result = t.generate(config)
        assert isinstance(result, dict)


# ── Generate pipeline through generator ─────────────────────────────


class TestGeneratePipeline:
    @pytest.mark.parametrize("provider", list(PipelineProvider))
    def test_all_providers_basic(self, provider):
        gen = PipelineTemplateGenerator()
        config = PipelineConfig(
            provider=provider,
            complexity=PipelineComplexity.BASIC,
        )
        result = gen.generate_pipeline(config)
        assert isinstance(result, dict)
        assert len(result) > 0

    @pytest.mark.parametrize("complexity", list(PipelineComplexity))
    def test_github_all_complexities(self, complexity):
        gen = PipelineTemplateGenerator()
        config = PipelineConfig(
            provider=PipelineProvider.GITHUB_ACTIONS,
            complexity=complexity,
        )
        result = gen.generate_pipeline(config)
        assert isinstance(result, dict)


# ── S-005: GitLab CI OIDC credential hygiene ────────────────────────


class TestGitlabOidcCredentialHygiene:
    """SECURITY_REVIEW S-005: generated GitLab CI templates must never
    write federated credentials to a predictable ``/tmp/*.json`` path.

    The fix uses ``mktemp`` for a random filename, ``chmod 600`` to
    restrict permissions, and ``trap 'rm -f' EXIT`` to clean up when the
    shell exits — so the credential material exists on disk only for
    the lifetime of the single shell invocation that consumes it.
    """

    def _generate_gitlab_yaml(self, oidc_provider):
        gen = PipelineTemplateGenerator()
        # STANDARD and ENTERPRISE hit the deploy-per-environment branch
        # where the OIDC credential snippet is emitted.
        config = PipelineConfig(
            provider=PipelineProvider.GITLAB_CI,
            complexity=PipelineComplexity.STANDARD,
            oidc_provider=oidc_provider,
        )
        result = gen.generate_pipeline(config)
        assert ".gitlab-ci.yml" in result
        return result[".gitlab-ci.yml"]

    @pytest.mark.parametrize("oidc_provider", ["gcp", "aws"])
    def test_no_literal_tmp_paths(self, oidc_provider):
        yaml_out = self._generate_gitlab_yaml(oidc_provider)
        assert "/tmp/oidc_token.json" not in yaml_out
        assert "/tmp/aws_creds.json" not in yaml_out

    @pytest.mark.parametrize("oidc_provider", ["gcp", "aws"])
    def test_uses_mktemp_chmod_trap(self, oidc_provider):
        yaml_out = self._generate_gitlab_yaml(oidc_provider)
        # mktemp-backed random filename
        assert 'CRED_FILE="$(mktemp)"' in yaml_out
        # permission-tightening on the temp file
        assert 'chmod 600 "$CRED_FILE"' in yaml_out
        # cleanup on shell exit — key hygiene win
        assert "trap 'rm -f \"$CRED_FILE\"' EXIT" in yaml_out

    def test_gcp_consumes_cred_file(self):
        yaml_out = self._generate_gitlab_yaml("gcp")
        assert 'gcloud auth login --cred-file="$CRED_FILE"' in yaml_out
        # Still reads the OIDC token from the env var (GitLab native
        # id_tokens injection) — not from a hard-coded /tmp path.
        assert '"${FLUID_OIDC_TOKEN}" > "$CRED_FILE"' in yaml_out

    def test_aws_writes_sts_output_to_cred_file(self):
        yaml_out = self._generate_gitlab_yaml("aws")
        assert "aws sts assume-role-with-web-identity" in yaml_out
        assert '> "$CRED_FILE"' in yaml_out

    def test_no_oidc_no_cred_snippets(self):
        """Smoke test: without an OIDC provider configured, none of the
        credential-file plumbing appears in the output."""
        yaml_out = self._generate_gitlab_yaml(oidc_provider=None)
        assert "mktemp" not in yaml_out
        assert "FLUID_OIDC_TOKEN" not in yaml_out


# ── PipelineConfig.workdir + generates_artifacts ────────────────────


class TestPipelineConfigWorkdirAndArtifacts:
    """New fields on PipelineConfig: ``workdir`` (SCM-root → contract folder
    relative path) and ``generates_artifacts`` (skip Generate Artifacts stage
    for reference-only contracts)."""

    def test_workdir_default_is_none(self):
        cfg = PipelineConfig(
            provider=PipelineProvider.JENKINS,
            complexity=PipelineComplexity.BASIC,
        )
        assert cfg.workdir is None

    def test_workdir_explicit_value_preserved(self):
        cfg = PipelineConfig(
            provider=PipelineProvider.JENKINS,
            complexity=PipelineComplexity.BASIC,
            workdir="examples/demo",
        )
        assert cfg.workdir == "examples/demo"

    def test_generates_artifacts_default_true(self):
        cfg = PipelineConfig(
            provider=PipelineProvider.JENKINS,
            complexity=PipelineComplexity.BASIC,
        )
        assert cfg.generates_artifacts is True

    def test_generates_artifacts_explicit_false(self):
        cfg = PipelineConfig(
            provider=PipelineProvider.JENKINS,
            complexity=PipelineComplexity.BASIC,
            generates_artifacts=False,
        )
        assert cfg.generates_artifacts is False


# ── Jenkins template hardening ──────────────────────────────────────


class TestJenkinsTemplateHardening:
    """Jenkins template now hardens against three independent regressions:

    1. **Subfolder checkouts.** Jenkins checks out at the SCM root; when
       ``fluid generate ci`` runs from a product subfolder, every ``sh`` step
       must be prefixed with ``cd "<workdir>" && ...``. Otherwise the fluid
       command runs in the wrong dir and every contract reference fails.

    2. **Reference-only contracts.** When ``generates_artifacts=False``, the
       ``Generate Artifacts`` stage must be omitted entirely — not just
       no-op'd. Emitting it surfaces spurious failures for externally-owned
       dbt/Airflow projects.

    3. **Missing-artifact tolerance.** Reference-only contracts don't produce
       ``plan.json`` or test-results XML, so ``archiveArtifacts`` and ``junit``
       must tolerate empty matches. Without ``allowEmptyArchive: true`` /
       ``allowEmptyResults: true`` the build fails at the archive stage.
    """

    def _jenkinsfile(self, **kwargs):
        cfg = PipelineConfig(
            provider=PipelineProvider.JENKINS,
            complexity=PipelineComplexity.BASIC,
            **kwargs,
        )
        files = PipelineTemplateGenerator().generate_pipeline(cfg)
        return files["Jenkinsfile"]

    # Regression 1: workdir wrapping -----------------------------------

    def test_no_workdir_produces_no_cd_wrapper(self):
        content = self._jenkinsfile()
        # Without workdir, commands should not have a `cd "..." &&` prefix.
        assert (
            'cd "' not in content
        ), "Jenkinsfile should not contain cd wrappers when workdir is unset"

    def test_workdir_wraps_every_fluid_command(self):
        content = self._jenkinsfile(workdir="examples/demo")
        assert 'cd "examples/demo" && fluid validate' in content
        assert 'cd "examples/demo" && fluid plan' in content

    def test_workdir_prefixes_archive_patterns(self):
        content = self._jenkinsfile(workdir="examples/demo")
        # plan.json archive path must be workdir-prefixed or Jenkins can't
        # find it (archiveArtifacts is rooted at the SCM checkout root).
        assert "examples/demo/runtime/plan.json" in content

    def test_workdir_prefixes_junit_patterns(self):
        content = self._jenkinsfile(workdir="examples/demo")
        assert "examples/demo/test-results-unit.xml" in content
        assert "examples/demo/test-results-integration.xml" in content

    # Regression 2: reference-only stage omission ----------------------

    def test_default_contains_generate_artifacts_stage(self):
        content = self._jenkinsfile()
        assert "stage('Generate Artifacts')" in content

    def test_generates_artifacts_false_omits_stage(self):
        content = self._jenkinsfile(generates_artifacts=False)
        assert "stage('Generate Artifacts')" not in content
        # Adjacent stages must still be present — we're skipping a stage,
        # not breaking the template.
        assert "stage('Validate')" in content
        assert "stage('Plan')" in content

    # Regression 3: empty-archive/result tolerance --------------------

    def test_archiveartifacts_tolerates_empty(self):
        content = self._jenkinsfile()
        # Every archiveArtifacts must include allowEmptyArchive: true so
        # reference-only contracts (no plan.json) don't fail the build.
        archive_lines = [line for line in content.splitlines() if "archiveArtifacts" in line]
        assert archive_lines, "expected at least one archiveArtifacts line"
        for line in archive_lines:
            assert (
                "allowEmptyArchive: true" in line
            ), f"archiveArtifacts without allowEmptyArchive breaks reference-only builds: {line}"

    def test_junit_tolerates_empty_results(self):
        content = self._jenkinsfile()
        junit_lines = [line for line in content.splitlines() if line.strip().startswith("junit ")]
        assert junit_lines, "expected at least one junit line"
        for line in junit_lines:
            assert (
                "allowEmptyResults: true" in line
            ), f"junit without allowEmptyResults breaks reference-only builds: {line}"

    # Other hardening fixes --------------------------------------------

    def test_uses_fluid_test_no_data_for_unit(self):
        """`fluid test` has no --type flag; --no-data is the structural-only
        mode. Emitting --type unit silently fails with unrecognised-arg."""
        content = self._jenkinsfile()
        assert "fluid test" in content
        assert "--no-data" in content
        # The broken `--type unit` shape must not appear.
        assert "--type unit" not in content
        assert "--type integration" not in content

    def test_fluid_env_uses_export_not_inline(self):
        """`FLUID_ENV=dev cmd` only exports for one command; subshell
        invocations inside ``cmd`` don't see it. The `export FLUID_ENV=dev;
        cmd` form sets it for the whole shell."""
        content = self._jenkinsfile()
        # Inline form is a regression — would break subshells.
        assert "sh 'FLUID_ENV=" not in content
        # Export form is the expected shape.
        assert "export FLUID_ENV=" in content

    def test_doctor_not_extended(self):
        """`fluid doctor --extended` requires scripts/diagnose.sh which
        forge-generated variants don't ship — running it always fails with
        a CLIError. Generated pipelines must call plain `fluid doctor`."""
        content = self._jenkinsfile()
        assert "fluid doctor" in content
        assert "fluid doctor --extended" not in content
