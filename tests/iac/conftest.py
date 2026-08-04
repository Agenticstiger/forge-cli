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
import functools
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

# ---------------------------------------------------------------------------
# tofu init resilience — tolerate transient provider-registry outages
# ---------------------------------------------------------------------------

# Markers in tofu output that mean a transient infra/registry outage (e.g.
# registry.opentofu.org 504s while fetching providers), NOT a contract/tfjson
# error. The IaC tests validate emitted tfjson against the real ``tofu``
# schema (``tofu plan``/``validate``); a provider *download* failure during
# ``tofu init`` is orthogonal, so a failure caused purely by it is converted
# to a skip rather than redding the build on an upstream outage. Real
# tfjson/schema drift fails at plan/validate with output that looks nothing
# like these markers, so it is unaffected.
_REGISTRY_FAILURE_MARKERS = (
    "could not query provider",
    "failed to retrieve",
    "failed to query available provider",
    "authentication checksums for provider",
    "cryptographic signature for provider",
    "request failed after",
    "504",
    "context deadline exceeded",
    "no such host",
    "connection reset",
    "i/o timeout",
    "tls handshake timeout",
)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """Convert an IaC test that failed purely because ``tofu init`` could not
    reach the provider registry (transient 504/network) into a SKIP.

    Mirrors how ``_pytest.skipping`` rewrites ``rep.outcome``/``rep.longrepr``.
    Only the unambiguous registry-failure markers trigger it, so a real
    ``tofu validate``/``plan`` schema error (very different output) still fails.
    """
    outcome = yield
    report = outcome.get_result()
    if report.when != "call" or not report.failed:
        return
    text = str(report.longrepr).lower()
    if any(marker in text for marker in _REGISTRY_FAILURE_MARKERS):
        report.outcome = "skipped"
        report.longrepr = (
            str(item.fspath),
            (item.location[1] or 0) + 1,
            "Skipped: transient OpenTofu provider-registry outage (504/network) "
            "during `tofu init` — not a tfjson error",
        )


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


@functools.lru_cache(maxsize=1)
def _sf_supports_governance_policies(conn_id: int) -> bool:
    """Whether this account can create masking / row-access policies.

    Column-level and row-level security are Enterprise-Edition features. On a
    Standard-Edition account Snowflake answers ``CREATE MASKING POLICY`` with
    ``000002 (0A000): Unsupported feature 'MASKING POLICY'``, which surfaced as
    three red live tests that no code change could ever fix.

    Probed once per connection rather than assumed from an edition string —
    ``SHOW PARAMETERS`` does not report the edition, and the authoritative
    answer is whether the DDL is accepted. ``conn_id`` keys the cache; the
    caller passes ``id(conn)`` so the session-scoped connection probes once.

    Two properties this probe has to hold, both learned the hard way:

    * **It is fully qualified into a throwaway ``FLUID_IACTEST_*`` database.**
      An unqualified ``CREATE`` lands in whatever the user's
      ``DEFAULT_NAMESPACE`` resolves to — a real, operator-owned database —
      which breaks this module's "no pre-existing database is ever touched"
      guarantee, and leaves residue that ``_sweep_stray_test_databases``
      (databases only) could never reach. Creating our own database means the
      existing sweeper covers the probe for free.
    * **Only the edition signal counts as "unsupported".** A bare ``except``
      would fold "no current schema", a permissions error and a network blip
      into the same answer, and the memoised ``False`` would silently skip the
      masking- and row-access-policy tests for the whole session — leaving the
      emitters for column masking and row-level security unverified while the
      suite still reported green. Anything that is not the edition refusal
      propagates, so a broken probe fails loudly instead of quietly disarming
      three governance tests.
    """
    conn = _SF_CONN_BY_ID.get(conn_id)
    if conn is None:  # pragma: no cover - defensive
        return False
    database = f"{TEST_DB_PREFIX}CAPPROBE_{uuid.uuid4().hex[:8].upper()}"
    schema = "S1"
    probe = f'"{database}"."{schema}"."CAPABILITY_PROBE"'
    try:
        create_container(conn, database, schema)
        try:
            with contextlib.closing(conn.cursor()) as cur:
                cur.execute(
                    f"CREATE MASKING POLICY IF NOT EXISTS {probe} "
                    "AS (v STRING) RETURNS STRING -> '***'"
                )
        except Exception as exc:  # noqa: BLE001 — inspected, then re-raised
            if "unsupported feature" in str(exc).lower():
                return False
            raise
        return True
    finally:
        with contextlib.suppress(Exception):
            _drop_database(conn, database)


#: ``lru_cache`` cannot key on the unhashable connection object, so the probe
#: takes ``id(conn)`` and looks the real connection up here.
_SF_CONN_BY_ID: Dict[int, Any] = {}


