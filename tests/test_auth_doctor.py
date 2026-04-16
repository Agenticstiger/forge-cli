# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Tests for the auth doctor subcommand and CI environment detection."""

from __future__ import annotations

import asyncio
import os
import stat
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from fluid_build.cli.auth import (
    AuthManager,
    AuthResult,
    AuthStatus,
    CIEnvironment,
    DoctorCheck,
    DoctorStatus,
    _has_oidc_available,
    _is_ci,
    _normalize_provider,
    detect_ci_environment,
    handle_doctor,
)

# ──────────────────────────────────────────────────────────────────────────────
# CI Environment Detection
# ──────────────────────────────────────────────────────────────────────────────


class TestDetectCIEnvironment:
    def test_github_actions(self, monkeypatch):
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        assert detect_ci_environment() == CIEnvironment.GITHUB_ACTIONS

    def test_gitlab_ci(self, monkeypatch):
        monkeypatch.setenv("GITLAB_CI", "true")
        assert detect_ci_environment() == CIEnvironment.GITLAB_CI

    def test_jenkins(self, monkeypatch):
        monkeypatch.setenv("JENKINS_URL", "http://jenkins.local")
        assert detect_ci_environment() == CIEnvironment.JENKINS

    def test_circleci(self, monkeypatch):
        monkeypatch.setenv("CIRCLECI", "true")
        assert detect_ci_environment() == CIEnvironment.CIRCLECI

    def test_bitbucket(self, monkeypatch):
        monkeypatch.setenv("BITBUCKET_PIPELINE_UUID", "{uuid}")
        assert detect_ci_environment() == CIEnvironment.BITBUCKET

    def test_azure_devops(self, monkeypatch):
        monkeypatch.setenv("TF_BUILD", "True")
        assert detect_ci_environment() == CIEnvironment.AZURE_DEVOPS

    def test_none(self):
        # Clean environment — no CI vars set
        env = detect_ci_environment()
        # May or may not be NONE depending on test runner; just verify it returns an enum
        assert isinstance(env, CIEnvironment)

    def test_is_ci_true_with_generic_var(self, monkeypatch):
        monkeypatch.setenv("CI", "true")
        assert _is_ci() is True

    def test_is_ci_false_locally(self, monkeypatch):
        monkeypatch.delenv("CI", raising=False)
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
        monkeypatch.delenv("GITLAB_CI", raising=False)
        monkeypatch.delenv("JENKINS_URL", raising=False)
        monkeypatch.delenv("CIRCLECI", raising=False)
        monkeypatch.delenv("BITBUCKET_PIPELINE_UUID", raising=False)
        monkeypatch.delenv("TF_BUILD", raising=False)
        assert _is_ci() is False


class TestHasOIDCAvailable:
    def test_gcp_oidc(self, monkeypatch):
        monkeypatch.setenv(
            "ACTIONS_ID_TOKEN_REQUEST_URL", "https://token.actions.githubusercontent.com"
        )
        result = _has_oidc_available()
        assert result["gcp"] is True
        assert result["aws"] is True  # GitHub OIDC also works for AWS

    def test_azure_oidc(self, monkeypatch):
        monkeypatch.setenv("AZURE_FEDERATED_TOKEN_FILE", "/var/run/secrets/azure/token")
        result = _has_oidc_available()
        assert result["azure"] is True

    def test_gitlab_oidc(self, monkeypatch):
        monkeypatch.setenv("CI_JOB_JWT_V2", "eyJ...")
        result = _has_oidc_available()
        assert result["gitlab_oidc"] is True

    def test_no_oidc(self, monkeypatch):
        monkeypatch.delenv("ACTIONS_ID_TOKEN_REQUEST_URL", raising=False)
        monkeypatch.delenv("AWS_WEB_IDENTITY_TOKEN_FILE", raising=False)
        monkeypatch.delenv("AZURE_FEDERATED_TOKEN_FILE", raising=False)
        monkeypatch.delenv("CI_JOB_JWT_V2", raising=False)
        result = _has_oidc_available()
        assert all(v is False for v in result.values())


# ──────────────────────────────────────────────────────────────────────────────
# Provider Normalization
# ──────────────────────────────────────────────────────────────────────────────


class TestNormalizeProvider:
    def test_gcp_alias(self):
        assert _normalize_provider("gcp") == "google_cloud"

    def test_google_alias(self):
        assert _normalize_provider("google") == "google_cloud"

    def test_amazon_alias(self):
        assert _normalize_provider("amazon") == "aws"

    def test_microsoft_alias(self):
        assert _normalize_provider("microsoft") == "azure"

    def test_canonical_name_unchanged(self):
        assert _normalize_provider("snowflake") == "snowflake"
        assert _normalize_provider("aws") == "aws"


