# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The operator's policy for secret references: which may resolve, and where a value may go.

A contract names a secret and, beside it, the host the value is sent to. Both
halves are the contract's to choose, so without a policy a contract run where
the operator's identity can read secrets (CI, an operator's shell, a Command
Center runner) could fetch any secret and send it to a host of its choosing.
The operator decides; the contract only names.

The policy has four parts, all read from the operator's environment (the shell,
CI, or the operator's ``FLUID_SECRETS_FILE``) and never from a project file:
every name below is registered in ``credentials/protected_env.py``, so a value
a project dotenv file sets is ignored. ``fluid apply`` hydrates the environment
from ``.env`` files committed beside the contract; without that rule a contract
repository could grant itself the policy meant to constrain it.

* :data:`SCHEMES_ENV` -- the schemes that resolve. Default: the four core
  resolves itself (``env``, ``file``, ``vault``, ``aws-sm``). A pack's scheme
  resolves only once the operator lists it: installing a package is not
  consent.
* :data:`PREFIXES_ENV` -- per scheme, the locators a contract may name, for
  example ``vault://kv/orgs/acme/`` or ``aws-sm://fluid/acme/``. A scheme with
  no entry is unrestricted (the store's own access policy is the first lock),
  except ``file``, which defaults to ``/run/secrets/`` (Docker and Kubernetes
  mounted secrets): a contract must not read an arbitrary file of the
  operator's. The model is Kafka Connect's FileConfigProvider ``allowed.paths``.
* :data:`STAGES_ENV` -- the stages that may fetch a value a contract names.
  Default: ``apply``. ``validate``, ``plan``, ``bundle`` and ``diff`` never
  fetch one, whatever is listed: they lint references (design invariant I4).
  A provider credential the credential chain reads from the operator's
  ``FLUID_CREDENTIAL_STORE`` (``resolve_credential``) is not stage-gated:
  forge, not the contract, names that key.
* :data:`ALLOWLIST_ENV` -- where a resolved value may be sent. A loopback host
  needs no entry: sending to your own machine discloses nothing. Every
  destination that leaves the machine needs one. Entries, comma-separated:

  - ``NAME`` / ``NAME@host`` -- environment variable ``NAME`` (``env://NAME``)
    to any host / to that host. The original form, chosen by the founder on
    2026-10-26 for ``env://``.
  - ``<reference>@<host>`` / ``<reference>@*`` -- that reference, any scheme,
    to that host / any host, for example ``vault://kv/acme/pg#password@db.acme.internal``.
    The ``@<host>`` part is required in this form, because a Secrets Manager
    name may itself contain ``@``.
  - ``<reference-prefix>*@<host>`` -- every reference starting with the
    prefix, for example ``aws-sm://fluid/acme/*@db.acme.internal``.

  A host written with a port never matches: leave the port off.

The destination policy used to cover ``env://`` only, on the reasoning that the
other schemes name a store the operator provisioned. That left the same hole one
step removed: a contract could name any secret the operator's identity can read
and send it to its own host. It now covers every scheme (design section 4.4).

This mirrors the federation credential policy in ``forge/federation.py``, which
solved the same problem for ``federation/upstreams.yaml``. The one deliberate
difference: federation defaults ``GITHUB_TOKEN`` to GitHub hosts because a
token's destination is self-evident there. An acquisition source is an
arbitrary database at an arbitrary host, so no reference has an obvious
destination and none is allowed by default.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Tuple, Union
from urllib.parse import urlsplit

from fluid_build._errors import doc_url
from fluid_build._net import is_loopback_host

from .errors import SecretPolicyError, SecretRefError
from .ref import ALIASES, CORE_SCHEMES, SecretRef, parse_secret_ref

#: Operator-only. Comma-separated destination entries (module docstring).
ALLOWLIST_ENV = "FLUID_ACQUISITION_SECRET_REF_ALLOWLIST"
#: Operator-only. Comma-separated schemes that may resolve.
SCHEMES_ENV = "FLUID_SECRET_SCHEMES"
#: Operator-only. Comma-separated ``<scheme>://<locator-prefix>`` entries.
PREFIXES_ENV = "FLUID_SECRET_PREFIXES"
#: Operator-only. Comma-separated stage names that may fetch a value.
STAGES_ENV = "FLUID_SECRET_STAGES"

DEFAULT_SCHEMES: FrozenSet[str] = frozenset(CORE_SCHEMES)
DEFAULT_PREFIXES: Dict[str, Tuple[str, ...]] = {"file": ("/run/secrets/",)}
DEFAULT_STAGES: FrozenSet[str] = frozenset({"apply"})
#: Stages that lint references and never fetch a value, whatever is configured.
NEVER_RESOLVE_STAGES: FrozenSet[str] = frozenset({"validate", "plan", "bundle", "diff"})

#: Every field that can carry a destination. ALL of them are checked, not just
#: the first that parses: a contract that set ``host: localhost`` as a decoy
#: beside a remote ``connection_string`` would otherwise be exempted on the
#: strength of the decoy. ``connection_string`` is debezium's mongodb shape.
_HOST_FIELDS = ("host", "uri", "instance_url", "connection_string", "server", "endpoint")

#: Connector settings that name where a connector connects, or the credential it
#: connects with, in Debezium's and Kafka Connect JDBC's spellings. The runner
#: derives them from the connection block, which this policy clears before the
#: secret is read; an override (Debezium's ``extra_config``) would send the
#: resolved value past that check, to a host the contract chose.
DESTINATION_OVERRIDE_KEYS: FrozenSet[str] = frozenset(
    {
        "database.hostname",
        "database.url",
        "mongodb.connection.string",
        "mongodb.hosts",
        "connection.url",
        "database.password",
        "mongodb.password",
        "connection.password",
    }
)

#: URI schemes that name an object store rather than a database server. Their
#: authority is a BUCKET, not a host, so reporting it as a destination both
#: refuses working contracts and suggests an allowlist entry that is nonsense.
_OBJECT_STORE_SCHEMES = frozenset({"s3", "s3a", "s3n", "gs", "gcs", "az", "azure", "abfss", "file"})

#: One ``host[:port]`` of a URI authority: a bracketed IPv6 address or a name,
#: then an optional numeric port. Anything else is a destination we cannot read.
_HOST_PORT = re.compile(r"(?:\[(?P<v6>[^\[\]]*)\]|(?P<name>[^\[\]:]*))(?::\d*)?")
#: One ``{{ env.NAME }}`` placeholder, as ``build_runners.base`` renders it
#: (``util.binding_paths.ENV_PLACEHOLDER_RE``): a name only, so it cannot hold a host.
_ENV_PLACEHOLDER = re.compile(r"\{\{\s*env\.([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


@dataclass
class AcquisitionSecretPolicyError(SecretPolicyError):
    """A secret would be sent somewhere the operator has not allowed.

    Raised BEFORE the reference is resolved, so the secret is never read. The
    message names the reference and the host and never contains the value.
    """

    code: str = "AcquisitionSecretPolicyError"


# ── stage names ──────────────────────────────────────────────────────────────


def normalize_stage(stage: str) -> str:
    """``policy_apply`` and ``policy-apply`` are one stage; compare them alike."""
    return stage.strip().lower().replace("_", "-")


# ── list parsing ─────────────────────────────────────────────────────────────


def _items(raw: Optional[str]) -> List[str]:
    return [part.strip() for part in (raw or "").split(",") if part.strip()]


def _canonical_scheme(written: str) -> str:
    lowered = written.strip().lower()
    return ALIASES.get(lowered, lowered)


def enabled_schemes(environ: Optional[Mapping[str, str]] = None) -> FrozenSet[str]:
    """The schemes that may resolve: the operator's list, else the core four."""
    env = os.environ if environ is None else environ
    listed = _items(env.get(SCHEMES_ENV))
    if not listed:
        return DEFAULT_SCHEMES
    return frozenset(_canonical_scheme(item.partition("://")[0]) for item in listed)


def locator_prefixes(
    environ: Optional[Mapping[str, str]] = None,
) -> Dict[str, Tuple[str, ...]]:
    """Per scheme, the locator prefixes a contract may name.

    A scheme the operator lists replaces its default; a scheme with no entry
    and no default is absent from the result, which means unrestricted.
    Malformed entries (no ``://``) are dropped: they grant nothing.
    """
    env = os.environ if environ is None else environ
    listed: Dict[str, List[str]] = {}
    for item in _items(env.get(PREFIXES_ENV)):
        written, sep, prefix = item.partition("://")
        if not sep or not written or not prefix:
            continue
        listed.setdefault(_canonical_scheme(written), []).append(prefix)
    out: Dict[str, Tuple[str, ...]] = dict(DEFAULT_PREFIXES)
    for scheme, prefixes in listed.items():
        out[scheme] = tuple(prefixes)
    return out


def _env_items(name: str, environ: Optional[Mapping[str, str]]) -> List[str]:
    """The comma/space list in operator variable ``name`` (``os.environ`` by default)."""
    env = os.environ if environ is None else environ
    return _items(env.get(name))


def allowed_stages(environ: Optional[Mapping[str, str]] = None) -> FrozenSet[str]:
    """The stages that may fetch a value. Never includes :data:`NEVER_RESOLVE_STAGES`."""
    listed = frozenset(normalize_stage(item) for item in _env_items(STAGES_ENV, environ))
    return (listed or DEFAULT_STAGES) - NEVER_RESOLVE_STAGES


def prefix_matches(locator: str, prefix: str) -> bool:
    """Whether ``locator`` is covered by one prefix entry.

    ``p*`` matches any locator starting with ``p``; ``p/`` matches locators
    under ``p/``; a bare ``p`` matches ``p`` itself and locators under ``p/``,
    so ``kv/orgs/acme`` never covers ``kv/orgs/acme-evil``.
    """
    if prefix.endswith("*"):
        return locator.startswith(prefix[:-1])
    if prefix.endswith("/"):
        return locator.startswith(prefix)
    return locator == prefix or locator.startswith(prefix + "/")


# ── resolution policy: scheme, prefix, stage ─────────────────────────────────


def _policy_error(what: str, why: str, fix: str) -> SecretPolicyError:
    return SecretPolicyError(what=what, why=why, fix=fix, doc=doc_url("secrets"))


def check_scheme(ref: SecretRef, environ: Optional[Mapping[str, str]] = None) -> None:
    """Raise unless the operator lets ``ref``'s scheme resolve."""
    schemes = enabled_schemes(environ)
    if ref.scheme in schemes:
        return
    current = ",".join(sorted(schemes))
    raise _policy_error(
        f"{ref.canonical} cannot resolve, because the {ref.scheme}:// scheme is not enabled",
        "The operator decides which secret stores a contract may read from.",
        f"Set it in the operator environment (a shell or CI variable, not a project .env): "
        f'{SCHEMES_ENV}="{current},{ref.scheme}".',
    )


def check_prefix(ref: SecretRef, environ: Optional[Mapping[str, str]] = None) -> None:
    """Raise unless ``ref``'s locator is under a prefix the operator allows for its scheme."""
    prefixes = locator_prefixes(environ).get(ref.scheme)
    if prefixes is None:
        return
    if any(prefix_matches(ref.locator, prefix) for prefix in prefixes):
        return
    listed = ", ".join(f"{ref.scheme}://{p}" for p in prefixes) or "none"
    raise _policy_error(
        f"{ref.canonical} cannot resolve, because its locator is outside the prefixes "
        f"allowed for {ref.scheme}://",
        f"The operator allows {ref.scheme}:// references under: {listed}.",
        f"If this is intended, add a prefix in the operator environment: "
        f'{PREFIXES_ENV}="{ref.scheme}://<prefix>/" (comma-separated, one per entry).',
    )


def check_stage(
    ref: SecretRef, stage: Optional[str], environ: Optional[Mapping[str, str]] = None
) -> None:
    """Raise unless ``stage`` may fetch a value a contract names (design invariant I4).

    ``resolve_secret`` calls it; ``resolve_credential`` (a provider credential
    the chain reads from ``FLUID_CREDENTIAL_STORE``) does not.
    """
    if stage is None:
        raise _policy_error(
            f"{ref.canonical} was asked for outside a pipeline stage",
            "A secret value is fetched only by the stage that sends it, so a lookup "
            "with no stage running is refused.",
            "Run the stage that uses it (fluid apply). Code calling the resolver "
            "directly runs it inside fluid_build.secrets.secret_run('apply').",
        )
    name = normalize_stage(stage)
    if name in NEVER_RESOLVE_STAGES:
        raise _policy_error(
            f"{ref.canonical} was not fetched, because `fluid {name}` never fetches a "
            "secret value",
            f"{name} checks references against the policy and the grammar without "
            "reading them; only the stage that sends a value fetches it.",
            "Nothing to change here: the value is read when fluid apply runs.",
        )
    stages = allowed_stages(environ)
    if name in stages:
        return
    current = ",".join(sorted(stages))
    raise _policy_error(
        f"{ref.canonical} was not fetched, because the {name} stage may not fetch secret values",
        f"The operator lets these stages fetch values: {current}.",
        f"If {name} must send this value, set it in the operator environment: "
        f'{STAGES_ENV}="{current},{name}".',
    )


# ── destination policy ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class DestinationGrant:
    """One allowlist entry: a reference (or prefix) and a host (``None`` is any host)."""

    reference: Optional[str]
    prefix: Optional[str]
    host: Optional[str]

    def covers(self, ref: SecretRef) -> bool:
        if self.reference is not None:
            return self.reference == ref.canonical
        return self.prefix is not None and ref.canonical.startswith(self.prefix)


def _host_part(text: str) -> Optional[str]:
    """``*`` is any host; otherwise the lower-cased name without a trailing dot."""
    host = text.strip().lower().rstrip(".")
    return None if host == "*" else host


def _grant(entry: str) -> Optional[DestinationGrant]:
    """Parse one entry; ``None`` for a malformed one, which grants nothing."""
    if "://" not in entry:
        name, sep, host = entry.partition("@")
        name = name.strip()
        if not name:
            return None
        if sep:
            target = host.strip().lower().rstrip(".")
            if not target:
                return None  # `NAME@` grants nothing, not everything
            return DestinationGrant(reference=f"env://{name}", prefix=None, host=target)
        return DestinationGrant(reference=f"env://{name}", prefix=None, host=None)
    left, sep, host = entry.rpartition("@")
    if not sep or not left.strip() or not host.strip():
        return None  # the @<host> part is required in the reference form
    left = left.strip()
    if left.endswith("*"):
        written, _, rest = left[:-1].partition("://")
        return DestinationGrant(
            reference=None,
            prefix=f"{_canonical_scheme(written)}://{rest}",
            host=_host_part(host),
        )
    try:
        canonical = parse_secret_ref(left).canonical
    except SecretRefError:
        return None
    return DestinationGrant(reference=canonical, prefix=None, host=_host_part(host))


def destination_grants(environ: Optional[Mapping[str, str]] = None) -> List[DestinationGrant]:
    """The operator's destination allowlist, parsed; malformed entries dropped."""
    grants = (_grant(item) for item in _env_items(ALLOWLIST_ENV, environ))
    return [grant for grant in grants if grant is not None]


def allowlist(environ: Optional[Mapping[str, str]] = None) -> List[Tuple[str, Optional[str]]]:
    """The ``env://`` entries as ``(variable, host-or-None)`` pairs, for messages.

    The streaming lanes judge an environment-variable catalog token by these.
    """
    out: List[Tuple[str, Optional[str]]] = []
    for grant in destination_grants(environ):
        if grant.reference is not None and grant.reference.startswith("env://"):
            out.append((grant.reference[len("env://") :], grant.host))
    return out


def _authority_hosts(authority: str) -> List[Optional[str]]:
    """Every host a URI authority names; ``None`` for a part we cannot read.

    MongoDB, libpq and pgjdbc take a comma-separated host list
    (``mongodb://u:p@h1:27017,h2:27017/db``) and may send the credential to any
    of them. ``urlsplit(...).hostname`` reads only the first, so a loopback
    first host would exempt a remote second one.
    """
    hosts: List[Optional[str]] = []
    for piece in authority.rpartition("@")[2].split(","):
        match = _HOST_PORT.fullmatch(piece.strip())
        name = (match.group("v6") or match.group("name") or "") if match else ""
        hosts.append(name.strip().lower() or None)
    return hosts


def _port_is_readable(port: Any) -> bool:
    """Whether ``port`` is absent or a port number, so it cannot carry a host.

    Debezium (``mongodb://{host}:{port}``) and the Kafka Connect JDBC URL
    (``jdbc:postgresql://{host}:{port}/db``) put it right after the host, where
    ``5432,evil.example:5432`` would add a host the policy never saw. One
    unrendered ``{{ env.NAME }}`` passes: ``fluid apply`` renders it before a
    runner asks for the secret, and this check judges the rendered port then.
    """
    if port is None:
        return True
    if isinstance(port, bool):
        return False
    if isinstance(port, int):
        return True
    if isinstance(port, str):
        text = port.strip()
        return (
            text == ""
            or (text.isascii() and text.isdigit())
            or _ENV_PLACEHOLDER.fullmatch(text) is not None
        )
    return False


def _blank(raw: Any) -> bool:
    """``None``, or a string of whitespace only: a field that names nothing."""
    return raw is None or (isinstance(raw, str) and not raw.strip())


def _is_object_store_uri(text: str) -> bool:
    """Whether ``text`` is an object-store URI, whose authority is a bucket, not a host."""
    return "://" in text and text.partition("://")[0].strip().lower() in _OBJECT_STORE_SCHEMES


def connection_hosts(connection: Mapping[str, Any]) -> List[Optional[str]]:
    """Every destination this connection block could send a credential to.

    A list, because the policy has to clear ALL of them: trusting the first
    field that happened to parse let a contract put ``host: localhost`` beside
    a remote ``connection_string`` and be exempted on the decoy.

    An entry is ``None`` when a present field names something we cannot read as
    a host, which the policy treats as remote -- a destination we cannot read is
    not one we can clear. An object-store URI (``s3://bucket/...``) is skipped
    rather than reported as a host: its authority is a bucket.

    A ``host`` that is present but names no host (empty, ``null``, not a
    string) is ``None`` too, not the absence of a destination: a database
    client given no host picks one itself, and libpq reads ``PGHOST``, which a
    project dotenv file may set (``credentials/protected_env.py`` pairs it only
    with ``PGPASSWORD``). An unset ``{{ env.PGHOST }}`` renders to exactly that.
    Any other destination field that is empty names nothing and is skipped.

    Every host of a host list counts (:func:`_authority_hosts`), and a ``port``
    that is not a port number adds a ``None`` (:func:`_port_is_readable`).
    """
    out: List[Optional[str]] = []
    for field_name in _HOST_FIELDS:
        if field_name in connection:
            out.extend(_field_hosts(field_name, connection[field_name]))
    if not _port_is_readable(connection.get("port")):
        out.append(None)
    return out


def _field_hosts(field_name: str, raw: Any) -> List[Optional[str]]:
    """The destinations one present field names, as :func:`connection_hosts` counts them."""
    if field_name == "host":
        return [raw.strip() if isinstance(raw, str) and raw.strip() else None]
    if _blank(raw):
        return []
    if not isinstance(raw, str):
        return [None]
    text = raw.strip()
    if _is_object_store_uri(text):
        return []
    try:
        parsed = urlsplit(text if "//" in text else f"//{text}")
    except ValueError:
        return [None]
    return _authority_hosts(parsed.netloc)


def connection_host(connection: Mapping[str, Any]) -> Optional[str]:
    """The first remote destination :func:`connection_hosts` found, for messages."""
    hosts = connection_hosts(connection)
    for host in hosts:
        if not is_loopback(host):
            return host
    return hosts[0] if hosts else None


def is_loopback(host: Optional[str]) -> bool:
    """Whether ``host`` names this machine, so sending it a secret leaves nowhere.

    ``_net.is_loopback_host`` is the one definition, shared with
    ``apply_loopback_host_override``: the exemption here is sound only because
    the override rewrites exactly the hosts that helper calls loopback. So
    ``localhost``, the whole of ``127.0.0.0/8`` and ``::1`` are loopback, and a
    name like ``db.localhost`` is NOT -- it needs an allowlist entry. No DNS: a
    name that resolves to loopback now can resolve elsewhere mid-run.
    """
    if host is None:
        return False
    # A trailing dot is the fully-qualified spelling of the same name.
    return is_loopback_host(host.strip().rstrip(".") or host)


def _grants_allow(ref: SecretRef, host: Optional[str], grants: List[DestinationGrant]) -> bool:
    target = (host or "").strip().lower().rstrip(".")
    for grant in grants:
        if not grant.covers(ref):
            continue
        if grant.host is None:
            return True
        if target and grant.host == target:
            return True
    return False


def variable_allowed(
    variable: str,
    host: Optional[str],
    *,
    environ: Optional[Mapping[str, str]] = None,
    loopback_exempt: bool = True,
) -> bool:
    """Whether environment variable ``variable`` may be sent to ``host``: the decision alone.

    For a caller that phrases its own refusal: the managed Flink lane's catalog
    token (``streaming/policy.py``) and the Iceberg sinks' catalog token
    (``kafka_connect/iceberg_sink_validation.py``). A loopback host needs no
    entry unless ``loopback_exempt`` is false: a send made from somewhere other
    than this process, where loopback is not this machine (a pod's own network
    namespace), is judged by the allowlist alone.
    """
    if loopback_exempt and is_loopback(host):
        return True
    ref = SecretRef(scheme="env", locator=variable, written_scheme="env")
    return _grants_allow(ref, host, destination_grants(environ))


def refuse_destination_overrides(overrides: Mapping[str, Any], *, where: str) -> None:
    """Raise when ``overrides`` would move a connector's destination or credential.

    Called before the connection's secret is resolved, so a refusal reads nothing.
    """
    for key in overrides:
        if str(key).strip().lower() not in DESTINATION_OVERRIDE_KEYS:
            continue
        raise AcquisitionSecretPolicyError(
            what=f"{where} sets {str(key)!r}, which would move where the secret is sent",
            why=(
                "The connector's host and credential come from the connection block, which "
                "the operator's secret policy clears before the secret is read. An override "
                "would send the resolved value past that check, to a host the contract chose."
            ),
            fix=(
                f"Remove {str(key)!r} from {where}. Name the host in the connection block "
                "(connection.host, or connection.connection_string for MongoDB) and, if it "
                f"is remote, have the operator allow it in {ALLOWLIST_ENV}."
            ),
            doc=doc_url("secrets"),
        )


def _suggested_entry(ref: SecretRef, host: Optional[str]) -> str:
    if ref.scheme == "env":
        return f"{ref.locator}@{host}" if host else ref.locator
    return f"{ref.canonical}@{host}" if host else f"{ref.canonical}@*"


def _names_an_object_store(connection: Mapping[str, Any]) -> bool:
    """Whether a destination field of ``connection`` is an object-store URI."""
    return any(
        isinstance(connection.get(field_name), str)
        and _is_object_store_uri(connection[field_name].strip())
        for field_name in _HOST_FIELDS
        if field_name != "host"
    )


def _destination_targets(
    connection: Optional[Mapping[str, Any]], host: Optional[str]
) -> Optional[List[Optional[str]]]:
    """Every destination to judge, or ``None`` when the connection has none to judge.

    ``None`` only for a connection whose one destination is an object store (the
    known gap :func:`refuse_unless_allowed` names). Fields that are present but
    empty are not that: they leave the client to pick its host, so the result is
    a destination this policy cannot clear, as for a connection naming none.
    """
    targets: List[Optional[str]] = []
    if connection is not None:
        targets.extend(connection_hosts(connection))
        if not targets and _names_an_object_store(connection):
            return None
    if host is not None or not targets:
        targets.append(host)
    return targets


def _what_sent(parsed: SecretRef) -> str:
    if parsed.scheme == "env":
        return f"environment variable {parsed.locator!r}"
    return f"secret {parsed.canonical}"


#: Where an allowlist entry must be set, for the fix of every destination refusal.
_OPERATOR_ONLY = (
    "the OPERATOR environment (a shell or CI variable, not a project .env, which is "
    "ignored for this setting)"
)


def _host_is_blank(connection: Mapping[str, Any]) -> bool:
    """Whether ``connection`` has a ``host`` that is empty or ``null`` (not one left out)."""
    return "host" in connection and _blank(connection["host"])


def _blank_host_refusal(parsed: SecretRef) -> AcquisitionSecretPolicyError:
    """The refusal for a connection whose ``host`` is present and names no host."""
    return AcquisitionSecretPolicyError(
        what=(
            f"the contract would send {_what_sent(parsed)} as a credential to a host the "
            "client picks itself, because connection.host is empty"
        ),
        why=(
            "A database client given no host picks one on its own: libpq reads PGHOST, "
            "which a project .env file may set. So this check cannot tell where the value "
            "goes, and a destination it cannot read is not one it can clear."
        ),
        fix=(
            "Name the host in connection.host. A placeholder such as {{ env.PGHOST }} "
            "renders empty when the variable is not set where fluid apply runs, so set it "
            f"there. To send the value to any host instead, set it in {_OPERATOR_ONLY}: "
            f'{ALLOWLIST_ENV}="{_suggested_entry(parsed, None)}".'
        ),
        doc=doc_url("secrets"),
    )


def _destination_refusal(parsed: SecretRef, target: Optional[str]) -> AcquisitionSecretPolicyError:
    where = f"host {target!r}" if target else "a host that could not be determined"
    what_sent = _what_sent(parsed)
    entry = _suggested_entry(parsed, target)
    any_host = _suggested_entry(parsed, None)
    return AcquisitionSecretPolicyError(
        what=f"the contract would send {what_sent} to {where} as a credential",
        why=(
            "A contract names both the secret to read and the host it is sent to, so "
            "without an operator allowlist any secret this run can read could be sent "
            "to a host the contract chose. A loopback host needs no entry; every "
            "destination that leaves this machine does."
        ),
        fix=(
            f"If this is intended, set it in {_OPERATOR_ONLY}: "
            f'{ALLOWLIST_ENV}="{entry}" for that host only, or '
            f'{ALLOWLIST_ENV}="{any_host}" for any host. A host written with a port '
            "never matches, so leave the port off."
        ),
        doc=doc_url("secrets"),
    )


def refuse_unless_allowed(
    ref: Union[SecretRef, str],
    *,
    connection: Optional[Mapping[str, Any]] = None,
    host: Optional[str] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> None:
    """Raise unless ``ref`` may be sent to every destination given.

    Called before the reference is resolved, so a refusal never reads the
    secret. Every scheme is policed.

    ``connection`` is the preferred argument: EVERY destination it carries has
    to clear, so a decoy loopback field cannot mask a remote sibling. ``host``
    is for call sites that know their one destination directly (an
    orchestrator URL). A connection whose only destination is an object store
    has no host to judge, and passes: that credential is not covered here (a
    known gap). A ``host`` that is present but empty is judged as a host this
    policy cannot read (:func:`connection_hosts`), so it passes only under an
    entry for any host.
    """
    parsed = parse_secret_ref(ref) if isinstance(ref, str) else ref
    targets = _destination_targets(connection, host)
    if targets is None:
        return
    grants = destination_grants(environ)
    for target in targets:
        if is_loopback(target) or _grants_allow(parsed, target, grants):
            continue
        if target is None and connection is not None and _host_is_blank(connection):
            raise _blank_host_refusal(parsed)
        raise _destination_refusal(parsed, target)


# ── destinations only fluid apply can read ───────────────────────────────────


def renders_at_apply(field_name: str, value: Any) -> bool:
    """Whether the host in ``value`` is a ``{{ env.NAME }}`` placeholder.

    ``fluid apply`` renders every such placeholder from its own environment
    before a runner asks for the secret (``build_runners/base.py``), and this
    policy then judges the host it names. ``fluid validate`` sees the
    placeholder, which no allowlist entry can match, so its lint leaves that
    destination to apply. Only where the placeholder decides the host: anywhere
    in ``host``, and in a URI's scheme or the host part of its authority, not in
    its user name or path. The same rule as
    ``kafka_connect.iceberg_sink_validation._rendered_at_run``; Terraform defers
    a check on a value known only after apply the same way.
    """
    if not isinstance(value, str) or not _ENV_PLACEHOLDER.search(value):
        return False
    return any(_ENV_PLACEHOLDER.search(part) for part in _deciding_parts(field_name, value))


def _deciding_parts(field_name: str, value: str) -> Tuple[str, str]:
    """The scheme and the host part of a written destination, the parts that decide the host.

    ``host`` is all host part. A URI's user name, password, path, query and
    fragment never decide where it goes, so they are left out.
    """
    text = value.strip()
    if field_name == "host":
        return "", text
    scheme, sep, rest = text.partition("://")
    if not sep:
        scheme, rest = "", text
    authority = re.split(r"[/?#]", rest, maxsplit=1)[0]
    return scheme, authority.rpartition("@")[2]


def _shown_destination(name: str, value: str) -> str:
    """What the deferred notice quotes of ``value``: only its :func:`_deciding_parts`."""
    scheme, host = _deciding_parts(name.rpartition(".")[2], value)
    return f"{scheme}://{host}" if scheme else host


def destinations_rendered_at_apply(connection: Mapping[str, Any]) -> List[str]:
    """The destination fields of ``connection`` whose host :func:`renders_at_apply`."""
    return [
        field_name
        for field_name in _HOST_FIELDS
        if renders_at_apply(field_name, connection.get(field_name))
    ]


def deferred_destination_notice(
    ref: Union[SecretRef, str], written: Mapping[str, str]
) -> SecretPolicyError:
    """What ``fluid validate`` says about a destination it leaves to ``fluid apply``.

    ``written`` maps each such field, as the user knows it
    (``connection.host``), to its value as written. Only the contract's own
    text is quoted: the lint never reads the environment, so it never prints a
    value a placeholder names. Of a URI it quotes the scheme and the host part
    alone, never a user name or password written inline, nor the path.
    """
    parsed = parse_secret_ref(ref) if isinstance(ref, str) else ref
    shown = {name: _shown_destination(name, value) for name, value in written.items()}
    fields = ", ".join(f"{name} ({value!r})" for name, value in shown.items())
    variables = sorted(
        {match.group(1) for value in shown.values() for match in _ENV_PLACEHOLDER.finditer(value)}
    )
    return SecretPolicyError(
        what=(
            f"{parsed.canonical} is sent to the host {fields} renders to, which only "
            "fluid apply knows"
        ),
        why=(
            "fluid apply renders {{ env.NAME }} placeholders from the environment it runs "
            "in, then judges the host they name against the destination allowlist. This "
            "check does not read the environment, so it leaves that host to apply; an "
            "allowlist entry naming the placeholder itself never matches."
        ),
        fix=(
            f"Unless it renders to a loopback host, allow that host in {_OPERATOR_ONLY} "
            f'where fluid apply runs: {ALLOWLIST_ENV}="{_suggested_entry(parsed, "<host>")}", '
            f"with <host> the host it renders to there (with {', '.join(variables)} as set "
            "there), without a port."
        ),
        doc=doc_url("secrets"),
    )
