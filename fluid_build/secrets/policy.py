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
    2026-10-26 for ``env://``. ``NAME@*`` is any host too.
  - ``<reference>@<host>`` / ``<reference>@*`` -- that reference, any scheme,
    to that host / any host, for example ``vault://kv/acme/pg#password@db.acme.internal``.
    The ``@<host>`` part is required in this form, because a Secrets Manager
    name may itself contain ``@``.
  - ``<reference-prefix>*@<host>`` -- every reference starting with the
    prefix, for example ``aws-sm://fluid/acme/*@db.acme.internal``.

  A host written with a port never matches: leave the port off.

The destinations judged (:func:`connection_hosts`) are ``host``, the host part
of every URI field, each host of a host list, the host of an SSH tunnel in
use, and a URI anywhere else in the block: the Singer and Airbyte runners hand
the whole block to a connector, whose own settings may hold one. A driver can
be pointed elsewhere by a parameter that names a host, in a URI's query string
or in a mapping a connector reads as one, or by a database name a connection
URL reads as more than a name; this policy cannot judge either, so beside a
secret both are refused unless the reference may go to any host.

The settings that name a host are listed here, a denylist, rather than the
settings a connector may hold, an allowlist: a connector's own settings cannot
all be listed, and refusing each one this policy does not know would leave an
operator only an entry for any host, which judges nothing. So a host that a
connector reads from a setting of its own as a bare name, not a URI, is not
judged.

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
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, Iterator, List, Mapping, Optional, Set, Tuple, Union
from urllib.parse import unquote, unquote_plus, urlsplit

from fluid_build._errors import doc_url
from fluid_build._net import is_loopback_host
from fluid_build._validation_issues import dotted as _label
from fluid_build._validation_issues import nearest_key as _nearest_key
from fluid_build.util.binding_paths import resolve_env_placeholders

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
#: strength of the decoy. ``connection_string`` is debezium's mongodb shape;
#: ``sqlalchemy_url`` is the Singer taps' (tap-postgres, tap-mysql), which wins
#: over their ``host`` and ``port``.
_HOST_FIELDS = (
    "host",
    "uri",
    "instance_url",
    "connection_string",
    "server",
    "endpoint",
    "sqlalchemy_url",
)

#: Connector settings that name where a connector connects, or the credential it
#: connects with, in Debezium's and Kafka Connect JDBC's spellings. The runner
#: derives them from the connection block, which this policy clears before the
#: secret is read; an override (Debezium's ``extra_config``) would send the
#: resolved value past that check, to a host the contract chose. The port and
#: the database go into the JDBC URL Debezium builds next to the host, and the
#: connector class decides which of the settings are read at all.
DESTINATION_OVERRIDE_KEYS: FrozenSet[str] = frozenset(
    {
        "database.hostname",
        "database.port",
        "database.dbname",
        "database.names",
        "database.url",
        "mongodb.connection.string",
        "mongodb.hosts",
        "rac.nodes",
        "connection.url",
        "connector.class",
        "database.password",
        "mongodb.password",
        "connection.password",
    }
)
#: Debezium hands every ``database.*`` and ``driver.*`` setting, its prefix
#: dropped, to the JDBC driver (``RelationalDatabaseConnectorConfig``:
#: ``config.subset("database.", true).merge(config.subset("driver.", true))``)
#: and builds the JDBC URL from some of them. These, under either prefix, name
#: where it connects or what it connects with; so does a driver property that
#: names a host (:func:`_host_key_name`), such as mssql-jdbc's
#: ``failoverPartner``, which gets the same login when the first host fails.
_PASS_THROUGH_PREFIXES = ("database.", "driver.")
_PASS_THROUGH_DESTINATIONS = frozenset(
    {"hostname", "port", "dbname", "names", "url", "password", "protocol"}
)

#: Query-string parameters that name the host a driver connects to, or a file or
#: a connection string that does, spelled as :func:`_host_key_name` compares them
#: (lower case, letters and digits only). A driver can take its host from the
#: query string as well as from the URI's host part, and drivers differ on which
#: wins: libpq lets the query win (``conninfo_uri_parse_params``), and
#: SQLAlchemy's dialects merge the query into the DBAPI's keyword arguments
#: (``opts.update(url.query)``). This policy reads the host part only, so a
#: query that names a host is refused beside a secret rather than guessed at.
_QUERY_HOST_KEYS: FrozenSet[str] = frozenset(
    {
        # libpq keywords, which SQLAlchemy's psycopg dialects pass through
        # (``service`` reads its parameters from a service file)
        "host",
        "hostaddr",
        "service",
        # pgjdbc's spellings of the same properties
        "pghost",
        "pghostaddr",
        "pgservice",
        # MySQL drivers: mysqlclient, PyMySQL, MySQL Connector/J
        "unixsocket",
        "readdefaultfile",
        "readdefaultgroup",
        "socksproxyhost",
        # SQL Server: pyodbc (``odbc_connect`` is a whole ODBC string) and mssql-jdbc
        "odbcconnect",
        "dsn",
        "server",
        "servername",
        "address",
        "addr",
        "networkaddress",
        "datasource",
        "failoverpartner",
        "filedsn",
        # MongoDB's SOCKS5 proxy; Snowflake's account and region make its host
        "proxyhost",
        "account",
        "region",
        # clickhouse-driver's failover hosts
        "althosts",
        # a host or a URL under another name
        "hostname",
        "url",
        "uri",
        "endpoint",
        "connectionstring",
        # a class that opens the driver's socket, to wherever it likes: pgjdbc's
        # ``socketFactory``, mssql-jdbc's ``socketFactoryClass``
        "socketfactory",
        "socketfactoryclass",
    }
)

#: Connection fields that hold a query string of their own: Airbyte's JDBC
#: sources read ``jdbc_url_params`` (``key=value&...``) into the properties they
#: hand the JDBC driver. Written as a mapping, its keys are read the same way.
_QUERY_STRING_FIELDS = ("jdbc_url_params",)

#: Connection fields holding a mapping that a Singer tap merges into its
#: connection URL's query string, which the SQLAlchemy dialect then passes to
#: the driver as keyword arguments (``opts.update(url.query)``): BuzzCutNorman's
#: tap-mssql ``sqlalchemy_url_query`` (``update_query_dict``) and MeltanoLabs
#: tap-mysql's ``sqlalchemy_options`` (``URL.create(query=...)``). Any other
#: field named ``*_query`` that holds a mapping is read the same way.
_QUERY_MAPPING_FIELDS = ("sqlalchemy_url_query", "sqlalchemy_options")
#: Mappings a tap hands its driver's connect call as keyword arguments:
#: tap-mssql passes ``sqlalchemy_eng_params`` to ``engine_from_config``, whose
#: ``connect_args`` reach the driver.
_DRIVER_ARGUMENT_PATHS = (("sqlalchemy_eng_params", "connect_args"),)
#: Query parameters that may name a Unix-domain socket on this machine rather
#: than a host: libpq's ``host`` (a value that looks like an absolute path names
#: the socket's directory) and the MySQL drivers' ``unix_socket``.
_SOCKET_QUERY_KEYS = frozenset({"host", "unixsocket"})
#: One absolute POSIX path: not a host list (``,``), not a UNC path (``//``,
#: ``\``), nothing a driver could decode once more (``%``), no blank.
_SOCKET_PATH = re.compile(r"/(?!/)[^\\,%\s\x00-\x1f\x7f-\x9f]*")
#: A value read as a URI wherever the block holds it, after any blank or control
#: character: ``scheme://`` (SQLAlchemy's ``ibm_db_sa://``, a scheme of several
#: parts such as ``mysql:loadbalance://``, and a placeholder scheme included), and
#: any JDBC URL, Oracle's ``jdbc:oracle:thin:@host`` too.
_URI_SHAPE = re.compile(
    r"[\s\x00-\x1f\x7f-\x9f]*(?:(?P<jdbc>jdbc:)|(?P<scheme>[A-Za-z][\w+.\-:]*|\{\{[^{}]*\}\})://)",
    re.IGNORECASE,
)
#: A field whose value may be a credential, which the walk for a URI never
#: reads, so a refusal never quotes any part of it (letters and digits only).
_CREDENTIAL_FIELD = re.compile(
    r"(?:password|passwd|pwd|passphrase|secret|token|apikey|privatekey|accesskey|credential)s?$"
)

#: URI schemes whose client never takes its host from the query string.
_UNROUTED_QUERY_SCHEMES = frozenset({"http", "https", "ws", "wss"})

