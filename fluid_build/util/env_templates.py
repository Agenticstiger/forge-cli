# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""One reading of ``{{ env.NAME }}`` for every stage that renders a contract.

Four resolvers used to read the placeholder and disagreed about a variable that
is not set: one left the text as written, two rendered nothing, one refused. A
contract then deployed to a bucket named ``{{ env.S3_BUCKET }}``, or to a path
missing its first half, and the command exited 0. This is the one place that
decides.

The rules, for every caller:

* A name is **missing** when the variable is unset, empty or only blanks.
  :func:`render_contract` raises :class:`EnvTemplateError`, which lists every missing
  name and where it is written, before anything is deployed. The one exception is
  :func:`render_text`, the single-string primitive for a provider's optional field
  with a default: it leaves an unset variable's placeholder and renders an empty
  variable as empty, so that the field's fallback applies. The stages that deploy
  render a contract strictly before they reach it.
* A stage that deliberately leaves the placeholder for a later one says so by
  name: ``unset=UNSET_DEFER``. The call sites that do are the publish and export
  paths (a catalog shows the template), the preload of a contract embedded in a
  plan (each build renders it strictly when it runs), the validation report, and
  ``generate artifacts`` (it runs where the bundle is built, not where it is deployed).
  ``tests/test_env_templates_defer_sites.py`` pins the list, so nothing passes
  through unresolved by accident.
* A credential-shaped name (:func:`~fluid_build.observability.secret_redactor.is_sensitive_key_name`)
  is never rendered, whatever the mode: the rendered contract is written to
  ``main.tf.json``, to state, to a table COMMENT or to a catalog. It stays as
  written, with one warning per name, and is not "missing": policy refuses it.

* On the acquisition path, a host is not the contract's to pick from the operator's
  environment (:func:`check_host_templates`). A credential-shaped variable never
  renders into a URL or a host field. Any other variable renders into a URL's
  authority or a host field only when the operator pairs it with the host it
  makes (``NAME@host`` in ``FLUID_ACQUISITION_SECRET_REF_ALLOWLIST``), unless it
  *is* the host (``host: "{{ env.PGHOST }}"``): then the operator chose the
  address and the contract only named the variable. Rendered into
  ``https://{{ env.TOKEN }}.attacker.example`` a secret would be sent, in the host
  name, to whoever owns the domain.

Borrowed from the strict modes of ``envsubst -no-unset`` (collect every missing
name, then fail), Docker Compose's ``${VAR:?}`` (unset or empty is an error) and
Flux's ``--strict``; their "resolved later is always an explicit marker" is the
named ``UNSET_DEFER``. Stdlib only, so the light CLI path may import it.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional

from fluid_build._errors import FluidUserError, doc_url

#: ``{{ env.NAME }}`` in a string. The one pattern: it takes any run of
#: non-space characters as the name, so a name that is not an identifier is
#: looked up (and reported) as written instead of passing through unseen.
ENV_TEMPLATE_RE = re.compile(r"\{\{\s*env\.(\S+?)\s*\}\}")

#: An unset or empty variable is an error (:class:`EnvTemplateError`).
UNSET_ERROR = "error"
#: The stage leaves the placeholder of an unset or empty variable as written, for a later stage
#: (which refuses it, if it is the one that deploys).
UNSET_DEFER = "defer"

#: A credential-shaped name is left as written (the default: a rendered contract is written to files,
#: state and catalogs).
SENSITIVE_REFUSE = "refuse"
#: A credential-shaped name renders like any other. For the build runners, which hand a connection's
#: ``password: "{{ env.PGPASSWORD }}"`` to the engine that connects; where a rendered value may go is the
#: secret policy's to judge there.
SENSITIVE_RENDER = "render"

#: How many field paths the error names before it counts the rest.
_PATHS_SHOWN = 6

_DEFAULT_LOG = logging.getLogger("fluid.env_templates")


def _has_value(value: Optional[str], strip: bool = True) -> bool:
    """Whether a variable holds something: not unset, not empty and, where the rendered string
    is stripped, not only blanks (which the ``.strip()`` would turn into an empty name)."""
    return value is not None and bool(value.strip() if strip else value)


@dataclass
class EnvTemplateError(FluidUserError):
    """``{{ env.NAME }}`` names variables that are unset or empty here."""

    code: str = "EnvTemplateError"


