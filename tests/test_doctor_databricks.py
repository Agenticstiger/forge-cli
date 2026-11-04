# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Doctor's Databricks row reports what ``fluid apply`` would do (same policy)."""

import importlib.metadata as md
import os

import pytest

from fluid_build.cli.doctor import _check_databricks_readiness

HOST = "https://example.cloud.databricks.com"
SECRET = "fake-" + "x" * 12 + "-secret"  # pragma: allowlist secret


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in list(os.environ):
        if k.startswith("DATABRICKS_"):
            monkeypatch.delenv(k)


def test_nothing_set_is_info_and_ok():
    row = _check_databricks_readiness()
    assert row["ok"] is True
    assert "Not configured" in row["status"]


def test_host_and_token_ok_without_leaking(monkeypatch):
    monkeypatch.setenv("DATABRICKS_HOST", HOST)
    monkeypatch.setenv("DATABRICKS_TOKEN", SECRET)
    monkeypatch.setenv("DATABRICKS_WAREHOUSE_ID", "wh1")
    row = _check_databricks_readiness()
    text = str(row)
    assert row["ok"] is True and "Configured" in row["status"]
    assert HOST in text and "pat" in text
    assert SECRET not in text


def test_profile_host_mismatch_warns_with_fix(monkeypatch, tmp_path):
    cfg = tmp_path / "cfg"
    cfg.write_text("[work]\nhost = https://other.cloud.databricks.com\ntoken = " + SECRET + "\n")
    monkeypatch.setenv("DATABRICKS_HOST", HOST)
    monkeypatch.setenv("DATABRICKS_CONFIG_PROFILE", "work")
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(cfg))
    monkeypatch.setenv("DATABRICKS_WAREHOUSE_ID", "wh1")
    row = _check_databricks_readiness()
    assert row["ok"] is True and "Needs attention" in row["status"]
    assert "refused" in row["details"] and "Fix:" in row["details"]
    assert SECRET not in str(row)


def test_missing_warehouse_warns(monkeypatch):
    monkeypatch.setenv("DATABRICKS_HOST", HOST)
    monkeypatch.setenv("DATABRICKS_TOKEN", SECRET)
    row = _check_databricks_readiness()
    assert "Needs attention" in row["status"]
    assert "DATABRICKS_WAREHOUSE_ID not set" in row["details"]


def test_sdk_missing_is_informational(monkeypatch):
    monkeypatch.setenv("DATABRICKS_HOST", HOST)
    monkeypatch.setenv("DATABRICKS_TOKEN", SECRET)
    monkeypatch.setenv("DATABRICKS_WAREHOUSE_ID", "wh1")
    real = md.version

    def fake(name):
        if name == "databricks-sdk":
            raise md.PackageNotFoundError(name)
        return real(name)

    monkeypatch.setattr(md, "version", fake)
    row = _check_databricks_readiness()
    assert row["ok"] is True and "Configured" in row["status"]
    assert "pip install 'data-product-forge[databricks]'" in row["details"]


def test_the_install_command_keeps_its_extra(monkeypatch, capsys):
    # Rich read `[databricks]` as markup and printed `pip install 'data-product-forge'`,
    # which installs no databricks-sdk.
    from fluid_build.cli.doctor import _print_feature_checks

    monkeypatch.setenv("COLUMNS", "200")
    details = "databricks-sdk not installed: pip install 'data-product-forge[databricks]'"
    _print_feature_checks(
        [
            {
                "check": "Databricks Provider",
                "status": "✅ Configured",
                "ok": True,
                "details": details,
            }
        ],
        verbose=True,
    )
    assert "pip install 'data-product-forge[databricks]'" in capsys.readouterr().out