#: Fields that runners and connectors put into a connection URL as its database
#: (``URL.create(database=...)``, ``jdbc:postgresql://host:port/<database>``).
_DATABASE_FIELDS = ("database", "dbname")
#: What such a URL reads in a database name as more than the name: ``?`` starts
#: the connection settings, ``;`` starts SQL Server's, ``&`` and ``#`` delimit
#: them, and ``@`` and ``\`` end a part in other parsers.
_URL_SYNTAX = "?#;&@\\"
#: Control characters (Unicode Cc), as ``providers/_sql_safety.py`` refuses them.
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")
#: Fields that name a host by their name wherever the block holds them, and how
#: each is read: as a bare host, or as a URI or a host list. The top-level
#: destination names, and the host lists of Airbyte's older MongoDB source
#: (``instance_type.host``, ``server_addresses``, ``cluster_url``).
_NAMED_HOST_FIELDS: Dict[str, str] = {
    **{name: "uri" for name in _HOST_FIELDS},
    "host": "host",
    "hostname": "host",
    "hosts": "uri",
    "server_addresses": "uri",
    "cluster_url": "uri",
}
#: What such a field holds when it names no host: a path (a REST stream's
#: ``endpoint: /v1/users``, never ``//host/...``), or words with an optional type
#: expression with digits only in its brackets (a column map's ``host: varchar``,
#: ``hostname: host_name``, ``numeric(10, 2)``). Anything else is judged, digits and non-ASCII
#: included: a resolver reads ``134744072`` and ``evil\u3002example`` as hosts. A
#: single word resolves only on the operator's own network.
_NOT_A_HOST = re.compile(
    r"/(?!/).*|[A-Za-z_]+(?:\s+[A-Za-z_]+)*\s*(?:\(\s*\d+(?:\s*,\s*\d+)?\s*\))?", re.DOTALL
)
#: Embedded or local JDBC databases, which name no host while their settings name
#: none: SQLite, H2 in memory or in a file, embedded Derby, HSQLDB in memory, in
#: a file or in a resource.
_EMBEDDED_JDBC = re.compile(
    r"[\s\x00-\x1f\x7f-\x9f]*jdbc:"
    r"(?:sqlite:|h2:(?:mem:|file:|~|\.|/)|derby:(?!//)|hsqldb:(?:mem|file|res):)",
    re.IGNORECASE,
)
#: How many values the walk for a destination elsewhere reads at most. A block
#: whose shared settings repeat past it is a destination this policy cannot read.
_WALK_LIMIT = 10_000
#: Singer SDK settings whose keys name a stream's fields, not where a connector
#: connects (``stream_maps: {users: {host: __NULL__}}``): a URI there is a
#: destination, a key is not.
_FIELD_MAP_SETTINGS = frozenset({"stream_maps", "stream_map_config"})

#: The SSH tunnels a client can connect through, as ``(field, host key, off)``:
#: the Singer taps' ``ssh_tunnel``, on only when ``enable`` is set and true
#: (MeltanoLabs tap-postgres and tap-mysql read ``.get("enable", False)``), and
#: Airbyte's ``tunnel_method``, off for ``tunnel_method: NO_TUNNEL`` in any case
#: (its connectors read no other spelling as no tunnel, and refuse one they do
#: not know).
_TUNNELS: Tuple[Tuple[str, str, Tuple[str, Any]], ...] = (
    ("ssh_tunnel", "host", ("enable", False)),
    ("tunnel_method", "tunnel_host", ("tunnel_method", "NO_TUNNEL")),
)

#: Top-level fields judged by name, which the walk for a destination elsewhere
#: leaves to their own rule: a tunnel is judged as one, on or off.
_JUDGED_BY_NAME = frozenset(
    {*_HOST_FIELDS, *_QUERY_STRING_FIELDS, *(field for field, _key, _off in _TUNNELS)}
)

#: URI schemes that name an object store rather than a database server. Their
#: authority is a BUCKET, not a host, so reporting it as a destination both
#: refuses working contracts and suggests an allowlist entry that is nonsense.
_OBJECT_STORE_SCHEMES = frozenset({"s3", "s3a", "s3n", "gs", "gcs", "az", "azure", "abfss", "file"})
#: Schemes the walk for a URI elsewhere passes over: an object store or a file
#: system, whose authority is a bucket, an account or a name node rather than a
#: database host (the duckdb runner's remote stores among them), and a local file
#: or socket. A field judged by name still reads only :data:`_OBJECT_STORE_SCHEMES`.
_UNJUDGED_SCHEMES = _OBJECT_STORE_SCHEMES | frozenset(
    {"abfs", "wasb", "wasbs", "adl", "r2", "oss", "hdfs", "dbfs", "cos", "sqlite", "unix"}
)

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
            # `NAME@*` is any host, as `env://NAME@*` and a bare `NAME` are.
            return DestinationGrant(reference=f"env://{name}", prefix=None, host=_host_part(target))
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

    So does every other way a connection can name where a driver connects: a
    query-string parameter that names a host (:data:`_QUERY_HOST_KEYS`), in a
    URI field or in ``jdbc_url_params``; a database name holding URL syntax
    (:data:`_URL_SYNTAX`), which a runner or connector puts into a connection
    URL; and the host of an SSH tunnel in use (:data:`_TUNNELS`), which adds a
    destination of its own and is ``None`` when it names no host.
    """
    return [target.host for target in _connection_targets(connection)]


@dataclass(frozen=True)
class _Target:
    """One destination of a connection, and where the connection names it.

    ``host`` is ``None`` for a destination this policy cannot read, and
    ``query_key`` or ``database_char`` then says why when the reason is a query
    parameter that names a host or URL syntax in a database name. ``tunnel``
    marks an SSH tunnel's host, and ``behind_tunnel`` a host the client reads
    from the tunnel host's side, where a loopback host is not this machine.
    ``local`` marks a Unix-domain socket a query names, which is this machine
    whatever tunnel the client uses. ``field`` is where the connection holds the
    destination, as a refusal names it, and ``path`` the same place as keys and
    list indexes.
    """

    host: Optional[str]
    field: Optional[str] = None
    query_key: Optional[str] = None
    database_char: Optional[str] = None
    tunnel: bool = False
    behind_tunnel: bool = False
    #: The field's text, for a keyword connection string (``Server=...;...``):
    #: a destination this policy cannot read, whose host part a refusal shows.
    keyword_text: Optional[str] = None
    local: bool = False
    path: Tuple[Any, ...] = ()
    #: For a ``query_key``: it is a key of a mapping, not of a URI's query string.
    query_mapping: bool = False
    #: A destination under a field that may hold a credential: judged, never quoted.
    quiet: bool = False

    @property
    def has_reason(self) -> bool:
        """Whether a query parameter or a database name is why this cannot be read."""
        return self.query_key is not None or self.database_char is not None


def _connection_targets(connection: Mapping[str, Any]) -> List[_Target]:
    """:func:`connection_hosts`, each destination with the field that names it."""
    # The tunnel host first: it is the first host the value reaches.
    tunnels = _tunnel_targets(connection)
    behind = bool(tunnels)
    out: List[_Target] = list(tunnels)
    for field_name in _HOST_FIELDS:
        if field_name in connection:
            out.extend(_field_targets(field_name, connection[field_name], (field_name,), behind))
    out.extend(_query_field_targets(connection))
    out.extend(_elsewhere_targets(connection, behind))
    out.extend(_database_targets(connection))
    if not _port_is_readable(connection.get("port")):
        out.append(_Target(None, "port", path=("port",)))
    return out


def _query_field_targets(connection: Mapping[str, Any]) -> List[_Target]:
    """The destinations the block's query strings and query mappings name."""
    out: List[_Target] = []
    for field_name in _QUERY_STRING_FIELDS:
        raw = connection.get(field_name)
        if isinstance(raw, (str, Mapping)):
            out.extend(_query_targets(raw, (field_name,)))
        elif raw is not None:
            out.append(_Target(None, field_name, path=(field_name,)))
    for path, raw in _query_mappings(connection):
        if not isinstance(raw, Mapping):
            out.append(_Target(None, _label(path), path=path))
            continue
        out.extend(_query_targets(raw, path))
        if path in _DRIVER_ARGUMENT_PATHS and _extends_a_connection_string(raw):
            out.append(_Target(None, _label(path), path=path))
    return out