def _missing_error(fields: Mapping[str, List[str]], stage: Optional[str]) -> EnvTemplateError:
    names = sorted(fields)
    shown: List[str] = []
    for name in names:
        for place in fields[name][:2]:
            shown.append(f"{name} at {place}" if place else name)
    total = sum(len(places) for places in fields.values())
    if len(shown) > _PATHS_SHOWN:
        shown = shown[:_PATHS_SHOWN] + [f"and {total - _PATHS_SHOWN} more places"]
    return EnvTemplateError(
        what=(
            f"The contract needs {'an environment variable that is' if len(names) == 1 else 'environment variables that are'} "
            f"not set or empty here: {', '.join(names)}"
        ),
        where="; ".join(shown) or None,
        why=(
            "{{ env.NAME }} renders from the environment of the process that runs this "
            f"command{f' ({stage})' if stage else ''}. With no value it would deploy a name "
            "the contract never wrote (an empty one, or the literal placeholder) and still "
            "exit 0."
        ),
        fix=(
            f"Export {', '.join(names)} before the command (an empty value counts as unset), "
            "or correct the placeholder in the contract."
        ),
        doc=doc_url(),
        extras={
            "missing": names,
            "fields": {name: list(places) for name, places in fields.items()},
        },
    )


def placeholder_names(value: Any) -> List[str]:
    """The variable names ``value`` names, each once, in the order first written.

    Walks strings, lists and dicts; anything else names nothing.
    """
    found: Dict[str, None] = {}

    def walk(node: Any) -> None:
        if isinstance(node, str):
            if "{{" in node:
                for name in ENV_TEMPLATE_RE.findall(node):
                    found.setdefault(name.strip(), None)
        elif isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            for item in node.values():
                walk(item)

    walk(value)
    return list(found)


def render_text(
    text: str, environ: Optional[Mapping[str, str]] = None, *, strip: bool = True
) -> str:
    """``text`` with each ``{{ env.NAME }}`` replaced by the variable's value, leaving the
    placeholder of an unset variable as written (the :data:`UNSET_DEFER` rule).

    The single-string primitive for a caller that decides for itself what an
    unrendered placeholder means (a provider's optional field with a default). A
    string with ``{{`` in it comes back stripped, as it always did, unless ``strip``
    is false.
    """
    if not isinstance(text, str) or "{{" not in text:
        return text
    env = os.environ if environ is None else environ

    def sub(match: "re.Match[str]") -> str:
        value = env.get(match.group(1).strip())
        return match.group(0) if value is None else value

    out = ENV_TEMPLATE_RE.sub(sub, text)
    return out.strip() if strip else out


def render_contract(
    value: Any,
    *,
    unset: str = UNSET_ERROR,
    sensitive: str = SENSITIVE_REFUSE,
    environ: Optional[Mapping[str, str]] = None,
    purpose: Optional[str] = None,
    refusal: Optional[str] = None,
    stage: Optional[str] = None,
    strip: bool = True,
    logger: Optional[logging.Logger] = None,
) -> Any:
    """``value`` with every ``{{ env.NAME }}`` in its strings rendered, through lists and dicts.

    ``unset`` says what an unset or empty variable is: :data:`UNSET_ERROR` (the
    default) raises one :class:`EnvTemplateError` naming every missing variable
    and the fields that write it; :data:`UNSET_DEFER` leaves the placeholder as
    written. Nothing else decides: the caller names the mode.

    A credential-shaped name is never rendered in either ``unset`` mode unless the
    caller says ``sensitive=SENSITIVE_RENDER`` (see the module docstring); the
    warning says where the rendered ``value`` goes, as ``purpose``; a caller whose log line
    is part of its output contract gives it whole as ``refusal`` (one ``%s``, the variable
    name). ``stage`` names the command, for the error. ``strip`` keeps the historical ``.strip()`` of a string
    that holds ``{{``; a caller that renders paths or SQL turns it off. ``value`` is
    not changed.
    """
    if unset not in (UNSET_ERROR, UNSET_DEFER):
        raise ValueError(f"unset must be {UNSET_ERROR!r} or {UNSET_DEFER!r}, not {unset!r}")
    if sensitive not in (SENSITIVE_REFUSE, SENSITIVE_RENDER):
        raise ValueError(
            f"sensitive must be {SENSITIVE_REFUSE!r} or {SENSITIVE_RENDER!r}, not {sensitive!r}"
        )
    from fluid_build.observability.secret_redactor import is_sensitive_key_name

    env = os.environ if environ is None else environ
    log = logger or _DEFAULT_LOG
    warned: set[str] = set()
    missing: Dict[str, List[str]] = {}

    def refuse_sensitive(name: str) -> None:
        if name in warned:
            return
        warned.add(name)
        if refusal is not None:
            log.warning(refusal, name)
            return
        log.warning(
            "Refusing to resolve sensitive-looking env placeholder '{{ env.%s }}' in contract "
            "body; leaving it literal. %s: a credential must not ride along. If this value is "
            "not a secret, rename the variable outside the password/secret/token/key family.",
            name,
            purpose or "The rendered contract is written to files, state and catalogs",
        )

    def render(text: str, where: str) -> str:
        if "{{" not in text:
            return text

        def sub(match: "re.Match[str]") -> str:
            name = match.group(1).strip()
            if sensitive == SENSITIVE_REFUSE and is_sensitive_key_name(name):
                refuse_sensitive(name)
                return match.group(0)
            rendered = env.get(name)
            if _has_value(rendered, strip):
                return rendered
            missing.setdefault(name, []).append(where)
            return match.group(0)

        out = ENV_TEMPLATE_RE.sub(sub, text)
        return out.strip() if strip else out

    def walk(node: Any, where: str) -> Any:
        if isinstance(node, dict):
            return {
                key: walk(item, f"{where}.{key}" if where else str(key))
                for key, item in node.items()
            }
        if isinstance(node, list):
            return [walk(item, f"{where}[{index}]") for index, item in enumerate(node)]
        if isinstance(node, str):
            return render(node, where)
        return node

    rendered_value = walk(value, "")
    if missing and unset == UNSET_ERROR:
        raise _missing_error(missing, stage)
    return rendered_value


