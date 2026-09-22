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

"""``_resolve_destination_path`` must honour ``location.bucket``.

The case these cover was uncovered: ``test_duckdb_minio_e2e`` points the
contract at a full ``s3://`` URI, which takes the ``_is_remote_uri`` branch and
always worked. A real AWS binding does not look like that — it names a bucket
and a bucket-relative path, exactly as the IaC planner expects — and that
combination was resolved to a LOCAL path, so the Glue table pointed at
``s3://bucket/prefix/`` while the rows were written to ``./prefix`` on disk,
with the build reporting success.

``test_bucket_relative_path_matches_the_iac_planner`` is the regression: it
asserts the runner and ``providers.aws.util.warehouse`` — which documents itself
as the sole writer of that string — agree on one location for one binding.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import pytest

from fluid_build.build_runners.duckdb.runner import _resolve_destination_path
from fluid_build.providers.aws.util.warehouse import get_iceberg_warehouse


class _Connection:
    def __init__(self):
        self.raw: Dict[str, Any] = {}


class _Source:
    def __init__(self, streams):
        self.streams = streams
        self.connection = _Connection()


class _Ctx:
    """The three attributes ``_resolve_destination_path`` actually reads."""

    def __init__(self, binding: Dict[str, Any], workdir: Path, streams=("public.orders",)):
        self.contract = {"exposes": [{"exposeId": "orders", "binding": binding}]}
        self.source = _Source(list(streams))
        self.workdir = str(workdir)


def _aws(**location) -> Dict[str, Any]:
    return {"platform": "aws", "format": "parquet", "location": location}


def test_bucket_relative_path_becomes_an_s3_uri(tmp_path):
    ctx = _Ctx(_aws(bucket="acme-lake", path="bronze/orders/", table="orders"), tmp_path)
    got = _resolve_destination_path(ctx, "public.orders", "parquet", tmp_path)
    assert got == "s3://acme-lake/bronze/orders/orders.parquet"


def test_a_prefix_gets_a_file_inside_it_not_an_object_named_like_the_prefix(tmp_path):
    """COPY TO a prefix writes one object whose key ends in "/", which anything
    listing that prefix for files cannot see — including the Glue table built
    from the same binding."""
    ctx = _Ctx(_aws(bucket="acme-lake", path="bronze/orders/", table="orders"), tmp_path)
    got = _resolve_destination_path(ctx, "public.orders", "parquet", tmp_path)
    assert not got.endswith("/"), "a prefix must not be written to directly"
    assert got.startswith("s3://acme-lake/bronze/orders/")


def test_a_bucket_relative_path_is_a_prefix_with_or_without_the_slash(tmp_path):
    """The planner makes this path the table's LOCATION either way, so the
    runner must write inside it either way. Depending on the author remembering
    a trailing slash would leave the original defect one character away."""
    for path in ("bronze/orders/", "bronze/orders"):
        ctx = _Ctx(_aws(bucket="acme-lake", path=path, table="orders"), tmp_path)
        got = _resolve_destination_path(ctx, "public.orders", "parquet", tmp_path)
        assert got == "s3://acme-lake/bronze/orders/orders.parquet", path


def test_the_runner_writes_inside_the_location_the_planner_declares(tmp_path):
    for path in ("bronze/orders/", "bronze/orders"):
        location = {"bucket": "acme-lake", "path": path, "table": "orders"}
        ctx = _Ctx(_aws(**location), tmp_path)
        runner_dest = _resolve_destination_path(ctx, "public.orders", "parquet", tmp_path)
        planner_dest = get_iceberg_warehouse(location, account_ref="123456789012")
        assert runner_dest.startswith(
            planner_dest.rstrip("/") + "/"
        ), f"{runner_dest} is not inside {planner_dest}"


def test_prefix_file_falls_back_to_the_stream_name(tmp_path):
    ctx = _Ctx(_aws(bucket="acme-lake", path="bronze/"), tmp_path)
    assert _resolve_destination_path(ctx, "public.orders", "parquet", tmp_path) == (
        "s3://acme-lake/bronze/public.orders.parquet"
    )


def test_bucket_relative_path_matches_the_iac_planner(tmp_path):
    """One binding, one location. This is the property that was violated."""
    location = {"bucket": "acme-lake", "path": "bronze/orders/", "region": "eu-west-1"}
    ctx = _Ctx(_aws(**location), tmp_path)
    runner_dest = _resolve_destination_path(ctx, "public.orders", "parquet", tmp_path)
    planner_dest = get_iceberg_warehouse(location, account_ref="123456789012")
    # The planner declares the table's location (a prefix); the runner writes a
    # file INSIDE it. Equality would be the wrong assertion — containment is the
    # property that was violated, and it is what the Glue table needs to be able
    # to list what the build produced.
    assert runner_dest.startswith(planner_dest), f"{runner_dest} is not inside {planner_dest}"
    assert runner_dest != planner_dest, "writing to the prefix itself is the bug"


def test_a_leading_slash_does_not_double(tmp_path):
    ctx = _Ctx(_aws(bucket="acme-lake", path="/bronze/orders/", table="orders"), tmp_path)
    assert _resolve_destination_path(ctx, "public.orders", "parquet", tmp_path) == (
        "s3://acme-lake/bronze/orders/orders.parquet"
    )


def test_env_template_in_the_bucket_is_resolved(tmp_path, monkeypatch):
    monkeypatch.setenv("DEMO_BUCKET", "from-the-environment")
    ctx = _Ctx(_aws(bucket="{{ env.DEMO_BUCKET }}", path="bronze/", table="orders"), tmp_path)
    assert _resolve_destination_path(ctx, "public.orders", "parquet", tmp_path) == (
        "s3://from-the-environment/bronze/orders.parquet"
    )


def test_unresolved_env_template_stays_local(tmp_path):
    """No bucket means no bucket. Inventing one would move data off the machine."""
    ctx = _Ctx(_aws(bucket="{{ env.NOT_SET_ANYWHERE }}", path="bronze/orders/"), tmp_path)
    got = _resolve_destination_path(ctx, "orders", "parquet", tmp_path)
    assert not got.startswith("s3://")
    assert got == str(tmp_path / "bronze/orders/")


def test_a_full_uri_naming_a_file_is_passed_through(tmp_path):
    """The branch the MinIO e2e covers: a URI naming a file is untouched."""
    ctx = _Ctx(_aws(bucket="ignored", path="s3://explicit/out/orders.parquet"), tmp_path)
    assert _resolve_destination_path(ctx, "public.orders", "parquet", tmp_path) == (
        "s3://explicit/out/orders.parquet"
    )


def test_a_full_uri_naming_a_prefix_also_gets_a_file(tmp_path):
    """A prefix is a prefix however it was spelled. Fixing only the composed
    spelling would leave the same defect reachable by the other one, which is
    the shape of the bug this change exists to remove."""
    ctx = _Ctx(_aws(bucket="ignored", path="s3://explicit/prefix/", table="orders"), tmp_path)
    assert _resolve_destination_path(ctx, "public.orders", "parquet", tmp_path) == (
        "s3://explicit/prefix/orders.parquet"
    )


def test_local_binding_is_unchanged(tmp_path):
    ctx = _Ctx({"platform": "local", "location": {"path": "./out/orders.parquet"}}, tmp_path)
    got = _resolve_destination_path(ctx, "orders", "parquet", tmp_path)
    assert got == str(tmp_path / "out/orders.parquet")
    assert (tmp_path / "out").is_dir(), "parent directories are still created"


def test_no_bucket_on_an_aws_binding_stays_local(tmp_path):
    ctx = _Ctx(_aws(path="bronze/orders/"), tmp_path)
    assert _resolve_destination_path(ctx, "orders", "parquet", tmp_path) == str(
        tmp_path / "bronze/orders/"
    )


def test_gcp_binding_with_a_bucket_uses_gs(tmp_path):
    ctx = _Ctx(
        {
            "platform": "gcp",
            "location": {"bucket": "acme-gcs", "path": "staging/orders/", "table": "orders"},
        },
        tmp_path,
    )
    assert _resolve_destination_path(ctx, "public.orders", "parquet", tmp_path) == (
        "gs://acme-gcs/staging/orders/orders.parquet"
    )


def test_an_unknown_platform_stays_local(tmp_path):
    ctx = _Ctx(
        {"platform": "mainframe", "location": {"bucket": "acme-lake", "path": "bronze/orders/"}},
        tmp_path,
    )
    got = _resolve_destination_path(ctx, "orders", "parquet", tmp_path)
    assert not got.startswith(("s3://", "gs://"))


def test_multiple_streams_ignore_the_binding_path(tmp_path):
    """Unchanged: with more than one stream the per-stream default wins."""
    ctx = _Ctx(
        _aws(bucket="acme-lake", path="bronze/orders/"),
        tmp_path,
        streams=("public.orders", "public.customers"),
    )
    assert _resolve_destination_path(ctx, "orders", "parquet", tmp_path) == str(
        tmp_path / "orders.parquet"
    )


@pytest.mark.parametrize("path", ["", None])
def test_absent_path_falls_back_to_the_default(tmp_path, path):
    ctx = _Ctx(_aws(bucket="acme-lake", path=path), tmp_path)
    assert _resolve_destination_path(ctx, "orders", "parquet", tmp_path) == str(
        tmp_path / "orders.parquet"
    )


def test_a_table_format_is_not_given_a_file_name(tmp_path):
    """``orders.bigquery_table`` would be worse than leaving the prefix alone."""
    ctx = _Ctx(
        {
            "platform": "gcp",
            "format": "bigquery_table",
            "location": {"bucket": "acme-gcs", "path": "staging/orders/", "table": "orders"},
        },
        tmp_path,
    )
    assert _resolve_destination_path(ctx, "public.orders", "bigquery_table", tmp_path) == (
        "gs://acme-gcs/staging/orders/"
    )


# ── The destination secret, which executes SQL ───────────────────────────
# The tests above exercise pure path composition. These cover the half that
# runs statements against DuckDB, which had no coverage at all and is where a
# raw f-string interpolation of `location.region` let a schema-valid contract
# execute stacked SQL before any data moved.


class _Conn:
    def __init__(self):
        self.statements: list[str] = []

    def execute(self, sql: str):
        self.statements.append(sql)


def _ctx_with_region(region, tmp_path, platform="aws"):
    return _Ctx(
        {
            "platform": platform,
            "location": {"bucket": "acme-lake", "path": "bronze/", "region": region},
        },
        tmp_path,
    )


def test_region_is_quoted_not_interpolated(tmp_path):
    from fluid_build.build_runners.duckdb.runner import _apply_destination_secret

    con = _Conn()
    _apply_destination_secret(
        con, _ctx_with_region("eu-west-1", tmp_path), "s3://acme-lake/bronze/x.parquet"
    )
    assert len(con.statements) == 1
    assert "REGION 'eu-west-1'" in con.statements[0]
    assert "PROVIDER credential_chain" in con.statements[0]


def test_a_region_carrying_sql_cannot_break_out_of_the_literal(tmp_path):
    """Reproduces the injection this file's first version shipped: a region of
    ``eu-west-1'); ATTACH ':memory:' AS pwned; --`` created a database."""
    import duckdb

    from fluid_build.build_runners.duckdb.runner import _apply_destination_secret

    evil = "eu-west-1'); ATTACH ':memory:' AS pwned; CREATE TABLE pwned.x(a INT); --"
    con = duckdb.connect()
    _apply_destination_secret(
        con, _ctx_with_region(evil, tmp_path), "s3://acme-lake/bronze/x.parquet"
    )
    names = {r[0] for r in con.execute("select database_name from duckdb_databases()").fetchall()}
    assert "pwned" not in names, "stacked statements executed from contract input"


