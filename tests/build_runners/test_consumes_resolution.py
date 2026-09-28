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

"""An embedded-SQL build reads what its ``consumes[]`` names.

``consumes[]`` was a logical reference no engine resolved: the local provider
logged ``local_consumes_not_bound`` and ran the SQL anyway, so a Silver
product's SQL had to spell out where its Bronze upstream happened to land on
each target, and the same contract could not run on a laptop and on AWS.

These tests hold the resolver (``build_runners/_embedded_sql_io.py``) to the
rules the build now follows: the upstream is found in the workspace by its id,
loaded with the SAME ``--env`` overlay, and read where its own binding lands
it (a local file anchored at the upstream's directory, or the S3 prefix the
duckdb acquisition runner writes into); explicit ``parameters.inputs`` win on a
name collision; anything unresolved fails before the SQL runs.
"""

from __future__ import annotations

import argparse
import contextlib
import fnmatch
import json
import logging
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional
from unittest.mock import patch

import pytest
import yaml

from fluid_build.build_runners._embedded_sql_io import (
    ConsumesResolutionError,
    EmbeddedSqlLandingError,
    MaskingNotAppliedError,
    UnreadableBindingError,
    object_store_landing,
    resolve_consumes,
)
from fluid_build.build_runners.base import _execute_embedded_sql_build, run_builds_from_args

duckdb = pytest.importorskip("duckdb")

BRONZE = "bronze.customer_subscriptions"
SQL = "SELECT product_id, status, COUNT(*) AS subscription_count " "FROM subscriptions GROUP BY 1,2"
ROWS = [("p1", "active"), ("p1", "active"), ("p2", "ended")]


# ── Workspace builders ──────────────────────────────────────────────────