def requires_governance_policies(conn: Any) -> None:
    """Skip the calling test when the account has no masking/row-access DDL."""
    _SF_CONN_BY_ID[id(conn)] = conn
    if not _sf_supports_governance_policies(id(conn)):
        pytest.skip(
            "account does not support masking / row-access policies "
            "(Enterprise Edition feature) — Snowflake returns "
            "\"Unsupported feature 'MASKING POLICY'\""
        )


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
# GCP Stage 2 — Docker-emulator harness (analogous to LocalStack)
# ---------------------------------------------------------------------------
#
# Three emulator processes (all canonical ports, all OSS):
#
#   * goccy/bigquery-emulator   — HTTP 9050 / gRPC 9060
#   * fsouza/fake-gcs-server    — HTTP 4443 (use ``-scheme http``)
#   * gcloud beta emulators pubsub — TCP 8085
#
# The tofu ``hashicorp/google`` provider exposes per-service
# ``*_custom_endpoint`` knobs (``bigquery_custom_endpoint``,
# ``storage_custom_endpoint``, ``pubsub_custom_endpoint``) — the override
# block emitted here aims those at the three emulators while the
# plugin's ``main.tf.json`` stays portable + credential-free.
#
# Quad-gated, same shape as LocalStack:
#   * ``tofu`` on PATH
#   * the three emulators reachable on their TCP ports
#   * the explicit opt-in ``FLUID_IAC_LIVE_GCP_EMULATOR=1``
#
# Why not testcontainers-python? The existing LocalStack fixture style
# is small, well-understood, and lets the GCP suite stay parallel to
# the AWS suite. Adding a dependency to manage 3 short-lived containers
# wouldn't pay for itself. See
# ``tests/iac/_gcp_emulator/docker-compose.yml`` for the recommended
# local startup; CI runs the same compose file before invoking pytest.


GCP_EMULATOR_BIGQUERY = os.environ.get("FLUID_GCP_BIGQUERY_EMULATOR", "http://localhost:9050")
GCP_EMULATOR_STORAGE = os.environ.get("FLUID_GCP_STORAGE_EMULATOR", "http://localhost:4443")
GCP_EMULATOR_PUBSUB = os.environ.get("PUBSUB_EMULATOR_HOST", "localhost:8085")
GCP_EMULATOR_PROJECT = os.environ.get("FLUID_GCP_EMULATOR_PROJECT", "fluid-emulator")


def _gcp_emulator_port_reachable(host_and_port: str, default_port: int) -> bool:
    """TCP-probe a ``host:port`` pair with a 2 s timeout."""
    import socket as _socket

    host, _, port = host_and_port.replace("http://", "").replace("https://", "").partition(":")
    try:
        with _socket.socket() as sock:
            sock.settimeout(2)
            return sock.connect_ex((host or "localhost", int(port or default_port))) == 0
    except Exception:  # noqa: BLE001
        return False


def _gcp_emulator_ready() -> tuple[bool, str]:
    """Return ``(enabled, skip_reason)`` for the GCP Stage-2 emulator tier."""
    if runner.tofu_path() is None:
        return False, "the `tofu` (OpenTofu) binary is not on PATH"
    if os.environ.get("FLUID_IAC_LIVE_GCP_EMULATOR", "").strip().lower() not in _TRUE:
        return False, (
            "GCP emulator tests are opt-in — set FLUID_IAC_LIVE_GCP_EMULATOR=1 "
            "and start the emulators (tests/iac/_gcp_emulator/docker-compose.yml)"
        )
    missing: List[str] = []
    if not _gcp_emulator_port_reachable(GCP_EMULATOR_BIGQUERY, 9050):
        missing.append(f"BigQuery emulator at {GCP_EMULATOR_BIGQUERY}")
    if not _gcp_emulator_port_reachable(GCP_EMULATOR_STORAGE, 4443):
        missing.append(f"GCS emulator at {GCP_EMULATOR_STORAGE}")
    if not _gcp_emulator_port_reachable(GCP_EMULATOR_PUBSUB, 8085):
        missing.append(f"Pub/Sub emulator at {GCP_EMULATOR_PUBSUB}")
    if missing:
        return False, "GCP emulators not reachable: " + " / ".join(missing)
    return True, ""


GCP_EMULATOR_ENABLED, GCP_EMULATOR_SKIP_REASON = _gcp_emulator_ready()


def gcp_provider_override(
    *,
    project: str = GCP_EMULATOR_PROJECT,
    bigquery_endpoint: str = GCP_EMULATOR_BIGQUERY,
    storage_endpoint: str = GCP_EMULATOR_STORAGE,
    pubsub_endpoint: str = GCP_EMULATOR_PUBSUB,
) -> Dict[str, Any]:
    """A sidecar ``provider`` block aiming the google provider at the
    three local emulators.

    Each provider field is required-by-default for the matching API
    surface. Note the provider's idiosyncratic naming:
        ``big_query_custom_endpoint``  (underscore in big_query!)
        ``storage_custom_endpoint``
        ``pubsub_custom_endpoint``
    Trailing-slash conventions match the provider's own examples.
    """
    bq = bigquery_endpoint.rstrip("/")
    gcs = storage_endpoint.rstrip("/")
    ps = pubsub_endpoint
    if not ps.startswith("http"):
        ps = f"http://{ps}"
    return {
        "provider": {
            "google": {
                "project": project,
                "big_query_custom_endpoint": f"{bq}/bigquery/v2/",
                "storage_custom_endpoint": f"{gcs}/storage/v1/",
                "pubsub_custom_endpoint": f"{ps}/v1/",
                # The emulators ignore auth; an in-memory dummy token
                # stops the provider from looking for ADC.
                "access_token": "emulator-dummy-token",
            }
        }
    }


