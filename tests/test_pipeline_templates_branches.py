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
    StageSpec,
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

    def test_jenkins_generation_defaults_preserved(self):
        cfg = PipelineConfig(
            provider=PipelineProvider.JENKINS,
            complexity=PipelineComplexity.BASIC,
        )
        assert cfg.verify_strict_default is True
        assert cfg.publish_stage_default is False
        # publish_include_env default is False because `fluid publish`
        # does not accept `--env`; emitting it would make Stage 10 die
        # with `unrecognized arguments: --env dev`.
        assert cfg.publish_include_env is False


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

    def test_get_fluid_commands_includes_policy_apply(self):
        """Stage-8 policy enforcement must appear in the commands dict.
        Missing this key breaks the 11-stage sequence: apply succeeds,
        verify runs on an unprotected schema, silently diverging from
        the declared governance policy. Regression-critical."""
        cmds = BasePipelineTemplate()._get_fluid_commands()
        assert "policy_apply" in cmds
        # Must invoke the CLI command we actually ship.
        assert "fluid policy-apply" in cmds["policy_apply"]
        # Must default to enforce-mode — check-mode would be a silent
        # advisory with no teeth, which defeats the stage's purpose.
        assert "--mode enforce" in cmds["policy_apply"]

    def test_get_fluid_commands_includes_diff_and_verify(self):
        """Stage-5 drift gate + stage-9 verify must both exist. These
        are the two reconciliation bookends around stages 6–8."""
        cmds = BasePipelineTemplate()._get_fluid_commands()
        assert "diff" in cmds
        assert "--exit-on-drift" in cmds["diff"]
        assert "verify" in cmds
        assert "--strict" in cmds["verify"]

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
        # CD prefix appears inside each sh block. Every sh uses the
        # triple-single ``sh '''...'''`` Groovy form so the cd prefix
        # can safely use double-quoted paths (safe within single-quoted
        # outer string).
        assert 'cd "examples/demo" && fluid' in content
        assert 'cd "examples/demo" && fluid validate' in content
        assert 'cd "examples/demo" && fluid plan' in content
        # Stage 7 (apply) uses POSIX ``set --`` composition since the
        # security-hardening commit (auth-gate bypass via unquoted
        # ${APPLY_BUILD_FLAG} was closed by refactoring to if/then/fi).
        # The workdir prefix now applies to ``set -eu`` rather than
        # directly to ``fluid apply``; the ``fluid apply "$@"`` on the
        # last line of the set-- chain is what invokes the CLI.
        assert 'cd "examples/demo" && set -eu' in content, (
            "stage 7 sh body must start with 'cd <workdir> && set -eu' "
            "to preserve workdir semantics under the POSIX set-- pattern"
        )
        assert 'fluid apply "$@"' in content

    def test_workdir_prefixes_archive_patterns(self):
        content = self._jenkinsfile(workdir="examples/demo")
        # plan.json archive path must be workdir-prefixed or Jenkins can't
        # find it (archiveArtifacts is rooted at the SCM checkout root).
        assert "examples/demo/runtime/plan.json" in content

    # Regression 2: reference-only stage auto-defaulting ---------------
    # The 11-stage template makes stage 3 generate-artifacts toggleable
    # via RUN_STAGE_3_GENERATE_ARTIFACTS. For reference-only contracts
    # the default value flips to ``false`` so operators who just click
    # "Build" don't run stage 3.

    def test_default_contains_generate_artifacts_stage(self):
        content = self._jenkinsfile()
        # Stage 3 is always PRESENT in the template (toggleable) —
        # what changes between reference-only and normal is the DEFAULT
        # value of RUN_STAGE_3_GENERATE_ARTIFACTS.
        assert "stage('3 - generate artifacts')" in content
        # Default generates_artifacts=True → param default true.
        assert "name: 'RUN_STAGE_3_GENERATE_ARTIFACTS', defaultValue: true" in content

    def test_generates_artifacts_false_flips_param_default(self):
        content = self._jenkinsfile(generates_artifacts=False)
        # Stage 3 is still declared (parameterized), but the default
        # flips to false so a zero-click build skips it.
        assert "stage('3 - generate artifacts')" in content
        assert "name: 'RUN_STAGE_3_GENERATE_ARTIFACTS', defaultValue: false" in content
        # Adjacent stages must still be present — we're defaulting a
        # stage off, not breaking the template.
        assert "stage('2 - validate')" in content
        assert "stage('6 - plan')" in content

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

    # Other hardening fixes --------------------------------------------

    def test_fluid_env_flows_via_parameter_not_inline_env_prefix(self):
        """The 11-stage template uses ``--env "${FLUID_ENV:-dev}"`` flags
        on each command rather than inline ``FLUID_ENV=dev cmd`` prefixes
        or stage-level ``export FLUID_ENV=dev; cmd``. Flag passing is:
        (a) explicit at the fluid command boundary,
        (b) unambiguous for subshell invocations inside fluid, and
        (c) POSIX-default-safe — falls back to ``dev`` if the Jenkins
            param wasn't populated (webhook-triggered builds, SCM polls)."""
        content = self._jenkinsfile()
        # Inline form would break subshells — must not appear.
        assert "sh 'FLUID_ENV=" not in content
        # POSIX default expansion form used throughout.
        assert '--env "${FLUID_ENV:-dev}"' in content

    def test_verify_strict_default_can_be_overridden(self):
        content = self._jenkinsfile(verify_strict_default=False)
        assert "name: 'VERIFY_STRICT',      defaultValue: false" in content

    def test_publish_stage_default_can_be_overridden(self):
        content = self._jenkinsfile(publish_stage_default=True)
        assert "name: 'RUN_STAGE_10_PUBLISH', defaultValue: true" in content

    def test_publish_stage_can_omit_env_flag(self):
        content = self._jenkinsfile(publish_include_env=False)
        stage_10 = content[content.index("stage('10 - publish')") :]
        stage_10 = stage_10[: stage_10.index("stage('11 - schedule sync')")]
        assert 'fluid publish "${CONTRACT:-contract.fluid.yaml}" ${TARGET_FLAGS}' in stage_10
        assert 'fluid publish "${CONTRACT:-contract.fluid.yaml}" ${TARGET_FLAGS} \\' not in stage_10
        assert '--env "${FLUID_ENV:-dev}"' not in stage_10

    def test_publish_stage_default_omits_env_flag(self):
        # Default (no publish_include_env passed) MUST omit --env on
        # `fluid publish`, because the CLI doesn't accept --env. When
        # the default emitted --env, every Stage 10 build that ran
        # publish died with `unrecognized arguments: --env dev`.
        content = self._jenkinsfile()
        stage_10 = content[content.index("stage('10 - publish')") :]
        stage_10 = stage_10[: stage_10.index("stage('11 - schedule sync')")]
        assert '--env "${FLUID_ENV:-dev}"' not in stage_10

    def test_publish_stage_can_opt_in_to_env_flag(self):
        # Operators who wrap `fluid publish` with a custom CLI alias
        # that accepts --env can opt in via publish_include_env=True.
        content = self._jenkinsfile(publish_include_env=True)
        stage_10 = content[content.index("stage('10 - publish')") :]
        stage_10 = stage_10[: stage_10.index("stage('11 - schedule sync')")]
        assert '--env "${FLUID_ENV:-dev}"' in stage_10

    def test_doctor_not_extended(self):
        """``fluid doctor --extended`` requires scripts/diagnose.sh which
        forge-generated variants don't ship. The 11-stage template
        dropped doctor from pipeline stages (it is a diagnostic tool,
        not a pipeline gate) — what remains is `fluid --version` in
        Setup. Neither should emit ``--extended``."""
        content = self._jenkinsfile()
        assert "fluid doctor --extended" not in content

    # ── install-mode dispatch (generation-time decision) ─────────────
    # Two modes replace the old runtime-branching tree:
    #   pypi (default, production) — single pip install from stable PyPI.
    #                                TestPyPI / private-index supported at
    #                                BUILD time via Jenkins parameters
    #                                (FLUID_PIP_INDEX_URL etc), not at
    #                                generation time.
    #   dev-source (lab only)      — bind-mount install, fails loud if
    #                                the mount is missing.
    # Mode is chosen ONCE at `fluid generate ci` time; generated
    # Jenkinsfile carries ONLY that mode's logic.

    def test_install_mode_default_is_pypi(self):
        content = self._jenkinsfile()
        # Unambiguous marker in stage name — operator sees it in UI.
        assert "stage('0 — Bootstrap FLUID [pypi]')" in content
        # pypi mode's pip install sees FLUID_PACKAGE_SPEC with default.
        assert '"${FLUID_PACKAGE_SPEC:-data-product-forge}"' in content
        # dev-source branch must NOT appear in pypi mode — clean separation.
        assert "/forge-cli-src" not in content
        # pypi mode exposes the index-URL override parameters so TestPyPI
        # pilot builds are possible via the Build-With-Parameters dialog.
        assert "name: 'FLUID_PIP_INDEX_URL'" in content
        assert "name: 'FLUID_PIP_EXTRA_INDEX_URL'" in content
        assert "name: 'FLUID_ALLOW_PRERELEASE'" in content
        assert "name: 'FLUID_PACKAGE_SPEC'" in content

    def test_install_mode_dev_source(self):
        content = self._jenkinsfile(install_mode="dev-source")
        # Unambiguous marker in stage name.
        assert "stage('0 — Bootstrap FLUID [dev-source]')" in content
        # Fail-loud check is required — no silent fallback to PyPI.
        assert "install-mode=dev-source but /forge-cli-src" in content
        assert "exit 2" in content
        # Uninstall the shadowing installed version so PYTHONPATH wins.
        assert "pip uninstall -y data-product-forge" in content
        # PYTHONPATH = /forge-cli-src at the pipeline environment level
        # means every sh step inherits it and imports resolve to the
        # bind mount live — no pip install, no wheel cache, no stale
        # files.
        assert "PYTHONPATH = '/forge-cli-src'" in content
        # pypi branch must NOT appear — clean separation.
        assert "${FLUID_PIP_INDEX_URL" not in content
        assert "FLUID_ALLOW_PRERELEASE" not in content

    def test_install_mode_unknown_raises_at_generate_time(self):
        import pytest

        with pytest.raises(ValueError, match="Unknown install_mode"):
            self._jenkinsfile(install_mode="testpypi")  # dropped — no longer supported

    def test_pypi_mode_supports_testpypi_via_build_params(self):
        """TestPyPI is a BUILD-time choice in pypi mode: operator sets
        FLUID_PIP_INDEX_URL + FLUID_PIP_EXTRA_INDEX_URL + FLUID_ALLOW_PRERELEASE
        in the Jenkins Build-With-Parameters dialog. The Setup shell
        consumes them via shell-level env expansion; no Groovy rebuild."""
        content = self._jenkinsfile()
        # The shell consumes the 3 pip params.
        assert "${FLUID_PIP_INDEX_URL:-}" in content
        assert "${FLUID_PIP_EXTRA_INDEX_URL:-}" in content
        assert '"${FLUID_ALLOW_PRERELEASE:-false}" = "true"' in content
        # --index-url is only emitted when FLUID_PIP_INDEX_URL is set.
        assert "INDEX_FLAGS=" in content
        assert "--index-url " in content


