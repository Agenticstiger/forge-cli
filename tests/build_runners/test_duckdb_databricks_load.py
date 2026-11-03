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

"""A Databricks binding is built locally, uploaded to a volume and loaded into the managed table.

What these pin, each of which was wrong before: for ``platform: databricks`` the acquisition
runner wrote a LOCAL file (``location.path``, else ``out/``) and reported success with nothing
in Databricks. Now the run's Parquet stages under ``.fluid/staging``, goes to
``$DATABRICKS_STAGING_VOLUME`` through the Files API, one ``INSERT OVERWRITE ... read_files``
on ``$DATABRICKS_WAREHOUSE_ID`` replaces the table's rows, and the staged files are deleted
whether the load worked or not.

The workspace is ``tests/iac/_fake_databricks.py``, an in-process stand-in: no Databricks
account, credential or network is involved. These tests need no ``databricks-sdk``: the client is
the stand-in's own ``api_client.do`` over HTTP.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

import pytest

from fluid_build.build_runners import _databricks_load as load

# The iac-tests job collects every file with no duckdb installed.
duckdb = pytest.importorskip("duckdb")
from fluid_build.build_runners import _masking as masking  # noqa: E402
from fluid_build.build_runners.duckdb.runner import execute_duckdb_build  # noqa: E402

from ..iac._fake_databricks import FakeDatabricks  # noqa: E402

pytestmark = pytest.mark.unit

VOLUME = "/Volumes/main/staging/landing"
TABLE = "main.bronze.subs"
SALT = "landing-test-salt-0123456789"
COLUMNS = [("id", "string", True), ("msisdn", "string", False), ("zip", "string", False)]
ROWS = [("1", "+46701234567", "11122"), ("2", "+46709876543", "22233"), ("3", "", "33344")]


@pytest.fixture
def fake() -> Iterator[FakeDatabricks]:
    server = FakeDatabricks().start()
    server.add_volume("main.staging.landing")
    server.create_table(TABLE, COLUMNS)
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture(autouse=True)
def workspace(monkeypatch, fake: FakeDatabricks) -> None:
    """The stand-in as the only workspace the loader can reach, and its secrets as env."""
    for name in ("DATABRICKS_HOST", "DATABRICKS_TOKEN", "DATABRICKS_CONFIG_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DATABRICKS_WAREHOUSE_ID", fake.warehouse_id)
    monkeypatch.setenv(load.STAGING_VOLUME_ENV, VOLUME)
    monkeypatch.setenv(masking.DEFAULT_SALT_ENV, SALT)
    monkeypatch.setattr(load, "default_client", fake.client)


# ── the staging volume: one environment variable, three identifiers ──────


@pytest.mark.parametrize("value", [VOLUME, VOLUME + "/", "/Volumes/Main_1/_s/v2"])
def test_a_staging_volume_is_three_identifiers_under_volumes(value):
    volume = load.staging_volume({load.STAGING_VOLUME_ENV: value})
    assert (volume.catalog, volume.schema, volume.volume) == tuple(value.strip("/").split("/")[1:])
    assert volume.path == value.rstrip("/")


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        "/Volumes/main/staging",
        "/Volumes/main/staging/landing/extra",
        "main.staging.landing",
        "/volumes/main/staging/landing",
        "/Volumes/main/../landing",
        "/Volumes/ma-in/staging/landing",
        "/Volumes/main/stag ing/landing",
        "/Volumes/main/staging/`landing`",
        "/Volumes/main/staging/landing;DROP",
        "/Volumes//staging/landing",
        "dbfs:/Volumes/main/staging/landing",
    ],
)
def test_a_staging_volume_that_is_not_one_is_refused_by_name(value):
    with pytest.raises(load.DatabricksLoadError) as exc:
        load.staging_volume({load.STAGING_VOLUME_ENV: value})
    assert load.STAGING_VOLUME_ENV in str(exc.value)
    assert "/Volumes/<catalog>/<schema>/<volume>" in str(exc.value)


def test_an_unset_staging_volume_is_refused_by_name():
    with pytest.raises(load.DatabricksLoadError) as exc:
        load.staging_volume({})
    assert load.STAGING_VOLUME_ENV in str(exc.value) and "not set" in str(exc.value)


def test_a_run_stages_under_its_product_build_and_run():
    volume = load.staging_volume({load.STAGING_VOLUME_ENV: VOLUME})
    prefix = load.run_prefix(volume, "bronze.subs", "ingest", "run-20261103-abc")
    assert prefix == f"{VOLUME}/bronze_subs/ingest/run-20261103-abc"


@pytest.mark.parametrize("hostile", ["../../x", "..", "a/b", "a\\b", "a'b", "a b", "", "é"])
def test_a_contract_supplied_id_cannot_climb_out_of_the_volume(hostile):
    volume = load.staging_volume({load.STAGING_VOLUME_ENV: VOLUME})
    for prefix in (
        load.run_prefix(volume, hostile, "b", "r"),
        load.run_prefix(volume, "p", hostile, "r"),
        load.run_prefix(volume, "p", "b", hostile),
    ):
        assert prefix.startswith(VOLUME + "/")
        tail = prefix[len(VOLUME) + 1 :].split("/")
        assert len(tail) == 3 and all(tail) and ".." not in tail
        assert all(
            set(part) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-")
            for part in tail
        )


# ── the target and the statement ─────────────────────────────────────────


def _expose(**location: Any) -> Dict[str, Any]:
    loc = {"database": "Main", "schema": "bronze", "table": "subs", **location}
    return {
        "exposeId": "subs",
        "kind": "table",
        "binding": {"platform": "databricks", "format": "delta_table", "location": loc},
        "contract": {
            # varchar, not string: the run-time schema gate compares the declared type with
            # DuckDB's own (VARCHAR) and reads "string" as a type change; the emitter maps both
            # to string
            "schema": [
                {"name": "id", "type": "varchar"},
                {"name": "msisdn", "type": "varchar"},
                {"name": "zip", "type": "varchar"},
            ],
        },
    }


def test_the_target_is_the_table_apply_made_with_its_columns_in_order():
    expose = _expose()
    target = load.databricks_load_target(expose["binding"], expose)
    assert target.full_name == "main.bronze.subs"  # lower-cased, as the emitter creates it
    assert target.columns == ("id", "msisdn", "zip")


@pytest.mark.parametrize("platform", ["local", "gcp", "aws", "snowflake", ""])
def test_another_platform_has_no_databricks_target(platform):
    expose = _expose()
    expose["binding"]["platform"] = platform
    assert load.databricks_load_target(expose["binding"], expose) is None


def test_the_insert_overwrite_statement_is_exact():
    expose = _expose()
    target = load.databricks_load_target(expose["binding"], expose)
    assert load.insert_overwrite_statement(target, f"{VOLUME}/bronze_subs/ingest/run-1") == (
        "INSERT OVERWRITE `main`.`bronze`.`subs` (`id`, `msisdn`, `zip`) SELECT `id`, `msisdn`, `zip` "
        f"FROM read_files('{VOLUME}/bronze_subs/ingest/run-1', format => 'parquet')"
    )


def test_the_path_is_a_spark_string_literal_with_backslash_escapes():
    expose = _expose()
    target = load.databricks_load_target(expose["binding"], expose)
    sql = load.insert_overwrite_statement(target, "/Volumes/a/b/c/it's\\x")
    assert sql.endswith("read_files('/Volumes/a/b/c/it\\'s\\\\x', format => 'parquet')")


@pytest.mark.parametrize(
    "mutate, fragment",
    [
        (lambda e: e["binding"]["location"].update(table="subs`; DROP TABLE x"), "table"),
        (lambda e: e["binding"]["location"].update(database="a.b"), "database"),
        (lambda e: e["binding"]["location"].pop("schema"), "schema"),
        (lambda e: e["contract"].update(schema=[]), "contract.schema"),
        (lambda e: e["contract"]["schema"].append({"name": "ID", "type": "string"}), "twice"),
        (
            lambda e: e["contract"]["schema"].append({"name": "x`) FROM t --", "type": "string"}),
            "name",
        ),
    ],
)
def test_an_unsafe_or_incomplete_binding_is_refused_before_any_statement(mutate, fragment):
    expose = _expose()
    mutate(expose)
    with pytest.raises(load.DatabricksLoadError) as exc:
        load.databricks_load_target(expose["binding"], expose)
    assert fragment in str(exc.value)


@pytest.mark.parametrize(
    "mode, sink, streams, fragment",
    [
        ("full_refresh", "parquet", 2, "exactly one stream"),
        ("full_refresh", "csv", 1, "sink format"),
        ("incremental_append", "parquet", 1, "full_refresh"),
        ("cdc", "parquet", 1, "full_refresh"),
    ],
)
def test_unsupported_shapes_are_named(mode, sink, streams, fragment):
    assert fragment in load.unsupported_reason(mode, sink, streams)


def test_a_full_refresh_of_one_parquet_stream_is_supported():
    assert load.unsupported_reason("full_refresh", "parquet", 1) is None


# ── upload, load, delete: the landing itself ─────────────────────────────


def _parquet(tmp_path: Path, rows=ROWS, name: str = "subs.parquet") -> Path:
    path = tmp_path / name
    con = duckdb.connect()
    try:
        values = ", ".join("(" + ", ".join(repr(v) for v in row) + ")" for row in rows)
        con.execute(
            f"COPY (SELECT * FROM (VALUES {values}) AS t(id, msisdn, zip)) TO '{path}' (FORMAT parquet)"
        )
    finally:
        con.close()
    return path


def _landing(fake: FakeDatabricks) -> "load.Landing":
    expose = _expose()
    target = load.databricks_load_target(expose["binding"], expose)
    return load.open_landing(target)


def _load(landing, path: Path, *, expected: int = 3, run_id: str = "run-1") -> Dict[str, Any]:
    return landing.load(
        str(path),
        product_id="bronze.subs",
        build_id="ingest",
        run_id=run_id,
        expected_rows=expected,
        logger=logging.getLogger("test"),
    )


def test_the_file_is_uploaded_loaded_and_the_staged_copy_deleted(fake, tmp_path):
    facts = _load(_landing(fake), _parquet(tmp_path))

    assert [r["id"] for r in fake.rows(TABLE)] == ["1", "2", "3"]
    assert facts["table"] == TABLE and facts["rows"] == 3
    assert facts["staged_files_deleted"] is True
    run_dir = f"{VOLUME}/bronze_subs/ingest/run-1"
    assert facts["staged_to"] == run_dir
    assert fake.volume_files() == [], "never leave landed data in the volume"
    puts = [p for m, p in fake.file_ops if m == "PUT"]
    assert puts == [f"{run_dir}/subs.parquet"]
    # in order: earlier runs looked for, the directory, the file, then (after the load) the file
    # and the directory go
    assert [m for m, _ in fake.file_ops] == ["LIST", "PUT-DIR", "PUT", "DELETE", "DELETE-DIR"]
    assert fake.sql_log[-1].startswith("INSERT OVERWRITE `main`.`bronze`.`subs`")
    assert fake.unknown == []


def test_the_uploaded_bytes_are_the_files_bytes(fake, tmp_path):
    path = _parquet(tmp_path)
    fake.fail_on("INSERT OVERWRITE")  # the load fails, so only the upload is looked at
    landing = _landing(fake)
    seen: List[bytes] = []
    real = fake._fs_put

    def spy(m, q, b):
        seen.append(b.get("_raw"))
        return real(m, q, b)

    fake.routes = [(mth, rx, spy if fn == real else fn) for mth, rx, fn in fake.routes]
    with pytest.raises(load.DatabricksLoadError):
        _load(landing, path)
    assert seen == [path.read_bytes()]


def test_a_second_load_replaces_the_rows_it_does_not_append(fake, tmp_path):
    landing = _landing(fake)
    _load(landing, _parquet(tmp_path), run_id="run-1")
    newer = _parquet(tmp_path, rows=[("9", "+1", "99999")], name="later.parquet")
    _load(landing, newer, expected=1, run_id="run-2")
    assert [r["id"] for r in fake.rows(TABLE)] == ["9"]
    assert fake.volume_files() == []


def test_the_load_keeps_the_table_s_definition_row_filter_and_masks(fake, tmp_path):
    fake.tables[TABLE]["row_filter"] = {
        "function_name": "main.bronze.fluid_rf_x",
        "input_column_names": ["id"],
    }
    fake.tables[TABLE]["columns"][1]["mask"] = {"function_name": "main.bronze.fluid_mask_y"}
    before = json.dumps(fake.tables[TABLE], sort_keys=True)
    _load(_landing(fake), _parquet(tmp_path))
    assert json.dumps(fake.tables[TABLE], sort_keys=True) == before
    assert not [
        s for s in fake.sql_log if s.lstrip().upper().startswith(("CREATE", "ALTER", "DROP"))
    ]
    assert not [s for s in fake.sql_log if "REPLACE TABLE" in s.upper()], "INSERT OVERWRITE only"


def test_a_missing_table_names_fluid_apply_and_uploads_nothing(fake, tmp_path):
    del fake.tables[TABLE]
    with pytest.raises(load.DatabricksLoadError) as exc:
        _load(_landing(fake), _parquet(tmp_path))
    assert "fluid apply" in str(exc.value) and TABLE in str(exc.value)
    assert fake.file_ops == [], "nothing was uploaded for a table that is not there"
    assert not [s for s in fake.sql_log if "INSERT" in s]
    assert TABLE not in fake.tables, "the loader never creates the table"


def test_a_failed_load_still_deletes_the_staged_files(fake, tmp_path):
    fake.fail_on("INSERT OVERWRITE", "[INSUFFICIENT_PERMISSIONS] no MODIFY on the table")
    with pytest.raises(load.DatabricksLoadError) as exc:
        _load(_landing(fake), _parquet(tmp_path))
    assert "INSUFFICIENT_PERMISSIONS" in str(exc.value)
    assert fake.volume_files() == []
    assert "deleted" in str(exc.value)
    assert fake.rows(TABLE) == []


def test_a_short_load_fails_the_build_and_deletes_the_staged_files(fake, tmp_path):
    with pytest.raises(load.DatabricksLoadError) as exc:
        _load(_landing(fake), _parquet(tmp_path), expected=4)
    assert f"loaded 3 rows into {TABLE}, but the landed file holds 4" in str(exc.value)
    assert fake.volume_files() == []


def test_a_load_that_reports_no_count_is_counted_in_the_table(fake, tmp_path):
    fake.insert_reports_count = False
    facts = _load(_landing(fake), _parquet(tmp_path))
    assert facts["rows"] == 3 and facts["rows_from"] == "count_after_load"
    assert any("COUNT" in s.upper() for s in fake.sql_log)


def test_a_staged_file_that_cannot_be_deleted_fails_the_run_naming_it(fake, tmp_path):
    fake.fail_route("DELETE", r"/fs/files/", 403, "no permission to delete")
    with pytest.raises(load.DatabricksLoadError) as exc:
        _load(_landing(fake), _parquet(tmp_path))
    text = str(exc.value)
    assert "could not delete" in text and "subs.parquet" in text
    assert [r["id"] for r in fake.rows(TABLE)] == ["1", "2", "3"], "the load itself worked"
    assert fake.volume_files() != [], "and the file is still there: the error says so"


def test_a_failed_upload_is_cleaned_up_and_nothing_loads(fake, tmp_path):
    fake.fail_route("PUT", r"/fs/files/", 403, "PERMISSION_DENIED: no WRITE VOLUME")
    with pytest.raises(load.DatabricksLoadError) as exc:
        _load(_landing(fake), _parquet(tmp_path))
    assert "PERMISSION_DENIED" in str(exc.value)
    assert not [s for s in fake.sql_log if "INSERT" in s]
    assert fake.volume_files() == []


def test_the_load_puts_each_column_in_the_column_of_its_name(fake, tmp_path):
    # fluid apply appended `id` to a live table that keeps the other two first: by position,
    # the msisdn values would land in `id`
    cols = fake.tables[TABLE]["columns"]
    fake.tables[TABLE]["columns"] = cols[1:] + cols[:1]
    _load(_landing(fake), _parquet(tmp_path))
    assert [r["id"] for r in fake.rows(TABLE)] == ["1", "2", "3"]
    assert fake.rows(TABLE)[0]["msisdn"] == ROWS[0][1]


def _volume_file(
    fake: FakeDatabricks, volume_path: str, data: bytes, *, age_seconds: float = 0
) -> None:
    real = fake._fs_path(volume_path)
    assert real is not None, volume_path
    Path(real).parent.mkdir(parents=True, exist_ok=True)
    Path(real).write_bytes(data)
    if age_seconds:
        when = time.time() - age_seconds
        os.utime(real, (when, when))


_OLD = load.STALE_STAGING_SECONDS + 60


def test_the_load_reads_the_uploaded_file_not_the_run_directory(fake, tmp_path):
    run_dir = f"{VOLUME}/bronze_subs/ingest/run-1"
    # someone with WRITE VOLUME drops a file into the run's directory before the INSERT
    planted = _parquet(tmp_path, rows=[("666", "+0", "00000")], name="planted.parquet")
    _volume_file(fake, f"{run_dir}/planted.parquet", planted.read_bytes())

    with pytest.raises(load.DatabricksLoadError) as exc:
        _load(_landing(fake), _parquet(tmp_path))

    insert = [s for s in fake.sql_log if s.startswith("INSERT OVERWRITE")]
    assert insert and insert[0].endswith(
        f"read_files('{run_dir}/subs.parquet', format => 'parquet')"
    )
    assert [r["id"] for r in fake.rows(TABLE)] == ["1", "2", "3"], "the planted rows are not loaded"
    # the directory cannot be removed while the planted file is in it: the run says so
    assert "could not delete" in str(exc.value) and run_dir in str(exc.value)


def test_an_interrupt_mid_load_still_deletes_the_staged_files(fake, tmp_path, monkeypatch):
    landing = _landing(fake)

    def interrupted(statement):
        raise KeyboardInterrupt

    monkeypatch.setattr(landing._sql, "execute", interrupted)
    with pytest.raises(KeyboardInterrupt):
        _load(landing, _parquet(tmp_path))
    assert fake.volume_files() == [], "Ctrl-C must not leave landed data in the volume"
    assert [m for m, _ in fake.file_ops][-2:] == ["DELETE", "DELETE-DIR"]


def test_an_interrupted_statement_is_cancelled_on_the_warehouse():
    from fluid_build.iac.providers.databricks_enforce import SqlRunner

    calls: List[tuple] = []

    class _Api:
        def do(self, method, path, **kwargs):
            calls.append((method, path))
            if method == "POST" and path.endswith("/statements/"):
                return {"statement_id": "s-1", "status": {"state": "RUNNING"}}
            if method == "GET":
                raise KeyboardInterrupt
            return {}

    class _Client:
        api_client = _Api()

    runner = SqlRunner(_Client(), "wh", sleep=lambda _s: None)
    with pytest.raises(KeyboardInterrupt):
        runner.execute("INSERT OVERWRITE `a`.`b`.`c` SELECT 1")
    assert ("POST", "/api/2.0/sql/statements/s-1/cancel") in calls


def test_staged_files_an_earlier_killed_run_left_are_deleted_first(fake, tmp_path):
    stale = f"{VOLUME}/bronze_subs/ingest/run-0"
    other_build = f"{VOLUME}/bronze_subs/other/run-0"
    _volume_file(fake, f"{stale}/subs.parquet", b"left by a run that was killed", age_seconds=_OLD)
    _volume_file(fake, f"{other_build}/subs.parquet", b"another build's run", age_seconds=_OLD)

    facts = _load(_landing(fake), _parquet(tmp_path), run_id="run-1")

    assert facts["earlier_runs_staging_deleted"] == [stale]
    assert fake.volume_files() == [f"{other_build}/subs.parquet"], "only this build's directory"
    assert [r["id"] for r in fake.rows(TABLE)] == ["1", "2", "3"]


def test_a_recent_run_of_the_same_build_keeps_its_staged_files(fake, tmp_path):
    # another run of this build, uploaded a minute ago, may still be loading
    running = f"{VOLUME}/bronze_subs/ingest/run-0"
    _volume_file(fake, f"{running}/subs.parquet", b"in flight", age_seconds=60)

    facts = _load(_landing(fake), _parquet(tmp_path), run_id="run-1")

    assert facts["earlier_runs_staging_deleted"] == []
    assert fake.volume_files() == [f"{running}/subs.parquet"]


def test_a_stale_run_that_cannot_be_deleted_does_not_stop_the_load(fake, tmp_path):
    stale = f"{VOLUME}/bronze_subs/ingest/run-0"
    _volume_file(fake, f"{stale}/subs.parquet", b"left behind", age_seconds=_OLD)
    fake.fail_route("DELETE", r"/fs/files/Volumes/.*/ingest/run-0/", 403, "PERMISSION_DENIED")
    facts = _load(_landing(fake), _parquet(tmp_path), run_id="run-1")
    assert facts["earlier_runs_staging_deleted"] == []
    assert fake.volume_files() == [f"{stale}/subs.parquet"]
    assert [r["id"] for r in fake.rows(TABLE)] == ["1", "2", "3"]


def test_a_volume_that_does_not_exist_names_it(fake, tmp_path, monkeypatch):
    monkeypatch.setenv(load.STAGING_VOLUME_ENV, "/Volumes/main/staging/nowhere")
    with pytest.raises(load.DatabricksLoadError) as exc:
        _load(_landing(fake), _parquet(tmp_path))
    assert "/Volumes/main/staging/nowhere" in str(exc.value)


def test_no_credential_reaches_an_error_message(fake, tmp_path, monkeypatch):
    secret = "dapi-0123456789abcdef-not-real"  # pragma: allowlist secret
    monkeypatch.setenv("DATABRICKS_TOKEN", secret)
    fake.fail_on("INSERT OVERWRITE", f"[FAILED] rejected the token {secret}")
    with pytest.raises(load.DatabricksLoadError) as exc:
        _load(_landing(fake), _parquet(tmp_path))
    assert secret not in str(exc.value)
    assert "rejected the token" in str(exc.value)


def test_nothing_calls_a_second_client_or_endpoint(fake, tmp_path):
    _load(_landing(fake), _parquet(tmp_path))
    assert fake.unknown == []
    allowed = ("/api/2.0/fs/", "/api/2.0/sql/statements", "/api/2.1/unity-catalog/tables/")
    assert all(path.startswith(allowed) for _, path in fake.requests), fake.requests


# ── the runner: a databricks binding lands in the table, never in a local file ────


def _source(tmp_path: Path, rows=ROWS) -> Path:
    src = tmp_path / "subs.csv"
    lines = ["id,msisdn,zip"] + [",".join(row) for row in rows]
    src.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return src


def _contract(
    tmp_path: Path,
    *,
    mode: str = "full_refresh",
    rows=ROWS,
    masking_rules: Optional[List[Dict[str, Any]]] = None,
    location: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    expose = _expose(**(location or {}))
    expose["binding"]["location"]["database"] = "main"
    if masking_rules is not None:
        expose["policy"] = {"privacy": {"masking": masking_rules}}
    return {
        "fluidVersion": "0.7.6",
        "kind": "DataProduct",
        "id": "bronze.subs",
        "name": "Subs",
        "builds": [
            {
                "id": "ingest",
                "pattern": "acquisition",
                "engine": "duckdb",
                "properties": {
                    "source": {
                        "kind": "filesystem",
                        "mode": mode,
                        "connection": {"uri": str(_source(tmp_path, rows))},
                        # all_varchar: the CSV reader would otherwise type msisdn and zip as numbers
                        "reader": {"format": "csv", "options": {"all_varchar": True}},
                        "streams": ["subs"],
                    },
                    "sink": {"format": "parquet"},
                },
                "outputs": ["subs"],
            }
        ],
        "exposes": [expose],
    }


def _run(contract: Dict[str, Any], tmp_path: Path) -> int:
    return execute_duckdb_build(contract["builds"][0], contract, tmp_path, dry_run=False)


def _sha(value: str) -> str:
    return hashlib.sha256((SALT + value).encode("utf-8")).hexdigest()


HASH = [{"column": "msisdn", "strategy": "hash"}]


def test_the_runner_lands_the_masked_rows_in_the_table(fake, tmp_path):
    assert _run(_contract(tmp_path, masking_rules=HASH), tmp_path) == 0

    rows = sorted(fake.rows(TABLE), key=lambda r: r["id"])
    assert [r["id"] for r in rows] == ["1", "2", "3"]
    assert [r["msisdn"] for r in rows] == [_sha("+46701234567"), _sha("+46709876543"), None]
    assert [r["zip"] for r in rows] == ["11122", "22233", "33344"], "the rest is unchanged"
    assert "+4670" not in json.dumps(rows), "no cleartext left the machine"
    assert fake.volume_files() == [], "and nothing is left in the volume"
    assert fake.unknown == []


def test_the_run_record_says_what_was_loaded(fake, tmp_path):
    assert _run(_contract(tmp_path, masking_rules=HASH), tmp_path) == 0
    (record,) = (tmp_path / ".fluid" / "runs" / "bronze.subs" / "ingest" / "runs").glob("*.json")
    facets = json.loads(record.read_text(encoding="utf-8"))["facets"]
    facts = facets["databricks_load"]
    assert facts["table"] == TABLE and facts["rows"] == 3
    assert facts["staged_files_deleted"] is True
    assert facts["staged_to"].startswith(f"{VOLUME}/bronze_subs/ingest/")
    assert facets["masking"]["columns"]["msisdn"]["strategy"] == "hash"
    assert VOLUME not in json.dumps(facets["landed"]), "the landed facet names no volume path"


@pytest.mark.parametrize(
    "location",
    [None, {"path": "./bronze/subs.parquet"}, {"path": "dbfs:/mnt/bronze/subs/"}],
)
def test_a_databricks_binding_never_writes_the_local_fallback(fake, tmp_path, location):
    """It used to write ``location.path``, or ``out/<stream>.parquet``, and report success."""
    assert _run(_contract(tmp_path, location=location), tmp_path) == 0
    assert [r["id"] for r in sorted(fake.rows(TABLE), key=lambda r: r["id"])] == ["1", "2", "3"]
    assert list((tmp_path / "out").glob("*")) == []
    assert not (tmp_path / "bronze").exists()
    assert not (tmp_path / "dbfs:").exists()
    written = {p.name for p in tmp_path.rglob("*.parquet")}
    assert written <= {"subs.parquet"}
    assert all(".fluid/staging" in str(p) for p in tmp_path.rglob("*.parquet"))


def test_a_second_run_overwrites_the_table(fake, tmp_path):
    assert _run(_contract(tmp_path), tmp_path) == 0
    again = _contract(tmp_path, rows=[("7", "+1", "00007")])
    assert _run(again, tmp_path) == 0
    assert [r["id"] for r in fake.rows(TABLE)] == ["7"]


def test_an_unset_staging_volume_is_refused_before_any_work(fake, tmp_path, monkeypatch, caplog):
    monkeypatch.delenv(load.STAGING_VOLUME_ENV)
    assert _run(_contract(tmp_path), tmp_path) != 0
    assert load.STAGING_VOLUME_ENV in caplog.text
    assert not (tmp_path / ".fluid" / "staging").exists(), "refused before anything landed"
    assert fake.requests == [], "and before the workspace was touched"


def test_an_unset_warehouse_is_refused_before_any_work(fake, tmp_path, monkeypatch, caplog):
    monkeypatch.delenv("DATABRICKS_WAREHOUSE_ID")
    assert _run(_contract(tmp_path), tmp_path) != 0
    assert "DATABRICKS_WAREHOUSE_ID" in caplog.text
    assert fake.requests == []


def test_an_unsupported_mode_is_refused_before_any_work(fake, tmp_path, caplog):
    assert _run(_contract(tmp_path, mode="incremental_append"), tmp_path) != 0
    assert "full_refresh" in caplog.text
    assert not (tmp_path / ".fluid" / "staging").exists()
    assert fake.requests == []


def test_a_missing_table_fails_the_build_naming_fluid_apply(fake, tmp_path, caplog):
    del fake.tables[TABLE]
    assert _run(_contract(tmp_path), tmp_path) != 0
    assert "fluid apply" in caplog.text
    assert not [p for m, p in fake.file_ops if m == "PUT"]


def test_a_failed_load_fails_the_build_and_leaves_the_volume_empty(fake, tmp_path, caplog):
    fake.fail_on("INSERT OVERWRITE", "[PERMISSION_DENIED] no MODIFY")
    assert _run(_contract(tmp_path, masking_rules=HASH), tmp_path) != 0
    assert "databricks load" in caplog.text and "PERMISSION_DENIED" in caplog.text
    assert fake.volume_files() == []


def test_a_local_binding_never_touches_databricks(fake, tmp_path):
    contract = _contract(tmp_path)
    out = tmp_path / "out" / "subs.parquet"
    contract["exposes"][0]["binding"] = {
        "platform": "local",
        "format": "parquet",
        "location": {"path": str(out)},
    }
    assert _run(contract, tmp_path) == 0
    assert out.exists()
    assert fake.requests == []


def test_each_build_loads_its_own_table(fake, tmp_path):
    """The target is the expose the build's ``outputs`` names, not ``exposes[0]``."""
    fake.create_table("main.bronze.other", COLUMNS)
    contract = _contract(tmp_path)
    decoy = _expose(table="other")
    decoy["exposeId"] = "other"
    contract["exposes"].insert(0, decoy)
    assert _run(contract, tmp_path) == 0
    assert fake.rows("main.bronze.other") == []
    assert len(fake.rows(TABLE)) == 3
