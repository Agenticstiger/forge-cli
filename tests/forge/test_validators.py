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

"""Tests for fluid_build.forge.core.validators — stage-2 pipeline gate.

Adversarial bias: every test pins a specific behavior the CI pipeline
depends on. A passing test under a behavior regression means stage 2
would silently let bad bundles through.
"""

from __future__ import annotations

import gzip
import io
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from fluid_build.forge.core.bundle import SOURCE_SENTINEL, build_bundle_tgz
from fluid_build.forge.core.validators import (
    BundleValidationReport,
    ValidationIssue,
    infer_sqlglot_dialect,
    unwrap_source_pointers,
    validate_bundle,
    validate_openapi,
    validate_sql,
)

# ---------------------------------------------------------------------------
# Permissive stub schema — tests that don't care about JSON Schema results
# use this to isolate the non-schema validators.
# ---------------------------------------------------------------------------


class _PermissiveSchema:
    def get_schema(self, version):
        # type: object → accepts anything; we test schema behavior in separate
        # cases that use the real FluidSchemaManager.
        return {"type": "object"}


# ---------------------------------------------------------------------------
# unwrap_source_pointers
# ---------------------------------------------------------------------------


class TestUnwrapSourcePointers:
    """{'$source': 'sources/...'} → parsed content of the referenced file."""

    def _resolver(self, files):
        def _get(path):
            if path not in files:
                raise ValueError(f"missing: {path}")
            return files[path]

        return _get

    def test_sql_pointer_becomes_string(self):
        doc = {"sql": {SOURCE_SENTINEL: "sources/sql/x.sql"}}
        out = unwrap_source_pointers(doc, self._resolver({"sources/sql/x.sql": b"SELECT 1\n"}))
        assert out == {"sql": "SELECT 1\n"}

    def test_yaml_pointer_becomes_parsed_dict(self):
        doc = {"openapi": {SOURCE_SENTINEL: "sources/openapi/y.yaml"}}
        out = unwrap_source_pointers(
            doc,
            self._resolver(
                {
                    "sources/openapi/y.yaml": (
                        b"openapi: '3.0.0'\ninfo:\n  title: Orders\n  version: '1.0'\n"
                    )
                }
            ),
        )
        assert out["openapi"]["info"]["title"] == "Orders"

    def test_json_pointer_becomes_parsed_dict(self):
        doc = {"cfg": {SOURCE_SENTINEL: "sources/x.json"}}
        out = unwrap_source_pointers(doc, self._resolver({"sources/x.json": b'{"a": 1}'}))
        assert out == {"cfg": {"a": 1}}

    def test_recurses_through_nested_structures(self):
        doc = {
            "builds": [
                {"embeddedLogicPattern": {"sql": {SOURCE_SENTINEL: "sources/sql/a.sql"}}},
                {"embeddedLogicPattern": {"sql": {SOURCE_SENTINEL: "sources/sql/b.sql"}}},
            ]
        }
        out = unwrap_source_pointers(
            doc,
            self._resolver({"sources/sql/a.sql": b"SELECT 1", "sources/sql/b.sql": b"SELECT 2"}),
        )
        assert out["builds"][0]["embeddedLogicPattern"]["sql"] == "SELECT 1"
        assert out["builds"][1]["embeddedLogicPattern"]["sql"] == "SELECT 2"

    def test_non_pointer_dicts_pass_through(self):
        """A dict that happens to contain '$source' as ONE of many keys is not
        a sentinel — only the canonical {'$source': path} shape triggers unwrap."""
        doc = {"sql": "SELECT 1", "$source": "looks-like-pointer-but-has-neighbour"}
        out = unwrap_source_pointers(doc, self._resolver({}))
        assert out == doc  # untouched

    def test_missing_file_raises(self):
        doc = {"sql": {SOURCE_SENTINEL: "sources/sql/nope.sql"}}
        with pytest.raises(ValueError, match="missing: sources/sql/nope.sql"):
            unwrap_source_pointers(doc, self._resolver({}))

    def test_non_string_source_value_raises(self):
        """Defensive: if somehow a non-string snuck into $source value, surface
        a clear error instead of a cryptic resolver failure."""
        doc = {"sql": {SOURCE_SENTINEL: 123}}
        with pytest.raises(ValueError, match="must be a string"):
            unwrap_source_pointers(doc, self._resolver({}))

    def test_input_is_not_mutated(self):
        original = {"sql": {SOURCE_SENTINEL: "sources/sql/x.sql"}}
        snapshot = json.loads(json.dumps(original))
        unwrap_source_pointers(original, self._resolver({"sources/sql/x.sql": b"SELECT 1"}))
        assert original == snapshot, "unwrap_source_pointers must not mutate input"


