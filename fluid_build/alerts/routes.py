# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A contract's ``alerts`` block, read and checked, with the organisation's default routes.

::

    alerts:
      sla: PT4H                    # optional: a run not done this long after it started is late
      defaults: true               # take the org pack's default routes (the default)
      routes:
        - name: oncall
          kind: webhook            # email | slack | webhook | pagerduty | a plugin's kind
          format: zenduty          # webhook only: generic (default) | zenduty | opsgenie
          connection: zenduty_oncall
          events: [failure, sla_miss]
          severity: critical
        - name: team
          kind: email
          to: [data-team@corp.example]

``fluid validate`` reports what :func:`contract_alert_findings` and
:func:`plugin_alert_findings` find; ``fluid generate schedule`` renders
:func:`read_alert_plan`, which refuses the same problems.

A route never carries a secret. ``connection`` names the Airflow connection that holds the
address, URL or key, and is held to Airflow's connection-id characters: a URL, an address
with a password or something shaped like a key is refused, and the message never shows the
value. Every other string a route gives (``options`` included) is held to no template syntax
(``{`` or ``}``, which Airflow's Jinja would read in a notifier's template fields), no URL
and nothing the secret redactor would mask, and an option named like a secret (``api_key``,
``password``, ``routingKey``) is refused whatever its value.
"""

from __future__ import annotations

import keyword
import logging
import math
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from fluid_build.alerts.builtin import BUILTIN_KINDS, KIND_KEYS, make_alert
from fluid_build.alerts.model import (
    DEFAULT_EVENTS,
    EVENTS,
    SEVERITIES,
    AlertRoute,
    AlertRouteError,
    NotifierCall,
)
from fluid_build.alerts.plugins import (
    default_routes,
    kind_for,
    normalise_kind,
    refused_default_packs,
)

_log = logging.getLogger("fluid.alerts")

_ROUTE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_-]{0,63}\Z")
_KIND = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
#: Airflow connection ids, in the characters every secrets backend keeps.
_CONNECTION = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,249}\Z")
#: A value that is a key rather than a name: 20 or more hex digits, or a UUID.
_KEY_SHAPED = re.compile(
    r"(?:[0-9a-fA-F]{20,}|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
    r"-[0-9a-fA-F]{12})\Z"
)
#: A random alphanumeric token (an API or routing key) rather than an id: 24 or more letters and
#: digits with no separator, switching between letters and digits, or between upper and lower
#: case, far more often than words do. Ids use separators or camelCase words; measured on
#: random tokens it catches most, and on long camelCase ids none.
_MIN_TOKEN = 24


def _token_shaped(value: str) -> bool:
    if len(value) < _MIN_TOKEN or not value.isascii() or not value.isalnum():
        return False
    pairs = list(zip(value, value[1:], strict=False))
    digit_switches = sum(1 for a, b in pairs if a.isdigit() != b.isdigit())
    case_switches = sum(
        1 for a, b in pairs if a.isalpha() and b.isalpha() and a.isupper() != b.isupper()
    )
    return digit_switches >= 8 or case_switches >= 0.45 * len(value)


def _key_shaped(value: str) -> bool:
    """Whether ``value`` looks like a key, not a name: hex, a UUID or a random token."""
    return bool(_KEY_SHAPED.match(value)) or _token_shaped(value)


_EMAIL = re.compile(r"[A-Za-z0-9._+-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63})+\Z")
#: ISO-8601 durations without years or months, whose length depends on the calendar.
_DURATION = re.compile(
    r"P(?=\d|T\d)(?:(\d{1,7})W)?(?:(\d{1,7})D)?"
    r"(?:T(?=\d)(?:(\d{1,7})H)?(?:(\d{1,7})M)?(?:(\d{1,7})S)?)?\Z"
)
_OPTION_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\Z")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

_ALERTS_KEYS = frozenset({"routes", "sla", "defaults"})
_ROUTE_KEYS = frozenset(
    {"name", "kind", "events", "connection", "to", "from", "format", "severity", "options"}
)

#: How many routes a product may have, its own and the defaults together.
MAX_ROUTES = 20
#: The longest SLA a contract may set.
MAX_SLA_SECONDS = 30 * 86400
_MAX_ADDRESSES = 50
_MAX_TEXT = 1000
_MAX_ITEMS = 50
_MAX_DEPTH = 4

#: Kwargs a notifier call may not set: ``context`` is Airflow's (its deadline alerts pass it).
_RESERVED_KWARGS = frozenset({"context"})

#: How a refusal stands in for a value that may be a pasted secret.
NOT_SHOWN = "(value not shown)"


@dataclass(frozen=True)
class AlertPlan:
    """The routes a product's DAGs alert through, and the SLA its ``sla_miss`` routes watch."""

    routes: Tuple[AlertRoute, ...] = ()
    sla_seconds: Optional[int] = None

    def listens(self, event: str) -> bool:
        """Whether any route alerts on ``event`` (``sla_miss`` only with an SLA)."""
        if event == "sla_miss" and self.sla_seconds is None:
            return False
        return any(event in route.events for route in self.routes)