#: The last word of a field name that holds a host, or a URL whose authority is one
#: (``host``, ``catalog_uri``, ``iceberg.catalog.uri``, ``bootstrap_servers``, ``server_url``):
#: the destination fields of ``secrets.policy`` plus the spellings connectors use.
HOST_FIELD_WORDS = frozenset(
    {"host", "hostname", "hosts", "uri", "url", "endpoint", "server", "servers", "brokers"}
    | {"address", "addresses"}
)
#: Whole field names that hold one, whatever their last word.
HOST_FIELD_NAMES = frozenset({"connection_string", "sqlalchemy_url"})


def is_host_field(key: str) -> bool:
    """Whether a field called ``key`` holds a host or a URL."""
    lowered = key.lower()
    return lowered in HOST_FIELD_NAMES or re.split(r"[._\-]", lowered)[-1] in HOST_FIELD_WORDS


#: A value read as a URL: a scheme (or a placeholder for it) and ``://``.
_URL_START = re.compile(r"[\s\x00-\x1f\x7f-\x9f]*(?:[A-Za-z][\w+.\-:]*|\{\{[^{}]*\}\})://")


def _authority_end(text: str, start: int) -> int:
    """Where the authority that begins at ``start`` ends: at the first ``/``, ``?`` or ``#``."""
    ends = [i for i in (text.find(c, start) for c in "/?#") if i != -1]
    return min(ends) if ends else len(text)


def _hosts_of(authority: str) -> List[str]:
    """Every host an authority (or a bare host field) names: userinfo and port dropped, a
    comma list split, lower-cased. MongoDB and libpq take several and may use any of them."""
    hosts: List[str] = []
    for piece in authority.rpartition("@")[2].split(","):
        piece = piece.strip()
        if piece.startswith("["):
            host = piece[1:].partition("]")[0]
        else:
            host = piece.partition(":")[0]
        hosts.append(host.strip().lower().rstrip("."))
    return hosts


#: The schemes whose "authority" is a bucket or a container, not a server a contract can point a
#: credential at: the engine is configured for the store, and the name only picks within it.
_STORE_SCHEMES = frozenset(
    {"s3", "s3a", "s3n", "gs", "gcs", "az", "azure", "abfs", "abfss", "wasb", "wasbs", "file"}
)
_CONTROL = re.compile(r"[\s\x00-\x1f\x7f-\x9f]")
#: The host in a keyword/value connection string (libpq, ODBC): ``host=db1 dbname=x``.
_KEYWORD_HOST = re.compile(
    r"(?:^|[\s;])(?:host|hostaddr|server|data source)\s*=\s*((?:\{\{[^{}]*\}\}|[^\s;{])+)", re.I
)