# ---------------------------------------------------------------------------
# infer_sqlglot_dialect
# ---------------------------------------------------------------------------


class TestInferDialect:
    @pytest.mark.parametrize(
        "platform,expected",
        [
            ("snowflake", "snowflake"),
            ("bigquery", "bigquery"),
            ("gcp", "bigquery"),  # gcp aliases to bigquery
            ("redshift", "redshift"),
            ("aws", "redshift"),  # aws aliases to redshift
            ("postgres", "postgres"),
            ("postgresql", "postgres"),  # long form
            ("duckdb", "duckdb"),
            ("local", "duckdb"),  # local aliases to duckdb
            ("  SNOWFLAKE  ", "snowflake"),  # trim + case-insensitive
        ],
    )
    def test_known_platforms(self, platform, expected):
        assert infer_sqlglot_dialect({"binding": {"platform": platform}}) == expected

    def test_unknown_platform_returns_none(self):
        assert infer_sqlglot_dialect({"binding": {"platform": "mysql"}}) is None

    def test_missing_binding_returns_none(self):
        assert infer_sqlglot_dialect({}) is None

    def test_malformed_binding_returns_none(self):
        # binding is a string, not a dict → defensive None
        assert infer_sqlglot_dialect({"binding": "snowflake"}) is None


# ---------------------------------------------------------------------------
# validate_sql
# ---------------------------------------------------------------------------


class TestValidateSql:
    """sqlglot is optional. These tests cover both installed + missing cases."""

    def test_jinja_detection_fires_without_sqlglot(self):
        """Jinja check must run BEFORE the availability gate — catching the
        'author mixed dbt with declarative SQL' mistake doesn't need sqlglot."""
        content = b"SELECT * FROM {{ ref('raw_orders') }}"
        issues = validate_sql("x.sql", content, dialect=None, strict=False)
        assert len(issues) == 1
        assert issues[0].code == "SQL-JINJA"
        assert issues[0].severity == "error"
        assert "transformation.dbt.project_dir" in issues[0].message

    def test_jinja_statement_marker(self):
        """'{%' (statement) also triggers — not just '{{' (expression)."""
        issues = validate_sql("x.sql", b"{% set x = 1 %}SELECT {{ x }}", dialect=None, strict=False)
        assert len(issues) == 1
        assert issues[0].code == "SQL-JINJA"

    def test_missing_sqlglot_info_in_non_strict(self):
        with patch("fluid_build.forge.core.validators._sqlglot_available", return_value=False):
            issues = validate_sql("x.sql", b"SELECT 1", dialect=None, strict=False)
        assert len(issues) == 1
        assert issues[0].severity == "info"
        assert "sqlglot not installed" in issues[0].message

    def test_missing_sqlglot_error_in_strict(self):
        with patch("fluid_build.forge.core.validators._sqlglot_available", return_value=False):
            issues = validate_sql("x.sql", b"SELECT 1", dialect=None, strict=True)
        assert issues[0].severity == "error"


class TestValidateSqlWithSqlglot:
    """Real sqlglot behavior — only runs when sqlglot is installed."""

    @pytest.fixture(autouse=True)
    def _skip_if_sqlglot_missing(self):
        sqlglot = pytest.importorskip("sqlglot")
        assert sqlglot is not None

    def test_valid_sql_returns_no_issues(self):
        issues = validate_sql("x.sql", b"SELECT 1 AS x FROM dual", dialect=None, strict=False)
        assert issues == [], f"valid SQL should not flag: {issues}"

    def test_broken_sql_flags_parse_error(self):
        issues = validate_sql("x.sql", b"SELECT FROM WHERE )))", dialect=None, strict=False)
        assert len(issues) >= 1
        assert all(i.severity == "error" for i in issues)
        assert all(i.validator == "sqlglot" for i in issues)


# ---------------------------------------------------------------------------
# validate_openapi
# ---------------------------------------------------------------------------


