# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Airflow's REST API on both majors, for ``fluid schedule-sync --airflow-url``.

Airflow 2 serves the stable API under ``/api/v1``; Airflow 3 removed it for ``/api/v2``
(every ``/api/v1`` path answers 404 with ``"/api/v1 has been removed in Airflow 3"``).
:meth:`AirflowApi.connect` finds out which one a URL serves: ``GET /api/v2/version``,
then ``GET /api/v1/version``, both answered without credentials on 2.10 and 3.x.

Authentication, per major:

* a **token** (``FLUID_AIRFLOW_TOKEN_REF``) is sent as ``Authorization: Bearer`` on both
  majors: an Airflow 3 JWT issued elsewhere (``airflowctl auth token``, a CI job), or the
  token of a deployment that fronts Airflow 2 with a token auth backend;
* a **user name and password** (``FLUID_AIRFLOW_CREDENTIAL_REF``, ``user:password``) is
  HTTP Basic on Airflow 2, which needs ``airflow.api.auth.backend.basic_auth`` in
  ``[api] auth_backends``, and on Airflow 3 is exchanged for a JWT with
  ``POST /auth/token`` (``{"username", "password"}`` -> 201 ``{"access_token"}``, the
  simple and the FAB auth manager alike). Airflow 3 does not take Basic on ``/api/v2``.
  A token Airflow 3 refuses later (403 ``Invalid JWT token``, not 401) is renewed once.

