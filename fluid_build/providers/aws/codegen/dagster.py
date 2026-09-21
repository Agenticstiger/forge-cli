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
Dagster Pipeline Generation for AWS Provider.

Generates Dagster pipeline Python code from FLUID contracts.

Supports:
- Software-defined assets
- Task dependencies
- AWS resource integration
- Type-safe ops
"""

import logging
from datetime import datetime
from datetime import timezone as dt_timezone
from graphlib import CycleError, TopologicalSorter
from typing import Any, Dict, Iterable, List, Sequence, Tuple

from fluid_build.providers.common.codegen_utils import (
    convert_schedule_to_cron,
    escape_for_docstring,
    json_literal,
    py_str_literal,
    task_identifier,
)

logger = logging.getLogger(__name__)

# Every name the generated module binds for its own use. An op name is derived
# from a contract ``taskId``, so without seeding the collision map with these a
# task called ``json`` or ``aws_s3_resource`` would emit a ``def`` that rebinds
# a module-level name the rest of the file still depends on. Mirrors
# ``providers/aws/codegen/airflow._EMITTED_MODULE_NAMES``.
_EMITTED_MODULE_NAMES = frozenset(
    {
        "op",
        "job",
        "In",
        "Out",
        "Nothing",
        "resource",
        "ResourceDefinition",
        "ScheduleDefinition",
        "repository",
        "DagsterType",
        "Field",
        "String",
        "boto3",
        "json",
        "logging",
        "logger",
        "aws_s3_resource",
        "aws_glue_resource",
        "aws_athena_resource",
        "aws_lambda_resource",
    }
)

# Services with a dedicated ``@resource`` in the generated file.
_RESOURCE_SERVICES = ("s3", "glue", "athena", "lambda")

_DEFAULT_CRON = "0 2 * * *"


def generate_dagster_pipeline(contract: Dict[str, Any], account_id: str, region: str) -> str:
    """
    Generate Dagster pipeline Python code from FLUID contract.

    Args:
        contract: FLUID contract with orchestration section
        account_id: AWS account ID
        region: AWS region

    Returns:
        Python code for Dagster pipeline
    """
    orchestration = contract.get("orchestration") or {}
    if not orchestration:
        raise ValueError("Contract missing orchestration section")

    tasks = orchestration.get("tasks") or []
    if not tasks:
        raise ValueError("Orchestration has no tasks")

    # Extract configuration
    contract_id = contract.get("id", "unknown")
    contract_name = contract.get("name", contract_id)
    schedule = orchestration.get("schedule", _DEFAULT_CRON)

    # Filter provider action tasks
    provider_tasks = [t for t in tasks if t.get("type") == "provider_action"]

    # The job / schedule / repository names are module-level bindings too, and
    # they are derived from the contract id rather than from a taskId — so they
    # have to be reserved BEFORE op names are assigned, or a taskId equal to the
    # contract id would emit a ``def`` that shadows the job.
    job_base = _sanitize_job_name(contract_id)
    reserved = (f"{job_base}_job", f"{job_base}_schedule", f"{job_base}_repository")

    # One collision map, built once, consulted by the op emitter, the ``ins``
    # keys and the ``@job`` call sites alike. Deriving any of those names a
    # second way is what lets the defining and referencing sides diverge.
    op_names, op_name_by_task_id = _unique_op_identifiers(provider_tasks, reserved)
    wiring = _resolve_dependencies(provider_tasks, op_names, op_name_by_task_id)
    order = _topological_order(provider_tasks, op_names, wiring)

    # Generate pipeline code
    code = _generate_header(contract_id, contract_name, account_id, region)
    code += "\n\n"
    code += _generate_imports()
    code += "\n\n"
    code += _generate_resources(account_id, region)
    code += "\n\n"
    code += _generate_ops(provider_tasks, op_names, wiring, order, account_id, region)
    code += "\n\n"
    code += _generate_job(contract_id, contract_name, provider_tasks, op_names, wiring, order)
    code += "\n\n"
    code += _generate_schedule(contract_id, schedule)
    code += "\n\n"
    code += _generate_repository(contract_id)

    return code


def _unique_op_identifiers(
    tasks: Sequence[Dict[str, Any]], reserved: Iterable[str] = ()
) -> Tuple[List[str], Dict[str, str]]:
    """Assign every task a module-unique op name.

    Returns ``(names_by_index, name_by_raw_task_id)``.

    ``task_identifier`` is deliberately not injective — ``a-b`` and ``a.b`` both
    become ``a_b`` — so two tasks differing only in punctuation emitted two
    ``def``\\ s with the SAME name: the second shadowed the first and one
    declared task silently vanished from the job. Sanitize, then suffix on
    collision, exactly as ``codegen/airflow._unique_task_identifiers`` does.

    The map is keyed by *index* rather than by raw id so that even a contract
    with duplicate ``taskId``\\ s still emits one op per declared task; the
    raw-id lookup (which dependency wiring needs) keeps the first occurrence and
    warns, because a dependency on an ambiguous id cannot be resolved honestly.
    """
    names: List[str] = []
    by_raw: Dict[str, str] = {}
    # Reserving ``<name>_result`` alongside ``<name>`` keeps the job body's
    # invocation bindings from colliding with a sibling op's ``def``.
    used = set(_EMITTED_MODULE_NAMES) | set(reserved)
    used |= {f"{n}_result" for n in used}

    for task in tasks:
        raw = str(task.get("taskId") or "")
        base = task_identifier(raw)
        candidate, suffix = base, 2
        while candidate in used or f"{candidate}_result" in used:
            candidate = f"{base}_{suffix}"
            suffix += 1
        used.add(candidate)
        used.add(f"{candidate}_result")
        names.append(candidate)
        if raw in by_raw:
            logger.warning(
                "aws dagster: duplicate taskId %r — dependencies naming it resolve to the "
                "first occurrence; taskIds must be unique",
                raw,
            )
        else:
            by_raw[raw] = candidate

    return names, by_raw


def _resolve_dependencies(
    tasks: Sequence[Dict[str, Any]],
    op_names: Sequence[str],
    op_name_by_task_id: Dict[str, str],
) -> Dict[str, Tuple[List[str], List[str]]]:
    """Resolve each task's ``dependsOn`` into (kept op names, skipped raw ids).

    Resolved ONCE and shared by the ``ins`` keys and the ``@job`` call kwargs,
    so the two sides cannot disagree about which dependencies exist or what they
    are called.

    Only ``provider_action`` tasks become ops, so a dependency on anything else
    — or on an id no task declares — has no op to point at. Synthesising a name
    for it emits an undefined name (Dagster fails the whole file on import) or,
    because sanitisation is not injective, silently wires the edge to a
    *different* task. It is dropped, loudly.
    """
    wiring: Dict[str, Tuple[List[str], List[str]]] = {}

    for task, op_name in zip(tasks, op_names, strict=False):
        kept: List[str] = []
        skipped: List[str] = []
        for dep in task.get("dependsOn") or []:
            raw_dep = str(dep or "")
            dep_name = op_name_by_task_id.get(raw_dep)
            if dep_name is None or dep_name == op_name:
                if dep_name == op_name:
                    logger.warning(
                        "aws dagster: task %r dependsOn itself — edge skipped",
                        task.get("taskId"),
                    )
                else:
                    logger.warning(
                        "aws dagster: task %r dependsOn %r, which is not a provider_action "
                        "task in this pipeline — edge skipped",
                        task.get("taskId"),
                        raw_dep,
                    )
                skipped.append(raw_dep)
            elif dep_name not in kept:
                kept.append(dep_name)
        wiring[op_name] = (kept, skipped)

    return wiring


def _topological_order(
    tasks: Sequence[Dict[str, Any]],
    op_names: Sequence[str],
    wiring: Dict[str, Tuple[List[str], List[str]]],
) -> List[str]:
    """Order op names so every op is emitted after the ops it consumes.

    A Python call site cannot reference a result bound later in the module, so
    declaration order is not good enough: a contract that declares the dependent
    task first produced ``b(dep_a=a_result)`` above the line binding
    ``a_result``. ``graphlib`` is the stdlib answer (Python 3.9+).
    """
    # A *list*, not a set: TopologicalSorter.add(node, *predecessors) unpacks
    # whatever it is given, so set iteration order (which varies with
    # PYTHONHASHSEED) would leak into static_order() and make the generated
    # file differ run-to-run for an identical contract. Generated artifacts
    # have to be reproducible.
    graph = {name: list(dict.fromkeys(wiring[name][0])) for name in op_names}
    try:
        return list(TopologicalSorter(graph).static_order())
    except CycleError as exc:  # pragma: no cover - rejected upstream
        # detect_circular_dependencies / the schedulers' validators reject cycles
        # before generation; this is a defensive guard, not a user-facing path.
        raise ValueError(f"Circular task dependency in orchestration: {exc.args[1]}") from exc


def _generate_header(contract_id: str, contract_name: str, account_id: str, region: str) -> str:
    """Generate file header."""
    # Every value here lands inside the emitted file's own triple-quoted
    # docstring, so a contract ``name`` carrying ``"""`` would close it and let
    # the rest of the value run as a top-level statement on import.
    return f'''"""
FLUID Generated Dagster Pipeline: {escape_for_docstring(contract_name)}

Contract ID: {escape_for_docstring(contract_id)}
AWS Account: {escape_for_docstring(account_id)}
AWS Region: {escape_for_docstring(region)}

Auto-generated by FLUID Forge - DO NOT EDIT MANUALLY
Generated: {datetime.now(dt_timezone.utc).replace(tzinfo=None).isoformat()}
"""'''