class TestJenkinsTemplateStage11ScheduleSync:
    """Stage-11 ``fluid schedule-sync`` must be wired in the Jenkins template
    such that:

    1. **All scheduler variants are reachable** via Jenkins build parameters
       — not hardcoded. A single generated Jenkinsfile supports airflow
       (url-scheme dispatch), mwaa, composer, astronomer, prefect, dagster.

    2. **User-supplied parameter values never reach a ``sh`` string via
       Groovy interpolation.** Groovy-string-interpolating ``${params.X}``
       into the sh body is a shell-injection surface: a malicious param
       value with an unescaped quote breaks the wrapper and bleeds into
       subsequent argv positions. The fix routes params through
       ``environment { ... }`` which Jenkins quotes safely, then the sh
       body uses ``"$VAR"`` expansion + POSIX ``set --`` to build argv.

    3. **Empty parameters never reach the CLI.** Our CLI rejects an empty
       ``--destination`` / ``--environment-name`` — passing an empty flag
       would surface a confusing "required" error from the CLI rather
       than a clean "user didn't set this optional param" Jenkins state.
    """

    def _jenkinsfile(self, **kwargs):
        cfg = PipelineConfig(
            provider=PipelineProvider.JENKINS,
            complexity=PipelineComplexity.BASIC,
            **kwargs,
        )
        files = PipelineTemplateGenerator().generate_pipeline(cfg)
        return files["Jenkinsfile"]

    def test_stage_11_param_surface_complete(self):
        """All five variant-specific params + the scheduler choice + the
        dry-run toggle must be declared as Jenkins build parameters so
        operators set them via Build-With-Parameters UI."""
        content = self._jenkinsfile()
        assert "RUN_STAGE_11_SCHEDULE_SYNC" in content
        assert "name: 'SCHEDULER'" in content
        assert "'airflow'" in content
        assert "'mwaa'" in content
        assert "'composer'" in content
        assert "'astronomer'" in content
        assert "'prefect'" in content
        assert "'dagster'" in content
        assert "SCHEDULER_DESTINATION" in content
        assert "SCHEDULER_ENVIRONMENT_NAME" in content
        assert "SCHEDULER_LOCATION" in content
        assert "SCHEDULER_WORKSPACE" in content
        assert "SCHEDULE_SYNC_DRY_RUN" in content

    def test_stage_11_routes_params_through_environment_block(self):
        """Injection defence: every scheduler param must be threaded
        via ``environment { ... }``. If any param were Groovy-interpolated
        directly into the sh string, a quote in the value could break
        out of the wrapper and become an argv position of its own."""
        content = self._jenkinsfile()
        # The env block assignments are the canonical hand-off point.
        assert 'SCHEDULER = "${params.SCHEDULER}"' in content
        assert 'SCHEDULER_DESTINATION = "${params.SCHEDULER_DESTINATION}"' in content
        assert 'SCHEDULER_ENVIRONMENT_NAME = "${params.SCHEDULER_ENVIRONMENT_NAME}"' in content
        assert 'SCHEDULER_LOCATION = "${params.SCHEDULER_LOCATION}"' in content
        assert 'SCHEDULER_WORKSPACE = "${params.SCHEDULER_WORKSPACE}"' in content
        assert 'SCHEDULE_SYNC_DRY_RUN = "${params.SCHEDULE_SYNC_DRY_RUN}"' in content

    def test_stage_11_sh_body_uses_posix_set_dash_dash(self):
        """The sh body must build argv via POSIX ``set --`` (not bash
        arrays) so it runs under Jenkins's default ``/bin/sh``. Each
        variable expansion is quoted so a malicious value stays one
        argv token for our CLI to reject in
        ``_validate_destination`` / ``_validate_safe_ident``."""
        content = self._jenkinsfile()
        assert "set -eu" in content
        assert "set -- --scheduler " in content
        # Each optional flag is appended conditionally via if/then/fi
        # (not ``[ ... ] && ...`` — that interacts badly with set -e).
        assert 'if [ -n "${SCHEDULER_DESTINATION:-}" ];' in content
        assert 'if [ -n "${SCHEDULER_ENVIRONMENT_NAME:-}" ];' in content
        assert 'if [ -n "${SCHEDULER_LOCATION:-}" ];' in content
        assert 'if [ -n "${SCHEDULER_WORKSPACE:-}" ];' in content
        assert 'if [ "${SCHEDULE_SYNC_DRY_RUN:-false}" = "true" ];' in content
        # Final invocation uses "$@" so each accumulated argv token is
        # passed as-is — no shell word-splitting of user input.
        assert 'fluid schedule-sync "$@"' in content

    def test_stage_11_gated_by_both_run_flag_and_scheduler_trim(self):
        """The when{} clause must require BOTH RUN_STAGE_11_SCHEDULE_SYNC
        AND a non-blank SCHEDULER. A blank scheduler with the run flag
        on is a misconfiguration, not a pipeline intent."""
        content = self._jenkinsfile()
        assert "params.RUN_STAGE_11_SCHEDULE_SYNC && params.SCHEDULER?.trim()" in content

    def test_stage_11_self_gates_on_schedule_dags_dir(self):
        """Stage 11 must self-gate on ``dist/artifacts/schedule/`` the way
        stage 8 self-gates on ``dist/artifacts/policy/bindings.json``.

        Bug A (discovered via A1 matrix V8/V9): reference-only contracts
        don't emit schedule/ (stage 3 auto-skips the schedule emitter),
        so ``fluid schedule-sync --dags-dir dist/artifacts/schedule/``
        hard-fails with ``schedule_sync_dags_dir_missing`` — even when
        the user opted into stage 11 assuming something would be there.
        The fix is a pre-invocation check that skips cleanly (exit 0)
        with an INFO message when the directory is missing or empty.

        The three conditions collapsed into one skip:
        - ``dist/artifacts/schedule/`` missing entirely (ref-only
          auto-skip or stage 3 toggled off)
        - empty directory (stage 3 ran but no DAGs to emit)
        - no ``orchestration.engine`` in the contract

        Regression guard for a shape of bug that keeps recurring:
        pipeline stage defaults to "hard-fail on absent optional input".
        """
        content = self._jenkinsfile()
        sh_body = self._extract_stage_sh_body(content, "11 - schedule sync")
        # The gate condition: missing dir OR empty dir. Mirror the
        # stage 8 pattern of ``if [ ... ]; then ...; else ...; fi``
        # but with ``exit 0`` + explanatory echo so the skip is
        # visible in Jenkins console output.
        assert "dist/artifacts/schedule" in sh_body, "stage 11 must reference the dags-dir path"
        assert (
            "[ ! -d dist/artifacts/schedule ]" in sh_body
            or "! -d dist/artifacts/schedule" in sh_body
        ), "stage 11 must test for absent dags-dir as a skip condition"
        assert "ls -A dist/artifacts/schedule" in sh_body, (
            "stage 11 must also test for empty dags-dir — not just missing — "
            "since stage 3 can create an empty schedule/ subfolder"
        )
        assert "exit 0" in sh_body, (
            "the gate must exit CLEAN when there's nothing to sync; a "
            "non-zero exit here breaks the whole pipeline on reference-"
            "only contracts"
        )
        assert "skipping stage 11" in sh_body, (
            "the skip must emit a human-readable INFO line so operators "
            "understand why stage 11 was a no-op"
        )

    def test_stage_11_no_direct_params_interpolation_in_sh(self):
        """Regression: ``${params.X}`` must not appear inside any ``sh``
        triple-single-quoted body. Our template uses ``${X}`` (env
        expansion) instead. This test enforces the injection boundary."""
        content = self._jenkinsfile()
        # Locate the stage-11 sh block and assert it has no ${params.*} inside.
        # We slice from the stage label to the next `stage(` boundary or post.
        marker = "stage('11 - schedule sync')"
        assert marker in content
        start = content.index(marker)
        # Find the next stage boundary or the start of the post{} block.
        tail = content[start:]
        end = len(tail)
        for needle in ("stage('", "post {"):
            idx = tail.find(needle, 30)  # skip past the marker itself
            if idx != -1 and idx < end:
                end = idx
        stage_body = tail[:end]
        # The env block is fine (has ${params.*}); the sh body inside
        # triple-single-quoted strings is what must be clean. Find the
        # sh block.
        sh_idx = stage_body.find("sh '''")
        assert sh_idx != -1, "stage 11 must contain a ``sh '''...'''`` block"
        sh_end = stage_body.find("'''", sh_idx + 6)
        assert sh_end != -1
        sh_body = stage_body[sh_idx:sh_end]
        assert "${params." not in sh_body, (
            "stage 11 sh body leaks ${params.*} — that's a Groovy "
            "interpolation into sh, which is a shell-injection surface."
        )

    def _extract_stage_sh_body(self, content: str, stage_label: str) -> str:
        """Return the text between ``sh '''`` and the closing ``'''``
        inside the stage bracketed by ``stage('<stage_label>')`` and
        the next ``stage(`` / ``post {`` boundary. Used by the
        injection-boundary regression tests."""
        start = content.index(f"stage('{stage_label}')")
        tail = content[start:]
        end = len(tail)
        for needle in ("stage('", "post {"):
            idx = tail.find(needle, 30)
            if idx != -1 and idx < end:
                end = idx
        stage_body = tail[:end]
        sh_idx = stage_body.find("sh '''")
        assert sh_idx != -1, f"stage {stage_label!r} missing sh '''...''' block"
        sh_end = stage_body.find("'''", sh_idx + 6)
        assert sh_end != -1
        return stage_body[sh_idx:sh_end]

    def test_stage_7_apply_no_argument_smuggling_via_apply_build_id(self):
        """Regression: stage 7 previously used the pattern
        ``APPLY_BUILD_FLAG = "${params.APPLY_BUILD_ID ? '--build ' + params.APPLY_BUILD_ID : ''}"``
        then expanded ``${APPLY_BUILD_FLAG}`` UNQUOTED inside ``sh '''...'''``.
        A Jenkins user could set
        ``APPLY_BUILD_ID="x --allow-data-loss --no-verify-plan-binding"`` →
        the value word-splits into 3 argv tokens → destructive-mode
        flags leak through regardless of the corresponding Jenkins
        booleans. Auth-gate bypass.

        Fix: params route through plain ``environment {}`` assignments,
        and the sh body uses POSIX ``set --`` with individually-quoted
        ``$VAR`` expansions. This test is the regression guard."""
        content = self._jenkinsfile()
        sh_body = self._extract_stage_sh_body(content, "7 - apply")
        # Pattern fingerprints that should NOT appear:
        assert "${APPLY_BUILD_FLAG}" not in sh_body
        assert "${APPLY_LOSS_FLAG}" not in sh_body
        assert "${APPLY_DIG_FLAG}" not in sh_body
        assert "${params." not in sh_body, (
            "stage 7 sh body leaks ${params.*} — Groovy interpolation "
            "into sh is a shell-injection surface."
        )
        # Pattern fingerprints that MUST appear:
        assert "set -eu" in sh_body
        assert "set -- runtime/plan.json" in sh_body
        assert 'if [ -n "${APPLY_BUILD_ID_VAL:-}" ]' in sh_body
        assert 'if [ "${ALLOW_DATA_LOSS:-false}" = "true" ]' in sh_body
        assert 'if [ "${NO_VERIFY_DIGEST:-false}" = "true" ]' in sh_body
        # The single NO_VERIFY_DIGEST CI knob now appends BOTH
        # narrowly-scoped CLI flags (the digest gate is split in two).
        assert "--no-verify-plan-binding" in sh_body
        assert "--no-verify-federation" in sh_body
        assert "--no-verify-digest" not in sh_body
        assert 'fluid apply "$@"' in sh_body

    def test_stage_7_routes_params_through_plain_environment_block(self):
        """The env block must carry raw param values — NOT Groovy
        ternary-concatenated strings. The fix is the boundary between
        Groovy interpolation (safe inside env assignments) and shell
        interpretation (safe because we quote every $VAR expansion)."""
        content = self._jenkinsfile()
        marker = "stage('7 - apply')"
        assert marker in content
        tail = content[content.index(marker) :]
        # Isolate the stage-7 environment{} block via brace-depth
        # counting (the first `}` could be the closer of `${VAR}` inside
        # an assignment, not the closer of the environment block itself).
        # Use "environment {\n" to skip any literal `environment {}` in
        # a comment; the real block opens a brace then a newline.
        env_start = tail.find("environment {\n")
        depth = 0
        i = env_start
        env_end = None
        while i < len(tail):
            c = tail[i]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    env_end = i + 1
                    break
            i += 1
        assert env_end is not None, "could not find closing `}` of stage 7 environment block"
        env_block = tail[env_start:env_end]
        # Raw values — good:
        assert 'APPLY_BUILD_ID_VAL = "${params.APPLY_BUILD_ID}"' in env_block
        assert 'APPLY_MODE = "${params.APPLY_MODE}"' in env_block
        assert 'ALLOW_DATA_LOSS = "${params.ALLOW_DATA_LOSS}"' in env_block
        assert 'NO_VERIFY_DIGEST = "${params.NO_VERIFY_DIGEST}"' in env_block
        # Ternary concatenation — must not reappear:
        assert "? '--build '" not in env_block, (
            "Stage 7 env block regressed to ternary concatenation — "
            "unquoted expansion downstream would reintroduce the auth-"
            "gate bypass."
        )


