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

"""The DuckDB runner lands ``policy.privacy.masking`` columns treated, or not at all.

``exposes[].policy.privacy.masking[]`` was schema-valid and read only by the MCP
output-port gateway: the runner copied the source straight into the
destination, so a contract declaring ``msisdn`` hashed landed phone numbers in
the file, in S3 and in the DLQ. Every test here runs the real
``execute_duckdb_build`` against a CSV source; the S3 ones either rewrite only
the COPY's target (offline) or write to a moto S3 server.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

duckdb = pytest.importorskip("duckdb")

from fluid_build.build_runners import _masking as m  # noqa: E402
from fluid_build.build_runners.duckdb.runner import execute_duckdb_build  # noqa: E402

pytestmark = pytest.mark.unit

PRODUCT = "bronze.subs"
BUILD = "ingest"
SALT = "landing-test-salt-0123456789"
TOKEN_KEY = "landing-test-tokenization-key-0123456789ab"
AES_KEY = bytes(range(32))
AES_KEY_B64 = base64.b64encode(AES_KEY).decode("ascii")
SECRETS = (SALT, TOKEN_KEY, AES_KEY_B64)

ROWS = [
    # id, msisdn, email, name, zip
    (1, "+46701234567", "anna@example.com", "Anna", "11122"),
    (2, "+46709876543", "bo@example.com", "Bo", "22233"),
    (3, "", "cleo@example.com", "Cleo", "33344"),  # an empty CSV field reads as NULL
    (4, None, None, None, "44455"),
]
CLEARTEXT = ("+46701234567", "+46709876543", "anna@example.com", "bo@example.com", "Anna")

HASH_MSISDN = {"column": "msisdn", "strategy": "hash"}
DEFAULT_RULES: List[Dict[str, Any]] = [
    HASH_MSISDN,
    {"column": "email", "strategy": "tokenize"},
    {"column": "name", "strategy": "encrypt"},
    {"column": "zip", "strategy": "mask", "params": {"keepLast": 2}},
]


@pytest.fixture(autouse=True)
def _secrets(monkeypatch):
    monkeypatch.setenv(m.DEFAULT_SALT_ENV, SALT)
    monkeypatch.setenv(m.DEFAULT_TOKENIZATION_KEY_ENV, TOKEN_KEY)
    monkeypatch.setenv(m.DEFAULT_ENCRYPTION_KEY_ENV, AES_KEY_B64)


def _source(tmp_path: Path) -> Path:
    lines = ["id,msisdn,email,name,zip"]
    for row in ROWS:
        lines.append(",".join("" if v is None else str(v) for v in row))
    path = tmp_path / "subs.csv"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _contract(
    tmp_path: Path,
    rules: Optional[List[Dict[str, Any]]] = None,
    *,
    schema: Optional[List[Dict[str, Any]]] = None,
    schema_policy: Optional[str] = None,
    gates: Optional[List[Dict[str, Any]]] = None,
    binding: Optional[Dict[str, Any]] = None,
    connection_extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    connection: Dict[str, Any] = {"uri": str(_source(tmp_path))}
    connection.update(connection_extra or {})
    properties: Dict[str, Any] = {
        "source": {
            "kind": "filesystem",
            "mode": "full_refresh",
            "connection": connection,
            # all_varchar: the CSV reader would otherwise type zip as a number.
            "reader": {"format": "csv", "options": {"all_varchar": True}},
            "streams": ["subs"],
        },
        "sink": {"format": "parquet"},
    }
    if gates:
        properties["quality"] = {"gates": gates, "onError": "route_to_dlq"}
    expose: Dict[str, Any] = {
        "exposeId": "subs",
        "kind": "table",
        "binding": binding
        or {"platform": "local", "format": "parquet", "location": {"path": "./out/subs.parquet"}},
    }
    if rules is not None:
        expose["policy"] = {"privacy": {"masking": rules}}
    if schema is not None:
        expose["contract"] = {"schema": schema}
        if schema_policy:
            expose["contract"]["schemaPolicy"] = schema_policy
    return {
        "fluidVersion": "0.7.5",
        "kind": "DataProduct",
        "id": PRODUCT,
        "name": "Subs",
        "builds": [
            {
                "id": BUILD,
                "pattern": "acquisition",
                "engine": "duckdb",
                "properties": properties,
                "outputs": ["subs"],
            }
        ],
        "exposes": [expose],
    }


def _run(tmp_path: Path, contract: Dict[str, Any]) -> tuple[int, Dict[str, Any]]:
    runs = tmp_path / ".fluid" / "runs" / PRODUCT / BUILD / "runs"
    before = set(runs.glob("*.json")) if runs.is_dir() else set()
    rc = execute_duckdb_build(contract["builds"][0], contract, tmp_path)
    (written,) = set(runs.glob("*.json")) - before
    return rc, json.loads(written.read_text(encoding="utf-8"))


def _landed(path: Path) -> List[Dict[str, Any]]:
    con = duckdb.connect()
    try:
        rel = con.execute(f"SELECT * FROM read_parquet('{path}') ORDER BY id")
        cols = [d[0] for d in rel.description]
        return [dict(zip(cols, row, strict=True)) for row in rel.fetchall()]
    finally:
        con.close()


def _sha(value: str) -> str:
    return hashlib.sha256((SALT + value).encode("utf-8")).hexdigest()


# ── What lands ──────────────────────────────────────────────────────────


def test_every_masked_column_lands_treated_and_the_rest_unchanged(tmp_path):
    rc, record = _run(tmp_path, _contract(tmp_path, DEFAULT_RULES))

    assert rc == 0, record
    rows = _landed(tmp_path / "out" / "subs.parquet")
    assert [r["id"] for r in rows] == ["1", "2", "3", "4"]
    for row, (_id, msisdn, email, name, zip_) in zip(rows, ROWS, strict=True):
        assert row["msisdn"] == (_sha(msisdn) if msisdn else None)
        assert row["email"] == (
            None if email is None else m.tokenize_value(email, key=TOKEN_KEY.encode())
        )
        if name is None:
            assert row["name"] is None
        else:
            assert m.decrypt_value(row["name"], key=AES_KEY, column="name") == name
        assert row["zip"] == "***" + zip_[-2:]
    assert record["facets"]["masking"] == {
        "applied": "at_landing",
        "expose": "subs",
        "columns": {
            "msisdn": {"strategy": "hash", "saltEnv": m.DEFAULT_SALT_ENV},
            "email": {"strategy": "tokenize", "keyEnv": m.DEFAULT_TOKENIZATION_KEY_ENV},
            "name": {"strategy": "encrypt", "keyEnv": m.DEFAULT_ENCRYPTION_KEY_ENV},
            "zip": {"strategy": "mask", "keepFirst": 0, "keepLast": 2},
        },
    }


def test_no_cleartext_value_is_anywhere_in_the_landed_file(tmp_path):
    _run(tmp_path, _contract(tmp_path, DEFAULT_RULES))
    blob = (tmp_path / "out" / "subs.parquet").read_bytes()
    for value in CLEARTEXT:
        assert value.encode("utf-8") not in blob


def test_the_dlq_gets_treated_rows_too(tmp_path):
    """Rows a gate rejects are written to disk as well; they must not be the
    one place the cleartext survives."""
    gates = [{"rule": "not_null", "columns": ["zip"]}, {"rule": "range", "column": "id", "max": 2}]
    rc, record = _run(tmp_path, _contract(tmp_path, DEFAULT_RULES, gates=gates))

    assert rc == 0, record
    assert record["streams"][0]["records"] == 2
    (dlq,) = list((tmp_path / ".fluid" / "dlq").rglob("*.ndjson"))
    text = dlq.read_text(encoding="utf-8")
    records = [json.loads(line)["record"] for line in text.splitlines() if line]
    assert {r["id"] for r in records} == {"3", "4"}
    assert all(r["msisdn"] is None for r in records)  # the empty field and the NULL
    for value in CLEARTEXT + ("cleo@example.com", "Cleo"):
        assert value not in text


def test_quality_gates_judge_the_source_value_not_the_treated_one(tmp_path):
    """A regex gate on the cleartext format keeps working: had it run on the
    hashes, every row would have failed it."""
    gates = [{"rule": "regex", "column": "msisdn", "pattern": "^\\+46[0-9]{9}$"}]
    rc, record = _run(tmp_path, _contract(tmp_path, [HASH_MSISDN], gates=gates))

    assert rc == 0, record
    rows = _landed(tmp_path / "out" / "subs.parquet")
    assert [r["msisdn"] for r in rows] == [_sha("+46701234567"), _sha("+46709876543")]


def test_sample_rows_still_lands_treated_rows(tmp_path):
    contract = _contract(tmp_path, [HASH_MSISDN])
    rc = execute_duckdb_build(contract["builds"][0], contract, tmp_path, sample_rows=1)
    assert rc == 0
    (row,) = _landed(tmp_path / "out" / "subs.parquet")
    assert row["msisdn"] == _sha("+46701234567")


def test_strict_schema_policy_compares_a_masked_column_as_what_lands(tmp_path):
    """The declared schema describes the landed table. A hashed INTEGER id is
    a VARCHAR there, so declaring VARCHAR passes the strict gate."""
    source = tmp_path / "ids.csv"
    source.write_text("id,msisdn\n7,+46701234567\n", encoding="utf-8")
    contract = _contract(
        tmp_path,
        [{"column": "id", "strategy": "hash"}],
        schema=[{"name": "id", "type": "VARCHAR"}, {"name": "msisdn", "type": "VARCHAR"}],
        schema_policy="strict",
    )
    props = contract["builds"][0]["properties"]["source"]
    props["connection"]["uri"] = str(source)
    props["reader"] = {"format": "csv"}  # id is typed BIGINT by the reader

    rc, record = _run(tmp_path, contract)

    assert rc == 0, record
    (row,) = _landed(tmp_path / "out" / "subs.parquet")
    assert row["id"] == _sha("7")


# ── What refuses to land ────────────────────────────────────────────────


def _refused(tmp_path: Path, contract: Dict[str, Any]) -> Dict[str, Any]:
    rc, record = _run(tmp_path, contract)
    assert rc == 1
    assert record["state"] == "failed"
    assert not (tmp_path / "out" / "subs.parquet").exists(), "something landed"
    return record


def test_an_unset_salt_refuses_the_run_and_lands_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv(m.DEFAULT_SALT_ENV)
    record = _refused(tmp_path, _contract(tmp_path, [HASH_MSISDN]))
    error = record["streams"][0]["error"]
    assert error.startswith("masking: ")
    assert m.DEFAULT_SALT_ENV in error and "'msisdn'" in error


@pytest.mark.parametrize("strategy", ["tokenize", "encrypt"])
def test_an_unset_key_refuses_the_run_and_lands_nothing(tmp_path, monkeypatch, strategy):
    monkeypatch.delenv(m.DEFAULT_TOKENIZATION_KEY_ENV)
    monkeypatch.delenv(m.DEFAULT_ENCRYPTION_KEY_ENV)
    record = _refused(tmp_path, _contract(tmp_path, [{"column": "email", "strategy": strategy}]))
    assert "environment variable FLUID_PII_" in record["streams"][0]["error"]


def test_k_anonymity_refuses_the_run_naming_the_strategy(tmp_path):
    record = _refused(tmp_path, _contract(tmp_path, [{"column": "zip", "strategy": "k_anonymity"}]))
    assert "'k_anonymity'" in record["streams"][0]["error"]


def test_a_rule_on_a_column_the_source_lacks_fails_before_the_copy(tmp_path):
    record = _refused(tmp_path, _contract(tmp_path, [{"column": "imsi", "strategy": "hash"}]))
    error = record["streams"][0]["error"]
    assert "'imsi'" in error and "does not have" in error


def test_a_masked_column_declared_with_a_non_string_type_is_refused_not_reported_as_drift(
    tmp_path,
):
    contract = _contract(
        tmp_path,
        [{"column": "id", "strategy": "hash"}],
        schema=[{"name": "id", "type": "INTEGER"}],
        schema_policy="strict",
    )
    record = _refused(tmp_path, contract)
    assert "declares 'id' as INTEGER" in record["streams"][0]["error"]


def test_without_masking_rules_the_copy_is_exactly_what_it_was(tmp_path, monkeypatch):
    statements = _record_statements(monkeypatch)
    rc, record = _run(tmp_path, _contract(tmp_path))
    assert rc == 0
    (copy,) = [s for s in statements if s.startswith("COPY")]
    assert "REPLACE" not in copy and "__fluid_mask_" not in copy
    assert "masking" not in record["facets"]
    assert _landed(tmp_path / "out" / "subs.parquet")[0]["msisdn"] == "+46701234567"


# ── Secrets ─────────────────────────────────────────────────────────────


def test_no_salt_or_key_reaches_the_logs_the_run_record_the_dlq_or_the_sql(
    tmp_path, monkeypatch, caplog
):
    statements = _record_statements(monkeypatch)
    gates = [{"rule": "not_null", "columns": ["zip", "name"]}]
    with caplog.at_level(logging.DEBUG):
        rc, record = _run(tmp_path, _contract(tmp_path, DEFAULT_RULES, gates=gates))
    assert rc == 0

    surfaces = {
        "logs": "\n".join(r.getMessage() for r in caplog.records),
        "run record": json.dumps(record),
        "sql": "\n".join(statements),
        "dlq": "\n".join(p.read_text() for p in (tmp_path / ".fluid").rglob("*.ndjson")),
        "landed file": (tmp_path / "out" / "subs.parquet").read_bytes().decode("latin-1"),
    }
    for where, text in surfaces.items():
        for secret in SECRETS:
            assert secret not in text, f"a secret reached the {where}"


def test_a_refusal_never_echoes_the_secret(tmp_path, monkeypatch):
    weak = "short-salt"
    monkeypatch.setenv(m.DEFAULT_SALT_ENV, weak)
    record = _refused(tmp_path, _contract(tmp_path, [HASH_MSISDN]))
    assert weak not in json.dumps(record)


# ── S3 ──────────────────────────────────────────────────────────────────


def _record_statements(monkeypatch, *, s3_to: Optional[Path] = None) -> List[str]:
    """Record every statement the runner sends to DuckDB.

    With ``s3_to``, the run stays offline: extension loads and secrets are
    recorded but not executed, and a ``COPY ... TO 's3://...'`` is recorded
    and then pointed at ``s3_to`` instead, so what would have gone to S3 can
    be read back.
    """
    statements: List[str] = []
    real_connect = duckdb.connect

    class _Recording:
        def __init__(self, con: Any) -> None:
            self._con = con

        def __getattr__(self, name: str) -> Any:
            return getattr(self._con, name)

        def execute(self, sql: str, *args: Any) -> Any:
            statements.append(sql)
            if s3_to is not None:
                if sql.startswith(("INSTALL", "LOAD", "CREATE OR REPLACE SECRET")):
                    return self._con.execute("SELECT 1")
                if sql.startswith("COPY") and "'s3://" in sql:
                    sql = re.sub(r"TO 's3://[^']*'", f"TO '{s3_to}'", sql)
            return self._con.execute(sql, *args)

    monkeypatch.setattr(duckdb, "connect", lambda *a, **k: _Recording(real_connect(*a, **k)))
    return statements


AWS_BINDING = {
    "platform": "aws",
    "format": "parquet",
    "location": {
        "bucket": "northwind-demo-lake",
        "path": "bronze/customer_subscriptions/",
        "table": "customer_subscriptions",
        "database": "demo_bronze",
        "region": "eu-north-1",
    },
}


#: Where the runner writes for that binding: the prefix plus ``<table>.parquet``.
S3_OBJECT = "s3://northwind-demo-lake/bronze/customer_subscriptions/customer_subscriptions.parquet"


def test_an_s3_destination_gets_the_same_treated_rows(tmp_path, monkeypatch):
    """The AWS binding of the demo: the COPY that goes to S3 carries the
    masking, and what it writes is treated. Offline: the COPY's target is the
    only thing rewritten."""
    diverted = tmp_path / "diverted.parquet"
    statements = _record_statements(monkeypatch, s3_to=diverted)

    rc, record = _run(tmp_path, _contract(tmp_path, [HASH_MSISDN], binding=AWS_BINDING))

    assert rc == 0, record
    (copy,) = [s for s in statements if s.startswith("COPY")]
    assert f"TO '{S3_OBJECT}'" in copy
    assert '__fluid_mask_0(CAST("msisdn" AS VARCHAR)) AS "msisdn"' in copy
    rows = _landed(diverted)
    assert rows[0]["msisdn"] == _sha("+46701234567")
    assert record["facets"]["landed"]["destinations"] == {"subs": S3_OBJECT}


def _httpfs_available() -> bool:
    con = duckdb.connect()
    try:
        con.execute("INSTALL httpfs")
        con.execute("LOAD httpfs")
        return True
    except Exception:  # noqa: BLE001 — offline, or no extension repository
        return False
    finally:
        con.close()


@pytest.fixture
def moto_s3(monkeypatch):
    try:
        import boto3
        from moto.server import ThreadedMotoServer
    except Exception:  # noqa: BLE001 — the test-emulators extra is absent
        pytest.skip("needs moto[server] and boto3 (pip install -e '.[test-emulators]')")
    if not _httpfs_available():
        pytest.skip("DuckDB's httpfs extension cannot be installed here")
    for var in ("AWS_PROFILE", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", "/dev/null")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", "/dev/null")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    server = ThreadedMotoServer(port=0, verbose=False)
    server.start()
    try:
        _, port = server.get_host_and_port()
        endpoint = f"127.0.0.1:{port}"
        s3 = boto3.client(
            "s3",
            endpoint_url=f"http://{endpoint}",
            region_name="us-east-1",
            aws_access_key_id="testing",
            aws_secret_access_key="testing",  # pragma: allowlist secret — moto dummy
        )
        s3.create_bucket(Bucket="northwind-demo-lake")
        yield endpoint, s3
    finally:
        server.stop()


@pytest.mark.emulated
def test_the_object_written_to_s3_holds_treated_values(tmp_path, moto_s3):
    endpoint, s3 = moto_s3
    binding = json.loads(json.dumps(AWS_BINDING))
    binding["location"]["region"] = "us-east-1"
    moto_secret = {
        "endpoint": endpoint,
        "url_style": "path",
        "use_ssl": False,
        "region": "us-east-1",
        "key_id": "testing",
        "secret": "testing",  # pragma: allowlist secret — moto dummy
    }
    contract = _contract(
        tmp_path, DEFAULT_RULES, binding=binding, connection_extra={"s3": moto_secret}
    )

    rc, record = _run(tmp_path, contract)

    assert rc == 0, record
    key = "bronze/customer_subscriptions/customer_subscriptions.parquet"
    local = tmp_path / "from-s3.parquet"
    local.write_bytes(s3.get_object(Bucket="northwind-demo-lake", Key=key)["Body"].read())
    rows = _landed(local)
    assert rows[0]["msisdn"] == _sha("+46701234567")
    assert rows[1]["zip"] == "***33"
    blob = local.read_bytes()
    for value in CLEARTEXT:
        assert value.encode("utf-8") not in blob