def _generate_imports() -> str:
    """Generate import statements."""
    # ``Nothing`` is load-bearing: a dependency edge is emitted as
    # ``In(Nothing)``, which is the one In() shape Dagster does NOT require a
    # matching function parameter for. Without it every pipeline with a
    # ``dependsOn`` was refused at import with "decorated function does not have
    # argument(s) ...".
    return """from dagster import (
    op,
    job,
    In,
    Nothing,
    Out,
    resource,
    ResourceDefinition,
    ScheduleDefinition,
    repository,
    DagsterType,
    Field,
    String,
)
import boto3
import json
import logging

logger = logging.getLogger(__name__)"""


def _generate_resources(account_id: str, region: str) -> str:
    """Generate Dagster resources for AWS clients."""
    # ``default_value`` sits inside a module-level ``@resource`` decorator, which
    # Dagster evaluates on import — the most severe position in the file. The
    # hand-written quote pair is gone because py_str_literal (repr) brings its
    # own; keeping them would emit ``default_value="'us-east-1'"``.
    return f'''# AWS Resources

@resource(
    config_schema={{
        "account_id": Field(String, default_value={py_str_literal(account_id)}),
        "region": Field(String, default_value={py_str_literal(region)}),
    }}
)
def aws_s3_resource(context):
    """S3 client resource."""
    return boto3.client(
        's3',
        region_name=context.resource_config["region"]
    )


@resource(
    config_schema={{
        "account_id": Field(String, default_value={py_str_literal(account_id)}),
        "region": Field(String, default_value={py_str_literal(region)}),
    }}
)
def aws_glue_resource(context):
    """Glue client resource."""
    return boto3.client(
        'glue',
        region_name=context.resource_config["region"]
    )


@resource(
    config_schema={{
        "account_id": Field(String, default_value={py_str_literal(account_id)}),
        "region": Field(String, default_value={py_str_literal(region)}),
    }}
)
def aws_athena_resource(context):
    """Athena client resource."""
    return boto3.client(
        'athena',
        region_name=context.resource_config["region"]
    )


@resource(
    config_schema={{
        "account_id": Field(String, default_value={py_str_literal(account_id)}),
        "region": Field(String, default_value={py_str_literal(region)}),
    }}
)
def aws_lambda_resource(context):
    """Lambda client resource."""
    return boto3.client(
        'lambda',
        region_name=context.resource_config["region"]
    )'''


