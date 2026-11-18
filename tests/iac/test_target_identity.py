# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The cloud target a saved plan is bound to, read as each provider reads it.

No cloud SDK or network: boto3 and google-auth are stand-ins put into
``sys.modules`` for one test, and the module-level seams are patched.
"""

from __future__ import annotations

import sys
import types
from typing import Any, Dict

import pytest

from fluid_build.cli import _apply_opentofu_engine as engine
from fluid_build.iac import target_identity as ti

pytestmark = [pytest.mark.unit]


def _module(provider: str, **settings: Any) -> Dict[str, Any]:
    return {"provider": {provider: settings}} if settings else {}


class TestAws:
    def test_account_and_region(self, monkeypatch):
        monkeypatch.setattr(ti, "aws_caller_account", lambda env, region: ("123456789012", ""))
        identity, unread = ti.target_identity("aws", {}, {"AWS_REGION": "eu-west-1"})
        assert identity == {"account": "123456789012", "region": "eu-west-1"}
        assert unread == {}

    @pytest.mark.parametrize(
        "module, env, region",
        [
            (_module("aws", region="eu-central-1"), {"AWS_REGION": "us-east-1"}, "eu-central-1"),
            ({}, {"AWS_REGION": "us-east-1", "AWS_DEFAULT_REGION": "us-west-2"}, "us-east-1"),
            ({}, {"AWS_DEFAULT_REGION": "us-west-2"}, "us-west-2"),
        ],
        ids=["provider-block-wins", "AWS_REGION-before-default", "AWS_DEFAULT_REGION"],
    )
    def test_region_precedence(self, monkeypatch, module, env, region):
        monkeypatch.setattr(ti, "aws_caller_account", lambda env, region: ("1", ""))
        assert ti.target_identity("aws", module, env)[0]["region"] == region

    def test_what_cannot_be_read_says_why(self, monkeypatch):
        monkeypatch.setattr(ti, "aws_caller_account", lambda env, region: ("", "no creds"))
        identity, unread = ti.target_identity("aws", {}, {})
        assert identity == {}
        assert unread == {
            "account": "no creds",
            "region": "no provider region, AWS_REGION or AWS_DEFAULT_REGION",
        }

    def test_sts_gets_the_runs_own_credentials(self, monkeypatch):
        seen: Dict[str, Any] = {}

        class _Client:
            def get_caller_identity(self):
                return {"Account": "123456789012", "Arn": "arn:aws:iam::123456789012:user/x"}

        class _Session:
            def __init__(self, **kwargs):
                seen["session"] = kwargs

            def client(self, name, endpoint_url=None):
                seen["client"] = (name, endpoint_url)
                return _Client()

        boto3 = types.ModuleType("boto3")
        boto3.session = types.SimpleNamespace(Session=_Session)
        monkeypatch.setitem(sys.modules, "boto3", boto3)
        env = {
            "AWS_ACCESS_KEY_ID": "AKIAEXAMPLE",  # pragma: allowlist secret
            "AWS_SECRET_ACCESS_KEY": "example",  # pragma: allowlist secret
            "AWS_ENDPOINT_URL_STS": "http://localhost:4566",
        }
        assert ti.aws_caller_account(env, "eu-west-1") == ("123456789012", "")
        assert seen["session"]["aws_access_key_id"] == "AKIAEXAMPLE"
        assert seen["session"]["region_name"] == "eu-west-1"
        assert seen["client"] == ("sts", "http://localhost:4566")

    def test_an_sts_failure_is_a_reason_named_by_type(self, monkeypatch):
        class _Session:
            def __init__(self, **kwargs):
                pass

            def client(self, name, endpoint_url=None):
                raise RuntimeError("secret detail that must not be printed")

        boto3 = types.ModuleType("boto3")
        boto3.session = types.SimpleNamespace(Session=_Session)
        monkeypatch.setitem(sys.modules, "boto3", boto3)
        account, why = ti.aws_caller_account({}, None)
        assert account == ""
        assert why == "sts GetCallerIdentity failed (RuntimeError)"

    def test_without_a_region_sts_still_gets_one(self, monkeypatch):
        seen: Dict[str, Any] = {}

        class _Session:
            def __init__(self, **kwargs):
                seen.update(kwargs)

            def client(self, name, endpoint_url=None):
                return types.SimpleNamespace(get_caller_identity=lambda: {"Account": "1"})

        boto3 = types.ModuleType("boto3")
        boto3.session = types.SimpleNamespace(Session=_Session)
        monkeypatch.setitem(sys.modules, "boto3", boto3)
        assert ti.aws_caller_account({}, None) == ("1", "")
        assert seen["region_name"] == "us-east-1"

    def test_without_boto3_it_says_how_to_install_it(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "boto3", None)
        account, why = ti.aws_caller_account({}, None)
        assert account == "" and "data-product-forge[aws]" in why


class TestGcp:
    @pytest.mark.parametrize(
        "module, env, project",
        [
            (_module("google", project="from-block"), {"GOOGLE_PROJECT": "env"}, "from-block"),
            ({}, {"GOOGLE_CLOUD_PROJECT": "b", "GCLOUD_PROJECT": "c"}, "b"),
            ({}, {"GOOGLE_PROJECT": "a", "GOOGLE_CLOUD_PROJECT": "b"}, "a"),
            ({}, {"CLOUDSDK_CORE_PROJECT": "d"}, "d"),
        ],
        ids=["provider-block-wins", "GOOGLE_CLOUD_PROJECT", "GOOGLE_PROJECT-first", "CLOUDSDK"],
    )
    def test_project_precedence(self, module, env, project):
        assert ti.target_identity("gcp", module, env)[0]["project"] == project

    def test_region(self):
        identity, _ = ti.target_identity("gcp", {}, {"GOOGLE_PROJECT": "p", "GCLOUD_REGION": "r"})
        assert identity == {"project": "p", "region": "r"}

    def test_the_credentials_own_project_is_not_read(self, monkeypatch):
        # The provider reads no such default, so binding it would bind the wrong project.
        monkeypatch.setitem(sys.modules, "google.auth", None)
        identity, unread = ti.target_identity("gcp", {}, {})
        assert identity == {}
        assert unread["project"].startswith("no provider project and no GOOGLE_PROJECT")


class TestOtherProviders:
    def test_snowflake_reads_the_names_the_plugin_derives(self):
        identity, unread = ti.target_identity(
            "snowflake",
            {},
            {"SNOWFLAKE_ORGANIZATION_NAME": "myorg", "SNOWFLAKE_ACCOUNT_NAME": "acct1"},
        )
        assert identity == {"organization": "MYORG", "account": "ACCT1"}
        assert unread == {}

    def test_databricks_host_is_normalised(self):
        identity, _ = ti.target_identity(
            "databricks", {}, {"DATABRICKS_HOST": "https://DBC-1.cloud.databricks.com/"}
        )
        assert identity == {"host": "https://dbc-1.cloud.databricks.com"}

    def test_an_unknown_provider_binds_nothing(self):
        assert ti.target_identity("local", {}, {"AWS_REGION": "x"}) == ({}, {})


class TestCompareAndDescribe:
    def test_changed_and_unverified(self):
        recorded = {"account": "1", "region": "eu-west-1"}
        assert ti.compare(recorded, {"account": "2", "region": "eu-west-1"}) == (["account"], [])
        assert ti.compare(recorded, {"account": "1"}) == ([], ["region"])

    def test_a_field_read_only_now_is_not_compared(self):
        assert ti.compare({"account": "1"}, {"account": "1", "region": "x"}) == ([], [])

    def test_describe(self):
        assert ti.describe("aws", {"region": "eu-west-1", "account": "1"}) == (
            "aws account 1, region eu-west-1"
        )
        assert ti.describe("local", {}) == "local (no target fields bound)"


_LOCK = """# This file is maintained automatically by "tofu init".
provider "registry.opentofu.org/hashicorp/aws" {
  version     = "6.73.0"
  constraints = ">= 6.0.0"
  hashes = [
    "h1:abc=",
  ]
}

provider "registry.opentofu.org/hashicorp/null" {
  version = "3.3.2"
  hashes = [
    "h1:def=",
  ]
}
"""


class TestLockedProviders:
    def test_reads_each_provider_and_its_version(self, tmp_path):
        (tmp_path / ".terraform.lock.hcl").write_text(_LOCK, encoding="utf-8")
        assert engine._locked_providers(str(tmp_path)) == {
            "registry.opentofu.org/hashicorp/aws": "6.73.0",
            "registry.opentofu.org/hashicorp/null": "3.3.2",
        }

    def test_no_lock_is_no_providers(self, tmp_path):
        assert engine._locked_providers(str(tmp_path)) == {}
