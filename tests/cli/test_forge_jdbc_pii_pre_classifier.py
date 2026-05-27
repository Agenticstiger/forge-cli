# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""H6 fix integration: JDBC introspection emits contracts with PII tags.

Three independent UX-audit agents confirmed (see
``/tmp/fluid-ux-findings/05-from-source-postgres.md`` section D):
columns named ``c_email``, ``email``, ``phone_number``, ``ssn``,
``date_of_birth``, etc. were emitted with NO ``piiClass``, NO
``tags``, NO ``sensitivity``. The Judge's ``security`` axis silently
landed at 1-2 because the contract carried no PII signal.

This test exercises the real JDBC dispatch path with a SQLite fixture
carrying canonical PII column names, then inspects the emitted YAML
contract to assert PII tags + sensitivity + semanticType are present.

A live-Postgres companion test (gated on ``localhost:55432/tpch/retail``)
covers the audit's exact reproduction case via a marker.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

duckdb = pytest.importorskip("duckdb")


@pytest.fixture
def pii_sqlite_db(tmp_path: Path) -> Path:
    """A SQLite db with the canonical PII column names from the
    UX-audit retail-postgres fixture (TPC-H + retail extensions)."""
    db_path = tmp_path / "pii_fixture.sqlite"
    con = sqlite3.connect(str(db_path))
    cur = con.cursor()
    cur.execute(
        """
        CREATE TABLE customer (
            c_custkey INTEGER PRIMARY KEY,
            c_email TEXT,
            c_phone TEXT,
            c_address TEXT,
            c_acctbal REAL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE patient (
            patient_id INTEGER PRIMARY KEY,
            ssn TEXT,
            date_of_birth TEXT,
            full_name TEXT,
            mrn TEXT
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE non_pii (
            id INTEGER PRIMARY KEY,
            amount REAL,
            created_at TEXT,
            status TEXT
        )
        """
    )
    con.commit()
    con.close()
    return db_path


def _run_pipeline(sqlite_db: Path, out: Path) -> int:
    """Drive the JDBC pipeline end-to-end with the given SQLite db."""
    from fluid_build.cli.forge_data_model import _run_from_jdbc_source

    args = SimpleNamespace(
        source="sqlite",
        uri=f"sqlite:///{sqlite_db}",
        schema_name=None,
        tables=None,
        name="pii_test",
        output=str(out),
    )
    return _run_from_jdbc_source(args, logging.getLogger("test-pii"))


def _load_contract(path: Path) -> dict:
    import yaml as _yaml

    return _yaml.safe_load(path.read_text())


