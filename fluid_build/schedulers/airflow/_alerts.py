# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The alert routes of a product (:mod:`fluid_build.alerts`), written into one Airflow DAG.

Both DAG renderers use this (:mod:`fluid_build.schedulers.airflow.fluid_apply` and the
``orchestration`` renderer in :mod:`fluid_build.schedulers.airflow`). What a DAG gets:

* an import per notifier class its routes use, a module of an Airflow provider or of a
  plugin's package;
* ``alert(notifier, **kwargs)``: a callback that builds a NEW notifier each time it runs.
  ``BaseNotifier`` renders its template fields in place ("This mutates the attributes in-place
  and is irreversible", its docstring says), so one instance shared by the DAG's tasks would
  send the first alert's text for every later one;
* ``ALERT_TASK_ARGS``, merged into ``default_args``: ``on_failure_callback`` (a task failed
  for good) and ``on_retry_callback`` (it failed and will retry);
* ``ALERT_DAG_ARGS``, passed to ``DAG``: ``on_success_callback`` (a run succeeded) and the SLA:
  on Airflow 2 every task's ``sla`` with the DAG's ``sla_miss_callback``; on Airflow 3.1+,
  which has no SLAs, one ``DeadlineAlert`` per route on the run's queued time, whose
  ``AsyncCallback`` builds the notifier in the triggerer. Deadline alerts are experimental in
  Airflow 3, so they are built inside ``try``: an Airflow whose deadline API changed loads the
  DAG without them and logs why, and never fails the import. Airflow 3.0 has neither.

Every keyword argument is a literal (:func:`py_literal`); every string in it is forge's own
text or a contract value checked to hold no template syntax.
"""

from __future__ import annotations

import keyword
import math
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from fluid_build.alerts.builtin import duration_words
from fluid_build.alerts.model import NotifierCall
from fluid_build.alerts.routes import AlertPlan, notifier_calls, requirements
from fluid_build.providers.common.codegen_utils import py_str_literal

#: Names a DAG file defines or imports, which a notifier class is aliased away from.
_TAKEN = frozenset(
    {
        "AIRFLOW_VERSION",
        "ALERT_DAG_ARGS",
        "ALERT_SLA",
        "ALERT_TASK_ARGS",
        "Asset",
        "AssetAny",
        "AsyncCallback",
        "BashOperator",
        "DAG",
        "copy",
        "DeadlineAlert",
        "DeadlineReference",
        "PythonOperator",
        "ZoneInfo",
        "alert",
        "dag",
        "datetime",
        "json",
        "logger",
        "logging",
        "pendulum",
        "tasks",
        "timedelta",
    }
)

_ALERT_HELPER = '''def alert(notifier, /, **kwargs):
    """An Airflow callback that sends one alert through a new ``notifier(**kwargs)``.

    A notifier renders its template fields in place, so one instance shared by the tasks
    of this DAG would repeat the first alert's text; each call gets its own copy of the
    arguments too. ``deepcopy`` is bound here, before a task of this file can take the
    name ``copy``.
    """
    deepcopy = copy.deepcopy

    def callback(*args):
        notifier(**deepcopy(kwargs))(*args)

    return callback
'''

_DEADLINE_IMPORT = (
    "try:  # Airflow 3.2+\n"
    "    from airflow.sdk import AsyncCallback, DeadlineAlert, DeadlineReference\n"
    "except ImportError:\n"
    "    try:  # Airflow 3.1\n"
    "        from airflow.sdk.definitions.deadline import (\n"
    "            AsyncCallback,\n"
    "            DeadlineAlert,\n"
    "            DeadlineReference,\n"
    "        )\n"
    "    except ImportError:  # Airflow 2, or 3.0: no deadline alerts\n"
    "        DeadlineAlert = None\n"
)


@dataclass(frozen=True)
class RenderedAlerts:
    """The parts of one DAG file that carry its alerts; all empty when it has none.

    When :attr:`active`, ``definitions`` sets ``ALERT_TASK_ARGS``, which the renderer merges
    into ``default_args``, and ``ALERT_DAG_ARGS``, which it passes to ``DAG``. ``notes`` are
    paragraphs for the DAG's documentation.
    """

    imports: str = ""
    definitions: str = ""
    notes: Tuple[str, ...] = ()
    #: ``definitions`` logs through ``logging``, which the file must import.
    needs_logging: bool = False

    @property
    def active(self) -> bool:
        """Whether the DAG has any alert."""
        return bool(self.definitions)


def py_literal(value: Any, indent: str = "") -> str:
    """``value`` (str, int, float, bool, None, and lists and string-keyed dicts of them) as
    Python source; a non-empty list or dict takes one line per item, indented under
    ``indent``. Strings go through :func:`py_str_literal`."""
    inner = indent + "    "
    if value is None or isinstance(value, bool):
        return repr(value)
    if isinstance(value, int):
        return str(int(value))
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("a notifier argument is not a finite number")
        return repr(value)
    if isinstance(value, str):
        return py_str_literal(value)
    if isinstance(value, (list, tuple)):
        if not value:
            return "[]"
        items = "".join(f"{inner}{py_literal(item, inner)},\n" for item in value)
        return f"[\n{items}{indent}]"
    if isinstance(value, Mapping):
        if not value:
            return "{}"
        items = "".join(
            f"{inner}{py_str_literal(str(key))}: {py_literal(item, inner)},\n"
            for key, item in value.items()
        )
        return f"{{\n{items}{indent}}}"
    raise ValueError(f"a notifier argument holds a {type(value).__name__}, not a literal")


def _aliases(calls: Sequence[NotifierCall]) -> Dict[Tuple[str, str], str]:
    """``{(module, class): name in the DAG}``: the class name, unless the DAG already uses it."""
    out: Dict[Tuple[str, str], str] = {}
    used = set(_TAKEN)
    for module, name in sorted({(c.module, c.name) for c in calls}):
        alias = name
        count = 1
        while alias in used or keyword.iskeyword(alias):
            count += 1
            alias = f"{name}_{count}"
        used.add(alias)
        out[(module, name)] = alias
    return out


def _import_lines(aliases: Mapping[Tuple[str, str], str]) -> str:
    lines = []
    for (module, name), alias in sorted(aliases.items()):
        suffix = "" if alias == name else f" as {alias}"
        lines.append(f"from {module} import {name}{suffix}\n")
    return "".join(lines)


def _call(call: NotifierCall, alias: str, indent: str) -> str:
    """``alert(Notifier, key=value, ...)``, one argument per line."""
    inner = indent + "    "
    args = "".join(
        f"{inner}{key}={py_literal(value, inner)},\n" for key, value in call.kwargs.items()
    )
    return f"alert(\n{inner}{alias},\n{args}{indent})"


def _callbacks(calls: Sequence[NotifierCall], aliases: Mapping[Any, str], indent: str) -> str:
    inner = indent + "    "
    items = "".join(f"{inner}{_call(c, aliases[(c.module, c.name)], inner)},\n" for c in calls)
    return f"[\n{items}{indent}]"


def _args(args: Mapping[str, Sequence[NotifierCall]], aliases: Mapping[Any, str]) -> str:
    """``{"on_failure_callback": [alert(...), ...], ...}``, or ``{}``."""
    if not args:
        return "{}"
    items = "".join(
        f"    {py_str_literal(key)}: {_callbacks(calls, aliases, '    ')},\n"
        for key, calls in args.items()
    )
    return "{\n" + items + "}"


def _deadline(call: NotifierCall, alias: str, indent: str) -> str:
    """One ``DeadlineAlert`` whose callback builds ``call``'s notifier in the triggerer."""
    inner = indent + "    "
    kwargs = py_literal(dict(call.kwargs), inner + "    ")
    return (
        f"DeadlineAlert(\n"
        f"{inner}reference=DeadlineReference.DAGRUN_QUEUED_AT,\n"
        f"{inner}interval=ALERT_SLA,\n"
        f"{inner}callback=AsyncCallback(\n"
        f"{inner}    {alias},\n"
        f"{inner}    kwargs={kwargs},\n"
        f"{inner}),\n"
        f"{indent})"
    )


def _notes(plan: AlertPlan, sla_calls: Sequence[NotifierCall]) -> Tuple[str, ...]:
    from fluid_build.alerts.plugins import kind_for

    routes = []
    packs = sorted({r.origin[len("org default ") :] for r in plan.routes if r.origin != "contract"})
    for route in plan.routes:
        events = [e for e in route.events if e != "sla_miss" or plan.sla_seconds is not None]
        if not events:
            continue
        conn = route.connection or getattr(kind_for(route.kind), "default_connection", None)
        via = f", through Airflow connection {conn}" if conn else ""
        routes.append(f"{route.name} ({route.kind}, on {', '.join(events)}{via})")
    need, deadline = requirements(plan)
    source = "the contract's alerts.routes"
    if packs:
        source += " and the default routes of " + ", ".join(packs)
    notes = [
        f"Alerts, from {source}: "
        + "; ".join(routes)
        + ". The URLs, keys and passwords are in those Airflow connections, never in this "
        "file. The notifiers run where Airflow runs callbacks: the workers, the scheduler's "
        "DAG processor and, for deadline alerts, the triggerer, which all need "
        + ", ".join(need)
        + ". A webhook URL that holds its key keeps it in the connection's host, which "
        "Airflow's secrets masker does not hide: put the key in the connection's password too, "
        "and the masker hides it wherever it is logged."
    ]
    if sla_calls:
        words = duration_words(int(plan.sla_seconds or 0))
        note = (
            f"SLA: {words}. On Airflow 2.7+ every task has that sla, from the end of the run's "
            "data interval, and a miss calls the DAG's sla_miss_callback (Airflow 2.6's "
            "notifiers cannot take it). Airflow 3 has no "
            "SLAs: on 3.1+ each route gets a deadline "
            f"alert {words} after the run was queued, which needs "
            + ", ".join(deadline or need)
            + " in the triggerer; on 3.0 a missed SLA alerts no one."
        )
        if any(not c.supports_async for c in sla_calls):
            note += " A route whose notifier has no async_notify alerts on Airflow 2 only."
        notes.append(note)
    return tuple(notes)


def render_alerts(plan: AlertPlan, *, dag_id: str, product_id: str) -> RenderedAlerts:
    """The alerts of the DAG ``dag_id``. ``dag_id`` and ``product_id`` must be validated
    identifiers. Raises :class:`fluid_build.alerts.AlertRouteError` (a ``ValueError``) when a
    route cannot be rendered."""
    by_event = {
        event: notifier_calls(plan, event, dag_id=dag_id, product_id=product_id)
        for event in ("failure", "retry", "success", "sla_miss")
    }
    every = [call for calls in by_event.values() for call in calls]
    if not every:
        return RenderedAlerts()
    aliases = _aliases(every)
    sla_calls = by_event["sla_miss"]
    imports = "import copy\n\n" + _import_lines(aliases)
    if sla_calls:
        imports += "\nfrom airflow import __version__ as AIRFLOW_VERSION\n\n" + _DEADLINE_IMPORT
    task_args = {
        key: calls
        for key, calls in (
            ("on_failure_callback", by_event["failure"]),
            ("on_retry_callback", by_event["retry"]),
        )
        if calls
    }
    dag_args = {"on_success_callback": by_event["success"]} if by_event["success"] else {}
    out: List[str] = [
        "# Alerts (alerts.routes): Airflow's notifiers, one new instance per callback. The\n"
        + "# connections they name hold the URLs, keys and passwords.\n",
        _ALERT_HELPER,
        "\n",
    ]
    out.append(f"ALERT_TASK_ARGS = {_args(task_args, aliases)}\n")
    out.append(f"ALERT_DAG_ARGS = {_args(dag_args, aliases)}\n")
    if sla_calls:
        async_calls = [c for c in sla_calls if c.supports_async]
        out.append(
            f"ALERT_SLA = timedelta(seconds={int(plan.sla_seconds or 0)})\n"
            'if AIRFLOW_VERSION.startswith("2.") and not AIRFLOW_VERSION.startswith("2.6."):\n'
            "    # Airflow 2.7+: every task's SLA, and the DAG's sla_miss_callback (2.6's\n"
            "    # BaseNotifier takes one argument, not the callback's five).\n"
            '    ALERT_TASK_ARGS["sla"] = ALERT_SLA\n'
            f'    ALERT_DAG_ARGS["sla_miss_callback"] = {_callbacks(sla_calls, aliases, "    ")}\n'
        )
        if async_calls:
            deadlines = "".join(
                f"            {_deadline(c, aliases[(c.module, c.name)], '            ')},\n"
                for c in async_calls
            )
            out.append(
                "elif DeadlineAlert is not None:\n"
                "    # Airflow 3.1+ has no SLAs: a deadline alert per route, ALERT_SLA after the\n"
                "    # run was queued. The API is experimental, so a change to it (or a notifier\n"
                "    # it refuses) costs this DAG its deadline alerts, never its import.\n"
                "    try:\n"
                '        ALERT_DAG_ARGS["deadline"] = [\n'
                f"{deadlines}"
                "        ]\n"
                "    except Exception as exc:  # noqa: BLE001 - the API may change any way\n"
                "        logging.getLogger(__name__).warning(\n"
                '            "fluid: this Airflow refused the deadline alerts (%s); a missed SLA "\n'
                '            "alerts no one",\n'
                "            type(exc).__name__,\n"
                "        )\n"
            )
    return RenderedAlerts(
        imports=imports,
        definitions="".join(out),
        notes=_notes(plan, sla_calls),
        needs_logging=bool(sla_calls),
    )


def alert_plan_or_none(contract: Mapping[str, Any]) -> Optional[AlertPlan]:
    """The contract's alert plan (its routes and the org packs' defaults), or ``None`` when
    it has none. Raises :class:`fluid_build.alerts.AlertRouteError` on a bad route."""
    from fluid_build.alerts.routes import read_alert_plan

    plan = read_alert_plan(contract)
    return plan if plan.routes else None


__all__ = ["RenderedAlerts", "alert_plan_or_none", "py_literal", "render_alerts"]
