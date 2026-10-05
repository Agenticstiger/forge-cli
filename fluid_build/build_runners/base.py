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

"""Shared base for build_runners: constants, helpers, and the top-level
``run_builds_from_args`` dispatcher.

This module is the runtime-execution counterpart of
``fluid_build.engines.*`` (which generates dbt/SQL project files at
``fluid generate speed-transformation`` time). See
``fluid_build/engines/__init__.py`` for the generation-side framework.

``cli/apply.py`` calls :func:`run_builds_from_args` directly under
``--mode amend-and-build``.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from fluid_build._console import cprint, success
from fluid_build._console import error as console_error
from fluid_build._contract_loader import CLIError, load_contract_with_overlay
from fluid_build.util.binding_paths import ENV_PLACEHOLDER_RE

LOG = logging.getLogger("fluid.build_runners")

# ``{{ env.NAME }}`` placeholder substitution — resolved against
# ``os.environ`` at run time. Used by both the dbt profile generator and
# the dbt command builder to expand author-supplied vars/resources
# references.
# The pattern itself lives in fluid_build/util/binding_paths.py, shared with
# the readers of a landed file (verify, diff, the local provider).

# Used by the dbt command-log renderer to decide whether the value of an
# ``-e KEY=VALUE`` pair should be redacted. A structural hack: if the KEY
# looks sensitive (password/token/etc.), the value is replaced with
# ``<redacted>`` before being printed. Keep this in sync with the module
# that owns the renderer (``build_runners.dbt.runner._render_command_for_log``).
SENSITIVE_ENV_KEY_RE = re.compile(
    r"(?i)(password|passphrase|secret|token|api[_-]?key|private[_-]?key|credential|auth)"
)


def _resolve_env_placeholders(value: Any) -> Any:
    """Resolve ``{{ env.NAME }}`` placeholders against the current environment.

    Recurses into lists and dicts. Non-string/list/dict values are returned
    unchanged. A missing env var resolves to the empty string (not ``None``)
    so downstream YAML dumps and command-line args don't carry a literal
    ``None`` token.
    """
    if isinstance(value, str):
        return ENV_PLACEHOLDER_RE.sub(lambda m: os.getenv(m.group(1), ""), value)
    if isinstance(value, list):
        return [_resolve_env_placeholders(item) for item in value]
    if isinstance(value, dict):
        return {key: _resolve_env_placeholders(item) for key, item in value.items()}
    return value


def is_dbt_build(build: Dict[str, Any]) -> bool:
    """Return True when a build should execute as a dbt project.

    Accepts ``dbt`` plus any ``dbt-<adapter>`` variant (``dbt-bigquery``,
    ``dbt-snowflake``, ``dbt-redshift``, ``dbt-postgres``, ``dbt-duckdb``, …).
    The adapter is selected by dbt itself from ``profiles.yml``; the engine
    name just routes the build into the dbt execute path here.
    """
    engine = (build.get("engine") or "").strip().lower()
    return engine == "dbt" or engine.startswith("dbt-")


def is_embedded_sql_build(build: Dict[str, Any]) -> bool:
    """Return True for builds with inline SQL (``engine: sql`` or
    ``pattern: embedded-logic`` + inline ``properties.sql``).

    These builds carry their SQL directly in the contract and are executed
    by the runtime platform the build declares — the local provider's
    DuckDB engine for ``platform: local`` (or an unset platform), the
    warehouse itself for a declared warehouse (see
    :data:`_EMBEDDED_SQL_EXECUTORS`).  They do NOT require an external
    Python script, so the python-runner's ``resolve_script_path`` lookup
    is the wrong dispatch — it always returns ``None`` and emits the
    confusing "Script not found: ingest.py" warning.

    Bug A4-A: ``--mode amend-and-build`` delegated to ``run_builds_from_args``
    which fell through to the Python-runner branch for embedded-SQL builds.
    """
    engine = (build.get("engine") or "").strip().lower()
    pattern = (build.get("pattern") or "").strip().lower()
    if engine == "sql":
        return True
    if pattern == "embedded-logic":
        props = build.get("properties") or {}
        return bool(props.get("sql"))
    return False


#: ``execution.runtime.platform`` values that mean "run this inline SQL on
#: the operator's machine". An absent platform keeps the historical local
#: behaviour, so contracts that never declared one are unaffected.
LOCAL_SQL_PLATFORMS = frozenset({"", "local", "duckdb"})


def embedded_sql_platform(build: Dict[str, Any]) -> str:
    """Return the lower-cased ``execution.runtime.platform`` of a build.

    Empty string when the build declares no runtime platform.
    """
    execution = build.get("execution") or {}
    runtime = execution.get("runtime") or {} if isinstance(execution, dict) else {}
    if not isinstance(runtime, dict):
        return ""
    return str(runtime.get("platform") or "").strip().lower()


# Engines that ship as acquisition runners under build_runners/<engine>/.
ACQUISITION_ENGINES = frozenset(
    {"duckdb", "airbyte", "meltano", "dlt", "kafka-connect", "debezium"}
)


def _declared_capabilities_for_engine(engine: str):
    """Return the set of capabilities the named engine declares.

    Returns None when we can't import the runner (so the caller skips the
    capability gate rather than failing loudly on an unrelated import
    issue). The lazy import keeps fluid CLI startup fast.
    """
    runner_modpaths = {
        "duckdb": ("fluid_build.build_runners.duckdb.runner", "DuckdbRunner"),
        "dlt": ("fluid_build.build_runners.dlt.runner", "DltRunner"),
        "meltano": ("fluid_build.build_runners.meltano.runner", "MeltanoRunner"),
        "airbyte": ("fluid_build.build_runners.airbyte.runner", "AirbyteRunner"),
        "kafka-connect": (
            "fluid_build.build_runners.kafka_connect.runner",
            "KafkaConnectRunner",
        ),
        "debezium": ("fluid_build.build_runners.debezium.runner", "DebeziumRunner"),
    }
    target = runner_modpaths.get(engine)
    if not target:
        return None
    try:
        import importlib

        mod = importlib.import_module(target[0])
        cls = getattr(mod, target[1], None)
        if cls is None:
            return None
        decl = getattr(cls, "declared_capabilities", None)
        if decl is None:
            return None
        return [str(c.value if hasattr(c, "value") else c) for c in decl]
    except Exception:  # noqa: BLE001
        return None


def is_acquisition_build(build: Dict[str, Any]) -> bool:
    """Return True for builds with ``pattern: acquisition`` AND a known runner."""
    if (build.get("pattern") or "").strip().lower() != "acquisition":
        return False
    engine = (build.get("engine") or "").strip().lower()
    return engine in ACQUISITION_ENGINES


# Acquisition-engine registry: ``engine name → (module path, callable name)``.
# Adding a new engine is one entry here + a runner module — no edits to
# the dispatcher's switch chain. Modules are imported lazily so a missing
# optional extra (e.g. ``dlt`` not installed) doesn't abort the import
# of this whole module.
_ACQUISITION_RUNNER_REGISTRY: Dict[str, tuple] = {
    "duckdb": (".duckdb.runner", "execute_duckdb_build"),
    "dlt": (".dlt.runner", "execute_dlt_build"),
    "meltano": (".meltano.runner", "execute_meltano_build"),
    "airbyte": (".airbyte.runner", "execute_airbyte_build"),
    "kafka-connect": (".kafka_connect.runner", "execute_kafka_connect_build"),
    "debezium": (".debezium.runner", "execute_debezium_build"),
}


def _execute_acquisition_build(
    build: Dict[str, Any],
    contract: Dict[str, Any],
    contract_dir: Path,
    *,
    dry_run: bool,
    sample_rows: Any = None,
) -> int:
    """Dispatch an acquisition build to its runner.

    Looks up the engine in ``_ACQUISITION_RUNNER_REGISTRY`` and invokes
    the registered callable. Capability negotiation runs first so a
    contract asking for an unsupported capability fails with the rich
    ``CapabilityMismatchError`` instead of running the wrong shape.
    """
    engine = (build.get("engine") or "").strip().lower()

    # Capability negotiation: each runner declares the capabilities it
    # supports via ``declared_capabilities``. If the build asks for one
    # the runner doesn't declare, raise the typed catalog error so the
    # user sees the five-field Panel pointing to a runner that does.
    asked = [str(c) for c in (build.get("capabilities") or [])]
    if asked:
        declared = _declared_capabilities_for_engine(engine)
        if declared is not None and not set(asked).issubset(set(declared)):
            from fluid_build._errors import CapabilityMismatchError

            raise CapabilityMismatchError.for_runner(
                runner_name=engine,
                asked=asked,
                declared=list(declared),
            )

    entry = _ACQUISITION_RUNNER_REGISTRY.get(engine)
    if entry is None:
        LOG.error(
            "acquisition.engine_not_implemented engine=%s build=%s",
            engine,
            build.get("id"),
        )
        return 1

    # Resolve ``{{ env.X }}`` placeholders across the ENTIRE build + contract
    # before the runner sees them. Some runners do their own per-slice
    # resolution (e.g. dlt source_dict at runner.py:480) — those calls are
    # left in place as a defence-in-depth fallback for runners invoked
    # outside this dispatcher. Doing it once HERE guarantees every runner
    # gets resolved values for every nested field (source.connection.*,
    # properties.<engine>.*, sink.*, delivery.*, schemaEvolution.*, …) so
    # operators don't need to memorise which fields support templating.
    #
    # Secrets policy: this is the runtime path; values stay in process
    # memory and never get serialised to a remote catalog, so we resolve
    # everything (including secret-shaped vars). Catalog-export paths use
    # ``cli/_common.py::resolve_contract_env_templates`` instead, which
    # leaves sensitive placeholders literal.
    build = _resolve_env_placeholders(build)
    contract = _resolve_env_placeholders(contract)

    module_path, function_name = entry
    import importlib

    module = importlib.import_module(module_path, package="fluid_build.build_runners")
    runner_fn = getattr(module, function_name)
    return runner_fn(
        build,
        contract,
        contract_dir,
        dry_run=dry_run,
        sample_rows=sample_rows,
    )


def _execute_embedded_sql_build_snowflake(
    build: Dict[str, Any],
    contract: Dict[str, Any],
) -> int:
    """Execute an embedded-SQL build's inline SQL on Snowflake.

    The connection is resolved by the same precedence chain every other
    Snowflake surface uses (:func:`get_connection_params`): explicit
    argument → contract binding / ``builds[].execution.runtime.resources``
    → environment / credential adapter. So a build that declares

    .. code-block:: yaml

        execution:
          runtime:
            platform: snowflake
            resources: {warehouse: WH, database: DB, schema: SC, role: R}

    runs against exactly that warehouse context.

    Returns 0 on success, 1 on failure.
    """
    props = build.get("properties") or {}
    sql = (props.get("sql") or "").strip()
    if not sql:
        cprint("   ❌ Build declares platform 'snowflake' but carries no properties.sql")
        return 1

    from fluid_build.providers.snowflake.connection import SnowflakeConnection
    from fluid_build.providers.snowflake.util.config import get_connection_params

    # ``schema=None`` is deliberate: ``get_connection_params`` defaults the
    # parameter to ``PUBLIC``, and an explicit argument outranks the
    # contract in ``resolve_snowflake_settings`` — passing the default
    # through would silently override the build's declared schema.
    params = get_connection_params(contract=contract, schema=None)
    resources = ((build.get("execution") or {}).get("runtime") or {}).get("resources") or {}
    for key in ("warehouse", "database", "schema", "role"):
        value = resources.get(key)
        if value:
            params[key] = value

    cprint(
        f"   target: {params.get('database') or '?'}.{params.get('schema') or '?'} "
        f"(warehouse={params.get('warehouse') or '?'}, role={params.get('role') or 'default'})"
    )
    with SnowflakeConnection(**params) as conn:
        conn.executescript(sql)
    return 0


def _print_typed_error(exc: Any) -> None:
    """A ``FluidUserError`` as the build output shows it: what, why, fix."""
    cprint(f"   ❌ {exc.what}", markup=False)
    cprint(f"      why: {exc.why}", markup=False)
    cprint(f"      fix: {exc.fix}", markup=False)


def _print_embedded_sql_io(io: Any) -> None:
    """The consumes[] bindings and the landing, printed before the SQL runs.

    This path writes no run record and emits no lineage event, so the build
    output (and the provider's ``runtime/out/local_apply_log.jsonl``, which
    records each input's productId/exposeId/uri) is where the resolved
    lineage is kept.
    """
    for c in io.covered:
        cprint(
            f"   ⬅ consumes {c.product_id}/{c.expose_id}: explicit input "
            f"'{c.input_name}' wins (properties.parameters.inputs)",
            markup=False,
        )
    for lin in io.lineage_only:
        cprint(
            f"   ⬅ consumes {lin.product_id}/{lin.expose_id}: lineage only, the SQL reads "
            f"no relation named {lin.expose_id!r}, so it is not resolved",
            markup=False,
        )
    for r in io.inputs:
        cprint(
            f'   ⬅ consumes {r.product_id}/{r.expose_id} as view "{r.view}": {r.uri}',
            markup=False,
        )
    if io.inputs and io.bigquery_landing is None:
        cprint(
            "     (no run record or lineage event on this path: the resolved inputs are "
            "listed here and in runtime/out/local_apply_log.jsonl)",
            markup=False,
        )
    if io.landing is not None:
        cprint(f"   ➡ lands {io.landing.uri}", markup=False)
    if io.bigquery_landing is not None:
        cprint(
            f"   ➡ lands BigQuery table {io.bigquery_landing.table_id} "
            f"({io.bigquery_landing.location}): staged as Parquet, then one load job "
            "(WRITE_TRUNCATE_DATA), recorded in the build's run record",
            markup=False,
        )
    for warning in getattr(io, "warnings", None) or []:
        cprint(f"   ⚠️  {warning}", markup=False)
        LOG.warning("embedded_sql_plan_warning %s", warning)


def _print_action_errors(results: List[Dict[str, Any]], io: Any) -> None:
    """Why each failed provider action failed, and, with resolved inputs, what to check.

    The provider only logs ``local_apply_action_error``; without this the build
    output said "1 action(s) failed" and nothing else. An upstream that has not
    landed yet reads as a missing file (or, in S3, a glob matching nothing).
    """
    from fluid_build.observability.secret_redactor import redact_secret_text

    for r in results:
        if r.get("status") == "error" and r.get("error"):
            # Redacted: an error can quote a value the SQL was templated with.
            cprint(f"      {redact_secret_text(str(r['error']))}", markup=False)
    if io is not None and io.inputs:
        products = ", ".join(sorted({r.product_id for r in io.inputs}))
        cprint(
            f"      (if a consumes input above does not exist, its upstream has not landed "
            f"there yet: build {products} first, with the same --env)",
            markup=False,
        )


def _bind_embedded_sql_io(actions: List[Dict[str, Any]], io: Any) -> None:
    """Give the provider's SQL action the resolved inputs and the landing.

    Explicit ``parameters.inputs`` come first and keep their names; a consumes
    entry they cover was never resolved, so no two inputs share a view.
    """
    for action in actions:
        if (action.get("op") or "").lower() not in {"sql", "query", "execute_sql"}:
            continue
        action["inputs"] = [
            *(action.get("inputs") or []),
            *(r.as_input_spec() for r in io.inputs),
        ]
        if io.landing is not None:
            action["outputs"] = [io.landing.as_output_spec()]
        if io.bigquery_landing is not None:
            # The staged file the load reads, never the binding's own
            # location.path (a gs:// staging prefix is not a local file).
            if io.bigquery_landing.staged is None:
                raise RuntimeError("the BigQuery landing was not staged before the SQL ran")
            action["outputs"] = [{"path": io.bigquery_landing.staged, "format": "parquet"}]


def _local_sql_actions(
    provider: Any, contract: Dict[str, Any], build: Dict[str, Any], io: Any
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """``(mini_contract, actions)`` the local provider runs for this one build.

    The build is wrapped as a mini-contract so ``_derive_actions_from_contract``
    finds its inputs and outputs. With ``io`` (the DuckDB inline-SQL path),
    every consumes entry the SQL reads was resolved or covered by an explicit
    input; only the lineage-only entries (the SQL reads no relation of their
    name) reach the provider, which warns about them as it always did
    (``local_consumes_not_bound``). Its SQL action is bound to the resolved
    views and the landing.
    """
    consumes = (
        [c.as_consume() for c in io.lineage_only]
        if io is not None
        else contract.get("consumes", [])
    )
    mini_contract = {
        "id": contract.get("id", "product"),
        "builds": [build],
        "consumes": consumes,
        "exposes": contract.get("exposes", []),
    }
    actions = provider._derive_actions_from_contract(mini_contract)
    if io is not None:
        _bind_embedded_sql_io(actions, io)
    return mini_contract, actions


def _plan_duckdb_io(
    contract: Dict[str, Any],
    build: Dict[str, Any],
    contract_dir: Path,
    *,
    env: Optional[str],
) -> Any:
    """What a DuckDB inline-SQL build reads and lands, printed; ``None`` if refused.

    ``contract`` is the one ``{{ env.* }}`` templates are still in (see
    ``_embedded_sql_io.object_store_landing``). A refusal is printed as the
    typed error's what / why / fix, and the build must then return 1 before
    anything runs.
    """
    from fluid_build._errors import FluidUserError

    from ._embedded_sql_io import plan_embedded_sql_io

    build_id = build.get("id", "unknown")
    try:
        io = plan_embedded_sql_io(contract, build, contract_dir, env=env, logger=LOG)
    except FluidUserError as exc:
        _print_typed_error(exc)
        LOG.error("embedded_sql_io_refused build_id=%s code=%s", build_id, exc.code)
        return None
    _print_embedded_sql_io(io)
    if io.inputs:
        LOG.debug(
            "embedded_sql_consumes_resolved build_id=%s inputs=%s",
            build_id,
            json.dumps([r.record() for r in io.inputs]),
        )
    return io


def _register_placeholder_secrets(*values: Any) -> None:
    """Register the value of every credential-named ``{{ env.X }}`` in ``values``.

    The runtime path resolves every placeholder, secrets included, into the
    SQL the provider runs, and an engine error can quote that SQL back
    (``CAST('{{ env.PARTNER_API_TOKEN }}' AS INTEGER)`` fails naming the
    value). Registered, the value is masked by exact match wherever the shared
    redactor runs: the build output, the provider's apply log, and every log
    line the CLI's redacting filter sees. Names follow the redactor's own
    predicate (``is_sensitive_key_name``).
    """
    from fluid_build.observability.secret_redactor import is_sensitive_key_name, register_secret

    names: Set[str] = set()

    def _walk(value: Any) -> None:
        if isinstance(value, str):
            names.update(m.group(1) for m in ENV_PLACEHOLDER_RE.finditer(value))
        elif isinstance(value, list):
            for item in value:
                _walk(item)
        elif isinstance(value, dict):
            for item in value.values():
                _walk(item)

    for value in values:
        _walk(value)
    for name in sorted(names):
        if is_sensitive_key_name(name) and os.environ.get(name):
            register_secret(os.environ[name])


def _redacted(exc: BaseException) -> str:
    from fluid_build.observability.secret_redactor import redact_secret_text

    return redact_secret_text(str(exc))


def _execute_embedded_sql_build(
    build: Dict[str, Any],
    contract: Dict[str, Any],
    contract_dir: Path,
    dry_run: bool = False,
    *,
    env: Optional[str] = None,
) -> int:
    """Execute an embedded-SQL build on the runtime platform it declares.

    Embedded-SQL builds (``engine: sql`` / ``pattern: embedded-logic`` +
    ``properties.sql``) carry their transformation inline and do not ship a
    Python driver script, so the python runner is the wrong dispatch.

    ``execution.runtime.platform`` selects the executor:

    * ``local`` / ``duckdb`` / unset — the local provider's DuckDB engine
      (the historical behaviour, unchanged).
    * ``snowflake`` — the declared warehouse, via
      :func:`_execute_embedded_sql_build_snowflake`.
    * anything else — a hard error. This branch used to fall through to
      DuckDB, so a contract declaring ``platform: snowflake`` had its
      warehouse/database/schema/role accepted and silently discarded: the
      SQL ran against an in-process DuckDB, the rows landed in a local CSV,
      and the declared warehouse was never touched. Refusing beats
      downgrading a declared platform without telling anyone.

    ``{{ env.X }}`` placeholders in the build are resolved before any engine
    sees the SQL — the plan.json input shape resolved them (via
    ``resolve_contract_env_templates`` in :func:`run_builds_from_args`) and
    the YAML shape did not, so the same contract shipped a literal
    ``{{ env.SNOWFLAKE_DATABASE }}`` to the executor depending on which
    documented input the operator used. Resolving here makes both shapes
    identical, and mirrors what ``_execute_acquisition_build`` already does
    on the runtime path.

    On the DuckDB engine, ``consumes[]`` is resolved to the relations the
    upstream products land (``_embedded_sql_io.resolve_consumes``): each entry
    the SQL reads becomes a view named by its ``exposeId``, read from the
    upstream's binding under the same ``env`` overlay this run uses. An entry
    the SQL does not read (no relation of that name) is lineage only, as it
    was before, and is not resolved. An explicit
    ``properties.parameters.inputs`` entry with the same name WINS on the
    collision, and the entry is then not resolved at all. An entry the SQL
    reads that is neither resolved nor covered fails the build before the SQL
    runs, and so does one naming this contract's own id. The
    result lands in S3 when the first expose is an AWS object-store binding,
    at the object the duckdb acquisition runner would write for it; a local
    binding is unchanged. An entry resolving to a GCP ``bigquery_table`` is
    read through the BigQuery API into a staged Parquet file first, and a
    first expose bound to one is loaded into that table by one load job after
    the SQL (``_embedded_sql_io.stage_bigquery_io`` /
    ``load_bigquery_landing``); a failed or short load fails the build. Any
    other landing this path cannot write (a ``gs://`` path, a GCS bucket,
    another warehouse) is refused before the SQL runs. An expose declaring
    ``policy.privacy.masking`` is refused on this path, which does not apply
    it.

    Returns 0 on success, 1 on failure.
    """
    import time

    build_id = build.get("id", "unknown")
    # Kept before resolution: the S3 landing refuses a bucket or path whose
    # ``{{ env.X }}`` is unset instead of resolving it to "" (a local write).
    unresolved_contract = contract
    # Runtime path: resolve every placeholder, secrets included — the value
    # stays in process memory and is never serialised to a catalog. See the
    # secrets note in ``_execute_acquisition_build``. Credential-named ones are
    # registered with the redactor first, so an error quoting one is masked.
    _register_placeholder_secrets(build, contract)
    build = _resolve_env_placeholders(build)
    contract = _resolve_env_placeholders(contract)
    platform = embedded_sql_platform(build)

    cprint(f"\n{'─' * 60}")
    engine_label = "local DuckDB" if platform in LOCAL_SQL_PLATFORMS else platform
    cprint(f"🔷 Build '{build_id}' (embedded-SQL / {engine_label})")

    io = None
    if platform in LOCAL_SQL_PLATFORMS:
        planned, io = _plan_local_sql(unresolved_contract, build, contract_dir, env=env)
        if not planned:
            return 1

    if dry_run:
        props = build.get("properties") or {}
        sql_preview = (props.get("sql") or "").strip()[:200]
        cprint(f"   [DRY RUN] Would execute SQL:\n{sql_preview}")
        return 0

    if platform not in LOCAL_SQL_PLATFORMS:
        if platform != "snowflake":
            cprint(
                f"   ❌ Build '{build_id}' declares "
                f"execution.runtime.platform: '{platform}', which has no "
                "embedded-SQL executor. forge-cli will not downgrade a declared "
                "platform to the local DuckDB engine. Supported: "
                f"{', '.join(sorted(LOCAL_SQL_PLATFORMS - {''}))}, snowflake."
            )
            LOG.error(
                "embedded_sql_platform_unsupported build_id=%s platform=%s",
                build_id,
                platform,
            )
            return 1
        try:
            t0 = time.time()
            rc = _execute_embedded_sql_build_snowflake(build, contract)
            if rc == 0:
                cprint(f"   ✅ Completed in {round(time.time() - t0, 2)}s on Snowflake")
            return rc
        except Exception as exc:
            cprint(f"   ❌ Embedded-SQL build '{build_id}' error: {_redacted(exc)}")
            LOG.exception("embedded_sql_build_error build_id=%s", build_id)
            return 1

    return _run_local_sql(build, contract, contract_dir, io)


def _plan_local_sql(
    unresolved_contract: Dict[str, Any],
    build: Dict[str, Any],
    contract_dir: Path,
    *,
    env: Optional[str],
) -> Tuple[bool, Any]:
    """``(planned, io)`` for a build on the local DuckDB engine; ``planned`` False if refused.

    Only a build carrying inline SQL reads the views (``io``): one without it
    (a multi-stage ``engine: sql`` build) keeps the provider's old handling,
    except that a first expose it would write as a local file of the wrong
    kind (a BigQuery table, a ``gs://`` path) is refused.
    """
    has_inline_sql = bool(str((build.get("properties") or {}).get("sql") or "").strip())
    if has_inline_sql:
        io = _plan_duckdb_io(unresolved_contract, build, contract_dir, env=env)
        return io is not None, io
    from fluid_build._errors import FluidUserError

    from ._embedded_sql_io import refuse_unlandable_first_expose

    try:
        refuse_unlandable_first_expose(unresolved_contract)
    except FluidUserError as exc:
        _print_typed_error(exc)
        LOG.error("embedded_sql_io_refused build_id=%s code=%s", build.get("id"), exc.code)
        return False, None
    return True, None


def _run_local_sql(
    build: Dict[str, Any], contract: Dict[str, Any], contract_dir: Path, io: Any
) -> int:
    """Run the build on the local provider's DuckDB; 0 on success, 1 on failure.

    A BigQuery upstream is staged first and its copy removed afterwards; a
    BigQuery landing is loaded after the SQL, and a failed load fails the build.
    """
    import time

    from ._acquisition_common import utc_now_iso

    build_id = build.get("id", "unknown")
    staged_inputs: List[Path] = []
    # A BigQuery landing is recorded as a run, as the acquisition load is, so
    # ``fluid verify`` holds the table to the rows it landed.
    planned_landing = io.bigquery_landing if io is not None else None
    outcome: Dict[str, Any] = {}
    started_at = utc_now_iso()
    try:
        from fluid_build.providers.local.local import LocalProvider

        if io is not None and (io.bigquery_inputs or io.bigquery_landing is not None):
            io, staged_inputs = _stage_bigquery(io, contract_dir, build_id)

        # ``anchor_dir``: a relative ``location.path`` lands under the
        # source contract's directory, the same place the acquisition
        # runners write and ``fluid verify`` reads (it used to land under
        # whatever directory ``fluid apply`` was launched from).
        provider = LocalProvider(project="local", region="local", anchor_dir=contract_dir)
        mini_contract, actions = _local_sql_actions(provider, contract, build, io)
        t0 = time.time()
        result = provider.apply(actions=actions, plan={"contract": mini_contract})
        elapsed = round(time.time() - t0, 2)

        applied = result.get("applied", 0)
        failed = result.get("failed", 0)
        if failed == 0:
            cprint(f"   ✅ Completed in {elapsed}s — {applied} action(s) executed")
            written_files = []
            for r in result.get("results") or []:
                if r.get("status") == "ok":
                    written_files.extend(r.get("written", []))
            for p in written_files:
                cprint(f"   📁 {p}")
            if io is not None and io.bigquery_landing is not None:
                return _load_bigquery_result(io.bigquery_landing, build_id, outcome)
            return 0
        else:
            cprint(f"   ❌ Failed: {failed} action(s) failed")
            _print_action_errors(result.get("results") or [], io)
            outcome["error"] = f"{failed} action(s) failed"
            return 1
    except Exception as exc:
        cprint(f"   ❌ Embedded-SQL build '{build_id}' error: {_redacted(exc)}")
        LOG.exception("embedded_sql_build_error build_id=%s", build_id)
        outcome["error"] = _redacted(exc)
        return 1
    finally:
        if staged_inputs:
            from ._embedded_sql_io import remove_staged

            remove_staged(staged_inputs)
        if planned_landing is not None:
            _record_bigquery_run(
                build, contract, contract_dir, planned_landing, started_at, outcome
            )


def _record_bigquery_run(
    build: Dict[str, Any],
    contract: Dict[str, Any],
    contract_dir: Path,
    landing: Any,
    started_at: str,
    outcome: Dict[str, Any],
) -> None:
    """Write the run record of a BigQuery-landing build; never changes the build's result."""
    from ._embedded_sql_io import write_bigquery_run_record

    facts = outcome.get("facts")
    error = outcome.get("error") or (None if facts else "the build did not reach the load")
    try:
        run_id = write_bigquery_run_record(
            contract, build, contract_dir, landing, started_at=started_at, facts=facts, error=error
        )
    except Exception as exc:  # noqa: BLE001 - reported; the load's outcome stands
        cprint(f"   ⚠️  the run record could not be written: {_redacted(exc)}", markup=False)
        LOG.warning("embedded_sql_run_record_failed error=%s", type(exc).__name__)
        return
    if run_id is not None:
        LOG.info("embedded_sql_run_recorded build_id=%s run_id=%s", build.get("id"), run_id)


def _stage_bigquery(io: Any, contract_dir: Path, build_id: Any) -> Tuple[Any, List[Path]]:
    """Read every BigQuery upstream into a staged Parquet file, printing each read."""
    from ._embedded_sql_io import stage_bigquery_io

    io, staged, reads = stage_bigquery_io(io, contract_dir, build_id, logger=LOG)
    for read in reads:
        cprint(
            f'   ⬇ read {int(read["rows"]):,} row(s) from BigQuery table {read["table"]} '
            f'for view "{read["view"]}"',
            markup=False,
        )
    return io, staged


def _load_bigquery_result(landing: Any, build_id: Any, outcome: Dict[str, Any]) -> int:
    """Load the staged result into its BigQuery table; 0 only when the rows arrived.

    ``outcome`` gets the load's facts, or its error, for the run record.
    """
    from ._embedded_sql_io import load_bigquery_landing

    try:
        facts = load_bigquery_landing(landing, logger=LOG)
    except Exception as exc:  # noqa: BLE001 - a failed or short load fails the build
        cprint(f"   ❌ BigQuery load into {landing.table_id} failed: {_redacted(exc)}")
        LOG.error("embedded_sql_bigquery_load_failed build_id=%s", build_id)
        outcome["error"] = _redacted(exc)
        return 1
    outcome["facts"] = facts
    cprint(
        f"   ⬆ loaded {int(facts['rows']):,} row(s) into BigQuery table {facts['table']} "
        f"(job {facts.get('job_id')}, rows from {facts.get('rows_from')})",
        markup=False,
    )
    return 0


def _manifest_env(path: Any) -> Optional[str]:
    """The env a bundle's MANIFEST records (``fluid bundle --env``), or ``None``."""
    if not path or not str(path).lower().endswith((".tgz", ".tar.gz")):
        return None
    try:
        from fluid_build.forge.core.bundle import read_bundle_source

        source = read_bundle_source(Path(str(path)))
    except Exception:  # noqa: BLE001 - an unreadable bundle is reported elsewhere
        return None
    env = source.get("env") if source else None
    return str(env) if env else None


def _plan_env(plan_data: Optional[Dict[str, Any]]) -> Optional[str]:
    """The overlay env a plan's contract was loaded with, as the plan records it.

    ``contract_metadata.env``, which ``fluid plan`` writes under planDigest. A
    plan written before that key existed but made from a bundle links the
    bundle by ``contract_metadata.source_path``: its MANIFEST records the env.
    """
    meta = plan_data.get("contract_metadata") if isinstance(plan_data, dict) else None
    if not isinstance(meta, dict):
        return None
    env = meta.get("env")
    if isinstance(env, str) and env:
        return env
    return _manifest_env(meta.get("source_path"))


def _run_env(args: argparse.Namespace, plan_data: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """The overlay env this run loaded its contract with.

    ``--env`` when given. Otherwise, for a ``plan.json`` (``plan_data``), the
    env the plan records (:func:`_plan_env`): the contract embedded in a plan
    was overlaid when it was planned, so ``fluid apply plan.json`` without
    ``--env`` runs in that env. For a bundle input (or ``--bundle``), the env
    its MANIFEST records, because a bundle carries its overlay already applied
    and is never re-overlaid.

    Raises ``CLIError(plan_env_mismatch)`` when ``--env`` disagrees with the env
    the plan records: the plan's contract carries the recorded env's bindings,
    so reading the upstreams with another env would mix two targets.
    """
    requested = getattr(args, "env", None) or None
    recorded = _plan_env(plan_data) if plan_data is not None else None
    if requested and recorded and str(requested) != recorded:
        raise CLIError(
            1,
            "plan_env_mismatch",
            {
                "plan": str(getattr(args, "contract", "")),
                "plan_env": recorded,
                "requested_env": str(requested),
                "hint": (
                    f"the plan was made with --env {recorded} and carries that overlay; "
                    f"apply it with --env {recorded} (or without --env), or re-plan with "
                    f"--env {requested}."
                ),
            },
        )
    if requested:
        return str(requested)
    if recorded:
        return recorded
    for candidate in (getattr(args, "contract", None), getattr(args, "bundle", None)):
        env = _manifest_env(candidate)
        if env:
            return env
    return None


def _runs_dir(contract_dir: Path, product_id: str, build_id: str) -> Optional[Path]:
    """Where a build's run records are (``FileStateStore``), for ids the store accepts."""
    from ._ids import IdentifierViolation, validate_identifier

    try:
        validate_identifier(product_id, kind="contract.id")
        validate_identifier(build_id, kind="build.id")
    except IdentifierViolation:
        return None
    return contract_dir / ".fluid" / "runs" / product_id / build_id / "runs"


def _run_ids(contract_dir: Path, product_id: str, build_id: str) -> Set[str]:
    """The run ids a build has recorded so far."""
    runs = _runs_dir(contract_dir, product_id, build_id)
    try:
        return {p.stem for p in runs.glob("*.json")} if runs and runs.is_dir() else set()
    except OSError:
        return set()


def _report_build(
    report: Any,
    contract_dir: Path,
    product_id: str,
    build_id: str,
    result: int,
    runs_before: Optional[Set[str]],
) -> None:
    """Record one build on ``report``, with the newest run record it wrote, if any.

    Run ids sort by time (``generate_run_id``), so the newest id this build
    added is its run. Nothing is read when no ``fluid apply`` report is open.
    """
    if report is None:
        return
    run: Optional[Dict[str, Any]] = None
    runs = _runs_dir(contract_dir, product_id, build_id)
    added = sorted(_run_ids(contract_dir, product_id, build_id) - (runs_before or set()))
    if runs is not None and added:
        try:
            loaded = json.loads((runs / f"{added[-1]}.json").read_text(encoding="utf-8"))
            run = loaded if isinstance(loaded, dict) else None
        except (OSError, ValueError):
            run = None
    report.record_build(build_id=build_id, status="succeeded" if result == 0 else "failed", run=run)


def run_builds_from_args(
    args: argparse.Namespace,
    logger: logging.Logger,
    *,
    force_run: bool = False,
    plan_data: Optional[Dict[str, Any]] = None,
) -> int:
    """Execute builds from a FLUID contract.

    Loads the contract, filters by ``args.build_id`` if present, and
    dispatches each build to the dbt or python engine via
    :func:`is_dbt_build`. Returns 0 if no build failed, 1 otherwise.

    ``force_run=True`` (the default when called from ``fluid apply --build``)
    forces scheduled builds to run once — normally the scheduler owns the
    run, but apply may legitimately kick off a one-shot refresh.

    ``plan_data`` is the plan ``fluid apply`` already digest-verified and
    mode-checked when ``args.contract`` is a ``plan.json``. When given it is
    used as-is and the file is NOT re-read, so the builds that run are the
    ones the plan-binding gate attested.

    Every input shape anchors at the SOURCE contract's directory (relative
    ``binding.location.path``, dbt ``repository``, the ``.fluid`` state
    root): the contract itself, the contract a bundle's MANIFEST records, or
    the contract a plan records (through its bundle when it was planned from
    one). See :func:`fluid_build._contract_loader.source_contract_path`.

    Each build is recorded on the running ``fluid apply``'s Command Center
    report, when there is one (``observability.apply_run``): its status and
    the run record it wrote, and why the build phase failed.
    """
    # Deferred imports to avoid circular import at module-load time:
    # base.py -> python.runner -> base.py (for _resolve_env_placeholders).
    from .dbt.runner import execute_dbt_build, resolve_dbt_project_path
    from .python.runner import execute_build, resolve_script_path

    global LOG
    LOG = logger

    contract_path = Path(args.contract)

    if not contract_path.exists():
        raise CLIError(1, "contract_not_found", {"path": str(contract_path)})

    # Two input shapes are supported:
    #   1. ``<contract>.yaml`` — load via the standard FLUID loader
    #      (handles overlays, $ref bundling, alias normalization).
    #   2. ``<plan>.json`` — the build runner is being invoked from
    #      ``fluid apply <plan>.json --mode amend-and-build``. The plan
    #      embeds the FULL FLUID contract under ``plan["contract"]``
    #      (with ``builds[]`` intact) so we extract it directly. Without
    #      this branch the standard "load contract from path" path would
    #      treat plan.json as a contract, find no ``builds`` key, and
    #      log "No builds defined in contract" — leaving acquisition /
    #      hybrid-reference dbt builds as silent no-ops on the canonical
    #      stage-7 path the lab Taskfile uses.
    if str(contract_path).endswith(".json"):
        LOG.info(f"Loading contract from execution plan: {contract_path}")
        if plan_data is None:
            try:
                plan_data = json.loads(contract_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise CLIError(
                    1, "contract_load_failed", {"path": str(contract_path), "error": str(exc)}
                )
        contract = plan_data.get("contract") or {}
        if not contract:
            raise CLIError(
                1,
                "plan_missing_contract",
                {
                    "path": str(contract_path),
                    "hint": (
                        "plan.json has no embedded ``contract`` key. "
                        "Re-run ``fluid plan <contract>.yaml --out <plan>.json`` "
                        "with a current forge-cli; older plan generators omit it."
                    ),
                },
            )
        # Re-anchor ``contract_path`` at the original source file so build
        # runners can resolve contract-relative paths (e.g.
        # ``repository: ../../reference-assets/dbt_dv2_subscriber360``).
        # Without this, ``contract_path.parent`` below would point at
        # ``runtime/`` (where plan.json lives) and dbt project lookups
        # would resolve wrong. The source path is recorded by ``fluid
        # plan`` in ``contract_metadata.source_path``.
        # A plan made from a bundle records the BUNDLE as its source_path;
        # ``source_contract_path`` follows it to the contract the bundle's
        # MANIFEST records, so a bundle-planned build lands its data where a
        # contract-planned one does (it used to anchor at ``runtime/``).
        from fluid_build._contract_loader import resolve_source_contract

        source_path_str = (plan_data.get("contract_metadata") or {}).get("source_path")
        if source_path_str:
            source_path, why = resolve_source_contract(contract_path, plan_data=plan_data)
            if source_path is not None:
                LOG.info(f"Anchoring builds at source contract dir: {source_path.parent}")
                contract_path = source_path
            else:
                LOG.warning(
                    "source_contract_unresolved: plan %s %s; anchoring at plan dir %s "
                    "(relative paths in builds may not resolve)",
                    contract_path,
                    why,
                    contract_path.parent,
                )
        else:
            LOG.warning(
                "plan.json has no contract_metadata.source_path; anchoring at plan "
                "dir %s. Relative paths in builds (e.g. dbt repository) may not "
                "resolve. Re-run ``fluid plan`` with a current forge-cli to embed "
                "the source path.",
                contract_path.parent,
            )
        # Mirror the YAML loader's env-template + alias normalization that
        # ``load_contract_with_overlay`` would have done. Plan-embedded
        # contracts are still authored with ``{{ env.X }}`` placeholders;
        # the build runner needs them resolved before passing to engine
        # SDKs (dlt destination spec, dbt vars, target-snowflake config).
        # Apply the same secret-aware resolver the publish path uses so we
        # don't accidentally exfiltrate secret-named placeholders.
        try:
            from fluid_build._contract_loader import resolve_contract_env_templates

            contract = resolve_contract_env_templates(contract)
        except Exception as exc:  # noqa: BLE001 — defensive
            LOG.debug("env template resolution failed (non-fatal): %s", exc)
    else:
        # Standard YAML contract path (or a bundle).
        LOG.info(f"Loading contract: {contract_path}")
        try:
            contract = load_contract_with_overlay(
                str(contract_path), getattr(args, "env", None), logger
            )
        except CLIError:
            raise
        except Exception as e:
            raise CLIError(1, "contract_load_failed", {"path": str(contract_path), "error": str(e)})
        if str(contract_path).lower().endswith((".tgz", ".tar.gz")):
            # A bundle's own directory is not where its contract lives:
            # anchor at the source contract its MANIFEST records.
            from fluid_build._contract_loader import resolve_source_contract

            bundle_source, why = resolve_source_contract(contract_path)
            if bundle_source is not None:
                LOG.info(f"Anchoring builds at source contract dir: {bundle_source.parent}")
                contract_path = bundle_source
            else:
                LOG.warning(
                    "source_contract_unresolved: bundle %s %s; anchoring builds at the "
                    "bundle's directory %s (relative binding paths will not land where "
                    "the contract's author meant)",
                    contract_path,
                    why,
                    contract_path.resolve().parent,
                )

    builds = contract.get("builds", [])

    if not builds:
        LOG.warning("No builds defined in contract")
        return 0

    # ── Identifier guard (fail CLOSED) ───────────────────────────────────
    # This is the single runtime chokepoint for ``fluid apply --mode
    # amend-and-build``, which loads a contract WITHOUT jsonschema
    # validation. ``contract['id']`` and each ``build['id']`` flow into
    # ``RunContext.product_id`` / ``build_id`` (``_acquisition_common``)
    # and then into ``FileStateStore._build_dir`` (``_state``), which
    # joins them as ``<root>/runs/<product_id>/<build_id>`` with
    # ``parents=True`` and (historically) no sanitisation — so an
    # ``id`` like ``../../../../tmp/escape`` would write JSON OUTSIDE the
    # workspace. Validate every id here, BEFORE any runner runs and BEFORE
    # any state-store path is created, so a malicious contract is rejected
    # rather than traversing the filesystem. The parallel pipeline
    # (``cli/_acquisition_stage_ext``) already validates the same fields;
    # this closes the one-sided-guard gap on the runtime path.
    from ._ids import validate_identifier

    # Only non-empty ids are checked: an absent/empty id cannot traverse
    # (``_build_dir`` falls back to a safe "product" default and
    # ``FileStateStore._confine()`` is the backstop), while a non-empty id
    # like ``../../../../tmp/escape`` is rejected here before any path is made.
    if contract.get("id"):
        validate_identifier(contract["id"], kind="contract.id")
    for _b in builds:
        if _b.get("id"):
            validate_identifier(_b["id"], kind="build.id")

    # The running ``fluid apply``'s run report, if any: each build is recorded on it.
    from fluid_build.observability.apply_run import current_apply_run

    report = current_apply_run()
    product_id = str(contract.get("id") or "")

    # Filter builds if specific ID requested
    if args.build_id:
        builds = [b for b in builds if b.get("id") == args.build_id]
        if not builds:
            LOG.error(f"Build not found: {args.build_id}")
            if report is not None:
                report.build_failed(f"build_not_found:{args.build_id}")
            return 1

    # The overlay env the contract above was loaded with, decided once and
    # before any build runs: a plan's recorded env that ``--env`` contradicts
    # is refused here, not after a first build has landed.
    run_env = _run_env(args, plan_data if str(args.contract).endswith(".json") else None)

    cprint(f"\n{'=' * 80}")
    cprint("🚀 FLUID Build Runner")
    cprint(f"{'=' * 80}")
    cprint(f"Contract: {contract_path}")
    cprint(f"Builds: {len(builds)}")
    if args.dry_run:
        cprint("Mode: DRY RUN")
    cprint(f"{'=' * 80}")

    total_executed = 0
    total_failed = 0
    total_skipped = 0

    for build in builds:
        build_id = build.get("id", "unknown")
        runs_before = _run_ids(contract_path.parent, product_id, build_id) if report else None

        if is_acquisition_build(build):
            sample_rows = getattr(args, "sample_rows", None)
            result = _execute_acquisition_build(
                build,
                contract,
                contract_path.parent,
                dry_run=args.dry_run,
                sample_rows=sample_rows,
            )
            _report_build(report, contract_path.parent, product_id, build_id, result, runs_before)
            if result == 0:
                total_executed += 1
            else:
                total_failed += 1
                if args.fail_fast:
                    break
            continue

        if is_embedded_sql_build(build):
            # An inline-SQL build that ALSO declares a dbt(-adapter) engine
            # still runs via the local DuckDB engine below — NOT dbt. Surface
            # that explicitly so ``engine: dbt`` is never silently ignored.
            if is_dbt_build(build):
                cprint(
                    f"\n⚠️  Build '{build_id}' sets engine "
                    f"'{build.get('engine')}' but carries inline SQL "
                    f"('properties.sql'); running it via the local DuckDB "
                    f"engine, not dbt. To run dbt, point the build at a dbt "
                    f"project (repository + dbt_project.yml) instead of inline "
                    f"SQL."
                )
            # Bug A4-A fix: embedded-SQL builds (engine: sql / pattern:
            # embedded-logic + properties.sql) carry their SQL inline and do
            # NOT have an external Python script.  They are executed via the
            # local provider's DuckDB engine, not the python runner.
            # Previously they fell through to the python-runner branch and
            # emitted the confusing "Script not found: ingest.py" warning.
            result = _execute_embedded_sql_build(
                build,
                contract,
                contract_path.parent,
                dry_run=args.dry_run,
                env=run_env,
            )
        elif is_dbt_build(build):
            project_dir = resolve_dbt_project_path(contract_path, build)
            if not project_dir:
                repository = build.get("repository", "./")
                expected = (contract_path.parent / repository / "dbt_project.yml").resolve()
                cprint(f"\n⚠️  Build '{build_id}' - dbt project not found: {expected}")
                total_skipped += 1
                if report is not None:
                    report.record_build(build_id=build_id, status="skipped")
                continue

            result = execute_dbt_build(
                build,
                project_dir,
                contract_path.parent,
                dry_run=args.dry_run,
                delay=args.delay,
                no_output=args.no_output,
                fail_fast=args.fail_fast,
                force_run=force_run,
                # Forward the apply mode so destructive modes (replace
                # / replace-and-build) append ``--full-refresh`` to dbt.
                apply_mode=getattr(args, "mode", None),
            )
        else:
            # Resolve script path
            script_path = resolve_script_path(contract_path, build)

            if not script_path:
                repository = build.get("repository", "./")
                properties = build.get("properties", {})
                model = properties.get("model", "ingest")
                expected = contract_path.parent / repository / f"{model}.py"

                cprint(
                    f"\n⚠️  Build '{build_id}' - Script not found: {expected}\n"
                    "   Hint: for inline-SQL builds use ``engine: sql`` (or "
                    "``pattern: embedded-logic`` + ``properties.sql``). "
                    "For Python builds, create the script at the expected path above."
                )
                total_skipped += 1
                if report is not None:
                    report.record_build(build_id=build_id, status="skipped")
                continue

            # Execute build
            result = execute_build(
                build,
                script_path,
                contract_path.parent,
                dry_run=args.dry_run,
                delay=args.delay,
                no_output=args.no_output,
                fail_fast=args.fail_fast,
                force_run=force_run,
            )

        _report_build(report, contract_path.parent, product_id, build_id, result, runs_before)
        if result == 0:
            total_executed += 1
        else:
            total_failed += 1
            if args.fail_fast:
                break

    # Final summary
    cprint(f"\n{'=' * 80}")
    cprint("📈 Overall Summary")
    cprint(f"{'=' * 80}")
    cprint(f"Total builds: {len(builds)}")
    success(f"Executed: {total_executed}")
    console_error(f"Failed: {total_failed}")
    cprint(f"⏭️  Skipped: {total_skipped}")
    cprint(f"{'=' * 80}\n")

    if total_failed:
        return 1

    # Green-on-nothing guard. A build-augmented apply whose every build was
    # skipped used to exit 0: the DDL landed, no transformation ran, no rows
    # were produced, and CI reported a successful data-product deployment
    # against an empty table. "Nothing ran" is not "success" — a missing dbt
    # project or driver script is a broken deployment, not a no-op.
    # ``--allow-skipped-builds`` is the explicit opt-in for contracts whose
    # build artifacts legitimately live outside the checkout.
    if total_skipped and not total_executed:
        if getattr(args, "allow_skipped_builds", False):
            LOG.warning(
                "build.all_skipped_allowed skipped=%d (--allow-skipped-builds)",
                total_skipped,
            )
            return 0
        if report is not None:
            report.build_failed("builds_all_skipped")
        console_error(
            f"Every build was skipped ({total_skipped}/{len(builds)}) — nothing "
            "was transformed and no rows were produced. Fix the missing build "
            "artifact(s) reported above, or pass --allow-skipped-builds if the "
            "skip is expected."
        )
        LOG.error("build.all_skipped skipped=%d total=%d", total_skipped, len(builds))
        return 1

    return 0