# ── reading ──────────────────────────────────────────────────────────────────


def _block(contract: Mapping[str, Any]) -> Any:
    return contract.get("alerts") if isinstance(contract, Mapping) else None


def parse_duration(value: Any) -> Optional[int]:
    """Seconds in an ISO-8601 duration (weeks to seconds), or ``None`` when it is not one."""
    if not isinstance(value, str):
        return None
    match = _DURATION.match(value.strip())
    if not match:
        return None
    weeks, days, hours, minutes, seconds = (int(part or 0) for part in match.groups())
    return (((weeks * 7 + days) * 24 + hours) * 60 + minutes) * 60 + seconds


def _redacted(text: str) -> bool:
    """Whether the secret redactor would mask part of ``text``."""
    from fluid_build.observability.secret_redactor import redact_secret_text

    return redact_secret_text(text) != text


def _text_problem(value: str) -> str:
    """Why a string a route gives may not go into a DAG, or ``""``."""
    if len(value) > _MAX_TEXT:
        return f"is longer than {_MAX_TEXT} characters"
    if "{" in value or "}" in value:
        return "holds '{' or '}', which Airflow's templates would read"
    if _CONTROL.search(value):
        return "holds a control character"
    if "://" in value:
        return "holds a URL; a URL belongs in the Airflow connection the route names"
    if _redacted(value):
        return "holds what looks like a secret; secrets belong in the Airflow connection"
    return ""


def _option_problems(value: Any, where: str, depth: int = 0) -> List[str]:
    """What keeps a plugin route's ``options`` value from being written into a DAG."""
    if depth > _MAX_DEPTH:
        return [f"{where} is nested deeper than {_MAX_DEPTH} levels"]
    if value is None or isinstance(value, bool):
        return []
    if isinstance(value, int):
        return []
    if isinstance(value, float):
        return [] if math.isfinite(value) else [f"{where} is not a finite number"]
    if isinstance(value, str):
        problem = _text_problem(value)
        return [f"{where} {problem} {NOT_SHOWN}"] if problem else []
    if isinstance(value, (list, tuple)):
        if len(value) > _MAX_ITEMS:
            return [f"{where} has more than {_MAX_ITEMS} items"]
        out: List[str] = []
        for index, item in enumerate(value):
            out += _option_problems(item, f"{where}[{index}]", depth + 1)
        return out
    if isinstance(value, Mapping):
        if len(value) > _MAX_ITEMS:
            return [f"{where} has more than {_MAX_ITEMS} keys"]
        out = []
        for key, item in value.items():
            if not isinstance(key, str) or not _OPTION_KEY.match(key):
                out.append(f"{where} has a key that is not a plain name {NOT_SHOWN}")
                continue
            if _secret_key(key):
                out.append(
                    f"{where}.{key} is named like a secret; a kind's secret belongs in the "
                    f"Airflow connection the route names, never in the DAG {NOT_SHOWN}"
                )
                continue
            out += _option_problems(item, f"{where}.{key}", depth + 1)
        return out
    return [f"{where} is a {type(value).__name__}, not a JSON value"]


