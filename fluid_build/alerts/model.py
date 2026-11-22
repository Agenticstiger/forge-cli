# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The alert routes of a contract, the alerts they send, and the base class of a route kind.

A route names who hears about an event of the DAGs forge generates for a product (a task
failed, a task failed and will retry, a run succeeded, a run missed its SLA) and how: a kind
(``email``, ``slack``, ``webhook``, ``pagerduty`` or a plugin's) and the Airflow connection that
holds the address, URL or key. The secret stays in that connection, which Airflow's secrets
backend serves; a route never carries one.

A kind is an :class:`AlertNotifier`: it names an Airflow notifier class (a ``BaseNotifier``
subclass the Airflow workers can import) and builds its keyword arguments for one
:class:`Alert`. The DAG instantiates the notifier with those arguments when the event
happens, so the sending is Airflow's own. Forge's four kinds are written this way, and a
package adds one through the ``fluid_build.alert_notifiers`` entry-point group
(:mod:`fluid_build.alerts.plugins`) without importing this module: any object with the same
attributes works.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, FrozenSet, Mapping, Optional, Sequence, Tuple

#: Every event a route can alert on, in the order a DAG wires them.
EVENTS: Tuple[str, ...] = ("failure", "retry", "success", "sla_miss")

#: The events a route alerts on when it names none.
DEFAULT_EVENTS: Tuple[str, ...] = ("failure",)

#: The severities a route can set, most severe first. They are PagerDuty's, and each kind maps
#: them onto its service's own (Zenduty's ``alert_type``, Opsgenie's ``priority``).
SEVERITIES: Tuple[str, ...] = ("critical", "error", "warning", "info")

#: The severity an event alerts with when its route sets none. A success is always ``info``.
EVENT_SEVERITY: Mapping[str, str] = {
    "failure": "critical",
    "retry": "warning",
    "sla_miss": "warning",
    "success": "info",
}


class AlertRouteError(ValueError):
    """An alert route cannot be rendered into a safe, working DAG.

    The message names the route and what to change. It never quotes a value that may be a
    secret pasted in the wrong place.
    """


@dataclass(frozen=True)
class AlertRoute:
    """One route, read and checked (:func:`fluid_build.alerts.routes.read_alert_plan`).

    ``connection`` is the Airflow connection id, never a URL or a key. ``to`` and ``sender``
    are email addresses (``sender`` is the contract's ``from``). ``options`` holds a plugin
    kind's own settings: JSON values whose strings carry no template syntax, no URL and no
    secret. ``origin`` says where the route came from: ``contract`` or ``org default <entry
    point name>``.
    """

    name: str
    kind: str
    events: Tuple[str, ...] = DEFAULT_EVENTS
    connection: Optional[str] = None
    to: Tuple[str, ...] = ()
    sender: Optional[str] = None
    format: Optional[str] = None
    severity: Optional[str] = None
    options: Mapping[str, Any] = field(default_factory=dict, hash=False)
    origin: str = "contract"

    def severity_for(self, event: str) -> str:
        """The severity this route alerts ``event`` with."""
        if event == "success":
            return "info"
        return self.severity or EVENT_SEVERITY.get(event, "critical")


@dataclass(frozen=True)
class Alert:
    """What one notifier call reports: one event of one DAG, as forge words it.

    ``title`` and ``text`` are Jinja templates the notifier renders with Airflow's context
    when the event happens (the failed task, the run, the log URL). They are forge's own text
    around validated ids, so a kind may pass them on as they are. ``dedup_key`` is the same
    for every event of one DAG, so an incident service folds a run's failures into one
    incident and a later success can resolve it. ``sla_seconds`` is set for ``sla_miss``.
    """

    event: str
    dag_id: str
    product_id: str
    severity: str
    title: str
    text: str
    dedup_key: str
    sla_seconds: Optional[int] = None


@dataclass(frozen=True)
class NotifierCall:
    """One notifier the DAG instantiates for an event: ``module.name(**kwargs)``.

    ``kwargs`` holds literals only (str, int, float, bool, None, and lists and string-keyed
    dicts of them), which the renderer writes into the DAG as Python literals.
    """

    module: str
    name: str
    kwargs: Mapping[str, Any]
    route: str
    supports_async: bool = False


class AlertNotifier:
    """Base class of an alert route kind: the SDK surface of ``fluid_build.alert_notifiers``.

    A subclass sets :attr:`notifier` and implements :meth:`kwargs`; the rest has defaults.
    Register it in the package's ``pyproject.toml``::

        [project.entry-points."fluid_build.alert_notifiers"]
        teams = "acme_alerts.forge:TeamsNotifier"

    and a contract route with ``kind: teams`` renders through it. The entry point may name a
    class (built with no arguments), an instance or a function that returns one, and it need
    not subclass this: forge reads the same attributes from any object.

    The notifier itself is an ordinary Airflow ``BaseNotifier`` subclass in a package the
    Airflow workers, the scheduler's DAG processor and (for deadline alerts) the triggerer can
    import. Forge never runs it: it writes ``Notifier(**kwargs)`` into the DAG.
    """

    #: ``"<module>:<class>"`` of the Airflow notifier the DAG imports, such as
    #: ``"acme_alerts.notifiers:TeamsNotifier"``.
    notifier: str = ""

    #: The pip requirements the Airflow environment needs for :attr:`notifier`, listed in the
    #: DAG's documentation, such as ``("acme-alerts>=1.2",)``.
    requirements: Tuple[str, ...] = ()

    #: Whether the notifier implements ``async_notify``. Airflow 3 has no SLAs: an
    #: ``sla_miss`` route alerts there through a deadline alert (Airflow 3.1+), which runs the
    #: notifier in the triggerer and needs it. Without it the route's ``sla_miss`` alerts on
    #: Airflow 2 only.
    supports_async: bool = False

    #: What the triggerer needs for ``async_notify`` when it is newer than
    #: :attr:`requirements`, listed in the DAG's documentation of a deadline alert.
    async_requirements: Tuple[str, ...] = ()

    #: The events this kind can send. A route that asks for another is refused.
    events: FrozenSet[str] = frozenset(EVENTS)

    #: Whether a route of this kind must name an Airflow connection.
    requires_connection: bool = True

    #: The Airflow connection the notifier uses when a route names none (the provider's
    #: default), shown in the DAG's documentation.
    default_connection: Optional[str] = None

    def check(self, route: AlertRoute) -> Sequence[str]:
        """Problems with ``route`` this kind cannot render, each a sentence; none when it can.

        Called before :meth:`kwargs`, at ``fluid validate`` and when a DAG is rendered.
        """
        return ()

    def kwargs(self, route: AlertRoute, alert: Alert) -> Mapping[str, Any]:
        """The notifier's keyword arguments for ``alert``: literals only.

        A string may hold Jinja for Airflow's context (``{{ ti.task_id }}``): the notifier
        renders its template fields when it sends. The route's own values are already checked
        to hold no template syntax, so passing them on is safe.
        """
        raise NotImplementedError(f"{type(self).__name__}.kwargs")


__all__ = [
    "Alert",
    "AlertNotifier",
    "AlertRoute",
    "AlertRouteError",
    "DEFAULT_EVENTS",
    "EVENTS",
    "EVENT_SEVERITY",
    "NotifierCall",
    "SEVERITIES",
]
