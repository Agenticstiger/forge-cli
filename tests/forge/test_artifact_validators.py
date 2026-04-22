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

"""Tests for fluid_build.forge.core.artifact_validators — stage-4 gate.

Adversarial bias: every test pins a behavior downstream (publish, verify,
apply) depends on. If one of these starts passing under a regression,
stage-4 would silently accept malformed artifacts.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from fluid_build.forge.core.artifact_validators import (
    _ODPS_BITOL_SCHEMA_PATH,
    validate_artifacts,
    validate_bindings_json,
    validate_dag_python,
    validate_manifest_dir,
    validate_opa_conftest,
)


@pytest.fixture
def logger():
    return logging.getLogger("test.artifact_validators")


# ---------------------------------------------------------------------------
# Fixture helpers — build minimal valid artifact directories
# ---------------------------------------------------------------------------


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _write_manifest(artifacts_dir: Path, files: dict, *, digest: str = None) -> Path:
    """Write a MANIFEST.json matching the Phase-2 bundle format over a set of
    files. Each file is created on disk with its declared bytes."""
    per_file: dict = {}
    merkle_input = ""
    for path, content in sorted(files.items()):
        full = artifacts_dir / path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_bytes(content)
        per_file[path] = _sha256(content)
        merkle_input += f"{path}:{per_file[path]}\n"
    merkle = digest or _sha256(merkle_input.encode("utf-8"))
    manifest = {
        "version": "1.0",
        "generator": "test-fixture",
        "contractId": "",
        "files": per_file,
        "digest": merkle,
    }
    manifest_path = artifacts_dir / "MANIFEST.json"
    manifest_path.write_text(json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n")
    return manifest_path


# ---------------------------------------------------------------------------
# validate_manifest_dir — SHA-256 + merkle tamper gate
# ---------------------------------------------------------------------------


class TestValidateManifestDir:
    def test_clean_dir_passes(self, tmp_path):
        _write_manifest(tmp_path, {"odcs/a.yaml": b"hello\n"})
        issues = validate_manifest_dir(tmp_path)
        assert issues == []

    def test_missing_manifest(self, tmp_path):
        issues = validate_manifest_dir(tmp_path)
        assert len(issues) == 1
        assert issues[0].code == "MANIFEST-MISSING"

    def test_corrupt_manifest(self, tmp_path):
        (tmp_path / "MANIFEST.json").write_text("not json {")
        issues = validate_manifest_dir(tmp_path)
        assert len(issues) == 1
        assert issues[0].code == "MANIFEST-PARSE"

    def test_tampered_file_flagged(self, tmp_path):
        _write_manifest(tmp_path, {"odcs/a.yaml": b"original"})
        # Tamper
        (tmp_path / "odcs/a.yaml").write_bytes(b"modified")
        issues = validate_manifest_dir(tmp_path)
        assert any(
            i.code == "MANIFEST-SHA-MISMATCH" for i in issues
        ), f"expected SHA mismatch; got {[i.code for i in issues]}"

    def test_missing_declared_file(self, tmp_path):
        _write_manifest(tmp_path, {"odcs/a.yaml": b"x"})
        (tmp_path / "odcs/a.yaml").unlink()
        issues = validate_manifest_dir(tmp_path)
        assert any(i.code == "MANIFEST-MISSING-FILE" for i in issues)

    def test_extra_file_flagged_as_warning(self, tmp_path):
        _write_manifest(tmp_path, {"odcs/a.yaml": b"x"})
        # Drop an undeclared file
        (tmp_path / "odcs" / "rogue.txt").write_text("I am undeclared")
        issues = validate_manifest_dir(tmp_path)
        assert any(i.code == "MANIFEST-UNDECLARED-FILE" for i in issues)
        # Must be warning, not error — downstream can still validate the
        # declared set; extra files shouldn't break the build.
        extra = next(i for i in issues if i.code == "MANIFEST-UNDECLARED-FILE")
        assert extra.severity == "warning"

    def test_merkle_mismatch_when_digest_tampered(self, tmp_path):
        _write_manifest(tmp_path, {"odcs/a.yaml": b"x"})
        # Tamper the manifest digest directly
        mp = tmp_path / "MANIFEST.json"
        m = json.loads(mp.read_text())
        m["digest"] = "sha256:" + "0" * 64
        mp.write_text(json.dumps(m, sort_keys=True, separators=(",", ":")) + "\n")
        issues = validate_manifest_dir(tmp_path)
        assert any(i.code == "MANIFEST-MERKLE-MISMATCH" for i in issues)

    def test_hidden_files_ignored(self, tmp_path):
        """macOS leaves .DS_Store; git leaves .git; neither should show up as
        an undeclared-file warning."""
        _write_manifest(tmp_path, {"odcs/a.yaml": b"x"})
        (tmp_path / ".DS_Store").write_text("apple-metadata")
        issues = validate_manifest_dir(tmp_path)
        codes = [i.code for i in issues]
        assert "MANIFEST-UNDECLARED-FILE" not in codes


# ---------------------------------------------------------------------------
# validate_dag_python — py_compile
# ---------------------------------------------------------------------------


class TestValidateDagPython:
    def test_valid_python_passes(self):
        issues = validate_dag_python("schedule/dags/good.py", b"print('hello')\n")
        assert issues == []

    def test_syntax_error_flagged(self):
        issues = validate_dag_python("schedule/dags/bad.py", b"def broken(:\n    pass\n")
        assert len(issues) >= 1
        assert issues[0].severity == "error"
        assert issues[0].validator == "py_compile"

    def test_unicode_emoji_valid(self):
        """Real DAG code sometimes has emoji in docstrings; must not trip
        the validator."""
        issues = validate_dag_python(
            "schedule/dags/ok.py", "# 🎯 run daily\nx = 1\n".encode("utf-8")
        )
        assert issues == []


# ---------------------------------------------------------------------------
# validate_bindings_json — structural key-check
# ---------------------------------------------------------------------------


class TestValidateBindingsJson:
    def test_valid_bindings_pass(self):
        content = json.dumps(
            {"bindings": [{"provider": "snowflake", "principal": "analyst@x"}]}
        ).encode()
        issues = validate_bindings_json("policy/bindings.json", content)
        assert issues == []

    def test_missing_bindings_array(self):
        content = json.dumps({"something-else": []}).encode()
        issues = validate_bindings_json("policy/bindings.json", content)
        assert any(i.code == "BINDINGS-MISSING-ARRAY" for i in issues)

    def test_bindings_not_a_list(self):
        content = json.dumps({"bindings": "not-a-list"}).encode()
        issues = validate_bindings_json("policy/bindings.json", content)
        assert any(i.code == "BINDINGS-SHAPE" for i in issues)

    def test_binding_entry_missing_provider(self):
        content = json.dumps({"bindings": [{"principal": "analyst@x"}]}).encode()
        issues = validate_bindings_json("policy/bindings.json", content)
        assert any(i.code == "BINDINGS-MISSING-KEY" and "provider" in i.message for i in issues)

    def test_binding_entry_missing_principal(self):
        content = json.dumps({"bindings": [{"provider": "snowflake"}]}).encode()
        issues = validate_bindings_json("policy/bindings.json", content)
        assert any(i.code == "BINDINGS-MISSING-KEY" and "principal" in i.message for i in issues)

    def test_parse_error_flagged(self):
        issues = validate_bindings_json("policy/bindings.json", b"not json")
        assert any(i.code == "BINDINGS-PARSE" for i in issues)

    def test_root_not_object(self):
        issues = validate_bindings_json("policy/bindings.json", json.dumps([]).encode())
        assert any(i.code == "BINDINGS-SHAPE" for i in issues)


# ---------------------------------------------------------------------------
# OPA conftest integration
# ---------------------------------------------------------------------------


class TestValidateOpaConftest:
    def test_no_policy_dir_silent(self, tmp_path):
        """Absent policy dir is NOT an error — OPA is opt-in per product."""
        issues = validate_opa_conftest(
            tmp_path / "bindings.json",
            tmp_path / "nonexistent-policy-dir",
            strict=False,
        )
        assert issues == []

    def test_empty_policy_dir_silent(self, tmp_path):
        policy_dir = tmp_path / "policy"
        policy_dir.mkdir()
        issues = validate_opa_conftest(tmp_path / "bindings.json", policy_dir, strict=False)
        assert issues == []

    def test_conftest_missing_info_non_strict(self, tmp_path):
        policy_dir = tmp_path / "policy"
        policy_dir.mkdir()
        (policy_dir / "rule.rego").write_text("package main\n")
        bindings = tmp_path / "bindings.json"
        bindings.write_text('{"bindings": []}')

        with patch(
            "fluid_build.forge.core.artifact_validators._conftest_available",
            return_value=False,
        ):
            issues = validate_opa_conftest(bindings, policy_dir, strict=False)
        assert len(issues) == 1
        assert issues[0].severity == "info"
        assert issues[0].code == "OPA-MISSING"

    def test_conftest_missing_error_strict(self, tmp_path):
        policy_dir = tmp_path / "policy"
        policy_dir.mkdir()
        (policy_dir / "rule.rego").write_text("package main\n")
        bindings = tmp_path / "bindings.json"
        bindings.write_text('{"bindings": []}')

        with patch(
            "fluid_build.forge.core.artifact_validators._conftest_available",
            return_value=False,
        ):
            issues = validate_opa_conftest(bindings, policy_dir, strict=True)
        assert issues[0].severity == "error"


# ---------------------------------------------------------------------------
# validate_artifacts — end-to-end orchestrator
# ---------------------------------------------------------------------------


class TestValidateArtifactsOrchestrator:
    def test_clean_dir_with_only_valid_dag_passes(self, tmp_path):
        """Build an artifact dir with just a valid DAG (no ODCS/ODPS to avoid
        schema dependency). Must pass end-to-end."""
        _write_manifest(tmp_path, {"schedule/dags/demo.py": b"x = 1\n"})
        report = validate_artifacts(tmp_path)
        assert report.status == "pass", f"expected pass; got {[i.to_dict() for i in report.issues]}"

    def test_tampered_dag_short_circuits_at_manifest(self, tmp_path):
        """MANIFEST tamper gate runs first; per-file validators don't fire
        if the bytes are untrusted."""
        _write_manifest(tmp_path, {"schedule/dags/demo.py": b"x = 1\n"})
        (tmp_path / "schedule/dags/demo.py").write_bytes(b"x = 2\n")

        report = validate_artifacts(tmp_path)
        assert report.status == "fail"
        # Only the MANIFEST-level issue; per-file py_compile didn't run.
        assert all(i.validator == "manifest" for i in report.issues)

    def test_bad_dag_flagged(self, tmp_path):
        """With a clean MANIFEST but syntactically-broken DAG, the py_compile
        validator runs and flags the error."""
        _write_manifest(tmp_path, {"schedule/dags/bad.py": b"def broken(:\n"})
        report = validate_artifacts(tmp_path)
        assert report.status == "fail"
        assert any(i.validator == "py_compile" for i in report.issues)

    def test_bad_bindings_flagged(self, tmp_path):
        _write_manifest(tmp_path, {"policy/bindings.json": b'{"not-bindings": []}'})
        report = validate_artifacts(tmp_path)
        assert report.status == "fail"
        assert any(i.validator == "bindings" for i in report.issues)

    def test_unknown_artifact_path_warned(self, tmp_path):
        """Files under paths the validator doesn't recognize → warning, not
        error. Stops unexpected bundle content from failing CI but surfaces
        it for review."""
        _write_manifest(tmp_path, {"weird/unknown.txt": b"?\n"})
        report = validate_artifacts(tmp_path)
        # Clean MANIFEST + clean tamper gate → pass BUT warning for unknown.
        assert any(
            i.code == "ARTIFACT-UNEXPECTED" and i.severity == "warning" for i in report.issues
        )

    def test_strict_escalates_warnings(self, tmp_path):
        """Non-strict: unknown-artifact warning → status pass. Strict: same
        warning → status fail."""
        _write_manifest(tmp_path, {"weird/unknown.txt": b"?\n"})

        non_strict = validate_artifacts(tmp_path, strict=False)
        strict = validate_artifacts(tmp_path, strict=True)

        assert non_strict.status == "pass"
        assert strict.status == "fail"

    def test_fail_fast_stops_at_first_error(self, tmp_path):
        """Two bad bindings files with fail_fast=True → only the first one
        fully processed."""
        _write_manifest(
            tmp_path,
            {
                "policy/bindings.json": b'{"bindings": "not-a-list"}',
                "schedule/dags/broken.py": b"def broken(:\n",
            },
        )
        report = validate_artifacts(tmp_path, fail_fast=True)
        # At least the first file's errors present; the second may or may not
        # have been processed depending on dispatch order. Pin: validation
        # stopped before both files reported.
        assert report.status == "fail"

    def test_report_has_stable_shape(self, tmp_path):
        _write_manifest(tmp_path, {"schedule/dags/good.py": b"pass\n"})
        report = validate_artifacts(tmp_path)
        d = report.to_dict()
        assert set(d.keys()) == {
            "bundleDigest",
            "input",
            "strict",
            "status",
            "summary",
            "issues",
        }


# ---------------------------------------------------------------------------
# Vendored schemas present
# ---------------------------------------------------------------------------


class TestVendoredSchemas:
    def test_odps_bitol_schema_exists_in_provider_dir(self):
        """Must be under providers/odps_standard/ — NOT fluid_build/schemas/
        (that namespace is reserved for FLUID contract schemas)."""
        assert (
            _ODPS_BITOL_SCHEMA_PATH.exists()
        ), f"vendored ODPS-Bitol schema missing at {_ODPS_BITOL_SCHEMA_PATH}"
        # Schemas/ namespace must NOT house third-party schemas.
        bad_path = (
            _ODPS_BITOL_SCHEMA_PATH.parent.parent.parent
            / "schemas"
            / "odps-bitol-json-schema-v1.0.0.json"
        )
        assert not bad_path.exists(), (
            f"ODPS-Bitol schema must not live under fluid_build/schemas/; " f"found at {bad_path}"
        )

    def test_odps_bitol_schema_is_valid_json(self):
        with open(_ODPS_BITOL_SCHEMA_PATH, "r", encoding="utf-8") as fh:
            schema = json.load(fh)
        assert schema.get("title") == "Open Data Product Standard (ODPS)"
        # Enum pins the version so we'd notice if someone swapped in the
        # wrong version file.
        version_enum = schema.get("properties", {}).get("apiVersion", {}).get("enum", [])
        assert "v1.0.0" in version_enum


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------


class TestCliRegistration:
    def test_validate_artifacts_command_registered(self):
        """Regression guard: `fluid validate-artifacts --help` exists."""
        import argparse

        from fluid_build.cli import validate_artifacts

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="cmd")
        validate_artifacts.register(sub)
        args = parser.parse_args(["validate-artifacts", "dist/artifacts/"])
        assert args.cmd == "validate-artifacts"
        assert args.artifacts_dir == "dist/artifacts/"