def _secret_key(key: str) -> bool:
    """Whether an option key names a credential, as the redactor reads key names: its parts
    (``api_key``, ``password``, ``routing_key``, ``token``, ...) in any case and spelling
    (``apiKey``, ``routing-key``)."""
    from fluid_build.observability.secret_redactor import (
        _SENSITIVE_KEY_PARTS,
        is_sensitive_key_name,
    )

    folded = re.sub(r"[^a-z0-9]", "", key.lower())
    return is_sensitive_key_name(key) or any(
        re.sub(r"[^a-z0-9]", "", part) in folded for part in _SENSITIVE_KEY_PARTS
    )


def _connection_problem(value: Any) -> str:
    if not isinstance(value, str) or not _CONNECTION.match(value):
        return (
            "is not an Airflow connection id (letters, digits, '_', '.' and '-'); a URL, an "
            "address or a key belongs IN that connection, which the route names"
        )
    if _key_shaped(value) or _redacted(value):
        return "looks like a key, not a connection id; put the key in an Airflow connection"
    return ""


def _read_route(raw: Any, where: str, origin: str, errors: List[str]) -> Optional[AlertRoute]:
    """One route, or ``None`` with its problems added to ``errors``."""
    if not isinstance(raw, Mapping):
        errors.append(f"{where} is not a mapping")
        return None
    before = len(errors)
    unknown = sorted(str(k) for k in raw if k not in _ROUTE_KEYS)
    if unknown:
        errors.append(
            f"{where} has keys a route does not read: {', '.join(unknown)} (a route reads "
            f"{', '.join(sorted(_ROUTE_KEYS))})"
        )
    name = raw.get("name")
    if (
        not isinstance(name, str)
        or not _ROUTE_NAME.match(name)
        or _key_shaped(name)
        or _redacted(name)
    ):
        errors.append(
            f"{where}.name must be a plain name (a letter or '_', then letters, digits, '_' "
            f"and '-', at most 64) {NOT_SHOWN}"
        )
    kind_raw = raw.get("kind")
    kind = normalise_kind(kind_raw) if isinstance(kind_raw, str) else ""
    if not isinstance(kind_raw, str) or not _KIND.match(kind_raw) or _redacted(kind_raw):
        errors.append(
            f"{where}.kind must name a kind: {', '.join(BUILTIN_KINDS)}, or a plugin's "
            f"(lower case letters, digits, '_' and '-') {NOT_SHOWN}"
        )
    events_raw = raw.get("events", list(DEFAULT_EVENTS))
    events: Tuple[str, ...] = ()
    if (
        not isinstance(events_raw, (list, tuple))
        or not events_raw
        or not all(isinstance(e, str) and e in EVENTS for e in events_raw)
        or len(set(events_raw)) != len(events_raw)
    ):
        errors.append(f"{where}.events must be a list of distinct events from {', '.join(EVENTS)}")
    else:
        events = tuple(e for e in EVENTS if e in events_raw)
    connection = raw.get("connection")
    if connection is not None:
        problem = _connection_problem(connection)
        if problem:
            errors.append(f"{where}.connection {problem} {NOT_SHOWN}")
    to_raw = raw.get("to", [])
    to: Tuple[str, ...] = ()
    if not isinstance(to_raw, (list, tuple)) or len(to_raw) > _MAX_ADDRESSES:
        errors.append(f"{where}.to must be a list of at most {_MAX_ADDRESSES} email addresses")
    elif not all(isinstance(a, str) and _EMAIL.match(a) for a in to_raw):
        errors.append(f"{where}.to holds something that is not a plain email address")
    else:
        to = tuple(to_raw)
    sender = raw.get("from")
    if sender is not None and not (isinstance(sender, str) and _EMAIL.match(sender)):
        errors.append(f"{where}.from must be a plain email address")
    fmt = raw.get("format")
    if fmt is not None and (not isinstance(fmt, str) or _text_problem(fmt)):
        errors.append(f"{where}.format must be a plain word {NOT_SHOWN}")
    severity = raw.get("severity")
    if severity is not None and severity not in SEVERITIES:
        errors.append(f"{where}.severity must be one of {', '.join(SEVERITIES)}")
    options_raw = raw.get("options", {})
    if not isinstance(options_raw, Mapping):
        errors.append(f"{where}.options must be a mapping")
        options_raw = {}
    else:
        errors += _option_problems(options_raw, f"{where}.options")
    for key, kinds in KIND_KEYS.items():
        if key in raw and kind and kind not in kinds:
            errors.append(f"{where}.{key} is read by a {' or '.join(sorted(kinds))} route only")
    if options_raw and kind in BUILTIN_KINDS:
        errors.append(f"{where}.options is read by a plugin's kind only, not by {kind!r}")
    if len(errors) > before:
        return None
    return AlertRoute(
        name=str(name),
        kind=kind,
        events=events,
        connection=connection,
        to=to,
        sender=sender,
        format=fmt,
        severity=severity,
        options=dict(options_raw),
        origin=origin,
    )


