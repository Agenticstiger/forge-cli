# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Tests for ``fluid_build.util.upstream_discovery``."""

from __future__ import annotations

from pathlib import Path

import yaml

from fluid_build.util.upstream_discovery import (
    collect_search_roots,
    discover_upstream_products,
    project_upstream_for_prompt,
)


def _write_contract(dir_path: Path, data: dict) -> Path:
    dir_path.mkdir(parents=True, exist_ok=True)
    path = dir_path / "contract.fluid.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


class TestCollectSearchRoots:
    def test_returns_empty_when_no_inputs(self, monkeypatch):
        monkeypatch.delenv("FLUID_UPSTREAM_CONTRACTS", raising=False)
        assert collect_search_roots(None) == []

    def test_workspace_root_first(self, tmp_path, monkeypatch):
        monkeypatch.delenv("FLUID_UPSTREAM_CONTRACTS", raising=False)
        roots = collect_search_roots(tmp_path)
        assert roots == [tmp_path.resolve()]

    def test_env_var_adds_extra_roots(self, tmp_path, monkeypatch):
        a = tmp_path / "a"
        b = tmp_path / "b"
        a.mkdir()
        b.mkdir()
        monkeypatch.setenv("FLUID_UPSTREAM_CONTRACTS", f"{a}:{b}")
        roots = collect_search_roots(None)
        assert a.resolve() in roots
        assert b.resolve() in roots

    def test_deduplicates_resolved_paths(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FLUID_UPSTREAM_CONTRACTS", str(tmp_path))
        roots = collect_search_roots(tmp_path)
        assert len(roots) == 1

    def test_skips_nonexistent(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FLUID_UPSTREAM_CONTRACTS", "/nonexistent/path/xxxxx")
        roots = collect_search_roots(tmp_path)
        assert roots == [tmp_path.resolve()]


class TestDiscoverUpstreamProducts:
    def test_finds_single_contract(self, tmp_path, monkeypatch):
        monkeypatch.delenv("FLUID_UPSTREAM_CONTRACTS", raising=False)
        _write_contract(
            tmp_path / "product_a",
            {"id": "bronze.party_v1", "exposes": [{"exposeId": "party"}]},
        )
        idx = discover_upstream_products(tmp_path)
        assert "bronze.party_v1" in idx
        assert idx["bronze.party_v1"]["id"] == "bronze.party_v1"

    def test_ignores_files_without_id(self, tmp_path, monkeypatch):
        monkeypatch.delenv("FLUID_UPSTREAM_CONTRACTS", raising=False)
        _write_contract(tmp_path / "no_id", {"kind": "DataProduct"})
        idx = discover_upstream_products(tmp_path)
        assert idx == {}

    def test_tolerates_bad_yaml(self, tmp_path, monkeypatch):
        monkeypatch.delenv("FLUID_UPSTREAM_CONTRACTS", raising=False)
        broken = tmp_path / "broken"
        broken.mkdir()
        (broken / "contract.fluid.yaml").write_text(": not: valid: yaml: [\n")
        _write_contract(
            tmp_path / "good",
            {"id": "bronze.good_v1", "exposes": []},
        )
        idx = discover_upstream_products(tmp_path)
        assert "bronze.good_v1" in idx

    def test_skips_ignored_dirs(self, tmp_path, monkeypatch):
        monkeypatch.delenv("FLUID_UPSTREAM_CONTRACTS", raising=False)
        _write_contract(
            tmp_path / ".venv" / "contracts",
            {"id": "hidden.v1", "exposes": []},
        )
        _write_contract(
            tmp_path / "visible",
            {"id": "visible.v1", "exposes": []},
        )
        idx = discover_upstream_products(tmp_path)
        assert "hidden.v1" not in idx
        assert "visible.v1" in idx

    def test_env_var_extra_paths(self, tmp_path, monkeypatch):
        extra = tmp_path / "extra"
        _write_contract(extra / "p", {"id": "env.v1", "exposes": []})
        monkeypatch.setenv("FLUID_UPSTREAM_CONTRACTS", str(extra))
        other = tmp_path / "other_ws"
        other.mkdir()
        idx = discover_upstream_products(other)
        assert "env.v1" in idx


class TestProjectUpstreamForPrompt:
    def test_compacts_expose_schema(self):
        contracts = {
            "bronze.party_v1": {
                "id": "bronze.party_v1",
                "name": "Party Bronze",
                "description": "TM Forum SID party data",
                "domain": "telco",
                "exposes": [
                    {
                        "exposeId": "party_source",
                        "kind": "table",
                        "title": "Party Source",
                        "binding": {
                            "platform": "snowflake",
                            "format": "snowflake_table",
                            "location": {
                                "account": "xy12345.eu-west-1",
                                "database": "TELCO",
                                "schema": "RAW",
                                "table": "PARTY",
                            },
                        },
                        "contract": {
                            "schema": [
                                {"name": "PARTY_ID", "type": "STRING", "required": True},
                                {"name": "STATUS", "type": "STRING", "required": False},
                            ],
                            "dq": {"rules": []},
                        },
                    }
                ],
            }
        }

        projection = project_upstream_for_prompt(contracts)
        product = projection["bronze.party_v1"]
        assert product["name"] == "Party Bronze"
        assert product["domain"] == "telco"
        expose = product["exposes"]["party_source"]
        assert expose["kind"] == "table"
        assert expose["platform"] == "snowflake"
        # account is sensitive — dropped from prompt location.
        assert "account" not in expose["location"]
        assert expose["location"]["database"] == "TELCO"
        assert expose["location"]["schema"] == "RAW"
        assert expose["location"]["table"] == "PARTY"
        assert expose["schema"] == [
            {"name": "PARTY_ID", "type": "STRING", "required": True},
            {"name": "STATUS", "type": "STRING", "required": False},
        ]

    def test_drops_products_without_exposes(self):
        contracts = {
            "lone.v1": {"id": "lone.v1", "exposes": []},
            "real.v1": {
                "id": "real.v1",
                "exposes": [{"exposeId": "thing", "contract": {"schema": []}}],
            },
        }
        projection = project_upstream_for_prompt(contracts)
        assert "lone.v1" not in projection
        assert "real.v1" in projection

    def test_handles_missing_schema_gracefully(self):
        contracts = {
            "skeleton.v1": {
                "id": "skeleton.v1",
                "exposes": [{"exposeId": "x", "kind": "table"}],
            }
        }
        projection = project_upstream_for_prompt(contracts)
        # exposes with no schema still listed, just without schema key.
        assert "x" in projection["skeleton.v1"]["exposes"]
        assert "schema" not in projection["skeleton.v1"]["exposes"]["x"]