Both variables hold a secret *reference* (``env://``, ``file://``, ``vault://``,
``aws-sm://``) resolved through :func:`fluid_build.secrets.resolve_credential`, like the
``FLUID_LAKEKEEPER_*`` references, and both are operator-only
(``credentials/protected_env.py``): a project ``.env`` cannot choose them. The operator
names both halves, the URL on the command line and the credential in the environment,
so the contract's destination policy does not apply. Resolved values are registered with
the exact-value redactor.

Transport: :func:`fluid_build.util.http_client.new_client` (TLS verified, proxy
environment honoured), redirects never followed (a 307 would resend a password to
wherever it points), plain ``http`` only to a loopback host unless
``FLUID_AIRFLOW_ALLOW_INSECURE_HTTP`` is set. Nothing here echoes a URL's user info or
query.

The two dialects are read into one shape (:class:`DagState`, :class:`ImportErrorRecord`,
:class:`BundleState`); the field names are Airflow's own, from the OpenAPI documents the
2.10.5 and 3.x wheels ship (``api_connexion/openapi/v1.yaml``,
``api_fastapi/core_api/openapi/v2-rest-api-generated.yaml``). The official
``apache-airflow-client`` was not taken: its 2.x line speaks only ``/api/v1`` and its 3.x
line only ``/api/v2``, under one import name, so one process cannot hold both.

``httpx`` is imported inside the functions that send a request: this module is imported
only when ``--airflow-url`` is set.
"""

from __future__ import annotations

import base64
import ipaddress
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional
from urllib.parse import quote, urlsplit, urlunsplit

#: Operator-only: a secret reference resolving to a bearer token.
TOKEN_REF_ENV = "FLUID_AIRFLOW_TOKEN_REF"
#: Operator-only: a secret reference resolving to ``user:password``.
CREDENTIAL_REF_ENV = "FLUID_AIRFLOW_CREDENTIAL_REF"
#: Operator-only: allow plain ``http`` to a host other than loopback.
ALLOW_HTTP_ENV = "FLUID_AIRFLOW_ALLOW_INSECURE_HTTP"

API_CHOICES = ("auto", "v1", "v2")

_PAGE = 100
#: 10,000 rows: far past any DAG folder, short of an unbounded read.
_MAX_PAGES = 100
_DETAIL_CHARS = 200
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_TRUE = {"1", "true", "yes", "on"}
_TIMESTAMP = re.compile(
    r"(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|z|[+-]\d{2}:?\d{2})?"
)
_V1_REMOVED = "has been removed in Airflow 3"
_INVALID_JWT = "Invalid JWT token"

_E_URL = "schedule_sync_airflow_bad_url"
_E_HTTP = "schedule_sync_airflow_insecure_url"
_E_CREDS = "schedule_sync_airflow_credentials"
_E_UNREACHABLE = "schedule_sync_airflow_unreachable"
_E_NOT_FOUND = "schedule_sync_airflow_api_not_found"
_E_MISMATCH = "schedule_sync_airflow_api_mismatch"
_E_VERSION = "schedule_sync_airflow_unsupported_version"
_E_AUTH = "schedule_sync_airflow_auth_failed"
_E_FORBIDDEN = "schedule_sync_airflow_forbidden"
_E_REDIRECT = "schedule_sync_airflow_redirect"
_E_RESPONSE = "schedule_sync_airflow_bad_response"


class AirflowApiError(Exception):
    """A typed failure: ``event`` is the error-catalog key, ``fix`` the next step.

    ``transient`` marks what may pass on its own (a connection refused, a 5xx, a 429):
    :meth:`AirflowApi.connect` tries again, and the wait for a parse rides it out.
    """

    def __init__(
        self,
        event: str,
        message: str,
        fix: str = "",
        *,
        transient: bool = False,
        out_of_time: bool = False,
    ):
        super().__init__(message)
        self.event = event
        self.message = message
        self.fix = fix
        self.transient = transient
        #: The request was not sent because the caller's deadline had passed.
        self.out_of_time = out_of_time


# ── credentials ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Credentials:
    """How requests authenticate: ``none``, ``token`` or ``password``."""

    style: str = "none"
    username: str = ""
    password: str = field(default="", repr=False)
    token: str = field(default="", repr=False)

    @classmethod
    def none(cls) -> "Credentials":
        return cls()

    @classmethod
    def from_token(cls, token: str) -> "Credentials":
        return cls(style="token", token=token)

    @classmethod
    def from_password(cls, username: str, password: str) -> "Credentials":
        return cls(style="password", username=username, password=password)


def credentials_from_env(environ: Optional[Mapping[str, str]] = None) -> Credentials:
    """The credentials the operator configured, resolved; :meth:`Credentials.none` if none."""
    env = os.environ if environ is None else environ
    token_ref = str(env.get(TOKEN_REF_ENV) or "").strip()
    credential_ref = str(env.get(CREDENTIAL_REF_ENV) or "").strip()
    if token_ref and credential_ref:
        raise AirflowApiError(
            _E_CREDS,
            f"Both {TOKEN_REF_ENV} and {CREDENTIAL_REF_ENV} are set; set one, not both.",
            f"Keep {TOKEN_REF_ENV} for a token Airflow issued, or {CREDENTIAL_REF_ENV} for "
            "a user name and password.",
        )
    if token_ref:
        value = _resolve(token_ref, TOKEN_REF_ENV, env)
        return Credentials.from_token(_header_safe(value, f"{TOKEN_REF_ENV} resolved to"))
    if credential_ref:
        value = _resolve(credential_ref, CREDENTIAL_REF_ENV, env).strip()
        username, sep, password = value.partition(":")
        if not sep or not username or not password:
            raise AirflowApiError(
                _E_CREDS,
                f"{CREDENTIAL_REF_ENV} must resolve to username:password (the value is not "
                "shown).",
                "Store the user name, a colon and the password in the secret it names.",
            )
        if _CONTROL.search(value):
            raise AirflowApiError(
                _E_CREDS,
                f"{CREDENTIAL_REF_ENV} resolved to a value with a control character (the "
                "value is not shown).",
            )
        _register(password)
        # The form an HTTP Basic header carries, in case a proxy echoes request headers.
        _register(base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii"))
        return Credentials.from_password(username, password)
    return Credentials.none()


def _resolve(ref: str, name: str, env: Mapping[str, str]) -> str:
    from fluid_build.secrets import SecretError, resolve_credential

    try:
        secret = resolve_credential(ref, environ=env)
    except SecretError as exc:
        raise AirflowApiError(
            _E_CREDS,
            f"{name} did not resolve: {exc}",
            f"Point {name} at a secret reference that exists (env://, file://, vault://, "
            "aws-sm://).",
        ) from None
    value = secret.get_secret_value() if hasattr(secret, "get_secret_value") else secret
    text = str(value or "")
    _register(text)
    return text


def _header_safe(value: str, what: str) -> str:
    plain = value.strip()
    if not plain:
        raise AirflowApiError(_E_CREDS, f"{what} an empty value.")
    if _CONTROL.search(plain) or not plain.isascii():
        raise AirflowApiError(
            _E_CREDS,
            f"{what} a value with a control or non-ASCII character (the value is not shown).",
        )
    _register(plain)
    return plain


def _register(value: str) -> None:
    # The exact-value redactor refuses (and warns about) values under 6 characters;
    # resolve_credential has already registered the whole resolved value.
    if len(value) >= 6:
        from fluid_build.observability.secret_redactor import register_secret

        register_secret(value)


# ── the URL ──────────────────────────────────────────────────────────────


def normalize_url(raw: str, environ: Optional[Mapping[str, str]] = None) -> str:
    """``--airflow-url`` as the base every API path is appended to, or a typed refusal."""
    env = os.environ if environ is None else environ
    text = (raw or "").strip()
    fix = "Pass the http(s) URL of Airflow's API server, e.g. https://airflow.example.org."
    try:
        parts = urlsplit(text)
        host = parts.hostname
        parts.port  # noqa: B018 -- raises ValueError on a bad port
    except ValueError as exc:
        # Not echoed: the URL can carry a user name and password.
        raise AirflowApiError(_E_URL, f"--airflow-url does not parse ({exc}).", fix) from None
    if parts.scheme not in ("http", "https") or not host:
        raise AirflowApiError(
            _E_URL,
            f"--airflow-url must be an http(s) URL with a host (scheme "
            f"{parts.scheme or 'none'!r}, {'a' if host else 'no'} host).",
            fix,
        )
    if not _host_ok(host):
        raise AirflowApiError(
            _E_URL,
            "--airflow-url has a host name DNS cannot hold (an empty or overlong label).",
            fix,
        )
    if "@" in parts.netloc:
        raise AirflowApiError(
            _E_URL,
            "--airflow-url carries a user name or password.",
            f"Drop them from the URL; put the credential behind {CREDENTIAL_REF_ENV} or "
            f"{TOKEN_REF_ENV}.",
        )
    if parts.query or parts.fragment:
        raise AirflowApiError(
            _E_URL,
            "--airflow-url has a query or a fragment; forge appends API paths to it.",
            fix,
        )
    if parts.scheme == "http" and not _loopback(host):
        if str(env.get(ALLOW_HTTP_ENV) or "").strip().lower() not in _TRUE:
            raise AirflowApiError(
                _E_HTTP,
                f"Refusing to send Airflow credentials over plain HTTP to {host}.",
                f"Use https://, or set {ALLOW_HTTP_ENV}=true in the operator environment "
                "for a network where plain HTTP is intended.",
            )
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


def _host_ok(host: str) -> bool:
    """A host name made of labels of 1-63 characters that IDNA can encode, or an IP."""
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    labels = host[:-1].split(".") if host.endswith(".") else host.split(".")
    if len(host) > 253 or any(not label or len(label) > 63 for label in labels):
        return False
    try:
        host.encode("idna")
    except UnicodeError:
        return False
    return True


def _loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _where(location: Optional[str]) -> str:
    """Where a redirect points, without user info, query or fragment."""
    try:
        parts = urlsplit(location or "")
        host = parts.hostname or ""
        port = f":{parts.port}" if parts.port else ""
    except ValueError:
        return "an unreadable location"
    if not host:
        return f"a path on the same host ({parts.path or '/'})"
    return f"{parts.scheme}://{host}{port}{parts.path}"


# ── what the API says, in one shape ──────────────────────────────────────


def parse_timestamp(value: Any) -> Optional[datetime]:
    """An Airflow timestamp as an aware UTC datetime; ``None`` if it is not one.

    Python 3.10's ``fromisoformat`` takes neither ``Z`` nor a fraction other than 3 or 6
    digits; Airflow 3 sends both shapes.
    """
    if not isinstance(value, str):
        return None
    match = _TIMESTAMP.fullmatch(value.strip())
    if not match:
        return None
    day, clock, fraction, zone = match.groups()
    micros = (fraction or "").ljust(6, "0")[:6]
    if not zone or zone in ("Z", "z"):
        zone = "+00:00"
    elif ":" not in zone:
        zone = f"{zone[:3]}:{zone[3:]}"
    try:
        return datetime.fromisoformat(f"{day}T{clock}.{micros}{zone}").astimezone(timezone.utc)
    except (ValueError, OverflowError):  # a day out of range, or a year 1/9999 offset
        return None


def _path_matches(name: str, rel: str) -> bool:
    """Whether Airflow's file name (absolute, or relative to its bundle) is ``rel``."""
    name = name.replace("\\", "/")
    return bool(name) and (name == rel or name.endswith("/" + rel))


def _text(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value else None


@dataclass(frozen=True)
class DagState:
    """A DAG as Airflow reports it, on either major."""

    dag_id: str
    active: bool
    has_import_errors: bool
    last_parsed_time: Optional[datetime]
    fileloc: str = ""
    relative_fileloc: str = ""
    bundle_name: Optional[str] = None
    bundle_version: Optional[str] = None
    #: Airflow 2 reads a DAG file's stored source by this token.
    file_token: str = ""

    @classmethod
    def from_api(cls, doc: Dict[str, Any], dialect: str) -> "DagState":
        if dialect == "v2":
            active = not bool(doc.get("is_stale"))
        else:
            active = bool(doc.get("is_active", True))
        return cls(
            dag_id=str(doc.get("dag_id") or ""),
            active=active,
            has_import_errors=bool(doc.get("has_import_errors")),
            last_parsed_time=parse_timestamp(doc.get("last_parsed_time")),
            fileloc=str(doc.get("fileloc") or ""),
            relative_fileloc=str(doc.get("relative_fileloc") or ""),
            bundle_name=_text(doc.get("bundle_name")) if dialect == "v2" else None,
            bundle_version=_text(doc.get("bundle_version")) if dialect == "v2" else None,
            file_token=str(doc.get("file_token") or ""),
        )

    def path_matches(self, rel: str) -> bool:
        return _path_matches(self.relative_fileloc, rel) or _path_matches(self.fileloc, rel)


@dataclass(frozen=True)
class ImportErrorRecord:
    """One import error: Airflow 2 names the file absolutely, Airflow 3.4 by its bundle path."""

    filename: str
    timestamp: Optional[datetime]
    stack_trace: str
    bundle_name: Optional[str] = None

    @classmethod
    def from_api(cls, doc: Dict[str, Any], dialect: str) -> "ImportErrorRecord":
        return cls(
            filename=str(doc.get("filename") or ""),
            timestamp=parse_timestamp(doc.get("timestamp")),
            stack_trace=str(doc.get("stack_trace") or ""),
            bundle_name=_text(doc.get("bundle_name")) if dialect == "v2" else None,
        )

    def path_matches(self, rel: str) -> bool:
        return _path_matches(self.filename, rel)


@dataclass(frozen=True)
class BundleState:
    """An Airflow 3 DAG bundle (``/api/v2/dagBundles``, served from 3.4, not by 3.0)."""

    name: str
    version: Optional[str]
    last_refreshed: Optional[datetime]


# ── the client ───────────────────────────────────────────────────────────


class AirflowApi:
    """One Airflow API server, after :meth:`connect` found its major and signed in."""

    def __init__(
        self,
        base_url: str,
        credentials: Credentials,
        *,
        timeout: float = 15.0,
        attempts: int = 4,
        sleep: Callable[[float], None] = time.sleep,
        client: Any = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.credentials = credentials
        self.timeout = timeout
        self.attempts = max(1, attempts)
        self._sleep = sleep
        self._client = client
        self.dialect = ""
        self.version = ""
        self.major = 0
        self._token = credentials.token if credentials.style == "token" else ""
        self._bundles_served: Optional[bool] = None
        self._probed_version = "2"
        self._deadline: Optional[float] = None
        self._clock: Callable[[], float] = time.monotonic

    def set_deadline(self, deadline: Optional[float], clock: Callable[[], float]) -> None:
        """Cap every later request's timeout to the time left until ``deadline``."""
        self._deadline, self._clock = deadline, clock

    @property
    def auth_style(self) -> str:
        return self.credentials.style

    def close(self) -> None:
        if self._client is not None:
            self._client.close()

    # ── connecting ──

    def connect(self, api: str = "auto") -> None:
        """Find the major, sign in and make one authorized read; typed errors otherwise.

        A transient failure is tried again up to ``attempts`` times (1, 2, 4 ... s apart):
        an API server restarting while a deploy runs answers 502/503 for a moment.
        """
        if api not in API_CHOICES:
            raise ValueError(f"api must be one of {API_CHOICES}")
        for attempt in range(self.attempts):
            try:
                self._detect(api)
                self._login()
                # Both reads the wait depends on, with their shape checked: a token that
                # cannot list DAGs, or a proxy that answers {} to everything, is refused
                # before anything is pushed.
                for path, key in (("importErrors", "import_errors"), ("dags", "dags")):
                    doc = self._get_json(f"/api/{self.dialect}/{path}", params={"limit": 1})
                    if not isinstance((doc or {}).get(key), list):
                        raise AirflowApiError(
                            _E_RESPONSE,
                            f"GET /api/{self.dialect}/{path} answered no {key} list: "
                            "is --airflow-url the Airflow API server?",
                        )
                return
            except AirflowApiError as exc:
                if not exc.transient or attempt == self.attempts - 1:
                    raise
                self._sleep(min(2.0**attempt, 8.0))

    def _detect(self, api: str) -> None:
        answers: Dict[str, Any] = {}
        for dialect in ("v2", "v1") if api == "auto" else (api,):
            what = f"GET /api/{dialect}/version"
            resp = self._send("GET", f"/api/{dialect}/version", what=what, basic=dialect == "v1")
            if resp.status_code == 200:
                self._take_version(self._json(resp, what), dialect, what)
                return
            if resp.status_code in (401, 403):
                raise AirflowApiError(
                    _E_AUTH,
                    f"{what} answered {resp.status_code} ({_detail(resp)}): Airflow serves it "
                    "without credentials, so something in front of Airflow wants them.",
                    "Point --airflow-url at the Airflow API server itself, or give "
                    f"{TOKEN_REF_ENV} the token that proxy accepts.",
                )
            answers[dialect] = resp
        if api == "v1":
            gone = _V1_REMOVED in _detail(answers["v1"])
            if gone or self._serves("v2"):
                raise AirflowApiError(
                    _E_MISMATCH,
                    "--airflow-api v1 was asked for, but this is Airflow 3"
                    + (": it answers that /api/v1 has been removed." if gone else "."),
                    "Use --airflow-api v2, or leave it at auto.",
                )
        if api == "v2" and self._serves("v1"):
            raise AirflowApiError(
                _E_MISMATCH,
                f"--airflow-api v2 was asked for, but this is Airflow {self._probed_version}: "
                "it serves /api/v1, not /api/v2.",
                "Use --airflow-api v1, or leave it at auto.",
            )
        tried = " nor ".join(f"GET /api/{d}/version" for d in answers)
        raise AirflowApiError(
            _E_NOT_FOUND,
            f"No Airflow API answered at {_where(self.base_url)}: neither {tried} did "
            f"({', '.join(str(r.status_code) for r in answers.values())}).",
            "Pass the root URL of Airflow's API server (on Airflow 2, the webserver); one "
            "served under a path prefix needs the prefix in --airflow-url.",
        )

    def _serves(self, dialect: str) -> bool:
        what = f"GET /api/{dialect}/version"
        resp = self._send("GET", f"/api/{dialect}/version", what=what, basic=dialect == "v1")
        if resp.status_code != 200:
            return False
        try:
            doc = resp.json()
        except (ValueError, RecursionError):
            return False
        version = doc.get("version") if isinstance(doc, dict) else None
        if isinstance(version, str):
            self._probed_version = version
        return isinstance(version, str)

    def _take_version(self, doc: Dict[str, Any], dialect: str, what: str) -> None:
        version = doc.get("version")
        match = re.match(r"(\d+)\.\d+", version) if isinstance(version, str) else None
        if not match:
            raise AirflowApiError(
                _E_RESPONSE,
                f"{what} answered without an Airflow version: is --airflow-url an Airflow "
                "API server?",
            )
        major = int(match.group(1))
        if major < 2 or (dialect == "v2") != (major >= 3):
            raise AirflowApiError(
                _E_VERSION,
                f"{what} reports Airflow {version}, which forge does not check "
                "(Airflow 2 serves /api/v1, Airflow 3 /api/v2).",
            )
        self.dialect, self.version, self.major = dialect, str(version), major

    def _login(self) -> None:
        if self.credentials.style != "password" or self.dialect != "v2":
            return
        what = "POST /auth/token"
        body = {"username": self.credentials.username, "password": self.credentials.password}
        resp = self._send("POST", "/auth/token", what=what, json_body=body, authorize=False)
        if resp.status_code in (200, 201):
            token = self._json(resp, what).get("access_token")
            if not isinstance(token, str) or not token.strip():
                raise AirflowApiError(_E_RESPONSE, f"{what} answered without an access_token.")
            if _CONTROL.search(token.strip()) or not token.strip().isascii():
                raise AirflowApiError(_E_RESPONSE, f"{what} answered a token no header can carry.")
            self._token = token.strip()
            _register(self._token)
            return
        if resp.status_code == 401:
            raise AirflowApiError(
                _E_AUTH,
                f"Airflow refused the user name and password ({what} answered 401, "
                f"{_detail(resp)}).",
                f"Check the secret {CREDENTIAL_REF_ENV} names. Airflow 3 issues API tokens "
                "from a password for its own users (simple or FAB auth manager); a user who "
                f"signs in through SSO needs a token in {TOKEN_REF_ENV}.",
            )
        if resp.status_code == 404:
            raise AirflowApiError(
                _E_AUTH,
                f"This Airflow serves no {what}: its auth manager does not issue a token "
                "for a password.",
                f"Give forge a token Airflow issued, in {TOKEN_REF_ENV}.",
            )
        raise AirflowApiError(_E_RESPONSE, f"{what} answered {resp.status_code} ({_detail(resp)}).")

    # ── reading ──

    def dag(self, dag_id: str) -> Optional[DagState]:
        """The DAG ``dag_id``, or ``None`` while Airflow lists no such DAG."""
        path = f"/api/{self.dialect}/dags/{quote(dag_id, safe='')}"
        doc = self._get_json(path, allow_404=True)
        return None if doc is None else DagState.from_api(doc, self.dialect)

    def dag_source(self, dag: DagState) -> Optional[str]:
        """The source Airflow stored for ``dag``'s file, or ``None`` when it cannot be read
        (no permission to read DAG code, no file token, nothing stored)."""
        if self.dialect == "v2":
            path = f"/api/v2/dagSources/{quote(dag.dag_id, safe='')}"
        elif dag.file_token:
            path = f"/api/v1/dagSources/{quote(dag.file_token, safe='')}"
        else:
            return None
        try:
            doc = self._get_json(path, allow_404=True)
        except AirflowApiError as exc:
            if exc.event == _E_FORBIDDEN:
                return None
            raise
        content = (doc or {}).get("content")
        return content if isinstance(content, str) else None

    def dags(self) -> List[DagState]:
        """Every DAG Airflow lists (active ones: its own default filter)."""
        rows = self._paged(f"/api/{self.dialect}/dags", "dags")
        return [DagState.from_api(row, self.dialect) for row in rows]

    def import_errors(self) -> List[ImportErrorRecord]:
        """Every import error this user may see (Airflow 3 hides some; see the check)."""
        rows = self._paged(f"/api/{self.dialect}/importErrors", "import_errors")
        return [ImportErrorRecord.from_api(row, self.dialect) for row in rows]

    def bundles(self) -> Optional[List[BundleState]]:
        """The DAG bundles, or ``None`` where Airflow serves no ``/dagBundles``."""
        if self.dialect != "v2" or self._bundles_served is False:
            return None
        doc = self._get_json("/api/v2/dagBundles", params={"limit": _PAGE}, allow_404=True)
        if doc is None:
            self._bundles_served = False
            return None
        self._bundles_served = True
        rows = doc.get("dag_bundles")
        if not isinstance(rows, list):
            raise AirflowApiError(
                _E_RESPONSE, "GET /api/v2/dagBundles answered no dag_bundles list."
            )
        return [
            BundleState(
                name=str(row.get("name") or ""),
                version=_text(row.get("version")),
                last_refreshed=parse_timestamp(row.get("last_refreshed")),
            )
            for row in rows
            if isinstance(row, dict)
        ]

    def _paged(self, path: str, key: str) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        offset = 0
        for _ in range(_MAX_PAGES):
            doc = self._get_json(path, params={"limit": _PAGE, "offset": offset}) or {}
            page = doc.get(key)
            if not isinstance(page, list):
                raise AirflowApiError(_E_RESPONSE, f"GET {path} answered no {key} list.")
            rows.extend(row for row in page if isinstance(row, dict))
            offset += len(page)
            total = doc.get("total_entries")
            if not page or (isinstance(total, int) and offset >= total):
                break
        return rows

    # ── the wire ──

    def _get_json(
        self, path: str, *, params: Optional[Dict[str, Any]] = None, allow_404: bool = False
    ) -> Optional[Dict[str, Any]]:
        what = f"GET {path}"
        renewed = False
        while True:
            resp = self._send("GET", path, what=what, params=params)
            status = resp.status_code
            if status == 200:
                return self._json(resp, what)
            if status == 404 and allow_404:
                return None
            refused = status == 401 or (
                status == 403 and self.dialect == "v2" and _INVALID_JWT in _detail(resp)
            )
            if (
                refused
                and not renewed
                and self.credentials.style == "password"
                and self.dialect == "v2"
            ):
                # An Airflow 3 token lapsed mid-wait: sign in again, once.
                renewed = True
                self._login()
                continue
            if refused:
                raise AirflowApiError(
                    _E_AUTH, f"{what} answered {status} ({_detail(resp)}).", self._auth_fix()
                )
            if status == 403:
                raise AirflowApiError(
                    _E_FORBIDDEN,
                    f"{what} answered 403 ({_detail(resp)}): the user may not read it.",
                    "Give the user or token read access to DAGs and import errors (the Viewer "
                    "role reads both).",
                )
            raise AirflowApiError(_E_RESPONSE, f"{what} answered {status} ({_detail(resp)}).")

    def _auth_fix(self) -> str:
        style = self.credentials.style
        if style == "none":
            return (
                f"Airflow asks for credentials. Set {TOKEN_REF_ENV} (a token) or "
                f"{CREDENTIAL_REF_ENV} (username:password) to a secret reference."
            )
        if style == "password" and self.dialect == "v1":
            return (
                "Check the secret behind "
                f"{CREDENTIAL_REF_ENV}. Airflow 2 takes a password over HTTP Basic only when "
                "[api] auth_backends includes airflow.api.auth.backend.basic_auth."
            )
        return f"Check the secret behind {TOKEN_REF_ENV}: Airflow refused the token (expired?)."

    def _send(
        self,
        method: str,
        path: str,
        *,
        what: str,
        params: Optional[Dict[str, Any]] = None,
        json_body: Any = None,
        authorize: bool = True,
        basic: bool = False,
    ) -> Any:
        import httpx

        if self._client is None:
            self._client = _http_client(self.base_url, self.timeout)
        timeout = self.timeout
        if self._deadline is not None:
            left = self._deadline - self._clock()
            if left <= 0:
                raise AirflowApiError(
                    _E_UNREACHABLE,
                    f"{what} was not sent: --airflow-wait ran out.",
                    transient=True,
                    out_of_time=True,
                )
            timeout = max(min(timeout, left), 0.5)
        headers = {"Accept": "application/json"}
        auth = None
        if authorize and self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        elif authorize and self.credentials.style == "password":
            if self.dialect == "v1" or (not self.dialect and basic):
                auth = httpx.BasicAuth(self.credentials.username, self.credentials.password)
        try:
            resp = self._client.request(
                method,
                self.base_url + path,
                params=params,
                json=json_body,
                headers=headers,
                auth=auth,
                timeout=timeout,
            )
        except httpx.TimeoutException:
            raise AirflowApiError(
                _E_UNREACHABLE,
                f"{what} timed out after {timeout:.0f}s.",
                "Check that Airflow's API server is up and reachable from here.",
                transient=True,
            ) from None
        except UnicodeError:
            # httpx encodes the host with IDNA when it sends (``xn--`` labels it refuses).
            raise AirflowApiError(
                _E_URL,
                f"{what} could not be sent: --airflow-url has a host name IDNA refuses.",
                "Check the host name in --airflow-url.",
            ) from None
        except httpx.HTTPError as exc:
            raise AirflowApiError(
                _E_UNREACHABLE,
                f"{what} failed: {type(exc).__name__} ({_short(str(exc))}).",
                "Check that Airflow's API server is up and reachable from here.",
                transient=True,
            ) from None
        status = resp.status_code
        if 300 <= status < 400:
            raise AirflowApiError(
                _E_REDIRECT,
                f"{what} answered {status}, a redirect to {_where(resp.headers.get('location'))}; "
                "forge does not follow a redirect with credentials.",
                "Pass the URL the redirect points at as --airflow-url (a sign-in page means "
                "the URL is a proxy in front of Airflow).",
            )
        if status == 429 or status >= 500:
            raise AirflowApiError(
                _E_UNREACHABLE,
                f"{what} answered {status} ({_detail(resp)}).",
                "Airflow's API server is busy or restarting; it is tried again.",
                transient=True,
            )
        return resp

    @staticmethod
    def _json(resp: Any, what: str) -> Dict[str, Any]:
        try:
            doc = resp.json()
        except (ValueError, RecursionError):  # not JSON, or nested past the parser's depth
            doc = None
        if not isinstance(doc, dict):
            raise AirflowApiError(
                _E_RESPONSE,
                f"{what} answered {resp.status_code} with a body that is not a JSON object.",
            )
        return doc


def _http_client(base_url: str, timeout: float) -> Any:
    """The repo's HTTP client, which never sends a loopback host through a proxy.

    It honours the proxy environment otherwise, and ``HTTP_PROXY`` with no ``NO_PROXY``
    would send a plain-HTTP request for localhost, credentials included, to the proxy.
    """
    from fluid_build.util.http_client import new_client

    loopback = _loopback(urlsplit(base_url).hostname or "")
    return new_client(timeout=timeout, follow_redirects=False, trust_env=not loopback)


def _short(text: str) -> str:
    from fluid_build.observability.secret_redactor import redact_secret_text

    flat = _CONTROL.sub(" ", redact_secret_text(text)).strip()
    return flat[:_DETAIL_CHARS] + ("..." if len(flat) > _DETAIL_CHARS else "")


def _detail(resp: Any) -> str:
    """What an error answer says: v2 ``detail``, v1 problem ``title``/``detail``, or text."""
    try:
        doc = resp.json()
    except (ValueError, RecursionError):
        doc = None
    if isinstance(doc, dict):
        parts = [doc.get("title"), doc.get("detail"), doc.get("error")]
        said = [str(p) for p in parts if p not in (None, "")]
        if said:
            return _short(": ".join(dict.fromkeys(said)))
    text = getattr(resp, "text", "") or ""
    if "<html" in text.lower():
        return "an HTML page"
    return _short(text) or "no body"