def _generate_ops(
    tasks: Sequence[Dict[str, Any]],
    op_names: Sequence[str],
    wiring: Dict[str, Tuple[List[str], List[str]]],
    order: Sequence[str],
    account_id: str,
    region: str,
) -> str:
    """Generate Dagster ops for tasks, in dependency order."""
    ops_code = "# Task Ops\n\n"

    by_name = dict(zip(op_names, tasks, strict=False))
    for op_name in order:
        task = by_name.get(op_name)
        if task is None:  # pragma: no cover - order is built from op_names
            continue
        deps, skipped = wiring[op_name]
        ops_code += _generate_single_op(task, op_name, deps, skipped, account_id, region)
        ops_code += "\n\n"

    return ops_code.rstrip()


def _generate_single_op(
    task: Dict[str, Any],
    op_name: str,
    deps: Sequence[str],
    skipped: Sequence[str],
    account_id: str,
    region: str,
) -> str:
    """Generate code for a single op."""
    task_id = task.get("taskId")
    # ``action`` reaches here straight from YAML, where ``action:`` with no
    # value parses to ``None``. Coercing at the boundary keeps generation from
    # dying with a bare ``AttributeError`` on ``.split``.
    action = str(task.get("action") or "")
    params = task.get("params") or {}

    # Determine required resources based on action
    action_parts = action.split(".")
    service = action_parts[1] if len(action_parts) > 1 else "unknown"

    # Build ins from dependencies. ``dep_<op name>`` is an IDENTIFIER position,
    # not data: it is the op's input name here and the *keyword argument* name
    # _generate_job emits at the call site, and both come from the same entry of
    # the same collision map. In(Nothing) is what makes the pair legal without a
    # matching function parameter — a plain In() is refused by Dagster at import.
    # The key is also a quoted string inside a decorator Dagster evaluates on
    # import, and the map's values are ``[A-Za-z0-9_]`` only, so a dependsOn
    # entry carrying a quote or a newline can never reach the closing quote.
    ins_lines = [f"    # dependency on non-pipeline task {dep!r} skipped" for dep in skipped]
    if deps:
        ins_entries = ", ".join(f'"dep_{dep}": In(Nothing)' for dep in deps)
        ins_lines.append(f"    ins={{{ins_entries}}},")
    ins_str = "\n".join(ins_lines)
    if ins_str:
        ins_str += "\n"

    # Generate op decorator and function
    resource_name = f"aws_{service}_resource" if service in _RESOURCE_SERVICES else None
    # A SET, never a dict: ``required_resource_keys={}`` is an empty *dict*
    # literal, which Dagster rejects.
    required_resources = f'{{"{resource_name}"}}' if resource_name else "set()"

    # The emitted logger line keeps NO ``f`` prefix. repr() escapes quotes and
    # backslashes but NOT braces, so an ``action`` carrying ``{...}`` would stay
    # a live expression evaluated when the op runs. Formatting the whole message
    # here and emitting one plain literal closes that second channel.
    exec_log = py_str_literal(f"Executing: {action}")

    # ``task_id`` and ``action`` land inside the op body, but they are spliced
    # verbatim into module-level source: an embedded newline dedents straight out
    # of the function, so an unescaped value executes on import, not at task run.
    return f'''@op(
{ins_str}    required_resource_keys={required_resources},
)
def {op_name}(context):
    """Execute {escape_for_docstring(action)}."""
    logger.info({exec_log})
    params = json.loads({json_literal(params)})

    # Execute provider action
    {_generate_op_implementation(action, params, service)}

    return {{"status": "success", "task_id": {py_str_literal(task_id)}}}'''


