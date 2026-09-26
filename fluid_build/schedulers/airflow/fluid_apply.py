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
  that did not come from the DAG author. They are also validated to a grammar
  with no quotes, ``$``, ``{``, ``%``, ``#`` or whitespace, so neither the
  shell nor Airflow's Jinja rendering of ``bash_command`` and ``env`` can
  interpret them.
* No secret is ever written into the DAG. The env dict holds ids and the
  NAMES of the ``{{ env.X }}`` variables the contract reads; the values
  come from the worker's environment at run time. fluid is started with
  ``env -i`` and only the variables ``fluid apply`` needs; a name starting
  ``AIRFLOW`` never passes, even when the contract asks for it, so the
  Airflow worker's own secrets (``AIRFLOW__*``, ``AIRFLOW_CONN_*``, the
  Fernet key) never reach the fluid process.
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
from typing import Any, Dict, List, Optional, Set

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

#: Variable families that reach ``fluid apply`` from the worker environment:
#: fluid's own settings and the credentials of the platforms it deploys to.
PASSTHROUGH_FAMILIES = (
    "FLUID",
    "AWS",
    "GOOGLE",
    "GCP",
    "GCLOUD",
    "CLOUDSDK",
    "AZURE",
    "ARM",
    "SNOWFLAKE",
    "DATABRICKS",
    "DBT",
    "TF",
    "OPENLINEAGE",
    "OTEL",
)

#: Individual variables that reach ``fluid apply``: process basics, locale,
#: CA bundles and proxies.
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
)

# Airflow's dag_id grammar is ``^[\w.-]+$`` with a 250 character cap.
_MAX_DAG_ID = 250

_CRON_PRESETS = frozenset(
    {"@once", "@hourly", "@daily", "@weekly", "@monthly", "@yearly", "@annually", "@midnight"}
)
_CRON_FIELD_RE = re.compile(r"^[0-9A-Za-z*/,?#\-]+$")
_TIMEZONE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_+\-]*(?:/[A-Za-z0-9_+\-]+){0,3}$")
_PATH_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]*$")
_MAX_CONTRACT_PATH = 512
# Same placeholder grammar the providers resolve (``{{ env.NAME }}``),
# restricted to names a shell can hold.
_ENV_TEMPLATE_RE = re.compile(r"\{\{\s*env\.([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


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


def _validate_schedule(value: Any, build_id: str) -> str:
    if not isinstance(value, str):
        raise ScheduleRenderError(
            f"build {build_id!r}: trigger schedule must be a string, got {type(value).__name__}"
        )
    text = value.strip()
    if text.lower() in _CRON_PRESETS:
        return text.lower()
    fields = text.split()
    if len(fields) not in (5, 6) or not all(_CRON_FIELD_RE.match(f) for f in fields):
        raise ScheduleRenderError(
            f"build {build_id!r}: trigger schedule {value!r} is not a cron expression "
            "(5 or 6 fields) or an Airflow preset such as @daily"
        )
    return " ".join(fields)


def _validate_timezone(value: Any, build_id: str) -> str:
    tz = "UTC" if value in (None, "") else value
    if not isinstance(tz, str) or not _TIMEZONE_RE.match(tz):
        raise ScheduleRenderError(f"build {build_id!r}: trigger timezone {value!r} is not valid")
    try:
        zoneinfo.ZoneInfo(tz)
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
            build_id = validate_identifier(raw_id, kind="build.id")
        except IdentifierViolation as exc:
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
    """Names of the ``{{ env.NAME }}`` variables the contract reads, sorted.

    Names only; values are read on the worker at run time.
    """
    names: Set[str] = set()
    stack: List[Any] = [contract]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
        elif isinstance(node, str) and "{{" in node:
            names.update(_ENV_TEMPLATE_RE.findall(node))
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
    if not parts or not all(_PATH_SEGMENT_RE.match(p) for p in parts):
        raise ScheduleRenderError(
            f"contract path {raw!r} may only hold segments of [A-Za-z0-9_.-] that do "
            "not start with '.' or '-'"
        )
    return "/".join(parts)


def validate_env_name(raw: Any) -> str:
    """Validate the overlay env the scheduled run passes to ``--env``."""
    try:
        return validate_identifier(raw, kind="env")
    except IdentifierViolation as exc:
        raise ScheduleRenderError(str(exc)) from exc


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

_FAMILY_PATTERN = "|".join(f"{family}_*" for family in PASSTHROUGH_FAMILIES)

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
    f"    {_FAMILY_PATTERN}) ;;",
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
    "  the families\n"
    "\n" + _wrap([f"{family}_*" for family in PASSTHROUGH_FAMILIES], "    ") + "\n\n"
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
    try:
        product_id = validate_identifier(raw_id, kind="contract.id")
    except IdentifierViolation as exc:
        raise ScheduleRenderError(str(exc)) from exc
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
    "PASSTHROUGH_FAMILIES",
    "PASSTHROUGH_NAMES",
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
]