class TestValidateOpenapi:
    def test_missing_validator_info_non_strict(self):
        with patch(
            "fluid_build.forge.core.validators._openapi_validator_available",
            return_value=False,
        ):
            issues = validate_openapi("s.yaml", b"openapi: 3.0.0", strict=False)
        assert issues[0].severity == "info"
        assert "openapi-spec-validator not installed" in issues[0].message

    def test_missing_validator_error_strict(self):
        with patch(
            "fluid_build.forge.core.validators._openapi_validator_available",
            return_value=False,
        ):
            issues = validate_openapi("s.yaml", b"openapi: 3.0.0", strict=True)
        assert issues[0].severity == "error"


# ---------------------------------------------------------------------------
# validate_bundle orchestrator
# ---------------------------------------------------------------------------


class TestValidateBundle:
    """End-to-end bundle validation. Builds real tgz files via Phase-2's
    build_bundle_tgz so MANIFEST + $source unwrapping run for real."""

    def _contract(self, **overrides):
        base = {
            "fluidVersion": "0.7.2",
            "kind": "DataProduct",
            "id": "t",
            "binding": {"platform": "duckdb"},
        }
        base.update(overrides)
        return base

    def test_clean_bundle_status_pass(self, tmp_path):
        tgz = tmp_path / "b.tgz"
        build_bundle_tgz(self._contract(), tgz, contract_id="t")
        report = validate_bundle(tgz, schema_manager=_PermissiveSchema())
        assert report.status == "pass"
        assert report.summary["error"] == 0

    def test_digest_returned_in_report(self, tmp_path):
        tgz = tmp_path / "b.tgz"
        digest = build_bundle_tgz(self._contract(), tgz, contract_id="t")
        report = validate_bundle(tgz, schema_manager=_PermissiveSchema())
        assert report.bundle_digest == digest

    def test_tampered_sql_hard_fails_at_manifest_gate(self, tmp_path):
        """Any byte flipped inside the tgz must fail at MANIFEST, NOT be
        caught by per-file validators. The per-file stage should never
        even run for a tampered bundle."""
        tgz = tmp_path / "b.tgz"
        contract = self._contract(
            builds=[{"id": "b1", "embeddedLogicPattern": {"sql": "SELECT 1"}}]
        )
        build_bundle_tgz(contract, tgz, contract_id="t")

        raw = gzip.decompress(tgz.read_bytes())
        tampered = raw.replace(b"SELECT 1", b"SELECT 2")
        assert tampered != raw, "tamper substitution missed"
        gz = io.BytesIO()
        with gzip.GzipFile(filename="", fileobj=gz, mode="wb", mtime=0) as g:
            g.write(tampered)
        tgz.write_bytes(gz.getvalue())

        report = validate_bundle(tgz, schema_manager=_PermissiveSchema())
        assert report.status == "fail"
        # Exactly ONE issue — the MANIFEST error. Per-file validators didn't
        # run because the tamper gate short-circuited.
        assert len(report.issues) == 1
        assert report.issues[0].validator == "manifest"
        assert report.issues[0].code == "MANIFEST-TAMPER"

    def test_collect_all_does_not_stop_at_first_error(self, tmp_path):
        """With fail_fast=False (default), a bad SQL fragment doesn't block
        validation of the other files in the bundle."""
        tgz = tmp_path / "b.tgz"
        contract = self._contract(
            builds=[
                {"id": "good", "embeddedLogicPattern": {"sql": "SELECT 1"}},
                {"id": "bad", "embeddedLogicPattern": {"sql": "SELECT {{ bad }}"}},
            ]
        )
        build_bundle_tgz(contract, tgz, contract_id="t")

        report = validate_bundle(tgz, schema_manager=_PermissiveSchema(), fail_fast=False)

        # The bad build's Jinja fires regardless of sqlglot install state.
        jinja_issues = [i for i in report.issues if i.code == "SQL-JINJA"]
        assert len(jinja_issues) == 1
        assert "sources/sql/builds_1__bad.sql" in jinja_issues[0].file

    def test_fail_fast_stops_at_first_error(self, tmp_path):
        tgz = tmp_path / "b.tgz"
        # Two Jinja-flagged SQL fragments — fail_fast should only report the
        # first one (lexicographic order → builds_0 fires first).
        contract = self._contract(
            builds=[
                {"id": "first", "embeddedLogicPattern": {"sql": "SELECT {{ a }}"}},
                {"id": "second", "embeddedLogicPattern": {"sql": "SELECT {{ b }}"}},
            ]
        )
        build_bundle_tgz(contract, tgz, contract_id="t")

        report = validate_bundle(tgz, schema_manager=_PermissiveSchema(), fail_fast=True)
        jinja_issues = [i for i in report.issues if i.code == "SQL-JINJA"]
        assert (
            len(jinja_issues) == 1
        ), f"fail_fast should stop at first error; got {len(jinja_issues)}"
        assert "builds_0" in jinja_issues[0].file

    def test_dbt_external_project_emits_info(self, tmp_path):
        """Contracts that reference an external dbt project must not be
        silently ignored — emit an INFO guiding the user to `dbt parse`."""
        tgz = tmp_path / "b.tgz"
        contract = self._contract(
            builds=[
                {
                    "id": "b",
                    "pattern": "hybrid-reference",
                    "transformation": {"dbt": {"project_dir": "./dbt/"}},
                }
            ]
        )
        build_bundle_tgz(contract, tgz, contract_id="t")

        report = validate_bundle(tgz, schema_manager=_PermissiveSchema())
        dbt_infos = [i for i in report.issues if i.code == "DBT-EXTERNAL"]
        assert len(dbt_infos) == 1
        assert dbt_infos[0].severity == "info"
        assert "dbt parse" in dbt_infos[0].message

    def test_strict_escalates_warnings(self, tmp_path):
        """When a bundle has only warning-severity issues, strict=True must
        escalate status to fail; non-strict status stays pass."""
        # A $source pointing at a file outside sources/ is an *unexpected*
        # bundle entry; validator emits a warning. Construct a tgz manually
        # so we can inject an unexpected file.
        import tarfile

        tar_buf = io.BytesIO()
        manifest_files: dict = {
            "contract.resolved.yaml": b"fluidVersion: '0.7.2'\nid: t\n",
            "contract.resolved.json": b'{"fluidVersion":"0.7.2","id":"t"}\n',
            "weird/extra.txt": b"unexpected!\n",
        }
        import hashlib

        hashes = {p: "sha256:" + hashlib.sha256(d).hexdigest() for p, d in manifest_files.items()}
        merkle_in = "".join(f"{p}:{hashes[p]}\n" for p in sorted(hashes))
        merkle = "sha256:" + hashlib.sha256(merkle_in.encode()).hexdigest()
        manifest_doc = {
            "version": "1.0",
            "generator": "test-fixture",
            "contractId": "t",
            "files": hashes,
            "digest": merkle,
        }
        manifest_bytes = (json.dumps(manifest_doc, sort_keys=True) + "\n").encode()

        with tarfile.open(fileobj=tar_buf, mode="w") as tar:
            for p in sorted([*manifest_files.keys(), "MANIFEST.json"]):
                data = manifest_bytes if p == "MANIFEST.json" else manifest_files[p]
                info = tarfile.TarInfo(name=p)
                info.size = len(data)
                info.mtime = 0
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                tar.addfile(info, io.BytesIO(data))

        gz_buf = io.BytesIO()
        with gzip.GzipFile(filename="", fileobj=gz_buf, mode="wb", mtime=0) as gz:
            gz.write(tar_buf.getvalue())
        tgz = tmp_path / "b.tgz"
        tgz.write_bytes(gz_buf.getvalue())

        non_strict = validate_bundle(tgz, schema_manager=_PermissiveSchema(), strict=False)
        strict = validate_bundle(tgz, schema_manager=_PermissiveSchema(), strict=True)

        warnings = [i for i in non_strict.issues if i.severity == "warning"]
        assert len(warnings) >= 1, "expected warning for unexpected bundle entry"
        assert non_strict.status == "pass"
        assert strict.status == "fail", "strict must escalate warnings to failure"


