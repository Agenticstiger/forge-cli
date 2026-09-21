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

"""
Dagster Pipeline Generation for Snowflake Provider.

Generates Python pipeline code from FLUID contracts for Dagster with Snowflake.
"""

import logging
from datetime import datetime
from datetime import timezone as dt_timezone
from graphlib import CycleError, TopologicalSorter
from typing import Any, Dict, List, Optional, Sequence

from fluid_build.providers.common.codegen_utils import (
    convert_schedule_to_cron,
    escape_for_docstring,
    json_literal,
    py_str_literal,
    task_identifier,
)

logger = logging.getLogger(__name__)

_DEFAULT_CRON = "0 2 * * *"

# The keywords ``convert_schedule_to_cron`` recognises. Kept only to decide
# whether a *warning* is owed — the conversion itself is never re-implemented
# here (the module-local ``_convert_schedule`` that used to do it silently
# turned ``@yearly`` / ``@annually`` / a padded ``" @daily "`` into the 2am
# default, rewriting user input with no trace).
_SCHEDULE_KEYWORDS = frozenset({"@hourly", "@daily", "@weekly", "@monthly", "@yearly", "@annually"})

# Action operations this generator knows how to execute against Snowflake.
# Dispatch is on the *trailing* segment of the action, because the segment
# count is not fixed: the synthesiser emits ``snowflake.sql.execute_sql`` and
# ``snowflake.task.execute`` (3 segments), while other producers emit 2 or 4.
# Reading a fixed ``action_parts[2]`` meant none of the actions this repo
# actually emits ever matched, so every op fell through to a log-only body and
# the job reported SUCCESS having run no SQL at all.
# Operations whose params carry SQL. ``execute``/``run`` are deliberately NOT
# here on their own: ``snowflake.task.execute`` names a Snowflake TASK, and
# treating it as SQL made the op run a bogus ``SELECT 1`` and report success --
# the same silent-false-success failure this module was fixed to remove. They
# are accepted only under a SQL-ish service segment (below).
_SQL_OPERATIONS = frozenset({"query", "run_query", "execute_sql"})
# service segments under which a bare ``execute`` / ``run`` does mean SQL
_SQL_SERVICES = frozenset({"sql", "snowflake", "query"})
_SQL_AMBIGUOUS_OPERATIONS = frozenset({"execute", "run"})

# Names the emitted module binds itself. Seeding the collision map with them
# stops a ``taskId`` of ``logger`` / ``snowflake_conn`` rebinding one, which
# would break the pipeline only at run time.
_EMITTED_MODULE_NAMES = frozenset(
    {
        "op",
        "job",
        "resource",
        "In",
        "Out",
        "Nothing",
        "EnvVar",
        "ScheduleDefinition",
        "connect",
        "json",
        "logging",
        "logger",
        "snowflake_conn",
    }
)


def _dep_arg(op_name: str) -> str:
    """Name of the ``ins`` key / keyword argument wiring an upstream op in.

    Both sides of the wiring — the ``ins={...}`` key on the module-level
    ``@op`` decorator and the ``dep_x=...`` keyword at the call site inside the
    job body — must be the same legal Python identifier, because Dagster turns
    that key into the op function's input name. ``op_name`` is always a value
    taken from the collision map, so two dependencies can no longer collapse
    onto one ``ins`` key either.
    """
    return f"dep_{op_name}"


def _unique_op_names(tasks: Sequence[Dict[str, Any]]) -> Dict[str, str]:
    """Map each raw ``taskId`` to an op name unique within this pipeline.

    ``task_identifier`` is deliberately not injective — ``make-bucket`` and
    ``make.bucket`` both become ``make_bucket`` — so two tasks differing only
    in punctuation emitted two ``def``s of the SAME name: the second shadowed
    the first, and the job then called the survivor twice while one declared
    task vanished with no error.

    Sanitize, then suffix on collision, exactly as
    ``providers/aws/codegen/airflow._unique_task_identifiers`` (#623/#630) does
    for the DAG path. ``<name>_result`` is reserved alongside ``<name>``
    because the job body binds both.
    """
    names: Dict[str, str] = {}
    used = set(_EMITTED_MODULE_NAMES)
    for task in tasks:
        raw = str(task.get("taskId") or "")
        if raw in names:
            # Exact duplicate raw ids cannot both be represented in this map.
            # ``validate_contract_for_export`` rejects duplicates upstream;
            # warn honestly if one reaches here anyway.
            logger.warning(
                "snowflake dagster: duplicate taskId %r — only one op is "
                "emitted; taskIds must be unique",
                raw,
            )
        base = task_identifier(raw)
        candidate, suffix = base, 2
        while candidate in used or f"{candidate}_result" in used:
            candidate = f"{base}_{suffix}"
            suffix += 1
        used.add(candidate)
        used.add(f"{candidate}_result")
        names[raw] = candidate
    return names