def _dump(path: Path, doc: Dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return path


def _workspace(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "fluid.workspace.yaml").write_text("workspace: {name: t}\n", encoding="utf-8")
    return root


def _parquet(path: Path, rows=ROWS) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    values = ", ".join(f"('{p}', '{s}')" for p, s in rows)
    duckdb.sql(
        f"COPY (SELECT * FROM (VALUES {values}) t(product_id, status)) "
        f"TO '{path}' (FORMAT parquet)"
    )
    return path


def _bronze(root: Path, binding: Optional[Dict[str, Any]] = None, *, pid: str = BRONZE) -> Path:
    binding = binding or {
        "platform": "local",
        "format": "parquet",
        "location": {"path": "out/customer_subscriptions.parquet"},
    }
    return _dump(
        root / "contracts" / "customer_subscriptions" / "contract.fluid.yaml",
        {
            "fluidVersion": "0.7.5",
            "kind": "DataProduct",
            "id": pid,
            "name": "Customer Subscriptions",
            "exposes": [{"exposeId": "subscriptions", "kind": "table", "binding": binding}],
        },
    )


def _silver_build(sql: str = SQL, inputs: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    props: Dict[str, Any] = {"sql": sql}
    if inputs is not None:
        props["parameters"] = {"inputs": inputs}
    return {
        "id": "summarize_subscription_status",
        "pattern": "embedded-logic",
        "engine": "duckdb",
        "properties": props,
    }


def _silver(
    root: Path,
    *,
    consumes: Optional[List[Dict[str, Any]]] = None,
    build: Optional[Dict[str, Any]] = None,
    binding: Optional[Dict[str, Any]] = None,
    expose_extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    expose: Dict[str, Any] = {
        "exposeId": "status_summary",
        "kind": "table",
        "binding": binding
        or {
            "platform": "local",
            "format": "parquet",
            "location": {"path": "out/subscription_status_summary.parquet"},
        },
    }
    expose.update(expose_extra or {})
    contract = {
        "fluidVersion": "0.7.5",
        "kind": "DataProduct",
        "id": "silver.subscription_status_summary",
        "name": "Subscription Status Summary",
        "consumes": (
            consumes
            if consumes is not None
            else [{"productId": BRONZE, "exposeId": "subscriptions"}]
        ),
        "builds": [build or _silver_build()],
        "exposes": [expose],
    }
    _dump(root / "contracts" / "subscription_status_summary" / "contract.fluid.yaml", contract)
    return contract


def _silver_dir(root: Path) -> Path:
    return root / "contracts" / "subscription_status_summary"


def _read(path: Path) -> List[tuple]:
    return duckdb.sql(f"SELECT * FROM '{path}' ORDER BY 1, 2").fetchall()


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """No stray upstream roots, and the provider's apply log lands in tmp."""
    monkeypatch.delenv("FLUID_UPSTREAM_CONTRACTS", raising=False)
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def printed() -> Iterator[List[str]]:
    lines: List[str] = []
    with patch(
        "fluid_build.build_runners.base.cprint",
        side_effect=lambda *a, **_k: lines.append(" ".join(str(x) for x in a)),
    ):
        yield lines


def _run(root: Path, contract: Dict[str, Any], *, env: Optional[str] = None, dry_run=False) -> int:
    return _execute_embedded_sql_build(
        contract["builds"][0], contract, _silver_dir(root), dry_run=dry_run, env=env
    )


# ── Resolution: where the upstream is read from ─────────────────────────


def test_local_upstream_is_read_where_its_own_binding_lands_it(tmp_path):
    """A relative path is the UPSTREAM contract's, not this build's or the cwd's."""
    root = _workspace(tmp_path / "ws")
    _bronze(root)
    contract = _silver(root)
    resolved, covered, ws = resolve_consumes(
        contract, contract["builds"][0], _silver_dir(root), env=None
    )
    assert covered == []
    assert ws == root.resolve()
    [r] = resolved
    assert (r.product_id, r.expose_id, r.view, r.format) == (
        BRONZE,
        "subscriptions",
        "subscriptions",
        "parquet",
    )
    assert (
        Path(r.uri) == root / "contracts/customer_subscriptions/out/customer_subscriptions.parquet"
    )


def test_env_placeholders_in_the_upstream_path_resolve(tmp_path, monkeypatch):
    root = _workspace(tmp_path / "ws")
    monkeypatch.setenv("FLUID_DATA_DIR", str(tmp_path / "data"))
    _bronze(
        root,
        {
            "platform": "local",
            "format": "parquet",
            "location": {"path": "{{ env.FLUID_DATA_DIR }}/customer_subscriptions/cs.parquet"},
        },
    )
    contract = _silver(root)
    [r], _, _ = resolve_consumes(contract, contract["builds"][0], _silver_dir(root))
    assert r.uri == str(tmp_path / "data" / "customer_subscriptions" / "cs.parquet")


def test_an_unset_variable_in_the_upstream_path_is_refused_naming_it(tmp_path, monkeypatch):
    """Resolved to "" it would read ``/customer_subscriptions/cs.parquet``."""
    root = _workspace(tmp_path / "ws")
    monkeypatch.delenv("FLUID_DATA_DIR", raising=False)
    _bronze(
        root,
        {
            "platform": "local",
            "format": "parquet",
            "location": {"path": "{{ env.FLUID_DATA_DIR }}/cs.parquet"},
        },
    )
    contract = _silver(root)
    with pytest.raises(ConsumesResolutionError, match="FLUID_DATA_DIR"):
        resolve_consumes(contract, contract["builds"][0], _silver_dir(root))


def test_a_credential_shaped_variable_in_a_location_is_never_resolved(tmp_path, monkeypatch):
    """The resolved uri is printed and logged: a discovered upstream must not
    be able to have a secret echoed by spelling it into its path."""
    root = _workspace(tmp_path / "ws")
    monkeypatch.setenv(
        "LAKE_SECRET_TOKEN", "s3kr1t-value-never-printed"
    )  # pragma: allowlist secret
    _bronze(
        root,
        {
            "platform": "local",
            "format": "parquet",
            "location": {"path": "/tmp/{{ env.LAKE_SECRET_TOKEN }}/cs.parquet"},
        },
    )
    contract = _silver(root)
    with pytest.raises(ConsumesResolutionError, match="looks like a credential") as err:
        resolve_consumes(contract, contract["builds"][0], _silver_dir(root))
    assert "s3kr1t-value-never-printed" not in err.value.as_json()


def test_the_upstream_is_loaded_with_the_same_env_overlay(tmp_path):
    root = _workspace(tmp_path / "ws")
    bronze = _bronze(root)
    _dump(
        bronze.parent / "overlays" / "staging.yaml",
        {
            "exposes": [
                {
                    "binding": {
                        "platform": "local",
                        "format": "parquet",
                        "location": {"path": "staging/cs.parquet"},
                    }
                }
            ]
        },
    )
    contract = _silver(root)
    build = contract["builds"][0]
    [base], _, _ = resolve_consumes(contract, build, _silver_dir(root), env=None)
    [staged], _, _ = resolve_consumes(contract, build, _silver_dir(root), env="staging")
    assert base.uri.endswith("out/customer_subscriptions.parquet")
    assert Path(staged.uri) == bronze.parent / "staging" / "cs.parquet"


def test_an_aws_binding_is_read_as_the_prefix_the_acquisition_runner_writes_into(tmp_path):
    """The glob must match the object the duckdb runner lands for that binding."""
    from fluid_build.build_runners.duckdb.runner import _resolve_destination_path

    root = _workspace(tmp_path / "ws")
    binding = {
        "platform": "aws",
        "format": "parquet",
        "location": {
            "database": "demo_bronze",
            "table": "customer_subscriptions",
            "bucket": "northwind-demo-lake",
            "path": "bronze/customer_subscriptions/",
            "region": "eu-north-1",
        },
    }
    _bronze(root, binding)
    contract = _silver(root)
    [r], _, _ = resolve_consumes(contract, contract["builds"][0], _silver_dir(root))
    assert r.uri == "s3://northwind-demo-lake/bronze/customer_subscriptions/*.parquet"
    assert (r.platform, r.region) == ("aws", "eu-north-1")

    written = _resolve_destination_path(
        _AcquisitionCtx(binding, tmp_path), "public.product_subscription", "parquet", tmp_path
    )
    assert fnmatch.fnmatch(written, r.uri), f"{written} is not read by {r.uri}"


def test_a_json_upstream_prefix_globs_the_files_the_runner_names_ndjson(tmp_path):
    root = _workspace(tmp_path / "ws")
    _bronze(
        root,
        {"platform": "aws", "format": "json", "location": {"bucket": "b", "path": "bronze/cs"}},
    )
    contract = _silver(root)
    [r], _, _ = resolve_consumes(contract, contract["builds"][0], _silver_dir(root))
    assert (r.uri, r.format) == ("s3://b/bronze/cs/*.ndjson", "json")


@pytest.mark.parametrize(
    "binding, platform",
    [
        (
            {
                "platform": "gcp",
                "format": "bigquery_table",
                "location": {"project": "p", "dataset": "d", "table": "t"},
            },
            "gcp",
        ),
        (
            {
                "platform": "snowflake",
                "format": "snowflake_table",
                "location": {"database": "D", "schema": "S", "table": "T"},
            },
            "snowflake",
        ),
        (
            {
                "platform": "aws",
                "format": "redshift_table",
                "location": {"database": "d", "schema": "s", "table": "t", "bucket": "b"},
            },
            "aws",
        ),
        (
            {"platform": "aws", "format": "parquet", "location": {"database": "d", "table": "t"}},
            "aws",
        ),
        ({"platform": "kafka", "format": "kafka_topic", "location": {"topic": "t"}}, "kafka"),
        ({"platform": "local", "format": "parquet", "location": {"path": "gs://b/p/"}}, "local"),
    ],
)
def test_a_binding_the_engine_cannot_read_is_a_typed_error_naming_the_platform(
    tmp_path, binding, platform
):
    root = _workspace(tmp_path / "ws")
    _bronze(root, binding)
    contract = _silver(root)
    with pytest.raises(UnreadableBindingError) as info:
        resolve_consumes(contract, contract["builds"][0], _silver_dir(root))
    assert info.value.extras["platform"] == platform
    assert platform in info.value.what


def test_an_id_declared_twice_is_refused_naming_both_files(tmp_path):
    root = _workspace(tmp_path / "ws")
    first = _bronze(root)
    second = _dump(
        root / "contracts" / "copy_of_subscriptions" / "contract.fluid.yaml",
        yaml.safe_load(first.read_text()),
    )
    contract = _silver(root)
    with pytest.raises(ConsumesResolutionError) as info:
        resolve_consumes(contract, contract["builds"][0], _silver_dir(root))
    message = f"{info.value.what} {info.value.why}"
    assert BRONZE in message
    assert str(first) in message and str(second) in message


def test_a_copy_in_an_output_or_venv_directory_is_not_a_second_declaration(tmp_path):
    root = _workspace(tmp_path / "ws")
    bronze = _bronze(root)
    for stale in ("dist/artifacts", ".fluid-venv/lib", "contracts/customer_subscriptions/runtime"):
        _dump(root / stale / "contract.fluid.yaml", yaml.safe_load(bronze.read_text()))
    contract = _silver(root)
    [r], _, _ = resolve_consumes(contract, contract["builds"][0], _silver_dir(root))
    assert r.contract_path == bronze


def test_a_missing_upstream_names_the_product_and_where_it_looked(tmp_path):
    root = _workspace(tmp_path / "ws")
    contract = _silver(root)
    with pytest.raises(ConsumesResolutionError) as info:
        resolve_consumes(contract, contract["builds"][0], _silver_dir(root))
    assert BRONZE in info.value.what
    assert str(root.resolve()) in info.value.why
    assert "workspace root" in info.value.why


def test_without_a_workspace_file_it_says_it_looked_nowhere(tmp_path):
    contract = _silver(tmp_path / "loose")
    with pytest.raises(ConsumesResolutionError) as info:
        resolve_consumes(contract, contract["builds"][0], _silver_dir(tmp_path / "loose"))
    assert "nowhere" in info.value.why and "fluid.workspace.yaml" in info.value.why


def test_fluid_upstream_contracts_adds_a_root(tmp_path, monkeypatch):
    """The existing mechanism for an upstream kept in another repository."""
    root = _workspace(tmp_path / "ws")
    other = tmp_path / "bronze-repo"
    bronze = _bronze(other)
    monkeypatch.setenv("FLUID_UPSTREAM_CONTRACTS", str(other))
    contract = _silver(root)
    [r], _, _ = resolve_consumes(contract, contract["builds"][0], _silver_dir(root))
    assert r.contract_path == bronze


def test_a_missing_expose_lists_the_ones_the_upstream_has(tmp_path):
    root = _workspace(tmp_path / "ws")
    _bronze(root)
    contract = _silver(
        root,
        consumes=[{"productId": BRONZE, "exposeId": "orders"}],
        build=_silver_build(sql="SELECT COUNT(*) AS n FROM orders"),
    )
    with pytest.raises(ConsumesResolutionError) as info:
        resolve_consumes(contract, contract["builds"][0], _silver_dir(root))
    assert "orders" in info.value.what and "subscriptions" in info.value.why


def test_an_explicit_input_of_the_same_name_wins_and_is_not_resolved(tmp_path):
    """No upstream in the workspace at all: the explicit input is the binding."""
    root = _workspace(tmp_path / "ws")
    build = _silver_build(inputs=[{"name": "subscriptions", "path": "elsewhere.parquet"}])
    contract = _silver(root, build=build)
    resolved, covered, _ = resolve_consumes(contract, build, _silver_dir(root))
    assert resolved == []
    assert [(c.product_id, c.expose_id, c.input_name) for c in covered] == [
        (BRONZE, "subscriptions", "subscriptions")
    ]


@pytest.mark.parametrize("expose_id", ["status-summary", "v1.orders", "1st"])
def test_an_expose_id_that_cannot_name_a_view_is_refused(tmp_path, expose_id):
    """The SQL reads it (as a quoted name), so it must be a view; it cannot be one."""
    root = _workspace(tmp_path / "ws")
    contract = _silver(
        root,
        consumes=[{"productId": BRONZE, "exposeId": expose_id}],
        build=_silver_build(sql=f'SELECT COUNT(*) AS n FROM "{expose_id}"'),
    )
    with pytest.raises(ConsumesResolutionError, match="cannot name a SQL view"):
        resolve_consumes(contract, contract["builds"][0], _silver_dir(root))


def test_two_entries_that_would_share_a_view_are_refused(tmp_path):
    root = _workspace(tmp_path / "ws")
    contract = _silver(
        root,
        consumes=[
            {"productId": BRONZE, "exposeId": "subscriptions"},
            {"productId": "bronze.other", "exposeId": "Subscriptions"},
        ],
    )
    with pytest.raises(ConsumesResolutionError, match="also"):
        resolve_consumes(contract, contract["builds"][0], _silver_dir(root))


def test_an_env_that_is_a_path_never_selects_an_overlay(tmp_path):
    root = _workspace(tmp_path / "ws")
    _bronze(root)
    contract = _silver(root)
    with pytest.raises(ConsumesResolutionError, match="not an environment name"):
        resolve_consumes(contract, contract["builds"][0], _silver_dir(root), env="../../etc/x")


# ── The build: SQL runs against the resolved views ──────────────────────


def test_silver_builds_from_bronze_with_no_inputs_block(tmp_path, printed):
    root = _workspace(tmp_path / "ws")
    _parquet(root / "contracts/customer_subscriptions/out/customer_subscriptions.parquet")
    _bronze(root)
    contract = _silver(root)
    assert _run(root, contract) == 0
    out = _silver_dir(root) / "out" / "subscription_status_summary.parquet"
    assert _read(out) == [("p1", "active", 2), ("p2", "ended", 1)]
    assert any(f"consumes {BRONZE}/subscriptions" in line for line in printed)


def test_a_view_name_that_is_a_sql_keyword_still_registers(tmp_path):
    """The view is a quoted identifier, so ``exposeId: order`` works as ``"order"``."""
    root = _workspace(tmp_path / "ws")
    _parquet(root / "contracts/customer_subscriptions/out/o.parquet")
    _dump(
        root / "contracts/customer_subscriptions/contract.fluid.yaml",
        {
            "id": BRONZE,
            "exposes": [
                {
                    "exposeId": "order",
                    "binding": {
                        "platform": "local",
                        "format": "parquet",
                        "location": {"path": "out/o.parquet"},
                    },
                }
            ],
        },
    )
    contract = _silver(
        root,
        consumes=[{"productId": BRONZE, "exposeId": "order"}],
        build=_silver_build(sql='SELECT COUNT(*) AS n FROM "order"'),
    )
    assert _run(root, contract) == 0
    out = _silver_dir(root) / "out" / "subscription_status_summary.parquet"
    assert duckdb.sql(f"SELECT n FROM '{out}'").fetchall() == [(3,)]


def test_the_run_env_reaches_the_upstream(tmp_path, monkeypatch):
    """``fluid apply --env staging`` reads the upstream's staging binding."""
    root = _workspace(tmp_path / "ws")
    bronze = _bronze(root)
    _parquet(bronze.parent / "out" / "customer_subscriptions.parquet")
    _parquet(bronze.parent / "staging" / "cs.parquet", rows=[("s1", "trial")])
    _dump(
        bronze.parent / "overlays" / "staging.yaml",
        {
            "exposes": [
                {
                    "binding": {
                        "platform": "local",
                        "format": "parquet",
                        "location": {"path": "staging/cs.parquet"},
                    }
                }
            ]
        },
    )
    _silver(root)
    args = argparse.Namespace(
        contract=str(_silver_dir(root) / "contract.fluid.yaml"),
        env="staging",
        build_id=None,
        dry_run=False,
        fail_fast=True,
        delay=0,
        no_output=False,
        sample_rows=None,
    )
    with patch("fluid_build.build_runners.base.cprint"):
        assert run_builds_from_args(args, logging.getLogger("t")) == 0
    out = _silver_dir(root) / "out" / "subscription_status_summary.parquet"
    assert _read(out) == [("s1", "trial", 1)]


def test_a_bundle_run_without_env_reads_the_env_its_manifest_records(tmp_path):
    """``fluid apply bundle.tgz`` carries its overlay applied: that env is the run's."""
    import io
    import tarfile

    from fluid_build.build_runners.base import _run_env

    bundle = tmp_path / "bundle.tgz"
    manifest = json.dumps({"source": {"contract": "c.yaml", "env": "aws"}}).encode()
    with tarfile.open(bundle, "w:gz") as tar:
        info = tarfile.TarInfo("MANIFEST.json")
        info.size = len(manifest)
        tar.addfile(info, io.BytesIO(manifest))

    assert _run_env(argparse.Namespace(contract=str(bundle), env=None)) == "aws"
    assert _run_env(argparse.Namespace(contract="plan.json", bundle=str(bundle))) == "aws"
    assert _run_env(argparse.Namespace(contract=str(bundle), env="staging")) == "staging"
    assert _run_env(argparse.Namespace(contract="contract.fluid.yaml", env=None)) is None


def test_an_unresolved_upstream_fails_the_build_before_the_sql_runs(tmp_path, printed):
    root = _workspace(tmp_path / "ws")
    contract = _silver(root)
    assert _run(root, contract) == 1
    assert not (_silver_dir(root) / "out").exists(), "nothing may land"
    assert any(BRONZE in line for line in printed)


def test_an_upstream_that_has_not_landed_fails_saying_why(tmp_path, printed):
    """Resolved, but the file is not there yet: the build fails, and says so."""
    root = _workspace(tmp_path / "ws")
    _bronze(root)  # declared, never built
    contract = _silver(root)
    assert _run(root, contract) == 1
    assert not (_silver_dir(root) / "out").exists(), "nothing may land"
    missing = str(root / "contracts/customer_subscriptions/out/customer_subscriptions.parquet")
    assert any("Input file not found" in line and missing in line for line in printed), printed
    assert any(f"build {BRONZE} first" in line for line in printed), printed


def test_an_explicit_input_wins_over_the_resolved_upstream(tmp_path, printed):
    """Both entries of a join: one bound by hand, the other resolved."""
    root = _workspace(tmp_path / "ws")
    bronze = _bronze(root)
    _parquet(bronze.parent / "out" / "customer_subscriptions.parquet")
    hand = _parquet(tmp_path / "hand" / "subs.parquet", rows=[("p9", "active")])
    _dump(
        root / "contracts/customers/contract.fluid.yaml",
        {
            "id": "bronze.customers",
            "exposes": [
                {
                    "exposeId": "customers",
                    "binding": {
                        "platform": "local",
                        "format": "csv",
                        "location": {"path": "out/customers.csv"},
                    },
                }
            ],
        },
    )
    (root / "contracts/customers/out").mkdir(parents=True)
    (root / "contracts/customers/out/customers.csv").write_text("product_id,label\np9,Nine\n")
    build = _silver_build(
        sql=(
            "SELECT c.label, s.status, COUNT(*) AS n FROM subscriptions s "
            "JOIN customers c USING (product_id) GROUP BY 1, 2"
        ),
        inputs=[{"name": "subscriptions", "path": str(hand)}],
    )
    contract = _silver(
        root,
        consumes=[
            {"productId": BRONZE, "exposeId": "subscriptions"},
            {"productId": "bronze.customers", "exposeId": "customers"},
        ],
        build=build,
    )
    assert _run(root, contract) == 0
    out = _silver_dir(root) / "out" / "subscription_status_summary.parquet"
    assert _read(out) == [("Nine", "active", 1)]
    assert any("explicit input 'subscriptions' wins" in line for line in printed)


def test_the_resolved_inputs_are_recorded(tmp_path):
    """This path writes no run record; the provider's apply log is its record."""
    root = _workspace(tmp_path / "ws")
    _parquet(root / "contracts/customer_subscriptions/out/customer_subscriptions.parquet")
    _bronze(root)
    contract = _silver(root)
    with patch("fluid_build.build_runners.base.cprint"):
        assert _run(root, contract) == 0
    last = json.loads((tmp_path / "runtime/out/local_apply_log.jsonl").read_text().splitlines()[-1])
    [entry] = last["inputs"]
    assert entry["productId"] == BRONZE and entry["exposeId"] == "subscriptions"
    assert entry["uri"].endswith(
        "contracts/customer_subscriptions/out/customer_subscriptions.parquet"
    )


def test_a_dry_run_reports_the_bindings_and_writes_nothing(tmp_path, printed):
    root = _workspace(tmp_path / "ws")
    _bronze(root)
    contract = _silver(root)
    assert _run(root, contract, dry_run=True) == 0
    assert any('as view "subscriptions"' in line for line in printed)
    assert not (_silver_dir(root) / "out").exists()


def test_a_json_upstream_is_read_as_json(tmp_path):
    root = _workspace(tmp_path / "ws")
    data = root / "contracts/customer_subscriptions/out/cs.ndjson"
    data.parent.mkdir(parents=True)
    data.write_text("\n".join(json.dumps({"product_id": p, "status": s}) for p, s in ROWS) + "\n")
    _bronze(
        root,
        {"platform": "local", "format": "json", "location": {"path": "out/cs.ndjson"}},
    )
    contract = _silver(root)
    with patch("fluid_build.build_runners.base.cprint"):
        assert _run(root, contract) == 0
    out = _silver_dir(root) / "out" / "subscription_status_summary.parquet"
    assert _read(out) == [("p1", "active", 2), ("p2", "ended", 1)]


def test_masking_is_refused_before_anything_lands(tmp_path, printed):
    """Cleartext must never land silently where masking was declared."""
    root = _workspace(tmp_path / "ws")
    _parquet(root / "contracts/customer_subscriptions/out/customer_subscriptions.parquet")
    _bronze(root)
    contract = _silver(
        root,
        expose_extra={
            "policy": {"privacy": {"masking": [{"column": "product_id", "strategy": "hash"}]}}
        },
    )
    assert _run(root, contract) == 1
    assert not (_silver_dir(root) / "out").exists()
    assert any("does not apply masking yet" in line or "masking" in line for line in printed)
    with pytest.raises(MaskingNotAppliedError, match="product_id|masking"):
        from fluid_build.build_runners._embedded_sql_io import refuse_unapplied_masking

        refuse_unapplied_masking(contract, contract["builds"][0])


# ── Landing on an AWS binding ───────────────────────────────────────────


_SILVER_AWS = {
    "platform": "aws",
    "format": "parquet",
    "location": {
        "database": "demo_silver",
        "table": "subscription_status_summary",
        "bucket": "northwind-demo-lake",
        "path": "silver/subscription_status_summary/",
        "region": "eu-north-1",
    },
}


class _Connection:
    raw: Dict[str, Any] = {}


class _Source:
    def __init__(self, streams):
        self.streams = streams
        self.connection = _Connection()


class _AcquisitionCtx:
    """What ``duckdb.runner._resolve_destination_path`` reads of a run."""

    def __init__(self, binding: Dict[str, Any], workdir: Path, expose_id: str = "subscriptions"):
        self.contract = {"exposes": [{"exposeId": expose_id, "binding": binding}]}
        self.source = _Source(["public.product_subscription"])
        self.workdir = str(workdir)


def test_the_landing_is_the_object_the_acquisition_runner_writes(tmp_path):
    from fluid_build.build_runners.duckdb.runner import _resolve_destination_path

    contract = {"exposes": [{"exposeId": "status_summary", "binding": _SILVER_AWS}]}
    landing = object_store_landing(contract, {})
    assert landing is not None
    runner_writes = _resolve_destination_path(
        _AcquisitionCtx(_SILVER_AWS, tmp_path, "status_summary"),
        "public.product_subscription",
        "parquet",
        tmp_path,
    )
    assert (
        landing.uri
        == runner_writes
        == (
            "s3://northwind-demo-lake/silver/subscription_status_summary/"
            "subscription_status_summary.parquet"
        )
    )
    assert (landing.format, landing.region) == ("parquet", "eu-north-1")


def test_the_landing_is_inside_the_glue_table_fluid_apply_declares(tmp_path):
    """So ``fluid verify --env aws`` (Athena ``COUNT(*)`` over that table) counts it."""
    from fluid_build.cli._verify_athena import is_athena_verifiable
    from fluid_build.iac import get_iac_plugin

    contract = {
        "id": "silver.subscription_status_summary",
        "exposes": [
            {
                "exposeId": "status_summary",
                "binding": _SILVER_AWS,
                "contract": {"schema": [{"name": "status", "type": "VARCHAR"}]},
            }
        ],
    }
    resources = get_iac_plugin("aws").emit(contract)
    [table] = resources["aws_glue_catalog_table"].values()
    location = table["storage_descriptor"]["location"]
    landing = object_store_landing(contract, {})
    assert landing is not None and is_athena_verifiable(_SILVER_AWS)
    assert landing.uri.startswith(location.rstrip("/") + "/"), (landing.uri, location)


def test_a_local_binding_keeps_its_local_landing():
    contract = {
        "exposes": [
            {
                "exposeId": "x",
                "binding": {"platform": "local", "format": "parquet", "location": {"path": "o.pq"}},
            }
        ]
    }
    assert object_store_landing(contract, {}) is None


def test_an_unset_bucket_variable_is_refused_not_landed_locally(monkeypatch):
    monkeypatch.delenv("LAKE_BUCKET", raising=False)
    binding = json.loads(json.dumps(_SILVER_AWS))
    binding["location"]["bucket"] = "{{ env.LAKE_BUCKET }}"
    with pytest.raises(ConsumesResolutionError, match="LAKE_BUCKET"):
        object_store_landing({"exposes": [{"exposeId": "s", "binding": binding}]}, {})


def test_a_format_this_path_cannot_write_to_s3_is_refused():
    binding = json.loads(json.dumps(_SILVER_AWS))
    binding["format"] = "iceberg"
    with pytest.raises(EmbeddedSqlLandingError, match="iceberg"):
        object_store_landing({"exposes": [{"exposeId": "s", "binding": binding}]}, {})


# ── The shared S3 secret: endpoint override ─────────────────────────────


class _Recorder:
    def __init__(self):
        self.statements: List[str] = []

    def execute(self, sql: str):
        self.statements.append(sql)


@pytest.fixture
def no_endpoint(monkeypatch):
    for var in ("AWS_ENDPOINT_URL", "AWS_ENDPOINT_URL_S3", "AWS_IGNORE_CONFIGURED_ENDPOINT_URLS"):
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


def test_the_secret_follows_an_aws_endpoint_url_override(no_endpoint):
    from fluid_build.build_runners.duckdb.runner import create_s3_credential_chain_secret

    no_endpoint.setenv("AWS_ENDPOINT_URL", "http://127.0.0.1:5001")
    con = _Recorder()
    assert create_s3_credential_chain_secret(con, region="eu-north-1", scope="s3://b", name="__t")
    [sql] = con.statements
    assert "PROVIDER credential_chain" in sql and "REGION 'eu-north-1'" in sql
    assert "ENDPOINT '127.0.0.1:5001'" in sql and "USE_SSL false" in sql
    assert "URL_STYLE 'path'" in sql and "SCOPE 's3://b'" in sql


def test_the_s3_specific_endpoint_wins_and_aws_hosts_keep_virtual_hosting(no_endpoint):
    from fluid_build.build_runners.duckdb.runner import create_s3_credential_chain_secret

    no_endpoint.setenv("AWS_ENDPOINT_URL", "http://127.0.0.1:5001")
    no_endpoint.setenv("AWS_ENDPOINT_URL_S3", "https://s3.eu-north-1.amazonaws.com")
    con = _Recorder()
    create_s3_credential_chain_secret(con)
    [sql] = con.statements
    assert "ENDPOINT 's3.eu-north-1.amazonaws.com'" in sql and "USE_SSL true" in sql
    assert "URL_STYLE" not in sql


@pytest.mark.parametrize(
    "value, ignore",
    [
        ("http://127.0.0.1:5001", "true"),
        ("ftp://127.0.0.1", None),
        ("http://user:pw@127.0.0.1:5001", None),  # pragma: allowlist secret
    ],
)
def test_an_endpoint_override_is_ignored_when_disabled_or_unusable(no_endpoint, value, ignore):
    from fluid_build.build_runners.duckdb.runner import create_s3_credential_chain_secret

    no_endpoint.setenv("AWS_ENDPOINT_URL", value)
    if ignore:
        no_endpoint.setenv("AWS_IGNORE_CONFIGURED_ENDPOINT_URLS", ignore)
    con = _Recorder()
    create_s3_credential_chain_secret(con)
    [sql] = con.statements
    assert "ENDPOINT" not in sql and "pw" not in sql


def test_the_acquisition_runner_destination_uses_the_same_override(no_endpoint, tmp_path):
    """Bronze and Silver land in the same store: one helper, one rule."""
    from fluid_build.build_runners.duckdb.runner import _apply_destination_secret

    no_endpoint.setenv("AWS_ENDPOINT_URL", "http://127.0.0.1:5001")
    con = _Recorder()
    _apply_destination_secret(con, _AcquisitionCtx(_SILVER_AWS, tmp_path), "s3://b/k.parquet")
    [sql] = con.statements
    assert "ENDPOINT '127.0.0.1:5001'" in sql


# ── Against an S3 API (moto server) ─────────────────────────────────────


def _duckdb_can_reach_s3() -> bool:
    try:
        con = duckdb.connect()
        for ext in ("httpfs", "aws"):
            con.execute(f"INSTALL {ext}")
            con.execute(f"LOAD {ext}")
        return True
    except Exception:  # noqa: BLE001 - no network and no cached extension
        return False


def _free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def s3_endpoint(monkeypatch) -> Iterator[str]:
    """A moto S3 API in its own process.

    Not ``ThreadedMotoServer``: DuckDB's reads of a relation built from Python
    can hold the GIL while they wait on HTTP, and an in-process server thread
    then never answers (a timeout, measured). A separate process is also how
    the object store runs for real.
    """
    pytest.importorskip("moto.server")
    if not _duckdb_can_reach_s3():
        pytest.skip("duckdb httpfs/aws extensions unavailable")
    import subprocess
    import sys
    import time

    import requests

    port = _free_port()
    endpoint = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen(
        [sys.executable, "-m", "moto.server", "-H", "127.0.0.1", "-p", str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                requests.get(f"{endpoint}/moto-api/", timeout=1)
                break
            except requests.RequestException:
                if proc.poll() is not None or time.monotonic() > deadline:
                    pytest.skip("moto server did not start")
                time.sleep(0.2)
        # Nothing here may reach an account, read a profile, or ask IMDS.
        for var in ("AWS_PROFILE", "AWS_SESSION_TOKEN", "AWS_ENDPOINT_URL_S3"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("AWS_CONFIG_FILE", "/dev/null")
        monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", "/dev/null")
        monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")  # pragma: allowlist secret
        monkeypatch.setenv("AWS_REGION", "eu-north-1")
        monkeypatch.setenv("AWS_ENDPOINT_URL", endpoint)
        yield endpoint
    finally:
        proc.terminate()
        with contextlib.suppress(Exception):
            proc.wait(timeout=10)


@pytest.mark.emulated
def test_the_chain_reads_and_lands_in_s3(tmp_path, s3_endpoint):
    """Bronze in S3 at the runner's key; Silver reads its prefix, lands at its own."""
    import boto3

    from fluid_build.build_runners.duckdb.runner import _resolve_destination_path

    s3 = boto3.client("s3", endpoint_url=s3_endpoint, region_name="eu-north-1")
    s3.create_bucket(
        Bucket="northwind-demo-lake",
        CreateBucketConfiguration={"LocationConstraint": "eu-north-1"},
    )
    root = _workspace(tmp_path / "ws")
    bronze_binding = {
        "platform": "aws",
        "format": "parquet",
        "location": {
            "table": "customer_subscriptions",
            "bucket": "northwind-demo-lake",
            "path": "bronze/customer_subscriptions/",
            "region": "eu-north-1",
        },
    }
    _bronze(root, bronze_binding)
    bronze_key = _resolve_destination_path(
        _AcquisitionCtx(bronze_binding, tmp_path),
        "public.product_subscription",
        "parquet",
        tmp_path,
    )
    local = _parquet(tmp_path / "seed.parquet")
    s3.upload_file(str(local), "northwind-demo-lake", bronze_key.split("/", 3)[3])

    contract = _silver(root, binding=_SILVER_AWS)
    with patch("fluid_build.build_runners.base.cprint"):
        assert _run(root, contract) == 0

    expected = _resolve_destination_path(
        _AcquisitionCtx(_SILVER_AWS, tmp_path, "status_summary"),
        "public.product_subscription",
        "parquet",
        tmp_path,
    )
    keys = {o["Key"] for o in s3.list_objects_v2(Bucket="northwind-demo-lake")["Contents"]}
    assert expected.split("/", 3)[3] in keys, keys
    landed = tmp_path / "landed.parquet"
    s3.download_file("northwind-demo-lake", expected.split("/", 3)[3], str(landed))
    assert _read(landed) == [("p1", "active", 2), ("p2", "ended", 1)]
    assert not (_silver_dir(root) / "silver").exists(), "nothing may land on local disk"


# ── fluid apply --env aws ───────────────────────────────────────────────


def test_the_tofu_module_carries_no_trace_of_the_embedded_query():
    """The AWS planner turns embedded-logic into ``athena.execute_query``; the
    OpenTofu emitter provisions S3 + Glue from ``exposes[]`` and must ignore
    that action, so no SQL naming a consumes view is ever run in Athena."""
    from fluid_build.iac import build_module, get_iac_plugin
    from fluid_build.providers.aws.plan.planner import plan_actions

    contract = {
        "fluidVersion": "0.7.5",
        "kind": "DataProduct",
        "id": "silver.subscription_status_summary",
        "name": "Subscription Status Summary",
        "consumes": [{"productId": BRONZE, "exposeId": "subscriptions"}],
        "builds": [_silver_build()],
        "exposes": [
            {
                "exposeId": "status_summary",
                "binding": _SILVER_AWS,
                "contract": {"schema": [{"name": "status", "type": "VARCHAR"}]},
            }
        ],
    }
    actions = plan_actions(contract, "123456789012", "eu-north-1", logging.getLogger("t"))
    assert any(a.get("op") == "athena.execute_query" for a in actions)
    module = json.loads(build_module(get_iac_plugin("aws"), contract, actions=actions))
    # Only what exposes[] declares: the query is catalogue metadata (the Glue
    # table's ``fluid_contract`` parameter), never a resource that runs it.
    types = set(module.get("resource", {}))
    assert types == {"aws_glue_catalog_database", "aws_glue_catalog_table", "aws_s3_bucket"}


def test_the_cli_and_the_build_runners_find_the_same_workspace_root(tmp_path):
    """One function, moved to ``util`` so ``build_runners`` need not import ``cli``."""
    from fluid_build.cli import artifact_paths, workspace_config
    from fluid_build.util import workspace_root

    assert workspace_config.find_workspace_root is workspace_root.find_workspace_root
    assert artifact_paths.WORKSPACE_CONFIG_FILENAME == workspace_root.WORKSPACE_CONFIG_FILENAME
    root = _workspace(tmp_path / "ws")
    nested = root / "contracts" / "x"
    nested.mkdir(parents=True)
    assert workspace_root.find_workspace_root(nested) == root.resolve()


def test_each_bucket_gets_its_own_scoped_secret_and_region(no_endpoint):
    """Two upstreams in two regions: each bucket is signed with its own region."""
    from fluid_build.providers.local.local import LocalProvider

    con = _Recorder()
    LocalProvider(project="local", region="local")._attach_object_stores(
        con,
        [
            {"path": "s3://lake-a/bronze/x/*.parquet", "region": "eu-north-1"},
            {"path": "s3://lake-b/silver/y/y.parquet", "format": "parquet", "region": "eu-west-1"},
            {"path": "/local/file.parquet"},
        ],
    )
    secrets = [s for s in con.statements if s.startswith("CREATE OR REPLACE SECRET")]
    assert len(secrets) == 2
    assert "SCOPE 's3://lake-a'" in secrets[0] and "REGION 'eu-north-1'" in secrets[0]
    assert "SCOPE 's3://lake-b'" in secrets[1] and "REGION 'eu-west-1'" in secrets[1]
    assert {"LOAD httpfs", "LOAD aws"} <= set(con.statements)


def test_an_explicit_input_naming_a_url_is_not_fetched(tmp_path):
    """Only ``s3://`` is an object store here; ``http://`` stays a missing file."""
    root = _workspace(tmp_path / "ws")
    build = _silver_build(
        inputs=[{"name": "subscriptions", "path": "http://169.254.169.254/latest/meta-data"}]
    )
    contract = _silver(root, build=build)
    with patch("fluid_build.build_runners.base.cprint"):
        assert _run(root, contract) == 1
    assert not (_silver_dir(root) / "out").exists()


# ── An entry the SQL does not read is lineage, as it was ────────────────
#
# Before this change every consumes[] entry was lineage only. A contract that
# declared consumes[] and bound its inputs under other names, or read its
# upstream by path in the SQL, built on main; it must keep building. Only an
# entry the SQL actually reads (a relation named by its exposeId) has to
# resolve.

_EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "local" / "high_value_churn"


def test_the_high_value_churn_example_builds_as_it_did_before_consumes_resolved(tmp_path, printed):
    """The shipped example, untouched: inputs hv/deg, consumes hv_customers/bb_degradations."""
    import shutil

    copy = tmp_path / "examples" / "local" / "high_value_churn"
    shutil.copytree(_EXAMPLE, copy, ignore=shutil.ignore_patterns("output", "runtime"))
    raw = yaml.safe_load((copy / "contract.fluid.yaml").read_text())
    inputs = raw["builds"][0]["properties"]["parameters"]["inputs"]
    assert [i["name"] for i in inputs] == ["hv", "deg"], "the example must stay in its old form"
    assert [c["exposeId"] for c in raw["consumes"]] == ["hv_customers", "bb_degradations"]

    args = argparse.Namespace(
        contract=str(copy / "contract.fluid.yaml"),
        env=None,
        build_id=None,
        dry_run=False,
        fail_fast=True,
        delay=0,
        no_output=False,
        sample_rows=None,
    )
    assert run_builds_from_args(args, logging.getLogger("t")) == 0, printed
    out = copy / "output" / "high_value_churn.parquet"
    assert duckdb.sql(f"SELECT COUNT(*) FROM '{out}'").fetchone()[0] > 0
    assert any(
        "consumes bronze.local.high_value_customers_v1/hv_customers" in line
        and "lineage only" in line
        for line in printed
    ), printed


def test_sql_that_reads_its_upstream_by_path_keeps_building(tmp_path, printed, caplog):
    """The old workaround: consumes[] for lineage, the file named in the SQL."""
    root = _workspace(tmp_path / "ws")  # no upstream contract anywhere
    data = _parquet(tmp_path / "shared" / "cs.parquet")
    build = _silver_build(
        sql=(
            "SELECT product_id, status, COUNT(*) AS subscription_count "
            f"FROM read_parquet('{data}') GROUP BY 1, 2"
        )
    )
    contract = _silver(root, build=build)
    with caplog.at_level(logging.WARNING):
        assert _run(root, contract) == 0, printed
    out = _silver_dir(root) / "out" / "subscription_status_summary.parquet"
    assert _read(out) == [("p1", "active", 2), ("p2", "ended", 1)]
    assert "local_consumes_not_bound" in caplog.text, "the old warning is kept for it"
    assert any("lineage only" in line for line in printed), printed


def test_only_the_entries_the_sql_reads_must_resolve(tmp_path, printed):
    """One entry read (and resolvable), one declared for lineage (and not in the workspace)."""
    root = _workspace(tmp_path / "ws")
    _parquet(root / "contracts/customer_subscriptions/out/customer_subscriptions.parquet")
    _bronze(root)
    contract = _silver(
        root,
        consumes=[
            {"productId": BRONZE, "exposeId": "subscriptions"},
            {"productId": "bronze.not_in_this_workspace", "exposeId": "customers"},
        ],
    )
    assert _run(root, contract) == 0, printed
    assert any('as view "subscriptions"' in line for line in printed)
    assert any("bronze.not_in_this_workspace/customers" in line for line in printed)


@pytest.mark.parametrize(
    "sql, read",
    [
        ("SELECT h.x FROM hv AS h JOIN deg AS d ON h.id = d.id;", {"hv", "deg"}),
        # get_table_names() binds the query: it raises here (no such files, and
        # USING against a relation it has not seen), and it reads the files.
        ("SELECT * FROM read_parquet('/no/such/*.parquet') p JOIN hv USING (id)", {"hv"}),
        ("WITH subscriptions AS (SELECT 1 AS a) SELECT * FROM subscriptions", set()),
        ("WITH a AS (SELECT * FROM Subscriptions) SELECT * FROM a", {"subscriptions"}),
        # DuckDB binds a CTE's own name in its body to the relation, not the CTE.
        ("WITH s AS (SELECT * FROM s WHERE k > 0) SELECT * FROM s", {"s"}),
        ("WITH r AS (SELECT 1), s AS (SELECT * FROM r) SELECT * FROM s", set()),
        (
            "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r WHERE n < 3) "
            "SELECT * FROM r JOIN deg ON TRUE",
            {"deg"},
        ),
        ("SELECT * FROM o WHERE k IN (SELECT k FROM deg)", {"o", "deg"}),
        ('SELECT COUNT(*) FROM "order"', {"order"}),
        ("SELECT * FROM read_csv('http://127.0.0.1:1/x.csv')", set()),
    ],
)
def test_the_relations_a_query_reads_come_from_duckdbs_parser(sql, read):
    from fluid_build.build_runners._embedded_sql_io import relations_read

    assert relations_read(sql) == frozenset(read)


@pytest.mark.parametrize(
    "sql", ["SELEC * FRM subscriptions", "PIVOT subscriptions ON status USING count(*)", ""]
)
def test_sql_the_parser_cannot_read_resolves_every_entry(tmp_path, sql):
    """Cannot tell what it reads: strict, as before this review."""
    from fluid_build.build_runners._embedded_sql_io import relations_read

    assert relations_read(sql) is None
    root = _workspace(tmp_path / "ws")
    build = _silver_build(sql=sql)
    contract = _silver(root, build=build)
    with pytest.raises(ConsumesResolutionError, match="no contract in the workspace"):
        resolve_consumes(contract, build, _silver_dir(root))


# ── A plan applies with the env it was planned with ─────────────────────


def _valid_silver(root: Path) -> Path:
    """A silver contract ``fluid plan`` accepts (schema-valid), consuming bronze."""
    contract = {
        "fluidVersion": "0.7.5",
        "kind": "DataProduct",
        "id": "silver.subscription_status_summary",
        "name": "Subscription Status Summary",
        "description": "Counts subscriptions by product and status.",
        "domain": "Customer",
        "metadata": {"layer": "Silver", "owner": {"team": "t", "email": "t@example.com"}},
        "consumes": [{"productId": BRONZE, "exposeId": "subscriptions"}],
        "builds": [
            {
                "id": "summarize_subscription_status",
                "pattern": "embedded-logic",
                "engine": "sql",
                "properties": {"sql": SQL},
                "execution": {"trigger": {"type": "manual"}, "runtime": {"platform": "local"}},
            }
        ],
        "exposes": [
            {
                "exposeId": "status_summary",
                "kind": "table",
                "binding": {
                    "platform": "local",
                    "format": "parquet",
                    "location": {"path": "out/subscription_status_summary.parquet"},
                },
                "contract": {
                    "schema": [
                        {"name": "product_id", "type": "string"},
                        {"name": "status", "type": "string"},
                        {"name": "subscription_count", "type": "bigint"},
                    ]
                },
            }
        ],
    }
    path = _dump(_silver_dir(root) / "contract.fluid.yaml", contract)
    _dump(
        _silver_dir(root) / "overlays" / "staging.yaml",
        {
            "exposes": [
                {
                    "exposeId": "status_summary",
                    "binding": {
                        "platform": "local",
                        "format": "parquet",
                        "location": {"path": "staging/subscription_status_summary.parquet"},
                    },
                }
            ]
        },
    )
    return path


def _bronze_with_staging(root: Path) -> None:
    bronze = _bronze(root)
    _parquet(bronze.parent / "out" / "customer_subscriptions.parquet")
    _parquet(bronze.parent / "staging" / "cs.parquet", rows=[("s1", "trial")])
    _dump(
        bronze.parent / "overlays" / "staging.yaml",
        {
            "exposes": [
                {
                    "exposeId": "subscriptions",
                    "binding": {
                        "platform": "local",
                        "format": "parquet",
                        "location": {"path": "staging/cs.parquet"},
                    },
                }
            ]
        },
    )


def _plan(contract: Path, out: Path, env: Optional[str]) -> Dict[str, Any]:
    from fluid_build.cli import plan as plan_mod

    ns = argparse.Namespace(
        contract=str(contract),
        env=env,
        mode="amend-and-build",
        out=str(out),
        verbose=False,
        validate_actions=False,
        estimate_cost=False,
        check_sovereignty=False,
        provider="local",
        project=None,
        region=None,
        html_output=None,
    )
    assert plan_mod.run(ns, logging.getLogger("t")) == 0
    return json.loads(out.read_text())


def _apply_plan_args(plan_path: Path, env: Optional[str]) -> argparse.Namespace:
    return argparse.Namespace(
        contract=str(plan_path),
        env=env,
        build_id=None,
        dry_run=False,
        fail_fast=True,
        delay=0,
        no_output=False,
        sample_rows=None,
        bundle=None,
    )


def test_a_plan_records_its_overlay_env_under_the_plan_digest(tmp_path):
    from fluid_build.forge.core.plan_digest import PlanBindingError, verify_plan_binding

    root = _workspace(tmp_path / "ws")
    silver = _valid_silver(root)
    plan = _plan(silver, tmp_path / "plan.json", "staging")
    assert plan["contract_metadata"]["env"] == "staging"
    verify_plan_binding(plan)
    tampered = json.loads(json.dumps(plan))
    tampered["contract_metadata"]["env"] = "prod"
    with pytest.raises(PlanBindingError):
        verify_plan_binding(tampered)
    base = _plan(silver, tmp_path / "base.json", None)
    assert "env" not in base["contract_metadata"], "no env, no new key"


def test_applying_a_plan_reads_the_upstream_with_the_env_it_was_planned_with(tmp_path, printed):
    """``fluid plan --env staging`` then ``fluid apply plan.json`` (no --env)."""
    from fluid_build.forge.core.plan_digest import verify_plan_binding

    root = _workspace(tmp_path / "ws")
    _bronze_with_staging(root)
    plan_path = tmp_path / "plan.json"
    plan = _plan(_valid_silver(root), plan_path, "staging")
    verify_plan_binding(plan)
    rc = run_builds_from_args(
        _apply_plan_args(plan_path, None), logging.getLogger("t"), plan_data=plan
    )
    assert rc == 0, printed
    out = _silver_dir(root) / "staging" / "subscription_status_summary.parquet"
    assert _read(out) == [("s1", "trial", 1)], "the upstream's staging binding, not its base"


def test_an_env_that_disagrees_with_the_plan_is_refused_before_any_build(tmp_path, printed):
    from fluid_build._contract_loader import CLIError

    root = _workspace(tmp_path / "ws")
    _bronze_with_staging(root)
    plan_path = tmp_path / "plan.json"
    plan = _plan(_valid_silver(root), plan_path, "staging")
    with pytest.raises(CLIError) as info:
        run_builds_from_args(
            _apply_plan_args(plan_path, "prod"), logging.getLogger("t"), plan_data=plan
        )
    assert info.value.event == "plan_env_mismatch"
    assert not (_silver_dir(root) / "staging").exists()
    assert not (_silver_dir(root) / "out").exists()
    # The env it was planned with is accepted.
    assert (
        run_builds_from_args(
            _apply_plan_args(plan_path, "staging"), logging.getLogger("t"), plan_data=plan
        )
        == 0
    )


def test_a_plan_without_a_recorded_env_follows_its_bundle_manifest(tmp_path):
    """A plan made from a bundle links it by ``source_path``; the MANIFEST has the env."""
    import io
    import tarfile

    from fluid_build.build_runners.base import _run_env

    bundle = tmp_path / "bundle.tgz"
    manifest = json.dumps({"source": {"contract": "c.yaml", "env": "aws"}}).encode()
    with tarfile.open(bundle, "w:gz") as tar:
        info = tarfile.TarInfo("MANIFEST.json")
        info.size = len(manifest)
        tar.addfile(info, io.BytesIO(manifest))
    plan = {"contract": {}, "contract_metadata": {"source_path": str(bundle)}}
    args = argparse.Namespace(contract=str(tmp_path / "plan.json"), env=None, bundle=None)
    assert _run_env(args, plan) == "aws"


# ── The local writer and the consumes reader agree ──────────────────────


@pytest.mark.parametrize(
    "fmt, path",
    [
        ("parquet", "out/orders"),
        ("parquet", "out/orders.csv"),
        ("json", "out/orders.json"),
        ("", "out/orders.parquet"),
        ("csv", "out/orders.csv"),
    ],
)
def test_a_local_upstream_is_read_where_and_how_the_local_writer_wrote_it(
    tmp_path, printed, fmt, path
):
    root = _workspace(tmp_path / "ws")
    seed = tmp_path / "seed.csv"
    seed.write_text("product_id,status\np1,active\np1,active\np2,ended\n")
    bronze = {
        "fluidVersion": "0.7.5",
        "kind": "DataProduct",
        "id": BRONZE,
        "name": "b",
        "builds": [
            {
                "id": "b",
                "pattern": "embedded-logic",
                "engine": "duckdb",
                "properties": {
                    "sql": "SELECT * FROM s",
                    "parameters": {"inputs": [{"name": "s", "path": str(seed)}]},
                },
            }
        ],
        "exposes": [
            {
                "exposeId": "subscriptions",
                "kind": "table",
                "binding": {"platform": "local", "format": fmt, "location": {"path": path}},
            }
        ],
    }
    bronze_dir = root / "contracts" / "customer_subscriptions"
    _dump(bronze_dir / "contract.fluid.yaml", bronze)
    assert _execute_embedded_sql_build(bronze["builds"][0], bronze, bronze_dir) == 0
    contract = _silver(root)
    assert _run(root, contract) == 0, printed
    out = _silver_dir(root) / "out" / "subscription_status_summary.parquet"
    assert _read(out) == [("p1", "active", 2), ("p2", "ended", 1)]


def test_one_helper_names_the_local_file_and_format_for_writer_and_reader(tmp_path):
    from fluid_build.providers.local.local import LocalProvider
    from fluid_build.util.binding_paths import local_provider_landing

    for fmt, path in [("parquet", "o/x"), ("json", "o/x.json"), ("", "o/x.parquet")]:
        contract = {
            "id": "p",
            "builds": [{"id": "b", "properties": {"sql": "SELECT 1"}}],
            "exposes": [
                {
                    "exposeId": "x",
                    "binding": {"platform": "local", "format": fmt, "location": {"path": path}},
                }
            ],
        }
        [action] = LocalProvider(anchor_dir=tmp_path)._derive_actions_from_contract(contract)
        [spec] = action["outputs"]
        landed, landed_fmt = local_provider_landing(str(tmp_path / path), fmt)
        assert (spec["path"], spec["format"]) == (landed, landed_fmt)


# ── A contract never reads what it is about to overwrite ────────────────


def test_a_contract_that_consumes_its_own_id_is_refused(tmp_path, printed):
    root = _workspace(tmp_path / "ws")
    contract = _silver(
        root,
        consumes=[
            {"productId": "silver.subscription_status_summary", "exposeId": "status_summary"}
        ],
        build=_silver_build(sql="SELECT product_id, status, 1 AS n FROM status_summary"),
    )
    landed = _parquet(_silver_dir(root) / "out" / "subscription_status_summary.parquet")
    before = _read(landed)
    assert _run(root, contract) == 1
    assert _read(landed) == before, "the build must not overwrite what it reads"
    assert any("its own id" in line for line in printed), printed


# ── An endpoint override never falls back to AWS ───────────────────────


class _SecretFails:
    """A connection on which ``CREATE SECRET`` fails (e.g. no aws extension offline)."""

    def __init__(self):
        self.statements: List[str] = []

    def execute(self, sql: str):
        self.statements.append(sql)
        if sql.startswith("CREATE OR REPLACE SECRET"):
            raise duckdb.IOException("aws extension could not be loaded")


def test_a_secret_that_fails_with_an_endpoint_override_raises(no_endpoint):
    from fluid_build.build_runners.duckdb.runner import (
        ObjectStoreEndpointError,
        attach_object_store,
        create_s3_credential_chain_secret,
    )

    no_endpoint.setenv("AWS_ENDPOINT_URL", "http://127.0.0.1:5001")
    with pytest.raises(ObjectStoreEndpointError, match="AWS_ENDPOINT_URL"):
        create_s3_credential_chain_secret(_SecretFails(), region="eu-north-1")
    with pytest.raises(ObjectStoreEndpointError):
        attach_object_store(_SecretFails(), "s3://lake/", region="eu-north-1", scope="s3://lake")


def test_without_an_endpoint_override_a_failed_secret_is_still_best_effort(no_endpoint):
    from fluid_build.build_runners.duckdb.runner import create_s3_credential_chain_secret

    assert create_s3_credential_chain_secret(_SecretFails()) is False


# ── A provider error never echoes a resolved secret ─────────────────────


@pytest.fixture
def secret_registry():
    from fluid_build.observability.secret_redactor import forget_known_secrets

    yield
    forget_known_secrets()


def test_a_failed_action_never_prints_or_records_a_resolved_secret(
    tmp_path, printed, caplog, monkeypatch, secret_registry
):
    secret = "sk-live-SUPERSECRET-0123456789"  # pragma: allowlist secret
    monkeypatch.setenv("PARTNER_API_TOKEN", secret)
    root = _workspace(tmp_path / "ws")
    data = _parquet(tmp_path / "hand" / "subs.parquet")
    build = _silver_build(
        sql="SELECT * FROM subscriptions WHERE 1 = CAST('{{ env.PARTNER_API_TOKEN }}' AS INTEGER)",
        inputs=[{"name": "subscriptions", "path": str(data)}],
    )
    contract = _silver(root, build=build)
    with caplog.at_level(logging.DEBUG):
        assert _run(root, contract) == 1
    assert any("Conversion Error" in line for line in printed), "the error is still reported"
    assert not [line for line in printed if secret in line]
    assert secret not in caplog.text
    assert secret not in (tmp_path / "runtime/out/local_apply_log.jsonl").read_text()