def _elsewhere_targets(connection: Mapping[str, Any], behind: bool) -> List[_Target]:
    """The destinations :func:`_destinations_elsewhere` finds, read as :func:`_field_targets` reads them."""
    out: List[_Target] = []
    for path, reading, text in _destinations_elsewhere(connection):
        if reading == "unreadable":
            out.append(_Target(None, _label(path), path=path))
            continue
        secret = reading == "secret"
        out.extend(_field_targets("uri" if secret else reading, text, path, behind, quiet=secret))
    return out


def _extends_a_connection_string(arguments: Mapping[Any, Any]) -> bool:
    """Whether a driver argument holds ``;``, ``{`` or ``}``.

    pyodbc appends its keyword arguments to the ODBC string as they are, where
    ``;`` starts another setting, such as an ``Address`` that wins over the
    ``Server`` the policy judged.
    """
    return any(
        isinstance(value, str) and re.search(r"[;{}]", value) for value in arguments.values()
    )


def _field_targets(
    field_name: str, raw: Any, path: Tuple[Any, ...], behind: bool, *, quiet: bool = False
) -> List[_Target]:
    """The destinations of one field, read as ``host`` when ``field_name`` is, else as a URI.

    ``quiet`` marks them all, for a field that may hold a credential.
    """
    label = _label(path)
    out: List[_Target] = []
    if field_name != "host" and isinstance(raw, str):
        if _is_keyword_string(raw):
            return [_Target(None, label, keyword_text=raw, path=path, quiet=quiet)]
        if _hosts_read_differently(raw.strip()):
            # Clients disagree on where its authority ends, so its query too.
            return [_Target(None, label, path=path, quiet=quiet)]
        sockets, keys = _uri_query_hosts(raw)
        out.extend(_Target(None, label, query_key=key, path=path, quiet=quiet) for key in keys)
        out.extend(_Target(socket, label, local=True, path=path) for socket in sockets)
        if sockets and _authority_names_no_host(raw):
            # The client connects to the socket the query names, not to a host.
            return out
    out.extend(
        _Target(host, label, behind_tunnel=behind, path=path, quiet=quiet)
        for host in _field_hosts(field_name, raw)
    )
    return out


def _is_keyword_string(text: str) -> bool:
    """Whether ``text`` is a keyword connection string, not a URI or a host.

    ``Server=h;Database=d``, ``host=h dbname=d``: no ``://``, and an ``=`` before
    any ``/``, ``?`` or ``#``. This policy reads no host out of one, so it is a
    destination it cannot read, whose host part alone a refusal shows.
    """
    return "://" not in text and "=" in re.split(r"[/?#]", text, maxsplit=1)[0]


def _uri_query_hosts(text: str) -> Tuple[List[str], List[str]]:
    """The Unix-domain sockets, and the other host-naming parameters, of URI ``text``'s query.

    Everything after the first ``?`` counts, a fragment included: libpq has no
    fragment, and reads a ``#`` as part of the query. Text without a scheme is
    read as :func:`_field_hosts` reads it, as a host part and what follows. An
    object-store URI, and an http(s) or ws(s) URI (its client never takes the
    host from the query), have none.
    """
    stripped = text.strip()
    if "?" not in stripped or _is_object_store_uri(stripped):
        return [], []
    scheme, sep, _rest = stripped.partition("://")
    if sep and scheme.strip().lower() in _UNROUTED_QUERY_SCHEMES:
        return [], []
    return _query_hosts(_query_pairs(stripped.partition("?")[2]))


def _uri_query_host_keys(text: str) -> List[str]:
    """The parameters of URI ``text``'s query that name a host other than a socket here."""
    return _uri_query_hosts(text)[1]


def _authority_names_no_host(text: str) -> bool:
    """Whether URI ``text``'s authority holds no host (``scheme:///db``, ``scheme://u@:5432/db``)."""
    stripped = text.strip()
    _scheme, sep, rest = stripped.partition("://")
    if not sep or _hosts_read_differently(stripped):
        return False
    authority = re.split(r"[/?#]", rest, maxsplit=1)[0]
    return re.fullmatch(r"(?::\d*)?", authority.rpartition("@")[2]) is not None


def _query_targets(raw: Union[str, Mapping[Any, Any]], path: Tuple[Any, ...]) -> List[_Target]:
    """The destinations a query string, or a mapping read as one, names."""
    pairs = _query_pairs(raw) if isinstance(raw, str) else _mapping_pairs(raw)
    sockets, keys = _query_hosts(pairs)
    label = _label(path)
    mapping = not isinstance(raw, str)
    targets = [
        _Target(None, label, query_key=key, path=path, query_mapping=mapping) for key in keys
    ]
    targets.extend(_Target(socket, label, local=True, path=path) for socket in sockets)
    return targets


#: One parameter that names a host: its :func:`_host_key_name`, its value's
#: readings, and the key as a refusal names it.
_QueryPair = Tuple[str, List[Any], str]


def _query_pairs(query: str) -> List[_QueryPair]:
    """Each parameter of query string ``query`` that names a host, with its value's readings.

    Parameters are split on ``&`` and on ``;`` (SQL Server's JDBC properties); a
    ``#`` ends none, as no client the runners use reads a fragment there, and
    SQLAlchemy and libpq keep it in the value. A value is read percent-decoded, as libpq decodes it, and once
    more with ``+`` read as a space, as SQLAlchemy's ``parse_qsl`` reads it. A
    refusal names the key as :data:`_QUERY_HOST_KEYS` spells it: at apply the
    query string may have been rendered from a placeholder.
    """
    pairs: List[_QueryPair] = []
    for part in re.split(r"[&;]", query):
        key, _, value = part.partition("=")
        name = _host_key_name(key)
        if name is not None:
            pairs.append((name, [unquote(value), unquote_plus(value)], name))
    return pairs


def _mapping_pairs(mapping: Mapping[Any, Any]) -> List[_QueryPair]:
    """:func:`_query_pairs` for a mapping a connector reads as a query string's parameters.

    A refusal names the key as the contract wrote it, which is what to remove.
    """
    pairs: List[_QueryPair] = []
    for key, value in mapping.items():
        name = _host_key_name(str(key))
        if name is not None:
            pairs.append((name, [value], name if name == _SYNTAX_KEY else str(key)))
    return pairs


#: What a parameter's name may not hold, in either reading, blanks around it
#: aside: ``=``, ``;``, a brace, a quote, a blank or a control character. A
#: driver builds its connection string from the names as they are written
#: (psycopg's DSN builder and SQLAlchemy's pyodbc dialect quote the values only),
#: so such a name adds settings of its own, a host among them.
_SYNTAX_CHARS = re.compile(r"[=;{}'\"\s\x00-\x1f\x7f-\x9f]")
#: What :func:`_host_key_name` calls a parameter whose name holds
#: :data:`_SYNTAX_CHARS`. A refusal never quotes such a name.
_SYNTAX_KEY = "connection-string syntax"


def _host_key_name(key: str) -> Optional[str]:
    """``key`` as :data:`_QUERY_HOST_KEYS` spells it, when it names a host there.

    A key is compared percent-decoded, as libpq decodes both keys and values,
    and once more with ``+`` read as a space, as SQLAlchemy's ``parse_qsl``
    reads it; in any case; and with everything but letters and digits left
    out, so ``HOST``, ``%68ost`` and ``unix_socket`` are ``host``, ``host`` and
    ``unixsocket``. A key that holds :data:`_SYNTAX_CHARS` in either reading
    is :data:`_SYNTAX_KEY`.
    """
    spellings = (unquote(key).strip(), unquote_plus(key).strip())
    if any(_SYNTAX_CHARS.search(spelling) for spelling in spellings):
        return _SYNTAX_KEY
    for spelling in spellings:
        name = re.sub(r"[^a-z0-9]", "", spelling.lower())
        if name in _QUERY_HOST_KEYS:
            return name
    return None


def _query_hosts(pairs: List[_QueryPair]) -> Tuple[List[str], List[str]]:
    """``pairs`` as the sockets on this machine they name, and the other keys, each key once.

    A parameter names a socket only where it may (:data:`_SOCKET_QUERY_KEYS`)
    and when every reading of its value is one absolute path (:data:`_SOCKET_PATH`).
    """
    sockets: List[str] = []
    keys: List[str] = []
    names: Set[str] = set()
    for name, readings, shown in pairs:
        if name in _SOCKET_QUERY_KEYS and all(
            isinstance(reading, str) and _SOCKET_PATH.fullmatch(reading) for reading in readings
        ):
            sockets.append(str(readings[0]))
        elif name not in names:
            names.add(name)
            keys.append(shown)
    return sockets, keys


