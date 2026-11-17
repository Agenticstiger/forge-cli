# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Branch-coverage tests for fluid_build.forge.core.pipeline_templates"""

import re

import pytest

from fluid_build import __version__
from fluid_build.forge.core.pipeline_templates import (
    AzureDevOpsTemplate,
    BasePipelineTemplate,
    BitbucketTemplate,
    CircleCITemplate,
    GitHubActionsTemplate,
    GitLabCITemplate,
    PipelineConfig,
    PipelineProvider,
    PipelineTemplateGenerator,
    TektonTemplate,
    generate_pipeline_template,
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


# ── PipelineConfig tests ────────────────────────────────────────────


class TestPipelineConfig:
    def test_generation_defaults(self):
        cfg = PipelineConfig(provider=PipelineProvider.JENKINS)
        assert cfg.verify_strict_default is True
        assert cfg.publish_stage_default is False
        assert cfg.schedule_sync_default is False
        assert cfg.default_branch == "main"
        assert cfg.add_ons == ()
        assert cfg.cloud_auth == ()

    def test_the_tier_fields_are_gone(self):
        """One pipeline shape: complexity, environments and the per-tier switches
        no longer exist, so a caller that still passes one fails loudly."""
        for stale in ("complexity", "environments", "enable_approvals", "publish_include_env"):
            with pytest.raises(TypeError):
                PipelineConfig(provider=PipelineProvider.JENKINS, **{stale: "x"})

    def test_workdir_default_is_none(self):
        assert PipelineConfig(provider=PipelineProvider.JENKINS).workdir is None

    def test_workdir_explicit_value_preserved(self):
        cfg = PipelineConfig(provider=PipelineProvider.JENKINS, workdir="examples/demo")
        assert cfg.workdir == "examples/demo"

    def test_generates_artifacts_default_true(self):
        assert PipelineConfig(provider=PipelineProvider.JENKINS).generates_artifacts is True

    def test_generates_artifacts_explicit_false(self):
        cfg = PipelineConfig(provider=PipelineProvider.JENKINS, generates_artifacts=False)
        assert cfg.generates_artifacts is False


# ── BasePipelineTemplate tests ──────────────────────────────────────


class TestBasePipelineTemplate:
    def test_init_defaults(self):
        t = BasePipelineTemplate()
        assert t.provider_name == "unknown"
        assert t.file_extensions == [".yml"]

    def test_generate_raises_not_implemented(self):
        t = BasePipelineTemplate()
        config = PipelineConfig(provider=PipelineProvider.JENKINS)
        with pytest.raises(NotImplementedError):
            t.generate(config)

    def test_the_per_stage_command_table_is_gone(self):
        """Ship is the only implementation of the stages: a template holds no
        shell body of its own to drift from it."""
        t = BasePipelineTemplate()
        for retired in ("_get_fluid_commands", "_stage_specs", "get_features"):
            assert not hasattr(t, retired), retired


# ── PipelineTemplateGenerator tests ─────────────────────────────────


class TestPipelineTemplateGenerator:
    def test_init_populates_templates(self):
        gen = PipelineTemplateGenerator()
        for provider in PipelineProvider:
            assert provider in gen.templates

    def test_list_available_providers(self):
        gen = PipelineTemplateGenerator()
        providers = gen.list_available_providers()
        assert "github_actions" in providers
        assert "jenkins" in providers

    def test_generate_unsupported_provider(self):
        gen = PipelineTemplateGenerator()
        # Remove a provider to trigger error
        del gen.templates[PipelineProvider.TEKTON]
        config = PipelineConfig(provider=PipelineProvider.TEKTON)
        with pytest.raises(ValueError, match="Unsupported provider"):
            gen.generate_pipeline(config)


# ── GitHubActionsTemplate generate branches ─────────────────────────


class TestGitHubActionsGenerate:
    def test_init(self):
        t = GitHubActionsTemplate()
        assert t.provider_name == "GitHub Actions"
        assert ".yml" in t.file_extensions

    def test_the_workflow_is_one_file_named_fluid_pipeline(self):
        result = GitHubActionsTemplate().generate(
            PipelineConfig(provider=PipelineProvider.GITHUB_ACTIONS)
        )
        assert list(result) == [".github/workflows/fluid-pipeline.yml"]

    @pytest.mark.parametrize(
        ("add_on", "path"),
        [
            ("security", ".github/workflows/fluid-security.yml"),
            ("compliance", ".github/workflows/fluid-compliance.yml"),
            ("sbom", ".github/workflows/fluid-sbom.yml"),
        ],
    )
    def test_each_add_on_adds_its_own_workflow(self, add_on, path):
        result = GitHubActionsTemplate().generate(
            PipelineConfig(provider=PipelineProvider.GITHUB_ACTIONS, add_ons=(add_on,))
        )
        assert set(result) == {".github/workflows/fluid-pipeline.yml", path}


# ── Generate pipeline through generator ─────────────────────────────


class TestGeneratePipeline:
    @pytest.mark.parametrize("provider", list(PipelineProvider))
    def test_all_providers(self, provider):
        gen = PipelineTemplateGenerator()
        result = gen.generate_pipeline(PipelineConfig(provider=provider))
        assert isinstance(result, dict)
        assert len(result) > 0

    @pytest.mark.parametrize("provider", list(PipelineProvider))
    def test_all_providers_with_every_add_on(self, provider):
        gen = PipelineTemplateGenerator()
        plain = gen.generate_pipeline(PipelineConfig(provider=provider))
        full = gen.generate_pipeline(
            PipelineConfig(provider=provider, add_ons=("security", "compliance", "sbom"))
        )
        assert "".join(full.values()) != "".join(plain.values())

    def test_the_convenience_wrapper_takes_cloud_auth_and_add_ons(self):
        files = generate_pipeline_template("github_actions", ("aws",), ("sbom",))
        assert ".github/workflows/fluid-sbom.yml" in files
        assert "id-token: write" in files[".github/workflows/fluid-pipeline.yml"]

    def test_the_convenience_wrapper_refuses_an_unknown_provider(self):
        with pytest.raises(ValueError, match="Invalid parameter"):
            generate_pipeline_template("travis")


# ── S-005: GitLab CI OIDC credential hygiene ────────────────────────


class TestGitlabOidcCredentialHygiene:
    """SECURITY_REVIEW S-005: generated GitLab CI templates must never
    write federated credentials to a predictable ``/tmp/*.json`` path.

    The ID tokens go to files in a private ``mktemp -d`` directory (mode 700
    by ``umask 077``), removed by a ``trap`` when the job's shell exits, so
    the material exists on disk only for the lifetime of the job.
    """

    def _generate_gitlab_yaml(self, cloud_auth):
        gen = PipelineTemplateGenerator()
        config = PipelineConfig(provider=PipelineProvider.GITLAB_CI, cloud_auth=cloud_auth)
        result = gen.generate_pipeline(config)
        assert ".gitlab-ci.yml" in result
        return result[".gitlab-ci.yml"]

    @pytest.mark.parametrize("cloud_auth", [("gcp",), ("aws",), ("snowflake",), ("openbao",)])
    def test_no_literal_tmp_paths(self, cloud_auth):
        yaml_out = self._generate_gitlab_yaml(cloud_auth)
        assert "/tmp/" not in yaml_out
        assert "/tmp/oidc_token.json" not in yaml_out
        assert "/tmp/aws_creds.json" not in yaml_out

    @pytest.mark.parametrize("cloud_auth", [("gcp",), ("aws",), ("snowflake",), ("openbao",)])
    def test_uses_private_mktemp_dir_umask_and_trap(self, cloud_auth):
        yaml_out = self._generate_gitlab_yaml(cloud_auth)
        # mode-restricted files, in a random private directory
        assert "umask 077" in yaml_out
        assert 'FLUID_AUTH_DIR="$(mktemp -d)"' in yaml_out
        # cleanup on shell exit: the key-hygiene win
        assert "trap 'rm -rf \"$FLUID_AUTH_DIR\"' EXIT" in yaml_out

    def test_gcp_writes_an_external_account_file_naming_the_token_file(self):
        yaml_out = self._generate_gitlab_yaml(("gcp",))
        assert "external_account" in yaml_out
        assert 'export GOOGLE_APPLICATION_CREDENTIALS="$FLUID_AUTH_DIR/gcp-credentials.json"' in (
            yaml_out
        )
        # The token comes from GitLab's native id_tokens injection.
        assert '"$GCP_OIDC_TOKEN" > "$FLUID_AUTH_DIR/gcp-token"' in yaml_out

    def test_aws_hands_the_sdk_the_token_file_and_assumes_nothing_itself(self):
        yaml_out = self._generate_gitlab_yaml(("aws",))
        assert "AWS_WEB_IDENTITY_TOKEN_FILE" in yaml_out
        assert "sts.amazonaws.com" in yaml_out
        # The SDK assumes the role; no credential output is captured or printed.
        assert "aws sts assume-role-with-web-identity" not in yaml_out

    def test_no_oidc_no_cred_snippets(self):
        """Smoke test: with no cloud bound, none of the credential-file
        plumbing appears in the output."""
        yaml_out = self._generate_gitlab_yaml(cloud_auth=())
        assert "mktemp" not in yaml_out
        assert "id_tokens" not in yaml_out
        assert "OIDC_TOKEN" not in yaml_out


# ── Jenkins template hardening ──────────────────────────────────────


def _stage_text(content: str, label: str) -> str:
    """The text of the stage named ``label``, up to the next stage at any depth."""
    start = content.index(f"stage('{label}')")
    nxt = re.search(r"\n\s+stage\('", content[start + 10 :])
    return content[start : start + 10 + nxt.start() if nxt else content.index("    post {")]


class TestJenkinsTemplateHardening:
    """Jenkins template hardens against three independent regressions:

    1. **Subfolder checkouts.** Jenkins checks out at the SCM root; when
       ``fluid generate ci`` runs from a product subfolder, every ``sh`` step
       must be prefixed with ``cd "<workdir>" && ...``. Otherwise the fluid
       command runs in the wrong dir and every contract reference fails.

    2. **Reference-only contracts.** When ``generates_artifacts=False``, the
       ``Generate Artifacts`` stage defaults to off.

    3. **Missing-artifact tolerance.** Reference-only contracts don't produce
       ``plan.json``, so ``archiveArtifacts`` must tolerate empty matches.
    """

    def _jenkinsfile(self, **kwargs):
        cfg = PipelineConfig(provider=PipelineProvider.JENKINS, **kwargs)
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
        # CD prefix appears inside each sh block, which then runs under ``set -eu``.
        bodies = re.findall(r"sh '''(.*?)'''", content, re.S)
        fluid_bodies = [b for b in bodies if re.search(r"\bfluid\s", b)]
        assert len(fluid_bodies) >= 12  # stage 0's version check + stages 1-11
        for body in fluid_bodies:
            assert body.startswith('cd "examples/demo" && set -eu'), body[:80]
        for num in range(1, 12):
            assert f'fluid ship "$CONTRACT" --stage {num}' in content

    def test_workdir_prefixes_archive_patterns(self):
        content = self._jenkinsfile(workdir="examples/demo")
        # plan.json archive path must be workdir-prefixed or Jenkins can't
        # find it (archiveArtifacts is rooted at the SCM checkout root).
        assert "examples/demo/runtime/plan.json" in content

    # Regression 2: reference-only stage auto-defaulting ---------------
    # Stage 3 generate-artifacts is toggleable via RUN_STAGE_3_GENERATE_ARTIFACTS.
    # For reference-only contracts the default value flips to ``false``.

    def test_default_contains_generate_artifacts_stage(self):
        content = self._jenkinsfile()
        assert "stage('3 - generate artifacts')" in content
        assert "name: 'RUN_STAGE_3_GENERATE_ARTIFACTS', defaultValue: true" in content

    def test_generates_artifacts_false_flips_param_default(self):
        content = self._jenkinsfile(generates_artifacts=False)
        assert "stage('3 - generate artifacts')" in content
        assert "name: 'RUN_STAGE_3_GENERATE_ARTIFACTS', defaultValue: false" in content
        assert (
            "RUN_STAGE_3_GENERATE_ARTIFACTS = \"${params.RUN_STAGE_3_GENERATE_ARTIFACTS == null ? 'false'"
            in content
        )
        # Adjacent stages must still be present.
        assert "stage('2 - validate')" in content
        assert "stage('6 - plan')" in content

    # Regression 3: empty-archive/result tolerance --------------------

    def test_archiveartifacts_tolerates_empty(self):
        content = self._jenkinsfile()
        archive_lines = [line for line in content.splitlines() if "archiveArtifacts" in line]
        assert archive_lines, "expected at least one archiveArtifacts line"
        for line in archive_lines:
            assert (
                "allowEmptyArchive: true" in line
            ), f"archiveArtifacts without allowEmptyArchive breaks reference-only builds: {line}"

    # Other hardening fixes --------------------------------------------

    def test_fluid_env_flows_via_the_environment_block_not_an_inline_prefix(self):
        """FLUID_ENV reaches ship as an environment variable, with the parameter's
        default as the fallback when the build carries no parameters (webhook
        triggers, SCM polls); never as an inline ``FLUID_ENV=dev cmd`` prefix."""
        content = self._jenkinsfile()
        assert "sh 'FLUID_ENV=" not in content
        assert "FLUID_ENV = \"${params.FLUID_ENV == null ? 'dev' : params.FLUID_ENV}\"" in content

    def test_verify_strict_default_can_be_overridden(self):
        content = self._jenkinsfile(verify_strict_default=False)
        assert "name: 'VERIFY_STRICT', defaultValue: false" in content

    def test_publish_stage_default_can_be_overridden(self):
        content = self._jenkinsfile(publish_stage_default=True)
        assert "name: 'RUN_STAGE_10_PUBLISH', defaultValue: true" in content

    def test_doctor_not_extended(self):
        """``fluid doctor --extended`` requires scripts/diagnose.sh which
        forge-generated variants don't ship."""
        content = self._jenkinsfile()
        assert "fluid doctor --extended" not in content

    # ── install-mode dispatch (generation-time decision) ─────────────
    # pypi (default): a single pip install from PyPI; TestPyPI / a private
    # index is a BUILD-time choice through Jenkins parameters.
    # dev-source (lab only): bind-mount install, fails loud without the mount.
    # The mode is chosen ONCE at `fluid generate ci` time.

    def test_install_mode_default_is_pypi(self):
        content = self._jenkinsfile()
        assert "stage('0 — Bootstrap FLUID [pypi]')" in content
        # pypi mode's pip install sees FLUID_PACKAGE_SPEC with its default:
        # the forge-cli version that generated the file.
        assert f'-- "${{FLUID_PACKAGE_SPEC:-data-product-forge=={__version__}}}"' in content
        # dev-source branch must NOT appear in pypi mode — clean separation.
        assert "/forge-cli-src" not in content
        assert "name: 'FLUID_PIP_INDEX_URL'" in content
        assert "name: 'FLUID_PIP_EXTRA_INDEX_URL'" in content
        assert "name: 'FLUID_ALLOW_PRERELEASE'" in content
        assert "name: 'FLUID_PACKAGE_SPEC'" in content

    def test_install_mode_dev_source(self):
        content = self._jenkinsfile(install_mode="dev-source")
        assert "stage('0 — Bootstrap FLUID [dev-source]')" in content
        # Fail-loud check is required — no silent fallback to PyPI.
        assert "install-mode=dev-source but /forge-cli-src" in content
        assert "exit 2" in content
        # Must NOT uninstall data-product-forge: `fluid` is that package's
        # console_scripts entry point, so uninstalling deletes the command.
        assert "pip uninstall -y data-product-forge" not in content
        assert 'python -c "import fluid_build"' in content
        # PYTHONPATH at the pipeline environment level: every sh step inherits it.
        assert "PYTHONPATH = '/forge-cli-src'" in content
        # pypi branch must NOT appear — clean separation.
        assert "${FLUID_PIP_INDEX_URL" not in content
        assert "FLUID_ALLOW_PRERELEASE" not in content

    def test_install_mode_unknown_raises_at_generate_time(self):
        with pytest.raises(ValueError, match="Unknown install_mode"):
            self._jenkinsfile(install_mode="testpypi")  # dropped — no longer supported

    def test_pypi_mode_supports_testpypi_via_build_params(self):
        """TestPyPI is a BUILD-time choice in pypi mode: the operator sets the
        index parameters in Build With Parameters. The Setup shell consumes
        them via shell-level env expansion; no Groovy rebuild."""
        content = self._jenkinsfile()
        assert 'INDEX_URL="${FLUID_PIP_INDEX_URL-}"' in content
        assert 'EXTRA_INDEX_URL="${FLUID_PIP_EXTRA_INDEX_URL-}"' in content
        assert '"${FLUID_ALLOW_PRERELEASE:-false}" = "true"' in content
        # --index-url is only emitted when FLUID_PIP_INDEX_URL is set, and as ONE
        # argument: the value can never add pip options.
        assert 'set -- "$@" "--index-url=$INDEX_URL"' in content
        assert 'set -- "$@" "--extra-index-url=$EXTRA_INDEX_URL"' in content
        assert "INDEX_FLAGS" not in content

    # ── the injection boundary ───────────────────────────────────────

    def test_no_sh_body_interpolates_a_parameter(self):
        """A ``${params.X}`` inside a ``sh '''`` body is Groovy interpolation into
        shell: a value with a quote breaks out. Parameters reach the shell
        only as the environment variables ``environment {}`` exports."""
        content = self._jenkinsfile(scheduler_default="airflow", diff_last_applied=True)
        for body in re.findall(r"sh '''(.*?)'''", content, re.S):
            assert "${params." not in body and "params." not in body, body[:80]

    def test_stage_7_is_one_ship_step_reading_the_environment(self):
        """A build id can no longer smuggle ``--allow-data-loss`` into argv: stage 7
        runs one fixed ship command, and the id is an environment variable that
        ship passes as one argument."""
        stage = _stage_text(self._jenkinsfile(), "7 - apply")
        assert "environment {" not in stage
        assert "${params." not in stage
        assert "APPLY_BUILD_FLAG" not in stage and "'--build '" not in stage
        assert 'fluid ship "$CONTRACT" --stage 7 --yes' in stage


class TestJenkinsTemplateStage11ScheduleSync:
    """Stage 11 must be wired in the Jenkins template such that:

    1. **All scheduler variants are reachable** via Jenkins build parameters
       (airflow, mwaa, composer, astronomer, prefect, dagster), not hardcoded.

    2. **User-supplied parameter values never reach a ``sh`` string via
       Groovy interpolation**: they are exported through ``environment {}``,
       each with the parameter's declared default as its fallback.
    """

    def _jenkinsfile(self, **kwargs):
        cfg = PipelineConfig(provider=PipelineProvider.JENKINS, **kwargs)
        files = PipelineTemplateGenerator().generate_pipeline(cfg)
        return files["Jenkinsfile"]

    def test_stage_11_param_surface_complete(self):
        """All five variant-specific params + the scheduler choice + the
        dry-run toggle must be declared as Jenkins build parameters."""
        content = self._jenkinsfile()
        assert "RUN_STAGE_11_SCHEDULE_SYNC" in content
        assert "name: 'SCHEDULER'" in content
        for scheduler in ("airflow", "mwaa", "composer", "astronomer", "prefect", "dagster"):
            assert f"'{scheduler}'" in content
        assert "SCHEDULER_DESTINATION" in content
        assert "SCHEDULER_ENVIRONMENT_NAME" in content
        assert "SCHEDULER_LOCATION" in content
        assert "SCHEDULER_WORKSPACE" in content
        assert "SCHEDULE_SYNC_DRY_RUN" in content

    def test_stage_11_reads_params_from_the_environment_with_their_defaults(self):
        """Every scheduler param reaches ship as an environment variable, with the
        default the parameter declares as its fallback (a parameterless build
        exports none), and the stage's own shell reads no parameter."""
        content = self._jenkinsfile(
            scheduler_default="airflow", scheduler_destination_default="file:///dags"
        )
        assert (
            "SCHEDULER = \"${params.SCHEDULER == null ? 'airflow' : params.SCHEDULER}\"" in content
        )
        assert (
            "SCHEDULER_DESTINATION = \"${params.SCHEDULER_DESTINATION == null ? 'file:///dags' "
            ': params.SCHEDULER_DESTINATION}"' in content
        )
        for name in ("SCHEDULER_ENVIRONMENT_NAME", "SCHEDULER_LOCATION", "SCHEDULER_WORKSPACE"):
            assert f"{name} = \"${{params.{name} == null ? '' : params.{name}}}\"" in content
        assert "defaultValue: 'file:///dags'" in content
        assert "choices: ['airflow', ''," in content
        stage = _stage_text(content, "11 - schedule sync")
        sh_body = re.search(r"sh '''(.*?)'''", stage, re.S).group(1)
        assert "params." not in sh_body
        assert 'fluid ship "$CONTRACT" --stage 11' in sh_body

    def test_stage_11_gated_by_its_run_flag_with_the_declared_default(self):
        """The when{} clause gates on RUN_STAGE_11_SCHEDULE_SYNC (its declared
        default when the build has no parameters); a blank SCHEDULER is ship's
        own logged skip, not a when{} one."""
        content = self._jenkinsfile()
        assert (
            "return (params.RUN_STAGE_11_SCHEDULE_SYNC == null ? false "
            ": params.RUN_STAGE_11_SCHEDULE_SYNC.toString() == 'true')" in content
        )


# ── Every system, through its own template ──────────────────────────


class TestElevenStagesAllSystems:
    """The 6-way contract check: every non-Jenkins CI system emits one
    ``fluid ship`` step or job per phase, declares a switch per stage, installs
    fluid before any stage, honours ``workdir``, and takes its parameters from
    the shared :meth:`BasePipelineTemplate._eleven_stage_parameters` table."""

    SYSTEMS = [
        # (name, template_class, provider_enum, filename_substring)
        (
            "GitHubActions",
            GitHubActionsTemplate,
            PipelineProvider.GITHUB_ACTIONS,
            ".github/workflows/fluid-pipeline.yml",
        ),
        ("GitLab", GitLabCITemplate, PipelineProvider.GITLAB_CI, ".gitlab-ci.yml"),
        ("AzureDevOps", AzureDevOpsTemplate, PipelineProvider.AZURE_DEVOPS, "azure-pipelines.yml"),
        ("Bitbucket", BitbucketTemplate, PipelineProvider.BITBUCKET, "bitbucket-pipelines.yml"),
        ("CircleCI", CircleCITemplate, PipelineProvider.CIRCLE_CI, ".circleci/config.yml"),
        ("Tekton", TektonTemplate, PipelineProvider.TEKTON, "tekton/pipeline.yaml"),
    ]

    def _generate(self, template_cls, provider, **kwargs):
        cfg = PipelineConfig(provider=provider, **kwargs)
        return template_cls().generate(cfg)

    def _all_content(self, out):
        """Join every file's content (Tekton splits into pipeline.yaml +
        tasks.yaml, the others are single-file)."""
        return "\n".join(out.values())

    @pytest.mark.parametrize("name,cls,provider,fname", SYSTEMS)
    def test_every_phase_runs_ship(self, name, cls, provider, fname):
        content = self._all_content(self._generate(cls, provider))
        assert "fluid ship" in content, name
        assert re.search(r"--stage\W+1-6", content) or "1-6" in content, name
        assert "7-11" in content, name

    @pytest.mark.parametrize("name,cls,provider,fname", SYSTEMS)
    def test_filename_matches_system_convention(self, name, cls, provider, fname):
        out = self._generate(cls, provider)
        assert fname in out, f"{name}: {fname!r} not among {list(out)}"

    @pytest.mark.parametrize("name,cls,provider,fname", SYSTEMS)
    def test_all_eleven_toggles_declared(self, name, cls, provider, fname):
        """Every RUN_STAGE_N_SLUG toggle appears verbatim as an input, parameter
        or variable, so operators can find it in the system's own UI."""
        content = self._all_content(self._generate(cls, provider))
        for n in range(1, 12):
            # Tekton and CircleCI lower-case their parameter names.
            toggles = [f"RUN_STAGE_{n}_", f"run_stage_{n}_", f"run-stage-{n}-"]
            assert any(t in content for t in toggles), f"{name}: stage-{n} toggle missing"

    @pytest.mark.parametrize("name,cls,provider,fname", SYSTEMS)
    def test_install_setup_emitted(self, name, cls, provider, fname):
        content = self._all_content(self._generate(cls, provider))
        assert "pip install" in content, f"{name}: missing pip install in install-setup body"
        assert "FLUID_PACKAGE_SPEC" in content, f"{name}: missing FLUID_PACKAGE_SPEC override"
        assert "fluid --version" in content, f"{name}: missing fluid --version sanity check"

    @pytest.mark.parametrize("name,cls,provider,fname", SYSTEMS)
    def test_workdir_applied_when_set(self, name, cls, provider, fname):
        content = self._all_content(self._generate(cls, provider, workdir="examples/demo"))
        assert "examples/demo" in content, f"{name}: workdir not reflected in emit"

    @pytest.mark.parametrize("name,cls,provider,fname", SYSTEMS)
    def test_standard_params_declared(self, name, cls, provider, fname):
        """CONTRACT / FLUID_ENV / APPLY_MODE / ALLOW_DATA_LOSS / NO_VERIFY_DIGEST /
        PUBLISH_TARGETS / SCHEDULER_* come from the one shared table, so a new
        parameter lands in every system. CONTRACT is written into the ship
        command rather than declared."""
        content = self._all_content(self._generate(cls, provider))
        for param in [
            "FLUID_ENV",
            "APPLY_MODE",
            "ALLOW_DATA_LOSS",
            "NO_VERIFY_DIGEST",
            "PUBLISH_TARGETS",
            "SCHEDULER_DESTINATION",
        ]:
            variants = [param, param.lower(), param.lower().replace("_", "-")]
            assert any(v in content for v in variants), f"{name}: {param!r} not declared"

    @pytest.mark.parametrize("name,cls,provider,fname", SYSTEMS)
    def test_reference_only_contract_stage_3_default_false(self, name, cls, provider, fname):
        """With generates_artifacts=False the RUN_STAGE_3_GENERATE_ARTIFACTS switch
        defaults to ``false``: reference-only contracts delegate artifact
        ownership upstream."""
        template = cls()
        on = {
            n: d
            for n, _k, d, _x in template._eleven_stage_parameters(PipelineConfig(provider=provider))
        }
        off = {
            n: d
            for n, _k, d, _x in template._eleven_stage_parameters(
                PipelineConfig(provider=provider, generates_artifacts=False)
            )
        }
        assert on["RUN_STAGE_3_GENERATE_ARTIFACTS"] == "true"
        assert off["RUN_STAGE_3_GENERATE_ARTIFACTS"] == "false"
        out = self._generate(cls, provider, generates_artifacts=False)
        assert out != self._generate(cls, provider)