# ---------------------------------------------------------------------------
# ValidationIssue / BundleValidationReport serialisation
# ---------------------------------------------------------------------------


class TestReportSerialisation:
    def test_issue_to_dict_omits_none_fields(self):
        issue = ValidationIssue(file="x.sql", validator="sqlglot", severity="error", message="boom")
        d = issue.to_dict()
        assert "line" not in d, (
            "None line must be omitted from to_dict — report JSON stays compact "
            "and consumer jq queries don't have to handle nulls"
        )
        assert "column" not in d
        assert "code" not in d

    def test_issue_to_dict_includes_location_when_set(self):
        issue = ValidationIssue(
            file="x.sql",
            validator="sqlglot",
            severity="error",
            message="boom",
            line=12,
            column=5,
            code="SQL001",
        )
        d = issue.to_dict()
        assert d["line"] == 12
        assert d["column"] == 5
        assert d["code"] == "SQL001"

    def test_report_summary_counts(self):
        report = BundleValidationReport(
            bundle_digest="sha256:x",
            input_path="/tmp/b.tgz",
            strict=False,
            status="fail",
            issues=[
                ValidationIssue(file="a", validator="v", severity="error", message=""),
                ValidationIssue(file="b", validator="v", severity="error", message=""),
                ValidationIssue(file="c", validator="v", severity="warning", message=""),
                ValidationIssue(file="d", validator="v", severity="info", message=""),
            ],
        )
        assert report.summary == {"total": 4, "error": 2, "warning": 1, "info": 1}

    def test_report_to_dict_shape(self):
        report = BundleValidationReport(
            bundle_digest="sha256:x",
            input_path="/tmp/b.tgz",
            strict=True,
            status="pass",
        )
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
# CLI integration — round-trip via `fluid validate <tgz>`
# ---------------------------------------------------------------------------


