# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The ``tofu`` child's environment is an allowlist (``iac/credentials.py``)."""

from __future__ import annotations

import json
import logging

import pytest

from fluid_build.iac import IAC_PLUGINS, get_iac_plugin
from fluid_build.iac.credentials import (
    BASE_ENV_NAMES,
    allowed_env_name,
    backend_clouds,
    build_tofu_env,
    credential_report,
)

pytestmark = pytest.mark.unit

#: Secrets an operator's shell plausibly holds that no cloud provider reads.
UNRELATED = {
    "ANTHROPIC_API_KEY": "sk-ant-x",  # pragma: allowlist secret
    "OPENAI_API_KEY": "sk-x",  # pragma: allowlist secret
    "GOOGLE_API_KEY": "AIza-x",  # pragma: allowlist secret
    "AWS_BEARER_TOKEN_BEDROCK": "bedrock-x",  # pragma: allowlist secret
    "GITHUB_TOKEN": "ghp_x",  # pragma: allowlist secret
    "FLUID_API_KEY": "cc-x",  # pragma: allowlist secret
    "VAULT_TOKEN": "hvs.x",  # pragma: allowlist secret
    "SECRET_KEY": "django-x",  # pragma: allowlist secret
    "DATABASE_URL": "postgres://u:p@h/db",  # pragma: allowlist secret
}

BASICS = {
    "PATH": "/usr/bin:/bin",
    "HOME": "/home/op",
    "USER": "op",
    "LANG": "C.UTF-8",
    "TMPDIR": "/tmp/op",
    "HTTPS_PROXY": "http://proxy:3128",
    "no_proxy": "localhost",
    "SSL_CERT_FILE": "/etc/ssl/ca.pem",
    "PLUGIN_UNIX_SOCKET_DIR": "/tmp",
    "TF_CLI_CONFIG_FILE": "/etc/fluid/tofu/offline.tfrc",
    "TF_PLUGIN_CACHE_DIR": "/var/cache/tofu",
    "TF_LOG": "INFO",
    "TF_TOKEN_registry_example_com": "registry-token",  # pragma: allowlist secret
}


def _env(**extra: str) -> dict:
    return {**BASICS, **UNRELATED, **extra}