def _check_kind(
    route: AlertRoute,
    where: str,
    errors: List[str],
    warnings: List[str],
    *,
    plugins: bool,
    sla_seconds: Optional[int] = None,
) -> None:
    """What the route's kind says about the route: a built-in always, a plugin's only with
    ``plugins`` (its package is imported then, so a phase that runs no plugin skips it). The
    kind's ``kwargs`` run once for each event the route sends, as a DAG would run them, so a
    route that passes here renders."""
    if route.kind not in BUILTIN_KINDS and not plugins:
        return
    try:
        kind = kind_for(route.kind)
    except AlertRouteError as exc:
        errors.append(f"{where}.kind: {exc}")
        return
    if getattr(kind, "requires_connection", True) and not route.connection:
        errors.append(
            f"{where}.connection is required for a {route.kind} route: it names the Airflow "
            "connection that holds the URL or key"
        )
    served = set(getattr(kind, "events", EVENTS))
    missing = [e for e in route.events if e not in served]
    if missing:
        errors.append(f"{where}.events: a {route.kind} route cannot send {', '.join(missing)}")
    try:
        found = kind.check(route) if callable(getattr(kind, "check", None)) else ()
        problems = [found] if isinstance(found, str) else list(found or ())
    except Exception as exc:  # noqa: BLE001 - a plugin bug is a finding, named by type only
        errors.append(f"{where}: the {route.kind} kind's check raised {type(exc).__name__}")
        return
    errors += [f"{where}: {problem}" for problem in problems if isinstance(problem, str)]
    if problems or missing:
        return
    for event in route.events:
        try:
            _call(
                kind,
                route,
                event,
                dag_id="fluid.validate",
                product_id="fluid.validate",
                sla_seconds=sla_seconds or 3600,
            )
        except AlertRouteError as exc:
            errors.append(f"{where}: {exc}")
            break
    if "sla_miss" in route.events and not getattr(kind, "supports_async", False):
        warnings.append(
            f"{where} alerts on sla_miss, but its {route.kind} notifier has no async_notify: "
            "Airflow 3 has no SLAs and runs a deadline alert's notifier in the triggerer, so "
            "this route alerts on a missed SLA on Airflow 2 only"
        )