def _control_free(text: str) -> str:
    return _CONTROL.sub("", text)


def _authority_of(text: str, url: Optional["re.Match[str]"]) -> Optional[str]:
    """The part of ``text`` where a variable makes the host, or ``None`` when it names none.

    A URL's authority; for a value that is not a URL, the host it starts with (``host``,
    ``host:port``, ``//host/path``) or the host of a keyword/value connection string.
    """
    if url is not None:
        start = url.end()
        return text[start : _authority_end(text, start)]
    body = text.strip()
    if body.startswith("//"):
        body = body[2:]
    head = body[: _authority_end(body, 0)]
    if "=" in head:  # a keyword/value connection string: ``host=db1 dbname=x``
        keyword = _KEYWORD_HOST.search(head)
        return keyword.group(1) if keyword else None
    return head


def _host_refusal(
    name: str, where: str, *, credential: bool, stage: Optional[str]
) -> EnvTemplateError:
    """The refusal for one placeholder: its name and field, never a rendered value."""
    if credential:
        what = (
            f"{{{{ env.{name} }}}} is a credential-shaped variable in the URL or host field {where}"
        )
        why = (
            "A variable named like a credential never renders into a URL or a host: it would be "
            "sent, in the address, to whichever host the contract names."
        )
        fix = (
            "Give the credential to the connector as a secretRef, and write the host in the "
            f"contract. If {name} is not a secret, rename it outside the "
            "password/secret/token/key family."
        )
    else:
        what = f"{{{{ env.{name} }}}} makes the host of {where}, and the operator has not paired it with that host"
        why = (
            "A variable renders into a host (or a URL's authority) only when the operator lists "
            "it with the host it makes, so a contract cannot send a value from the operator's "
            "environment to a host of its own choosing."
        )
        fix = (
            f"Add {name}@<that host> to FLUID_ACQUISITION_SECRET_REF_ALLOWLIST in the operator "
            "environment (the host is the one the value renders to), or write the host in the "
            "contract."
        )
    return EnvTemplateError(
        what=what,
        where=where,
        why=why + (f" ({stage})" if stage else ""),
        fix=fix,
        doc=doc_url(),
        extras={
            "variable": name,
            "field": where,
            "reason": "credential" if credential else "unpaired",
        },
        code="EnvTemplateHostError",
    )


def _is_one_template(text: str) -> bool:
    """Whether ``text`` is exactly one placeholder, read as :func:`render_contract` reads it
    (the shortest ``{{ env.NAME }}`` each time), so ``{{env.A}}.x{{env.B}}`` is two."""
    return len(ENV_TEMPLATE_RE.findall(text)) == 1 and not ENV_TEMPLATE_RE.sub("", text).strip()


def _pairing_names(authority: str) -> List[str]:
    """The variables of ``authority`` (a URL's authority, or a host field) that the operator must
    pair with the host: every one except a variable that is the whole host name.

    ``user@{{ env.H }}:5432`` leaves ``H`` alone (the operator's variable makes the address; the
    contract named only which one) but not a variable in the userinfo or the port, nor one with
    text beside it (``{{ env.H }}.example``, ``db-{{ env.H }}``), nor a second variable.
    """
    userinfo, at, hostport = authority.rpartition("@")
    port_at = hostport.rfind(":")
    if port_at != -1 and hostport.find("]", port_at) == -1:
        hostname, port = hostport[:port_at], hostport[port_at:]
    else:
        hostname, port = hostport, ""
    names = [raw.strip() for raw in ENV_TEMPLATE_RE.findall(userinfo + port)]
    if _is_one_template(hostname):
        return names
    return names + [raw.strip() for raw in ENV_TEMPLATE_RE.findall(hostname)]


