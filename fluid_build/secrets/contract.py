# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Find a contract's secret references and lint them without reading a value.

``fluid validate`` and ``fluid secrets check`` run :func:`lint_secret_refs`:
each reference is parsed, its scheme and locator are checked against the
operator's policy, and every destination the value would be sent to is
checked against the destination allowlist. Nothing is fetched (design
invariant I4), so a contract that would be refused at ``apply`` is refused at
``validate``.

The references a stage resolves, and where each value goes:

* ``builds[].properties.source.connection.secretRef`` -- every host the
  connection block names.
* ``builds[].properties.airbyte.deployment.auth.secretRef`` -- the host of
  ``deployment.server_url`` (the REST mode's token; an inline ``auth.token``
  wins, and then the reference is never read).

A destination whose host is a ``{{ env.NAME }}`` placeholder is left to
``fluid apply``, which renders it from its own environment and then judges
the host it names: the lint does not read the environment, and no allowlist
entry can match the placeholder. Such a finding carries ``deferred`` (a
warning naming the entry apply needs) instead of an error; every destination
field written without a placeholder is still judged here.

Any other ``secretRef`` string is linted for grammar, scheme and prefix only:
no stage resolves it, so it is sent nowhere. That includes meltano's,
kafka-connect's and debezium's ``deployment.auth`` and kafka-connect's
``schema_registry.auth``, which the schema allows and no runner reads today.
A runner that starts reading one asks ``resolve_secret`` with its
destination, which refuses at apply what this lint did not judge.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Mapping, Optional, Set

from .errors import SecretError
from .policy import (
    allows_any_host,
    check_prefix,
    check_scheme,
    connection_host,
    connection_hosts,
    deferred_destination_notice,
    destinations_rendered_at_apply,
    literal_destinations,
    refuse_destination_overrides,
    refuse_unless_allowed,
    renders_at_apply,
    written_destination,
)
from .ref import SecretRef, parse_secret_ref
from .registry import check_available

#: The engines whose runner resolves ``deployment.auth.secretRef``.
_DEPLOYMENT_ENGINES = ("airbyte",)


@dataclass(frozen=True)
class RefSite:
    """One reference in a contract and where its value would be sent."""

    pointer: str
    raw: Any
    connection: Optional[Mapping[str, Any]] = None
    url: Optional[str] = None
    sent: bool = True


@dataclass
class RefFinding:
    """A lint result for one site: ``error`` is ``None`` when the reference is fine.

    ``warning`` says no resolver for the scheme is installed here: the machine
    that runs ``fluid apply`` needs one, but this one (a laptop, a CI lint job)
    may not, so it does not fail validation.

    ``deferred`` says a destination's host is a ``{{ env.NAME }}`` placeholder,
    judged only when ``fluid apply`` renders it; it names the allowlist entry
    apply will need, and does not fail validation either.
    """

    site: RefSite
    ref: Optional[SecretRef] = None
    error: Optional[SecretError] = None
    warning: Optional[SecretError] = None
    extras: Dict[str, Any] = field(default_factory=dict)
    deferred: Optional[SecretError] = None

    @property
    def ok(self) -> bool:
        return self.error is None


def _dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _known_sites(contract: Mapping[str, Any]) -> Iterator[RefSite]:
    builds = contract.get("builds")
    if not isinstance(builds, list):
        return
    for index, build in enumerate(builds):
        props = _dict(_dict(build).get("properties"))
        base = f"builds[{index}].properties"
        connection = _dict(_dict(props.get("source")).get("connection"))
        if "secretRef" in connection:
            rest = {k: v for k, v in connection.items() if k != "secretRef"}
            yield RefSite(
                f"{base}.source.connection.secretRef",
                connection["secretRef"],
                connection=rest,
                # An inline password wins at run time, so the reference is never
                # read or sent (``resolve_connection_secrets``); a 0.7.7 contract
                # cannot carry one (``password`` is writeOnly there).
                sent=not connection.get("password"),
            )
        for engine in _DEPLOYMENT_ENGINES:
            engine_props = _dict(props.get(engine))
            deployment = _dict(engine_props.get("deployment"))
            auth = _dict(deployment.get("auth"))
            if "secretRef" in auth:
                yield RefSite(
                    f"{base}.{engine}.deployment.auth.secretRef",
                    auth["secretRef"],
                    url=str(deployment.get("server_url") or "") or None,
                    sent=not auth.get("token"),
                )