def _query_mappings(connection: Mapping[str, Any]) -> Iterator[Tuple[Tuple[Any, ...], Any]]:
    """Each value ``connection`` holds that a connector reads as a query string's parameters.

    The :data:`_QUERY_MAPPING_FIELDS`, whatever they hold (one that is not a
    mapping is a destination this policy cannot read), any other ``*_query``
    field that holds a mapping, and the :data:`_DRIVER_ARGUMENT_PATHS`.
    """
    for key, raw in connection.items():
        name = str(key)
        if name in _QUERY_MAPPING_FIELDS:
            if not _blank(raw):
                yield (key,), raw
        elif name.lower().endswith("_query") and isinstance(raw, Mapping):
            yield (key,), raw
    for parent, child in _DRIVER_ARGUMENT_PATHS:
        holder = connection.get(parent)
        if isinstance(holder, Mapping) and isinstance(holder.get(child), Mapping):
            yield (parent, child), holder[child]


def _destinations_elsewhere(
    connection: Mapping[str, Any],
) -> Iterator[Tuple[Tuple[Any, ...], str, str]]:
    """Each destination the block holds outside the fields judged by name, with where and how.

    A pass-through runner (Singer, Airbyte) hands the whole block to a connector
    whose settings this policy cannot list. So a URI under any setting is a
    destination (``api_url``, Airbyte's ``database_config.connection_string``),
    and so is a field named for a host that holds one (:data:`_NAMED_HOST_FIELDS`),
    at any depth and in any list, in order, at every path a shared mapping or list
    is reached by, and never twice on one path, so a cycle ends. Each is yielded
    with its reading, ``host`` or ``uri``; a URI under a field that may hold a
    credential (:data:`_CREDENTIAL_FIELD`) is ``secret``, judged as a URI but
    never quoted. A ``secretRef`` is never read, and past :data:`_WALK_LIMIT`
    values the rest is ``unreadable``.
    """
    pending: List[Tuple[Tuple[Any, ...], Any, Tuple[int, ...]]] = [
        ((key,), value, ()) for key, value in connection.items() if key not in _JUDGED_BY_NAME
    ]
    pending.reverse()
    visited = 0
    while pending:
        path, value, outer = pending.pop()
        visited += 1
        if visited > _WALK_LIMIT:
            yield path, "unreadable", ""
            return
        if path[-1] == "secretRef":
            continue
        if isinstance(value, str):
            reading = _reading_elsewhere(path, value)
            if reading is not None:
                yield path, reading, value
        elif isinstance(value, (Mapping, list, tuple)) and id(value) not in outer:
            inner = (*outer, id(value))
            items = value.items() if isinstance(value, Mapping) else enumerate(value)
            pending.extend(reversed([((*path, key), item, inner) for key, item in items]))


def _reading_elsewhere(path: Tuple[Any, ...], text: str) -> Optional[str]:
    """How string ``text`` at ``path`` is read as a destination, or ``None`` when it is not one.

    A URI of an object store or a local file names no host (:data:`_UNJUDGED_SCHEMES`),
    and nor does an embedded JDBC URL whose settings name none (:data:`_EMBEDDED_JDBC`).
    A field named for a host is one unless it plainly holds none (:data:`_NOT_A_HOST`).
    """
    name = _nearest_key(path)
    shape = _URI_SHAPE.match(text)
    if shape is not None:
        scheme = shape.group("scheme")
        if scheme is None:
            if _embedded_jdbc(text):
                return None
        elif re.split(r"[+:]", scheme.lower())[0] in _UNJUDGED_SCHEMES:
            return None
        return "secret" if _credential_field(name) else "uri"
    if not isinstance(name, str) or path[0] in _FIELD_MAP_SETTINGS:
        return None
    reading = _NAMED_HOST_FIELDS.get(name)
    return reading if reading is not None and _may_be_a_host(text) else None


def _embedded_jdbc(text: str) -> bool:
    """Whether JDBC URL ``text`` is an embedded database (:data:`_EMBEDDED_JDBC`) naming no host.

    Not when its location holds ``://`` (``jdbc:derby:net://`` is IBM's network
    driver), nor when its settings name a host.
    """
    location = re.split(r"[?;]", text, maxsplit=1)[0]
    return (
        bool(_EMBEDDED_JDBC.match(text))
        and "://" not in location
        and not (_settings_name_a_host(text))
    )


def _settings_name_a_host(text: str) -> bool:
    """Whether the settings of JDBC URL ``text``, after its first ``?`` or ``;``, name a host."""
    parts = re.split(r"[?;]", text, maxsplit=1)
    return len(parts) == 2 and bool(_query_pairs(parts[1]))


def _may_be_a_host(text: str) -> bool:
    """Whether a field named for a host holds what may be one: anything but :data:`_NOT_A_HOST`."""
    value = text.strip()
    return bool(value) and _NOT_A_HOST.fullmatch(value) is None


def _credential_field(key: Any) -> bool:
    """Whether field ``key`` may hold a credential, so no refusal quotes what it holds."""
    return _CREDENTIAL_FIELD.search(re.sub(r"[^a-z0-9]", "", str(key).lower())) is not None


def _database_targets(connection: Mapping[str, Any]) -> List[_Target]:
    """A destination this policy cannot read for each database name holding URL syntax."""
    out: List[_Target] = []
    for field_name in _DATABASE_FIELDS:
        raw = connection.get(field_name)
        if not isinstance(raw, str):
            continue
        char = next((c for c in raw if c in _URL_SYNTAX or _CONTROL_CHARS.match(c)), None)
        if char is not None:
            out.append(_Target(None, field_name, database_char=char))
    return out


def _tunnel_targets(connection: Mapping[str, Any]) -> List[_Target]:
    """The host of each SSH tunnel ``connection`` connects through (:data:`_TUNNELS`).

    A tunnel is in use unless it is switched off as its connector spells it, or
    it is empty (``{}``, ``null``, ``false``). One in use whose host is not a
    non-empty string is a destination this policy cannot read.
    """
    out: List[_Target] = []
    for field_name, host_key, tunnel in _tunnels_in_use(connection):
        raw = tunnel.get(host_key) if isinstance(tunnel, Mapping) else None
        host = raw.strip() if isinstance(raw, str) and raw.strip() else None
        out.append(
            _Target(host, f"{field_name}.{host_key}", tunnel=True, path=(field_name, host_key))
        )
    return out


def _tunnels_in_use(connection: Mapping[str, Any]) -> Iterator[Tuple[str, str, Any]]:
    """Each SSH tunnel ``connection`` connects through, as ``(field, host key, tunnel)``."""
    for field_name, host_key, (off_key, off_value) in _TUNNELS:
        tunnel = connection.get(field_name)
        if not tunnel:
            continue
        if isinstance(tunnel, Mapping) and _switched_off(tunnel, off_key, off_value):
            continue
        yield field_name, host_key, tunnel


def _switched_off(tunnel: Mapping[str, Any], off_key: str, off_value: Any) -> bool:
    """``enable`` absent or false (the taps read ``.get("enable", False)``), or ``NO_TUNNEL``.

    ``tunnel_method`` is compared in any case and without blanks: Airbyte reads
    ``NO_TUNNEL`` as no tunnel and refuses any value it does not know, so no
    other spelling connects anywhere.
    """
    if off_value is False:
        return not tunnel.get(off_key)
    value = tunnel.get(off_key)
    return isinstance(value, str) and value.strip().upper() == off_value


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
    if _hosts_read_differently(text):
        return [None]
    try:
        parsed = urlsplit(text if "//" in text else f"//{text}")
    except ValueError:
        return [None]
    return _authority_hosts(parsed.netloc)


#: SQLAlchemy's ``make_url`` reads a user part as a user name of ``[^:/]*`` and
#: an optional password of ``[^@]*`` up to the first ``@`` (``_parse_url``).
_SQLALCHEMY_USER_PART = re.compile(r"[^:/]*(?::[^@]*)?@")


