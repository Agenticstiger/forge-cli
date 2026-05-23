# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Shared harness for the Snowflake OpenTofu IaC test suite.

Layers
------
* ``test_iac_snowflake.py``            — emit unit tests (offline, no creds).
* ``test_iac_snowflake_validate.py``   — ``tofu validate`` (needs ``tofu``,
  no creds).
* ``test_iac_snowflake_live.py``       — live ``tofu apply``/``destroy``
  round-trips against a real Snowflake account.
* ``test_iac_snowflake_apply_engine.py`` — the ``apply_via_opentofu`` engine,
  live.
* ``test_iac_snowflake_lab_e2e.py``    — the in-repo example contracts, live.

Live-test gating
----------------
The live layers provision real objects and spend warehouse credits. They
self-skip unless ALL of the following hold, so a plain ``pytest`` run never
touches a cloud account:

* the ``tofu`` (OpenTofu) binary is on ``PATH``;
* Snowflake credentials are in the environment (``SNOWFLAKE_ACCOUNT``,
  ``SNOWFLAKE_USER`` and a password or key-pair);
* the explicit opt-in flag ``FLUID_IAC_LIVE_SNOWFLAKE=1`` is set.

Run the live suite against the snowflake-biz-lab account::

    set -a; . <lab>/runtime/generated/fluid.local.env; set +a
    FLUID_IAC_LIVE_SNOWFLAKE=1 .venv/bin/python -m pytest \\
        tests/iac -m "integration and snowflake"