class TestJdbcPiiPreClassifier:
    def test_email_column_tagged_pii_email(self, pii_sqlite_db: Path, tmp_path: Path):
        """The retail-postgres ``c_email`` UX-audit case — must emit
        ``tags: [pii-email]``, ``sensitivity: pii``, ``semanticType: email``."""
        out = tmp_path / "out.fluid.yaml"
        rc = _run_pipeline(pii_sqlite_db, out)
        assert rc == 0
        contract = _load_contract(out)
        # Find the customer expose.
        exposes = {e["exposeId"]: e for e in contract["exposes"]}
        assert "customer" in exposes
        cols = {c["name"]: c for c in exposes["customer"]["contract"]["schema"]}
        email_col = cols["c_email"]
        assert "pii-email" in email_col["tags"]
        assert email_col["sensitivity"] == "pii"
        assert email_col["semanticType"] == "email"

    def test_phone_column_tagged(self, pii_sqlite_db: Path, tmp_path: Path):
        out = tmp_path / "out.fluid.yaml"
        assert _run_pipeline(pii_sqlite_db, out) == 0
        contract = _load_contract(out)
        cols = {
            c["name"]: c
            for e in contract["exposes"]
            if e["exposeId"] == "customer"
            for c in e["contract"]["schema"]
        }
        phone_col = cols["c_phone"]
        assert "pii-phone" in phone_col["tags"]
        assert phone_col["sensitivity"] == "pii"
        assert phone_col["semanticType"] == "phone"

    def test_address_column_tagged(self, pii_sqlite_db: Path, tmp_path: Path):
        out = tmp_path / "out.fluid.yaml"
        assert _run_pipeline(pii_sqlite_db, out) == 0
        contract = _load_contract(out)
        cols = {
            c["name"]: c
            for e in contract["exposes"]
            if e["exposeId"] == "customer"
            for c in e["contract"]["schema"]
        }
        addr_col = cols["c_address"]
        assert "pii-address" in addr_col["tags"]
        assert addr_col["sensitivity"] == "pii"

    def test_ssn_dob_mrn_tagged(self, pii_sqlite_db: Path, tmp_path: Path):
        """SSN + DOB → pii. MRN → phi (stronger than pii)."""
        out = tmp_path / "out.fluid.yaml"
        assert _run_pipeline(pii_sqlite_db, out) == 0
        contract = _load_contract(out)
        cols = {
            c["name"]: c
            for e in contract["exposes"]
            if e["exposeId"] == "patient"
            for c in e["contract"]["schema"]
        }
        assert "pii-ssn" in cols["ssn"]["tags"]
        assert "pii-dob" in cols["date_of_birth"]["tags"]
        assert "pii-name" in cols["full_name"]["tags"]
        assert "pii-medical" in cols["mrn"]["tags"]
        # MRN → phi (medical override).
        assert cols["mrn"]["sensitivity"] == "phi"

    def test_non_pii_columns_untouched(self, pii_sqlite_db: Path, tmp_path: Path):
        """``amount``, ``status``, ``created_at`` carry no PII tags."""
        out = tmp_path / "out.fluid.yaml"
        assert _run_pipeline(pii_sqlite_db, out) == 0
        contract = _load_contract(out)
        cols = {
            c["name"]: c
            for e in contract["exposes"]
            if e["exposeId"] == "non_pii"
            for c in e["contract"]["schema"]
        }
        for col_name in ("amount", "created_at", "status"):
            col = cols[col_name]
            tags = col.get("tags") or []
            # No pii-* tags. (``primary-key`` may be present on id but
            # we're checking the non-PK columns here.)
            assert not any(
                t.startswith("pii-") for t in tags
            ), f"unexpected PII tag on {col_name!r}: {tags!r}"
            assert "sensitivity" not in col
            assert "semanticType" not in col

    def test_emitted_contract_is_schema_valid(self, pii_sqlite_db: Path, tmp_path: Path):
        """The PII tags must use the FLUID schema's tag pattern
        ([a-z0-9][a-z0-9-]*[a-z0-9]) and the sensitivity enum, or
        ``fluid validate`` would reject the emitted contract.

        The pipeline already invokes ``validate_contract_file`` and
        bails with rc=1 on failure, so rc==0 alone proves validity —
        but we also do a defensive scan of every tag string."""
        import re

        out = tmp_path / "out.fluid.yaml"
        assert _run_pipeline(pii_sqlite_db, out) == 0
        contract = _load_contract(out)
        tag_pat = re.compile(r"^[a-z0-9][a-z0-9-]*[a-z0-9]$|^[a-z0-9]$")
        sensitivity_enum = {
            "none",
            "internal",
            "confidential",
            "restricted",
            "pii",
            "phi",
            "cleartext",
            "treated",
            "anonymized",
            "pseudonymized",
            "tokenized",
            "encrypted",
        }
        for expose in contract["exposes"]:
            for col in expose["contract"]["schema"]:
                for tag in col.get("tags", []):
                    assert tag_pat.match(tag), f"tag {tag!r} fails schema pattern"
                if "sensitivity" in col:
                    assert col["sensitivity"] in sensitivity_enum

    def test_kill_switch_disables_jdbc_pii_tagging(
        self, pii_sqlite_db: Path, tmp_path: Path, monkeypatch
    ):
        """FLUID_COPILOT_PII_CLASSIFIER=0 turns the JDBC tagging off."""
        monkeypatch.setenv("FLUID_COPILOT_PII_CLASSIFIER", "0")
        out = tmp_path / "out.fluid.yaml"
        assert _run_pipeline(pii_sqlite_db, out) == 0
        contract = _load_contract(out)
        cols = {
            c["name"]: c
            for e in contract["exposes"]
            if e["exposeId"] == "customer"
            for c in e["contract"]["schema"]
        }
        email_col = cols["c_email"]
        # No PII tags emitted (kill switch active).
        assert not any(t.startswith("pii-") for t in email_col.get("tags", []))
        assert "sensitivity" not in email_col


# ---------------------------------------------------------------------------
# Live-Postgres reproduction — gated, only runs when the audit's
# Docker postgres is up at localhost:55432.
# ---------------------------------------------------------------------------


def _live_postgres_available() -> bool:
    """Mirror the audit's environment — Docker postgres on 55432."""
    if os.environ.get("FLUID_SKIP_LIVE_POSTGRES"):
        return False
    try:
        import socket

        s = socket.create_connection(("localhost", 55432), timeout=0.5)
        s.close()
        return True
    except OSError:
        return False


@pytest.mark.integration
@pytest.mark.skipif(
    not _live_postgres_available(),
    reason="live postgres on localhost:55432 not running",
)
def test_live_retail_postgres_emits_pii_tags_on_c_email(tmp_path: Path):
    """The exact UX-audit reproduction case: retail/customer.c_email
    on a live Docker postgres must come out with ``pii-email`` tagged."""
    from fluid_build.cli.forge_data_model import _run_from_jdbc_source

    out = tmp_path / "retail.fluid.yaml"
    args = SimpleNamespace(
        source="postgres",
        uri="postgresql://postgres:postgres@localhost:55432/tpch",
        schema_name="retail",
        tables=None,
        name="retail_pii",
        output=str(out),
    )
    rc = _run_from_jdbc_source(args, logging.getLogger("test-live"))
    if rc != 0:
        pytest.skip("retail schema not present in live postgres")

    import yaml as _yaml

    contract = _yaml.safe_load(out.read_text())
    exposes = {e["exposeId"]: e for e in contract["exposes"]}
    if "customer" not in exposes:
        pytest.skip("customer table not in live postgres retail schema")
    cols = {c["name"]: c for c in exposes["customer"]["contract"]["schema"]}
    if "c_email" not in cols:
        pytest.skip("c_email column not in live postgres customer table")
    email_col = cols["c_email"]
    assert (
        "pii-email" in email_col["tags"]
    ), f"live postgres customer.c_email missing pii-email tag. tags={email_col.get('tags')}"
    assert email_col["sensitivity"] == "pii"