def gcp_emulator_bigquery_client(*, project: str = GCP_EMULATOR_PROJECT):
    """A ``google.cloud.bigquery.Client`` pointed at the BigQuery emulator."""
    from google.api_core.client_options import ClientOptions
    from google.auth.credentials import AnonymousCredentials
    from google.cloud import bigquery

    return bigquery.Client(
        project=project,
        credentials=AnonymousCredentials(),
        client_options=ClientOptions(api_endpoint=GCP_EMULATOR_BIGQUERY),
    )


def gcp_emulator_storage_client(*, project: str = GCP_EMULATOR_PROJECT):
    """A ``google.cloud.storage.Client`` pointed at the fake-gcs-server."""
    from google.auth.credentials import AnonymousCredentials
    from google.cloud import storage as _storage

    client = _storage.Client(
        project=project,
        credentials=AnonymousCredentials(),
        client_options={"api_endpoint": GCP_EMULATOR_STORAGE},
    )
    return client


class GcpEmulatorProject:
    """A per-test ``tofu`` workdir pinned at the three GCP emulators.

    Mirrors :class:`LocalStackProject` — emit + init + plan + apply +
    destroy, with the provider-override sidecar overlaying the plugin's
    portable ``main.tf.json``.
    """

    def __init__(
        self, workdir: Path, env: Mapping[str, str], *, project: str = GCP_EMULATOR_PROJECT
    ) -> None:
        self.workdir = workdir
        self.env = dict(env)
        # The emulators ignore credentials; the dummy access token stops
        # the provider from trying to resolve real ADC.
        self.env["GOOGLE_OAUTH_ACCESS_TOKEN"] = "emulator-dummy-token"
        self.project = project
        self.applied = False

    def emit(
        self,
        contract: Mapping[str, Any],
        *,
        actions: Sequence[Mapping[str, Any]] = (),
    ) -> str:
        plugin = get_iac_plugin("gcp")
        text = build_module(plugin, contract, actions=actions)
        (self.workdir / "main.tf.json").write_text(text, encoding="utf-8")
        (self.workdir / "provider.tf.json").write_text(
            json.dumps(gcp_provider_override(project=self.project)),
            encoding="utf-8",
        )
        return text

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

    def apply_ok(
        self,
        contract: Mapping[str, Any],
        *,
        actions: Sequence[Mapping[str, Any]] = (),
    ):
        self.emit(contract, actions=actions)
        init = self.init()
        assert init.ok, f"tofu init failed:\n{init.stderr or init.stdout}"
        plan = self.plan()
        assert plan.ok, f"tofu plan failed:\n{plan.stderr or plan.stdout}"
        applied = self.apply()
        assert applied.ok, f"tofu apply failed:\n{applied.stderr or applied.stdout}"
        return applied


@pytest.fixture
def gcp_emulator_project(tofu_env: Dict[str, str], tmp_path: Path) -> Iterator[GcpEmulatorProject]:
    """A :class:`GcpEmulatorProject` bound to a fresh workdir.

    The emulators reset their state on container restart but not between
    individual ``tofu apply`` invocations, so per-test resource names
    should be unique (use ``aws_real_project``-style UUID suffixes if
    multiple tests touch the same dataset/bucket/topic).

    Teardown: best-effort ``tofu destroy``. Failures during destroy do
    not fail the test — emulator semantics around resource lifecycle
    are looser than real GCP, and a subsequent emulator restart wipes
    state anyway.
    """
    if not GCP_EMULATOR_ENABLED:
        pytest.skip(GCP_EMULATOR_SKIP_REASON)
    project = GcpEmulatorProject(tmp_path, tofu_env)
    try:
        yield project
    finally:
        if project.applied:
            with contextlib.suppress(Exception):
                project.destroy()


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