Isolation
---------
Every live test provisions into a unique throwaway database named
``FLUID_IACTEST_<hex>``; teardown drops it. No pre-existing database
(``TELCO_LAB`` or otherwise) is ever touched. A session finalizer sweeps
stray ``FLUID_IACTEST_*`` databases left behind by a hard-killed run.
"""

from __future__ import annotations

import contextlib
import json
import os
import uuid
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

import pytest

from fluid_build.iac import (
    assemble_tofu_document,
    build_module,
    get_iac_plugin,
    render_tofu_json,
    runner,
)
from fluid_build.iac.credentials import build_tofu_env

# Throwaway databases all share this prefix so the session finalizer can
# find and drop any a crashed run left behind.
TEST_DB_PREFIX = "FLUID_IACTEST_"

_TRUE = {"1", "true", "yes", "on"}


# ---------------------------------------------------------------------------
# Live-test gate
# ---------------------------------------------------------------------------


def _live_snowflake_ready() -> tuple[bool, str]:
    """Return ``(enabled, skip_reason)`` for the live Snowflake layers."""
    if runner.tofu_path() is None:
        return False, "the `tofu` (OpenTofu) binary is not on PATH"
    if os.environ.get("FLUID_IAC_LIVE_SNOWFLAKE", "").strip().lower() not in _TRUE:
        return False, "live Snowflake tests are opt-in — set FLUID_IAC_LIVE_SNOWFLAKE=1"
    missing = [v for v in ("SNOWFLAKE_ACCOUNT", "SNOWFLAKE_USER") if not os.environ.get(v)]
    if missing:
        return False, f"missing Snowflake credentials: {', '.join(missing)}"
    has_auth = any(
        os.environ.get(v)
        for v in ("SNOWFLAKE_PASSWORD", "SNOWFLAKE_PRIVATE_KEY", "SNOWFLAKE_PRIVATE_KEY_PATH")
    )
    if not has_auth:
        return False, "missing Snowflake auth (SNOWFLAKE_PASSWORD or a key-pair)"
    try:
        import snowflake.connector  # noqa: F401
    except ImportError:
        return False, "snowflake-connector-python is not installed"
    return True, ""


LIVE_SNOWFLAKE_ENABLED, LIVE_SKIP_REASON = _live_snowflake_ready()


# ---------------------------------------------------------------------------
# Pure helpers — contract / action builders (importable from test modules)
# ---------------------------------------------------------------------------


def snowflake_plugin():
    """The registered Snowflake IaC plugin."""
    return get_iac_plugin("snowflake")


def table_contract(
    database: str,
    schema: str,
    table: str = "EVENTS",
    *,
    columns: Optional[Sequence[Mapping[str, Any]]] = None,
    cid: str = "iac.livetest",
) -> Dict[str, Any]:
    """A contract with one ``snowflake_table`` exposure."""
    return {
        "id": cid,
        "name": "IaC Live Test",
        "exposes": [
            {
                "exposeId": "t",
                "binding": {
                    "platform": "snowflake",
                    "format": "snowflake_table",
                    "location": {"database": database, "schema": schema, "table": table},
                },
                "contract": {
                    "schema": (
                        list(columns)
                        if columns is not None
                        else [
                            {"name": "ID", "type": "integer", "required": True},
                            {"name": "LABEL", "type": "string"},
                            {"name": "AMOUNT", "type": "decimal(12,2)"},
                            {"name": "CREATED_AT", "type": "timestamp"},
                        ]
                    )
                },
            }
        ],
    }


def view_contract(
    database: str,
    schema: str,
    view: str = "EVENTS_V",
    *,
    query: str = "SELECT 1 AS ONE",
    cid: str = "iac.livetest",
) -> Dict[str, Any]:
    """A contract with one ``snowflake_view`` exposure."""
    return {
        "id": cid,
        "name": "IaC Live Test",
        "exposes": [
            {
                "exposeId": "v",
                "binding": {
                    "platform": "snowflake",
                    "format": "snowflake_view",
                    "location": {
                        "database": database,
                        "schema": schema,
                        "view": view,
                        "query": query,
                    },
                },
            }
        ],
    }


def grant_contract(
    database: str,
    schema: str,
    table: str,
    *,
    role: str,
    privilege: str = "SELECT",
    cid: str = "iac.livetest",
) -> Dict[str, Any]:
    """A table contract plus one ``security.access_control.grants[]`` entry."""
    contract = table_contract(database, schema, table, cid=cid)
    contract["security"] = {
        "access_control": {
            "grants": [
                {
                    "role": role,
                    "privilege": privilege,
                    "object_type": "TABLE",
                    "object_name": f"{database}.{schema}.{table}",
                }
            ]
        }
    }
    return contract


def stream_action(database: str, schema: str, source_table: str, name: str = "EVENTS_STREAM"):
    return {
        "op": "sf.stream.ensure",
        "database": database,
        "schema": schema,
        "name": name,
        "source_table": source_table,
        "append_only": True,
    }


def task_action(database: str, schema: str, warehouse: str, name: str = "ROLLUP_TASK"):
    # The emitter maps ``schedule`` straight onto the v2 provider's
    # ``schedule.using_cron`` — which wants a bare cron expression (the
    # provider prepends the ``USING CRON`` keyword itself).
    return {
        "op": "sf.task.ensure",
        "database": database,
        "schema": schema,
        "name": name,
        "sql": "SELECT CURRENT_TIMESTAMP()",
        "schedule": "0 0 * * * UTC",
        "warehouse": warehouse,
        "after": [],
    }


def planned_view_action(database: str, schema: str, name: str = "RECENT_V"):
    return {
        "op": "sf.view.ensure",
        "database": database,
        "schema": schema,
        "name": name,
        "query": "SELECT 1 AS ONE",
    }


def procedure_action(database: str, schema: str, name: str = "PING_PROC"):
    return {
        "op": "sf.procedure.ensure",
        "database": database,
        "schema": schema,
        "name": name,
        "language": "SQL",
        "parameters": [],
        "body": "BEGIN RETURN 'ok'; END;",
    }


def function_action(database: str, schema: str, name: str = "DOUBLE_FN"):
    return {
        "op": "sf.udf.ensure",
        "database": database,
        "schema": schema,
        "name": name,
        "language": "SQL",
        "return_type": "NUMBER",
        "parameters": [{"name": "N", "type": "NUMBER"}],
        "body": "N * 2",
    }


def masking_policy_contract(database: str, schema: str, table: str, name: str = "MASK_PII"):
    """A table contract carrying one ``security.policies.masking`` entry."""
    contract = table_contract(database, schema, table)
    contract["security"] = {
        "policies": {
            "masking": [
                {
                    "name": name,
                    "body": "'***'",
                    "signature": "(VAL VARCHAR) RETURNS VARCHAR",
                }
            ]
        }
    }
    return contract


def row_access_policy_contract(database: str, schema: str, table: str, name: str = "TENANT_RAP"):
    """A table contract carrying one ``security.policies.row_access`` entry."""
    contract = table_contract(database, schema, table)
    contract["security"] = {
        "policies": {
            "row_access": [
                {
                    "name": name,
                    "condition": "TRUE",
                    "signature": "(VAL VARCHAR) RETURNS BOOLEAN",
                }
            ]
        }
    }
    return contract


# ---------------------------------------------------------------------------
# Snowflake verification oracle — independent SDK queries (never `tofu state`)
# ---------------------------------------------------------------------------


def sf_rows(conn: Any, sql: str) -> List[tuple]:
    """Run ``sql`` and return all rows."""
    with contextlib.closing(conn.cursor()) as cur:
        cur.execute(sql)
        return list(cur.fetchall())


def sf_exists(conn: Any, kind: str, name: str, *, in_clause: str = "") -> bool:
    """``SHOW <kind> LIKE '<name>' [<in_clause>]`` — True when one row matches.

    ``kind`` is the ``SHOW`` object word, e.g. ``DATABASES`` /
    ``SCHEMAS`` / ``TABLES`` / ``VIEWS`` / ``STREAMS`` / ``TASKS`` /
    ``PROCEDURES`` / ``"USER FUNCTIONS"`` / ``"MASKING POLICIES"`` /
    ``"ROW ACCESS POLICIES"``.
    """
    sql = f"SHOW {kind} LIKE '{name}'"
    if in_clause:
        sql += f" {in_clause}"
    return len(sf_rows(conn, sql)) > 0


def sf_table_columns(
    conn: Any, database: str, schema: str, table: str
) -> Dict[str, Dict[str, Any]]:
    """``DESC TABLE`` → ``{COLUMN_NAME: {"type": ..., "nullable": bool}}``."""
    rows = sf_rows(conn, f'DESC TABLE "{database}"."{schema}"."{table}"')
    # DESC TABLE columns: name, type, kind, null?, default, ...
    return {row[0]: {"type": row[1], "nullable": str(row[3]).upper() == "Y"} for row in rows}


def create_container(
    conn: Any,
    database: str,
    schema: str,
    *,
    base_table: Optional[str] = None,
) -> None:
    """Pre-create a throwaway database + schema (and optional base table).

    Orchestration / governance resources (``snowflake_stream_on_table``,
    ``snowflake_task``, ``snowflake_masking_policy``, …) reference their
    database / schema by literal name with no OpenTofu dependency edge, so
    they must apply into a container that already exists. Data-plane tests
    (database / schema / table / view) do not call this — ``tofu`` builds
    those itself via the real ``snowflake_database`` → ``snowflake_schema``
    reference chain.
    """
    with contextlib.closing(conn.cursor()) as cur:
        cur.execute(f'CREATE DATABASE IF NOT EXISTS "{database}"')
        cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{database}"."{schema}"')
        if base_table:
            cur.execute(
                f'CREATE TABLE IF NOT EXISTS "{database}"."{schema}"."{base_table}" '
                '("ID" NUMBER(38,0), "LABEL" VARCHAR)'
            )


def _drop_database(conn: Any, database: str) -> None:
    with contextlib.closing(conn.cursor()) as cur:
        cur.execute(f'DROP DATABASE IF EXISTS "{database}"')


def _sweep_stray_test_databases(conn: Any) -> None:
    """Drop any leftover ``FLUID_IACTEST_*`` databases (crash-safety net)."""
    try:
        rows = sf_rows(conn, f"SHOW DATABASES LIKE '{TEST_DB_PREFIX}%'")
    except Exception:  # noqa: BLE001 — best-effort sweep
        return
    for row in rows:
        with contextlib.suppress(Exception):
            _drop_database(conn, row[1])


def _prune_dangling_depends_on(resources: Mapping[str, Any]) -> Dict[str, Any]:
    """Strip ``depends_on`` entries pointing at resources outside this subset.

    The Snowflake plugin attaches ``depends_on`` for the container resources
    each orchestration / governance resource sits in. When a test emits only
    a *subset* of what the plugin produced (e.g. just a masking policy,
    leaving the database / schema for an externally-pre-created container),
    those container addresses are not in the subset — OpenTofu would reject
    them as undeclared. This prunes only the dangling addresses; deps that
    still resolve in the subset are kept.
    """
    addresses = {f"{rt}.{rn}" for rt, items in resources.items() for rn in items}
    pruned: Dict[str, Any] = {}
    for resource_type, items in resources.items():
        pruned[resource_type] = {}
        for name, body in items.items():
            new_body = dict(body)
            deps = new_body.get("depends_on")
            if deps:
                kept = [dep for dep in deps if dep in addresses]
                if kept:
                    new_body["depends_on"] = kept
                else:
                    new_body.pop("depends_on", None)
            pruned[resource_type][name] = new_body
    return pruned


# ---------------------------------------------------------------------------
# A live OpenTofu working directory bound to one test
# ---------------------------------------------------------------------------


class TofuProject:
    """A per-test ``tofu`` workdir — emit a module, then init/plan/apply it."""

    def __init__(self, workdir: Path, env: Mapping[str, str]) -> None:
        self.workdir = workdir
        self.env = dict(env)
        self.applied = False

    # -- module emission ---------------------------------------------------

    def emit(
        self,
        contract: Mapping[str, Any],
        *,
        actions: Sequence[Mapping[str, Any]] = (),
        imports: Optional[Sequence[Any]] = None,
    ) -> str:
        """Compile a contract through the Snowflake plugin → ``main.tf.json``."""
        text = build_module(
            snowflake_plugin(),
            contract,
            actions=actions,
            imports=list(imports) if imports else None,
        )
        (self.workdir / "main.tf.json").write_text(text, encoding="utf-8")
        return text

    def emit_resources(self, resources: Mapping[str, Any]) -> str:
        """Write a module from a pre-built ``resource`` sub-tree.

        Used by orchestration / governance tests that apply a single
        resource type into a pre-created container — keeps the module free
        of the ``snowflake_database`` / ``snowflake_schema`` resources whose
        literal (edge-free) references would otherwise race the dependent.
        """
        plugin = snowflake_plugin()
        # An extracted subset may carry a ``depends_on`` referencing resources
        # the caller did not include — OpenTofu would reject those as
        # undeclared references. Prune any address that is not also in the
        # subset; intra-subset deps stay. Callers using this method apply
        # into an externally-managed container (see :func:`create_container`),
        # so the missing deps are satisfied out of band.
        pruned = _prune_dangling_depends_on(resources)
        document = assemble_tofu_document(
            required_providers=plugin.required_providers,
            resources=pruned,
            provider={plugin.name: plugin.provider_block()},
        )
        text = render_tofu_json(document)
        (self.workdir / "main.tf.json").write_text(text, encoding="utf-8")
        return text

    # -- tofu lifecycle ----------------------------------------------------

    def init(self):
        return runner.tofu_init(str(self.workdir), backend=False, env=self.env)

    def plan(self):
        return runner.tofu_plan(str(self.workdir), env=self.env)

    def apply(self):
        result = runner.tofu_apply(str(self.workdir), env=self.env)
        self.applied = True
        return result

    def destroy(self):
        return runner.tofu_destroy(str(self.workdir), env=self.env)

    def import_(self, address: str, resource_id: str):
        return runner.tofu_import(str(self.workdir), address, resource_id, env=self.env)

    def state(self) -> List[str]:
        return runner.tofu_state_list(str(self.workdir), env=self.env)

    def _init_plan_apply(self):
        init = self.init()
        assert init.ok, f"tofu init failed:\n{init.stderr or init.stdout}"
        plan = self.plan()
        assert plan.ok, f"tofu plan failed:\n{plan.stderr or plan.stdout}"
        applied = self.apply()
        assert applied.ok, f"tofu apply failed:\n{applied.stderr or applied.stdout}"
        return applied

    def apply_ok(
        self,
        contract: Mapping[str, Any],
        *,
        actions: Sequence[Mapping[str, Any]] = (),
        imports: Optional[Sequence[Any]] = None,
    ):
        """Emit a contract + init + plan + apply, asserting each step.

        Returns the apply :class:`~fluid_build.iac.runner.TofuResult`.
        """
        self.emit(contract, actions=actions, imports=imports)
        return self._init_plan_apply()

    def apply_resources_ok(self, resources: Mapping[str, Any]):
        """Emit a raw resource sub-tree + init/plan/apply, asserting each step.

        For orchestration / governance resources applied into a container
        created out-of-band by :func:`create_container`.
        """
        self.emit_resources(resources)
        return self._init_plan_apply()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tofu_binary() -> str:
    """The ``tofu`` binary path; skip the test when it is not installed.

    For the credential-free ``tofu validate`` layer.
    """
    path = runner.tofu_path()
    if path is None:
        pytest.skip("the `tofu` (OpenTofu) binary is not on PATH")
    return path


@pytest.fixture(scope="session")
def tofu_env(tmp_path_factory: pytest.TempPathFactory) -> Dict[str, str]:
    """The environment for ``tofu`` child processes.

    Carries the inherited environment (so the ``snowflakedb/snowflake``
    provider self-configures from ``SNOWFLAKE_*``), the plugin's
    ``credential_env`` overlay (the ``SNOWFLAKE_ACCOUNT`` → org/account
    split), and a session-shared provider cache so the ~16 live tests do
    not each re-download the provider.
    """
    env = build_tofu_env()
    env.update(snowflake_plugin().credential_env(env))
    env["TF_PLUGIN_CACHE_DIR"] = str(tmp_path_factory.mktemp("tofu-plugin-cache"))
    return env


@pytest.fixture(scope="session")
def sf_connection() -> Iterator[Any]:
    """A live ``snowflake.connector`` connection — the verification oracle.

    Session-scoped: one connection serves the whole live suite. Skips the
    test when the live gate is closed. On teardown it sweeps any stray
    ``FLUID_IACTEST_*`` databases before closing.
    """
    if not LIVE_SNOWFLAKE_ENABLED:
        pytest.skip(LIVE_SKIP_REASON)
    import snowflake.connector

    conn = snowflake.connector.connect(
        account=os.environ["SNOWFLAKE_ACCOUNT"],
        user=os.environ["SNOWFLAKE_USER"],
        password=os.environ.get("SNOWFLAKE_PASSWORD"),
        role=os.environ.get("SNOWFLAKE_ROLE"),
        warehouse=os.environ.get("SNOWFLAKE_WAREHOUSE"),
    )
    try:
        yield conn
    finally:
        with contextlib.suppress(Exception):
            _sweep_stray_test_databases(conn)
        conn.close()


@pytest.fixture
def live_db(sf_connection: Any) -> Iterator[str]:
    """A unique throwaway database name; teardown drops it unconditionally.

    The name is yielded — nothing is created here. Data-plane tests let
    ``tofu`` create the database; orchestration tests pass the name to
    :func:`create_container`. Teardown is a ``DROP DATABASE IF EXISTS`` so
    it cleans up regardless of how far the test got.
    """
    name = f"{TEST_DB_PREFIX}{uuid.uuid4().hex[:10].upper()}"
    try:
        yield name
    finally:
        with contextlib.suppress(Exception):
            _drop_database(sf_connection, name)


@pytest.fixture
def tofu_project(
    sf_connection: Any, tofu_env: Dict[str, str], tmp_path: Path
) -> Iterator[TofuProject]:
    """A :class:`TofuProject` bound to a fresh workdir.

    Teardown runs ``tofu destroy`` (best-effort) so a test that asserts
    mid-cycle still tears its resources down; ``live_db`` then drops the
    database as the belt-and-suspenders net.
    """
    project = TofuProject(tmp_path, tofu_env)
    try:
        yield project
    finally:
        if project.applied:
            with contextlib.suppress(Exception):
                project.destroy()


# ===========================================================================
# AWS Stage 2 — LocalStack harness (live AWS via Docker emulator)
# ===========================================================================
#
# Triple-gated, exactly like the Snowflake live layer:
#   * ``tofu`` is on PATH;
#   * a LocalStack container is reachable at the LOCALSTACK_ENDPOINT URL
#     (default ``http://localhost:4566``);
#   * the explicit opt-in flag ``FLUID_IAC_LIVE_LOCALSTACK=1`` is set.
#
# Tests apply the plugin's credential-free ``main.tf.json`` with a sidecar
# ``provider.tf.json`` that aims the AWS provider at LocalStack — the same
# pattern the existing moto e2e uses, but against the heavier emulator that
# faithfully covers Athena query execution, Glue ETL jobs, and many Pro-only
# services.
#
# Known LocalStack gaps (verified on the live container):
#   * ``redshift-serverless`` returns 501 InternalFailure → Redshift
#     Serverless live coverage defers to Stage 3 (real AWS).
#   * ``redshift-data`` returns 501 → the ``CREATE EXTERNAL SCHEMA`` bridge
#     defers to Stage 3 too. (Tofu validate of the bridge already proves the
#     module shape in Stage 1.)
# Classic ``redshift`` clusters, Athena queries, Glue Catalog + ETL,
# Lambda, Kinesis, Step Functions, EventBridge — all working.


_LOCALSTACK_DEFAULT_ENDPOINT = "http://localhost:4566"
LOCALSTACK_ENDPOINT = os.environ.get("LOCALSTACK_ENDPOINT", _LOCALSTACK_DEFAULT_ENDPOINT)


def _localstack_ready() -> tuple[bool, str]:
    """Return ``(enabled, skip_reason)`` for the LocalStack AWS Stage-2 tier."""
    if runner.tofu_path() is None:
        return False, "the `tofu` (OpenTofu) binary is not on PATH"
    if os.environ.get("FLUID_IAC_LIVE_LOCALSTACK", "").strip().lower() not in _TRUE:
        return False, "live LocalStack tests are opt-in — set FLUID_IAC_LIVE_LOCALSTACK=1"
    try:
        import requests  # noqa: F401 — verify present
    except ImportError:
        return False, "the `requests` package is not installed"
    try:
        import requests

        resp = requests.get(f"{LOCALSTACK_ENDPOINT}/_localstack/health", timeout=5)
        if resp.status_code != 200:
            return False, f"LocalStack health at {LOCALSTACK_ENDPOINT} returned {resp.status_code}"
    except Exception as exc:  # noqa: BLE001
        return False, f"LocalStack not reachable at {LOCALSTACK_ENDPOINT}: {exc}"
    return True, ""


LOCALSTACK_ENABLED, LOCALSTACK_SKIP_REASON = _localstack_ready()


# Services the AWS plugin or its planner-derived resources touch — every
# service must be in the sidecar provider's ``endpoints`` map so the AWS
# provider sends API calls to LocalStack and not to the real AWS API.
_LOCALSTACK_SERVICES: Tuple[str, ...] = (
    "s3",
    "glue",
    "sts",
    "iam",
    "kinesis",
    "lambda",
    "events",
    "scheduler",
    "stepfunctions",
    "athena",
    "logs",
    "redshift",
    "secretsmanager",
)


def aws_provider_override(endpoint: str, *, region: str = "us-east-1") -> Dict[str, Any]:
    """A sidecar ``provider`` block aiming the AWS provider at LocalStack.

    ``tofu`` merges every ``*.tf.json`` in the workdir, so this overlays
    endpoint + dummy-credential config onto the plugin's credential-free
    ``main.tf.json`` — emitter output stays portable and secret-free, only
    the test rig knows about the emulator.
    """
    return {
        "provider": {
            "aws": {
                "region": region,
                "access_key": "test",
                "secret_key": "test",
                "skip_credentials_validation": True,
                "skip_metadata_api_check": True,
                "skip_requesting_account_id": True,
                "s3_use_path_style": True,
                "endpoints": {svc: endpoint for svc in _LOCALSTACK_SERVICES},
            }
        }
    }


def localstack_boto(service: str, endpoint: str = LOCALSTACK_ENDPOINT) -> Any:
    """A boto3 client pointing at LocalStack — the verification oracle for
    LocalStack e2e tests."""
    import boto3

    return boto3.client(
        service,
        endpoint_url=endpoint,
        aws_access_key_id="test",
        aws_secret_access_key="test",  # noqa: S106 — dummy, LocalStack only
        region_name="us-east-1",
    )


def _localstack_reset(endpoint: str = LOCALSTACK_ENDPOINT) -> None:
    """Reset LocalStack state to a clean slate between tests.

    The ``/_localstack/state/reset`` admin endpoint wipes every backend
    (S3 buckets, Glue databases, Lambdas, …) so the next test sees an
    empty account. The default account ID stays ``000000000000``.
    """
    import requests

    with contextlib.suppress(Exception):
        # POST is the documented method; GET works on some Pro versions.
        requests.post(f"{endpoint}/_localstack/state/reset", timeout=15)


class LocalStackProject:
    """A per-test ``tofu`` workdir bound to LocalStack — emit + apply +
    destroy, with a sidecar provider override pinning every AWS API at the
    LocalStack endpoint."""

    def __init__(
        self, workdir: Path, env: Mapping[str, str], endpoint: str, *, region: str = "us-east-1"
    ) -> None:
        self.workdir = workdir
        self.env = dict(env)
        self.endpoint = endpoint
        self.region = region
        self.applied = False

    # -- module emission ---------------------------------------------------

    def emit(
        self,
        contract: Mapping[str, Any],
        *,
        actions: Sequence[Mapping[str, Any]] = (),
    ) -> str:
        """Compile a contract through the AWS plugin + drop the LocalStack
        provider override sidecar."""
        plugin = get_iac_plugin("aws")
        text = build_module(plugin, contract, actions=actions)
        (self.workdir / "main.tf.json").write_text(text, encoding="utf-8")
        (self.workdir / "provider.tf.json").write_text(
            json.dumps(aws_provider_override(self.endpoint, region=self.region)),
            encoding="utf-8",
        )
        return text

    # -- tofu lifecycle ----------------------------------------------------

    def init(self):
        return runner.tofu_init(str(self.workdir), backend=False, env=self.env)

    def plan(self):
        return runner.tofu_plan(str(self.workdir), env=self.env)

    def apply(self):
        result = runner.tofu_apply(str(self.workdir), env=self.env)
        self.applied = True
        return result

    def destroy(self):
        return runner.tofu_destroy(str(self.workdir), env=self.env)

    def state(self) -> List[str]:
        return runner.tofu_state_list(str(self.workdir), env=self.env)

    def apply_ok(
        self,
        contract: Mapping[str, Any],
        *,
        actions: Sequence[Mapping[str, Any]] = (),
    ):
        """Emit + init + plan + apply, asserting each step succeeds.

        Returns the apply :class:`~fluid_build.iac.runner.TofuResult`.
        """
        self.emit(contract, actions=actions)
        init = self.init()
        assert init.ok, f"tofu init failed:\n{init.stderr or init.stdout}"
        plan = self.plan()
        assert plan.ok, f"tofu plan failed:\n{plan.stderr or plan.stdout}"
        applied = self.apply()
        assert applied.ok, f"tofu apply failed:\n{applied.stderr or applied.stdout}"
        return applied


@pytest.fixture
def localstack_endpoint() -> str:
    """LocalStack endpoint URL; skip the test when LocalStack isn't reachable.

    Also resets LocalStack state before yielding — the emulator's backends
    are process-wide so cleanup between tests is essential for isolation.
    """
    if not LOCALSTACK_ENABLED:
        pytest.skip(LOCALSTACK_SKIP_REASON)
    _localstack_reset(LOCALSTACK_ENDPOINT)
    return LOCALSTACK_ENDPOINT


@pytest.fixture
def localstack_project(
    localstack_endpoint: str, tofu_env: Dict[str, str], tmp_path: Path
) -> Iterator[LocalStackProject]:
    """A :class:`LocalStackProject` bound to a fresh workdir.

    Teardown does NOT run ``tofu destroy`` — LocalStack's Glue API can
    hang for many minutes during destroy (an emulator-side bug, observed
    on 2026.5.0). Inter-test isolation comes from the per-test
    ``_localstack_reset`` call in :func:`localstack_endpoint` instead,
    which wipes every backend in ~1 s. The dedicated destroy test below
    exercises the ``tofu destroy`` path explicitly with a shorter blast
    radius (S3-only).
    """
    project = LocalStackProject(tmp_path, tofu_env, localstack_endpoint)
    yield project


# ---------------------------------------------------------------------------
# Pure-data AWS contract / action builders — importable from test modules
# ---------------------------------------------------------------------------


def aws_iceberg_contract(
    bucket: str,
    database: str = "mesh_silver",
    table: str = "events",
    *,
    schema_cols: Optional[Sequence[Mapping[str, Any]]] = None,
    cid: str = "iac.aws.live",
) -> Dict[str, Any]:
    """A contract with one Iceberg-on-Glue exposure (the mesh interface)."""
    return {
        "id": cid,
        "name": "AWS Live Iceberg",
        "exposes": [
            {
                "exposeId": "events",
                "binding": {
                    "platform": "aws",
                    "format": "iceberg",
                    "location": {
                        "database": database,
                        "table": table,
                        "bucket": bucket,
                        "path": f"silver/{table}/",
                    },
                },
                "contract": {
                    "schema": (
                        list(schema_cols)
                        if schema_cols is not None
                        else [
                            {"name": "event_id", "type": "string", "required": True},
                            {"name": "occurred_at", "type": "timestamp"},
                            {"name": "amount", "type": "decimal(12,2)"},
                        ]
                    )
                },
            }
        ],
    }


def aws_kinesis_contract(stream: str, cid: str = "iac.aws.kinesis") -> Dict[str, Any]:
    return {
        "id": cid,
        "name": "AWS Live Kinesis",
        "exposes": [
            {
                "exposeId": "events_stream",
                "binding": {
                    "platform": "aws",
                    "format": "kafka_topic",
                    "location": {"stream": stream, "shard_count": 2},
                },
            }
        ],
    }


def aws_s3_only_contract(bucket: str, cid: str = "iac.aws.s3only") -> Dict[str, Any]:
    """A contract with just an S3 binding — no Glue, no Kinesis."""
    return {
        "id": cid,
        "name": "AWS S3 Only",
        "exposes": [
            {
                "exposeId": "lake",
                "binding": {
                    "platform": "aws",
                    "format": "parquet",
                    "location": {"bucket": bucket},
                },
            }
        ],
    }


def lambda_inline_action(
    function_name: str, *, role: str = "arn:aws:iam::000000000000:role/fluid-lambda"
) -> Dict[str, Any]:
    """A planner action for a Lambda with inline source — the AWS plugin
    routes it through ``archive_file`` so ``tofu`` zips the code itself."""
    return {
        "op": "lambda.ensure_function",
        "function_name": function_name,
        "runtime": "python3.11",
        "handler": "index.handler",
        "role": role,
        "code": {"ZipFile": "def handler(event, context):\n    return {'ok': True}\n"},
        "timeout": 30,
        "memory_size": 128,
        "environment": {"FLUID_TEST": "1"},
        "tags": {"managed_by": "fluid"},
    }


def sfn_state_machine_action(
    name: str, *, role: str = "arn:aws:iam::000000000000:role/fluid-sfn"
) -> Dict[str, Any]:
    return {
        "op": "stepfunctions.ensure_state_machine",
        "state_machine_name": name,
        "definition": json.dumps(
            {"StartAt": "Done", "States": {"Done": {"Type": "Pass", "End": True}}}
        ),
        "role_arn": role,
        "type": "STANDARD",
        "tags": {"managed_by": "fluid"},
    }


def eventbridge_schedule_action(
    name: str, *, role: str = "arn:aws:iam::000000000000:role/fluid-sched"
) -> Dict[str, Any]:
    return {
        "op": "eventbridge.ensure_schedule",
        "schedule_name": name,
        "schedule_expression": "rate(1 hour)",
        "timezone": "UTC",
        "state": "ENABLED",
        "flexible_time_window": {"mode": "OFF"},
        "target": {
            "arn": "arn:aws:lambda:us-east-1:000000000000:function:fluid-target",
            "role_arn": role,
            "input": '{"x":1}',
        },
    }


def glue_job_action(
    name: str, *, role: str = "arn:aws:iam::000000000000:role/fluid-glue"
) -> Dict[str, Any]:
    return {
        "op": "glue.ensure_job",
        "name": name,
        "role": role,
        "script_location": "s3://fluid-mesh-scripts/etl.py",
        "command_name": "glueetl",
        "glue_version": "4.0",
        "worker_type": "G.1X",
        "number_of_workers": 2,
        "timeout": 60,
        "default_arguments": {"--enable-metrics": "true"},
        "tags": {"managed_by": "fluid"},
    }
