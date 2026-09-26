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

"""Airflow DAGs that run a scheduled build through ``fluid apply``.

A build that declares ``builds[].execution.trigger.schedule`` gets one DAG.
Each DAG run executes, on the Airflow worker::

    cd "$FLUID_PROJECT_DIR"
    fluid apply "$FLUID_PROJECT_DIR/<contract path>" [--env <env>] \\
        --mode amend-and-build --build-id <build id> --yes

which is the same OpenTofu-backed apply the pipeline's stage 7 runs, so a
cloud target keeps one state (``FLUID_STATE_BACKEND``) whether the run came
from CI or from the scheduler. The contract path and ``--env`` are fixed when
the DAG is generated; the project directory is read on the worker at run
time, because the checkout lives somewhere else there than it does in CI.

Rendering rules:

* Airflow 3 first: ``airflow.sdk.DAG``, ``BashOperator`` from
  ``airflow.providers.standard``, ``schedule=``. Guarded imports keep the
  file importable on Airflow 2.6+ (``schedule=`` needs 2.4,
  ``skip_on_exit_code`` needs 2.6).
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

import re
import zoneinfo
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Tuple

from fluid_build.build_runners._ids import IdentifierViolation, validate_identifier
from fluid_build.providers.common.codegen_utils import py_str_literal

#: Engine used for a scheduled build when the contract names none.
DEFAULT_ENGINE = "airflow"

#: The apply mode every scheduled run uses: DDL amend plus the build, never a
#: destructive mode.
APPLY_MODE = "amend-and-build"

#: Contract path baked into the DAG when the caller cannot know the real one
#: (a bundle carries no source path). Matches fluid's own discovery default
#: and the generated CI pipelines' ``CONTRACT`` default.
DEFAULT_CONTRACT_PATH = "contract.fluid.yaml"

#: Run-time variable naming the product checkout on the Airflow worker.
PROJECT_DIR_ENV = "FLUID_PROJECT_DIR"

DEFAULT_RETRIES = 3
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
    # vault:// secretRefs (VAULT_ADDR, VAULT_TOKEN)
    "VAULT_",
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
# ``resolve_secret_ref`` reads straight from the environment.
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


# ---------------------------------------------------------------------------
# Contract inspection
# ---------------------------------------------------------------------------


def _trigger_schedule(build: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The build's cron trigger, or ``None`` when Airflow should not schedule it."""
    execution = build.get("execution")
    if not isinstance(execution, dict):
        return None
    # A build may name its own engine (``execution.orchestration.engine``);
    # ``none`` opts it out and any other engine is not Airflow's to render.
    own = execution.get("orchestration")
    if isinstance(own, dict) and own.get("engine") not in (None, "", DEFAULT_ENGINE):
        return None
    trigger = execution.get("trigger")
    if not isinstance(trigger, dict):
        return None
    # ``schedule_and_dataset`` / ``timetable`` / ``event`` carry semantics a
    # plain cron DAG would silently drop, so only a plain schedule trigger
    # (or one with no type, which the schema allows) is rendered.
    if trigger.get("type") not in (None, "schedule"):
        return None
    if not (trigger.get("schedule") or trigger.get("cron")):
        return None
    return trigger


def has_scheduled_builds(contract: Dict[str, Any]) -> bool:
    """True when at least one build declares a cron trigger (no validation)."""
    builds = contract.get("builds")
    if not isinstance(builds, list):
        return False
    return any(isinstance(b, dict) and _trigger_schedule(b) is not None for b in builds)


def has_explicit_tasks(contract: Dict[str, Any]) -> bool:
    """True when the contract hand-declares ``orchestration.tasks``."""
    orchestration = contract.get("orchestration")
    return isinstance(orchestration, dict) and bool(orchestration.get("tasks"))


def uses_fluid_apply_dags(contract: Dict[str, Any], *, engine: Optional[str]) -> bool:
    """Whether *engine* renders this contract as ``fluid apply`` DAGs.

    Yes when the engine is Airflow (or unset), the contract declares no
    explicit ``orchestration.tasks`` (those keep their hand-declared
    operators), and at least one build carries a cron trigger.
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
    except ValueError as exc:  # ZoneInfoNotFoundError is a KeyError, not this
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


def scheduled_builds(contract: Dict[str, Any]) -> List[ScheduledBuild]:
    """Return every build with a cron trigger, validated for rendering.

    Raises :class:`ScheduleRenderError` when a scheduled build's id,
    schedule or timezone cannot be rendered safely.
    """
    out: List[ScheduledBuild] = []
    builds = contract.get("builds")
    if not isinstance(builds, list):
        return out
    for build in builds:
        if not isinstance(build, dict):
            continue
        trigger = _trigger_schedule(build)
        if trigger is None:
            continue
        raw_id: Any = build.get("id")
        try:
            build_id = validate_id(raw_id, kind="build.id")
        except ScheduleRenderError as exc:
            raise ScheduleRenderError(
                f"a scheduled build needs a valid id for --build-id: {exc}"
            ) from exc
        out.append(
            ScheduledBuild(
                build_id=build_id,
                schedule=_validate_schedule(
                    trigger.get("schedule") or trigger.get("cron"), build_id
                ),
                timezone=_validate_timezone(trigger.get("timezone"), build_id),
                retries=_retries(build),
            )
        )
    return out


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
    """:func:`validate_identifier`, refusing a trailing newline as well.

    The shared grammar ends in ``$``, which also matches before a final
    ``\\n``; an id or env that carries one names nothing fluid can find
    (``--env 'aws\\n'`` silently applies no overlay).
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


