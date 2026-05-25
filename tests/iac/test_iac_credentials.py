# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Unit tests for IaC credential plumbing."""

from __future__ import annotations

import pytest

from fluid_build.iac import get_iac_plugin
from fluid_build.iac.credentials import build_tofu_env, credential_report

pytestmark = pytest.mark.unit


class TestBuildTofuEnv:
    def test_returns_a_mutable_copy(self):
        base = {"PATH": "/usr/bin", "AWS_REGION": "us-east-1"}
        env = build_tofu_env(base)
        assert env == base
        env["EXTRA"] = "1"
        assert "EXTRA" not in base  # a copy, not an alias

    def test_defaults_to_process_environment(self):
        assert isinstance(build_tofu_env(), dict)

    def test_empty_base_is_respected(self):
        # An explicit empty mapping must not fall back to os.environ.
        assert build_tofu_env({}) == {}


class TestCredentialReport:
    def test_splits_present_and_absent(self):
        plugin = get_iac_plugin("aws")
        env = {"AWS_ACCESS_KEY_ID": "AKIA", "AWS_REGION": "us-east-1"}
        present, absent = credential_report(plugin, env)
        assert "AWS_ACCESS_KEY_ID" in present
        assert "AWS_REGION" in present
        assert "AWS_SECRET_ACCESS_KEY" in absent

    def test_empty_env_marks_all_absent(self):
        plugin = get_iac_plugin("gcp")
        present, absent = credential_report(plugin, {})
        assert present == []
        assert set(absent) == set(plugin.credential_env_vars)

    def test_blank_value_counts_as_absent(self):
        plugin = get_iac_plugin("snowflake")
        present, absent = credential_report(plugin, {"SNOWFLAKE_USER": ""})
        assert "SNOWFLAKE_USER" in absent