def _hosts_read_differently(text: str) -> bool:
    """Whether a client could connect to a host in URI ``text`` that this policy does not read.

    The policy reads a URI's hosts as ``urlsplit`` does: the authority ends at
    the first ``/``, ``?`` or ``#``, and the host list follows its last ``@``.
    The clients a runner hands a URI to read some URIs otherwise:

    * libpq (``conninfo_uri_parse_options``) ends the user part at its first
      ``@`` and reads ``#`` as part of a host;
    * the MongoDB drivers take everything before the first ``/`` as the user
      part and the host list, ``?`` and ``#`` included, split at the last ``@``,
      and pymongo percent-decodes the host list before splitting it at ``,``;
    * a connector given a value without a scheme may read it whole as a host
      name, ``?`` included;
    * SQLAlchemy's ``make_url`` reads a password through ``/``, ``?`` and ``#``
      to the first ``@`` after it, and a host through ``#`` and ``,``;
    * a WHATWG URL reader (urllib3, requests) ends an http(s) authority at ``\\``.

    Where any of them could read a host the policy does not, the URI is a
    destination this policy cannot read.
    """
    scheme, sep, rest = text.partition("://")
    if not sep:
        scheme, rest = "", text.lstrip("/")
    before_path = rest.split("/", 1)[0]
    if scheme.strip().lower() in _UNROUTED_QUERY_SCHEMES:
        return "\\" in before_path
    if before_path.count("@") > 1:
        return True
    user_part, at, hosts = before_path.rpartition("@")
    if at and ("?" in user_part or "#" in user_part):
        return True
    if "#" in re.split(r"[/?]", rest, maxsplit=1)[0] or "," in before_path.partition("?")[2]:
        return True
    mongodb = scheme.strip().lower() in ("mongodb", "mongodb+srv")
    if (mongodb or not sep) and "?" in before_path:
        return True
    if mongodb and "%" in hosts:
        return True
    match = _SQLALCHEMY_USER_PART.match(rest)
    return match is not None and any(char in match.group(0) for char in "/?#")


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
        if not _moves_destination(str(key)):
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


def _moves_destination(key: str) -> bool:
    """Whether connector setting ``key`` names where the connector connects, or with what.

    :data:`DESTINATION_OVERRIDE_KEYS` in any case, and each ``database.*`` or
    ``driver.*`` setting that Debezium hands the JDBC driver and that does
    (:data:`_PASS_THROUGH_DESTINATIONS`, or a property that names a host).
    """
    name = key.strip().lower()
    if name in DESTINATION_OVERRIDE_KEYS:
        return True
    for prefix in _PASS_THROUGH_PREFIXES:
        if not name.startswith(prefix):
            continue
        setting = name[len(prefix) :]
        if setting in _PASS_THROUGH_DESTINATIONS:
            return True
        if "." not in setting and _host_key_name(setting) is not None:
            return True
    return False


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
) -> Optional[List[_Target]]:
    """Every destination to judge, or ``None`` when the connection has none to judge.

    ``None`` only for a connection whose one destination is an object store (the
    known gap :func:`refuse_unless_allowed` names). Fields that are present but
    empty are not that: they leave the client to pick its host, so the result is
    a destination this policy cannot clear, as for a connection naming none.
    """
    targets: List[_Target] = []
    if connection is not None:
        targets.extend(_connection_targets(connection))
        if not targets and _names_an_object_store(connection):
            return None
    if host is not None or not targets:
        targets.append(_Target(host))
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
            f"{_any_host_fix(parsed)}."
        ),
        doc=doc_url("secrets"),
    )


def _field_label(target: _Target) -> str:
    """``connection.<field>`` for a destination a connection names, else nothing."""
    return f"connection.{target.field}" if target.field else ""


def _any_host_fix(parsed: SecretRef) -> str:
    """The allowlist entries that send ``parsed`` to any host, for a refusal's fix.

    For ``env://NAME`` that is a bare ``NAME`` or ``NAME@*``; for any other
    reference, ``<reference>@*``.
    """
    if parsed.scheme == "env":
        return f'{ALLOWLIST_ENV}="{parsed.locator}" or {ALLOWLIST_ENV}="{parsed.locator}@*"'
    return f'{ALLOWLIST_ENV}="{_suggested_entry(parsed, None)}"'


#: The keys a keyword connection string names its host with: libpq's
#: The keys whose value a keyword connection string connects to, as
#: :func:`_keyword_pairs` spells them: libpq's, ADO.NET's and ODBC's
#: (``Data Source=tcp:host,1433``).
_KEYWORD_HOST_KEYS = frozenset(
    {"host", "hostaddr", "server", "address", "addr", "network address", "data source"}
)
#: A key whose value may be a secret. A keyword string's host is looked for only
#: before the first one, since what follows it may be read as part of its value.
_SECRET_KEY = re.compile(r"pass|pwd|secret|key|token|signature|credential")
#: One key and its ``=``: words separated by blanks, so a long run of blanks
#: costs linear time.
_KEYWORD_KEY = re.compile(r"\s*([A-Za-z][A-Za-z0-9_]*(?:[ \t]+[A-Za-z0-9_]+)*)\s*=\s*")
#: What a refusal may quote as a host: a name, an address, a port, a placeholder.
_QUOTABLE_HOST = re.compile(r"(?:\{\{\s*env\.[A-Za-z_][A-Za-z0-9_]*\s*\}\}|[A-Za-z0-9._%:\[\]\-])+")


def _keyword_host(text: str) -> Optional[str]:
    """The host part of keyword connection string ``text``, or ``None`` when it shows none.

    Never anything else of the string. The host is looked for only where the
    string's own grammar reads a key (:func:`_keyword_pairs`), and only before
    the first key that may hold a secret (:data:`_SECRET_KEY`), so a password
    that holds ``host=...`` stays out of every message.
    """
    for key, value in _keyword_pairs(text):
        if key in _KEYWORD_HOST_KEYS:
            host = _unquoted(value.strip())
            if host[:4].lower() == "tcp:":
                host = host[4:]
            host = host.split(",")[0]
            return host if _QUOTABLE_HOST.fullmatch(host) else None
        if _SECRET_KEY.search(key.replace(" ", "")):
            return None
    return None


def _keyword_pairs(text: str) -> Iterator[Tuple[str, str]]:
    """Each ``key=value`` of keyword string ``text`` in order, its key in lower case.

    With a ``;`` it reads as an ODBC or ADO.NET string, whose values end at a
    ``;`` outside ``{...}``, ``"..."`` or ``'...'`` (a doubled closing character
    stands for itself). Without one it reads as a libpq string, whose values end
    at a blank outside ``'...'``, a ``\\`` escaping the next character
    (``conninfo_parse``). A ``{{ env.NAME }}`` placeholder is part of a value.
    Reading stops where no key follows.
    """
    odbc = ";" in text
    index = 0
    while True:
        while index < len(text) and (text[index] == ";" or text[index].isspace()):
            index += 1
        match = _KEYWORD_KEY.match(text, index)
        if match is None:
            return
        start = match.end()
        index = _odbc_value_end(text, start) if odbc else _libpq_value_end(text, start)
        yield " ".join(match.group(1).lower().split()), text[start:index]


def _libpq_value_end(text: str, index: int) -> int:
    """Where the libpq value that starts at ``index`` ends."""
    if text.startswith("'", index):
        index += 1
        while index < len(text) and text[index] != "'":
            index += 2 if text[index] == "\\" else 1
        return min(index + 1, len(text))
    while index < len(text) and not text[index].isspace():
        placeholder = _ENV_PLACEHOLDER.match(text, index)
        if placeholder:
            index = placeholder.end()
        else:
            index += 2 if text[index] == "\\" else 1
    return min(index, len(text))


def _odbc_value_end(text: str, index: int) -> int:
    """Where the ODBC or ADO.NET value that starts at ``index`` ends."""
    while index < len(text) and text[index] != ";":
        placeholder = _ENV_PLACEHOLDER.match(text, index)
        if placeholder:
            index = placeholder.end()
        elif text[index] in "{\"'":
            index = _quoted_end(text, index)
        else:
            index += 1
    return min(index, len(text))


def _quoted_end(text: str, index: int) -> int:
    """Past the ``{...}``, ``"..."`` or ``'...'`` that opens at ``index``.

    A doubled closing character inside it stands for itself.
    """
    close = "}" if text[index] == "{" else text[index]
    index += 1
    while index < len(text):
        if text[index] != close:
            index += 1
        elif text[index + 1 : index + 2] == close:
            index += 2
        else:
            return index + 1
    return index


def _unquoted(value: str) -> str:
    """``value`` without one pair of quotes or braces around it; a placeholder stays whole."""
    pairs = (("'", "'"), ('"', '"'), ("{", "}"))
    if len(value) >= 2 and (value[0], value[-1]) in pairs and not _ENV_PLACEHOLDER.fullmatch(value):
        return value[1:-1]
    return value


