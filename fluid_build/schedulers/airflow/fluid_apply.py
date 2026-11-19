# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Airflow DAGs that run a contract's builds through ``fluid apply``.

Each task of a DAG executes one build, on the Airflow worker::

    cd "$FLUID_PROJECT_DIR"
    fluid apply "$FLUID_PROJECT_DIR/<contract path>" [--env <env>] \\
        --mode amend-and-build --build-id <build id> --yes

which is the same OpenTofu-backed apply the pipeline's stage 7 runs, so a
cloud target keeps one state (``FLUID_STATE_BACKEND``) whether the run came
from CI or from the scheduler. The contract path and ``--env`` are fixed when
the DAG is generated; the project directory is read on the worker at run
time, because the checkout lives somewhere else there than it does in CI.

Which builds share a DAG, and when it runs (:func:`scheduled_dags`):

* Builds that ``builds[].dependencies`` link run in one DAG, one task per
  build, each after the builds it names. The DAG runs on the one trigger its
  builds declare, on whichever build declares it; a build with no trigger of
  its own (or ``trigger.type: dependency``) runs in its upstream's DAG. The
  DAG and its file are named after the build that carries the trigger. A
  dependency's ``condition`` is the task's trigger rule
  (:data:`TRIGGER_RULES`). Two different triggers in one linked group, a
  cycle, a dependency on itself, on a build that does not exist or on one
  another engine runs are refused, each with what to change.
* A dependency on a build that no scheduler starts (a stream ``fluid apply``
  keeps running, a Flink job, a Databricks job) is not waited for; the DAG's
  documentation names it.
* ``trigger.type: schedule`` is a cron (``SCHEDULE``). ``dataset`` runs when
  the datasets in ``trigger.datasets`` are updated, all of them or any one
  (``datasetsOperator``); ``schedule_and_dataset`` runs on the cron and on
  those updates (``AssetOrTimeSchedule``). A dataset is an Airflow asset
  URI, or ``{productId, exposeId}`` of an expose of this contract, named as
  :mod:`fluid_build.schedulers.airflow._assets` names it.
* Every task declares the assets of the exposes its build writes as
  ``outlets``, so Airflow starts a DAG scheduled on one of them when the
  task succeeds, in this contract or in another product's.

Each build's node is written by :func:`render_build_task` into
``tasks[<build id>]``, and the edges are written separately, so the one
function decides what runs a build. A DAG with one task keeps the shape it
has always had: ``task_id="fluid_apply"``, nothing but its schedule and
outlets changes with the contract.

A forge Spark build (``engine: spark`` with ``parameters.spark``, on EMR Serverless, Glue, EMR
on EC2 or EMR on EKS) gets the same DAG: the build phase of that ``fluid apply`` starts one run
on its runtime and waits for it, and fails the task unless the run succeeded. The task gets the
three hours every apply DAG's task gets (:data:`APPLY_TASK_TIMEOUT_S`, for ``tofu`` and the
rest of the apply) plus as long as the run may take (``execution.runtime.timeout``). A
stream that is not ``trigger.availableNow`` runs continuously and ``fluid apply`` keeps it
running, so it gets no DAG.

Rendering rules:

* Airflow 3 first: ``airflow.sdk.DAG``, ``BashOperator`` from
  ``airflow.providers.standard``, ``Asset`` from ``airflow.sdk``,
  ``schedule=``. Guarded imports keep the file importable on Airflow 2.6+
  (``schedule=`` and ``Dataset`` need 2.4, ``skip_on_exit_code`` needs 2.6);
  a ``datasetsOperator: any`` or a ``schedule_and_dataset`` trigger needs
  2.9 (``DatasetAny``, ``DatasetOrTimeSchedule``).