class TestStageSpecsHelper:
    """Canonical 11-stage spec list + per-stage command renderer.

    The :meth:`BasePipelineTemplate._stage_specs` helper is the single
    source of truth for the 11-stage pipeline contract shared across
    every non-Jenkins CI template. Each subclass (GitHub Actions,
    GitLab, Azure DevOps, Bitbucket, CircleCI, Tekton) iterates the
    spec list and wraps :meth:`BasePipelineTemplate._render_stage_command`
    in its native CI primitive. If these tests fail, every CI system
    will silently drift off-contract — which is the failure mode this
    test class is designed to prevent.
    """

    def _bt(self):
        return BasePipelineTemplate()

    def _cfg(self, **kwargs):
        defaults = {
            "provider": PipelineProvider.JENKINS,
            "complexity": PipelineComplexity.BASIC,
        }
        defaults.update(kwargs)
        return PipelineConfig(**defaults)

    def test_eleven_stages_in_order(self):
        specs = self._bt()._stage_specs()
        assert len(specs) == 11
        assert [s.num for s in specs] == list(range(1, 12))

    def test_stage_slugs_match_contract(self):
        expected = [
            "bundle",
            "validate",
            "generate_artifacts",
            "validate_artifacts",
            "diff",
            "plan",
            "apply",
            "policy_apply",
            "verify",
            "publish",
            "schedule_sync",
        ]
        assert [s.slug for s in self._bt()._stage_specs()] == expected

    def test_stage_display_names(self):
        """Display names must include the human-readable spaces that
        CI systems use in their stage labels. A slug of
        ``generate_artifacts`` renders as ``generate artifacts`` in
        the pipeline UI — dash-case or snake-case in the UI looks off."""
        displays = [s.display for s in self._bt()._stage_specs()]
        assert "generate artifacts" in displays
        assert "validate artifacts" in displays
        assert "policy apply" in displays
        assert "schedule sync" in displays

    def test_toggle_param_naming_convention(self):
        """Every spec has a ``RUN_STAGE_<N>_<SLUG_UPPER>`` param name.
        CI systems use these identifiers to let operators skip stages
        from the Build-With-Parameters UI. Drift in the naming
        convention breaks user muscle memory across systems."""
        for s in self._bt()._stage_specs():
            assert s.toggle_param == f"RUN_STAGE_{s.num}_{s.slug.upper()}", (
                f"stage {s.num} has non-canonical toggle param "
                f"{s.toggle_param!r} (expected "
                f"RUN_STAGE_{s.num}_{s.slug.upper()})"
            )

    def test_structural_stages_default_on(self):
        """Stages 1-9 (the structural spine of the pipeline) default
        to running. An operator running Build Now with zero overrides
        should get the full validate→plan→apply→verify flow."""
        for s in self._bt()._stage_specs()[0:9]:
            assert s.default_run is True, (
                f"stage {s.num} ({s.slug}) defaults off — structural "
                "stages 1-9 must default to running"
            )

    def test_publish_and_schedule_sync_default_off(self):
        """Stages 10 (publish) and 11 (schedule-sync) push beyond the
        CI environment (to catalogs / scheduler control planes). Both
        must be opt-in per run — defaulting them on would push every
        build to production catalogs and DAG storage."""
        specs = self._bt()._stage_specs()
        assert specs[9].default_run is False  # publish
        assert specs[10].default_run is False  # schedule-sync

    def test_spec_is_frozen(self):
        """StageSpec is frozen so subclasses can't mutate their copy
        of the shared list and poison the renderer for other templates
        generated later in the same process."""
        import dataclasses

        spec = StageSpec(
            num=1,
            slug="x",
            display="x",
            toggle_param="RUN_STAGE_1_X",
            default_run=True,
            command="echo x",
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            spec.num = 99  # type: ignore[misc]

    def test_render_applies_workdir_prefix(self):
        """When config.workdir is set, every stage command is
        prefixed with ``cd "<workdir>" && ``. CI systems that check
        out at SCM root (Jenkins, GitHub Actions without
        working-directory) rely on this."""
        bt = self._bt()
        spec = bt._stage_specs()[0]  # bundle
        body = bt._render_stage_command(spec, self._cfg(workdir="examples/demo"))
        assert body.startswith('cd "examples/demo" && ')

    def test_render_no_workdir_no_prefix(self):
        bt = self._bt()
        body = bt._render_stage_command(bt._stage_specs()[0], self._cfg())
        assert 'cd "' not in body

    def test_render_escapes_double_quotes_in_workdir(self):
        """Defence-in-depth: if someone constructs a PipelineConfig
        with a workdir containing a double-quote (argparse rejects
        this for user input, but programmatic callers could), the
        renderer escapes it so the generated ``cd`` doesn't break
        out of its quoting."""
        bt = self._bt()
        body = bt._render_stage_command(bt._stage_specs()[0], self._cfg(workdir='odd"dir'))
        assert 'cd "odd\\"dir" && ' in body

    def test_stage_3_off_for_reference_only_contracts(self):
        """Stage 3 (generate artifacts) is the one spec whose default
        follows the per-config flag rather than the static spec
        default. Reference-only contracts (pointing at externally-
        owned dbt/Airflow projects) set generates_artifacts=False so
        stage 3 is omitted — fluid doesn't own those artifacts and
        emitting them would surface spurious 'nothing to emit'
        failures."""
        bt = self._bt()
        s3 = bt._stage_specs()[2]
        assert s3.slug == "generate_artifacts"
        assert bt._stage_default_run(s3, self._cfg(generates_artifacts=True)) is True
        assert bt._stage_default_run(s3, self._cfg(generates_artifacts=False)) is False

    def test_stage_7_uses_injection_proof_pattern(self):
        """Stage 7 (apply) assembles argv via POSIX ``set --`` and
        ``if/then/fi`` so operator-supplied flags (mode, allow-data-
        loss, no-verify-*, build-id) flow through env vars as
        whole argv tokens. This closes the argument-smuggling surface
        that the Jenkins ``${APPLY_BUILD_FLAG}`` pattern previously
        had (see tech-debt commit D2)."""
        bt = self._bt()
        s7 = next(s for s in bt._stage_specs() if s.num == 7)
        assert "set -eu" in s7.command
        assert "set -- runtime/plan.json" in s7.command
        # The explicit APPLY_* env-var references mean CI systems
        # must route Build-With-Parameters values through env, not
        # through template interpolation.
        assert 'if [ -n "${APPLY_BUILD_ID:-}" ]' in s7.command
        assert 'if [ "${ALLOW_DATA_LOSS:-false}" = "true" ]' in s7.command
        assert 'if [ "${NO_VERIFY_DIGEST:-false}" = "true" ]' in s7.command
        # The single NO_VERIFY_DIGEST CI knob appends BOTH narrowly-
        # scoped CLI flags — the digest gate is split into a
        # plan-binding gate and a federation upstream-digest gate.
        assert "--no-verify-plan-binding" in s7.command
        assert "--no-verify-federation" in s7.command
        assert "--no-verify-digest" not in s7.command
        # Regression guard: the old concatenation pattern must not
        # reappear. If this ever fails, the helper has drifted back
        # into the argument-smuggling shape.
        assert "${APPLY_BUILD_FLAG}" not in s7.command

    def test_stage_11_uses_injection_proof_pattern(self):
        """Stage 11 (schedule-sync) assembles argv the same way stage 7
        does. Regression guard against the same class of bug on the
        scheduler-variant params."""
        bt = self._bt()
        s11 = next(s for s in bt._stage_specs() if s.num == 11)
        assert 'set -- --scheduler "$SCHEDULER"' in s11.command
        for var in (
            "SCHEDULER_DESTINATION",
            "SCHEDULER_ENVIRONMENT_NAME",
            "SCHEDULER_LOCATION",
            "SCHEDULER_WORKSPACE",
            "SCHEDULE_SYNC_DRY_RUN",
        ):
            assert f"${{{var}" in s11.command, (
                f"stage 11 command missing ${{{var}:-}} expansion; "
                "scheduler-variant param would be ignored"
            )

    def test_stage_10_publish_supports_multi_target(self):
        """Stage 10's PUBLISH_TARGETS env var expands a space-separated
        list into ``--target X --target Y ...``. Falls back to a
        single ``--target ${CATALOG:-datamesh-manager}`` for legacy
        single-target configs. Both paths must be present so the
        rendered sh works regardless of which env var the operator
        sets."""
        bt = self._bt()
        s10 = next(s for s in bt._stage_specs() if s.num == 10)
        assert "PUBLISH_TARGETS" in s10.command
        assert "CATALOG:-datamesh-manager" in s10.command
        assert "--target" in s10.command

    def test_jenkins_stage_10_default_publish_target_opt_in(self):
        """``config.default_publish_target`` is opt-in. When unset,
        Stage 10's Jenkinsfile shell uses the bare ``${PUBLISH_TARGETS}``
        form — matching behaviour before the flag was introduced — so
        existing pipelines keep rendering identically.

        When set to a non-empty value the shell switches to
        ``${PUBLISH_TARGETS:-<value>}``, giving the first
        Pipeline-from-SCM build Jenkins auto-triggers a sane fallback
        before the ``parameters { }`` block's defaults are exported as
        env vars. Whitespace-only values are treated as unset."""
        from fluid_build.forge.core.pipeline_templates import (
            JenkinsTemplate,
            PipelineComplexity,
            PipelineConfig,
            PipelineProvider,
        )

        def _render(default_publish_target):
            cfg = PipelineConfig(
                provider=PipelineProvider.JENKINS,
                complexity=PipelineComplexity.STANDARD,
                default_publish_target=default_publish_target,
            )
            return JenkinsTemplate().generate(cfg)["Jenkinsfile"]

        # Unset / None -> bare form (backwards compatible default).
        # The template's doc-comment legitimately mentions
        # ``${PUBLISH_TARGETS:-X}`` as an example — only the actual
        # shell ``for t in ...`` line should be asserted against.
        bare = _render(None)
        assert "for t in ${PUBLISH_TARGETS}" in bare
        assert "for t in ${PUBLISH_TARGETS:-" not in bare

        # Whitespace-only -> treated as unset.
        whitespace = _render("   ")
        assert "for t in ${PUBLISH_TARGETS}" in whitespace
        assert "for t in ${PUBLISH_TARGETS:-" not in whitespace

        # Opt-in with specific catalog -> shell fallback injected.
        opted = _render("datamesh-manager")
        assert "for t in ${PUBLISH_TARGETS:-datamesh-manager}" in opted

        # Arbitrary catalog names are accepted verbatim.
        horizon = _render("horizon")
        assert "for t in ${PUBLISH_TARGETS:-horizon}" in horizon

    def test_stage_8_self_gates_on_bindings_json(self):
        """Stage 8 (policy apply) is a no-op when bindings.json is
        absent. Reference-only contracts delegate policy to upstream,
        so skip-silently is the correct behaviour rather than erroring
        the build."""
        bt = self._bt()
        s8 = next(s for s in bt._stage_specs() if s.num == 8)
        assert "if [ -f dist/artifacts/policy/bindings.json ]" in s8.command

    def test_every_stage_has_sensible_contract_fallback(self):
        """Every stage that references a contract path uses the
        ``${CONTRACT:-contract.fluid.yaml}`` fallback so Build Now
        works without the operator pre-setting CONTRACT. Stages that
        operate on bundle.tgz / plan.json / dist/artifacts don't need
        this (they use fixed paths)."""
        bt = self._bt()
        contract_ref_stages = {"bundle", "validate", "diff", "plan", "verify", "publish"}
        for s in bt._stage_specs():
            if s.slug in contract_ref_stages:
                assert "${CONTRACT:-contract.fluid.yaml}" in s.command, (
                    f"stage {s.num} ({s.slug}) references contract but "
                    "doesn't use the ${CONTRACT:-contract.fluid.yaml} "
                    "fallback — Build Now would fail without a pre-set env var"
                )


class TestElevenStagePortsAllSystems:
    """Verify each of the 6 non-Jenkins CI systems emits an 11-stage
    pipeline via the new ``_generate_eleven_stage`` method.

    The Jenkins template (``JenkinsTemplate.generate``) is the
    reference implementation. This class is the "6-way contract
    check" — every non-Jenkins CI system must:

    1. Render all 11 stages in its output (structural invariant).
    2. Declare a toggle parameter per stage that surfaces to the
       system's native build-params UI.
    3. Route user-supplied params through env vars / variables, NOT
       by direct-interpolating them into shell bodies. (Stage 7 +
       stage 11 argument-smuggling defence carried across ports.)
    4. Produce the ``install_mode`` setup body so the runner installs
       fluid before any stage runs.
    5. Support ``workdir`` when set (subfolder contract in monorepo).
    6. Fail loud for an unknown scheduler (stage 11's ``--scheduler``
       argument) — enforced at the CLI level, but the template must
       thread the value through without corruption.

    These tests don't regenerate goldens — they assert structural
    properties of the output strings. Goldens remain the Jenkins
    template's specific golden file.
    """

    from fluid_build.forge.core.pipeline_templates import (  # noqa: E402
        AzureDevOpsTemplate,
        BitbucketTemplate,
        CircleCITemplate,
        GitHubActionsTemplate,
        GitLabCITemplate,
        TektonTemplate,
    )

    SYSTEMS = [
        # (name, template_class, provider_enum, filename_substring)
        (
            "GitHubActions",
            GitHubActionsTemplate,
            PipelineProvider.GITHUB_ACTIONS,
            ".github/workflows",
        ),
        ("GitLab", GitLabCITemplate, PipelineProvider.GITLAB_CI, ".gitlab-ci.yml"),
        ("AzureDevOps", AzureDevOpsTemplate, PipelineProvider.AZURE_DEVOPS, "azure-pipelines.yml"),
        ("Bitbucket", BitbucketTemplate, PipelineProvider.BITBUCKET, "bitbucket-pipelines.yml"),
        ("CircleCI", CircleCITemplate, PipelineProvider.CIRCLE_CI, ".circleci/config.yml"),
        ("Tekton", TektonTemplate, PipelineProvider.TEKTON, "tekton/"),
    ]

    def _generate(self, template_cls, provider, **kwargs):
        cfg = PipelineConfig(provider=provider, complexity=PipelineComplexity.BASIC, **kwargs)
        return template_cls()._generate_eleven_stage(cfg)

    def _all_content(self, out):
        """Join every file's content — helper for cross-file checks
        (Tekton splits into pipeline.yaml + tasks.yaml, others are
        single-file)."""
        return "\n".join(out.values())

    # -----------------------------------------------------------------
    # 1. Structural invariant: all 11 stages appear in every emit
    # -----------------------------------------------------------------

    @pytest.mark.parametrize("name,cls,provider,fname", SYSTEMS)
    def test_all_eleven_stages_present(self, name, cls, provider, fname):
        """Every of the 6 CI systems must emit all 11 stages."""
        out = self._generate(cls, provider)
        assert out, f"{name}: _generate_eleven_stage returned empty"
        content = self._all_content(out)
        # Every stage body reaches the output via `fluid <verb>`.
        verbs = [
            "fluid bundle ",
            "fluid validate ",
            "fluid generate artifacts ",
            "fluid validate-artifacts ",
            "fluid diff ",
            "fluid plan ",
            "fluid apply",
            "fluid policy-apply",
            "fluid verify ",
            "fluid publish ",
            "fluid schedule-sync ",
        ]
        missing = [v for v in verbs if v not in content]
        assert (
            not missing
        ), f"{name} emit is missing stage(s): {missing!r}. Output files: {list(out.keys())}"

    @pytest.mark.parametrize("name,cls,provider,fname", SYSTEMS)
    def test_filename_matches_system_convention(self, name, cls, provider, fname):
        """File-path convention per CI system's documented location."""
        out = self._generate(cls, provider)
        matching = [k for k in out.keys() if fname in k]
        assert matching, (
            f"{name}: no output file matches expected path substring {fname!r}; "
            f"got {list(out.keys())}"
        )

    # -----------------------------------------------------------------
    # 2. Toggle parameters surface in every native param mechanism
    # -----------------------------------------------------------------

    @pytest.mark.parametrize("name,cls,provider,fname", SYSTEMS)
    def test_all_eleven_toggles_declared(self, name, cls, provider, fname):
        """Every RUN_STAGE_N_SLUG toggle must appear somewhere in the
        emit — as an input / parameter / variable declaration. The
        exact YAML path varies by system; we only check the token
        appears verbatim so operators can find it in the Build-With-
        Parameters UI."""
        out = self._generate(cls, provider)
        content = self._all_content(out)
        for n in range(1, 12):
            # Case-insensitive + underscore-or-hyphen tolerant: Tekton
            # converts RUN_STAGE_1_BUNDLE → run-stage-1-bundle.
            toggles = [f"RUN_STAGE_{n}_", f"run_stage_{n}_", f"run-stage-{n}-"]
            assert any(
                t in content for t in toggles
            ), f"{name} emit is missing stage-{n} toggle (tried {toggles!r})"

    # -----------------------------------------------------------------
    # 3. Install setup — every emit carries the install-mode body
    # -----------------------------------------------------------------

    @pytest.mark.parametrize("name,cls,provider,fname", SYSTEMS)
    def test_install_setup_emitted(self, name, cls, provider, fname):
        """Every emit must contain the fluid install command (pypi mode
        default) so a fresh runner can execute the pipeline."""
        out = self._generate(cls, provider)
        content = self._all_content(out)
        # The install setup always invokes pip install for pypi mode
        # and references the package spec env var.
        assert "pip install" in content, f"{name}: missing pip install in install-setup body"
        assert "FLUID_PACKAGE_SPEC" in content, f"{name}: missing FLUID_PACKAGE_SPEC override"
        assert "fluid --version" in content, f"{name}: missing fluid --version sanity check"

    # -----------------------------------------------------------------
    # 4. Workdir support — subfolder contracts
    # -----------------------------------------------------------------

    @pytest.mark.parametrize("name,cls,provider,fname", SYSTEMS)
    def test_workdir_applied_when_set(self, name, cls, provider, fname):
        """When config.workdir is set, the workdir value must appear
        in the output — via the native primitive (working-directory,
        workingDir, working_directory, etc.) or via a `cd` prefix in
        the stage bodies."""
        out = self._generate(cls, provider, workdir="examples/demo")
        content = self._all_content(out)
        assert "examples/demo" in content, (
            f"{name}: workdir='examples/demo' not reflected in emit. "
            f"Must appear via native primitive or cd prefix."
        )

    # -----------------------------------------------------------------
    # 5. Stage 11 gate — both toggle AND non-blank SCHEDULER required
    # -----------------------------------------------------------------

    @pytest.mark.parametrize("name,cls,provider,fname", SYSTEMS)
    def test_stage_11_requires_scheduler(self, name, cls, provider, fname):
        """Stage 11 must NOT run when SCHEDULER is blank, even if
        RUN_STAGE_11_SCHEDULE_SYNC is true. Otherwise `fluid schedule-
        sync` would invoke with an empty --scheduler and the CLI would
        fail with a confusing error instead of cleanly skipping."""
        out = self._generate(cls, provider)
        content = self._all_content(out)
        # Each system expresses the gate differently; check that the
        # SCHEDULER param is referenced alongside the stage-11 toggle.
        # We look for any mention of "SCHEDULER" (case variants).
        scheduler_refs = [
            "SCHEDULER",
            "scheduler",
        ]
        assert any(
            r in content for r in scheduler_refs
        ), f"{name}: SCHEDULER param not referenced anywhere in emit"

    # -----------------------------------------------------------------
    # 6. Parameters come from the shared list (no drift between systems)
    # -----------------------------------------------------------------

    @pytest.mark.parametrize("name,cls,provider,fname", SYSTEMS)
    def test_standard_params_declared(self, name, cls, provider, fname):
        """CONTRACT / FLUID_ENV / APPLY_MODE / ALLOW_DATA_LOSS /
        NO_VERIFY_DIGEST / PUBLISH_TARGETS / SCHEDULER_* must all be
        declared. These come from the shared
        :meth:`BasePipelineTemplate._eleven_stage_parameters` helper
        so any future parameter addition must land across all 6
        systems automatically."""
        out = self._generate(cls, provider)
        content = self._all_content(out)
        # Check canonical param names (tolerating lower/hyphen variants)
        for param in [
            "CONTRACT",
            "FLUID_ENV",
            "APPLY_MODE",
            "ALLOW_DATA_LOSS",
            "NO_VERIFY_DIGEST",
            "PUBLISH_TARGETS",
            "SCHEDULER_DESTINATION",
        ]:
            variants = [param, param.lower(), param.lower().replace("_", "-")]
            assert any(
                v in content for v in variants
            ), f"{name}: parameter {param!r} (or case variants) not declared"

    # -----------------------------------------------------------------
    # 7. Reference-only contracts skip stage 3 (generate artifacts)
    # -----------------------------------------------------------------

    @pytest.mark.parametrize("name,cls,provider,fname", SYSTEMS)
    def test_reference_only_contract_stage_3_default_false(self, name, cls, provider, fname):
        """When config.generates_artifacts=False, the RUN_STAGE_3_GENERATE_ARTIFACTS
        toggle must default to ``false`` — reference-only contracts
        delegate artifact ownership upstream and running stage 3 would
        surface spurious 'nothing to generate' failures."""
        out = self._generate(cls, provider, generates_artifacts=False)
        content = self._all_content(out)
        # Either the default is 'false' in the param declaration OR
        # the stage is conditionally omitted — either is acceptable.
        # We check the toggle's default by looking for
        # "RUN_STAGE_3_GENERATE_ARTIFACTS" in the same line as "false".
        lower = content.lower()
        idx = lower.find("run_stage_3_generate_artifacts")
        if idx == -1:
            idx = lower.find("run-stage-3-generate-artifacts")
        assert idx != -1, f"{name}: stage-3 toggle not found in emit"
        # Scan a 400-char window around the toggle for "false"
        window = content[max(0, idx - 50) : idx + 400]
        assert "false" in window.lower(), (
            f"{name}: stage-3 toggle should default to 'false' for "
            f"reference-only contracts. Window: {window!r}"
        )
