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
Dagster Pipeline Generation for GCP Provider.

Generates Python pipeline code from FLUID contracts for Dagster on GCP.

Supports:
- BigQuery ops with type safety
- Cloud Storage resources
- Pub/Sub integration

The generated file has to *load* under Dagster, not merely parse, so three
properties are load-bearing throughout this module:

* every op name is resolved **once** per pipeline (:func:`_unique_task_identifiers`)
  and both the ``def`` and every reference to it read that one map;
* the job body is emitted in **dependency order**, because a Python call site
  cannot reference a result bound further down the module; and
* ``ins`` keys and call keyword arguments are built from the same resolved
  name, so the graph Dagster builds actually type-checks.
"""

import logging
from datetime import datetime
from datetime import timezone as dt_timezone
from graphlib import CycleError, TopologicalSorter
from typing import Any, Dict, List, Optional, Tuple

from fluid_build.providers.common.codegen_utils import (
    convert_schedule_to_cron,
    escape_for_docstring,
    json_literal,
    py_str_literal,
    task_identifier,
)

logger = logging.getLogger(__name__)

# Module-level names the generated pipeline binds itself. An op name must not
# collide with any of them: a taskId of ``bigquery_client`` would rebind the
# resource the ops resolve, and the failure would only surface at run time.
_EMITTED_MODULE_NAMES = frozenset(
    {
        "op",
        "job",
        "resource",
        "In",
        "Nothing",
        "ScheduleDefinition",
        "bigquery",
        "storage",
        "pubsub_v1",
        "gcp_exceptions",
        "json",
        "logging",
        "logger",
        "gcp_project",
        "gcp_region",
        "bigquery_client",
        "gcs_client",
        "pubsub_client",
    }
)

# Fallback when a schedule is neither a cron expression nor a known keyword.
# Mirrors ``convert_schedule_to_cron``; see :func:`_cron_for_schedule`.
_DEFAULT_CRON = "0 2 * * *"
_SCHEDULE_KEYWORDS = frozenset({"@hourly", "@daily", "@weekly", "@monthly", "@yearly", "@annually"})


def generate_dagster_pipeline(contract: Dict[str, Any], project: str, region: str) -> str:
    """
    Generate Dagster pipeline Python code from FLUID contract.

    Args:
        contract: FLUID contract with orchestration section
        project: GCP project ID
        region: GCP region

    Returns:
        Python code for Dagster pipeline
    """
    orchestration = contract.get("orchestration", {})
    if not orchestration:
        raise ValueError("Contract missing orchestration section")

    tasks = orchestration.get("tasks", [])
    if not tasks:
        raise ValueError("Orchestration has no tasks")

    # Extract configuration. Every one of these is a YAML scalar a contract can
    # leave null, and every one of them used to reach a ``.lower()`` / ``.split()``
    # / ``.get()`` call unguarded — a null ``schedule`` raised a bare
    # ``AttributeError`` from inside the generator instead of a named failure.
    contract_id = str(contract.get("id") or "unknown")
    contract_name = str(contract.get("name") or contract_id)
    # Absent means "use the default"; explicitly null means the contract said
    # something this generator does not understand, which _cron_for_schedule warns about.
    schedule = str(orchestration.get("schedule", _DEFAULT_CRON) or "")
    timezone = str(orchestration.get("timezone") or "UTC")

    # Filter provider action tasks
    provider_tasks = [t for t in tasks if t.get("type") == "provider_action"]

    # One resolution of every op name for the whole file. The ``def`` the op
    # emitters write, the ``ins`` keys, the call-site keyword arguments and the
    # ``<name>_result`` variables all read this same map, so they cannot drift.
    task_vars = _unique_task_identifiers(provider_tasks, reserved=_reserved_names(contract_id))
    ordered_tasks = _ordered_tasks(provider_tasks)

    # Generate pipeline code
    code = _generate_header(contract_id, contract_name, schedule, timezone, project, region)
    code += "\n\n"
    code += _generate_imports(
        include_gcp_exceptions=any(
            _action_parts(task) == ("pubsub", "create_topic") for task in ordered_tasks
        )
    )
    code += "\n\n"
    code += _generate_resources(project, region)
    code += "\n\n"
    code += _generate_ops(ordered_tasks, project, region, task_vars)
    code += "\n\n"
    code += _generate_job(contract_id, ordered_tasks, schedule, timezone, task_vars)

    return code


# ---------------------------------------------------------------------------
# Name resolution and ordering
# ---------------------------------------------------------------------------


def _reserved_names(contract_id: str) -> Tuple[str, ...]:
    """Module-level names derived from the contract id that ops must not take."""
    job_name = task_identifier(contract_id)
    return (job_name, f"{job_name}_schedule")


def _unique_task_identifiers(
    tasks: List[Dict[str, Any]], reserved: Tuple[str, ...] = ()
) -> Dict[str, str]:
    """Map each raw ``taskId`` to an op name unique within this pipeline.

    ``task_identifier`` is deliberately not injective — ``a-b`` and ``a.b`` both
    become ``a_b`` — so two tasks differing only in punctuation emitted two
    ``def``s with the same name: the second shadowed the first and one declared
    task silently vanished from the job while the survivor ran twice.

    Sanitize, then suffix on collision. Borrowed wholesale from
    ``providers/aws/codegen/airflow._unique_task_identifiers`` (#623/#630),
    which fixed the identical defect on the Airflow path.
    """
    identifiers: Dict[str, str] = {}
    # Seed with the names the emitted module already binds, so a taskId cannot
    # rebind one of them. ``<name>_result`` is reserved alongside each op name
    # because the job body binds that form too: without it, tasks ``a`` and
    # ``a_result`` make ``a_result`` mean two different things in one scope.
    used = set(_EMITTED_MODULE_NAMES)
    used.update(reserved)
    for task in tasks:
        raw = str(task.get("taskId") or "")
        if raw in identifiers:
            # Duplicates are rejected upstream by the contract validators; warn
            # honestly rather than emitting two ops under one name if one lands here.
            logger.warning(
                "gcp dagster: duplicate taskId %r — only the first occurrence is "
                "emitted; taskIds must be unique",
                raw,
            )
            continue
        base = task_identifier(raw)
        candidate, suffix = base, 2
        while candidate in used or f"{candidate}_result" in used:
            candidate = f"{base}_{suffix}"
            suffix += 1
        used.add(candidate)
        used.add(f"{candidate}_result")
        identifiers[raw] = candidate
    return identifiers


def _ordered_tasks(tasks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return ``tasks`` in dependency order, de-duplicated by ``taskId``.

    Contract order is not dependency order. The job body is a sequence of
    ordinary Python calls, so a task declared before the task it depends on
    emitted ``b_result = b(dep_a=a_result)`` above ``a_result = a()`` — a
    ``NameError`` that made the generated module unimportable.
    """
    by_id: Dict[str, Dict[str, Any]] = {}
    for task in tasks:
        raw = str(task.get("taskId") or "")
        by_id.setdefault(raw, task)

    sorter: TopologicalSorter = TopologicalSorter()
    for raw, task in by_id.items():
        # Only edges between tasks that become ops carry ordering information;
        # the rest are dropped here and reported by _resolved_dependencies.
        sorter.add(raw, *[d for d in _depends_on(task) if d in by_id and d != raw])

    try:
        order = list(sorter.static_order())
    except CycleError as exc:  # pragma: no cover - rejected by detect_circular_dependencies
        raise AssertionError(
            "cyclic dependsOn reached the Dagster emitter; "
            "detect_circular_dependencies should have rejected it upstream"
        ) from exc

    return [by_id[raw] for raw in order]


def _depends_on(task: Dict[str, Any]) -> List[str]:
    """Normalise ``dependsOn`` to a list of strings.

    A null scalar, or a single id written without a list, both reach here from
    ordinary YAML.
    """
    raw = task.get("dependsOn") or []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        logger.warning(
            "gcp dagster: task %r has a non-list dependsOn (%s) — ignored",
            task.get("taskId"),
            type(raw).__name__,
        )
        return []
    return [str(dep) for dep in raw]


def _action_parts(task: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    """``(service, operation)`` for a task's ``action``, or ``(None, None)``.

    ``action`` is a YAML scalar: a null one reached ``.split(".")`` as a bare
    ``AttributeError`` raised from inside the generator.
    """
    parts = str(task.get("action") or "").split(".")
    if len(parts) < 3:
        return None, None
    return parts[1], parts[2]


def _task_params(task: Dict[str, Any]) -> Dict[str, Any]:
    """``params`` is a mapping in the schema; a null scalar makes it ``None``."""
    params = task.get("params")
    if params is None:
        return {}
    if not isinstance(params, dict):
        logger.warning(
            "gcp dagster: task %r has non-mapping params (%s) — ignored",
            task.get("taskId"),
            type(params).__name__,
        )
        return {}
    return params


def _op_name(task: Dict[str, Any], task_vars: Optional[Dict[str, str]]) -> str:
    """Resolve a task's op name, honouring the per-pipeline collision map.

    ``task_vars`` is optional so the individual emitters stay directly callable
    (several tests exercise them in isolation); without it the behaviour is the
    plain sanitised name.
    """
    raw = str(task.get("taskId") or "")
    if task_vars and raw in task_vars:
        return task_vars[raw]
    return task_identifier(raw)


def _resolved_dependencies(
    task: Dict[str, Any], task_vars: Optional[Dict[str, str]], *, warn: bool
) -> List[Tuple[str, Optional[str]]]:
    """Pair every ``dependsOn`` entry with the op name it refers to, or ``None``.

    ``None`` means the dependency is not a ``provider_action`` task in this
    pipeline (a ``bash`` step, say). Such an edge has no op to point at, so
    emitting it produced a reference to a name the module never binds. Both the
    ``ins`` side and the call side must drop it — and say so.

    ``warn`` is set on the ``ins`` pass only, so one dropped edge logs once
    rather than once per side.
    """
    resolved: List[Tuple[str, Optional[str]]] = []
    for dep in _depends_on(task):
        if task_vars is None:
            # Called without the map (direct-call tests): no knowledge of which
            # tasks exist, so nothing can be skipped.
            resolved.append((dep, task_identifier(dep)))
            continue
        var = task_vars.get(dep)
        if var is None and warn:
            logger.warning(
                "gcp dagster: task %r dependsOn %r, which is not a provider_action "
                "task in this pipeline — edge skipped",
                task.get("taskId"),
                dep,
            )
        resolved.append((dep, var))
    return resolved


def _dependency_ins(
    task: Dict[str, Any], task_vars: Optional[Dict[str, str]]
) -> Tuple[List[str], List[str]]:
    """Return ``(ins items, skip comments)`` for a task's dependencies.

    ``dep_<name>`` is an identifier position, not data: it is the op's input
    name here and the *keyword argument* name :func:`_generate_job` emits at the
    call site. Both sides read :func:`_op_name`'s resolution of the same raw id,
    so they agree — deriving one of them any other way emits a keyword the op
    never declared, which Dagster rejects at load.
    """
    ins_items: List[str] = []
    skips: List[str] = []
    for dep, var in _resolved_dependencies(task, task_vars, warn=True):
        if var is None:
            skips.append(f"# dependency on non-DAG task {dep!r} skipped")
            continue
        ins_items.append(f'"dep_{var}": In(Nothing)')
    return ins_items, skips


def _ins_kwarg(ins_items: List[str], *, trailing: bool) -> str:
    """Render the ``ins={...}`` decorator argument, or nothing at all.

    ``trailing`` adds the separator for decorators that carry further keywords;
    the leading ``", "`` this once carried emitted ``@op(, ins={...})``, which
    does not parse.
    """
    if not ins_items:
        return ""
    rendered = "ins={" + ", ".join(ins_items) + "}"
    return f"{rendered}, " if trailing else rendered


# ---------------------------------------------------------------------------
# Emitters
# ---------------------------------------------------------------------------


def _generate_header(
    contract_id: str, contract_name: str, schedule: str, timezone: str, project: str, region: str
) -> str:
    """Generate file header with metadata."""
    # Every field is escaped: an unescaped ``"""`` in a contract value closes
    # this header docstring and leaves whatever follows as a top-level
    # statement, which Dagster runs the moment it imports the pipeline file.
    return f'''"""
FLUID Generated Dagster Pipeline: {escape_for_docstring(contract_name)}

Contract ID: {escape_for_docstring(contract_id)}
Schedule: {escape_for_docstring(schedule)}
Timezone: {escape_for_docstring(timezone)}
GCP Project: {escape_for_docstring(project)}
GCP Region: {escape_for_docstring(region)}

Auto-generated by FLUID Forge - DO NOT EDIT MANUALLY
Generated: {datetime.now(dt_timezone.utc).replace(tzinfo=None).isoformat()}Z
"""'''


def _generate_imports(include_gcp_exceptions: bool = False) -> str:
    """Generate import statements.

    Only what the emitted file actually uses. ``Out`` and ``DagsterEventType``
    were never referenced, and ``dataflow_v1beta3`` ships in a separate
    distribution from the other three clients — so a pipeline that touches only
    BigQuery raised ``ImportError`` on import, before a single op could run.

    ``bigquery``/``storage``/``pubsub_v1`` are unconditional because the resource
    block below uses all three. ``google.api_core.exceptions`` is only used by
    the Pub/Sub topic op, so it is emitted only for a pipeline that has one.
    """
    api_core = ""
    if include_gcp_exceptions:
        api_core = "from google.api_core import exceptions as gcp_exceptions\n"
    return f"""from dagster import (
    op,
    job,
    resource,
    In,
    Nothing,
    ScheduleDefinition,
)
{api_core}from google.cloud import bigquery, storage, pubsub_v1
import json
import logging

logger = logging.getLogger(__name__)"""


def _generate_resources(project: str, region: str) -> str:
    """Generate GCP resource definitions.

    Each client resource reads ``context.resources.gcp_project``, which Dagster
    only provides to a resource that *declares* the dependency: without
    ``required_resource_keys`` every BigQuery/GCS pipeline died at run time with
    ``DagsterUnknownResourceError``. ``pubsub_client`` does not use the project
    at all, so it declares nothing.
    """
    return f'''# GCP Resources
@resource
def gcp_project():
    """GCP project ID resource."""
    return {py_str_literal(project)}

@resource
def gcp_region():
    """GCP region resource."""
    return {py_str_literal(region)}

@resource(required_resource_keys={{"gcp_project"}})
def bigquery_client(context):
    """BigQuery client resource."""
    project = context.resources.gcp_project
    return bigquery.Client(project=project)

@resource(required_resource_keys={{"gcp_project"}})
def gcs_client(context):
    """Cloud Storage client resource."""
    project = context.resources.gcp_project
    return storage.Client(project=project)

@resource
def pubsub_client(context):
    """Pub/Sub client resource."""
    return pubsub_v1.PublisherClient()'''


def _generate_ops(
    tasks: List[Dict[str, Any]],
    project: str,
    region: str,
    task_vars: Optional[Dict[str, str]] = None,
) -> str:
    """Generate op definitions, in the dependency order of ``tasks``."""
    ops_code = "# Pipeline Ops\n"

    for task in tasks:
        ops_code += _generate_single_op(task, project, region, task_vars)
        ops_code += "\n\n"

    return ops_code.rstrip()


def _generate_single_op(
    task: Dict[str, Any],
    project: str,
    region: str,
    task_vars: Optional[Dict[str, str]] = None,
) -> str:
    """Generate code for a single op."""
    action = str(task.get("action") or "")
    params = _task_params(task)
    op_name = _op_name(task, task_vars)

    ins_items, skips = _dependency_ins(task, task_vars)
    # A skipped edge is invisible in the emitted graph, so it is stated in the
    # emitted source as well as in the log.
    prefix = "".join(f"{comment}\n" for comment in skips)

    service, operation = _action_parts(task)
    if service is None or operation is None:
        return prefix + _generate_generic_op(op_name, action, params, ins_items)

    # Map to appropriate op
    if service == "bigquery":
        return prefix + _generate_bigquery_op(op_name, operation, action, params, ins_items)
    elif service == "gcs" or service == "storage":
        return prefix + _generate_gcs_op(op_name, operation, action, params, ins_items)
    elif service == "pubsub":
        return prefix + _generate_pubsub_op(op_name, operation, action, params, ins_items)
    else:
        return prefix + _generate_generic_op(op_name, action, params, ins_items)


def _generate_bigquery_op(
    op_name: str,
    operation: str,
    action: str,
    params: Dict[str, Any],
    ins_items: List[str],
) -> str:
    """Generate BigQuery op code."""
    # The op's ``def`` name is a Python identifier; a raw taskId such as
    # ``load-orders`` emitted ``def load-orders(context):``. It comes from
    # _op_name so it is the same name the job body calls.
    ins_def = _ins_kwarg(ins_items, trailing=True)

    if operation == "create_dataset":
        dataset_id = params.get("dataset_id", "unknown_dataset")
        location = params.get("location", "US")
        # The dataset reference is built by concatenation rather than a
        # generated f-string: repr() escapes quotes but NOT braces, so a ``{``
        # in dataset_id would survive into an f-string as a live expression
        # evaluated at op run.
        #
        # The braces around the generator-written names below are doubled, not
        # quadrupled: quadrupling emitted ``{{dataset.dataset_id}}``, which an
        # f-string renders as the literal text ``{dataset.dataset_id}`` — the
        # log line swallowed the value it exists to report. The failure paths
        # log via ``context.log.exception``, which reports the real exception
        # *and* its traceback; the ``f"...{{e}}"`` they used to carry rendered
        # as the literal text ``{e}`` and reported neither.
        return f'''@op({ins_def}required_resource_keys={{"bigquery_client", "gcp_project"}})
def {op_name}(context):
    """Create BigQuery dataset: {escape_for_docstring(dataset_id)}"""
    client = context.resources.bigquery_client
    dataset = bigquery.Dataset(context.resources.gcp_project + "." + {py_str_literal(dataset_id)})
    dataset.location = {py_str_literal(location)}

    try:
        dataset = client.create_dataset(dataset, exists_ok=True)
        context.log.info(f"Created dataset {{dataset.dataset_id}}")
        return dataset.dataset_id
    except Exception:
        context.log.exception("Failed to create dataset")
        raise'''

    elif (operation == "query" or operation == "run_query") and (
        params.get("query") or params.get("sql")
    ):
        query_sql = params.get("query") or params.get("sql")
        # The SQL is DATA. The hand-rolled double-.replace() this had escaped
        # backslashes and double quotes but NOT newlines or triple quotes, so
        # an ordinary multi-line SELECT emitted an unterminated string literal
        # — and a ``"""`` in the query closed it and injected a statement.
        # repr() (via py_str_literal) picks its own quote style and escapes all
        # three, so the hand-written quotes around the splice are dropped.
        return f'''@op({ins_def}required_resource_keys={{"bigquery_client"}})
def {op_name}(context):
    """Run BigQuery query"""
    client = context.resources.bigquery_client
    query = {py_str_literal(query_sql)}

    try:
        query_job = client.query(query)
        results = query_job.result()
        row_count = results.total_rows
        context.log.info(f"Query completed: {{row_count}} rows")
        return row_count
    except Exception:
        context.log.exception("Query failed")
        raise'''

    else:
        return _generate_generic_op(op_name, action, params, ins_items)


def _generate_gcs_op(
    op_name: str,
    operation: str,
    action: str,
    params: Dict[str, Any],
    ins_items: List[str],
) -> str:
    """Generate Cloud Storage op code."""
    ins_def = _ins_kwarg(ins_items, trailing=True)

    if operation == "create_bucket" or operation == "ensure_bucket":
        bucket_name = params.get("bucket", "unknown-bucket")
        location = params.get("location", "US")

        return f'''@op({ins_def}required_resource_keys={{"gcs_client"}})
def {op_name}(context):
    """Create GCS bucket: {escape_for_docstring(bucket_name)}"""
    client = context.resources.gcs_client
    bucket = client.bucket({py_str_literal(bucket_name)})
    bucket.location = {py_str_literal(location)}

    try:
        if not bucket.exists():
            bucket.create()
            context.log.info(f"Created bucket {{bucket.name}}")
        else:
            context.log.info(f"Bucket {{bucket.name}} already exists")
        return bucket.name
    except Exception:
        context.log.exception("Failed to create bucket")
        raise'''

    else:
        return _generate_generic_op(op_name, action, params, ins_items)


def _generate_pubsub_op(
    op_name: str,
    operation: str,
    action: str,
    params: Dict[str, Any],
    ins_items: List[str],
) -> str:
    """Generate Pub/Sub op code.

    The already-exists branch matches ``google.api_core.exceptions.AlreadyExists``
    rather than substring-matching ``str(exc)``: the message is not an API
    contract, and matching it swallowed unrelated failures whose text happened
    to contain "already exists".
    """
    ins_def = _ins_kwarg(ins_items, trailing=True)

    if operation == "create_topic":
        topic_name = params.get("topic", "unknown-topic")
        return f'''@op({ins_def}required_resource_keys={{"pubsub_client", "gcp_project"}})
def {op_name}(context):
    """Create Pub/Sub topic: {escape_for_docstring(topic_name)}"""
    client = context.resources.pubsub_client
    topic_path = client.topic_path(context.resources.gcp_project, {py_str_literal(topic_name)})

    try:
        topic = client.create_topic(request={{"name": topic_path}})
        context.log.info(f"Created topic {{topic.name}}")
        return topic.name
    except gcp_exceptions.AlreadyExists:
        context.log.info(f"Topic {{topic_path}} already exists")
        return topic_path
    except Exception:
        context.log.exception("Failed to create topic")
        raise'''

    else:
        return _generate_generic_op(op_name, action, params, ins_items)


def _generate_generic_op(
    op_name: str,
    action: str,
    params: Dict[str, Any],
    ins_items: List[str],
) -> str:
    """Generate generic op."""
    ins_def = _ins_kwarg(ins_items, trailing=False)

    # Both log lines drop the generated ``f`` prefix. repr() does not escape
    # braces, so with the f prefix kept a ``{`` anywhere in ``action`` (or in
    # the params repr) stayed a LIVE expression evaluated when the op runs.
    # The whole message is pre-rendered here and emitted as one escaped
    # literal; params round-trips through json.loads so the op still logs a
    # real mapping (splicing its repr also emitted ``f"Params: {}"`` — a
    # SyntaxError — for the empty default).
    action_log = py_str_literal(f"Action: {action}")
    return f'''@op({ins_def})
def {op_name}(context):
    """Generic op: {escape_for_docstring(action)}"""
    context.log.info({action_log})
    params = json.loads({json_literal(params)})
    context.log.info("Params: " + repr(params))
    return True'''


def _generate_job(
    contract_id: str,
    tasks: List[Dict[str, Any]],
    schedule: str,
    timezone: str,
    task_vars: Optional[Dict[str, str]] = None,
) -> str:
    """Generate job and schedule definitions.

    ``task_vars`` and the ordering are resolved by :func:`generate_dagster_pipeline`
    and passed in, so the call sites here name exactly the ops
    :func:`_generate_ops` defined. Called standalone (direct-call tests) both are
    recomputed from ``tasks`` alone.
    """
    if task_vars is None:
        task_vars = _unique_task_identifiers(tasks, reserved=_reserved_names(contract_id))
        tasks = _ordered_tasks(tasks)

    # Build op call graph, in dependency order: a Python call cannot reference a
    # result bound further down the function body.
    op_calls = []

    for task in tasks:
        op_name = _op_name(task, task_vars)
        dep_args = []
        for dep, var in _resolved_dependencies(task, task_vars, warn=False):
            if var is None:
                # Already warned on the ins side; the emitted graph says so too.
                op_calls.append(f"    # dependency on non-DAG task {dep!r} skipped")
                continue
            # Two names on one line, and both come from the same resolution as
            # the op's ``ins`` key and the upstream op's ``def``: the
            # ``dep_<name>`` keyword must match the input the op declared, and
            # the ``<name>_result`` VALUE must match the variable the upstream
            # op was bound to above. Deriving either one separately is how this
            # diverged before — on a leading digit, silently wiring an op to a
            # name the module never defines.
            dep_args.append(f"dep_{var}={var}_result")
        op_calls.append(f"    {op_name}_result = {op_name}({', '.join(dep_args)})")

    op_calls_str = "\n".join(op_calls) if op_calls else "    pass"

    # Convert cron to Dagster schedule
    cron_schedule = _cron_for_schedule(schedule)

    # task_identifier, not the local _sanitize_name below: the latter is a
    # weaker fork with no keyword guard and no empty-string guard, so a
    # contract id of ``class`` or ``""`` emitted ``def class():`` / ``def ():``.
    job_name = task_identifier(contract_id)

    # The tag value, the cron string and the timezone are all spliced into
    # calls at MODULE scope, which Dagster evaluates on plain import — a quote
    # break-out in any of them runs before a single op does. convert_schedule_to_cron
    # passes any string with >= 4 spaces through verbatim, so the cron field is
    # no safer than the other two.
    return f'''# Job definition
@job(
    resource_defs={{
        "gcp_project": gcp_project,
        "gcp_region": gcp_region,
        "bigquery_client": bigquery_client,
        "gcs_client": gcs_client,
        "pubsub_client": pubsub_client,
    }},
    tags={{
        "fluid": "auto-generated",
        "contract_id": {py_str_literal(contract_id)},
        "provider": "gcp",
    }},
)
def {job_name}():
    """{escape_for_docstring(contract_id)} pipeline on GCP."""
{op_calls_str}

# Schedule
{job_name}_schedule = ScheduleDefinition(
    job={job_name},
    cron_schedule={py_str_literal(cron_schedule)},
    execution_timezone={py_str_literal(timezone)},
)'''


def _cron_for_schedule(schedule: Any) -> str:
    """Cron expression for ``schedule``, warning when it is not understood.

    ``convert_schedule_to_cron`` falls back to daily-at-02:00 for anything it
    does not recognise. Falling back is fine; doing it *silently* is not — a
    contract asking for ``@every_5_minutes`` used to run once a day with nothing
    in the log to say so.
    """
    raw = str(schedule or "")
    cron = _convert_schedule(raw)
    # Mirrors convert_schedule_to_cron's own two accepting branches.
    recognised = raw.strip().lower() in _SCHEDULE_KEYWORDS or raw.count(" ") >= 4
    if not recognised:
        logger.warning(
            "gcp dagster: schedule %r is neither a cron expression nor a supported "
            "keyword (%s) — falling back to %r",
            schedule,
            ", ".join(sorted(_SCHEDULE_KEYWORDS)),
            cron,
        )
    return cron


def _convert_schedule(schedule: Any) -> str:
    """Convert FLUID schedule to Dagster cron schedule.

    A thin alias for :func:`codegen_utils.convert_schedule_to_cron`, kept for its
    existing importers (``tests/test_gcp_codegen.py``). The local fork this
    replaces missed ``@yearly``/``@annually`` and did not strip whitespace, so a
    padded ``" @daily "`` silently became daily-at-02:00 instead. Emitters should
    call :func:`_cron_for_schedule`, which also warns on an unrecognised value.
    """
    return convert_schedule_to_cron(str(schedule or ""))


def _sanitize_name(name: str) -> str:
    """Sanitize name for Python identifier.

    Kept for its existing importers (``tests/test_gcp_codegen.py``); no emitter
    in this module still calls it. Every identifier this file generates now
    goes through ``codegen_utils.task_identifier``, which additionally prefixes
    leading digits and guards keywords and the empty string.
    """
    import re

    return re.sub(r"[^a-zA-Z0-9_]", "_", name)