def _generate_op_implementation(action: str, params: Dict[str, Any], service: str) -> str:
    """Generate op implementation code."""
    if service == "s3":
        return """s3_client = context.resources.aws_s3_resource
    bucket = params.get("bucket")
    if bucket:
        s3_client.create_bucket(Bucket=bucket)
        logger.info(f"Created S3 bucket: {bucket}")"""

    elif service == "glue":
        return """glue_client = context.resources.aws_glue_resource
    database = params.get("database")
    if database:
        try:
            glue_client.create_database(DatabaseInput={'Name': database})
            logger.info(f"Created Glue database: {database}")
        except glue_client.exceptions.AlreadyExistsException:
            logger.info(f"Database already exists: {database}")"""

    elif service == "athena":
        return """athena_client = context.resources.aws_athena_resource
    query = params.get("query")
    database = params.get("database", "default")
    if query:
        response = athena_client.start_query_execution(
            QueryString=query,
            QueryExecutionContext={'Database': database},
            ResultConfiguration={'OutputLocation': params.get("outputLocation", "s3://athena-results/")}
        )
        logger.info(f"Started Athena query: {response['QueryExecutionId']}")"""

    elif service == "lambda":
        return """lambda_client = context.resources.aws_lambda_resource
    function_name = params.get("function")
    payload = params.get("payload", {})
    if function_name:
        response = lambda_client.invoke(
            FunctionName=function_name,
            Payload=json.dumps(payload)
        )
        logger.info(f"Invoked Lambda: {function_name}")"""

    else:
        # Second splice of the same untrusted ``action``, reached for any service
        # outside the four above. The ``f`` prefix is dropped for the same reason
        # as in _generate_single_op; the ``Params`` line keeps its ``f`` because
        # the doubled braces are generator-escaping — the emitted f-string
        # interpolates the op's own local ``params``, never contract text.
        action_log = py_str_literal(f"Action: {action}")
        return f"""# Generic provider action execution
    logger.info({action_log})
    logger.info(f"Params: {{params}}")"""