class TestBasics:
    def test_keeps_the_process_basics_proxies_ca_bundles_and_tofu_settings(self):
        env = build_tofu_env(base=_env())
        assert {k: env[k] for k in BASICS} == BASICS

    def test_drops_every_unrelated_secret(self):
        env = build_tofu_env(get_iac_plugin("aws"), base=_env())
        assert not set(UNRELATED) & set(env)

    def test_model_keys_never_pass_inside_a_cloud_family(self):
        model_keys = {
            "GOOGLE_API_KEY": "g",  # pragma: allowlist secret
            "GOOGLE_GENERATIVE_AI_API_KEY": "g",  # pragma: allowlist secret
            "AZURE_API_KEY": "a",  # pragma: allowlist secret
            "AZURE_OPENAI_API_KEY": "a",  # pragma: allowlist secret
            "AZURE_OPENAI_ENDPOINT": "https://x.openai.azure.com",
            "AZURE_AI_INFERENCE_KEY": "a",  # pragma: allowlist secret
            "AWS_BEARER_TOKEN_BEDROCK": "b",  # pragma: allowlist secret
        }
        kept = {"GOOGLE_CREDENTIALS": "{}", "AZURE_TENANT_ID": "t", "ARM_CLIENT_ID": "c"}
        # Databricks reads the GOOGLE_, AZURE_ and ARM_ families on GCP and Azure workspaces.
        env = build_tofu_env(get_iac_plugin("databricks"), base={**model_keys, **kept})
        assert env == kept

    def test_is_a_copy_and_never_falls_back_to_the_process_environment(self):
        base = {"PATH": "/usr/bin"}
        env = build_tofu_env(base=base)
        env["EXTRA"] = "1"
        assert "EXTRA" not in base
        assert build_tofu_env(base={}) == {}

    def test_defaults_to_the_process_environment(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("PATH", "/opt/bin")
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_x")  # pragma: allowlist secret
        env = build_tofu_env()
        assert env["PATH"] == "/opt/bin"
        assert "GITHUB_TOKEN" not in env

    def test_setup_opentofu_wrapper_keeps_working(self):
        # The default wrapper runs $TOFU_CLI_PATH/tofu-bin and writes its outputs to
        # the GITHUB_OUTPUT file (else to stdout, after the JSON forge reads). The
        # job's other file commands and its token stay out.
        base = {
            "TOFU_CLI_PATH": "/opt/hostedtoolcache/tofu/1.13.9/x64",
            "GITHUB_OUTPUT": "/home/runner/work/_temp/_runner_file_commands/set_output_1",
            "GITHUB_ENV": "/home/runner/work/_temp/_runner_file_commands/set_env_1",
            "GITHUB_PATH": "/home/runner/work/_temp/_runner_file_commands/add_path_1",
            "GITHUB_TOKEN": "ghs_x",  # pragma: allowlist secret
            "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "x",  # pragma: allowlist secret
        }
        env = build_tofu_env(base=base)
        assert set(env) == {"TOFU_CLI_PATH", "GITHUB_OUTPUT"}


class TestOpenTofuSettings:
    @pytest.mark.parametrize(
        "name",
        [
            "TF_CLI_ARGS",
            "TF_CLI_ARGS_apply",
            "TF_VAR_fluid_state_passphrase",
            "TF_ENCRYPTION",
            "TF_INPUT",
            "TF_REATTACH_PROVIDERS",
        ],
    )
    def test_settings_that_change_forge_commands_never_pass(self, name: str):
        assert name not in build_tofu_env(base={name: "x"})

    def test_a_dropped_setting_is_named_in_a_warning_without_its_value(
        self, caplog: pytest.LogCaptureFixture
    ):
        caplog.set_level(logging.WARNING, logger="fluid_build.iac.credentials")
        build_tofu_env(base={"TF_CLI_ARGS_apply": "-auto-approve-canary", "TF_LOG": "INFO"})
        records = [json.loads(r.getMessage()) for r in caplog.records]
        assert [r["event"] for r in records] == ["tofu_env_dropped"]
        assert records[0]["names"] == ["TF_CLI_ARGS_apply"]
        assert "canary" not in caplog.text

    def test_no_warning_when_nothing_was_dropped(self, caplog: pytest.LogCaptureFixture):
        caplog.set_level(logging.WARNING, logger="fluid_build.iac.credentials")
        build_tofu_env(base={"TF_LOG": "INFO", "PATH": "/bin"})
        assert not caplog.records


class TestCloudFamilies:
    @pytest.mark.parametrize(
        ("plugin", "kept"),
        [
            (
                "aws",
                [
                    "AWS_PROFILE",
                    "AWS_CONFIG_FILE",
                    "AWS_WEB_IDENTITY_TOKEN_FILE",
                    "AWS_ROLE_ARN",
                    "AWS_CONTAINER_CREDENTIALS_FULL_URI",
                    "AWS_CONTAINER_AUTHORIZATION_TOKEN",
                    "AWS_CA_BUNDLE",
                    "AWS_ENDPOINT_URL_S3",
                ],
            ),
            (
                "gcp",
                [
                    "GOOGLE_APPLICATION_CREDENTIALS",
                    "GOOGLE_IMPERSONATE_SERVICE_ACCOUNT",
                    "GOOGLE_PROJECT",
                    "CLOUDSDK_CONFIG",
                    "CLOUDSDK_CORE_PROJECT",
                    "GCE_METADATA_HOST",
                ],
            ),
            ("snowflake", ["SNOWFLAKE_ACCOUNT", "SNOWFLAKE_CONFIG_PATH", "SNOWFLAKE_TOKEN"]),
            ("databricks", ["DATABRICKS_HOST", "DATABRICKS_AUTH_TYPE", "ARM_TENANT_ID"]),
            ("confluent", ["CONFLUENT_CLOUD_API_KEY", "TABLEFLOW_API_SECRET"]),
        ],
    )
    def test_each_plugin_keeps_its_whole_family(self, plugin: str, kept: list):
        env = build_tofu_env(get_iac_plugin(plugin), base=_env(**{k: "v" for k in kept}))
        assert set(kept) <= set(env)

    def test_one_cloud_never_gets_another_clouds_credentials(self):
        other = {
            "AWS_SECRET_ACCESS_KEY": "a",  # pragma: allowlist secret
            "SNOWFLAKE_PASSWORD": "s",  # pragma: allowlist secret
            "DATABRICKS_TOKEN": "d",  # pragma: allowlist secret
            "CONFLUENT_CLOUD_API_SECRET": "c",  # pragma: allowlist secret
        }
        env = build_tofu_env(get_iac_plugin("gcp"), base=_env(**other))
        assert not set(other) & set(env)

    @pytest.mark.parametrize("name", sorted(IAC_PLUGINS))
    def test_every_in_tree_plugin_declares_a_family_that_covers_its_names(self, name: str):
        plugin = IAC_PLUGINS[name]
        assert getattr(plugin, "credential_env_prefixes", ())
        for var in plugin.credential_env_vars:
            assert allowed_env_name(var, plugin), var

    def test_the_state_backend_cloud_is_kept_whatever_the_plugin(self):
        backend = {"s3": {"bucket": "b", "key": "fluid/x/snowflake/terraform.tfstate"}}
        assert backend_clouds(backend) == ("aws",)
        assert backend_clouds({"gcs": {"bucket": "b"}}) == ("gcp",)
        assert backend_clouds(None) == ()
        env = build_tofu_env(
            get_iac_plugin("snowflake"),
            clouds=backend_clouds(backend),
            base=_env(AWS_PROFILE="state", SNOWFLAKE_ACCOUNT="org-acct"),
        )
        assert env["AWS_PROFILE"] == "state"
        assert env["SNOWFLAKE_ACCOUNT"] == "org-acct"
        assert "AWS_PROFILE" not in build_tofu_env(
            get_iac_plugin("snowflake"), base=_env(AWS_PROFILE="state")
        )

    def test_github_token_request_reaches_databricks_only_for_github_oidc(self):
        request = {
            "ACTIONS_ID_TOKEN_REQUEST_URL": "https://token.example/request",
            "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "request-token",  # pragma: allowlist secret
        }
        plugin = get_iac_plugin("databricks")
        host = {"DATABRICKS_HOST": "https://adb-1.azuredatabricks.net"}
        oidc = {**host, "DATABRICKS_AUTH_TYPE": "github-oidc", "DATABRICKS_CLIENT_ID": "sp"}
        pat = {**host, "DATABRICKS_TOKEN": "dapi-x"}  # pragma: allowlist secret
        # Never through the allowlist: the job's token request is no Databricks variable.
        assert not set(request) & set(build_tofu_env(plugin, base={**oidc, **request}))
        assert set(request) <= set(plugin.credential_env({**oidc, **request}))
        assert not set(request) & set(plugin.credential_env({**pat, **request}))

    def test_azure_devops_job_variables_reach_databricks_only_for_its_oidc(self):
        job = {
            "SYSTEM_ACCESSTOKEN": "ado-token",  # pragma: allowlist secret
            "SYSTEM_TEAMFOUNDATIONCOLLECTIONURI": "https://dev.azure.com/org/",
            "SYSTEM_PLANID": "p",
            "SYSTEM_JOBID": "j",
            "SYSTEM_TEAMPROJECTID": "t",
            "SYSTEM_HOSTTYPE": "build",
        }
        plugin = get_iac_plugin("databricks")
        host = {
            "DATABRICKS_HOST": "https://adb-1.azuredatabricks.net",
            "DATABRICKS_CLIENT_ID": "sp",
        }
        ado = {**host, "DATABRICKS_AUTH_TYPE": "azure-devops-oidc", **job}
        assert set(job) <= set(plugin.credential_env(ado))
        github = {**host, "DATABRICKS_AUTH_TYPE": "github-oidc", **job}
        assert not set(job) & set(plugin.credential_env(github))

    def test_a_plugin_that_predates_the_allowlist_is_named_once(
        self, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ):
        from fluid_build.iac import credentials

        monkeypatch.setattr(credentials, "_WARNED", set())

        class OnPrem:  # an out-of-tree plugin written before credential_env_prefixes
            name = "onprem"
            credential_env_vars = ("ONPREM_TOKEN",)

        caplog.set_level(logging.WARNING, logger="fluid_build.iac.credentials")
        base = {"ONPREM_TOKEN": "t", "ONPREM_URL": "https://x"}  # pragma: allowlist secret
        assert build_tofu_env(OnPrem(), base=base) == {"ONPREM_TOKEN": "t"}
        build_tofu_env(OnPrem(), base=base)
        events = [json.loads(r.getMessage()) for r in caplog.records]
        assert [(e["event"], e["plugin"]) for e in events] == [
            ("tofu_env_plugin_undeclared", "onprem")
        ]
        caplog.clear()
        build_tofu_env(get_iac_plugin("aws"), base=base)
        assert not caplog.records

    def test_basics_cover_a_credential_process_helper(self):
        # aws-kc style credential_process helpers run through the shell and read
        # the user's keychain: PATH, HOME and USER must reach tofu.
        assert {"PATH", "HOME", "USER", "LOGNAME", "TMPDIR"} <= BASE_ENV_NAMES


class TestCredentialReport:
    def test_splits_present_and_absent(self):
        plugin = get_iac_plugin("aws")
        env = {"AWS_ACCESS_KEY_ID": "AKIA", "AWS_REGION": "us-east-1"}
        present, absent = credential_report(plugin, env)
        assert "AWS_ACCESS_KEY_ID" in present
        assert "AWS_REGION" in present
        assert "AWS_SECRET_ACCESS_KEY" in absent

    def test_all_absent_on_empty_env(self):
        plugin = get_iac_plugin("snowflake")
        present, absent = credential_report(plugin, {})
        assert present == []
        assert set(absent) == set(plugin.credential_env_vars)

    def test_empty_value_counts_as_absent(self):
        plugin = get_iac_plugin("snowflake")
        present, absent = credential_report(plugin, {"SNOWFLAKE_USER": ""})
        assert "SNOWFLAKE_USER" in absent