def _other_sites(value: Any, pointer: str, seen: Set[str]) -> Iterator[RefSite]:
    if isinstance(value, dict):
        for key, child in value.items():
            path = f"{pointer}.{key}" if pointer else str(key)
            if key == "secretRef" and isinstance(child, str) and path not in seen:
                yield RefSite(path, child, sent=False)
            else:
                yield from _other_sites(child, path, seen)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _other_sites(child, f"{pointer}[{index}]", seen)


def secret_ref_sites(contract: Mapping[str, Any]) -> List[RefSite]:
    """Every ``secretRef`` in ``contract``, the resolved ones first."""
    known = list(_known_sites(contract))
    seen = {site.pointer for site in known}
    return known + list(_other_sites(dict(contract), "", seen))


def lint_site(site: RefSite, environ: Optional[Mapping[str, str]] = None) -> RefFinding:
    """Parse and police one site. Never resolves."""
    try:
        ref = parse_secret_ref(site.raw)
    except SecretError as exc:
        return RefFinding(site=site, error=exc)
    warning: Optional[SecretError] = None
    try:
        check_available(ref.scheme)
    except SecretError as exc:
        warning = exc
    deferred: Optional[SecretError] = None
    try:
        check_scheme(ref, environ)
        check_prefix(ref, environ)
        if site.sent:
            deferred = _check_destinations(ref, site, environ)
    except SecretError as exc:
        return RefFinding(site=site, ref=ref, error=exc, warning=warning)
    return RefFinding(site=site, ref=ref, warning=warning, deferred=deferred)


def _check_destinations(
    ref: SecretRef, site: RefSite, environ: Optional[Mapping[str, str]]
) -> Optional[SecretError]:
    """Judge every destination field written literally; the notice for those apply renders.

    A destination whose host is a ``{{ env.NAME }}`` placeholder is taken out
    and left to apply (:func:`~fluid_build.secrets.policy.renders_at_apply`);
    the rest of the connection is judged as ``fluid apply`` would, unless
    nothing that names a destination is left. No notice when the operator lets
    the reference go to any host: apply clears whatever the placeholder
    renders to, and ``fluid validate --strict`` stays green.
    """
    if site.connection is None:
        if site.url and renders_at_apply("uri", site.url):
            if allows_any_host(ref, environ):
                return None
            return deferred_destination_notice(ref, {"deployment.server_url": site.url})
        host = connection_host({"uri": site.url}) if site.url else None
        refuse_unless_allowed(ref, host=host, environ=environ)
        return None
    waiting = destinations_rendered_at_apply(site.connection)
    if not waiting:
        refuse_unless_allowed(ref, connection=site.connection, environ=environ)
        return None
    literal = literal_destinations(site.connection, waiting)
    if connection_hosts(literal):
        refuse_unless_allowed(ref, connection=literal, environ=environ)
    if allows_any_host(ref, environ):
        return None
    return deferred_destination_notice(
        ref,
        {f"connection.{name}": str(written_destination(site.connection, name)) for name in waiting},
    )


def _override_findings(contract: Mapping[str, Any]) -> Iterator[RefFinding]:
    """A Debezium ``extra_config`` that would move a resolved secret's destination."""
    builds = contract.get("builds")
    for index, build in enumerate(builds if isinstance(builds, list) else []):
        props = _dict(_dict(build).get("properties"))
        connection = _dict(_dict(props.get("source")).get("connection"))
        extra = _dict(props.get("debezium")).get("extra_config")
        if not connection.get("secretRef") or not isinstance(extra, dict):
            continue
        where = f"builds[{index}].properties.debezium.extra_config"
        try:
            refuse_destination_overrides(extra, where=where)
        except SecretError as exc:
            yield RefFinding(site=RefSite(where, None, sent=False), error=exc)


def lint_secret_refs(
    contract: Mapping[str, Any], environ: Optional[Mapping[str, str]] = None
) -> List[RefFinding]:
    """One finding per reference in the contract (and per override that would move
    one's destination); check ``finding.ok``."""
    findings = [lint_site(site, environ) for site in secret_ref_sites(contract)]
    return findings + list(_override_findings(contract))