* The bash script is the same static text in every DAG. Contract-derived
  values (build id, env, contract path) reach it only as environment
  variables set through ``env=`` with ``append_env=True`` and are expanded
  inside double quotes, which is what the Airflow docs recommend for values
  that did not come from the DAG author. They are also validated, whole
  string, to a grammar with no quotes, ``$``, ``{``, ``%``, ``#`` or
  whitespace (a trailing newline included), so neither the shell nor
  Airflow's Jinja rendering of ``bash_command`` and ``env`` can interpret
  them.
* No secret is ever written into the DAG. The env dict holds ids and the
  NAMES of the variables the contract reads (``{{ env.X }}``, ``${X}``,
  ``secretRef: env://X``); the values come from the worker's environment at
  run time. fluid is started with ``env -i`` and only the variables
  ``fluid apply`` reads (:data:`PASSTHROUGH_PREFIXES`,
  :data:`PASSTHROUGH_NAMES`); a name starting ``AIRFLOW`` never passes, even
  when the contract asks for it, so the Airflow worker's own secrets
  (``AIRFLOW__*``, ``AIRFLOW_CONN_*``, the Fernet key) never reach the fluid
  process.
* The cron is checked to the grammar croniter, Airflow's cron parser,
  accepts (see :func:`_validate_schedule`), so a schedule Airflow would
  refuse fails generation instead of the DAG import.
* ``skip_on_exit_code=None``: a fluid exit status of 99 must fail the task,
  not mark it skipped.
* ``max_active_runs=1`` and ``catchup=False``: two applies of one build never
  overlap on one state, and a paused DAG does not replay missed runs.

Every contract-derived value in the generated Python goes through
:func:`fluid_build.providers.common.codegen_utils.py_str_literal`.
"""

from __future__ import annotations

import ast
import re
import zoneinfo
from dataclasses import dataclass
from graphlib import CycleError, TopologicalSorter
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Set, Tuple

from fluid_build.build_runners._ids import IdentifierViolation, validate_identifier
from fluid_build.providers.common.codegen_utils import py_str_literal
from fluid_build.schedulers.airflow._assets import (
    NotAddressable,
    expose_asset_uri,
    normalize_dataset_uri,
)
from fluid_build.util.contract import (
    builds_writing_expose,
    get_builds,
    get_exposes,
    is_continuous_build,
    is_databricks_job_build,
    is_unscheduled_build,
)

#: Engine used for a scheduled build when the contract names none.
DEFAULT_ENGINE = "airflow"

#: The apply mode every scheduled run uses: DDL amend plus the build, never a
#: destructive mode.
APPLY_MODE = "amend-and-build"

#: What a keepalive DAG passes to ``fluid apply`` after the contract and ``--env``, before
#: ``--build-id <stream> --yes``. ``--mode reconcile`` runs no ``tofu apply``: it starts that one
#: stream again on its checkpoint when it died, within the restart budget the build's
#: ``execution.retries`` sets, and leaves a run cancelled on purpose stopped. It exits non-zero
#: when it cannot keep the stream running (drift from the contract, nothing deployed, the
#: restarts spent). The one place a keepalive's apply arguments are named.
KEEPALIVE_APPLY_ARGS: Tuple[str, ...] = ("--mode", "reconcile")

#: How often a keepalive DAG reconciles its stream, unless the build's
#: ``execution.orchestration.airflow.dagConfig.schedule`` names another cron.
KEEPALIVE_SCHEDULE = "*/5 * * * *"

#: How long one keepalive run may take before Airflow stops it; the next run tries again.
KEEPALIVE_TASK_TIMEOUT_S = 30 * 60

#: Contract path baked into the DAG when the caller cannot know the real one
#: (a bundle carries no source path). Matches fluid's own discovery default
#: and the generated CI pipelines' ``CONTRACT`` default.
DEFAULT_CONTRACT_PATH = "contract.fluid.yaml"

#: Run-time variable naming the product checkout on the Airflow worker.
PROJECT_DIR_ENV = "FLUID_PROJECT_DIR"

DEFAULT_RETRIES = 3
#: How long a Spark run may take when the build does not say (``execution.runtime.timeout``):
#: ``build_runners.spark._reconcile.DEFAULT_RUN_WAIT_S``.
DEFAULT_SPARK_WAIT_S = 6 * 3600
#: How long an apply DAG's task may run: the ``execution_timeout`` of every DAG here. A
#: Spark build's task gets this on top of its run's wait, so fluid cancels the run first.
APPLY_TASK_TIMEOUT_S = 3 * 3600
MAX_RETRIES = 10

#: Name prefixes of the variables that reach ``fluid apply`` from the worker
#: environment. With :data:`PASSTHROUGH_NAMES` this is the registry of what
#: fluid reads from its environment while it applies and builds: a test scans
#: every module ``fluid apply`` imports (build runners and providers included)
#: and fails on a variable it reads that neither lets through, so a new
#: ``os.getenv`` cannot be silently dropped on the worker.
PASSTHROUGH_PREFIXES = (
    # fluid's own settings (FLUID_STATE_BACKEND, FLUID_SECRETS_FILE, ...)
    "FLUID_",
    # cloud and warehouse credentials, projects and regions
    "AWS_",
    "GOOGLE_",
    "GCP_",
    "GCLOUD_",
    "CLOUDSDK_",
    "AZURE_",
    "ARM_",
    "SNOWFLAKE_",
    "DATABRICKS_",
    # libpq (PGHOST, PGPASSWORD, PGSSLMODE, ...) and the dbt profiles built
    # for postgres, redshift and athena
    "PG",
    "POSTGRES_",
    "REDSHIFT_",
    "ATHENA_",
    # vault:// secretRefs, under Vault's names and OpenBao's (VAULT_ADDR,
    # BAO_ADDR, BAO_TOKEN, BAO_NAMESPACE, BAO_CACERT ...)
    "VAULT_",
    "BAO_",
    # build engines and catalog registration
    "DBT_",
    "DLT_",
    "DATAHUB_",
    "DMM_",
    "ODCS_",
    "ODPS_",
    "TF_",
    "OPENLINEAGE_",
    "OTEL_",
)

#: Individual variables that reach ``fluid apply``: process basics, locale,
#: CA bundles, proxies, and the few unprefixed names fluid reads.
#: ``VIRTUAL_ENV`` is left out on purpose: on a worker it names Airflow's
#: environment (the apache/airflow image sets it to ``/home/airflow/.local``),
#: and fluid's python runner would then run builds with Airflow's interpreter
#: instead of the one fluid runs under.
PASSTHROUGH_NAMES = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "LANG",
    "LANGUAGE",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    "TMPDIR",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "GLUE_ROLE_ARN",
    "S3_STAGING_DIR",
    "S3_DATA_DIR",
    "GOOG_SERVICE_ACCOUNT_NAME",
    "TESTCONTAINERS_HOST_OVERRIDE",
    # python-dotenv's off switch: fluid turns it on itself (so dbt and
    # litellm never re-read the project's .env); a value the worker's
    # operator set is theirs to keep
    "PYTHON_DOTENV_DISABLED",
)

# Airflow's dag_id grammar is ``^[\w.-]+$`` with a 250 character cap.
_MAX_DAG_ID = 250

_CRON_PRESETS = frozenset(
    {"@once", "@hourly", "@daily", "@weekly", "@monthly", "@yearly", "@annually", "@midnight"}
)
# The five cron fields as croniter, which Airflow validates every cron
# schedule with, reads them: (name, lowest, highest, names). Day of week 7 is
# Sunday, like 0.
_CRON_FIELDS: Tuple[Tuple[str, int, int, Dict[str, int]], ...] = (
    ("minute", 0, 59, {}),
    ("hour", 0, 23, {}),
    ("day of month", 1, 31, {}),
    (
        "month",
        1,
        12,
        {
            name: number
            for number, name in enumerate(
                ("jan", "feb", "mar", "apr", "may", "jun")
                + ("jul", "aug", "sep", "oct", "nov", "dec"),
                start=1,
            )
        },
    ),
    (
        "day of week",
        0,
        7,
        {
            name: number
            for number, name in enumerate(("sun", "mon", "tue", "wed", "thu", "fri", "sat"))
        },
    ),
)
_DAY_OF_MONTH, _DAY_OF_WEEK = 2, 4
_CRON_NUMBER_RE = re.compile(r"[0-9]{1,2}")
_CRON_STEP_RE = re.compile(r"[1-9][0-9]{0,2}")
_CRON_NTH = frozenset("12345")
_TIMEZONE_RE = re.compile(r"[A-Za-z][A-Za-z0-9_+\-]*(?:/[A-Za-z0-9_+\-]+){0,3}")
_PATH_SEGMENT_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.\-]*")
_MAX_CONTRACT_PATH = 512
# The three ways a contract names a variable fluid reads, restricted to names
# a shell can hold: the ``{{ env.NAME }}`` placeholder the loader and providers
# resolve, the ``${NAME}`` placeholder the schema documents (dbt sources turn
# it into ``env_var``), and an ``env://NAME`` secretRef, which
# ``fluid_build.secrets`` reads straight from the environment.
_ENV_TEMPLATE_RE = re.compile(r"\{\{\s*env\.([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
_SHELL_TEMPLATE_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_ENV_SECRET_REF_RE = re.compile(r"\s*env\s*://\s*([A-Za-z_][A-Za-z0-9_]*)\s*", re.IGNORECASE)


class ScheduleRenderError(ValueError):
    """A contract value cannot be rendered into a safe, loadable DAG."""


@dataclass(frozen=True)
class ScheduledBuild:
    """One ``builds[]`` entry that declares a cron trigger."""

    build_id: str
    schedule: str
    timezone: str
    retries: int
    #: Seconds the task may run; ``None`` for the default three hours. A Spark build's run may
    #: take its ``execution.runtime.timeout``, and the task outlives that wait.
    task_timeout_s: Optional[int] = None


#: The Airflow trigger rule of a task, from its ``builds[].dependencies[].condition``.
#: ``always`` is Airflow's own: "No dependencies at all, run this task at any time", so the
#: task starts without waiting for the builds it names. ``completion`` waits for them to
#: finish, whatever their outcome. A dependency with no condition is ``success``.
TRIGGER_RULES = {"success": "all_success", "completion": "all_done", "always": "always"}


@dataclass(frozen=True)
class DagSchedule:
    """When a DAG runs: on a cron, when datasets update, or on either."""

    #: The cron (or Airflow preset); ``None`` for a DAG that runs only on its datasets.
    cron: Optional[str]
    timezone: str
    #: Asset URIs whose update starts a run; empty for a DAG on a cron alone.
    datasets: Tuple[str, ...] = ()
    #: ``all``: every dataset updated since the last run; ``any``: one of them.
    operator: str = "all"


@dataclass(frozen=True)
class BuildTask:
    """One build's node in a DAG: the task that runs ``fluid apply --build-id <id>``."""

    build_id: str
    retries: int
    #: Seconds the task may run; ``None`` for the default three hours (see ``ScheduledBuild``).
    task_timeout_s: Optional[int] = None
    #: The builds this one runs after, in the DAG's run order.
    upstream: Tuple[str, ...] = ()
    #: The Airflow trigger rule its dependencies' condition gives; ``None`` with no upstream.
    trigger_rule: Optional[str] = None
    #: The asset URIs of the exposes the build writes, declared as the task's ``outlets``.
    outlets: Tuple[str, ...] = ()


@dataclass(frozen=True)
class ScheduledDag:
    """One ``fluid apply`` DAG: the builds ``builds[].dependencies`` link, on their trigger."""

    #: The build whose trigger runs the DAG; it names the DAG and its file.
    build_id: str
    schedule: DagSchedule
    #: One per build, in run order: a build comes after every build it depends on.
    tasks: Tuple[BuildTask, ...]
    #: ``(build id, why)``: builds a task depends on that no DAG runs, so none waits for them.
    unwaited: Tuple[Tuple[str, str], ...] = ()
    #: A keepalive DAG: its one task reconciles an always-on stream (:data:`KEEPALIVE_APPLY_ARGS`).
    keepalive: bool = False


# Where a build stands in the schedule (:func:`_classify`).
_OUTSIDE = "outside"  # no scheduler starts it: a stream, a Flink job, a Databricks job
_ELSEWHERE = "elsewhere"  # execution.orchestration.engine names another engine, or none
_NO_TRIGGER = "no trigger"  # runs in the DAG of the builds it depends on, if any
_CRON = "schedule"
_DATASET = "dataset"
_CRON_OR_DATASET = "schedule_and_dataset"
_UNRENDERED = "unrendered"  # manual, event, timetable: no fluid apply DAG
_DAG_TRIGGERS = (_CRON, _DATASET, _CRON_OR_DATASET)


# ---------------------------------------------------------------------------
# Contract inspection
# ---------------------------------------------------------------------------


def _managed_lane(contract: Any) -> bool:
    """Whether forge's Spark runner runs this contract's Spark builds at all.

    It reads the contract through the managed lane, which exists from schema 0.7.7 on; an
    older contract's Spark build is refused there, so it keeps its ``fluid apply`` DAG.
    """
    builds = contract.get("builds") if isinstance(contract, dict) else None
    if not isinstance(builds, list) or not any(
        isinstance(b, dict) and b.get("engine") == "spark" for b in builds
    ):
        return True  # nothing to decide: avoid loading the schema manager
    from fluid_build.streaming.compiler import managed_lane_enabled

    return managed_lane_enabled(contract)


def _spark_kind(build: Dict[str, Any], managed: bool = True) -> Optional[str]:
    """``scheduled`` or ``streaming`` for a forge Spark build, ``None`` for any other build.

    A batch job and a stream with ``trigger.availableNow`` end on their own, so a scheduler
    starts them (the compiler's rule, ``fluid_build.spark.compiler.runs_to_completion``).
    """
    if build.get("engine") != "spark" or not managed:
        return None
    props = build.get("properties")
    if not isinstance(props, dict):
        return None
    parameters = props.get("parameters")
    if not isinstance(parameters, dict) or not isinstance(parameters.get("spark"), dict):
        return None
    from fluid_build.spark.compiler import runs_to_completion

    return "scheduled" if runs_to_completion(build) else "streaming"


def _classify(build: Dict[str, Any], managed: bool = True) -> Tuple[str, Dict[str, Any]]:
    """Where the build stands in the schedule, and its trigger (``{}`` when it has none)."""
    # A Flink job runs on its own platform, and a dbt build on Databricks on its
    # job's own trigger: a DAG that runs ``fluid apply`` for either would run
    # nothing and pass. Nor does a continuous build need one.
    if is_unscheduled_build(build):
        return _OUTSIDE, {}
    # A stream keeps running and ``fluid apply`` reconciles it; a scheduler has nothing to start.
    if _spark_kind(build, managed) == "streaming":
        return _OUTSIDE, {}
    execution = build.get("execution")
    execution = execution if isinstance(execution, dict) else {}
    # A build may name its own engine (``execution.orchestration.engine``);
    # ``none`` opts it out and any other engine is not Airflow's to render.
    own = execution.get("orchestration")
    if isinstance(own, dict) and own.get("engine") not in (None, "", DEFAULT_ENGINE):
        return _ELSEWHERE, {}
    trigger = execution.get("trigger")
    if not isinstance(trigger, dict):
        return _NO_TRIGGER, {}
    kind = trigger.get("type")
    if kind in (None, "schedule"):
        has_cron = bool(trigger.get("schedule") or trigger.get("cron"))
        return (_CRON if has_cron else _NO_TRIGGER), trigger
    if kind == "dependency":
        return _NO_TRIGGER, trigger
    if kind in (_DATASET, _CRON_OR_DATASET):
        return kind, trigger
    # ``manual``, ``event`` and ``timetable`` carry semantics a fluid apply DAG
    # would silently drop, so none of them gets one.
    return _UNRENDERED, trigger


def _outside_why(build: Dict[str, Any], managed: bool) -> str:
    """Why no DAG runs a build :func:`_classify` puts outside every scheduler."""
    if _spark_kind(build, managed) == "streaming":
        return "a Spark stream that fluid apply keeps running"
    if is_continuous_build(build):
        return "a job its platform keeps running (trigger.type: continuous)"
    if is_databricks_job_build(build):
        return "a Databricks job that runs on its own trigger"
    return "a job that runs on its own platform (a Flink job, for example)"


def _keeps_alive(build: Dict[str, Any], managed: bool) -> bool:
    """Whether a keepalive DAG watches the build: a forge Spark stream (one that is not
    ``trigger.availableNow``) that declares ``execution.retries``, on Airflow's engine. A build
    another platform runs (:func:`is_unscheduled_build`: ``trigger.type: continuous``, a Flink
    job) is that platform's to keep running, as :func:`_classify` has it."""
    if is_unscheduled_build(build) or _spark_kind(build, managed) != "streaming":
        return False
    execution = build.get("execution")
    if not isinstance(execution, dict) or not isinstance(execution.get("retries"), dict):
        return False
    own = execution.get("orchestration")
    return not (isinstance(own, dict) and own.get("engine") not in (None, "", DEFAULT_ENGINE))


def _keepalive_schedule(build: Dict[str, Any]) -> str:
    """The cron a keepalive DAG runs on: ``execution.orchestration.airflow.dagConfig.schedule``,
    else :data:`KEEPALIVE_SCHEDULE`."""
    own = (build.get("execution") or {}).get("orchestration")
    airflow = own.get("airflow") if isinstance(own, dict) else None
    config = airflow.get("dagConfig") if isinstance(airflow, dict) else None
    value = config.get("schedule") if isinstance(config, dict) else None
    if value in (None, ""):
        return KEEPALIVE_SCHEDULE
    where = f"{build.get('id')!r}: execution.orchestration.airflow.dagConfig.schedule"
    try:
        cron = _validate_schedule(value, str(build.get("id")))
    except ScheduleRenderError as exc:
        raise ScheduleRenderError(f"{where}: {exc}") from None
    if cron == "@once":
        raise ScheduleRenderError(
            f"{where}: a keepalive DAG reconciles the stream on a recurring cron, and @once "
            "is not one"
        )
    return cron


def _trigger_schedule(build: Dict[str, Any], managed: bool = True) -> Optional[Dict[str, Any]]:
    """The build's cron trigger, or ``None`` when it has none Airflow should run."""
    kind, trigger = _classify(build, managed)
    return trigger if kind == _CRON else None


def _keepalive_dags(
    contract: Dict[str, Any], builds: List[Dict[str, Any]], managed: bool
) -> List[ScheduledDag]:
    """One keepalive DAG per always-on stream that declares ``execution.retries``.

    Its one task runs ``fluid apply --mode reconcile`` for the stream every few minutes, so a
    stream a Spot interruption or a failed driver stopped starts again, within the restart
    budget the build's ``execution.retries`` sets. A stream is in no build graph (nothing waits
    for it), so this reads the builds alone. It has no outlets: a stream updates its tables all
    the time, and an asset event on every run would start each DAG scheduled on them.
    """
    out: List[ScheduledDag] = []
    for build in builds:
        if not _keeps_alive(build, managed):
            continue
        build_id = _task_build_id(build)
        _check_spark_ids(contract, build_id)
        out.append(
            ScheduledDag(
                build_id=build_id,
                schedule=DagSchedule(cron=_keepalive_schedule(build), timezone="UTC"),
                tasks=(BuildTask(build_id, 0, KEEPALIVE_TASK_TIMEOUT_S),),
                keepalive=True,
            )
        )
    return out


def has_scheduled_builds(contract: Dict[str, Any]) -> bool:
    """True when at least one build declares a trigger a DAG runs on (no validation): a cron,
    datasets, or both."""
    builds = contract.get("builds")
    if not isinstance(builds, list):
        return False
    managed = _managed_lane(contract)
    return any(
        isinstance(b, dict)
        and (_classify(b, managed)[0] in _DAG_TRIGGERS or _keeps_alive(b, managed))
        for b in builds
    )


def runs_only_outside_schedulers(contract: Dict[str, Any], *, keepalive: bool = True) -> bool:
    """True when the contract has builds and no scheduler starts any of them: each is a stream
    ``fluid apply`` keeps running, runs continuously, or runs on its own platform.

    A stream that declares ``execution.retries`` has a keepalive DAG, so it counts as scheduled.
    ``keepalive=False`` is for a renderer that has no keepalive DAG (``scaffold-composer``).
    """
    builds = [b for b in get_builds(contract) if isinstance(b, dict)]
    if not builds:
        return False
    managed = _managed_lane(contract)
    return all(
        _classify(b, managed)[0] == _OUTSIDE and not (keepalive and _keeps_alive(b, managed))
        for b in builds
    )


def has_explicit_tasks(contract: Dict[str, Any]) -> bool:
    """True when the contract hand-declares ``orchestration.tasks``."""
    orchestration = contract.get("orchestration")
    return isinstance(orchestration, dict) and bool(orchestration.get("tasks"))


def uses_fluid_apply_dags(contract: Dict[str, Any], *, engine: Optional[str]) -> bool:
    """Whether *engine* renders this contract as ``fluid apply`` DAGs.

    Yes when the engine is Airflow (or unset), the contract declares no
    explicit ``orchestration.tasks`` (those keep their hand-declared
    operators), and at least one build carries a trigger a DAG runs on
    (:func:`has_scheduled_builds`).
    """
    return (
        (engine or DEFAULT_ENGINE) == DEFAULT_ENGINE
        and not has_explicit_tasks(contract)
        and has_scheduled_builds(contract)
    )


def _cron_value_ok(token: str, index: int) -> bool:
    _, low, high, names = _CRON_FIELDS[index]
    if _CRON_NUMBER_RE.fullmatch(token):
        return low <= int(token) <= high
    return token in names


def _cron_element_ok(element: str, index: int) -> bool:
    """One comma-separated element: ``*``, ``N``, ``N-M``, any of those with
    ``/step``, ``L`` (last day of the month) or ``DAY#n`` (nth weekday)."""
    if index == _DAY_OF_MONTH and element == "l":
        return True
    if index == _DAY_OF_WEEK and "#" in element:
        day, _, nth = element.partition("#")
        return _cron_value_ok(day, index) and nth in _CRON_NTH
    base, slash, step = element.partition("/")
    if slash and not _CRON_STEP_RE.fullmatch(step):
        return False
    if base == "*":
        return True
    low, dash, high = base.partition("-")
    return _cron_value_ok(low, index) and (not dash or _cron_value_ok(high, index))


def _cron_field_ok(field: str, index: int) -> bool:
    text = field.lower()
    if text == "?":
        # croniter takes ``?`` alone, and only for the two day fields.
        return index in (_DAY_OF_MONTH, _DAY_OF_WEEK)
    elements = text.split(",")
    # croniter refuses a day-of-week list that mixes ``DAY#n`` with plain days.
    if index == _DAY_OF_WEEK and 0 < sum("#" in e for e in elements) < len(elements):
        return False
    return all(_cron_element_ok(e, index) for e in elements)


def _validate_schedule(value: Any, build_id: str) -> str:
    """Return the trigger's cron, or raise when Airflow would not load it.

    Airflow parses a cron ``schedule=`` with croniter and refuses the whole
    DAG file when it does not parse, so a schedule is checked here to the
    same grammar: an Airflow preset, or five fields within their ranges
    (month and weekday names, ``*``, lists, ranges, steps, ``?``, ``L`` in
    the day of month, ``DAY#1``..``DAY#5`` in the day of week). A sixth
    field is refused: croniter reads it as seconds, a Quartz cron puts the
    seconds first, and the two disagree on when the build runs.
    """
    if not isinstance(value, str):
        raise ScheduleRenderError(
            f"build {build_id!r}: trigger schedule must be a string, got {type(value).__name__}"
        )
    text = value.strip()
    if text.lower() in _CRON_PRESETS:
        return text.lower()
    fields = text.split()
    if len(fields) != len(_CRON_FIELDS):
        raise ScheduleRenderError(
            f"build {build_id!r}: trigger schedule {value!r} is not a five-field cron "
            "expression (minute hour day-of-month month day-of-week) or an Airflow preset "
            "such as @daily"
        )
    for index, field in enumerate(fields):
        if not _cron_field_ok(field, index):
            raise ScheduleRenderError(
                f"build {build_id!r}: trigger schedule {value!r}: {field!r} is not a valid "
                f"{_CRON_FIELDS[index][0]} field (values {_CRON_FIELDS[index][1]}-"
                f"{_CRON_FIELDS[index][2]})"
            )
    # croniter parses ``DAY#n`` next to a day of month, then never finds a
    # next run, so the scheduler fails on it after the DAG has loaded.
    if "#" in fields[_DAY_OF_WEEK] and fields[_DAY_OF_MONTH] not in ("*", "?"):
        raise ScheduleRenderError(
            f"build {build_id!r}: trigger schedule {value!r}: a DAY#n day of week needs "
            "* or ? as the day of month"
        )
    return " ".join(fields)


def _validate_timezone(value: Any, build_id: str) -> str:
    tz = "UTC" if value in (None, "") else value
    if not isinstance(tz, str) or not _TIMEZONE_RE.fullmatch(tz):
        raise ScheduleRenderError(f"build {build_id!r}: trigger timezone {value!r} is not valid")
    try:
        zoneinfo.ZoneInfo(tz)
    except (ValueError, OSError) as exc:  # ZoneInfoNotFoundError is a KeyError, not these
        # ``America`` or ``Etc`` names a directory of the tz database, not a zone.
        raise ScheduleRenderError(
            f"build {build_id!r}: trigger timezone {tz!r} is not an IANA zone"
        ) from exc
    except zoneinfo.ZoneInfoNotFoundError as exc:
        # A host without a tz database cannot judge; Airflow will.
        if zoneinfo.available_timezones():
            raise ScheduleRenderError(
                f"build {build_id!r}: trigger timezone {tz!r} is not an IANA zone"
            ) from exc
    return tz


def _retries(build: Dict[str, Any]) -> int:
    execution = build.get("execution") or {}
    policy = execution.get("retries") if isinstance(execution, dict) else None
    attempts = policy.get("maxAttempts") if isinstance(policy, dict) else None
    # ``bool`` is an ``int``; ``maxAttempts: true`` is not a count.
    if isinstance(attempts, bool) or not isinstance(attempts, int):
        return DEFAULT_RETRIES
    return max(0, min(attempts - 1, MAX_RETRIES))


#: The ids forge's Spark runner accepts (``fluid_build.spark.compiler``): narrower than the
#: ids a ``fluid apply`` DAG takes, so a Spark build whose id it would refuse on every run
#: is refused here, once, instead.
_SPARK_BUILD_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
_SPARK_PRODUCT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def _check_spark_ids(contract: Dict[str, Any], build_id: str) -> None:
    product = contract.get("id")
    if not _SPARK_BUILD_ID.fullmatch(build_id):
        raise ScheduleRenderError(
            f"Spark build id {build_id!r}: forge's Spark runner takes letters, digits, _ and "
            "-, starting with a letter or digit"
        )
    if not isinstance(product, str) or not _SPARK_PRODUCT_ID.fullmatch(product) or ".." in product:
        raise ScheduleRenderError(
            f"contract id {product!r}: a Spark build's product id takes letters, digits, ., _ "
            "and -, starting with a letter or digit, and no '..'"
        )


def _task_timeout_s(build: Dict[str, Any], build_id: str) -> int:
    """How long a Spark build's task may run: the apply's own :data:`APPLY_TASK_TIMEOUT_S`
    (``tofu`` and the rest run before the run starts) plus the run's wait
    (``execution.runtime.timeout``, else :data:`DEFAULT_SPARK_WAIT_S`), so fluid cancels the
    run before Airflow kills the task. A timeout it cannot read is refused, never guessed."""
    from fluid_build.spark.compiler import run_timeout_seconds

    try:
        wait = run_timeout_seconds(build)
    except ValueError as exc:
        raise ScheduleRenderError(f"build {build_id!r}: {exc}") from exc
    return APPLY_TASK_TIMEOUT_S + (wait or DEFAULT_SPARK_WAIT_S)


def _task_build_id(build: Dict[str, Any]) -> str:
    raw_id: Any = build.get("id")
    try:
        return validate_id(raw_id, kind="build.id")
    except ScheduleRenderError as exc:
        raise ScheduleRenderError(
            f"a scheduled build needs a valid id for --build-id: {exc}"
        ) from exc


def _dependencies(build: Dict[str, Any], build_id: Any) -> List[Tuple[str, str]]:
    """``(upstream build id, condition)`` for each ``builds[].dependencies`` entry, deduplicated."""
    raw = build.get("dependencies")
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ScheduleRenderError(
            f"build {build_id!r}: dependencies must be a list of {{buildId, condition}} entries"
        )
    out: List[Tuple[str, str]] = []
    for index, entry in enumerate(raw):
        where = f"build {build_id!r}: dependencies[{index}]"
        upstream = entry.get("buildId") if isinstance(entry, dict) else None
        if not isinstance(upstream, str) or not upstream:
            raise ScheduleRenderError(
                f"{where} names no buildId; set it to the id of the build this one runs after"
            )
        condition = entry.get("condition") or "success"
        if not isinstance(condition, str) or condition not in TRIGGER_RULES:
            raise ScheduleRenderError(
                f"{where}: condition {condition!r} is not one of success, completion, always"
            )
        if (upstream, condition) not in out:
            out.append((upstream, condition))
    return out


def _describe(schedule: Optional[DagSchedule], trigger: Mapping[str, Any]) -> str:
    """A trigger as an error message names it."""
    if schedule is None:
        return f"trigger.type {trigger.get('type')}"
    parts = []
    if schedule.cron is not None:
        parts.append(f"schedule {schedule.cron!r} ({schedule.timezone})")
    if schedule.datasets:
        parts.append(f"{schedule.operator} of datasets {', '.join(schedule.datasets)}")
    return " and ".join(parts)


def _check_trigger_shape(build_id: Any, kind: str, trigger: Mapping[str, Any]) -> None:
    """Refuse a trigger whose fields contradict its ``type``, saying which field to change."""
    cron_value = trigger.get("schedule") or trigger.get("cron")
    datasets = trigger.get("datasets")
    on_datasets = kind in (_DATASET, _CRON_OR_DATASET)
    if datasets and not on_datasets:
        raise ScheduleRenderError(
            f"build {build_id!r}: trigger.datasets needs trigger.type dataset (run when they "
            "update) or schedule_and_dataset (run on the schedule and when they update)"
        )
    if kind == _DATASET and cron_value:
        raise ScheduleRenderError(
            f"build {build_id!r}: trigger.type dataset runs when its datasets update and takes "
            "no schedule; set trigger.type: schedule_and_dataset to run on both"
        )
    if kind == _CRON_OR_DATASET and not cron_value:
        raise ScheduleRenderError(
            f"build {build_id!r}: trigger.type schedule_and_dataset needs trigger.schedule, the "
            "cron it runs on besides its datasets (or set trigger.type: dataset)"
        )
    if kind == _CRON_OR_DATASET and str(cron_value).strip().lower() == "@once":
        # Airflow runs ``@once`` with its own once-only timetable, which an asset-or-time
        # schedule cannot hold: ``CronTriggerTimetable("@once")`` loads, then never runs.
        raise ScheduleRenderError(
            f"build {build_id!r}: trigger.type schedule_and_dataset runs on a recurring cron, "
            "and @once is not one; give it a cron, or set trigger.type: dataset"
        )
    if on_datasets and (not isinstance(datasets, list) or not datasets):
        raise ScheduleRenderError(
            f"build {build_id!r}: trigger.type {kind} needs trigger.datasets, the datasets whose "
            "update starts a run"
        )
    if on_datasets and (trigger.get("datasetsOperator") or "all") not in ("all", "any"):
        raise ScheduleRenderError(
            f"build {build_id!r}: datasetsOperator {trigger.get('datasetsOperator')!r} is not all "
            "or any"
        )


def _run_order(members: List[int], upstream: Dict[int, List[Tuple[int, str]]], name) -> List[int]:
    """``members`` with every build after the builds it depends on; ties in ``builds[]`` order."""
    graph = {i: [j for j, _ in upstream.get(i, [])] for i in members}
    sorter: TopologicalSorter = TopologicalSorter(graph)
    try:
        sorter.prepare()
    except CycleError as exc:
        # Each node of the cycle is an upstream of the next: the order the builds would run in.
        path = " -> ".join(repr(name(i)) for i in exc.args[1])
        raise ScheduleRenderError(
            f"builds[].dependencies form a cycle: {path} (each runs after the one before it), "
            "so none of them could ever start. Remove one of these dependencies"
        ) from None
    order: List[int] = []
    while sorter.is_active():
        ready = sorted(sorter.get_ready())
        order.extend(ready)
        sorter.done(*ready)
    return order


def _check_asset_loops(dags: List[ScheduledDag]) -> None:
    """Refuse DAGs that would start each other forever through the assets they write."""
    writes = {d.build_id: {u for t in d.tasks for u in t.outlets} for d in dags}
    started_by: Dict[str, List[str]] = {}
    for dag in dags:
        for other in dags:
            shared = sorted(writes[other.build_id] & set(dag.schedule.datasets))
            if shared and other.build_id == dag.build_id:
                raise ScheduleRenderError(
                    f"the DAG of build {dag.build_id!r} writes {shared[0]}, which is also one of "
                    "its own trigger.datasets, so each run would start the next. Trigger it by "
                    "another build's output, or on a schedule. If its builds do not write that "
                    "expose, list what each writes in builds[].outputs: a build with no outputs "
                    "counts as writing every expose no build lists"
                )
            if shared:
                started_by.setdefault(dag.build_id, []).append(other.build_id)
    try:
        TopologicalSorter(started_by).prepare()
    except CycleError as exc:
        path = " -> ".join(repr(b) for b in exc.args[1])
        raise ScheduleRenderError(
            f"the DAGs of builds {path} start each other in a loop through the assets they "
            "write (each one's output is in the next one's trigger.datasets). Put one of them "
            "on a schedule instead"
        ) from None


class _BuildGraph:
    """The contract's builds as :func:`scheduled_dags` reads them: the dependencies between
    the builds a DAG runs, and the builds a dependency names that no DAG runs."""

    def __init__(self, contract: Dict[str, Any], builds: List[Dict[str, Any]]) -> None:
        self.contract = contract
        self.builds = builds
        self.managed = _managed_lane(contract)
        self.kinds = [_classify(b, self.managed) for b in builds]
        self.in_dags = [i for i, (k, _) in enumerate(self.kinds) if k not in (_OUTSIDE, _ELSEWHERE)]
        self.positions: Dict[str, List[int]] = {}
        for i, build in enumerate(builds):
            if isinstance(build.get("id"), str):
                self.positions.setdefault(build["id"], []).append(i)
        #: ``i -> [(j, condition)]``: build ``i`` runs after build ``j``.
        self.upstream: Dict[int, List[Tuple[int, str]]] = {}
        #: ``i -> [(build id, why)]``: what build ``i`` depends on that no DAG runs.
        self.unwaited: Dict[int, List[Tuple[str, str]]] = {}
        #: ``(where, expose id, writers)`` of each ``{productId, exposeId}`` dataset of this
        #: contract, checked once every DAG is known (:meth:`check_expose_refs`).
        self.expose_refs: List[Tuple[str, Any, List[Dict[str, Any]]]] = []
        #: Each expose's asset URI with the builds that write it, read once.
        self.assets: List[Tuple[Optional[str], List[Dict[str, Any]]]] = [
            (expose_asset_uri(e)[0], builds_writing_expose(_expose_id(e), builds))
            for e in _exposes(contract)
        ]
        for i in self.in_dags:
            self._link(i)

    def name(self, i: int) -> Any:
        return self.builds[i].get("id")

    def _link(self, i: int) -> None:
        # The id as written names the build in a message; only a build that gets a task needs
        # one ``--build-id`` takes (:meth:`_task`), as before dependencies were read.
        build, (kind, trigger) = self.builds[i], self.kinds[i]
        label = self.name(i)
        if trigger:
            _check_trigger_shape(label, kind, trigger)
        explicit = trigger.get("type") == "dependency"
        if build.get("dependencies") is None and not explicit:
            return
        for upstream_id, condition in _dependencies(build, label):
            j = self._upstream(label, upstream_id)
            if self.kinds[j][0] == _OUTSIDE:
                why = _outside_why(self.builds[j], self.managed)
                self.unwaited.setdefault(i, []).append((upstream_id, why))
            else:
                self.upstream.setdefault(i, []).append((j, condition))
        if explicit and not self.upstream.get(i):
            raise ScheduleRenderError(
                f"build {label!r}: trigger.type dependency runs it after the builds in its "
                "dependencies, and no DAG runs any of them; give it a trigger of its own"
            )

    def _upstream(self, build_id: Any, upstream_id: str) -> int:
        """The position of the build a dependency names, or what is wrong with the name."""
        where = f"build {build_id!r}: dependency {upstream_id!r}"
        if upstream_id == build_id:
            raise ScheduleRenderError(
                f"build {build_id!r} depends on itself; remove {build_id!r} from its dependencies"
            )
        found = self.positions.get(upstream_id, [])
        if not found:
            known = ", ".join(repr(b) for b in self.positions) or "none"
            raise ScheduleRenderError(
                f"{where} names no build in builds[] (build ids: {known}); use the id of the "
                "build it runs after"
            )
        if len(found) > 1:
            raise ScheduleRenderError(
                f"{where} names {len(found)} builds that share that id; give each build its own id"
            )
        if self.kinds[found[0]][0] == _ELSEWHERE:
            raise ScheduleRenderError(
                f"{where}: {upstream_id!r} is not run by an Airflow DAG (its "
                "execution.orchestration.engine names another engine, or none), so no task can "
                f"wait for it. Run both on one engine, or drop the dependency and give {build_id!r} "
                "a schedule of its own"
            )
        return found[0]

    def groups(self) -> List[List[int]]:
        """The builds dependencies link, in either direction: one DAG's builds each."""
        parent = {i: i for i in self.in_dags}

        def root(i: int) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        for i, ups in self.upstream.items():
            for j, _ in ups:
                a, b = root(i), root(j)
                parent[max(a, b)] = min(a, b)
        groups: Dict[int, List[int]] = {}
        for i in self.in_dags:
            groups.setdefault(root(i), []).append(i)
        return list(groups.values())

    def dag(self, members: List[int]) -> Optional[ScheduledDag]:
        """The DAG of one linked group; ``None`` when nothing starts it."""
        carriers = [i for i in members if self.kinds[i][0] != _NO_TRIGGER]
        if not carriers:
            return None  # no build of the group has a trigger
        schedule = self._shared_schedule(carriers)
        if schedule is None:
            return None  # manual, event or timetable: no fluid apply DAG, as for one build
        order = _run_order(members, self.upstream, self.name)
        waits: List[Tuple[str, str]] = []
        for i in order:
            waits.extend(w for w in self.unwaited.get(i, []) if w not in waits)
        return ScheduledDag(
            build_id=_task_build_id(self.builds[carriers[0]]),
            schedule=schedule,
            tasks=tuple(self._task(i, order) for i in order),
            unwaited=tuple(waits),
        )

    def _shared_schedule(self, carriers: List[int]) -> Optional[DagSchedule]:
        """The one trigger the builds of a group declare, or what to do about two."""
        schedules = {i: self._schedule(i) for i in carriers}

        def same_as(i: int) -> Tuple[Any, ...]:
            s = schedules[i]
            if s is None:
                return ("no dag", self.kinds[i][1].get("type"))
            # A cron reads its month and day names in any case, and a timezone matters only to
            # a cron: a DAG on datasets alone takes it for its start date.
            cron = s.cron.lower() if s.cron is not None else None
            return (cron, s.timezone if cron else None, frozenset(s.datasets), s.operator)

        first = carriers[0]
        for other in carriers[1:]:
            if same_as(other) != same_as(first):
                raise ScheduleRenderError(
                    f"builds {self.name(first)!r} and {self.name(other)!r} are linked by "
                    "builds[].dependencies, so they run in one DAG, but their triggers differ "
                    f"({_describe(schedules[first], self.kinds[first][1])} vs "
                    f"{_describe(schedules[other], self.kinds[other][1])}). One DAG runs on one "
                    "trigger: keep it on one build and remove the other's, or drop the "
                    f"dependency and give {self.name(other)!r} a dataset trigger on what "
                    f"{self.name(first)!r} writes"
                )
        return schedules[first]

    def _schedule(self, i: int) -> Optional[DagSchedule]:
        """The validated schedule of build ``i``'s own trigger; ``None`` for one no DAG runs on."""
        kind, trigger = self.kinds[i]
        if kind not in _DAG_TRIGGERS:
            return None
        build_id = _task_build_id(self.builds[i])
        timezone = _validate_timezone(trigger.get("timezone"), build_id)
        cron_value = trigger.get("schedule") or trigger.get("cron")
        cron = _validate_schedule(cron_value, build_id) if kind != _DATASET else None
        if kind == _CRON:
            return DagSchedule(cron=cron, timezone=timezone)
        uris: List[str] = []
        for index, item in enumerate(trigger["datasets"]):
            uri = self._dataset_uri(f"build {build_id!r}: trigger.datasets[{index}]", item)
            if uri not in uris:
                uris.append(uri)
        return DagSchedule(
            cron=cron,
            timezone=timezone,
            datasets=tuple(uris),
            operator=trigger.get("datasetsOperator") or "all",
        )

    def _dataset_uri(self, where: str, item: Any) -> str:
        if isinstance(item, str):
            try:
                return normalize_dataset_uri(item)
            except NotAddressable as exc:
                raise ScheduleRenderError(f"{where}: {exc}") from None
        if isinstance(item, dict):
            return self._expose_ref_uri(where, item)
        raise ScheduleRenderError(
            f"{where} must be an asset URI or {{productId, exposeId}} of an expose"
        )

    def _expose_ref_uri(self, where: str, ref: Mapping[str, Any]) -> str:
        """The asset URI of a ``{productId, exposeId}`` dataset: an expose of this contract."""
        product, expose_id = ref.get("productId"), ref.get("exposeId")
        if not (isinstance(product, str) and product and isinstance(expose_id, str) and expose_id):
            raise ScheduleRenderError(f"{where} needs both productId and exposeId")
        if product != self.contract.get("id"):
            raise ScheduleRenderError(
                f"{where} names {product}/{expose_id}, an expose of another product. A "
                "{productId, exposeId} dataset names an expose of this contract; for another "
                "product's, write its asset URI as a string (that product's fluid apply DAGs "
                "declare it in outlets=; an aws expose's is s3://<bucket>/<path>)"
            )
        exposes = _exposes(self.contract)
        expose = next((e for e in exposes if _expose_id(e) == expose_id), None)
        if expose is None:
            declared = ", ".join(repr(_expose_id(e)) for e in exposes) or "none"
            raise ScheduleRenderError(
                f"{where} names expose {expose_id!r}, which this contract does not declare "
                f"(exposes: {declared})"
            )
        uri, why = expose_asset_uri(expose)
        if uri is None:
            raise ScheduleRenderError(
                f"{where}: expose {expose_id!r} names no Airflow asset: {why}. Write the asset "
                "URI its data lands at as a string instead"
            )
        self.expose_refs.append((where, expose_id, builds_writing_expose(expose_id, self.builds)))
        return uri

    def check_expose_refs(self, dags: List[ScheduledDag]) -> None:
        """Refuse a dataset trigger on an expose of this contract that no task updates: every
        build that writes it runs outside any DAG, or only in a keepalive DAG, which declares no
        outlets, so the trigger would never fire."""
        tasked = {t.build_id for dag in dags if not dag.keepalive for t in dag.tasks}
        for where, expose_id, writers in self.expose_refs:
            if not writers or any(_expose_writer_id(b) in tasked for b in writers):
                continue
            names = ", ".join(repr(b.get("id")) for b in writers)
            if all(_keeps_alive(b, self.managed) for b in writers):
                raise ScheduleRenderError(
                    f"{where}: expose {expose_id!r} is written by {names}, whose keepalive DAG "
                    "declares no outlets (a stream updates its tables all the time), so no task "
                    "ever updates its asset and the trigger would never fire. Run this build on "
                    "a schedule (trigger.type: schedule)"
                )
            raise ScheduleRenderError(
                f"{where}: expose {expose_id!r} is written by {names}, which no DAG runs "
                f"({self._why_no_task(writers[0])}), so no task ever updates its asset and the "
                "trigger would never fire. Give that build a trigger, or run this one on a "
                "schedule (trigger.type: schedule)"
            )

    def _why_no_task(self, build: Dict[str, Any]) -> str:
        kind, trigger = _classify(build, self.managed)
        if kind == _OUTSIDE:
            return _outside_why(build, self.managed)
        if kind == _ELSEWHERE:
            return "execution.orchestration.engine names another engine, or none"
        if kind == _UNRENDERED:
            return f"its trigger.type is {trigger.get('type')}"
        return "it has no trigger, and no build it depends on has one"

    def _task(self, i: int, order: List[int]) -> BuildTask:
        build = self.builds[i]
        build_id = _task_build_id(build)
        conditions = {condition for _, condition in self.upstream.get(i, [])}
        if len(conditions) > 1:
            raise ScheduleRenderError(
                f"build {build_id!r} depends on builds with different conditions "
                f"({', '.join(sorted(conditions))}); Airflow gives a task one trigger rule, so "
                "give all of one build's dependencies the same condition"
            )
        spark = _spark_kind(build, self.managed) == "scheduled"
        if spark:
            _check_spark_ids(self.contract, build_id)
        upstream_ids = {j for j, _ in self.upstream.get(i, [])}
        return BuildTask(
            build_id=build_id,
            retries=_retries(build),
            task_timeout_s=_task_timeout_s(build, build_id) if spark else None,
            upstream=tuple(self.name(j) for j in order if j in upstream_ids),
            trigger_rule=TRIGGER_RULES[conditions.pop()] if conditions else None,
            outlets=self._outlets(build),
        )

    def _outlets(self, build: Dict[str, Any]) -> Tuple[str, ...]:
        """The asset URIs of the exposes the build writes (``util.contract.builds_writing_expose``);
        an expose that names no asset (:func:`expose_asset_uri`) is left out."""
        uris: List[str] = []
        for uri, writers in self.assets:
            if uri and uri not in uris and any(w is build for w in writers):
                uris.append(uri)
        return tuple(uris)


def _expose_writer_id(build: Mapping[str, Any]) -> Optional[str]:
    """A build's id when it is one a task can carry (a string), else ``None``."""
    build_id = build.get("id")
    return build_id if isinstance(build_id, str) else None


def _expose_id(expose: Mapping[str, Any]) -> Any:
    return expose.get("exposeId") or expose.get("id")


def _exposes(contract: Mapping[str, Any]) -> List[Dict[str, Any]]:
    return [e for e in get_exposes(contract) if isinstance(e, dict)]


def scheduled_dags(contract: Dict[str, Any]) -> List[ScheduledDag]:
    """Every ``fluid apply`` DAG the contract's builds make, validated for rendering.

    Raises :class:`ScheduleRenderError` when a build's id, trigger or dependencies cannot be
    rendered safely, saying what to change.
    """
    raw_builds = contract.get("builds")
    if not isinstance(raw_builds, list):
        return []
    graph = _BuildGraph(contract, [b for b in raw_builds if isinstance(b, dict)])
    dags = [dag for members in graph.groups() if (dag := graph.dag(members)) is not None]
    dags += _keepalive_dags(contract, graph.builds, graph.managed)
    seen: Set[str] = set()
    for task in (t for dag in dags for t in dag.tasks):
        if task.build_id in seen:
            raise ScheduleRenderError(
                f"two builds share the id {task.build_id!r}: fluid apply --build-id "
                f"{task.build_id} could not tell them apart, and Airflow refuses a DAG with two "
                "tasks of one id. Give each build its own id"
            )
        seen.add(task.build_id)
    graph.check_expose_refs(dags)
    _check_asset_loops(dags)
    return dags


def scheduled_builds(contract: Dict[str, Any]) -> List[ScheduledBuild]:
    """Every build that gets a task in a ``fluid apply`` DAG (:func:`scheduled_dags`), with the
    cron and timezone of its DAG (no cron: ``""``).

    Raises :class:`ScheduleRenderError` when a scheduled build's id, trigger or dependencies
    cannot be rendered safely.
    """
    return [
        ScheduledBuild(
            build_id=task.build_id,
            schedule=dag.schedule.cron or "",
            timezone=dag.schedule.timezone,
            retries=task.retries,
            task_timeout_s=task.task_timeout_s,
        )
        for dag in scheduled_dags(contract)
        for task in dag.tasks
    ]


def contract_env_names(contract: Any) -> List[str]:
    """Names of the variables the contract tells fluid to read, sorted.

    ``{{ env.NAME }}`` and ``${NAME}`` placeholders anywhere in the contract,
    and ``env://NAME`` secretRefs. Names only; values are read on the worker
    at run time.
    """
    names: Set[str] = set()
    stack: List[Any] = [contract]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
        elif isinstance(node, str):
            names.update(_ENV_TEMPLATE_RE.findall(node))
            names.update(_SHELL_TEMPLATE_RE.findall(node))
            secret_ref = _ENV_SECRET_REF_RE.fullmatch(node)
            if secret_ref:
                names.add(secret_ref.group(1))
    return sorted(names)


def validate_contract_path(raw: Any) -> str:
    """Return *raw* as a normalised project-relative POSIX path.

    The path is appended to ``$FLUID_PROJECT_DIR`` on the worker, so it must
    be relative, stay inside the project (no ``..``) and hold nothing a shell
    or a Jinja template could interpret.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise ScheduleRenderError("contract path must be a non-empty string")
    if len(raw) > _MAX_CONTRACT_PATH or "\\" in raw or raw.startswith("/"):
        raise ScheduleRenderError(
            f"contract path {raw!r} must be a relative POSIX path inside the project"
        )
    parts = [p for p in raw.split("/") if p not in ("", ".")]
    if not parts or not all(_PATH_SEGMENT_RE.fullmatch(p) for p in parts):
        raise ScheduleRenderError(
            f"contract path {raw!r} may only hold segments of [A-Za-z0-9_.-] that do "
            "not start with '.' or '-'"
        )
    return "/".join(parts)


def validate_id(raw: Any, *, kind: str) -> str:
    """:func:`validate_identifier` as a :class:`ScheduleRenderError`, with its own newline check.

    An id or env that ends in a newline names nothing fluid can find (``--env
    'aws\\n'`` silently applies no overlay). The shared grammar ends in
    ``\\Z`` and refuses one already; the check below still holds if it changes.
    """
    try:
        value = validate_identifier(raw, kind=kind)
    except IdentifierViolation as exc:
        raise ScheduleRenderError(str(exc)) from exc
    if "\n" in value:
        raise ScheduleRenderError(f"{kind} {raw!r} is not a valid identifier (it holds a newline)")
    return value


def validate_env_name(raw: Any) -> str:
    """Validate the overlay env the scheduled run passes to ``--env``."""
    return validate_id(raw, kind="env")


def dag_id_for(product_id: str, build_id: str, env: Optional[str] = None) -> str:
    """``<product>__<build>``, or ``<product>__<env>__<build>`` for an env's DAG.

    The env is part of the id because one contract deployed to two clouds
    (``--env aws`` and ``--env gcp`` overlays) is one product id: both DAGs
    had the same id, and in one Airflow the second to parse replaced the
    first. Airflow keys run history on the id, so an env-bound DAG from an
    earlier release starts a new history under its new id.
    """
    dag_id = f"{product_id}__{env}__{build_id}" if env else f"{product_id}__{build_id}"
    if len(dag_id) > _MAX_DAG_ID:
        raise ScheduleRenderError(
            f"dag_id {dag_id!r} exceeds Airflow's {_MAX_DAG_ID}-character limit; "
            "shorten the contract or build id"
        )
    return dag_id


def schedule_scope_for(product_id: str, env: Optional[str] = None) -> str:
    """The directory an env's DAGs live in, under the schedule artifacts root.

    ``<product>`` with no env, ``<product>__<env>`` with one. ``fluid
    schedule-sync --delete-scope product`` mirrors each such directory onto
    the same-named one at the scheduler, deleting what the source lacks, so
    the aws and the gcp DAGs of one product need a directory each or each
    sync deletes the other's.
    """
    return f"{product_id}__{env}" if env else product_id


def dag_filename_for(build_id: str) -> str:
    # A plain module name: dots in a file name make Airflow import the DAG
    # under a dotted module path.
    return re.sub(r"[^A-Za-z0-9_]", "_", build_id) + "_dag.py"


def rendered_dag_of(path: Path) -> Optional[Tuple[str, str]]:
    """``(PRODUCT_ID, FLUID_ENV_NAME)`` of a DAG file this module rendered, else ``None``.

    Read from the file's syntax tree, never by importing it: its docstring opens as
    :func:`render_scheduled_dag` writes one, and it assigns both names a string.
    """
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 1_000_000:
            return None
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, ValueError):
        return None
    doc = ast.get_docstring(tree, clean=False) or ""
    if not doc.startswith(("Scheduled fluid build, ", "Scheduled fluid builds, ")):
        return None
    facts: Dict[str, str] = {}
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id in ("PRODUCT_ID", "FLUID_ENV_NAME")
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            facts[node.targets[0].id] = node.value.value
    if set(facts) != {"PRODUCT_ID", "FLUID_ENV_NAME"}:
        return None
    return facts["PRODUCT_ID"], facts["FLUID_ENV_NAME"]


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

_PREFIX_PATTERN = "|".join(f"{prefix}*" for prefix in PASSTHROUGH_PREFIXES)

# Static: identical in every DAG, and free of contract-derived text. Must not
# contain ``{{``, ``{%`` or ``{#`` (Airflow renders it as a Jinja template)
# and must not end in ``.sh`` / ``.bash`` (Airflow would read it as a path).
#
# fluid is started through ``env -i`` with only the variables collected in
# ``pass``: unsetting the rest instead would miss environment entries whose
# names are not shell identifiers (``db.password``, which Kubernetes allows),
# because bash hands those to its children without listing them.
_SCRIPT_HEAD = (
    "set -euo pipefail",
    ': "${FLUID_PROJECT_DIR:?set FLUID_PROJECT_DIR on the Airflow worker to the '
    'directory that holds the product checkout}"',
    'keep=" ' + " ".join(PASSTHROUGH_NAMES) + " ${FLUID_DAG_CONTRACT_ENV:-}"
    ' ${FLUID_DAG_ENV_PASSTHROUGH:-} "',
    "pass=()",
    "for name in $(compgen -e); do",
    '  case "$name" in',
    # Airflow's own namespace never passes, even when a contract's
    # ``{{ env.X }}`` or FLUID_DAG_ENV_PASSTHROUGH names it.
    "    AIRFLOW*) continue ;;",
    f"    {_PREFIX_PATTERN}) ;;",
    '    *) case "$keep" in *" $name "*) ;; *) continue ;; esac ;;',
    "  esac",
    '  pass+=("$name=${!name}")',
    "done",
    'cd -- "$FLUID_PROJECT_DIR"',
    'contract="$PWD/$FLUID_DAG_CONTRACT"',
    'if [ ! -f "$contract" ]; then',
    '  echo "fluid: no contract at $contract (FLUID_PROJECT_DIR + $FLUID_DAG_CONTRACT)" >&2',
    "  exit 2",
    "fi",
)

_APPLY_TAIL = (
    'set -- apply "$contract"',
    'if [ -n "$FLUID_DAG_ENV" ]; then set -- "$@" --env "$FLUID_DAG_ENV"; fi',
    f'set -- "$@" --mode {APPLY_MODE} --build-id "$FLUID_DAG_BUILD_ID" --yes',
)

_SCRIPT_END = (
    'bin="${FLUID_BIN:-fluid}"',
    # env(1) would read a name holding "=" as one more variable, not the program.
    'case "$bin" in *=*) echo "fluid: FLUID_BIN may not contain =" >&2; exit 2 ;; esac',
    'exec env -i ${pass[@]+"${pass[@]}"} "$bin" "$@"',
)


BASH_SCRIPT_LINES = _SCRIPT_HEAD + _APPLY_TAIL + _SCRIPT_END


def _apply_args(args: Tuple[str, ...], build_ref: str) -> str:
    """``args``, then ``--build-id <build_ref> --yes``: one space apart, however many ``args``."""
    return " ".join((*args, "--build-id", build_ref, "--yes"))


_KEEPALIVE_TAIL = (
    'set -- apply "$contract"',
    'if [ -n "$FLUID_DAG_ENV" ]; then set -- "$@" --env "$FLUID_DAG_ENV"; fi',
    'set -- "$@" ' + _apply_args(KEEPALIVE_APPLY_ARGS, '"$FLUID_DAG_BUILD_ID"'),
)

#: The script of a keepalive DAG's task: :data:`BASH_SCRIPT_LINES` but for the apply arguments.
KEEPALIVE_SCRIPT_LINES = _SCRIPT_HEAD + _KEEPALIVE_TAIL + _SCRIPT_END


def _wrap(words: List[str], indent: str, width: int = 76) -> str:
    lines: List[str] = []
    current = indent
    for word in words:
        if len(current) + len(word) + 1 > width and current.strip():
            lines.append(current.rstrip())
            current = indent
        current += word + " "
    if current.strip():
        lines.append(current.rstrip())
    return "\n".join(lines)


_DOC_REQUIREMENTS = (
    "Worker requirements:\n"
    "\n"
    "- FLUID_PROJECT_DIR names the directory that holds the product checkout,\n"
    "  the directory CI ran the pipeline from.\n"
    "- `fluid` is on PATH, or FLUID_BIN names the executable.\n"
    "- The credentials the apply needs are in the worker environment. Only\n"
    "  these reach the fluid process:\n"
    "\n" + _wrap(list(PASSTHROUGH_NAMES), "    ") + "\n\n"
    "  every name matching\n"
    "\n" + _wrap([f"{prefix}*" for prefix in PASSTHROUGH_PREFIXES], "    ") + "\n\n"
    "  the variables the contract reads (CONTRACT_ENV_NAMES), and any names\n"
    "  listed, space separated, in FLUID_DAG_ENV_PASSTHROUGH. fluid starts with\n"
    "  nothing else, and never with a variable whose name starts AIRFLOW.\n"
)

_ONE_BUILD_RUN = (
    "Every run executes this on the Airflow worker, with the constants below\n"
    "filled in (`--env` is left out when FLUID_ENV_NAME is empty):\n"
    "\n"
    '    cd "$FLUID_PROJECT_DIR"\n'
    '    fluid apply "$FLUID_PROJECT_DIR/$CONTRACT_PATH" --env "$FLUID_ENV_NAME" \\\\\n'
    '        --mode amend-and-build --build-id "$BUILD_ID" --yes\n'
)

_KEEPALIVE_RUN = (
    "Every run reconciles one always-on stream on the Airflow worker, with the\n"
    "constants below filled in (`--env` is left out when FLUID_ENV_NAME is empty):\n"
    "\n"
    '    cd "$FLUID_PROJECT_DIR"\n'
    '    fluid apply "$FLUID_PROJECT_DIR/$CONTRACT_PATH" --env "$FLUID_ENV_NAME" \\\\\n'
    "        " + _apply_args(KEEPALIVE_APPLY_ARGS, '"$BUILD_ID"') + "\n"
    "\n"
    "It runs no tofu apply. When the stream died (a Spot interruption, a failed\n"
    "driver), it starts again on its checkpoint, within the restart budget the\n"
    "build's execution.retries sets, so the task itself never retries. The run\n"
    "fails when the stream cannot be kept running: it drifted from the contract,\n"
    "nothing is deployed, the job config the last apply deployed cannot be read,\n"
    "its cluster ended, or the restarts are spent. It can also fail when this\n"
    "worker's FLUID_SPARK_* settings or forge release differ from the CI apply's.\n"
    "A run cancelled on purpose stays stopped until fluid apply starts it. It\n"
    "runs on SCHEDULE, one run at a time, with no catch-up. The build's\n"
    "execution.orchestration.airflow.dagConfig.schedule sets SCHEDULE.\n"
)

_BUILDS_RUN = (
    "Each task runs one build on the Airflow worker, with the constants below\n"
    "filled in (`--env` is left out when FLUID_ENV_NAME is empty) and the\n"
    "task's own build, its task_id, as the build id:\n"
    "\n"
    '    cd "$FLUID_PROJECT_DIR"\n'
    '    fluid apply "$FLUID_PROJECT_DIR/$CONTRACT_PATH" --env "$FLUID_ENV_NAME" \\\\\n'
    "        --mode amend-and-build --build-id <task_id> --yes\n"
)

_TRIGGER_RULES_NOTE = (
    "BUILD_ID is the build whose trigger runs this DAG. A build that depends on others, "
    "as its contract's builds[].dependencies say, starts once they succeeded (trigger rule "
    "all_success, condition: success), once they finished whatever their outcome (all_done, "
    "condition: completion), or at once, without waiting for them (always, condition: "
    "always)."
)


def _paragraph(text: str) -> str:
    return _wrap(text.split(), "", 72)


def _doc(title: str, run: str, notes: List[str]) -> str:
    """The DAG file's docstring, also its ``doc_md``. Only fluid's own text and validated
    ids go in it: a contract value that could close the triple quote never does."""
    return (
        f'"""{title}, generated by fluid. Edit the contract, not this file.\n'
        "\n"
        f"{run}"
        "\n" + "".join(f"{_paragraph(note)}\n\n" for note in notes) + _DOC_REQUIREMENTS + '"""'
    )


def _schedule_note(schedule: DagSchedule) -> Optional[str]:
    if not schedule.datasets:
        return None
    if schedule.operator == "any":
        update = "any asset in DATASETS is updated"
    else:
        update = "every asset in DATASETS has been updated since its last run"
    when = "on SCHEDULE, and also when" if schedule.cron is not None else "when"
    return (
        f"This DAG runs {when} {update}. An asset is updated when a task that declares it in "
        "its outlets succeeds, in any DAG of this Airflow."
    )


def _plain_id(build_id: Any) -> str:
    """``build_id`` for the DAG's documentation when it is a plain identifier, else a stand-in.

    No task runs the builds this names, so their ids were never checked; ``doc_md`` is Markdown,
    which Airflow's UI turns into HTML, and only the identifier grammar keeps markup out of it."""
    try:
        return validate_identifier(build_id, kind="build.id")
    except IdentifierViolation:
        return "a build whose id is not a plain identifier"


def _unwaited_note(unwaited: Tuple[Tuple[str, str], ...]) -> Optional[str]:
    if not unwaited:
        return None
    names = "; ".join(f"{_plain_id(build_id)} ({why})" for build_id, why in unwaited)
    return (
        f"A build here depends on these, but no DAG runs them, so no task waits for them: {names}."
    )


def _timeout_expr(task_timeout_s: Optional[int]) -> str:
    return f"timedelta(seconds={int(task_timeout_s)})" if task_timeout_s else "timedelta(hours=3)"


def _asset_imports(schedule: DagSchedule) -> str:
    """The ``Asset`` imports a DAG with outlets or datasets needs, Airflow 3 first."""
    if schedule.datasets and schedule.cron is not None:
        any_name = (
            (", AssetAny", ", DatasetAny as AssetAny") if schedule.operator == "any" else ("", "")
        )
        return (
            "from airflow.timetables.trigger import CronTriggerTimetable\n"
            "\n"
            "try:  # Airflow 3\n"
            f"    from airflow.sdk import Asset{any_name[0]}\n"
            "    from airflow.timetables.assets import AssetOrTimeSchedule\n"
            "\n"
            "    def asset_or_time_schedule(timetable, assets):\n"
            "        return AssetOrTimeSchedule(timetable=timetable, assets=assets)\n"
            "\n"
            "except ImportError:  # Airflow 2.9+\n"
            f"    from airflow.datasets import Dataset as Asset{any_name[1]}\n"
            "    from airflow.timetables.datasets import DatasetOrTimeSchedule\n"
            "\n"
            "    def asset_or_time_schedule(timetable, assets):\n"
            "        return DatasetOrTimeSchedule(timetable=timetable, datasets=assets)\n"
            "\n"
        )
    if schedule.datasets and schedule.operator == "any":
        return (
            "try:  # Airflow 3\n"
            "    from airflow.sdk import Asset, AssetAny\n"
            "except ImportError:  # Airflow 2.9+\n"
            "    from airflow.datasets import Dataset as Asset, DatasetAny as AssetAny\n"
            "\n"
        )
    return (
        "try:  # Airflow 3\n"
        "    from airflow.sdk import Asset\n"
        "except ImportError:  # Airflow 2.x\n"
        "    from airflow.datasets import Dataset as Asset\n"
        "\n"
    )


def _schedule_expr(schedule: DagSchedule) -> str:
    if not schedule.datasets:
        return "SCHEDULE"
    if schedule.operator == "any":
        assets = "AssetAny(*(Asset(uri) for uri in DATASETS))"
    else:
        assets = "[Asset(uri) for uri in DATASETS]"
    if schedule.cron is None:
        return assets
    return (
        "asset_or_time_schedule(\n"
        "        CronTriggerTimetable(SCHEDULE, timezone=TIMEZONE),\n"
        f"        {assets},\n"
        "    )"
    )


def render_build_task(task: BuildTask, *, only: bool, keepalive: bool = False) -> str:
    """The source of one build's node: a BashOperator that runs ``fluid apply --build-id``.

    This is the one function that decides what runs a build; the DAG's schedule and its edges
    are written around it. The node of a DAG with several builds is ``tasks[<build id>]``, and
    the edges refer to it by that key alone, so anything that sets ``tasks[<build id>]`` (a task
    group, for one) can stand in for the BashOperator. A DAG's ``only`` task is the
    ``fluid_apply`` task the DAG of one build has always had, reading ``BUILD_ID``.
    """
    lit = py_str_literal
    if only:
        name = '"fluid_keepalive"' if keepalive else '"fluid_apply"'
        head, task_id, build_id = "    BashOperator(\n", name, "BUILD_ID"
    else:
        head = f"    tasks[{lit(task.build_id)}] = BashOperator(\n"
        task_id = build_id = lit(task.build_id)
    extra = ""
    if not only:
        extra += f"        retries={int(task.retries)},\n"
        extra += f"        execution_timeout={_timeout_expr(task.task_timeout_s)},\n"
        if task.trigger_rule:
            extra += f"        trigger_rule={lit(task.trigger_rule)},\n"
    if task.outlets:
        extra += (
            "        outlets=[\n"
            + "".join(f"            Asset({lit(uri)}),\n" for uri in task.outlets)
            + "        ],\n"
        )
    return (
        f"{head}"
        f"        task_id={task_id},\n"
        "        bash_command=BASH_COMMAND,\n"
        "        env={\n"
        '            "FLUID_DAG_CONTRACT": CONTRACT_PATH,\n'
        '            "FLUID_DAG_ENV": FLUID_ENV_NAME,\n'
        f'            "FLUID_DAG_BUILD_ID": {build_id},\n'
        '            "FLUID_DAG_CONTRACT_ENV": CONTRACT_ENV_NAMES,\n'
        "        },\n"
        "        append_env=True,\n"
        "        skip_on_exit_code=None,\n"
        f"{extra}"
        "    )\n"
    )


def _edges(tasks: Tuple[BuildTask, ...]) -> str:
    lit = py_str_literal
    lines = []
    for task in tasks:
        if not task.upstream:
            continue
        nodes = [f"tasks[{lit(u)}]" for u in task.upstream]
        left = nodes[0] if len(nodes) == 1 else "[" + ", ".join(nodes) + "]"
        lines.append(f"    {left} >> tasks[{lit(task.build_id)}]\n")
    return "".join(lines)


_IMPORTS = (
    "from datetime import timedelta\n"
    "\n"
    "import pendulum\n"
    "\n"
    "try:  # Airflow 3\n"
    "    from airflow.sdk import DAG\n"
    "except ImportError:  # Airflow 2.x\n"
    "    from airflow import DAG\n"
    "\n"
    "try:  # Airflow 3, or Airflow 2 with apache-airflow-providers-standard\n"
    "    from airflow.providers.standard.operators.bash import BashOperator\n"
    "except ImportError:  # Airflow 2.x core operator\n"
    "    from airflow.operators.bash import BashOperator\n"
    "\n"
)


def _dag_doc(product_id: str, dag: ScheduledDag) -> Tuple[str, str]:
    """The DAG file's docstring and the DAG's ``description``."""
    if dag.keepalive:
        return (
            _doc("Scheduled fluid build", _KEEPALIVE_RUN, []),
            f"keep fluid stream {product_id} {dag.build_id} running",
        )
    only = len(dag.tasks) == 1
    notes = [] if only else [_TRIGGER_RULES_NOTE]
    notes += [n for n in (_schedule_note(dag.schedule), _unwaited_note(dag.unwaited)) if n]
    if only:
        return (
            _doc("Scheduled fluid build", _ONE_BUILD_RUN, notes),
            f"fluid apply {product_id} --build-id {dag.tasks[0].build_id}",
        )
    builds = ", ".join(t.build_id for t in dag.tasks)
    return (
        _doc("Scheduled fluid builds", _BUILDS_RUN, notes),
        f"fluid apply {product_id} --build-id each of {builds}",
    )


def _schedule_constants(dag: ScheduledDag) -> str:
    lit = py_str_literal
    schedule = dag.schedule
    out = f"SCHEDULE = {lit(schedule.cron)}\n" if schedule.cron is not None else ""
    if schedule.datasets:
        out += "DATASETS = [\n" + "".join(f"    {lit(uri)},\n" for uri in schedule.datasets) + "]\n"
    out += f"TIMEZONE = {lit(schedule.timezone)}\n"
    if len(dag.tasks) == 1:
        out += f"RETRIES = {int(dag.tasks[0].retries)}\n"
    return out


def _dag_tasks(dag: ScheduledDag) -> Tuple[str, str]:
    """The DAG's ``default_args`` entries and the source of its tasks and edges."""
    if len(dag.tasks) == 1:
        task = dag.tasks[0]
        default_args = (
            '        "owner": "fluid",\n'
            '        "retries": RETRIES,\n'
            '        "retry_delay": timedelta(minutes=5),\n'
            f'        "execution_timeout": {_timeout_expr(task.task_timeout_s)},\n'
        )
        return default_args, render_build_task(task, only=True, keepalive=dag.keepalive)
    nodes = "".join(render_build_task(t, only=False) for t in dag.tasks)
    default_args = '        "owner": "fluid",\n        "retry_delay": timedelta(minutes=5),\n'
    return default_args, f"    tasks = {{}}\n{nodes}\n{_edges(dag.tasks)}"


def render_scheduled_dag(
    *,
    product_id: str,
    dag: ScheduledDag,
    env: Optional[str],
    contract_path: str,
    env_names: List[str],
) -> str:
    """Render one DAG file (:func:`scheduled_dags`). Inputs must already be validated."""
    lit = py_str_literal
    doc, description = _dag_doc(product_id, dag)
    default_args, body = _dag_tasks(dag)
    uses_assets = bool(dag.schedule.datasets) or any(t.outlets for t in dag.tasks)
    lines = KEEPALIVE_SCRIPT_LINES if dag.keepalive else BASH_SCRIPT_LINES
    script = "".join(f"        {lit(line)},\n" for line in lines)
    return (
        f"{doc}\n"
        "\n"
        f"{_IMPORTS}"
        f"{_asset_imports(dag.schedule) if uses_assets else ''}"
        f"PRODUCT_ID = {lit(product_id)}\n"
        f"BUILD_ID = {lit(dag.build_id)}\n"
        f"CONTRACT_PATH = {lit(contract_path)}\n"
        f"FLUID_ENV_NAME = {lit(env or '')}\n"
        f"CONTRACT_ENV_NAMES = {lit(' '.join(env_names))}\n"
        f"{_schedule_constants(dag)}"
        "\n"
        'BASH_COMMAND = "\\n".join(\n'
        "    [\n"
        f"{script}"
        "    ]\n"
        ")\n"
        "\n"
        "with DAG(\n"
        f"    dag_id={lit(dag_id_for(product_id, dag.build_id, env))},\n"
        f"    description={lit(description)},\n"
        f"    schedule={_schedule_expr(dag.schedule)},\n"
        "    start_date=pendulum.datetime(2026, 1, 1, tz=TIMEZONE),\n"
        "    catchup=False,\n"
        "    max_active_runs=1,\n"
        "    default_args={\n"
        f"{default_args}"
        "    },\n"
        '    tags=["fluid", PRODUCT_ID[:100]],\n'
        "    doc_md=__doc__,\n"
        ") as dag:\n"
        f"{body}"
    )


def render_dag(
    *,
    product_id: str,
    build: ScheduledBuild,
    env: Optional[str],
    contract_path: str,
    env_names: List[str],
) -> str:
    """Render the one-task DAG of a build on a cron. Inputs must already be validated."""
    return render_scheduled_dag(
        product_id=product_id,
        dag=ScheduledDag(
            build_id=build.build_id,
            schedule=DagSchedule(cron=build.schedule, timezone=build.timezone),
            tasks=(BuildTask(build.build_id, build.retries, build.task_timeout_s),),
        ),
        env=env,
        contract_path=contract_path,
        env_names=env_names,
    )


def render_fluid_apply_dags(
    contract: Dict[str, Any],
    *,
    env: Optional[str],
    contract_path: str,
) -> Dict[str, str]:
    """Render every DAG of :func:`scheduled_dags`: ``{filename: source}``.

    ``env`` is the overlay env every run passes to ``fluid apply --env``
    (``None`` or empty omits the flag). ``contract_path`` is the contract's
    path relative to ``$FLUID_PROJECT_DIR``.
    """
    raw_id: Any = contract.get("id")
    product_id = validate_id(raw_id, kind="contract.id")
    env_value = validate_env_name(env) if env else None
    rel_path = validate_contract_path(contract_path)
    env_names = contract_env_names(contract)

    files: Dict[str, str] = {}
    for dag in scheduled_dags(contract):
        name = dag_filename_for(dag.build_id)
        if name in files:
            raise ScheduleRenderError(
                f"builds {dag.build_id!r} and another scheduled build map to the same "
                f"DAG file {name!r}; give them ids that differ in more than '.' or '-'"
            )
        files[name] = render_scheduled_dag(
            product_id=product_id,
            dag=dag,
            env=env_value,
            contract_path=rel_path,
            env_names=env_names,
        )
    return files


__all__ = [
    "APPLY_MODE",
    "BASH_SCRIPT_LINES",
    "DEFAULT_CONTRACT_PATH",
    "DEFAULT_ENGINE",
    "PASSTHROUGH_NAMES",
    "PASSTHROUGH_PREFIXES",
    "PROJECT_DIR_ENV",
    "TRIGGER_RULES",
    "BuildTask",
    "DagSchedule",
    "ScheduleRenderError",
    "ScheduledBuild",
    "ScheduledDag",
    "contract_env_names",
    "dag_filename_for",
    "dag_id_for",
    "has_explicit_tasks",
    "has_scheduled_builds",
    "render_build_task",
    "render_dag",
    "render_fluid_apply_dags",
    "render_scheduled_dag",
    "rendered_dag_of",
    "KEEPALIVE_APPLY_ARGS",
    "KEEPALIVE_SCHEDULE",
    "KEEPALIVE_SCRIPT_LINES",
    "KEEPALIVE_TASK_TIMEOUT_S",
    "runs_only_outside_schedulers",
    "schedule_scope_for",
    "scheduled_builds",
    "scheduled_dags",
    "uses_fluid_apply_dags",
    "validate_contract_path",
    "validate_env_name",
    "validate_id",
]
