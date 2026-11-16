# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid generate ci`` emits keyless (OIDC) cloud auth, never a static cloud key.

A contract that binds aws, gcp, snowflake and databricks, and holds a ``vault://``
secret reference, gets a GitHub Actions or GitLab pipeline whose cloud-touching
jobs ask the CI system for an ID token and exchange it themselves. The files
hold no static key; a contract that binds nothing but ``local`` gets no OIDC
step and no ``id-token`` permission.

Sources of the steps under test: GitHub OIDC docs, Snowflake WIF docs, Databricks
github-oidc docs, OpenBao JWT auth docs, GitLab id_tokens docs (see
``pipeline_systems/_keyless.py``).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Dict

import pytest
import yaml

from fluid_build.cli.generate_ci import _contract_cloud_auth
from fluid_build.cli.generate_ci import run as generate_ci_run
from fluid_build.forge.core.pipeline_systems import PipelineConfig, PipelineProvider, _keyless
from fluid_build.forge.core.pipeline_systems._base import PINNED_ACTIONS, PipelineComplexity
from fluid_build.forge.core.pipeline_systems.github_actions import GitHubActionsTemplate
from fluid_build.forge.core.pipeline_systems.gitlab_ci import GitLabCITemplate

pytestmark = pytest.mark.unit

_LOG = logging.getLogger("test_generate_ci_keyless")

#: A static cloud key or a way to carry one: none may appear in any generated file.
_FORBIDDEN = (
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "SNOWFLAKE_PASSWORD",
    "SNOWFLAKE_PRIVATE_KEY",
    "DATABRICKS_TOKEN",
    "DATABRICKS_CLIENT_SECRET",
    "secrets.",
)


def _binding(platform: str, **location: str) -> dict:
    return {"platform": platform, "format": "table", "location": location}


def _contract(*bindings: dict, reference: str = "") -> dict:
    contract = {
        "fluidVersion": "0.7.3",
        "kind": "DataProduct",
        "id": "gold.keyless_demo",
        "name": "keyless demo",
        "exposes": [
            {"exposeId": f"out_{i}", "binding": binding} for i, binding in enumerate(bindings)
        ],
    }
    if reference:
        contract["builds"] = [
            {
                "id": "ingest",
                "properties": {"source": {"connection": {"secretRef": reference}}},
            }
        ]
    return contract


_ALL_CLOUDS = (
    _binding("aws", bucket="b", path="p"),
    _binding("gcp", project="p", dataset="d", table="t"),
    _binding("snowflake", account="myorg-myaccount", database="D", schema="S", table="T"),
    _binding("databricks", catalog="c", schema="s", table="t"),
)


def _write(path: Path, contract: dict, overlay: dict | None = None) -> None:
    path.joinpath("contract.fluid.yaml").write_text(yaml.safe_dump(contract), encoding="utf-8")
    if overlay is not None:
        (path / "overlays").mkdir()
        (path / "overlays" / "prod.yaml").write_text(yaml.safe_dump(overlay), encoding="utf-8")


def _generate(tmp_path: Path, monkeypatch, system: str, complexity: str) -> Dict[str, str]:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("FLUID_ENV", raising=False)
    args = argparse.Namespace(
        system=system,
        out=None,
        contract="contract.fluid.yaml",
        complexity=complexity,
        provider=None,
    )
    assert generate_ci_run(args, _LOG) == 0
    return {
        str(p.relative_to(tmp_path)): p.read_text(encoding="utf-8")
        for p in sorted(tmp_path.rglob("*"))
        if p.is_file() and p.name != "contract.fluid.yaml" and "overlays" not in p.parts
    }


def _workflow(files: Dict[str, str], name: str) -> dict:
    text = files[name]
    return yaml.safe_load(text)


def _assert_no_static_key(files: Dict[str, str]) -> None:
    for name, text in files.items():
        for needle in _FORBIDDEN:
            assert needle not in text, f"{needle} in {name}"


# ── detection ───────────────────────────────────────────────────────────────