def _read_contract(
    contract: Mapping[str, Any], *, plugins: bool
) -> Tuple[List[AlertRoute], Optional[int], bool, List[str], List[str]]:
    """``(routes, sla seconds, take defaults, errors, warnings)`` of the contract's block.
    ``plugins``: also check the routes whose kind a plugin ships (:func:`_check_kind`)."""
    errors: List[str] = []
    warnings: List[str] = []
    block = _block(contract)
    if block is None:
        return [], None, True, errors, warnings
    if not isinstance(block, Mapping):
        return [], None, True, ["alerts must be a mapping"], warnings
    unknown = sorted(str(k) for k in block if k not in _ALERTS_KEYS)
    if unknown:
        errors.append(f"alerts has keys it does not read: {', '.join(unknown)}")
    take_defaults = block.get("defaults", True)
    if not isinstance(take_defaults, bool):
        errors.append("alerts.defaults must be true or false")
        take_defaults = True
    sla: Optional[int] = None
    if block.get("sla") is not None:
        sla = parse_duration(block.get("sla"))
        if sla is None or sla <= 0 or sla > MAX_SLA_SECONDS:
            errors.append(
                "alerts.sla must be an ISO-8601 duration from PT1S to P30D, in weeks, days, "
                "hours, minutes and seconds (PT4H, P1DT6H)"
            )
            sla = None
    raw_routes = block.get("routes", [])
    if not isinstance(raw_routes, (list, tuple)):
        return [], sla, take_defaults, errors + ["alerts.routes must be a list"], warnings
    if len(raw_routes) > MAX_ROUTES:
        errors.append(f"alerts.routes holds more than {MAX_ROUTES} routes")
        raw_routes = list(raw_routes)[:MAX_ROUTES]
    routes: List[AlertRoute] = []
    seen: Dict[str, int] = {}
    for index, raw in enumerate(raw_routes):
        where = f"alerts.routes[{index}]"
        route = _read_route(raw, where, "contract", errors)
        if route is None:
            continue
        if route.name in seen:
            errors.append(f"{where}.name {route.name!r} is also alerts.routes[{seen[route.name]}]")
            continue
        seen[route.name] = index
        _check_kind(route, where, errors, warnings, plugins=plugins, sla_seconds=sla)
        if "sla_miss" in route.events and block.get("sla") is None:
            errors.append(
                f"{where} alerts on sla_miss, but alerts.sla is not set: set the SLA the "
                "route watches (alerts.sla: PT4H)"
            )
        routes.append(route)
    return routes, sla, take_defaults, errors, warnings


def _read_defaults(
    taken: Sequence[AlertRoute], sla_seconds: Optional[int]
) -> Tuple[List[AlertRoute], List[str], List[str]]:
    """The org packs' default routes the contract does not replace: ``(routes, errors,
    warnings)``. A contract route of the same name replaces a default. An installed pack the
    operator's policy leaves out is a warning: its routes are an organisation's alerts."""
    errors: List[str] = []
    warnings = [
        f"the default alert routes {name!r} are installed, but FLUID_PLUGINS_ALLOWLIST / "
        "FLUID_PLUGINS_BLOCKLIST leave them out, so this product's DAGs do not alert through "
        "them"
        for name in refused_default_packs()
    ]
    try:
        packs = default_routes()
    except AlertRouteError as exc:
        return [], [str(exc)], warnings
    own = {route.name for route in taken}
    routes: List[AlertRoute] = []
    seen: Dict[str, str] = {}
    for entry, raw_routes in packs:
        origin = f"org default {entry}"
        for index, raw in enumerate(raw_routes):
            where = f"the default alert routes {entry!r}: routes[{index}]"
            route = _read_route(raw, where, origin, errors)
            if route is None:
                continue
            if route.name in seen:
                errors.append(
                    f"{where}: the route name {route.name!r} is also one of the default alert "
                    f"routes {seen[route.name]!r}"
                )
                continue
            seen[route.name] = entry
            if route.name in own:
                continue
            _check_kind(route, where, errors, warnings, plugins=True, sla_seconds=sla_seconds)
            routes.append(route)
    return routes, errors, warnings