#: What the running contract wrote, before ``fluid apply`` rendered its
#: placeholders, while :func:`destinations_as_written` is active; else ``None``.
_AS_WRITTEN: ContextVar[Optional[Tuple[Any, ...]]] = ContextVar(
    "fluid_secret_destinations_as_written", default=None
)


@contextmanager
def destinations_as_written(written: Any) -> Iterator[None]:
    """Within the block, a refusal at apply quotes only a host ``written`` holds as text.

    ``fluid apply`` renders a contract's ``{{ env.NAME }}`` placeholders before a
    runner asks for a secret (``build_runners/base.py``), so a host judged there
    may be the value of any variable the contract named. The acquisition
    dispatcher passes the contract as written: a refusal then quotes a host the
    contract wrote as it stands, in the same field of the connection block that
    renders to the judged one, names the placeholder of one it rendered, and
    quotes nothing it cannot trace (:func:`_shown_host`).
    """
    token = _AS_WRITTEN.set((written,))
    try:
        yield
    finally:
        _AS_WRITTEN.reset(token)


def _mappings_in(value: Any) -> Iterator[Mapping[str, Any]]:
    """Every mapping in ``value``, through its mappings and lists, each once."""
    pending = [value]
    seen: Set[int] = set()
    while pending:
        item = pending.pop()
        if not isinstance(item, (Mapping, list, tuple)) or id(item) in seen:
            continue
        seen.add(id(item))
        if isinstance(item, Mapping):
            yield item
            pending.extend(item.values())
        else:
            pending.extend(item)


def _written_blocks(connection: Optional[Mapping[str, Any]]) -> List[Mapping[str, Any]]:
    """The connection blocks the contract wrote that render to ``connection``.

    ``connection`` is a block as a runner hands it to this policy: rendered,
    its ``secretRef`` taken out. Only such a block shows how the contract wrote
    one of its fields; a string elsewhere that equals a rendered host says
    nothing about that host, and quoting it as written would confirm a guess.
    """
    written = _AS_WRITTEN.get()
    if written is None or connection is None:
        return []
    wanted = {key: value for key, value in connection.items() if key != "secretRef"}
    blocks: List[Mapping[str, Any]] = []
    for block in _mappings_in(written):
        own = {key: value for key, value in block.items() if key != "secretRef"}
        if own.keys() == wanted.keys() and resolve_env_placeholders(own) == wanted:
            blocks.append(block)
    return blocks


def _value_at(value: Any, path: Tuple[Any, ...]) -> Any:
    """What ``value`` holds at ``path``, of keys and list indexes; ``None`` for nothing."""
    for part in path:
        if isinstance(value, Mapping):
            value = value.get(part)
        elif isinstance(value, list) and isinstance(part, int) and 0 <= part < len(value):
            value = value[part]
        else:
            return None
    return value


def _host_readings(text: str) -> List[str]:
    """Every host ``text`` names, as a host field or a URI field reads it, compared case-blind."""
    hosts = [text.strip().lower().rstrip(".")]
    hosts.extend(host.rstrip(".") for host in _field_hosts("uri", text) if host)
    return hosts


def _render(text: str) -> str:
    """``text`` with its placeholders rendered, as ``build_runners/base.py`` renders them."""
    return _ENV_PLACEHOLDER.sub(lambda match: os.environ.get(match.group(1), ""), text)


def _shown_host(
    target: _Target, rendered: bool, connection: Optional[Mapping[str, Any]] = None
) -> Tuple[Optional[str], bool]:
    """What a refusal may quote of ``target``, and whether that is placeholder text.

    That is its host, or a keyword string whole (of which the refusal then shows
    the host part alone). Outside ``fluid apply`` it is the contract's own text,
    and is quoted. At apply, where placeholders are rendered, it is read from
    the same field of each block the contract wrote that renders to
    ``connection`` (:func:`_written_blocks`): quoted when every such block wrote
    it as it stands; for one a placeholder rendered, the contract's text is
    quoted with the placeholder; one traced to neither is not quoted at all.
    """
    quoted = target.keyword_text if target.keyword_text is not None else target.host
    if quoted is None or not rendered:
        return quoted, False
    texts = [_value_at(block, target.path) for block in _written_blocks(connection)]
    if texts and all(isinstance(text, str) and _names_as_written(target, text) for text in texts):
        return quoted, False
    return _placeholder_shown(target, texts)


def _placeholder_shown(target: _Target, texts: List[Any]) -> Tuple[Optional[str], bool]:
    """The contract's text that a placeholder rendered into ``target``, of ``texts``."""
    for text in texts:
        if (
            isinstance(text, str)
            and _ENV_PLACEHOLDER.search(text)
            and _names(target, _render(text))
        ):
            if target.keyword_text is not None:
                return text, True
            return _shown_destination(f"connection.{target.field or 'host'}", text), True
    return None, False


def _names(target: _Target, text: str) -> bool:
    """Whether ``text`` names ``target``: its keyword string whole, or its host."""
    if target.keyword_text is not None:
        return text.strip() == target.keyword_text.strip()
    return (target.host or "").strip().lower().rstrip(".") in _host_readings(text)


def _names_as_written(target: _Target, text: str) -> bool:
    """Whether the contract's ``text`` names ``target`` with no placeholder deciding it.

    A placeholder in a URI's user name, password or path leaves its host as
    written: only one in the scheme or the host part decides the host
    (:func:`_deciding_parts`), and a rendered password that would move it
    leaves a host this policy cannot read, never quoted.
    """
    if not _ENV_PLACEHOLDER.search(text):
        return _names(target, text)
    if target.keyword_text is not None:
        return False
    scheme, host = _deciding_parts("uri", text)
    if _ENV_PLACEHOLDER.search(scheme) or _ENV_PLACEHOLDER.search(host):
        return False
    wanted = (target.host or "").strip().lower().rstrip(".")
    return wanted in [h.rstrip(".") for h in _field_hosts("uri", text) if h]


@dataclass(frozen=True)
class _Shown:
    """How a destination refusal names a host: the ``text`` it may quote, and its ``kind``.

    ``literal`` is a host the contract wrote, ``placeholder`` the contract's text
    for one a placeholder rendered, ``keyword`` the host part of a keyword
    string (``text`` is ``None`` when it names none), ``untraced`` a host it may
    not quote, and ``unknown`` a destination nobody can read.
    """

    text: Optional[str]
    kind: str


def _shown(
    target: _Target, rendered: bool, connection: Optional[Mapping[str, Any]] = None
) -> _Shown:
    if target.quiet:
        return _Shown(None, "unknown" if target.host is None else "untraced")
    shown, placeholder = _shown_host(target, rendered, connection)
    if target.keyword_text is not None or (target.host is not None and "=" in target.host):
        return _Shown(_keyword_host(shown) if shown is not None else None, "keyword")
    if target.host is None:
        return _Shown(None, "unknown")
    if shown is None or (not placeholder and not _QUOTABLE_HOST.fullmatch(shown)):
        # A host field written as ``user:password@host`` is no host name to quote.
        return _Shown(None, "untraced")
    return _Shown(shown, "placeholder" if placeholder else "literal")


def _refusal_place(shown: _Shown, label: str) -> str:
    """Where a destination refusal says the value would go."""
    if shown.kind == "untraced":
        return f"the host in {label}" if label else "the host it is sent to"
    if shown.kind == "keyword":
        where = f"a keyword string naming host {shown.text!r}" if shown.text else "a keyword string"
    elif shown.kind == "placeholder":
        where = f"the host {shown.text!r} renders to"
    elif shown.kind == "literal":
        where = f"host {shown.text!r}"
    else:
        where = "a host that could not be determined"
    return f"{where} ({label})" if label else where


def _refusal_why(target: _Target) -> str:
    why = (
        "A contract names both the secret to read and the host it is sent to, so "
        "without an operator allowlist any secret this run can read could be sent "
        "to a host the contract chose. A loopback host needs no entry; every "
        "destination that leaves this machine does."
    )
    if target.tunnel:
        return why + " The value travels through this SSH tunnel host on its way to the database."
    if target.behind_tunnel and is_loopback(target.host):
        return why + (
            " Behind an SSH tunnel the client reads this host from the tunnel host's side, "
            "so a loopback host there is that machine, not this one."
        )
    return why


