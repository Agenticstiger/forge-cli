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


# ─────────── read-before-cap (stat-before-read) OOM gap (FIX 2) ──────────
#
# ``load_yaml_safe``'s byte cap only fires AFTER the whole file is in memory,
# so a multi-GB file would already have OOM'd the process. Both loaders now
# stat the file FIRST and skip/reject oversized ones BEFORE ``read_text`` —
# the cap is meaningless against a huge file otherwise.


class TestUpstreamDiscoveryStatBeforeRead:
    def test_oversized_file_not_read_into_memory(self, tmp_path, monkeypatch):
        """An oversized discovered contract is rejected by the stat gate
        WITHOUT ``read_text`` ever being called (no read-before-cap OOM)."""
        from fluid_build.util import upstream_discovery as ud
        from fluid_build.util.safe_yaml import MAX_YAML_BYTES

        monkeypatch.delenv("FLUID_UPSTREAM_CONTRACTS", raising=False)
        huge = _write(tmp_path / "huge", "id: huge.v1\n")
        good = _write(tmp_path / "good", "id: good.v1\nexposes: []\n")

        real_stat = Path.stat

        def fake_stat(self, *a, **k):
            st = real_stat(self, *a, **k)
            if self == huge:
                # Pretend the file is 1 byte over the cap without writing GBs.
                return os.stat_result(
                    (st.st_mode, st.st_ino, st.st_dev, st.st_nlink, st.st_uid, st.st_gid)
                    + (MAX_YAML_BYTES + 1,)
                    + (st.st_atime, st.st_mtime, st.st_ctime)
                )
            return st

        # If ``read_text`` is ever called on the oversized file, fail loud:
        # that is the read-before-cap OOM bug this fix closes.
        real_read_text = Path.read_text

        def guarded_read_text(self, *a, **k):
            if self == huge:
                raise AssertionError(
                    "read_text called on oversized file — stat-before-read gate missed it"
                )
            return real_read_text(self, *a, **k)

        monkeypatch.setattr(Path, "stat", fake_stat)
        monkeypatch.setattr(Path, "read_text", guarded_read_text)

        idx = ud.discover_upstream_products(tmp_path)
        assert "huge.v1" not in idx  # oversized file skipped
        assert "good.v1" in idx  # benign file still read+indexed


class TestValidationYamlFileStatBeforeRead:
    def test_oversized_yaml_not_read_into_memory(self, tmp_path, monkeypatch):
        """``_validate_yaml_file`` rejects an oversized YAML via the stat
        gate (an ERROR issue) WITHOUT ``read_text`` ever being called."""
        from fluid_build.forge.core.validation import ProjectValidator, ValidationLevel
        from fluid_build.util.safe_yaml import MAX_YAML_BYTES

        target = _write(tmp_path, "a: 1\n", name="huge.yaml")
        v = ProjectValidator(str(tmp_path))

        real_stat = Path.stat

        def fake_stat(self, *a, **k):
            st = real_stat(self, *a, **k)
            if self == target:
                return os.stat_result(
                    (st.st_mode, st.st_ino, st.st_dev, st.st_nlink, st.st_uid, st.st_gid)
                    + (MAX_YAML_BYTES + 1,)
                    + (st.st_atime, st.st_mtime, st.st_ctime)
                )
            return st

        real_read_text = Path.read_text

        def guarded_read_text(self, *a, **k):
            if self == target:
                raise AssertionError(
                    "read_text called on oversized file — stat-before-read gate missed it"
                )
            return real_read_text(self, *a, **k)

        monkeypatch.setattr(Path, "stat", fake_stat)
        monkeypatch.setattr(Path, "read_text", guarded_read_text)

        v._validate_yaml_file(target)
        assert any(i.level == ValidationLevel.ERROR for i in v.issues)


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
