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

"""Handler for ``fluid init --discover <uri>``.

Connects to the supplied URI, enumerates streams via
:mod:`fluid_build.cli.discover`, and emits one acquisition contract per
stream into the current directory.
"""

from __future__ import annotations

import builtins
import json
import logging
import re
from dataclasses import dataclass, replace
from itertools import groupby
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple
from urllib.parse import (
    ParseResult,
    quote,
    unquote,
    unquote_plus,
    urlparse,
    urlsplit,
    urlunparse,
)

import yaml

from fluid_build.cli._errors import (
    ConnectivityProbeError,
    FluidUserError,
    SchemaValidationError,
)
from fluid_build.cli.console import cprint
from fluid_build.cli.discover import DISCOVERERS, get_discoverer
from fluid_build.cli.discover._jdbc_base import JdbcDiscoverer
from fluid_build.cli.discover.emitter import emit_contract
from fluid_build.cli.discover.registry import DiscoveredStream
from fluid_build.observability import secret_redactor
from fluid_build.observability.secret_redactor import redact_secret_text, register_secret

# URI scheme → (engine, source.kind) tuple. The runner contract requires
# ``engine`` (which runner module to dispatch to) plus ``source.kind``
# (used for connector-class selection downstream).
_SCHEME_DEFAULTS = {
    "postgres": ("duckdb", "postgres"),
    "postgresql": ("duckdb", "postgres"),
    "mysql": ("duckdb", "mysql"),
    "mariadb": ("duckdb", "mysql"),
    "sqlite": ("duckdb", "sqlite"),
    "file": ("duckdb", "filesystem"),
    "https": ("duckdb", "filesystem"),
    "http": ("duckdb", "filesystem"),
    "s3": ("duckdb", "filesystem"),
    "gs": ("duckdb", "filesystem"),
    "gcs": ("duckdb", "filesystem"),
}

# The environment variable each database's own client reads the password
# from (libpq: PGPASSWORD; MySQL client: MYSQL_PWD). An emitted contract
# references it as ``secretRef: env://<VAR>`` instead of carrying the password.
_PASSWORD_ENV = {
    "postgres": "PGPASSWORD",
    "postgresql": "PGPASSWORD",
    "mysql": "MYSQL_PWD",
    "mariadb": "MYSQL_PWD",
}

# The shared redactor's mask, so every layer prints the same marker.
_MASK = secret_redactor._REDACTED

# The shared redaction registry refuses a literal shorter than this (it would
# match inside ordinary words). A query value that short is still masked
# wherever the URI is shown; it is just not also masked by value in later text.
_MIN_VALUE_SECRET_LEN = secret_redactor._MIN_REGISTERED_SECRET_LEN

# The name shown for a query parameter that has no "name=" part: the whole
# piece is a value, and values are never shown.
_UNNAMED = "(unnamed)"


@dataclass(frozen=True)
class _UriView:
    """What may be shown of a ``--discover`` URI, and what must not be."""

    safe: str  # the URI with its user:password part and every query value masked
    # The masked values, in every form a driver may print them, to mask again by
    # value in driver errors; not one too short to mask by value, nor one the
    # shown rest of the URI also spells out.
    secrets: Tuple[str, ...]
    ambiguous: bool  # urlsplit reads a different user:password part than we do
    # What the display does with each character of the URI (_SHOWN, _LEFT_OUT,
    # _MASKED), to mask a copy of it a driver quotes cut short. Empty for a path.
    marks: Tuple[int, ...] = ()
    # Every form of every masked value, however short and wherever else it is
    # spelled out: masked where a driver prints it as a DSN's password field.
    hidden: Tuple[str, ...] = ()
    # The user name is a database login, shown, not a possible token.
    database: bool = False
    # ``safe`` before the shared redactor masks known secret shapes in it (which
    # may hide a whole query's names): the parameter names the closing line may name.
    rendered: str = ""