def _refusal_fix(parsed: SecretRef, shown: _Shown, label: str) -> str:
    if shown.kind == "keyword":
        return (
            f"Write {label or 'the destination'} as a URI (scheme://host/...), whose host this "
            "check reads and the operator can allow, or, to send the value to any host, set it "
            f"in {_OPERATOR_ONLY}: {_any_host_fix(parsed)}."
        )
    if shown.kind in ("placeholder", "untraced"):
        return (
            f"If this is intended, set it in {_OPERATOR_ONLY}: "
            f'{ALLOWLIST_ENV}="{_suggested_entry(parsed, "<host>")}" for that host only, with '
            "<host> the host it renders to where fluid apply runs and without a port, or "
            f"{_any_host_fix(parsed)} for any host."
        )
    return (
        f"If this is intended, set it in {_OPERATOR_ONLY}: "
        f'{ALLOWLIST_ENV}="{_suggested_entry(parsed, shown.text)}" for that host only, or '
        f"{_any_host_fix(parsed)} for any host. A host written with a port "
        "never matches, so leave the port off."
    )


def _destination_refusal(
    parsed: SecretRef,
    target: _Target,
    *,
    rendered: bool = False,
    connection: Optional[Mapping[str, Any]] = None,
) -> AcquisitionSecretPolicyError:
    label = _field_label(target)
    shown = _shown(target, rendered, connection)
    return AcquisitionSecretPolicyError(
        what=(
            f"the contract would send {_what_sent(parsed)} to {_refusal_place(shown, label)} "
            "as a credential"
        ),
        why=_refusal_why(target),
        fix=_refusal_fix(parsed, shown, label),
        doc=doc_url("secrets"),
    )


def _query_host_refusal(parsed: SecretRef, target: _Target) -> AcquisitionSecretPolicyError:
    """The refusal for a query-string parameter that names a host (:data:`_QUERY_HOST_KEYS`)."""
    if target.query_key == _SYNTAX_KEY:
        return _syntax_key_refusal(parsed, target)
    label = _field_label(target)
    why = (
        "A driver can take its host from a query string as well as from the URI's host "
        "part, and drivers differ on which one wins. This check judges the host part, so "
        "a query parameter that names a host could send the value to a host the operator "
        "never allowed."
    )
    if target.field in _QUERY_STRING_FIELDS:
        where = f"{label}, the query string of the JDBC URL"
        fix = f"Name the host in connection.host and remove {target.query_key!r} from {label}."
    elif target.query_mapping:
        where = label
        fix = f"Name the host in connection.host and remove {target.query_key!r} from {label}."
        why = (
            "The connector merges this mapping into its connection URL's query string or "
            "into its driver's arguments, where a host can win over connection.host. This "
            "check judges connection.host, so a key that names a host could send the value "
            "to a host the operator never allowed."
        )
    else:
        where = f"the query string of {label}"
        fix = (
            f"Name the host in the host part of {label} (scheme://host/...), and remove "
            f"{target.query_key!r} from its query string."
        )
    return AcquisitionSecretPolicyError(
        what=(
            f"the contract would send {_what_sent(parsed)} to a host that {target.query_key!r} "
            f"names in {where}, which this check does not read"
        ),
        why=why,
        fix=(
            f"{fix} To send the value to any host instead, set it in {_OPERATOR_ONLY}: "
            f"{_any_host_fix(parsed)}."
        ),
        doc=doc_url("secrets"),
    )


def _syntax_key_refusal(parsed: SecretRef, target: _Target) -> AcquisitionSecretPolicyError:
    """The refusal for a parameter whose name holds connection-string syntax (:data:`_SYNTAX_KEY`)."""
    label = _field_label(target)
    return AcquisitionSecretPolicyError(
        what=(
            f"the contract would send {_what_sent(parsed)} over a connection whose {label} "
            "holds a parameter name with connection-string syntax, which this check does not read"
        ),
        why=(
            "A driver builds its connection string from these parameters with each name as "
            "written, so a name that holds '=', a blank, ';' or a quote adds settings of its "
            "own, and a host among them could send the value to a host the operator never "
            "allowed."
        ),
        fix=(
            f"Write each parameter name in {label} without '=', ';', a brace, a quote or a blank. "
            f"To send the value regardless, set it in {_OPERATOR_ONLY}: {_any_host_fix(parsed)}."
        ),
        doc=doc_url("secrets"),
    )


def _database_refusal(
    parsed: SecretRef, target: _Target, *, rendered: bool = False
) -> AcquisitionSecretPolicyError:
    """The refusal for a database name that holds URL syntax (:data:`_URL_SYNTAX`).

    At apply the name may have been rendered from a placeholder, so the refusal
    names no character of it then.
    """
    label = _field_label(target)
    char = target.database_char or ""
    if rendered:
        shown = "a character"
    else:
        shown = "a control character" if _CONTROL_CHARS.match(char) else repr(char)
    return AcquisitionSecretPolicyError(
        what=(
            f"the contract would send {_what_sent(parsed)} over a connection whose {label} "
            f"holds {shown}, which a connection URL reads as more than a database name"
        ),
        why=(
            "Runners and connectors put the database name into a connection URL (a "
            "SQLAlchemy or a JDBC URL), where '?' starts connection settings that can name "
            "another host and ';', '&', '#', '@' and '\\' end other parts of it, so this "
            "check cannot tell where the value would go."
        ),
        fix=(
            f"Write the database's own name in {label}. To send the value regardless, set it "
            f"in {_OPERATOR_ONLY}: {_any_host_fix(parsed)}."
        ),
        doc=doc_url("secrets"),
    )


def refuse_unless_allowed(
    ref: Union[SecretRef, str],
    *,
    connection: Optional[Mapping[str, Any]] = None,
    host: Optional[str] = None,
    environ: Optional[Mapping[str, str]] = None,
    rendered: bool = False,
) -> None:
    """Raise unless ``ref`` may be sent to every destination given.

    Called before the reference is resolved, so a refusal never reads the
    secret. Every scheme is policed. ``rendered`` says the destinations went
    through ``fluid apply``'s placeholder rendering (``resolve_secret`` passes
    it): a refusal then quotes only a host the contract wrote
    (:func:`destinations_as_written`), never a value a placeholder rendered.

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
    # A reason first: it is what to change whatever the hosts are.
    for target in sorted(targets, key=lambda target: not target.has_reason):
        exempt = target.local or (not target.behind_tunnel and is_loopback(target.host))
        if exempt or _grants_allow(parsed, target.host, grants):
            continue
        if target.query_key is not None:
            raise _query_host_refusal(parsed, target)
        if target.database_char is not None:
            raise _database_refusal(parsed, target, rendered=rendered)
        if target.host is None and connection is not None and _host_is_blank(connection):
            raise _blank_host_refusal(parsed)
        raise _destination_refusal(parsed, target, rendered=rendered, connection=connection)


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

    The field waits whole: a literal host beside a placeholder in one host list
    (``h1,{{ env.H }}``) is judged by apply alone, which judges every host. An
    object-store URI names a bucket, never a host, so it never waits, and nor
    does text that does not read as a destination (:func:`_reads_as_destination`)
    or a URI whose query string names a host other than a socket on this
    machine, which is refused whatever the placeholder renders to.
    """
    if not isinstance(value, str) or not _ENV_PLACEHOLDER.search(value):
        return False
    if field_name != "host" and _is_object_store_uri(value.strip()):
        return False
    if field_name != "host" and (_uri_query_host_keys(value) or _hosts_read_differently(value)):
        return False
    scheme, host = _deciding_parts(field_name, value)
    if not _reads_as_destination(scheme, host):
        return False
    return any(_ENV_PLACEHOLDER.search(part) for part in (scheme, host))


#: A URI scheme (RFC 3986 section 3.1), once each placeholder in it stands for a word.
_SCHEME_TEXT = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*")
#: A host list once its placeholders are taken out: names, IPv6 literals, ports, commas.
_HOST_LIST_TEXT = re.compile(r"[A-Za-z0-9._%\-\[\]:,]*")


def _reads_as_destination(scheme: str, host: str) -> bool:
    """Whether ``scheme`` and ``host`` read as a URI scheme and a host list, placeholders aside.

    A keyword string (``Server={{ env.H }};Database=d``) does not: apply reads
    no host out of it (:func:`_is_keyword_string`), so the lint judges it as
    written too, and never quotes it in a notice.
    """
    if scheme and not _SCHEME_TEXT.fullmatch(_ENV_PLACEHOLDER.sub("x", scheme)):
        return False
    return _HOST_LIST_TEXT.fullmatch(_ENV_PLACEHOLDER.sub("", host)) is not None