def dag_id_for(product_id: str, build_id: str) -> str:
    dag_id = f"{product_id}__{build_id}"
    if len(dag_id) > _MAX_DAG_ID:
        raise ScheduleRenderError(
            f"dag_id {dag_id!r} exceeds Airflow's {_MAX_DAG_ID}-character limit; "
            "shorten the contract or build id"
        )
    return dag_id


def dag_filename_for(build_id: str) -> str:
    # A plain module name: dots in a file name make Airflow import the DAG
    # under a dotted module path.
    return re.sub(r"[^A-Za-z0-9_]", "_", build_id) + "_dag.py"


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
BASH_SCRIPT_LINES = (
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
    'set -- apply "$contract"',
    'if [ -n "$FLUID_DAG_ENV" ]; then set -- "$@" --env "$FLUID_DAG_ENV"; fi',
    f'set -- "$@" --mode {APPLY_MODE} --build-id "$FLUID_DAG_BUILD_ID" --yes',
    'bin="${FLUID_BIN:-fluid}"',
    # env(1) would read a name holding "=" as one more variable, not the program.
    'case "$bin" in *=*) echo "fluid: FLUID_BIN may not contain =" >&2; exit 2 ;; esac',
    'exec env -i ${pass[@]+"${pass[@]}"} "$bin" "$@"',
)


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


_DAG_DOC = (
    '"""Scheduled fluid build, generated by fluid. Edit the contract, not this file.\n'
    "\n"
    "Every run executes this on the Airflow worker, with the constants below\n"
    "filled in (`--env` is left out when FLUID_ENV_NAME is empty):\n"
    "\n"
    '    cd "$FLUID_PROJECT_DIR"\n'
    '    fluid apply "$FLUID_PROJECT_DIR/$CONTRACT_PATH" --env "$FLUID_ENV_NAME" \\\\\n'
    '        --mode amend-and-build --build-id "$BUILD_ID" --yes\n'
    "\n"
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
    '"""'
)


def render_dag(
    *,
    product_id: str,
    build: ScheduledBuild,
    env: Optional[str],
    contract_path: str,
    env_names: List[str],
) -> str:
    """Render one DAG file. Inputs must already be validated."""
    lit = py_str_literal
    script = "".join(f"        {lit(line)},\n" for line in BASH_SCRIPT_LINES)
    return (
        f"{_DAG_DOC}\n"
        "\n"
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
        f"PRODUCT_ID = {lit(product_id)}\n"
        f"BUILD_ID = {lit(build.build_id)}\n"
        f"CONTRACT_PATH = {lit(contract_path)}\n"
        f"FLUID_ENV_NAME = {lit(env or '')}\n"
        f"CONTRACT_ENV_NAMES = {lit(' '.join(env_names))}\n"
        f"SCHEDULE = {lit(build.schedule)}\n"
        f"TIMEZONE = {lit(build.timezone)}\n"
        f"RETRIES = {int(build.retries)}\n"
        "\n"
        'BASH_COMMAND = "\\n".join(\n'
        "    [\n"
        f"{script}"
        "    ]\n"
        ")\n"
        "\n"
        "with DAG(\n"
        f"    dag_id={lit(dag_id_for(product_id, build.build_id))},\n"
        f"    description={lit(f'fluid apply {product_id} --build-id {build.build_id}')},\n"
        "    schedule=SCHEDULE,\n"
        "    start_date=pendulum.datetime(2026, 1, 1, tz=TIMEZONE),\n"
        "    catchup=False,\n"
        "    max_active_runs=1,\n"
        "    default_args={\n"
        '        "owner": "fluid",\n'
        '        "retries": RETRIES,\n'
        '        "retry_delay": timedelta(minutes=5),\n'
        '        "execution_timeout": timedelta(hours=3),\n'
        "    },\n"
        '    tags=["fluid", PRODUCT_ID[:100]],\n'
        "    doc_md=__doc__,\n"
        ") as dag:\n"
        "    BashOperator(\n"
        '        task_id="fluid_apply",\n'
        "        bash_command=BASH_COMMAND,\n"
        "        env={\n"
        '            "FLUID_DAG_CONTRACT": CONTRACT_PATH,\n'
        '            "FLUID_DAG_ENV": FLUID_ENV_NAME,\n'
        '            "FLUID_DAG_BUILD_ID": BUILD_ID,\n'
        '            "FLUID_DAG_CONTRACT_ENV": CONTRACT_ENV_NAMES,\n'
        "        },\n"
        "        append_env=True,\n"
        "        skip_on_exit_code=None,\n"
        "    )\n"
    )


def render_fluid_apply_dags(
    contract: Dict[str, Any],
    *,
    env: Optional[str],
    contract_path: str,
) -> Dict[str, str]:
    """Render one DAG per scheduled build: ``{filename: source}``.

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
    for build in scheduled_builds(contract):
        name = dag_filename_for(build.build_id)
        if name in files:
            raise ScheduleRenderError(
                f"builds {build.build_id!r} and another scheduled build map to the same "
                f"DAG file {name!r}; give them ids that differ in more than '.' or '-'"
            )
        files[name] = render_dag(
            product_id=product_id,
            build=build,
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
    "ScheduleRenderError",
    "ScheduledBuild",
    "contract_env_names",
    "dag_filename_for",
    "dag_id_for",
    "has_explicit_tasks",
    "has_scheduled_builds",
    "render_dag",
    "render_fluid_apply_dags",
    "scheduled_builds",
    "uses_fluid_apply_dags",
    "validate_contract_path",
    "validate_env_name",
    "validate_id",
]