def _sla_warning(routes: Sequence[AlertRoute], sla: Optional[int]) -> List[str]:
    if sla is not None and not any("sla_miss" in route.events for route in routes):
        return ["alerts.sla is set, but no route alerts on sla_miss, so a missed SLA alerts no one"]
    return []


def _total_error(count: int) -> List[str]:
    if count > MAX_ROUTES:
        return [f"a product may have at most {MAX_ROUTES} alert routes, defaults included"]
    return []


def contract_alert_findings(contract: Mapping[str, Any]) -> Tuple[List[str], List[str]]:
    """``(errors, warnings)`` about the contract's own ``alerts`` block, running no plugin:
    the routes of a built-in kind in full, a plugin kind's routes as far as the grammar goes.
    The rest is :func:`plugin_alert_findings`'s."""
    routes, sla, take_defaults, errors, warnings = _read_contract(contract, plugins=False)
    if not take_defaults:
        warnings += _sla_warning(routes, sla)
    return errors, warnings


def plugin_alert_findings(contract: Mapping[str, Any]) -> Tuple[List[str], List[str]]:
    """``(errors, warnings)`` that need plugins: each contract route whose kind a plugin ships,
    and the org packs' default routes this contract takes (``fluid_build.alert_defaults``),
    with the count of both. ``fluid validate``'s plugin phase; together with
    :func:`contract_alert_findings` it finds what :func:`read_alert_plan` refuses."""
    own, sla, take_defaults, own_errors, _ = _read_contract(contract, plugins=False)
    errors: List[str] = []
    warnings: List[str] = []
    block = _block(contract)
    raw = block.get("routes") if isinstance(block, Mapping) else None
    by_name = (
        {r.get("name"): i for i, r in enumerate(raw or []) if isinstance(r, Mapping)}
        if isinstance(raw, (list, tuple))
        else {}
    )
    for route in own:
        if route.kind not in BUILTIN_KINDS:
            where = f"alerts.routes[{by_name.get(route.name, '?')}]"
            _check_kind(route, where, errors, warnings, plugins=True, sla_seconds=sla)
    if not take_defaults:
        return errors, warnings
    defaults, default_errors, default_warnings = _read_defaults(own, sla)
    errors += default_errors + _total_error(len(own) + len(defaults))
    warnings += default_warnings
    if not own_errors and not default_errors:
        warnings += _sla_warning([*own, *defaults], sla)
    return errors, warnings


def read_alert_plan(contract: Mapping[str, Any], *, defaults: bool = True) -> AlertPlan:
    """The routes the contract's DAGs alert through: its own, then the org packs' defaults
    it takes (``defaults=False`` leaves those out). Raises :class:`AlertRouteError` naming
    every problem :func:`contract_alert_findings` and :func:`plugin_alert_findings` find."""
    routes, sla, take_defaults, errors, _warnings = _read_contract(contract, plugins=True)
    if defaults and take_defaults:
        extra, default_errors, default_warnings = _read_defaults(routes, sla)
        for warning in default_warnings:
            if "FLUID_PLUGINS_ALLOWLIST" in warning:
                _log.warning("alert_defaults_left_out: %s", warning)
        routes += extra
        errors += default_errors
    errors += _total_error(len(routes))
    if errors:
        raise AlertRouteError("the alert routes cannot be rendered: " + "; ".join(errors))
    return AlertPlan(routes=tuple(routes), sla_seconds=sla)


# ── the calls one DAG makes ──────────────────────────────────────────────────