def _ordered_tasks(tasks: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return ``tasks`` in dependency order.

    The job body is ordinary Python executed at decoration time, so a call site
    cannot reference a result bound later in the function — a contract that
    declares the dependent task first produced an ``UnboundLocalError`` the
    moment Dagster loaded the code location. Contract order is not dependency
    order, so sort.

    Nodes are list indices rather than taskIds so a duplicated id cannot make
    two declared tasks share one node.
    """
    first_index: Dict[str, int] = {}
    for index, task in enumerate(tasks):
        first_index.setdefault(str(task.get("taskId") or ""), index)

    graph: Dict[int, set] = {}
    for index, task in enumerate(tasks):
        depends_on = task.get("dependsOn") or []
        predecessors = set()
        for dep in depends_on:
            dep_index = first_index.get(str(dep))
            if dep_index is not None and dep_index != index:
                predecessors.add(dep_index)
        graph[index] = predecessors

    try:
        order = list(TopologicalSorter(graph).static_order())
    except CycleError:  # pragma: no cover - detect_circular_dependencies rejects upstream
        logger.warning(
            "snowflake dagster: dependency cycle reached the emitter; "
            "falling back to declaration order"
        )
        order = list(range(len(tasks)))
    return [tasks[i] for i in order]


def _resolve_schedule(orchestration: Dict[str, Any]) -> str:
    """Read the schedule from either place a contract may carry it.

    ``orchestration.schedule`` was the only key read, so a contract setting
    ``dagConfig.schedule`` — the spelling the DAG-config block uses — was
    silently ignored and every pipeline got the 2am default.
    """
    raw = orchestration.get("schedule")
    if not raw:
        dag_config = orchestration.get("dagConfig")
        if isinstance(dag_config, dict):
            raw = dag_config.get("schedule")
    # A null YAML scalar reaches here as None; coerce at the boundary so the
    # failure downstream is a named contract problem, not a bare AttributeError.
    # NOT ``raw or ""``: a schedule of ``0``, ``False`` (or the YAML 1.1
    # spellings ``off``/``no``, which PyYAML parses to False) is falsy but
    # present, and collapsing it to "" skips the unrecognised-value warning
    # and silently applies the default.
    return ("" if raw is None else str(raw)).strip()


def _schedule_to_cron(schedule: str) -> str:
    """Convert to cron via the shared helper, warning before any rewrite."""
    text = str(schedule or "").strip()
    if not text:
        return _DEFAULT_CRON
    if text.lower() not in _SCHEDULE_KEYWORDS and text.count(" ") < 4:
        # convert_schedule_to_cron falls back to the 2am default here. Silently
        # replacing what the user wrote is how a contract asking for hourly ran
        # once a day; say so.
        logger.warning(
            "snowflake dagster: unrecognised schedule %r — falling back to %r; "
            "use a 5-field cron expression or one of %s",
            text,
            _DEFAULT_CRON,
            ", ".join(sorted(_SCHEDULE_KEYWORDS)),
        )
    return convert_schedule_to_cron(text)


def generate_dagster_pipeline(
    contract: Dict[str, Any], account: str, database: str, warehouse: str = "COMPUTE_WH"
) -> str:
    """Generate Dagster pipeline from FLUID contract."""
    orchestration = contract.get("orchestration", {})
    if not orchestration:
        raise ValueError("Contract missing orchestration section")

    tasks = orchestration.get("tasks", []) or []
    provider_tasks = [t for t in tasks if t.get("type") == "provider_action"]

    contract_id = str(contract.get("id") or "unknown")
    contract_name = str(contract.get("name") or contract_id)
    schedule = _resolve_schedule(orchestration)
    timezone = str(orchestration.get("timezone") or "UTC")

    # One collision map for the whole file: the ``def`` names, the ``ins`` keys
    # and the job body's call sites all resolve through it, so the defining and
    # referencing sides of every name come from the same lookup.
    op_names = _unique_op_names(provider_tasks)

    code = _generate_header(contract_id, contract_name, account, database, warehouse)
    code += "\n\n"
    code += _generate_imports()
    code += "\n\n"
    code += _generate_resources(account, database, warehouse)
    code += "\n\n"
    code += _generate_ops(provider_tasks, database, warehouse, op_names)
    code += "\n\n"
    code += _generate_job(contract_id, provider_tasks, schedule, timezone, op_names)

    return code


def _generate_header(
    contract_id: str, contract_name: str, account: str, database: str, warehouse: str
) -> str:
    # Every contract-derived field is escaped so it cannot terminate this
    # docstring: a name carrying a triple quote used to close it and leave the
    # rest of the value at module scope, which Dagster executes as soon as it
    # loads the code location. The triple-quote delimiters below are this
    # generator's own and stay — escape_for_docstring returns a bare escaped
    # string, not a literal, so nothing is written around the placeholders.
    return f'''"""
FLUID Generated Dagster Pipeline: {escape_for_docstring(contract_name)}

Contract ID: {escape_for_docstring(contract_id)}
Snowflake Account: {escape_for_docstring(account)}
Database: {escape_for_docstring(database)}
Warehouse: {escape_for_docstring(warehouse)}

Auto-generated by FLUID Forge
Generated: {datetime.now(dt_timezone.utc).replace(tzinfo=None).isoformat()}Z
"""'''


def _generate_imports() -> str:
    # ``json`` is needed by the unsupported-action op, which rebuilds its params
    # with json.loads rather than carrying a repr spliced into the source.
    # Credentials are read through Dagster's own ``EnvVar`` instead of ``os``:
    # keeping every ``os``/``subprocess`` import out of the emitted file is
    # what lets a reviewer (and the codegen-injection pin) treat the presence
    # of one as evidence that a contract value became code.
    #
    # ``snowflake.connector`` is deliberately NOT imported here. It is the
    # driver, needed only when a run opens a connection, and a module-level
    # import made the whole code location unloadable — ``dagster dev`` could
    # not even list the job — on any machine without the connector installed.
    # It is imported inside the resource instead (see _generate_resources).
    return """from dagster import op, job, resource, In, Out, Nothing, EnvVar, ScheduleDefinition
import json
import logging

logger = logging.getLogger(__name__)"""


def _generate_resources(account: str, database: str, warehouse: str) -> str:
    # py_str_literal returns a repr(), which brings its own quotes — the
    # hand-written ones that used to wrap these placeholders are gone. With
    # them, a database of ``DB"`` emitted an unterminated literal and an
    # account carrying a newline dedented out of the function body and reached
    # module scope, where Dagster runs it on import.
    return f'''# Snowflake Resources
@resource
def snowflake_conn(context):
    """Snowflake connection resource."""
    from snowflake.connector import connect

    return connect(
        account={py_str_literal(account)},
        user=EnvVar("SNOWFLAKE_USER").get_value(),
        password=EnvVar("SNOWFLAKE_PASSWORD").get_value(),
        database={py_str_literal(database)},
        warehouse={py_str_literal(warehouse)},
        schema="PUBLIC"
    )'''


def _generate_ops(
    tasks: Sequence[Dict[str, Any]],
    database: str,
    warehouse: str,
    op_names: Optional[Dict[str, str]] = None,
) -> str:
    if op_names is None:
        op_names = _unique_op_names(tasks)
    ops_code = "# Pipeline Ops\n"
    for task in tasks:
        ops_code += _generate_single_op(task, database, warehouse, op_names)
        ops_code += "\n\n"
    return ops_code.rstrip()


def _resolved_dependencies(
    task: Dict[str, Any], op_names: Dict[str, str]
) -> tuple[List[str], List[str]]:
    """Split ``dependsOn`` into op names that exist here, and skipped raw ids.

    Only ``provider_action`` tasks become ops, so a dependency naming any other
    task has no op to wire to. Synthesising a name for it emitted an undefined
    reference that killed the whole module at import; dropping it silently hid
    a real modelling error. Both the ``ins`` side and the call side read this
    one result, so they cannot disagree about which edges survive.
    """
    keep: List[str] = []
    skipped: List[str] = []
    for dep in task.get("dependsOn") or []:
        op_name = op_names.get(str(dep))
        if op_name is None:
            logger.warning(
                "snowflake dagster: task %r dependsOn %r, which is not a "
                "provider_action task in this pipeline — edge skipped",
                task.get("taskId"),
                dep,
            )
            skipped.append(str(dep))
            continue
        keep.append(op_name)
    return keep, skipped


def _generate_single_op(
    task: Dict[str, Any], database: str, warehouse: str, op_names: Dict[str, str]
) -> str:
    task_id = str(task.get("taskId") or "")
    action = str(task.get("action") or "")
    raw_params = task.get("params")
    params = raw_params if isinstance(raw_params, dict) else {}

    # The op's Python function name, via the shared collision map. The raw
    # taskId used to be spliced into a module-level ``def``, so an ordinary
    # ``load-raw`` was a SyntaxError and a newline in the id put contract text
    # at module scope — executed on import of the code location. _generate_job
    # reads the same map, so the definition and its call site cannot resolve to
    # different names.
    op_name = op_names.get(task_id) or task_identifier(task_id)

    dep_ops, skipped = _resolved_dependencies(task, op_names)
    if dep_ops:
        # py_str_literal supplies the key's quotes; _dep_arg guarantees the key
        # is the same identifier Dagster will bind as the input name. In(Nothing)
        # declares an ordering edge with no value, so the op function takes no
        # matching parameter — which is why ``context`` stays its only argument.
        dep_items = ", ".join(f"{py_str_literal(_dep_arg(d))}: In(Nothing)" for d in dep_ops)
        ins_def = f"ins={{{dep_items}}}, "
    else:
        ins_def = ""
    skip_notes = "".join(f"# dependency on non-pipeline task {d!r} skipped\n" for d in skipped)

    # Dispatch on the trailing segment: the action is not always three parts.
    # The segment before it is the service, which disambiguates a bare
    # ``execute`` (SQL under ``sql``/``snowflake``, a Snowflake TASK under
    # ``task``).
    _parts = action.split(".")
    operation = _parts[-1]
    service = _parts[-2] if len(_parts) >= 2 else ""

    is_sql_action = operation in _SQL_OPERATIONS or (
        operation in _SQL_AMBIGUOUS_OPERATIONS and service in _SQL_SERVICES
    )
    raw_sql = params.get("sql") or params.get("query")
    if is_sql_action and not raw_sql:
        # A SQL action carrying no SQL used to fall back to ``SELECT 1``: the
        # op ran a placeholder against the warehouse, logged a row count and
        # reported success, while the declared work never happened. That is
        # the same silent-false-success this module was fixed to remove, so it
        # falls through to the loud unhandled branch below instead.
        logger.warning(
            "snowflake dagster: action %r on task %r declares neither sql nor "
            "query — the emitted op raises NotImplementedError at run time",
            action,
            task_id,
        )

    if is_sql_action and raw_sql:
        sql = str(raw_sql)
        # py_str_literal brings its own quotes, so none are written around
        # the placeholder. It replaces a local backslash/double-quote
        # replace pair that escaped neither newlines nor triple quotes,
        # while the target was a single-line "..." literal: an ordinary
        # multi-line SELECT emitted an unterminated string, and a crafted
        # one closed it and ran. The log line below deliberately drops its
        # ``f`` prefix — an f prefix survives into the generated file,
        # where any brace that reaches it is evaluated at op run.
        return f'''{skip_notes}@op({ins_def}required_resource_keys={{"snowflake_conn"}})
def {op_name}(context):
    """Execute Snowflake query"""
    conn = context.resources.snowflake_conn
    cursor = conn.cursor()
    try:
        cursor.execute({py_str_literal(sql)})
        results = cursor.fetchall()
        context.log.info("Query executed: %s rows", len(results))
        return len(results)
    finally:
        cursor.close()'''

    # No handler. The op this used to emit logged the action and returned
    # ``True``, so a pipeline of unmapped actions reported SUCCESS having done
    # nothing at all — the worst possible outcome for a data pipeline. Raise
    # instead, naming the action. The raise is emitted into the op body rather
    # than thrown here at generation time so that one unmapped task cannot stop
    # a whole contract from being generated and reviewed — but the run fails.
    if not is_sql_action:
        logger.warning(
            "snowflake dagster: no handler for action %r on task %r — the emitted "
            "op raises NotImplementedError at run time",
            action,
            task_id,
        )
    # ``action`` lands twice: escaped for a docstring it would otherwise close,
    # and as a plain literal argument to a %s-style log call. That call is not
    # an f-string (a ``{`` in the action would become a live expression), and
    # ``params`` is rebuilt with json.loads instead of being repr'd into the
    # source — see json_literal.
    return f'''{skip_notes}@op({ins_def})
def {op_name}(context):
    """Unsupported action: {escape_for_docstring(action)}"""
    context.log.error(
        "No Snowflake handler for action: %s, Params: %s",
        {py_str_literal(action)},
        json.loads({json_literal(params)}),
    )
    raise NotImplementedError(
        "FLUID: no Snowflake handler for action " + {py_str_literal(action)}
    )'''


def _generate_job(
    contract_id: str,
    tasks: Sequence[Dict[str, Any]],
    schedule: str,
    timezone: str,
    op_names: Optional[Dict[str, str]] = None,
) -> str:
    if op_names is None:
        op_names = _unique_op_names(tasks)

    op_calls = []
    for task in _ordered_tasks(tasks):
        # Dagster invokes this job body at decoration time to build the graph,
        # so every name here executes at import. Same collision map as
        # _generate_single_op: the name an op is defined under and the name
        # referenced here have to come from the same lookup, or the generated
        # job calls something that does not exist.
        op_name = op_names.get(str(task.get("taskId") or "")) or task_identifier(task.get("taskId"))
        dep_ops, skipped = _resolved_dependencies(task, op_names)
        for dep in skipped:
            op_calls.append(f"    # dependency on non-pipeline task {dep!r} skipped")
        if dep_ops:
            # The keyword half (_dep_arg) matches the op's ``ins`` key; the
            # VALUE half is the variable the upstream op was bound to. Both come
            # from the collision map, so neither a leading digit nor a
            # punctuation collision can make them diverge.
            dep_args = ", ".join(f"{_dep_arg(d)}={d}_result" for d in dep_ops)
            op_calls.append(f"    {op_name}_result = {op_name}({dep_args})")
        else:
            op_calls.append(f"    {op_name}_result = {op_name}()")

    op_calls_str = "\n".join(op_calls) if op_calls else "    pass"
    # The shared sanitizer, not the module-local near-duplicate it replaces:
    # that one stripped neither leading digits nor Python keywords, so a
    # contract id of ``2026-etl`` or ``class`` emitted an un-importable def.
    # It feeds three module-scope emissions below (the def, the schedule
    # variable, and the ``job=`` reference), which must all agree.
    job_name = task_identifier(contract_id)
    cron_schedule = _schedule_to_cron(schedule)

    # The tags value, the cron and the timezone are all data: py_str_literal
    # supplies their quotes, so none are hand-written.
    return f'''# Job definition
@job(
    resource_defs={{"snowflake_conn": snowflake_conn}},
    tags={{
        "fluid": "auto-generated",
        "contract_id": {py_str_literal(contract_id)},
        "provider": "snowflake",
    }},
)
def {job_name}():
    """{escape_for_docstring(contract_id)} pipeline on Snowflake."""
{op_calls_str}

# Schedule
{job_name}_schedule = ScheduleDefinition(
    job={job_name},
    cron_schedule={py_str_literal(cron_schedule)},
    execution_timezone={py_str_literal(timezone)},
)'''


# ``_sanitize_name`` (a local re.sub that handled neither leading digits, nor
# Python keywords, nor the empty string) is gone: every identifier this module
# emits now goes through codegen_utils.task_identifier, which does.
#
# ``_convert_schedule`` (a local near-copy of convert_schedule_to_cron that
# knew neither ``@yearly``/``@annually`` nor a padded keyword, and rewrote
# anything it did not recognise into the 2am default without a word) is gone
# too: see _schedule_to_cron.