def test_no_credential_chain_secret_for_gcs(tmp_path):
    """DuckDB's ``gcs`` secret is its S3-compatible one, so credential_chain
    there resolves the AWS chain and presents it to Google: accepted, then 403
    with a misleading message. GCS needs HMAC credentials this path lacks."""
    from fluid_build.build_runners.duckdb.runner import _apply_destination_secret

    con = _Conn()
    _apply_destination_secret(
        con,
        _ctx_with_region("europe-west1", tmp_path, platform="gcp"),
        "gs://acme/bronze/x.parquet",
    )
    assert con.statements == []


def test_an_explicit_contract_secret_is_not_overwritten(tmp_path):
    from fluid_build.build_runners.duckdb.runner import _apply_destination_secret

    ctx = _ctx_with_region("eu-west-1", tmp_path)
    ctx.source.connection.raw = {"s3": {"key_id": "AKIA", "secret": "s"}}
    con = _Conn()
    _apply_destination_secret(con, ctx, "s3://acme-lake/bronze/x.parquet")
    assert con.statements == [], "an explicit block in the contract must win"


# ── `fluid verify` must not look for an object-store artifact on disk ────


def test_verify_treats_a_bucket_binding_as_unsupported_not_failed():
    """Dispatch there is by ``format``, so ``platform: aws, format: parquet``
    fell into the local-file branch. "Not checked" and "check failed" are
    different answers, and the GCP branch already draws that line."""
    from fluid_build.cli.verify import _is_object_store_binding

    assert _is_object_store_binding(
        {"platform": "aws", "format": "parquet", "location": {"bucket": "b", "path": "p/"}}
    )
    assert _is_object_store_binding(
        {"platform": "azure", "location": {"bucket": "b", "path": "p/"}}
    )
    # A local binding still gets verified against the file it really wrote.
    assert not _is_object_store_binding(
        {"platform": "local", "format": "parquet", "location": {"path": "./out/x.parquet"}}
    )
    # An AWS binding with no bucket is the unchanged local case.
    assert not _is_object_store_binding({"platform": "aws", "location": {"path": "p/"}})
    assert not _is_object_store_binding(None)
    assert not _is_object_store_binding({})