def _deciding_parts(field_name: str, value: str) -> Tuple[str, str]:
    """The scheme and the host part of a written destination, the parts that decide the host.

    ``host`` is all host part. A URI's user name, password, path, query and
    fragment never decide where it goes, so they are left out. A URI without a
    scheme may start with ``//``, as :func:`_field_hosts` reads it.
    """
    scheme, authority, _rest = _authority_split(field_name, value)
    return scheme, authority.rpartition("@")[2]


def _authority_split(field_name: str, value: str) -> Tuple[str, str, str]:
    """``value`` as its scheme, its authority and what follows; ``host`` is all authority."""
    text = value.strip()
    if field_name == "host":
        return "", text, ""
    scheme, sep, rest = text.partition("://")
    if not sep:
        scheme, rest = "", text[2:] if text.startswith("//") else text
    authority, *after = re.split(r"(?=[/?#])", rest, maxsplit=1)
    return scheme, authority, "".join(after)


#: The destination fields that hold a bare host, not a URI, by the names
#: :func:`destinations_rendered_at_apply` gives them.
_BARE_HOST_FIELDS = frozenset({"host", *(f"{field}.{key}" for field, key, _off in _TUNNELS)})


def _reading(name: str, value: str) -> str:
    """How destination field ``name``, holding ``value``, is read: ``host`` or ``uri``.

    A URI is read as one wherever it is. Otherwise ``host``, a tunnel's host, and
    a field under another setting named for a bare host hold a bare host.
    """
    if _URI_SHAPE.match(value):
        return "uri"
    bare = name[len("connection.") :] if name.startswith("connection.") else name
    if bare in _BARE_HOST_FIELDS:
        return "host"
    last = re.sub(r"(?:\[\d+\])+$", "", bare).rpartition(".")[2]
    return "host" if _NAMED_HOST_FIELDS.get(last) == "host" else "uri"


def _shown_destination(name: str, value: str) -> str:
    """What the deferred notice quotes of ``value``: its :func:`_deciding_parts`, without a port.

    The allowlist entry takes no port. A password written inline with an
    unencoded ``/``, ``?`` or ``#`` ends the authority inside itself, and an
    ``@`` then follows the authority: only the placeholders are quoted then.
    """
    field_name = _reading(name, value)
    scheme, authority, rest = _authority_split(field_name, value)
    host = authority.rpartition("@")[2]
    if "@" in rest and "@" not in authority:
        host = " ".join(match.group(0) for match in _ENV_PLACEHOLDER.finditer(host))
    elif field_name != "host":
        host = ",".join(_without_port(piece) for piece in host.split(","))
    return f"{scheme}://{host}" if scheme else host


def _without_port(host: str) -> str:
    if host.startswith("[") and "]" in host:
        return host.partition("]")[0] + "]"
    return host.partition(":")[0]


def _holds_a_credential(name: str) -> bool:
    """Whether destination field ``name`` (``connection.tokens[0]``) may hold a credential."""
    return _credential_field(re.sub(r"(?:\[\d+\])+$", "", name).rpartition(".")[2])


def allows_any_host(
    ref: Union[SecretRef, str], environ: Optional[Mapping[str, str]] = None
) -> bool:
    """Whether the operator's allowlist lets ``ref`` go to any host.

    ``<ref>@*``, or a bare variable name for an ``env://`` reference. A
    destination the lint leaves to apply then needs no entry, whatever it
    renders to, an empty host included.
    """
    parsed = parse_secret_ref(ref) if isinstance(ref, str) else ref
    return _grants_allow(parsed, None, destination_grants(environ))


def destinations_rendered_at_apply(connection: Mapping[str, Any]) -> List[str]:
    """The destination fields of ``connection`` whose host :func:`renders_at_apply`.

    A tunnel's host is named ``<tunnel>.<key>`` (``ssh_tunnel.host``) and read as
    ``host`` is, and a destination elsewhere in the block (:func:`_destinations_elsewhere`) by
    where it is (``database_config.connection_string``, ``replicas[0]``).
    """
    waiting = [
        field_name
        for field_name in _HOST_FIELDS
        if renders_at_apply(field_name, connection.get(field_name))
    ]
    for field_name, host_key, tunnel in _tunnels_in_use(connection):
        if isinstance(tunnel, Mapping) and renders_at_apply("host", tunnel.get(host_key)):
            waiting.append(f"{field_name}.{host_key}")
    waiting.extend(
        _label(path)
        for path, reading, text in _destinations_elsewhere(connection)
        if renders_at_apply("uri" if reading == "secret" else reading, text)
    )
    return waiting


def literal_destinations(connection: Mapping[str, Any], waiting: List[str]) -> Dict[str, Any]:
    """``connection`` without the destinations ``waiting`` names, for the lint to judge now.

    A tunnel whose host waits for apply (``ssh_tunnel.host``) is still in use:
    its host is replaced by a loopback stand-in, which needs no entry, so the
    hosts behind the tunnel are judged now, from its side, as apply judges them.
    A URI elsewhere in the block that waits is taken out where it is.
    """
    literal: Dict[str, Any] = {
        key: value for key, value in connection.items() if key not in waiting
    }
    tunnels = {
        f"{field_name}.{host_key}": (field_name, host_key)
        for field_name, host_key, tunnel in _tunnels_in_use(connection)
        if isinstance(tunnel, Mapping)
    }
    nested = {_label(path): path for path, _kind, _text in _destinations_elsewhere(connection)}
    for name in waiting:
        if name in tunnels:
            field_name, host_key = tunnels[name]
            literal[field_name] = {**connection[field_name], host_key: "localhost"}
        elif name in nested:
            literal = _without_path(literal, nested[name])
    return literal


def _without_path(value: Any, path: Tuple[Any, ...]) -> Any:
    """A copy of ``value`` without what it holds at ``path``; a list keeps its length."""
    head, rest = path[0], path[1:]
    if isinstance(value, Mapping) and head in value:
        copy = dict(value)
        if rest:
            copy[head] = _without_path(value[head], rest)
        else:
            del copy[head]
        return copy
    if isinstance(value, list) and isinstance(head, int) and 0 <= head < len(value):
        items = list(value)
        items[head] = _without_path(value[head], rest) if rest else None
        return items
    return value


def written_destination(connection: Mapping[str, Any], name: str) -> Any:
    """The value of destination field ``name`` (:func:`destinations_rendered_at_apply`)."""
    for path, _kind, text in _destinations_elsewhere(connection):
        if _label(path) == name:
            return text
    head, _, rest = name.partition(".")
    value = connection.get(head)
    if rest and isinstance(value, Mapping):
        return value.get(rest)
    return value


def deferred_destination_notice(
    ref: Union[SecretRef, str], written: Mapping[str, str]
) -> SecretPolicyError:
    """What ``fluid validate`` says about a destination it leaves to ``fluid apply``.

    ``written`` maps each such field, as the user knows it
    (``connection.host``), to its value as written. Only the contract's own
    text is quoted: the lint never reads the environment, so it never prints a
    value a placeholder names. Of a URI it quotes the scheme and the host part
    alone, without a port: not the path, nor a password written inline, even
    one that holds an unencoded ``/``, ``?`` or ``#``.
    """
    parsed = parse_secret_ref(ref) if isinstance(ref, str) else ref
    fields = ", ".join(
        name if _holds_a_credential(name) else f"{name} ({_shown_destination(name, value)!r})"
        for name, value in written.items()
    )
    variables = sorted(
        {
            match.group(1)
            for name, value in written.items()
            for part in _deciding_parts(_reading(name, value), value)
            for match in _ENV_PLACEHOLDER.finditer(part)
        }
    )
    return SecretPolicyError(
        what=(
            f"{parsed.canonical} is sent to whatever host {fields} renders to, which only "
            "fluid apply knows"
        ),
        why=(
            "fluid apply renders {{ env.NAME }} placeholders from the environment it runs "
            "in, then judges the host they name against the destination allowlist. This "
            "check does not read the environment, so it leaves that host to apply; an "
            "allowlist entry naming the placeholder itself never matches. A variable "
            "unset there renders empty, and an empty host is refused."
        ),
        fix=(
            f"Unless it renders to a loopback host, allow that host in {_OPERATOR_ONLY} "
            f'where fluid apply runs: {ALLOWLIST_ENV}="{_suggested_entry(parsed, "<host>")}", '
            f"with <host> the host it renders to there (with {', '.join(variables)} as set "
            "there), without a port."
        ),
        doc=doc_url("secrets"),
    )