class TestCliBundleValidate:
    """Exercise the CLI layer end-to-end: build a tgz, run the validate
    subcommand, inspect the emitted --report JSON.

    Uses the shipped ``examples/01-hello-world`` contract as the fixture —
    it's known schema-valid for 0.7.2, so any validation failure in these
    tests points at the new code, not a hand-rolled invalid contract.
    """

    _FIXTURE_CONTRACT = Path(__file__).parent.parent.parent / (
        "examples/01-hello-world/contract.fluid.yaml"
    )

    def test_clean_bundle_report_shape(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        # Copy the fixture into the test workspace so the relative tgz path
        # stays inside tmp_path.
        contract = tmp_path / "contract.fluid.yaml"
        contract.write_text(self._FIXTURE_CONTRACT.read_text())

        # Build the bundle.
        import argparse
        import logging

        from fluid_build.cli.bundle import run as bundle_run

        bundle_args = argparse.Namespace(
            contract=str(contract),
            out=str(tmp_path / "b.tgz"),
            env=None,
            format="tgz",
        )
        rc = bundle_run(bundle_args, logging.getLogger("test"))
        assert rc == 0

        # Validate via the CLI path.
        from fluid_build.cli.validate import run as validate_run

        report_path = tmp_path / "report.json"
        validate_args = argparse.Namespace(
            contract=str(tmp_path / "b.tgz"),
            env=None,
            schema_version=None,
            min_version=None,
            max_version=None,
            strict=False,
            offline=False,
            force_refresh=False,
            clear_cache=False,
            cache_dir=None,
            verbose=False,
            quiet=True,
            format="text",
            list_versions=False,
            show_schema=False,
            report=str(report_path),
            fail_fast=False,
        )
        rc = validate_run(validate_args, logging.getLogger("test"))
        assert rc == 0
        assert report_path.exists()

        data = json.loads(report_path.read_text())
        assert data["status"] == "pass"
        assert data["bundleDigest"].startswith("sha256:")
        assert data["strict"] is False

    def test_tampered_bundle_exits_nonzero(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        contract = tmp_path / "contract.fluid.yaml"
        contract.write_text(self._FIXTURE_CONTRACT.read_text())

        import argparse
        import logging

        from fluid_build.cli.bundle import run as bundle_run
        from fluid_build.cli.validate import run as validate_run

        tgz = tmp_path / "b.tgz"
        bundle_run(
            argparse.Namespace(contract=str(contract), out=str(tgz), env=None, format="tgz"),
            logging.getLogger("test"),
        )

        # Tamper: flip a byte inside the tar payload.
        raw = gzip.decompress(tgz.read_bytes())
        tampered = raw.replace(b"fluidVersion", b"FLuidVersion")
        assert tampered != raw
        gz = io.BytesIO()
        with gzip.GzipFile(filename="", fileobj=gz, mode="wb", mtime=0) as g:
            g.write(tampered)
        tgz.write_bytes(gz.getvalue())

        rc = validate_run(
            argparse.Namespace(
                contract=str(tgz),
                env=None,
                schema_version=None,
                min_version=None,
                max_version=None,
                strict=False,
                offline=False,
                force_refresh=False,
                clear_cache=False,
                cache_dir=None,
                verbose=False,
                quiet=True,
                format="text",
                list_versions=False,
                show_schema=False,
                report=None,
                fail_fast=False,
            ),
            logging.getLogger("test"),
        )
        assert rc == 1, "tampered bundle must exit 1 (validation failure)"