def run_discover(args, logger: logging.Logger, *, uri: str) -> int:
    # Only the registry lookup, which reads the scheme, and
    # ``discoverer.discover`` see the URI as given. Everything shown to the
    # user, logged, raised or written to disk uses ``safe_uri`` or the
    # credential-free connection block.
    discoverer = get_discoverer(uri)
    # A JDBC discoverer, a registered one included, reads the user name apart
    # from the password, as libpq does: a login, whatever the scheme.
    view = _inspect_uri(uri, database=isinstance(discoverer, JdbcDiscoverer))
    safe_uri = view.safe
    if discoverer is None:
        raise SchemaValidationError(
            what=f"unsupported source scheme in --discover URI: {safe_uri}",
            why=(
                "The URI's scheme is not registered. Supported: "
                + ", ".join(sorted(_SCHEME_DEFAULTS.keys()))
                + "."
            ),
            fix="Use postgres://, mysql://, sqlite://, file://, s3://, https://, or http://.",
            doc="https://agenticstiger.github.io/forge_docs/cli/init.html",
            extras={"uri": safe_uri, "supported": sorted(_SCHEME_DEFAULTS.keys())},
        )
    if view.ambiguous:
        # A driver would read part of the password as the host or port and
        # quote it back in its error. Refuse before connecting.
        raise SchemaValidationError(
            what=f"cannot tell the password from the host in --discover URI: {safe_uri}",
            why=(
                "The URI has an '@' after a '/', '?' or '#' in what looks like "
                "user:password. URI parsers end the user:password part at the first of "
                "those characters, so a password containing one is read as the host, "
                "port and path, and a host with a port (host:9000/...) reads the same "
                "as a password that starts with digits. Only in a URL that is not a "
                "database URI, with no ':' or '@' before that first character, are a ':' "
                "and an '@' after the host read as the path's."
            ),
            fix=(
                "Take the password out of the URI: export PGPASSWORD (Postgres) or "
                "MYSQL_PWD (MySQL), or let the storage client's own credential chain "
                "supply it. For any other URL, percent-encode '/', '?', '#' and '@' "
                "inside the user:password part (%2F, %3F, %23, %40), and for an http:// "
                "or https:// URL an '@' in its path as %40."
            ),
            doc="https://agenticstiger.github.io/forge_docs/cli/init.html",
            extras={"uri": safe_uri},
        )

    cprint(f"🔍 Discovering streams at {safe_uri}…")
    failure: Optional[FluidUserError] = None
    try:
        # The operator typed this URI on the command line, so a JDBC
        # discoverer may use this host's client certificate and libpq
        # settings. Every discoverer, a registered JDBC subclass written
        # against main's interface included, is still called as discover(uri).
        from fluid_build.cli.discover._jdbc_base import discover_operator_uri

        streams = discover_operator_uri(discoverer, uri)
    except Exception as exc:  # noqa: BLE001
        # Driver errors often quote the URI or the libpq DSN they were given,
        # sometimes cut short, so a copy is masked by position before by value.
        failure = _probe_error(exc, uri, safe_uri, view)
    if failure is not None:
        # Raised once the handler has ended, so the driver's error is neither
        # the cause nor the context of this one. Inside the handler, ``raise ...
        # from None`` would only set __suppress_context__ (PEP 415) and keep it,
        # URI and all, on __context__, which a formatted traceback still prints.
        raise failure

    if not streams:
        cprint("(no streams found at the URI)")
        return 0

    parsed = urlparse(uri)
    scheme_base = (parsed.scheme.split("+", 1)[0]) or "file"
    # What the contracts record: the URI read as a URI, also when its discoverer is
    # registered under a key that is no RFC 3986 scheme.
    located = _located(uri, parsed)
    # The scheme the contracts are written under: for such a key, the key itself,
    # so the closing line and the stream names follow the connection block.
    written_as = located.scheme.split("+", 1)[0] or "file"
    streams = [_safe_stream(stream, uri, located, written_as, view) for stream in streams]
    engine, source_kind = _SCHEME_DEFAULTS.get(scheme_base, ("duckdb", scheme_base))
    connection, dropped_params, dropped_fragment = _connection_from_uri(located)

    target_dir = Path(getattr(args, "target_dir", None) or ".").resolve()
    target_dir.mkdir(parents=True, exist_ok=True)

    project = getattr(args, "name", None) or _slug(located.path.strip("/")) or scheme_base
    written: List[Path] = []

    for stream in streams:
        contract_id = f"bronze.{project}.{_slug(stream.name)}"
        contract = emit_contract(
            product_id=contract_id,
            name=f"{project} / {stream.name}",
            domain=getattr(args, "domain", None) or "data",
            owner_team=getattr(args, "owner_team", None) or "data-platform",
            owner_email=getattr(args, "owner_email", None) or "data-platform@example.com",
            engine=engine,
            source_kind=source_kind,
            connection=connection,
            streams=[stream],
        )
        out_path = target_dir / f"contract.{_slug(stream.name)}.fluid.yaml"
        out_path.write_text(yaml.safe_dump(contract, sort_keys=False), encoding="utf-8")
        written.append(out_path)
        cprint(f"  ✓ {out_path.relative_to(target_dir)}  ({len(stream.columns)} cols)")

    logger.info("discover.emitted count=%d uri=%s", len(written), safe_uri)
    cprint(f"\n✓ Emitted {len(written)} contract(s). Next: `fluid validate <file>`.")
    hint = _credential_hint(located, written_as, dropped_params, dropped_fragment, view.rendered)
    if hint:
        cprint(hint)
    return 0


def _probe_error(exc: Exception, uri: str, safe_uri: str, view: _UriView) -> FluidUserError:
    """The typed error for a discovery that failed with ``exc``, masked throughout.

    ``exc`` is usually the driver's own error, which quotes the URI or the DSN
    as given, in its message, its arguments, its notes or the errors chained to
    it. Only text is kept, masked as the display masks ``uri``: its message and
    those of the errors that explain it (:func:`_reason`). The cause is a
    stand-in naming the driver's exception type with that masked text, and
    carries no traceback, notes or chain of its own.

    An error that already says what to do keeps saying it. A refusal from the
    DuckDB sandbox reaches here through the same ``except Exception``, and
    reframing it as a connectivity failure replaced its own ``fix`` with "Check
    VPN / network policy / credentials" -- advice that sends the reader to their
    network for something in their working directory. Its fields are kept and
    masked instead, since the refusal names the location it refused and that
    can carry a password in its userinfo, which is what the masking here is
    for. (Spelling that URL shape out here trips the secret scanner, so it is
    described rather than written.)
    """

    def mask(text: str) -> str:
        return _masked(text, uri, view)

    reason = _reason(exc, mask)
    error = _kept_or_reframed(exc, mask, safe_uri, reason)
    error.__cause__ = RuntimeError(f"{_type_name(exc)}: {reason}")
    return error


def _kept_or_reframed(
    exc: Exception, mask: Callable[[str], str], safe_uri: str, reason: str
) -> FluidUserError:
    """``exc`` with its own fields masked when it is already a typed error.

    EVERY field :meth:`FluidUserError.as_json` emits is masked, ``extras`` and
    ``doc`` included. Leaving ``extras`` alone would have been the one way a
    value this handler masks everywhere else reached ``--json`` output: an
    out-of-tree discoverer that put the location as given under a key of its
    own (``dsn``, say) would have had it serialised verbatim, since ``"uri"``
    is overwritten here but no other key was touched. No error in the tree
    carries extras through this path, so the gap was latent -- but ``as_json``
    is precisely what the sibling tests assert a credential never reaches.

    ``replace`` needs the concrete class to be constructible from its fields,
    which every typed error in the tree is. A discoverer from outside the tree
    could raise a ``FluidUserError`` subclass that is not, and this code runs
    while an error is already being handled -- the last place to raise a second
    one -- so anything awkward falls back to the connectivity framing. The
    ``except`` is broad for that reason: a subclass can refuse to rebuild in
    more ways than ``TypeError``, and none of them is worth losing the original
    failure over.
    """
    if isinstance(exc, FluidUserError):
        try:
            return replace(
                exc,
                what=mask(exc.what),
                why=mask(exc.why),
                fix=mask(exc.fix),
                doc=mask(exc.doc) if exc.doc else exc.doc,
                where=mask(exc.where) if exc.where else exc.where,
                extras={**_masked_extras(exc.extras, mask), "uri": safe_uri},
            )
        except Exception:  # noqa: BLE001 - never raise while an error is live
            pass
    return ConnectivityProbeError.for_target(target=safe_uri, reason=reason)