def check_host_templates(
    value: Any,
    *,
    environ: Optional[Mapping[str, str]] = None,
    stage: Optional[str] = None,
) -> None:
    """Refuse an ``{{ env.NAME }}`` that would put the operator's environment into an address.

    Run on the contract as written, before :func:`render_contract` expands it, and
    for a string that is a URL or sits in a host field (:func:`is_host_field`):

    * a credential-shaped name (anywhere in that value) is refused;
    * any other name in the URL's authority, or in a host field, is refused unless
      the operator lists it with every host the value renders to
      (:func:`fluid_build.secrets.policy.variable_allowed`, so a loopback host needs no
      entry), or it is the whole host name: ``host: "{{ env.PGHOST }}"`` names which
      variable holds the address, and the operator chose the address. A variable that
      is unset is left to :func:`render_contract`, which says so.

    Raises :class:`EnvTemplateError` naming the variable and the field, never the
    rendered value.
    """
    from fluid_build.observability.secret_redactor import is_sensitive_key_name
    from fluid_build.secrets.policy import variable_allowed

    env = os.environ if environ is None else environ

    def check(text: str, where: str, key: str) -> None:
        if "{{" not in text or ENV_TEMPLATE_RE.search(text) is None:
            return
        url = _URL_START.match(text)
        if url is None and not is_host_field(key):
            return
        if url is not None and _control_free(text[: url.end() - 3]).lower() in _STORE_SCHEMES:
            return  # a bucket or a container of a store the engine is configured for, not a server
        if url is None:
            body = text.strip()
            if body.startswith("/") and not body.startswith("//"):
                return  # a path (a REST stream's ``endpoint: /v1/users``), not an address
        for raw in ENV_TEMPLATE_RE.findall(text):
            name = raw.strip()
            if is_sensitive_key_name(name):
                raise _host_refusal(name, where, credential=True, stage=stage)
        authority = _authority_of(text, url)
        if authority is None:
            return
        # A client that reads a backslash as a slash ends the authority there; one that does not
        # reads on to the ``@``. Either could be the one that connects, so with a backslash in
        # the authority every variable in it is paired, with both hosts.
        slashed = "\\" in authority
        names = (
            [raw.strip() for raw in ENV_TEMPLATE_RE.findall(authority)]
            if slashed
            else _pairing_names(authority)
        )
        if not names:
            return
        if any(not _has_value(env.get(raw.strip())) for raw in ENV_TEMPLATE_RE.findall(authority)):
            return  # render_contract reports the missing variable
        rendered = ENV_TEMPLATE_RE.sub(lambda m: env[m.group(1).strip()], authority)
        hosts = _hosts_of(rendered) or [""]
        if slashed:
            hosts += _hosts_of(rendered.partition("\\")[0]) or [""]
        for name in names:
            for host in hosts:
                if not variable_allowed(name, host or None, environ=env):
                    raise _host_refusal(name, where, credential=False, stage=stage)

    def walk(node: Any, where: str, key: str) -> None:
        if isinstance(node, str):
            check(node, where, key)
        elif isinstance(node, list):
            for index, item in enumerate(node):
                walk(item, f"{where}[{index}]", key)
        elif isinstance(node, dict):
            for field, item in node.items():
                walk(item, f"{where}.{field}" if where else str(field), str(field))

    walk(value, "", "")


def missing_fields(value: Any, environ: Optional[Mapping[str, str]] = None) -> Dict[str, List[str]]:
    """The variables ``value`` names that are unset or empty here, each with the fields that
    write it (``exposes[0].binding.location.bucket``), sorted by name.

    Credential-shaped names are left out: they are never rendered, so they are
    not waiting for a value. What ``fluid validate`` warns about; the same
    variables :func:`render_contract` refuses.
    """
    from fluid_build.observability.secret_redactor import is_sensitive_key_name

    env = os.environ if environ is None else environ
    found: Dict[str, List[str]] = {}

    def walk(node: Any, where: str) -> None:
        if isinstance(node, str):
            if "{{" in node:
                for raw in ENV_TEMPLATE_RE.findall(node):
                    name = raw.strip()
                    if not is_sensitive_key_name(name) and not _has_value(env.get(name)):
                        found.setdefault(name, []).append(where)
        elif isinstance(node, list):
            for index, item in enumerate(node):
                walk(item, f"{where}[{index}]")
        elif isinstance(node, dict):
            for key, item in node.items():
                walk(item, f"{where}.{key}" if where else str(key))

    walk(value, "")
    return {name: found[name] for name in sorted(found)}


__all__ = [
    "ENV_TEMPLATE_RE",
    "HOST_FIELD_NAMES",
    "HOST_FIELD_WORDS",
    "EnvTemplateError",
    "SENSITIVE_REFUSE",
    "SENSITIVE_RENDER",
    "UNSET_DEFER",
    "UNSET_ERROR",
    "check_host_templates",
    "missing_fields",
    "placeholder_names",
    "render_contract",
    "render_text",
]
