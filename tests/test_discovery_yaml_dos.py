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

"""Regression tests for the discovery/artifact YAML billion-laughs fixes.

FINDING 4 (LOW): bare ``yaml.safe_load`` on YAML files the user did NOT
explicitly name (discovered workspace contracts, pulled mesh repos,
tree-scanned artifacts) bypasses ``util/safe_yaml.load_yaml_safe``'s
50-alias + 5 MiB caps — a "billion laughs" anchor-expansion payload can
OOM the process. The primary site is
``util/upstream_discovery.discover_upstream_products``; the sibling
discovery/artifact readers (``forge/core/validation._validate_yaml_file``,
``forge/core/artifact_validators``) are routed through ``load_yaml_safe``
too. The explicitly-named primary-contract loaders (``util/io.load_contract``,
``validation._validate_contract``) are intentionally left untouched.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest


def _billion_laughs(*, alias_count: int = 60, contract_id: str = "evil.v1") -> str:
    """A small doc whose alias count exceeds MAX_YAML_ALIASES (50)."""
    refs = ",".join(["*a"] * alias_count)
    return f'a: &a ["lol"]\nboom: [{refs}]\nid: {contract_id}\n'


def _write(dir_path: Path, text: str, name: str = "contract.fluid.yaml") -> Path:
    dir_path.mkdir(parents=True, exist_ok=True)
    p = dir_path / name
    p.write_text(text)
    return p


# ──────────────── primary site: upstream discovery ──────────────────────


class TestUpstreamDiscoveryDoS:
    def test_billion_laughs_contract_skipped(self, tmp_path, monkeypatch):
        from fluid_build.util.upstream_discovery import discover_upstream_products

        monkeypatch.delenv("FLUID_UPSTREAM_CONTRACTS", raising=False)
        _write(tmp_path / "evil", _billion_laughs())
        _write(tmp_path / "good", "id: good.v1\nexposes: []\n")

        idx = discover_upstream_products(tmp_path)
        # Hostile file skipped (tolerant-by-design), benign file kept.
        assert "evil.v1" not in idx
        assert "good.v1" in idx

    def test_oversized_contract_skipped(self, tmp_path, monkeypatch):
        from fluid_build.util.safe_yaml import MAX_YAML_BYTES
        from fluid_build.util.upstream_discovery import discover_upstream_products

        monkeypatch.delenv("FLUID_UPSTREAM_CONTRACTS", raising=False)
        big = "id: huge.v1\n" + ("#" * (MAX_YAML_BYTES + 16))
        _write(tmp_path / "huge", big)
        _write(tmp_path / "good", "id: good.v1\nexposes: []\n")

        idx = discover_upstream_products(tmp_path)
        assert "huge.v1" not in idx
        assert "good.v1" in idx


# ──────────────── sibling reader: tree-scan validator ───────────────────


class TestValidationYamlFileDoS:
    def test_tree_scanned_yaml_uses_safe_loader(self, tmp_path):
        """``_validate_yaml_file`` runs over every ``*.yaml`` under the
        project tree; a billion-laughs file must be rejected via the
        capped loader, surfaced as a validation issue (never an OOM)."""
        from fluid_build.forge.core.validation import (
            ProjectValidator,
            ValidationLevel,
        )

        v = ProjectValidator(str(tmp_path))
        evil = _write(tmp_path, _billion_laughs(), name="evil.yaml")
        v._validate_yaml_file(evil)
        # The capped loader raised UnsafeYamlError → recorded as an ERROR
        # issue rather than OOMing the validator.
        assert any(i.level == ValidationLevel.ERROR for i in v.issues)

    def test_valid_yaml_passes(self, tmp_path):
        from fluid_build.forge.core.validation import ProjectValidator

        v = ProjectValidator(str(tmp_path))
        ok = _write(tmp_path, "a: 1\nb: [1,2,3]\n", name="fine.yaml")
        v._validate_yaml_file(ok)
        assert v.issues == []


# ──────────────── sibling reader: artifact validator ────────────────────


class TestArtifactValidatorDoS:
    def test_yaml_artifact_billion_laughs_reports_parse_error(self):
        """The shared artifact JSON-schema validator parses untrusted
        artifact content; a billion-laughs YAML body must be rejected by
        ``load_yaml_safe`` and surfaced as a parse issue, not OOM."""
        from fluid_build.forge.core import artifact_validators as av

        # Minimal non-None schema so we reach the parse step.
        schema = {"type": "object"}
        content = _billion_laughs().encode("utf-8")
        issues = av._validate_against_schema(
            "generated/x.yaml",
            content,
            schema,
            validator_name="test",
            code_prefix="TEST",
        )
        # A parse issue is raised (the UnsafeYamlError path), not a crash.
        assert any("PARSE" in (i.code or "") for i in issues)