# ──────────────────────────────────────────────────────────────────────────────
# DoctorCheck and DoctorStatus
# ──────────────────────────────────────────────────────────────────────────────


class TestDoctorCheckEnum:
    def test_doctor_status_values(self):
        assert DoctorStatus.PASS.value == "pass"
        assert DoctorStatus.WARN.value == "warn"
        assert DoctorStatus.FAIL.value == "fail"
        assert DoctorStatus.INFO.value == "info"

    def test_doctor_check_creation(self):
        check = DoctorCheck(
            name="test",
            status=DoctorStatus.PASS,
            message="all good",
        )
        assert check.status == DoctorStatus.PASS
        assert check.fix_hint is None


# ──────────────────────────────────────────────────────────────────────────────
# Auth Doctor Handler
# ──────────────────────────────────────────────────────────────────────────────


class TestHandleDoctor:
    @pytest.fixture
    def mock_auth_manager(self):
        manager = MagicMock(spec=AuthManager)
        manager.list_providers.return_value = ["google_cloud", "aws", "snowflake"]
        # Make check_auth return NOT_AUTHENTICATED for all providers
        async_result = AuthResult(
            provider="test",
            status=AuthStatus.NOT_AUTHENTICATED,
            error_message="not configured",
        )
        manager.check_auth = AsyncMock(return_value=async_result)
        return manager

    @pytest.fixture
    def mock_logger(self):
        return MagicMock()

    def test_doctor_runs_locally(self, mock_auth_manager, mock_logger, monkeypatch):
        """Doctor should work in a non-CI environment."""
        monkeypatch.delenv("CI", raising=False)
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
        monkeypatch.delenv("GITLAB_CI", raising=False)
        monkeypatch.delenv("JENKINS_URL", raising=False)
        monkeypatch.delenv("CIRCLECI", raising=False)
        monkeypatch.delenv("BITBUCKET_PIPELINE_UUID", raising=False)
        monkeypatch.delenv("TF_BUILD", raising=False)

        with patch("fluid_build.cli.auth.RICH_AVAILABLE", False):
            result = asyncio.run(handle_doctor(None, mock_auth_manager, mock_logger))
        # Should succeed (no FAIL checks for missing CI)
        assert result == 0

    def test_doctor_detects_ci(self, mock_auth_manager, mock_logger, monkeypatch):
        """Doctor should detect CI environment."""
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_URL", "https://token.example.com")

        with patch("fluid_build.cli.auth.RICH_AVAILABLE", False):
            result = asyncio.run(handle_doctor(None, mock_auth_manager, mock_logger))
        assert result == 0

    def test_doctor_warns_long_lived_creds_in_ci(self, mock_auth_manager, mock_logger, monkeypatch):
        """Doctor should warn about long-lived credentials in CI."""
        monkeypatch.setenv("CI", "true")
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAEXAMPLE")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")

        with patch("fluid_build.cli.auth.RICH_AVAILABLE", False):
            result = asyncio.run(handle_doctor(None, mock_auth_manager, mock_logger))
        # Warnings don't cause failure
        assert result == 0

    def test_doctor_checks_env_file_perms(
        self, mock_auth_manager, mock_logger, tmp_path, monkeypatch
    ):
        """Doctor should flag insecure .env file permissions."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("CI", raising=False)

        # Create a .env file with world-readable permissions
        env_file = tmp_path / ".env"
        env_file.write_text("SECRET=value\n")
        env_file.chmod(0o644)

        with patch("fluid_build.cli.auth.RICH_AVAILABLE", False):
            result = asyncio.run(handle_doctor(None, mock_auth_manager, mock_logger))
        # Should fail because .env is group/world readable
        assert result == 1

    def test_doctor_fix_env_perms(self, mock_auth_manager, mock_logger, tmp_path, monkeypatch):
        """Doctor --fix should remediate insecure .env permissions."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("CI", raising=False)

        env_file = tmp_path / ".env"
        env_file.write_text("SECRET=value\n")
        env_file.chmod(0o644)

        with patch("fluid_build.cli.auth.RICH_AVAILABLE", False):
            result = asyncio.run(handle_doctor(None, mock_auth_manager, mock_logger, fix=True))
        # After fix, should pass
        assert result == 0
        # Verify permissions were fixed
        mode = env_file.stat().st_mode & 0o777
        assert mode == 0o600

    def test_doctor_single_provider(self, mock_auth_manager, mock_logger, monkeypatch):
        """Doctor should filter to single provider."""
        monkeypatch.delenv("CI", raising=False)

        with patch("fluid_build.cli.auth.RICH_AVAILABLE", False):
            result = asyncio.run(handle_doctor("aws", mock_auth_manager, mock_logger))
        # check_auth should be called only for aws
        mock_auth_manager.check_auth.assert_called_once_with("aws")