def test_detection_walks_the_base_and_every_overlay(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write(
        tmp_path,
        _contract(_binding("local", path="out.csv")),
        overlay={"exposes": [{"exposeId": "out_0", "binding": _binding("gcp", project="p")}]},
    )
    assert _contract_cloud_auth("contract.fluid.yaml") == ("gcp",)


@pytest.mark.parametrize("scheme", ["vault", "openbao"])
def test_a_vault_secret_ref_means_openbao(tmp_path, monkeypatch, scheme):
    monkeypatch.chdir(tmp_path)
    _write(
        tmp_path,
        _contract(_binding("local", path="o.csv"), reference=f"{scheme}://kv/pg#password"),
    )
    assert _contract_cloud_auth("contract.fluid.yaml") == ("openbao",)


def test_other_secret_schemes_need_no_openbao(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write(
        tmp_path,
        _contract(_binding("local", path="o.csv"), reference="env://PG_PASSWORD"),
    )
    assert _contract_cloud_auth("contract.fluid.yaml") == ()


def test_all_platforms_in_wiring_order(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, _contract(*reversed(_ALL_CLOUDS), reference="vault://kv/x#y"))
    assert _contract_cloud_auth("contract.fluid.yaml") == (
        "aws",
        "gcp",
        "snowflake",
        "databricks",
        "openbao",
    )


# ── GitHub Actions ──────────────────────────────────────────────────────────


def _cloud_files(tmp_path, monkeypatch, system, complexity):
    _write(
        tmp_path,
        _contract(*_ALL_CLOUDS, reference="vault://kv/pg#password"),
    )
    return _generate(tmp_path, monkeypatch, system, complexity)


def _uses(job: dict) -> list:
    return [s["uses"] for s in job["steps"] if "uses" in s]


def _step_names(job: dict) -> list:
    return [s.get("name", "") for s in job["steps"]]


@pytest.mark.parametrize("complexity", ["standard", "advanced", "enterprise"])
def test_github_deploy_jobs_authenticate_keylessly(tmp_path, monkeypatch, complexity):
    files = _cloud_files(tmp_path, monkeypatch, "github", complexity)
    wf = _workflow(files, ".github/workflows/fluid-standard.yml")

    assert wf["permissions"] == {}
    deploy = [name for name in wf["jobs"] if name.startswith("deploy-")]
    assert deploy
    for name in deploy:
        job = wf["jobs"][name]
        assert job["permissions"] == {"contents": "read", "id-token": "write"}
        uses = " ".join(_uses(job))
        assert "aws-actions/configure-aws-credentials@" in uses
        assert "google-github-actions/auth@" in uses
        assert job["env"]["DATABRICKS_AUTH_TYPE"] == "github-oidc"
        assert job["env"]["DATABRICKS_HOST"] == "${{ vars.DATABRICKS_HOST }}"
        assert job["env"]["DATABRICKS_CLIENT_ID"] == "${{ vars.DATABRICKS_CLIENT_ID }}"
        assert job["env"]["SNOWFLAKE_ACCOUNT"] == "${{ vars.SNOWFLAKE_ACCOUNT }}"
        assert job["env"]["FLUID_VAULT_JWT_ROLE"] == "${{ vars.OPENBAO_JWT_ROLE }}"
        assert job["env"]["BAO_ADDR"] == "${{ vars.BAO_ADDR }}"
        # Every login step runs before the first fluid command.
        first_fluid = next(i for i, s in enumerate(job["steps"]) if "fluid" in s.get("run", ""))
        auth = [
            i
            for i, s in enumerate(job["steps"])
            if "Authenticate" in s.get("name", "") or "OIDC" in s.get("name", "")
        ]
        assert len(auth) == 4 and max(auth) < first_fluid
    _assert_no_static_key(files)


def test_github_least_privilege_for_jobs_that_never_touch_the_cloud(tmp_path, monkeypatch):
    files = _cloud_files(tmp_path, monkeypatch, "github", "standard")
    wf = _workflow(files, ".github/workflows/fluid-standard.yml")
    for name, job in wf["jobs"].items():
        if name.startswith("deploy-"):
            continue
        assert "id-token" not in job["permissions"], name
        assert "env" not in job or "DATABRICKS_AUTH_TYPE" not in job["env"], name


def test_github_enterprise_compliance_verify_is_keyless_and_scheduled_only(tmp_path, monkeypatch):
    files = _cloud_files(tmp_path, monkeypatch, "github", "enterprise")
    wf = _workflow(files, ".github/workflows/compliance.yml")
    audit = wf["jobs"]["compliance-audit"]
    assert audit["permissions"] == {"contents": "read", "id-token": "write"}
    assert "aws-actions/configure-aws-credentials@" in " ".join(_uses(audit))
    # It reaches the cloud, so it must not run on pull requests.
    assert "pull_request" not in wf.get("on", wf.get(True))
    # The security workflow never touches the cloud.
    security = _workflow(files, ".github/workflows/security.yml")
    assert "id-token" not in security["jobs"]["security-scan"]["permissions"]
    _assert_no_static_key(files)


def test_github_basic_single_job_is_keyless(tmp_path, monkeypatch):
    files = _cloud_files(tmp_path, monkeypatch, "github", "basic")
    wf = _workflow(files, ".github/workflows/fluid-pipeline.yml")
    job = wf["jobs"]["fluid-pipeline"]
    assert job["permissions"] == {"contents": "read", "id-token": "write"}
    assert wf["permissions"] == {}
    _assert_no_static_key(files)


def test_github_snowflake_step_is_wif_from_a_masked_token_file(tmp_path, monkeypatch):
    files = _cloud_files(tmp_path, monkeypatch, "github", "standard")
    wf = _workflow(files, ".github/workflows/fluid-standard.yml")
    step = next(s for s in wf["jobs"]["deploy-dev"]["steps"] if "Snowflake" in s.get("name", ""))
    run = step["run"]
    assert "audience=snowflakecomputing.com" in run
    assert "ACTIONS_ID_TOKEN_REQUEST_URL" in run and "ACTIONS_ID_TOKEN_REQUEST_TOKEN" in run
    # The request token reaches curl on stdin, never in its argv.
    assert "-H @- " in run and '-H "Authorization' not in run
    assert "jq" not in run
    assert "::add-mask::" in run
    # Written under umask 077 and swapped in whole, so the file is 0600 and never half-written.
    assert "umask 077" in run and "$RUNNER_TEMP" in run
    assert 'mv -f "$token_file.new" "$token_file"' in run
    # GitHub ID tokens last about five minutes: a background loop re-mints the file,
    # and a refresh that gets no token keeps the last one.
    assert "( while sleep 240; do mint || true; done ) >/dev/null 2>&1 &" in run
    assert '[ -n "$token" ] || return 1' in run
    # A fork or Dependabot pull request gets no ID token: the step is skipped there.
    assert step["if"] == _keyless.GITHUB_HAS_ID_TOKEN
    assert subprocess.run(["bash", "-n"], input=run, text=True).returncode == 0
    assert "SNOWFLAKE_AUTHENTICATOR=WORKLOAD_IDENTITY" in run
    assert "SNOWFLAKE_WORKLOAD_IDENTITY_PROVIDER=OIDC" in run
    assert "SNOWFLAKE_TOKEN_FILE_PATH=$token_file" in run
    # The token is never written to the environment (only its file path is).
    assert "SNOWFLAKE_TOKEN=" not in run


def test_the_github_token_script_keeps_the_last_token_when_a_refresh_fails(tmp_path, monkeypatch):
    """The generated step, run with stand-ins for curl and sleep: the first token is
    written 0600 and masked, the one refresh gets no token, and the file keeps it."""
    files = _cloud_files(tmp_path, monkeypatch, "github", "standard")
    wf = _workflow(files, ".github/workflows/fluid-standard.yml")
    step = next(s for s in wf["jobs"]["deploy-dev"]["steps"] if "Snowflake" in s.get("name", ""))
    fakes, state, runner_temp = (tmp_path / name for name in ("bin", "state", "runner"))
    for directory in (fakes, state, runner_temp):
        directory.mkdir()
    # curl answers once, then fails; sleep lets one refresh through, then ends the loop.
    stand_ins = {
        "curl": 'n=$(cat "$STATE/curl" 2>/dev/null || echo 0); echo $((n + 1)) > "$STATE/curl"\n'
        # The header arrives on stdin (-H @-), not in the arguments.
        'IFS= read -r header; [ "$header" = "Authorization: bearer request-token" ] || exit 9\n'
        'case " $* " in *request-token*) exit 8;; esac\n'
        '[ "$n" = 0 ] && echo \'{"value": "id-token-1"}\' && exit 0\nexit 22\n',
        "sleep": 'n=$(cat "$STATE/sleep" 2>/dev/null || echo 0); echo $((n + 1)) > "$STATE/sleep"\n'
        '[ "$n" = 0 ] && exit 0\ntouch "$STATE/done"\nexit 1\n',
    }
    for name, body in stand_ins.items():
        (fakes / name).write_text("#!/bin/bash\n" + body, encoding="utf-8")
        (fakes / name).chmod(0o755)
    env = {
        "PATH": f"{fakes}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "STATE": str(state),
        "RUNNER_TEMP": str(runner_temp),
        "GITHUB_ENV": str(tmp_path / "github_env"),
        "ACTIONS_ID_TOKEN_REQUEST_URL": "https://token.invalid/?api-version=2.0",
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "request-token",  # pragma: allowlist secret
    }
    result = subprocess.run(
        ["bash", "-c", step["run"]],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "::add-mask::id-token-1" in result.stdout
    deadline = time.monotonic() + 10
    while not (state / "done").exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert (state / "done").exists(), "the refresh loop never ran"
    assert (state / "curl").read_text(encoding="utf-8").strip() == "2"  # the refresh asked
    token_file = runner_temp / "snowflake-id-token"
    assert token_file.read_text(encoding="utf-8") == "id-token-1"
    assert token_file.stat().st_mode & 0o777 == 0o600
    exported = (tmp_path / "github_env").read_text(encoding="utf-8")
    assert f"SNOWFLAKE_TOKEN_FILE_PATH={token_file}" in exported


def test_github_openbao_step_uses_the_audience_variable(tmp_path, monkeypatch):
    files = _cloud_files(tmp_path, monkeypatch, "github", "standard")
    wf = _workflow(files, ".github/workflows/fluid-standard.yml")
    step = next(s for s in wf["jobs"]["deploy-dev"]["steps"] if "OpenBao" in s.get("name", ""))
    assert step["env"] == {"OIDC_AUDIENCE": "${{ vars.OPENBAO_JWT_AUDIENCE }}"}
    assert "FLUID_VAULT_JWT_FILE=$token_file" in step["run"]
    assert "::add-mask::" in step["run"] and "umask 077" in step["run"]
    assert step["if"] == _keyless.GITHUB_HAS_ID_TOKEN


def test_github_banner_carries_the_cloud_side_and_the_immutable_subject(tmp_path, monkeypatch):
    files = _cloud_files(tmp_path, monkeypatch, "github", "standard")
    text = files[".github/workflows/fluid-standard.yml"]
    assert "repo:OWNER@OWNER-ID/REPO@REPO-ID" in text
    assert "2026-07-15" in text
    assert "https://token.actions.githubusercontent.com" in text
    assert "WORKLOAD_IDENTITY = (TYPE = OIDC ISSUER" in text
    assert "bao write auth/jwt/config oidc_discovery_url=" in text
    assert "role_type=jwt user_claim=sub" in text and "bound_subject=" in text
    example = files[".env.ci.example"]
    for name in ("AWS_ROLE_ARN", "GCP_SA_EMAIL", "SNOWFLAKE_ACCOUNT", "DATABRICKS_CLIENT_ID"):
        assert name in example
    _assert_no_static_key(files)


def test_every_generated_github_action_is_pinned_by_sha(tmp_path, monkeypatch):
    files = _cloud_files(tmp_path, monkeypatch, "github", "enterprise")
    for name, text in files.items():
        if not name.endswith(".yml"):
            continue
        for line in text.splitlines():
            if "uses:" in line:
                ref = line.split("uses:", 1)[1].strip()
                _, _, sha = ref.partition("@")
                assert len(sha) == 40 and set(sha) <= set("0123456789abcdef"), (name, ref)


def test_the_new_actions_are_in_the_pin_table_with_a_version_comment():
    assert "aws-actions/configure-aws-credentials@v6" in PINNED_ACTIONS
    assert "google-github-actions/auth@v3" in PINNED_ACTIONS
    assert not [key for key in PINNED_ACTIONS if key.startswith("azure/login")]


def test_github_eleven_stage_job_is_keyless():
    cfg = PipelineConfig(
        provider=PipelineProvider.GITHUB_ACTIONS,
        complexity=PipelineComplexity.STANDARD,
        cloud_auth=("aws", "gcp", "snowflake", "databricks", "openbao"),
    )
    files = GitHubActionsTemplate()._generate_eleven_stage(cfg)
    text = files[".github/workflows/fluid-pipeline.yml"]
    wf = yaml.safe_load(text[text.index("{") :])
    assert wf["permissions"] == {}
    job = wf["jobs"]["pipeline"]
    assert job["permissions"] == {"contents": "read", "id-token": "write"}
    names = _step_names(job)
    first_stage = next(i for i, n in enumerate(names) if n.startswith("1 "))
    assert all(i < first_stage for i, n in enumerate(names) if "OIDC" in n or "Authenticate" in n)
    assert job["env"]["DATABRICKS_AUTH_TYPE"] == "github-oidc"
    _assert_no_static_key({"fluid-pipeline.yml": text})


def test_a_local_only_contract_gets_no_oidc(tmp_path, monkeypatch):
    _write(tmp_path, _contract(_binding("local", path="out.csv")))
    for complexity in ("basic", "standard", "advanced", "enterprise"):
        files = _generate(tmp_path, monkeypatch, "github", complexity)
        text = "\n".join(files.values())
        for name, content in files.items():
            if name.endswith((".yml", ".yaml")):
                data = yaml.safe_load(content)
                for job_name, job in data["jobs"].items():
                    if job_name == "supply-chain":
                        continue  # attestations, not cloud auth
                    assert "id-token" not in (job.get("permissions") or {}), (name, job_name)
        assert "configure-aws-credentials" not in text
        assert "google-github-actions/auth" not in text
        assert "ACTIONS_ID_TOKEN_REQUEST" not in text
        assert "binds no cloud platform" in text
        _assert_no_static_key(files)
    cfg = PipelineConfig(
        provider=PipelineProvider.GITHUB_ACTIONS, complexity=PipelineComplexity.STANDARD
    )
    (workflow,) = GitHubActionsTemplate()._generate_eleven_stage(cfg).values()
    assert "id-token" not in workflow and "ACTIONS_ID_TOKEN_REQUEST" not in workflow


# ── GitLab CI ───────────────────────────────────────────────────────────────


def _gitlab(tmp_path, monkeypatch, complexity):
    files = _cloud_files(tmp_path, monkeypatch, "gitlab", complexity)
    return files, yaml.safe_load(files[".gitlab-ci.yml"])


@pytest.mark.parametrize("complexity", ["basic", "standard", "advanced"])
def test_gitlab_deploy_jobs_get_id_tokens_per_platform(tmp_path, monkeypatch, complexity):
    files, ci = _gitlab(tmp_path, monkeypatch, complexity)
    cloud_jobs = [n for n in ci if n in ("apply", "publish") or n.startswith("deploy-")]
    assert cloud_jobs
    for name in cloud_jobs:
        job = ci[name]
        tokens = job["id_tokens"]
        assert tokens["AWS_OIDC_TOKEN"] == {"aud": "sts.amazonaws.com"}
        assert tokens["GCP_OIDC_TOKEN"]["aud"] == (
            "//iam.googleapis.com/projects/$GCP_PROJECT_NUMBER/locations/global/"
            "workloadIdentityPools/$WIF_POOL/providers/$WIF_PROVIDER"
        )
        assert tokens["SNOWFLAKE_OIDC_TOKEN"] == {"aud": "snowflakecomputing.com"}
        assert tokens["DATABRICKS_OIDC_TOKEN"] == {"aud": "$DATABRICKS_ACCOUNT_ID"}
        assert tokens["OPENBAO_OIDC_TOKEN"] == {"aud": "$OPENBAO_JWT_AUDIENCE"}
        assert job["variables"]["DATABRICKS_AUTH_TYPE"] == "env-oidc"
        script = "\n".join(job["script"])
        assert "AWS_WEB_IDENTITY_TOKEN_FILE" in script
        assert "GOOGLE_APPLICATION_CREDENTIALS" in script and "external_account" in script
        assert "SNOWFLAKE_AUTHENTICATOR=WORKLOAD_IDENTITY" in script
        assert "SNOWFLAKE_TOKEN_FILE_PATH" in script
        assert "FLUID_VAULT_JWT_FILE" in script and "FLUID_VAULT_JWT_ROLE" in script
        assert "umask 077" in script and "mktemp -d" in script
        # The login lines come before the first fluid command.
        assert script.index("AWS_WEB_IDENTITY_TOKEN_FILE") < script.index("fluid ")
    _assert_no_static_key(files)


def test_gitlab_jobs_that_never_touch_the_cloud_get_no_token(tmp_path, monkeypatch):
    _, ci = _gitlab(tmp_path, monkeypatch, "standard")
    for name, job in ci.items():
        if (
            isinstance(job, dict)
            and "stage" in job
            and not (name.startswith("deploy-") or name == "publish")
        ):
            assert "id_tokens" not in job, name


def test_gitlab_gcp_credential_file_is_valid_json_for_the_sdk(tmp_path, monkeypatch):
    _, ci = _gitlab(tmp_path, monkeypatch, "standard")
    line = next(s for s in ci["deploy-dev"]["script"] if "external_account" in s)
    template = line.split("printf '", 1)[1].split("' ", 1)[0]
    rendered = template.replace("%s", "X")
    data = json.loads(rendered)
    assert data["type"] == "external_account"
    assert data["credential_source"] == {"file": "X"}
    assert data["token_url"] == "https://sts.googleapis.com/v1/token"


def test_gitlab_eleven_stage_tokens_only_on_cloud_stages():
    cfg = PipelineConfig(
        provider=PipelineProvider.GITLAB_CI,
        complexity=PipelineComplexity.STANDARD,
        cloud_auth=("aws", "snowflake"),
    )
    files = GitLabCITemplate()._generate_eleven_stage(cfg)
    text = files[".gitlab-ci.yml"]
    ci = yaml.safe_load(text[text.index("{") :])
    with_tokens = {n for n, j in ci.items() if isinstance(j, dict) and "id_tokens" in j}
    assert with_tokens == {
        "stage-5-diff",
        "stage-7-apply",
        "stage-8-policy-apply",
        "stage-9-verify",
        "stage-10-publish",
        "stage-11-schedule-sync",
    }
    _assert_no_static_key({".gitlab-ci.yml": text})


def test_a_local_only_contract_gets_no_gitlab_token(tmp_path, monkeypatch):
    _write(tmp_path, _contract(_binding("local", path="out.csv")))
    for complexity in ("basic", "standard", "advanced"):
        files = _generate(tmp_path, monkeypatch, "gitlab", complexity)
        assert "id_tokens" not in files[".gitlab-ci.yml"]
        _assert_no_static_key(files)


# ── the other systems ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "system", ["jenkins", "azure", "bitbucket", "circleci", "tekton", "github", "gitlab"]
)
def test_no_system_suggests_a_static_cloud_key(tmp_path, monkeypatch, system):
    files = _cloud_files(tmp_path, monkeypatch, system, "standard")
    _assert_no_static_key(files)
    for name, text in files.items():
        for needle in ("GOOGLE_APPLICATION_CREDENTIALS=/", "sa-key", "aws-access-key"):
            assert needle not in text, (system, name, needle)


def test_jenkins_says_it_has_no_native_oidc(tmp_path, monkeypatch):
    files = _cloud_files(tmp_path, monkeypatch, "jenkins", "standard")
    text = files["Jenkinsfile"]
    assert "Jenkins has no native OIDC" in text
    assert "never commit" in text.lower() or "never commit" in text


@pytest.mark.parametrize(
    ("binding", "expected"),
    [
        ({"platform": "bigquery"}, ("gcp",)),
        ({"platform": "s3"}, ("aws",)),
        ({"provider": "gcp"}, ("gcp",)),
        ({"platform": "Snowflake"}, ("snowflake",)),
    ],
)
def test_detection_uses_the_cloud_names_fluid_apply_accepts(tmp_path, binding, expected):
    from fluid_build.cli.generate_ci import _contract_cloud_auth

    contract = _contract()
    contract["exposes"] = [{"exposeId": "out", "binding": {**binding, "format": "table"}}]
    _write(tmp_path, contract)
    assert _contract_cloud_auth(str(tmp_path / "contract.fluid.yaml")) == expected


@pytest.mark.parametrize(
    "builds",
    [["x"], [{"id": "a", "execution": "x"}], [{"id": "a", "execution": {"runtime": "sf"}}]],
)
def test_a_malformed_build_never_breaks_detection(tmp_path, builds):
    from fluid_build.cli.generate_ci import _contract_cloud_auth

    contract = _contract(_binding("aws"))
    contract["builds"] = builds
    _write(tmp_path, contract)
    assert _contract_cloud_auth(str(tmp_path / "contract.fluid.yaml")) == ("aws",)


def test_a_build_on_another_cloud_gets_its_own_login(tmp_path):
    from fluid_build.cli.generate_ci import _contract_cloud_auth

    contract = _contract(_binding("aws"))
    contract["builds"] = [{"id": "dbt", "execution": {"runtime": {"platform": "databricks"}}}]
    _write(tmp_path, contract)
    assert _contract_cloud_auth(str(tmp_path / "contract.fluid.yaml")) == ("aws", "databricks")


def test_the_jenkins_header_names_credentials_binding_not_keyless(tmp_path, monkeypatch):
    _write(tmp_path, _contract(_binding("aws")))
    text = _generate(tmp_path, monkeypatch, "jenkins", "standard")["Jenkinsfile"]
    lines = text.splitlines()
    header = "\n".join(
        lines[: next(i for i, line in enumerate(lines) if not line.startswith("//"))]
    )
    assert "Credential model: credentials binding (Jenkins has no native OIDC)" in header
    assert "Credential model: keyless" not in text
    # The header lists only the detected platform's token readers.
    assert "AWS_WEB_IDENTITY_TOKEN_FILE" in header
    assert "SNOWFLAKE_TOKEN_FILE_PATH" not in header and "FLUID_VAULT_JWT_FILE" not in header


def test_a_pipeline_built_without_a_contract_says_so():
    from fluid_build.forge.core.pipeline_systems import _keyless

    text = "\n".join(_keyless.banner_lines((), "GitHub Actions"))
    assert "no contract was read, or it binds none" in text
    assert "fluid generate ci <contract>" in text


def test_the_forge_scaffolder_reads_the_project_contract(tmp_path):
    from fluid_build.cli import _forge_ci_scaffolder

    assert _forge_ci_scaffolder._cloud_auth(tmp_path) == ()
    _write(tmp_path, _contract(_binding("aws"), reference="vault://kv/app#pw"))
    assert _forge_ci_scaffolder._cloud_auth(tmp_path) == ("aws", "openbao")


def test_systems_without_wiring_point_to_their_own_oidc(tmp_path, monkeypatch):
    for system, needle in (
        ("azure", "workload identity federation"),
        ("bitbucket", "BITBUCKET_STEP_OIDC_TOKEN"),
        ("circleci", "CIRCLE_OIDC_TOKEN_V2"),
    ):
        files = _cloud_files(tmp_path, monkeypatch, system, "standard")
        assert needle in "\n".join(files.values()), system
        for name in list(files):
            Path(tmp_path / name).unlink()


def test_the_pipeline_config_refuses_an_unknown_platform():
    with pytest.raises(ValueError, match="unknown cloud_auth"):
        PipelineConfig(
            provider=PipelineProvider.GITHUB_ACTIONS,
            complexity=PipelineComplexity.BASIC,
            cloud_auth=("azure",),
        )


def test_no_generated_file_leaks_a_cwd_path(tmp_path, monkeypatch):
    files = _cloud_files(tmp_path, monkeypatch, "gitlab", "standard")
    assert os.fspath(tmp_path) not in files[".gitlab-ci.yml"]