def _generate_job(
    contract_id: str,
    contract_name: str,
    tasks: Sequence[Dict[str, Any]],
    op_names: Sequence[str],
    wiring: Dict[str, Tuple[List[str], List[str]]],
    order: Sequence[str],
) -> str:
    """Generate Dagster job definition."""
    # ``description`` is a kwarg of the module-level ``@job`` decorator and the
    # docstring below is the job function's own — both are evaluated when Dagster
    # imports the file, so contract text reaches them through py_str_literal
    # (quote pair dropped, repr supplies its own) and escape_for_docstring.
    #
    # Build the dependency graph. Each op is INVOKED and its invocation bound to
    # ``<op>_result``; downstream ops take that bound value as the keyword
    # argument matching their ``ins`` key. Passing the op *definition* (the bare
    # name) instead — which is what this emitted before — hands Dagster an
    # OpDefinition where it expects an output, and the whole file is refused.
    graph_calls: List[str] = []
    emitted = set(op_names)

    for op_name in order:
        if op_name not in emitted:  # pragma: no cover - order is built from op_names
            continue
        deps, skipped = wiring[op_name]
        for dep in skipped:
            graph_calls.append(f"    # dependency on non-pipeline task {dep!r} skipped")
        if deps:
            # Both halves of every ``dep_<x>=<x>_result`` pair come from the one
            # collision-map entry that also produced the op's ``def`` and its
            # ``ins`` key, so the kwarg name matches the declared input and the
            # value matches a variable this body has already bound.
            args = ", ".join(f"dep_{dep}={dep}_result" for dep in deps)
            graph_calls.append(f"    {op_name}_result = {op_name}({args})")
        else:
            graph_calls.append(f"    {op_name}_result = {op_name}()")

    graph_body = "\n".join(graph_calls)

    return f'''@job(
    resource_defs={{
        "aws_s3_resource": aws_s3_resource,
        "aws_glue_resource": aws_glue_resource,
        "aws_athena_resource": aws_athena_resource,
        "aws_lambda_resource": aws_lambda_resource,
    }},
    description={py_str_literal(contract_name)},
)
def {_sanitize_job_name(contract_id)}_job():
    """{escape_for_docstring(contract_name)} workflow."""
{graph_body}'''


def _generate_schedule(contract_id: str, schedule: Any) -> str:
    """Generate schedule definition."""
    # _convert_to_cron is a *converter*, not a sanitizer — it returns any string
    # with >= 4 spaces verbatim — so the value still has to be emitted as a
    # literal rather than wrapped in hand-written quotes it could close.
    cron_schedule = _convert_to_cron(schedule)

    return f"""# Schedule
{_sanitize_job_name(contract_id)}_schedule = ScheduleDefinition(
    job={_sanitize_job_name(contract_id)}_job,
    cron_schedule={py_str_literal(cron_schedule)},
)"""


def _generate_repository(contract_id: str) -> str:
    """Generate Dagster repository."""
    job_name = _sanitize_job_name(contract_id)

    return f'''# Repository
@repository
def {job_name}_repository():
    """Dagster repository for {escape_for_docstring(contract_id)}."""
    return [
        {job_name}_job,
        {job_name}_schedule,
    ]'''


def _sanitize_op_name(name: Any) -> str:
    """Sanitize op name for Dagster.

    A Dagster op name is emitted as a ``def`` at module scope, so it has to be a
    legal, non-keyword Python identifier. This delegates to the shared escaper
    rather than keeping the hand-rolled character loop it replaced, which was
    strictly weaker: no keyword guard (a ``taskId`` of ``class`` emitted
    ``def class(context):`` and killed the whole generated file with a
    SyntaxError) and no ``str()`` coercion (a task missing ``taskId`` raised
    ``TypeError`` during generation).

    It is NOT injective, so op names go through ``_unique_op_identifiers``
    before they are emitted; this is the base name that map suffixes.
    """
    return task_identifier(name)


def _sanitize_job_name(name: Any) -> str:
    """Sanitize job name for Dagster."""
    return _sanitize_op_name(name)


def _convert_to_cron(schedule: Any) -> str:
    """Convert a contract schedule to a cron expression.

    Delegates to ``codegen_utils.convert_schedule_to_cron`` — the local fork
    this replaced knew four bare presets, did not strip whitespace, did not know
    ``@hourly``/``@weekly``/``@yearly`` at all, and turned everything it failed
    to recognise into "daily at 02:00" *silently*. An unrecognised schedule is a
    contract mistake the user needs to hear about, so it warns before falling
    back.
    """
    raw = str(schedule or "").strip()
    if not raw:
        # A null/absent schedule is the documented default, not a mistake.
        return _DEFAULT_CRON

    cron = convert_schedule_to_cron(raw)
    if cron == _DEFAULT_CRON and raw != _DEFAULT_CRON:
        logger.warning(
            "aws dagster: unrecognised schedule %r — falling back to %r; use a 5-field cron "
            "expression or one of @hourly/@daily/@weekly/@monthly/@yearly",
            raw,
            _DEFAULT_CRON,
        )
    return cron