def _literal_problem(value: Any, depth: int = 0) -> str:
    """Why a notifier keyword argument cannot be written into a DAG as a literal, or ``""``."""
    if depth > 8:
        return "is nested too deep"
    if value is None or isinstance(value, (bool, int, str)):
        return ""
    if isinstance(value, float):
        return "" if math.isfinite(value) else "is not a finite number"
    if isinstance(value, (list, tuple)):
        for item in value:
            problem = _literal_problem(item, depth + 1)
            if problem:
                return problem
        return ""
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                return "has a key that is not a string"
            problem = _literal_problem(item, depth + 1)
            if problem:
                return problem
        return ""
    return f"holds a {type(value).__name__}, which is not a literal"


def _call(
    kind: Any,
    route: AlertRoute,
    event: str,
    *,
    dag_id: str,
    product_id: str,
    sla_seconds: Optional[int],
) -> NotifierCall:
    """``route``'s notifier for ``event`` of the DAG ``dag_id``, its arguments checked to be
    literals the DAG can hold. Raises :class:`AlertRouteError` (by the kind's exception type
    only) when they are not."""
    alert = make_alert(
        event,
        dag_id=dag_id,
        product_id=product_id,
        severity=route.severity_for(event),
        sla_seconds=sla_seconds,
    )
    try:
        kwargs = dict(kind.kwargs(route, alert))
    except Exception as exc:  # noqa: BLE001 - a plugin bug refuses the DAG, type only
        raise AlertRouteError(
            f"alert route {route.name!r}: its {route.kind} kind raised "
            f"{type(exc).__name__} building the notifier's arguments"
        ) from None
    for key, value in kwargs.items():
        if not isinstance(key, str) or not key.isidentifier() or keyword.iskeyword(key):
            raise AlertRouteError(
                f"alert route {route.name!r}: its {route.kind} kind returned an argument "
                "name that is not a Python identifier"
            )
        if key in _RESERVED_KWARGS:
            raise AlertRouteError(
                f"alert route {route.name!r}: its {route.kind} kind sets {key!r}, which "
                "Airflow passes itself"
            )
        problem = _literal_problem(value)
        if problem:
            raise AlertRouteError(
                f"alert route {route.name!r}: its {route.kind} kind's argument {key!r} "
                f"{problem}"
            )
    module, _, name = str(kind.notifier).partition(":")
    return NotifierCall(
        module=module,
        name=name,
        kwargs=kwargs,
        route=route.name,
        supports_async=bool(getattr(kind, "supports_async", False)),
    )


def notifier_calls(
    plan: AlertPlan, event: str, *, dag_id: str, product_id: str
) -> List[NotifierCall]:
    """The notifier each route of ``plan`` sends ``event`` of the DAG ``dag_id`` through.

    ``dag_id`` and ``product_id`` must be validated identifiers. Raises
    :class:`AlertRouteError` when a kind's ``kwargs`` raises or returns what cannot be
    written into the DAG as literals.
    """
    if not plan.listens(event):
        return []
    return [
        _call(
            kind_for(route.kind),
            route,
            event,
            dag_id=dag_id,
            product_id=product_id,
            sla_seconds=plan.sla_seconds,
        )
        for route in plan.routes
        if event in route.events
    ]


def requirements(plan: AlertPlan) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
    """``(requirements, deadline requirements)`` of the kinds ``plan`` uses, each sorted."""
    need: Set[str] = set()
    deadline: Set[str] = set()
    for route in plan.routes:
        kind = kind_for(route.kind)
        need.update(getattr(kind, "requirements", ()) or ())
        if "sla_miss" in route.events and plan.sla_seconds is not None:
            deadline.update(getattr(kind, "async_requirements", ()) or ())
    return tuple(sorted(need)), tuple(sorted(deadline))


__all__ = [
    "AlertPlan",
    "MAX_ROUTES",
    "MAX_SLA_SECONDS",
    "NOT_SHOWN",
    "contract_alert_findings",
    "notifier_calls",
    "parse_duration",
    "plugin_alert_findings",
    "read_alert_plan",
    "requirements",
]