def _masked_extras(extras: Any, mask: Callable[[str], str]) -> Dict[str, Any]:
    """``extras`` with every string masked, at any depth ``as_json`` would dump."""

    def walk(value: Any) -> Any:
        if isinstance(value, str):
            return mask(value)
        if isinstance(value, dict):
            return {key: walk(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [walk(item) for item in value]
        return value

    return {key: walk(value) for key, value in dict(extras or {}).items()}


def _type_name(exc: BaseException) -> str:
    """``exc``'s type as a traceback names it: the module is left out for a builtin."""
    kind = type(exc)
    module = getattr(kind, "__module__", None)
    return kind.__qualname__ if module in (None, "builtins") else f"{module}.{kind.__qualname__}"


def _message(exc: BaseException) -> str:
    """``str(exc)``, or the placeholder a traceback prints when that fails.

    This runs while the driver's error is being handled, so a failure raised
    from here would carry that error, URI and password in its arguments, as its
    ``__context__``. The placeholder is CPython's own (``traceback._safe_string``).
    """
    try:
        return str(exc)
    except Exception:  # noqa: BLE001 - whatever fails, the message is unavailable
        return "<exception str() failed>"


def _masked(text: str, uri: str, view: _UriView) -> str:
    """``text`` from a driver, with every copy of what the display of ``uri`` hides masked."""
    text = _mask_password_fields(_mask_uri_copies(text, uri, view), view.hidden)
    return redact_secret_text(text, extra_secrets=view.secrets)


# A formatted traceback shows at most this many sub-exceptions of an exception
# group, nested at most this deep (the ``max_group_width`` and ``max_group_depth``
# defaults of CPython's ``traceback.TracebackException``); the reason names as many.
_MAX_GROUP_WIDTH = 15
_MAX_GROUP_DEPTH = 10

# BaseExceptionGroup (PEP 654) is a builtin from Python 3.11.
_EXCEPTION_GROUPS: Tuple[type, ...] = tuple(
    kind for kind in (getattr(builtins, "BaseExceptionGroup", None),) if isinstance(kind, type)
)


def _reason(exc: BaseException, mask: Callable[[str], str]) -> str:
    """``exc``'s message, then each error that explains it, one line each, masked.

    Those are the errors a formatted traceback shows as its reasons: the one it
    was raised from (``raise ... from err``, its ``__cause__``), and so on down
    the chain, and the sub-exceptions of an exception group (an
    ``asyncio.TaskGroup``'s failed tasks). The stand-in that replaces the
    driver's error keeps no chain, so without them a plugin that wraps its
    low-level error would lose the reason it failed.

    The error being handled when ``exc`` was raised, an implicit
    ``__context__``, is not one of them: the DuckDB sandbox installs an
    extension when loading it fails, and the load failure is not why the
    install failed. Each error is named once, so a cycle ends.
    """
    lines: List[str] = []
    seen: set[int] = set()

    def explain(link: Optional[BaseException], label: str, depth: int) -> None:
        while link is not None and id(link) not in seen:
            seen.add(id(link))
            text = mask(_message(link))
            lines.append(f"{label}: {_type_name(link)}: {text}" if label else text)
            members: Sequence[BaseException] = (
                getattr(link, "exceptions", ()) if isinstance(link, _EXCEPTION_GROUPS) else ()
            )
            if depth < _MAX_GROUP_DEPTH:
                for number, member in enumerate(members[:_MAX_GROUP_WIDTH], start=1):
                    explain(member, f"sub-exception {number}", depth + 1)
                if len(members) > _MAX_GROUP_WIDTH:
                    lines.append(f"and {len(members) - _MAX_GROUP_WIDTH} more sub-exceptions")
            link, label = link.__cause__, "caused by"

    explain(exc, "", 0)
    return "\n".join(lines)


# The key of a password field and what separates it from the value: libpq's
# ``password=``, MySQL's ``password=`` or ``passwd=``, ODBC's ``PWD=``, the
# ``'password': `` of a mapping's repr or JSON (the separators the shared
# redactor's assignment pattern accepts), and any key that ends in one of those
# words, in any case: the clients' own environment variables (libpq's
# ``PGPASSWORD``, the MySQL client's ``MYSQL_PWD``), ``db_password``. The shared
# redactor reads a credential environment variable by its suffix the same way
# (``secret_redactor._SECRET_ENV_SUFFIXES``); the prefix is bounded as in its
# assignment pattern.
_PASSWORD_KEY = r"(?i:\b\w{,128}?(?:password|passwd|pwd))['\"]?\s*[:=]\s*"


def _mask_password_fields(text: str, hidden: Sequence[str]) -> str:
    """``text`` with each hidden value masked where it is a DSN's password field.

    The Postgres and MySQL scanners quote the DSN they were given in a connect
    error: libpq's ``password=<value>``, quoted with ``'`` when it holds a space
    or a quote, and the MySQL scanner's ``password="<value>"``. A value there is
    masked by its position, however short it is and whether or not the rest of
    the URI spells it too: the stock dev URI uses ``postgres`` as user, password
    and database, and the reason keeps naming the user and database it tried.

    A hidden value is masked wherever it starts right after a password key,
    whatever follows it: the end of a sentence, the next ``&KEY=`` of a query
    string or environment listing, a port, a path, an escaped newline or a
    terminal colour code. The value is one we hold, so no terminator has to be
    guessed (the shared redactor's note on its assignment pattern says the same
    of a literal it holds). Only a password key's value is touched, so the user
    and database names the reason gives stay readable.
    """
    if not hidden:
        return text
    values = "|".join(re.escape(value) for value in sorted(hidden, key=len, reverse=True))
    field = re.compile(_PASSWORD_KEY + r"(?:''|['\"])?(" + values + ")")
    # The key and the opening quote stay; the value, group 1, is masked.
    return field.sub(lambda m: m.group(0)[: m.start(1) - m.start(0)] + _MASK, text)


def _inspect_uri(uri: str, *, database: bool = False) -> _UriView:
    """What of ``uri`` may be shown: no credential, whatever the URI looks like.

    No part of the URI's grammar is trusted to say where a credential ends, and
    no parameter name is trusted to be harmless. Past the ``://`` that ends its
    scheme (:func:`_scheme_separator`):

    - the user:password part runs to the LAST ``@``, so a raw ``/``, ``?``, ``#``,
      ``@`` or space inside it does not end it early (pip's
      ``split_auth_from_netloc`` reads userinfo the same way). For a database
      URI (a database scheme, or ``database``: one a JDBC discoverer reads) the
      user name is shown and the rest is masked; for any other scheme, whose user
      name can be the token (git and HTTP clients put it there), the whole part
      is masked;
    - every query value is masked and its name kept, and the fragment is left
      out. The query is read from the first ``?`` and the fragment from the first
      ``#``, wherever that ``@`` is, so an ``@`` inside the query hides none of
      it. A ``?`` or ``#`` inside a password then hides the host too: over-masking
      the display is safe.

    ``ambiguous`` is set when ``urlsplit`` (what the drivers follow) would read
    part of the password as the host, port or path (:func:`_password_misread`);
    the caller refuses such a URI before any driver can quote that part back.

    A location with no such ``://`` is read as a local path, or as a credential
    URI typed without its ``//`` (:func:`_inspect_location`). One whose ``://``
    follows a key that is no RFC 3986 scheme is ``ambiguous`` wherever the text
    after it would be after such a scheme (:func:`_after_key`).

    The masked values are registered with the process-wide redaction registry
    before any driver sees the URI, each in every form a driver may print it in
    (:data:`_REQUOTINGS`: as given, as a SQL literal, as a libpq DSN value,
    repr, JSON, percent-encoded), so a later line that quotes one (a driver
    error, a log line) is masked by value however it was escaped.
    A value that also appears in the shown rest of the URI (the stock dev URI,
    whose user, password and database are all ``postgres``) is not registered,
    nor masked by value in this run's driver errors: it is a word of the
    target's own name, and masking it would garble the output without hiding
    anything. It is still masked as a DSN's password field
    (:func:`_mask_password_fields`).
    """
    sep = _scheme_separator(uri)
    if sep < 0:
        view = _inspect_location(uri)
        stand_in = _after_key(uri)
        if stand_in is not None and _userinfo_misread(stand_in, 1, database):
            view = replace(view, ambiguous=True)
        return view
    database = database or uri[:sep].lower().split("+", 1)[0] in _PASSWORD_ENV
    marks = [_SHOWN] * len(uri)
    # Each hidden literal, with the length below which it is not also masked by
    # value in later text: a credential always is (the registry warns when it
    # cannot), an ordinary word (a user name, a query value) only when long enough.
    found: List[Tuple[str, int]] = []
    ambiguous = _mark_userinfo(uri, sep, marks, found, database)
    _mark_query_and_fragment(uri, sep + 3, marks, found)
    return _view(uri, marks, found, ambiguous=ambiguous, database=database)


# RFC 3986, section 3.1: scheme = ALPHA *( ALPHA / DIGIT / "+" / "-" / "." )
_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*")


def _scheme_separator(uri: str) -> int:
    """Where the ``://`` that ends ``uri``'s scheme starts, or -1 when none does.

    Only the first ``://`` can, and only when the text before it is a scheme
    (:data:`_SCHEME`). In ``postgres:alice:PW@h/db?sslrootcert=file:///ca.pem``
    or ``...@h/db#https://x`` the first ``://`` is the query's or the fragment's,
    and in ``jdbc:postgresql://`` or ``my_db://`` what precedes it is no scheme:
    each is read as a location (:func:`_inspect_location`). A discoverer
    registered under such a key (documented on ``registry.register_discoverer``)
    gets the refusal the text after its ``://`` gets after an RFC 3986 scheme,
    and its contracts are written from that text read the same way
    (:func:`_after_key`, :func:`_located`).
    """
    sep = uri.find("://")
    return sep if sep >= 0 and _SCHEME.fullmatch(uri[:sep]) else -1


def _after_key(uri: str) -> Optional[str]:
    """``uri`` with ``x`` in place of a key that is no RFC 3986 scheme; else None.

    A discoverer registered under such a key (``my_db``, ``jdbc:postgresql``) is
    routed ``<key>://`` URIs (``registry.get_discoverer``), but ``urlparse`` reads
    no authority after the key: the whole URI is its path. Any RFC 3986 scheme
    reads the text after its ``://`` the same way, so ``x`` stands in for the
    key, and that text is refused, shown and written as it would be after one.
    """
    if _scheme_separator(uri) >= 0:
        return None
    _key, sep, rest = uri.partition("://")
    return f"x://{rest}" if sep else None


def _inspect_location(location: str) -> _UriView:
    """What of a location with no scheme followed by ``://`` may be shown.

    It is read as a local path, and the error for a path that does not exist
    quotes it. When it starts with the scheme of a credential URI
    (:func:`_location_scheme_end`), it is that URI typed without its ``//``
    (``postgres:alice:PW@h/db``, ``https:/h/f.csv?sig=...``): its user:password
    part and its query values are masked and registered as a URI's are
    (:func:`_mark_location`). Any other location is shown as given: a local file
    name may hold a ``:`` before an ``@`` (``backup:2024@snapshot.csv``,
    ``C:\\dir\\a@b.csv``).
    """
    colon = _location_scheme_end(location)
    if colon < 0:
        return _UriView(
            safe=redact_secret_text(location), secrets=(), ambiguous=False, rendered=location
        )
    marks = [_SHOWN] * len(location)
    found: List[Tuple[str, int]] = []
    _mark_location(location, colon, marks, found)
    return _view(location, marks, found, ambiguous=False, database=False)


# Schemes whose URI may carry a credential, besides those a discoverer registers:
# the database and storage schemes this handler reads, and Azure's.
_CREDENTIAL_SCHEMES = frozenset({*_SCHEME_DEFAULTS, "az", "azure", "abfs", "abfss"})


def _location_scheme_end(location: str) -> int:
    """Where the ``:`` after the scheme of a credential URI typed without ``//`` is.

    That is the first ``:`` of ``location``, when the text before it, its case
    and spaces and a ``+driver`` suffix aside, is a scheme a discoverer registers
    or one of :data:`_CREDENTIAL_SCHEMES`, or when ``location`` holds a ``://``
    that no scheme precedes. An encoded ``%3A`` before it counts as the ``:`` it
    decodes to. Otherwise -1: the location is read as a local path.
    """
    colon = location.find(":")
    if colon < 0:
        return -1
    if "://" in location:
        # No scheme precedes it (:func:`_scheme_separator`), and no local file
        # name holds a "://": a URI, whatever its scheme, masked from the first ":".
        return colon
    scheme = unquote(location[:colon]).partition(":")[0].strip().lower().split("+", 1)[0]
    registered = {name.lower() for name in DISCOVERERS}
    return colon if scheme in _CREDENTIAL_SCHEMES or scheme in registered else -1


def _mark_location(
    location: str, colon: int, marks: List[int], found: List[Tuple[str, int]]
) -> None:
    """Mark what a credential URI typed without ``//`` hides; its scheme ends at ``colon``.

    From that ``:`` to its last ``@`` is its user:password part, masked whole: the
    user name may be the token. Its query values are masked and its fragment left
    out, read from the first ``?`` and ``#`` after the scheme, as a URI's are.
    """
    start = colon + 1
    at = location.rfind("@", start)
    if at >= 0:
        span = location[start:at]
        # The whole part, and what follows its own first ":" (the password, when
        # the "//" alone is missing: ``postgres:alice:PW@``).
        found.extend([(span, 1), (span.partition(":")[2], 1)])
        _mark(marks, start, at, _MASKED)
    _mark_query_and_fragment(location, start, marks, found)


def _view(
    uri: str,
    marks: List[int],
    found: List[Tuple[str, int]],
    *,
    ambiguous: bool,
    database: bool,
) -> _UriView:
    """The view of ``uri`` shown as ``marks`` say, its ``found`` values registered."""
    safe = _render(uri, marks)
    # Each form of each hidden value, with the shortest length at which a value
    # that has it is masked by value too.
    hidden: Dict[str, int] = {}
    for value, min_len in found:
        for literal in (value, unquote(value), unquote_plus(value)):
            for form in _requoted(literal):
                if form:
                    hidden[form] = min(hidden.get(form, min_len), min_len)
    secrets = tuple(
        form for form, min_len in hidden.items() if len(form) >= min_len and form not in safe
    )
    for literal in secrets:
        register_secret(literal)
    return _UriView(
        safe=redact_secret_text(safe),
        secrets=secrets,
        ambiguous=ambiguous,
        marks=tuple(marks),
        hidden=tuple(hidden),
        database=database,
        rendered=safe,
    )


# What the display does with each character of a URI. A character marked twice
# keeps the stronger mark.
_SHOWN, _LEFT_OUT, _MASKED = 0, 1, 2


def _mark(marks: List[int], lo: int, hi: int, mark: int) -> None:
    for i in range(lo, hi):
        marks[i] = max(marks[i], mark)


def _mark_userinfo(
    uri: str, sep: int, marks: List[int], found: List[Tuple[str, int]], database: bool
) -> bool:
    """Mark the user:password part, up to the LAST "@" after ``://`` at ``sep``.

    ``database`` says the user name is a database login, not a possible token.
    Returns whether a driver following ``urlsplit`` would read part of the
    password as the host, port or path (:func:`_userinfo_misread`).
    """
    start = sep + 3
    at = uri.rfind("@", start)
    if at < 0:
        return False
    userinfo = uri[start:at]
    user, colon, password = userinfo.partition(":")
    if not database:
        # The user name can be the token: git and HTTP clients put it there.
        if userinfo:
            found.extend([(userinfo, 1), (password, 1), (user, _MIN_VALUE_SECRET_LEN)])
            _mark(marks, start, at, _MASKED)
    elif colon:
        # A database user name is not a credential; the password is.
        found.append((password, 1))
        _mark(marks, start + len(user) + 1, at, _MASKED)
    elif userinfo != _rfc_userinfo(uri):
        # What reads as a database user name may be the host and path.
        found.append((userinfo, 1))
        _mark(marks, start, at, _MASKED)
    return _userinfo_misread(uri, sep, database)


def _userinfo_misread(uri: str, sep: int, database: bool) -> bool:
    """Whether a driver would read part of the password of ``uri`` as its host,
    port or path; ``uri``'s scheme ends with the ``://`` at ``sep``.

    That needs a ``user:password`` part, read up to the last ``@``, that
    ``urlsplit`` reads differently (:func:`_password_misread` says when the
    difference reaches the password).
    """
    start = sep + 3
    at = uri.rfind("@", start)
    if at < 0:
        return False
    userinfo = uri[start:at]
    if ":" not in userinfo or userinfo == _rfc_userinfo(uri):
        return False
    return _password_misread(uri, database)


# What a host name alone never holds: an "@", or the ":" of a port or of an IP
# literal, or an IP literal's brackets (RFC 3986, sections 3.2.1 to 3.2.3).
_NOT_A_HOST_NAME = frozenset("@:[]")


def _password_misread(uri: str, database: bool) -> bool:
    """Whether ``urlsplit`` may read part of a password in ``uri`` as the host or port.

    It is asked of a URI whose user:password part, read up to the last ``@``,
    holds a ``:`` and differs from the one ``urlsplit`` reads. ``urlsplit`` ends
    the authority at the first ``/``, ``?`` or ``#`` after the ``//`` (RFC 3986,
    section 3.2), as the DuckDB sandbox reads a remote location's host
    (``_duckdb_sandbox._REMOTE_RE``), and ends its user:password part at the
    authority's last ``@``. A URI that reads both ways is refused:

    - the authority holds an ``@`` as well: ``urlsplit`` ends the user:password
      part there and reads the rest of the password as the host and path;
    - the authority holds a ``:``, a port's included, or a bracket:
      ``alice:pa/ss@host`` reads as the host ``alice`` and the rest of the
      password as the port and path, and ``localhost:9000/bucket/a@b.csv`` as the
      user name ``localhost`` with a password that starts ``9000/``. No int()
      reads the port, so no run of digits is too long to judge;
    - a database URI: its path is a database name, and a credential typed with an
      extra ``/`` (``postgres:///alice:PW@host/db``) has that shape.

    What is left is a URL whose authority is a host name alone: the ``:`` and the
    ``@`` after it are its path's, query's or fragment's own
    (``s3://bucket/2024-01-01T10:00:00/user@example.com/x.csv``), and it is used
    as given. A URI that ``urlsplit`` cannot read is refused too.
    """
    try:
        netloc = urlsplit(uri).netloc
    except ValueError:  # e.g. an unbalanced "[" in the host
        return True
    return database or not _NOT_A_HOST_NAME.isdisjoint(netloc)


def _rfc_userinfo(uri: str) -> Optional[str]:
    """The user:password part as ``urlsplit`` (RFC 3986) reads it; None if it cannot."""
    try:
        netloc = urlsplit(uri).netloc
    except ValueError:  # e.g. an unbalanced "[" in the host
        return None
    return netloc.rpartition("@")[0]


def _mark_query_and_fragment(
    uri: str, start: int, marks: List[int], found: List[Tuple[str, int]]
) -> None:
    """Mask every query value and leave the fragment out.

    Both are read from the first "?" and "#" after ``start``, wherever the
    user:password part ends, so an "@" inside the query hides none of it.
    """
    hash_at = uri.find("#", start)
    query_end = hash_at if hash_at >= 0 else len(uri)
    query_at = uri.find("?", start, query_end)
    if query_at >= 0:
        for lo, hi in _value_spans(uri, query_at + 1, query_end):
            found.append((uri[lo:hi], _MIN_VALUE_SECRET_LEN))
            _mark(marks, lo, hi, _MASKED)
    if hash_at >= 0:
        # Its values are still masked by value in later text, like the query's.
        found.append((uri[hash_at + 1 :], _MIN_VALUE_SECRET_LEN))
        for lo, hi in _value_spans(uri, hash_at + 1, len(uri)):
            found.append((uri[lo:hi], _MIN_VALUE_SECRET_LEN))
        _mark(marks, hash_at, len(uri), _LEFT_OUT)


def _render(uri: str, marks: List[int]) -> str:
    """``uri`` as shown: a run of hidden characters becomes one mask, or nothing
    when the whole run is only left out."""
    shown: List[str] = []
    for hidden, run in groupby(range(len(uri)), key=lambda i: marks[i] != _SHOWN):
        indexes = list(run)
        if not hidden:
            shown.append(uri[indexes[0] : indexes[-1] + 1])
        elif any(marks[i] == _MASKED for i in indexes):
            shown.append(_MASK)
    return "".join(shown)


def _percent_encoded(safe: str) -> Callable[[str], str]:
    """Percent-encodes one character unless it is in ``safe``, a letter, a digit or ``-._~``."""

    def encode(c: str) -> str:
        try:
            # An argument byte that is not UTF-8 arrives as a surrogate: encode that byte.
            return quote(c, safe=safe, errors="surrogateescape")
        except UnicodeEncodeError:  # a surrogate no such byte stands for
            return quote(c, safe=safe, errors="surrogatepass")

    return encode


_LIBPQ_ESCAPES = {"\\": "\\\\", "'": "\\'"}

# What a URL may hold besides letters, digits and "-._~" (RFC 3986), "%" included.
_URL_CHARS = "!#$%&'()*+,/:;=?@[]~"

# How a driver or library may print back a string it was given, one character at
# a time. The URI, and each value it hides, is masked in every one of these forms,
# so a copy is masked however the code that echoed it escaped it.
_REQUOTINGS: Tuple[Callable[[str], str], ...] = (
    # As given: DuckDB's "HTTP HEAD to '<url>'" and "File <url> requires ...".
    lambda c: c,
    # A SQL string literal, each quote doubled: DuckDB's "LINE 1:" context shows
    # the query it parsed (quote_ansi_string_literal).
    lambda c: "''" if c == "'" else c,
    # A libpq DSN value: the Postgres and MySQL extensions print their DSN, a
    # quote or backslash escaped with a backslash (libpq_escape) ...
    lambda c: _LIBPQ_ESCAPES.get(c, c),
    # ... and that DSN inside the SQL literal of the ATTACH statement.
    lambda c: _LIBPQ_ESCAPES.get(c, c).replace("'", "''"),
    # Python's repr(), as the DuckDB sandbox and urllib quote a value. It escapes a
    # single quote only when the string holds a double one too.
    lambda c: "\\'" if c == "'" else repr(c)[1:-1],
    lambda c: repr(c)[1:-1],
    # A JSON string, non-ASCII escaped or not.
    lambda c: json.dumps(c)[1:-1],
    lambda c: json.dumps(c, ensure_ascii=False)[1:-1],
    # Percent-encoded: what a URL may not hold, or everything that is not unreserved.
    _percent_encoded(_URL_CHARS),
    _percent_encoded(""),
)


def _requote(text: str, requoting: Callable[[str], str]) -> List[str]:
    """``text`` as ``requoting`` prints it, one piece per character of ``text``."""
    return [requoting(c) for c in text]


def _requoted(text: str) -> Iterator[str]:
    """``text`` in each form of :data:`_REQUOTINGS`."""
    for requoting in _REQUOTINGS:
        yield "".join(_requote(text, requoting))


def _requoted_forms(uri: str, marks: Sequence[int]) -> List[Tuple[str, Tuple[int, ...]]]:
    """``uri`` in each form of :data:`_REQUOTINGS`, with the display's mark for
    each character of the form: the mark of the URI character it came from."""
    forms: List[Tuple[str, Tuple[int, ...]]] = []
    for requoting in _REQUOTINGS:
        pieces = _requote(uri, requoting)
        form_marks = tuple(
            mark for piece, mark in zip(pieces, marks, strict=True) for _char in piece
        )
        forms.append(("".join(pieces), form_marks))
    return forms


def _closest_form(
    text: str, at: int, forms: Sequence[Tuple[str, Tuple[int, ...]]]
) -> Tuple[int, List[int], int]:
    """How far ``text`` from ``at`` reads the same as the URI in its closest form.

    Returns that length, the display's marks for what was read, and where the
    form's first hidden character is (past its end when it hides nothing). When
    several forms read as far, each character keeps the strongest of their marks
    and the earliest first hidden character counts.
    """
    best = -1
    best_marks: List[int] = []
    first_hidden = 0
    for form, form_marks in forms:
        same = 0
        limit = min(len(form), len(text) - at)
        while same < limit and text[at + same] == form[same]:
            same += 1
        hidden = next((i for i, mark in enumerate(form_marks) if mark != _SHOWN), len(form) + 1)
        if same > best:
            best, best_marks, first_hidden = same, list(form_marks[:same]), hidden
        elif same == best:
            best_marks = [max(a, b) for a, b in zip(best_marks, form_marks[:same], strict=True)]
            first_hidden = min(first_hidden, hidden)
    return best, best_marks, first_hidden


# Characters that end a URI quoted in a driver's error text.
_QUOTED_URI_END = frozenset("'\"`<>\t\r\n ")

# What a driver may put after a URI it cut short ("...", "…") or after a whole
# one (",", ")"), none of it taken from the URI.
_TRAILER = frozenset(".\u2026,;:)]}")


def _mask_uri_copies(text: str, uri: str, view: _UriView) -> str:
    """``text`` with each copy of ``uri`` in it masked as the display masks ``uri``.

    A copy is a run that starts with the URI's ``scheme://`` (for a location, the
    text up to the ``:`` its credential follows), as any form of
    :data:`_REQUOTINGS` prints it (DuckDB's "LINE 1:" context shows the URI as the
    SQL literal it parsed, each quote doubled; repr doubles a backslash a
    location's head may hold). A driver may quote the URI cut short
    (DuckDB ends that context with ``<prefix>...``), so a copy may hold only the
    start of a masked value, which masking by value cannot find. Each character
    of a copy is therefore masked by its position:

    - while the copy reads the same as the URI in the form it reads closest to,
      by what the display does with the URI character it came from;
    - past the point where it stops reading the same, if that point is at or
      after the form's first hidden character, the rest is masked (a copy of a
      value escaped some other way), unless the rest is only a cut-short marker
      or trailing punctuation;
    - throughout, by what the display would do with the copy as a URI of its
      own, so a query value or user:password part it carries is masked too.
    """
    if not view.marks:
        return text
    forms = _requoted_forms(uri, view.marks)
    head = uri[: _scheme_head(uri)]
    heads = {"".join(_requote(head, requoting)) for requoting in _REQUOTINGS}
    start = re.compile(
        "|".join(re.escape(form) for form in sorted(heads, key=len, reverse=True)), re.IGNORECASE
    )
    shown: List[str] = []
    pos = 0
    while True:
        found = start.search(text, pos)
        if found is None:
            break
        at = found.start()
        same, marks, first_hidden = _closest_form(text, at, forms)
        end = at + same
        while end < len(text) and text[end] not in _QUOTED_URI_END:
            end += 1
        copy = text[at:end]
        rest = copy[same:]
        trailer = set(rest) <= _TRAILER
        rest_hidden = same >= first_hidden and not trailer
        marks += [_MASKED if rest_hidden else _SHOWN] * len(rest)
        # A trailer is not part of the copy read as a URI of its own.
        body = copy[:same] if trailer else copy
        own = [_SHOWN] * len(copy)
        _mark_own(body, own, view.database)
        shown.append(text[pos:at])
        shown.append(_render(copy, [max(a, b) for a, b in zip(marks, own, strict=True)]))
        pos = end
    shown.append(text[pos:])
    return "".join(shown)


def _scheme_head(uri: str) -> int:
    """The length of ``uri``'s ``scheme://``, or of ``scheme:`` for a credential
    URI typed without ``//``; 0 for a local path."""
    sep = _scheme_separator(uri)
    if sep >= 0:
        return sep + 3
    return _location_scheme_end(uri) + 1


def _mark_own(copy: str, marks: List[int], database: bool) -> None:
    """Mark what the display would hide of ``copy`` read as a URI of its own."""
    sep = _scheme_separator(copy)
    if sep >= 0:
        _mark_userinfo(copy, sep, marks, [], database)
        _mark_query_and_fragment(copy, sep + 3, marks, [])
        return
    colon = _location_scheme_end(copy)
    if colon >= 0:
        _mark_location(copy, colon, marks, [])


def _value_spans(text: str, lo: int, hi: int) -> List[Tuple[int, int]]:
    """The ``(start, end)`` of each parameter value in the query ``text[lo:hi]``.

    Parameters are separated by "&". A value is everything after its parameter's
    first "=", or the whole parameter when it has none. A ";" is not a separator
    (``urllib.parse`` has not split on it since Python 3.10): whatever follows one
    stays inside the value it ends up in.
    """
    spans: List[Tuple[int, int]] = []
    while lo <= hi:
        amp = text.find("&", lo, hi)
        end = amp if amp >= 0 else hi
        eq = text.find("=", lo, end)
        value_lo = eq + 1 if eq >= 0 else lo
        if value_lo < end:
            spans.append((value_lo, end))
        lo = end + 1
    return spans


def _safe_stream(
    stream: DiscoveredStream, uri: str, parsed: ParseResult, scheme_base: str, view: _UriView
) -> DiscoveredStream:
    """``stream``, renamed if its name could carry part of a URL's query or userinfo.

    The name becomes the contract's name, its stream list and its file name. The
    filesystem discoverer names a remote file after the URL's path; a discoverer
    that named it after the last segment of the URI as given (``Path(uri).stem``)
    would put a query holding a "/" into it, so such a name is replaced by the
    path's. As a backstop, a value the display masks is masked in the name too.
    """
    if scheme_base in _PASSWORD_ENV:
        # A table name read from the database, not from the URI.
        return stream
    name = stream.name
    if parsed.netloc and name == Path(uri).stem:
        name = Path(parsed.path).stem or parsed.hostname or scheme_base
    # A credential the URI's path or host also spells out (``https://data@h/data.csv``)
    # names the target; it is left alone, as ``_inspect_uri`` leaves it unregistered.
    for literal in view.secrets:
        if literal and literal in name and literal not in view.safe:
            name = name.replace(literal, "redacted")
    if name == stream.name:
        return stream
    return replace(stream, name=name)


def _credential_hint(
    parsed: ParseResult,
    scheme_base: str,
    dropped_params: Tuple[str, ...],
    dropped_fragment: bool,
    shown: str,
) -> Optional[str]:
    """One line saying what was left out of the contracts, and how to supply it.

    It names the query parameters left out, never their values, and a name only
    where ``shown``, the URI as the display renders it (``_UriView.rendered``),
    spells it out as one (:func:`_shows_parameter`). A URL whose user name or
    token holds a raw ``?`` is used as given, and what follows that ``?`` is its
    query: the display masks it as part of the user:password part, and so does
    this line.
    """
    try:
        password = parsed.password
        username = parsed.username
    except ValueError:
        return None
    env_var = _PASSWORD_ENV.get(scheme_base)
    if env_var:
        if not password:
            return None
        return (
            f"🔑 The password was not written to the contracts: they read it from "
            f"secretRef env://{env_var}. Export {env_var} before `fluid apply`."
        )
    left_out = []
    if password:
        left_out.append("user and password")
    elif username:
        left_out.append("user name")
    if dropped_params:
        named = {
            name if name == _UNNAMED or _shows_parameter(shown, name) else _MASK: None
            for name in dropped_params
        }
        left_out.append("query parameter(s) " + ", ".join(named))
    if dropped_fragment:
        left_out.append("#fragment")
    if not left_out:
        return None
    listed = (
        left_out[-1] if len(left_out) == 1 else ", ".join(left_out[:-1]) + " and " + left_out[-1]
    )
    return (
        f"🔑 Not written to the contracts: the URI's {listed}. If the source needs "
        "them, supply them at run time: give the connection a secretRef "
        "(secretRef: env://<VAR>) or let the storage client's own credential "
        "chain authenticate."
    )


def _shows_parameter(shown: str, name: str) -> bool:
    """Whether ``shown``, a URI as the display renders it, spells ``name`` out as a
    query parameter's name.

    The mask holds no ``?``, ``&`` or ``=``, so a match is text the display's
    rules show: naming it shows nothing they hide.
    """
    return f"?{name}=" in shown or f"&{name}=" in shown


def _located(uri: str, parsed: ParseResult) -> ParseResult:
    """``uri`` as the contracts record it: ``parsed``, unless a key that is no
    RFC 3986 scheme names its discoverer.

    ``urlparse`` reads no authority after such a key (``my_db://``,
    ``jdbc:postgresql://``): the whole URI, user:password part included, is its
    path, and a connection or project name built from that would carry the
    password. The text after the ``://`` is read here as it would be read after
    an RFC 3986 scheme (:func:`_after_key`), the key standing in as the scheme,
    and written as such a URI would be. Where that reading would refuse the URI,
    it was refused before any connection (:func:`_inspect_uri`).
    """
    stand_in = _after_key(uri)
    if stand_in is None:
        return parsed
    try:
        located = urlparse(stand_in)
    except ValueError:  # e.g. an unbalanced "[" in the host: nothing of it is kept
        located = urlparse("x:")
    return located._replace(scheme=uri.partition("://")[0])


def _connection_from_uri(
    parsed: ParseResult,
) -> Tuple[Dict[str, Any], Tuple[str, ...], bool]:
    """The contract's connection block, and what of the URI was left out of it.

    Returns the block, the names of the query parameters left out, and whether a
    fragment was. The block never carries a credential. Emitted contracts are
    meant to be committed, so a database password becomes
    ``secretRef: env://<VAR>`` (resolved at run time by
    ``build_runners._acquisition_common.resolve_connection_secrets``), and a URL
    keeps no userinfo, no query string and no fragment: no parameter name is
    trusted to be harmless, so every one is left out and named in the hint.
    ``parsed`` is the URI as :func:`_located` reads it, so this holds for a
    discoverer registered under any key.
    """
    scheme_base = parsed.scheme.split("+", 1)[0]
    if scheme_base in _PASSWORD_ENV:
        connection: Dict[str, Any] = {
            "host": parsed.hostname or "localhost",
            "port": parsed.port or (5432 if "postgres" in parsed.scheme else 3306),
            "database": (parsed.path or "/").lstrip("/") or None,
            "user": parsed.username,
        }
        if parsed.password:
            connection["secretRef"] = f"env://{_PASSWORD_ENV[scheme_base]}"
        return connection, (), False
    if parsed.scheme == "sqlite":
        return {"path": parsed.path}, (), False
    # Drop the userinfo the way pip's ``remove_auth_from_url`` does: keep only
    # what follows the last "@" of the authority.
    netloc = parsed.netloc.rpartition("@")[2]
    uri = urlunparse(parsed._replace(netloc=netloc, query="", fragment=""))
    return {"uri": uri}, _query_names(parsed.query), bool(parsed.fragment)


def _query_names(query: str) -> Tuple[str, ...]:
    """The parameter names of ``query``, as written, each once; values never."""
    names: Dict[str, None] = {}
    for piece in query.split("&"):
        if piece:
            name, eq, _value = piece.partition("=")
            names[name if eq and name else _UNNAMED] = None
    return tuple(names)


def _slug(s: str) -> str:
    out = []
    for ch in (s or "").lower():
        if ch.isalnum():
            out.append(ch)
        elif ch in (".", "_", "-"):
            out.append("_")
        else:
            out.append("_")
    slug = "".join(out).strip("_")
    return slug or "default"