def aws_cross_account_iceberg_contract(
    bucket: str,
    database: str,
    table: str,
    *,
    consumer_principal: str,
    cid: str = "iac.aws.xacc",
) -> Dict[str, Any]:
    """Iceberg-on-Glue contract with an LF grant to a non-deployer principal.

    Any IAM-principal LF grant on a Glue-catalog-backed S3 binding
    triggers BOTH:

      * ``aws_lakeformation_permissions`` granting SELECT/DESCRIBE
      * ``aws_s3_bucket_policy`` granting s3:GetObject + s3:ListBucket

    The pairing is automatic — the canonical AWS LF cross-account pattern
    (the aws-lakeformation-best-practices cross-account FAQ + Komminar's
    Terraform article both spell it out: LF alone does not authorise
    object-byte reads; a bucket-policy companion is required). No
    opt-in flag — emitting both is correct for in-account principals
    too (the bucket policy is additive on top of their IAM read).
    """
    return {
        "id": cid,
        "name": "AWS X-acc Iceberg",
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
                    "governance": {
                        "lakeFormation": {
                            "grants": [
                                {
                                    "principal": consumer_principal,
                                    "permissions": ["SELECT", "DESCRIBE"],
                                }
                            ]
                        }
                    },
                },
                "contract": {
                    "schema": [
                        {"name": "event_id", "type": "string", "required": True},
                        {"name": "amount", "type": "decimal(12,2)"},
                    ]
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


# ===========================================================================
# AWS Stage 3 — REAL AWS account (no emulator, real billable resources)
# ===========================================================================
#
# Triple-gated:
#   * ``tofu`` on PATH
#   * boto3 can authenticate (caller-identity returns a non-root :user/...)
#   * the four ``FLUID_AWS_*_ROLE_ARN`` env vars are set
#   * the explicit opt-in flag ``FLUID_IAC_LIVE_AWS=1``
#
# Every test provisions into resources with a unique
# ``fluid-iactest-<uuid>`` prefix; teardown destroys; a session finalizer
# sweeps any survivors. No pre-existing AWS resources are ever touched.


def _aws_live_ready() -> Tuple[bool, str]:
    """Return ``(enabled, skip_reason)`` for the real-AWS Stage-3 tier."""
    if runner.tofu_path() is None:
        return False, "the `tofu` (OpenTofu) binary is not on PATH"
    if os.environ.get("FLUID_IAC_LIVE_AWS", "").strip().lower() not in _TRUE:
        return False, "live AWS tests are opt-in — set FLUID_IAC_LIVE_AWS=1"
    for var in (
        "FLUID_AWS_LAMBDA_ROLE_ARN",
        "FLUID_AWS_SFN_ROLE_ARN",
        "FLUID_AWS_GLUE_ROLE_ARN",
        "FLUID_AWS_SPECTRUM_ROLE_ARN",
    ):
        if not os.environ.get(var):
            return False, f"missing IAM role ARN: ${var}"
    try:
        import boto3

        sts = boto3.client("sts")
        identity = sts.get_caller_identity()
        if identity.get("Arn", "").endswith(":root"):
            return False, "live AWS tests refuse to run with root credentials"
    except Exception as exc:  # noqa: BLE001
        return False, f"AWS auth check failed: {exc}"
    return True, ""


AWS_LIVE_ENABLED, AWS_LIVE_SKIP_REASON = _aws_live_ready()
# Throwaway resource-name prefix so the sweeper can find leftovers.
AWS_LIVE_PREFIX = "fluid-iactest"


def aws_real_boto(service: str) -> Any:
    """A boto3 client for real AWS — uses the default credentials chain
    (AWS_PROFILE / env / ~/.aws/credentials). No endpoint override.

    Region is resolved explicitly from ``AWS_REGION`` / ``AWS_DEFAULT_REGION``
    rather than relying on boto3's default chain: when ``AWS_PROFILE`` is set
    and the profile carries its own ``region = …`` in ``~/.aws/config``,
    botocore prefers the profile region over the ``AWS_REGION`` env var
    (documented quirk). Tofu honours ``AWS_REGION`` directly, so without
    this override the test's boto client would query a different region
    than the one tofu provisioned into.
    """
    import boto3

    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"
    return boto3.client(service, region_name=region)


def _aws_live_uuid() -> str:
    """Short uppercase-hex token for unique resource names per test."""
    return uuid.uuid4().hex[:10]


class RealAwsProject:
    """A per-test ``tofu`` workdir bound to real AWS — emit + apply + destroy.

    No provider override; the ``hashicorp/aws`` provider self-configures
    from ``AWS_*`` env / ``~/.aws/credentials``. Resource names are scoped
    by the per-test uuid suffix so concurrent runs never collide.
    """

    def __init__(self, workdir: Path, env: Mapping[str, str]) -> None:
        self.workdir = workdir
        self.env = dict(env)
        self.applied = False
        self.uid = _aws_live_uuid()

    def name(self, slug: str) -> str:
        """``fluid-iactest-<slug>-<uid>`` — unique, sweeper-discoverable."""
        return f"{AWS_LIVE_PREFIX}-{slug}-{self.uid}"

    def emit(
        self,
        contract: Mapping[str, Any],
        *,
        actions: Sequence[Mapping[str, Any]] = (),
    ) -> str:
        plugin = get_iac_plugin("aws")
        text = build_module(plugin, contract, actions=actions)
        (self.workdir / "main.tf.json").write_text(text, encoding="utf-8")
        return text

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

    def apply_ok(
        self,
        contract: Mapping[str, Any],
        *,
        actions: Sequence[Mapping[str, Any]] = (),
    ):
        """Emit + init + plan + apply with assertions at each step."""
        self.emit(contract, actions=actions)
        init = self.init()
        assert init.ok, f"tofu init failed:\n{init.stderr or init.stdout}"
        plan = self.plan()
        assert plan.ok, f"tofu plan failed:\n{plan.stderr or plan.stdout}"
        applied = self.apply()
        assert applied.ok, f"tofu apply failed:\n{applied.stderr or applied.stdout}"
        return applied


@pytest.fixture(scope="session")
def aws_account() -> Dict[str, str]:
    """Session-scoped real-AWS account identity. Skips when the gate is shut.

    Returns ``{"account_id": ..., "user_arn": ..., "region": ...}``.
    """
    if not AWS_LIVE_ENABLED:
        pytest.skip(AWS_LIVE_SKIP_REASON)
    import boto3

    sts = boto3.client("sts")
    identity = sts.get_caller_identity()
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"
    return {
        "account_id": identity["Account"],
        "user_arn": identity["Arn"],
        "region": region,
    }


@pytest.fixture
def aws_real_project(
    aws_account: Dict[str, str], tofu_env: Dict[str, str], tmp_path: Path
) -> Iterator[RealAwsProject]:
    """A :class:`RealAwsProject` bound to a fresh workdir.

    Teardown: best-effort ``tofu destroy`` so applied resources don't
    linger. The session-end ``_aws_live_sweeper`` is the belt-and-
    suspenders net.
    """
    project = RealAwsProject(tmp_path, tofu_env)
    try:
        yield project
    finally:
        if project.applied:
            with contextlib.suppress(Exception):
                project.destroy()


@pytest.fixture(scope="session", autouse=True)
def _aws_live_sweeper(request) -> Iterator[None]:
    """Session finalizer: list and delete any ``fluid-iactest-*`` resources
    still present in the account when the test session ends.

    Catches resources that a crashed or interrupted test left behind. Only
    fires when the live tier is enabled; otherwise a no-op so plain test
    runs don't touch any AWS API.

    Region note: every boto client below passes ``region_name`` explicitly
    — when ``AWS_PROFILE`` is set and the profile carries its own
    ``region = …`` in ``~/.aws/config``, botocore prefers that over the
    ``AWS_REGION`` env var. Without the explicit region, the sweeper
    would scan ``us-east-1`` (profile default) and silently miss any
    test that ran in another region — which is exactly how a
    Redshift Serverless workgroup once leaked at $2.88/hour.

    Coverage: S3 buckets, Glue databases/tables, Kinesis streams,
    Lambda functions, Step Functions state machines, Redshift Serverless
    namespaces + workgroups (workgroup first, then namespace —
    AWS rejects namespace deletes while a workgroup still attaches).
    """
    yield
    if not AWS_LIVE_ENABLED:
        return
    try:
        import boto3
    except Exception:  # noqa: BLE001
        return

    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"

    def _client(service: str):
        return boto3.client(service, region_name=region)

    # S3 buckets — global namespace; region doesn't matter for listing.
    # Bootstrap-managed buckets use the ``fluid-iacboot-*`` prefix so
    # they never match ``AWS_LIVE_PREFIX`` here. As defense-in-depth
    # we additionally re-check the bucket's tags and skip anything
    # tagged ``purpose=stage3-iac-testing``.
    def _is_bootstrap_resource(client_get_tags) -> bool:
        try:
            tags = client_get_tags() or []
        except Exception:  # noqa: BLE001
            return False
        for t in tags:
            if t.get("Key") == "purpose" and t.get("Value") == "stage3-iac-testing":
                return True
        return False

    with contextlib.suppress(Exception):
        s3 = _client("s3")
        s3r = boto3.resource("s3", region_name=region)
        for entry in s3.list_buckets().get("Buckets", []):
            name = entry["Name"]
            if not name.startswith(AWS_LIVE_PREFIX):
                continue
            if _is_bootstrap_resource(lambda: s3.get_bucket_tagging(Bucket=name).get("TagSet")):
                continue
            with contextlib.suppress(Exception):
                s3r.Bucket(name).objects.all().delete()
                s3r.Bucket(name).object_versions.all().delete()
                s3.delete_bucket(Bucket=name)
    # Glue databases — must drop tables first.
    with contextlib.suppress(Exception):
        glue = _client("glue")
        for db in glue.get_databases().get("DatabaseList", []):
            name = db["Name"]
            if not name.startswith(AWS_LIVE_PREFIX.replace("-", "_")):
                continue
            with contextlib.suppress(Exception):
                for tbl in glue.get_tables(DatabaseName=name).get("TableList", []):
                    glue.delete_table(DatabaseName=name, Name=tbl["Name"])
                glue.delete_database(Name=name)
    # Kinesis streams.
    with contextlib.suppress(Exception):
        ks = _client("kinesis")
        for name in ks.list_streams().get("StreamNames", []):
            if name.startswith(AWS_LIVE_PREFIX):
                with contextlib.suppress(Exception):
                    ks.delete_stream(StreamName=name, EnforceConsumerDeletion=True)
    # Lambda functions.
    with contextlib.suppress(Exception):
        lam = _client("lambda")
        paginator = lam.get_paginator("list_functions")
        for page in paginator.paginate():
            for fn in page.get("Functions", []):
                if fn["FunctionName"].startswith(AWS_LIVE_PREFIX):
                    with contextlib.suppress(Exception):
                        lam.delete_function(FunctionName=fn["FunctionName"])
    # Step Functions state machines.
    with contextlib.suppress(Exception):
        sfn = _client("stepfunctions")
        paginator = sfn.get_paginator("list_state_machines")
        for page in paginator.paginate():
            for sm in page.get("stateMachines", []):
                if sm["name"].startswith(AWS_LIVE_PREFIX):
                    with contextlib.suppress(Exception):
                        sfn.delete_state_machine(stateMachineArn=sm["stateMachineArn"])
    # Redshift Serverless — workgroups MUST go before namespaces. Both
    # are billable while AVAILABLE; the workgroup at base-capacity-8
    # alone is ~$2.88/hour, so this is the highest-impact sweep target.
    with contextlib.suppress(Exception):
        rs = _client("redshift-serverless")
        for wg in rs.list_workgroups().get("workgroups", []):
            if wg["workgroupName"].startswith(AWS_LIVE_PREFIX):
                with contextlib.suppress(Exception):
                    rs.delete_workgroup(workgroupName=wg["workgroupName"])
        for ns in rs.list_namespaces().get("namespaces", []):
            if ns["namespaceName"].startswith(AWS_LIVE_PREFIX):
                with contextlib.suppress(Exception):
                    rs.delete_namespace(namespaceName=ns["namespaceName"])


def aws_real_iceberg_contract(
    bucket: str, database: str, table: str, *, cid: str = "iac.aws.real"
) -> Dict[str, Any]:
    """An Iceberg-on-Glue contract for Stage 3, using real-AWS-safe names."""
    return aws_iceberg_contract(bucket, database=database, table=table, cid=cid)


def aws_real_role_arn(kind: str) -> str:
    """Resolve one of the bootstrap IAM role ARNs from the env."""
    key = {
        "lambda": "FLUID_AWS_LAMBDA_ROLE_ARN",
        "sfn": "FLUID_AWS_SFN_ROLE_ARN",
        "glue": "FLUID_AWS_GLUE_ROLE_ARN",
        "spectrum": "FLUID_AWS_SPECTRUM_ROLE_ARN",
        # Cross-account-consumer proxy role — trust policy allows any
        # IAM identity in the deployer's account to sts:AssumeRole. Used
        # by the Stage 3 cross-account proxy tests as the "principal in
        # account B" — same trust-policy shape a real cross-account
        # consumer would have, just collapsed onto a single sandbox.
        "consumer": "FLUID_AWS_CONSUMER_ROLE_ARN",
    }[kind]
    arn = os.environ.get(key)
    if not arn:
        raise RuntimeError(f"missing ${key} — Stage 3 bootstrap not applied?")
    return arn


# ---------------------------------------------------------------------------
# GCP Stage 3 — real-GCP harness (cloud-side e2e via Application Default Credentials)
# ---------------------------------------------------------------------------
#
# Mirrors the AWS Stage 3 harness: per-test ``tofu`` workdir bound to a
# real GCP project + service-account impersonation, with a session
# sweeper that cleans up any ``fluid-iactest-*`` resources at session
# end (BigQuery datasets, GCS buckets, Pub/Sub topics).
#
# Quad-gated:
#   * ``tofu`` on PATH
#   * ADC reachable (``GOOGLE_APPLICATION_CREDENTIALS`` or
#     ``~/.config/gcloud/application_default_credentials.json``)
#   * the explicit opt-in ``FLUID_IAC_LIVE_GCP=1``
#   * project + test-SA env vars from the bootstrap
#
# Why impersonation, not SA keys: many GCP orgs enforce
# ``constraints/iam.disableServiceAccountKeyCreation`` (a CIS+SOC2-friendly
# guard against leaked credentials). The bootstrap grants the runner's
# user principal ``serviceAccountTokenCreator`` on the test SA so ADC
# + impersonation works without any key material on disk.


GCP_LIVE_PREFIX = "fluid-iactest"


def _gcp_adc_path() -> Optional[str]:
    """Return the ADC file path if findable, else None."""
    p = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if p and Path(p).exists():
        return p
    home_adc = Path.home() / ".config" / "gcloud" / "application_default_credentials.json"
    if home_adc.exists():
        return str(home_adc)
    return None


def _gcp_live_ready() -> Tuple[bool, str]:
    """Return ``(enabled, skip_reason)`` for the real-GCP Stage-3 tier."""
    if runner.tofu_path() is None:
        return False, "the `tofu` (OpenTofu) binary is not on PATH"
    if os.environ.get("FLUID_IAC_LIVE_GCP", "").strip().lower() not in _TRUE:
        return False, "live GCP tests are opt-in — set FLUID_IAC_LIVE_GCP=1"
    if not os.environ.get("FLUID_GCP_PROJECT"):
        return (
            False,
            "missing $FLUID_GCP_PROJECT — apply tests/iac/_gcp_stage3_bootstrap and source its outputs",
        )
    if not os.environ.get("FLUID_GCP_TEST_SA"):
        return False, "missing $FLUID_GCP_TEST_SA — apply the bootstrap module"
    adc = _gcp_adc_path()
    if not adc:
        return False, (
            "no Application Default Credentials found. Run "
            "`gcloud auth application-default login` or set "
            "GOOGLE_APPLICATION_CREDENTIALS"
        )
    return True, ""


GCP_LIVE_ENABLED, GCP_LIVE_SKIP_REASON = _gcp_live_ready()
GCP_LIVE_PROJECT = os.environ.get("FLUID_GCP_PROJECT", "")
GCP_LIVE_TEST_SA = os.environ.get("FLUID_GCP_TEST_SA", "")
GCP_LIVE_REGION = os.environ.get("FLUID_GCP_REGION", "europe-west1")
# Cross-project-consumer proxy SA — same project for now (boundary
# crossing needs a second sandbox project). Used by the Stage 3 GCP
# cross-project proxy test as the "principal in project B"; same
# member-string syntax + dataset-IAM grant shape a real cross-project
# consumer would have. See tests/iac/_gcp_stage3_bootstrap/main.tf.json
# (fluid_test_consumer SA + output ``consumer_sa_email``).
GCP_LIVE_CONSUMER_SA = os.environ.get("FLUID_GCP_CONSUMER_SA", "")


def _gcp_live_uuid() -> str:
    """Short hex token unique per test."""
    return uuid.uuid4().hex[:10]


def gcp_real_client(service: str, *, target_sa: Optional[str] = None) -> Any:
    """Return a google-cloud-* client authenticated as the user, impersonating
    a target SA. Service: 'bigquery' | 'storage' | 'pubsub_publisher' |
    'pubsub_subscriber'. The impersonation lets the test verify resources
    created by tofu (which also impersonates) without needing the SA's
    own key material on disk.

    ``target_sa`` defaults to ``GCP_LIVE_TEST_SA`` (the deployer/runner SA).
    Pass ``GCP_LIVE_CONSUMER_SA`` to take the consumer's credentials —
    used by the Stage 3 cross-project proxy tests to verify the consumer
    actually gets the access the contract's dataset-IAM grant promised.
    """
    from google.auth import default as _default
    from google.auth import impersonated_credentials

    target_principal = target_sa or GCP_LIVE_TEST_SA
    source_creds, _ = _default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    target = impersonated_credentials.Credentials(
        source_credentials=source_creds,
        target_principal=target_principal,
        target_scopes=["https://www.googleapis.com/auth/cloud-platform"],
        lifetime=3600,
    )
    if service == "bigquery":
        from google.cloud import bigquery

        return bigquery.Client(project=GCP_LIVE_PROJECT, credentials=target)
    if service == "storage":
        from google.cloud import storage

        return storage.Client(project=GCP_LIVE_PROJECT, credentials=target)
    if service == "pubsub_publisher":
        from google.cloud import pubsub_v1

        return pubsub_v1.PublisherClient(credentials=target)
    if service == "pubsub_subscriber":
        from google.cloud import pubsub_v1

        return pubsub_v1.SubscriberClient(credentials=target)
    raise ValueError(f"unknown service for gcp_real_client: {service}")


class GcpRealProject:
    """A per-test ``tofu`` workdir bound to real GCP via impersonation.

    The plugin emits a credential-free ``main.tf.json``; the harness
    overlays a sidecar ``provider.tf.json`` setting ``project`` +
    ``impersonate_service_account``. Resource names use the per-test
    UUID suffix so concurrent runs never collide.
    """

    def __init__(self, workdir: Path, env: Mapping[str, str]) -> None:
        self.workdir = workdir
        self.env = dict(env)
        self.applied = False
        self.uid = _gcp_live_uuid()

    def name(self, slug: str) -> str:
        """``fluid-iactest-<slug>-<uid>``. Resource-naming rules differ
        by GCP service (BQ datasets want ``[a-zA-Z0-9_]`` only; GCS
        buckets want lowercase alphanumeric + dashes; Pub/Sub topics
        are lenient). Callers normalise with ``.replace("-", "_")``
        for BQ, leave hyphens for GCS / Pub/Sub.
        """
        return f"{GCP_LIVE_PREFIX}-{slug}-{self.uid}"

    def emit(
        self,
        contract: Mapping[str, Any],
        *,
        actions: Sequence[Mapping[str, Any]] = (),
    ) -> str:
        plugin = get_iac_plugin("gcp")
        text = build_module(plugin, contract, actions=actions)
        (self.workdir / "main.tf.json").write_text(text, encoding="utf-8")
        # Sidecar provider override — pins ``project`` and triggers
        # impersonation. tofu merges every ``*.tf.json`` in the workdir.
        (self.workdir / "provider.tf.json").write_text(
            json.dumps(
                {
                    "provider": {
                        "google": {
                            "project": GCP_LIVE_PROJECT,
                            "region": GCP_LIVE_REGION,
                            "impersonate_service_account": GCP_LIVE_TEST_SA,
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        return text

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

    def apply_ok(
        self,
        contract: Mapping[str, Any],
        *,
        actions: Sequence[Mapping[str, Any]] = (),
    ):
        self.emit(contract, actions=actions)
        init = self.init()
        assert init.ok, f"tofu init failed:\n{init.stderr or init.stdout}"
        plan = self.plan()
        assert plan.ok, f"tofu plan failed:\n{plan.stderr or plan.stdout}"
        applied = self.apply()
        assert applied.ok, f"tofu apply failed:\n{applied.stderr or applied.stdout}"
        return applied


@pytest.fixture(scope="session")
def gcp_account() -> Dict[str, str]:
    """Session-scoped real-GCP identity. Skips when the gate is shut."""
    if not GCP_LIVE_ENABLED:
        pytest.skip(GCP_LIVE_SKIP_REASON)
    return {
        "project_id": GCP_LIVE_PROJECT,
        "region": GCP_LIVE_REGION,
        "test_sa": GCP_LIVE_TEST_SA,
    }


@pytest.fixture
def gcp_real_project(
    gcp_account: Dict[str, str], tofu_env: Dict[str, str], tmp_path: Path
) -> Iterator[GcpRealProject]:
    """A :class:`GcpRealProject` bound to a fresh workdir.

    Teardown: best-effort ``tofu destroy``. The session-end sweeper is
    the belt-and-suspenders net (catches resources left behind by
    crashed tests).
    """
    if not GCP_LIVE_ENABLED:
        pytest.skip(GCP_LIVE_SKIP_REASON)
    project = GcpRealProject(tmp_path, tofu_env)
    try:
        yield project
    finally:
        if project.applied:
            with contextlib.suppress(Exception):
                project.destroy()


@pytest.fixture(scope="session", autouse=True)
def _gcp_live_sweeper(request) -> Iterator[None]:
    """Session finalizer: nuke any ``fluid-iactest-*`` GCP resources
    still present in the project at session end.

    BigQuery: lists datasets matching the prefix and deletes them
    (with ``delete_contents=true`` to drop any straggling tables).
    GCS: empties + deletes matching buckets.
    Pub/Sub: deletes matching topics + subscriptions.

    Only fires when the live tier is enabled.
    """
    yield
    if not GCP_LIVE_ENABLED:
        return
    bq_prefix = GCP_LIVE_PREFIX.replace("-", "_")  # BQ dataset names disallow hyphens
    with contextlib.suppress(Exception):
        bq = gcp_real_client("bigquery")
        for ds in bq.list_datasets():
            if not ds.dataset_id.startswith(bq_prefix):
                continue
            with contextlib.suppress(Exception):
                bq.delete_dataset(ds.dataset_id, delete_contents=True, not_found_ok=True)
    with contextlib.suppress(Exception):
        gcs = gcp_real_client("storage")
        for b in gcs.list_buckets(prefix=GCP_LIVE_PREFIX):
            with contextlib.suppress(Exception):
                # Empty any objects first, then delete.
                bkt = gcs.bucket(b.name)
                for blob in bkt.list_blobs():
                    with contextlib.suppress(Exception):
                        blob.delete()
                bkt.delete(force=True)
    with contextlib.suppress(Exception):
        from google.cloud import pubsub_v1

        pub = gcp_real_client("pubsub_publisher")
        for topic in pub.list_topics(request={"project": f"projects/{GCP_LIVE_PROJECT}"}):
            name = topic.name.rsplit("/", 1)[-1]
            if not name.startswith(GCP_LIVE_PREFIX):
                continue
            with contextlib.suppress(Exception):
                pub.delete_topic(request={"topic": topic.name})
        sub_client = gcp_real_client("pubsub_subscriber")
        for sub in sub_client.list_subscriptions(
            request={"project": f"projects/{GCP_LIVE_PROJECT}"}
        ):
            sname = sub.name.rsplit("/", 1)[-1]
            if not sname.startswith(GCP_LIVE_PREFIX):
                continue
            with contextlib.suppress(Exception):
                sub_client.delete_subscription(request={"subscription": sub.name})
