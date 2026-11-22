# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The Airflow REST client of ``fluid schedule-sync``: both API majors, both auth styles.

Every test talks HTTP to a fake Airflow on 127.0.0.1 (``tests/cli/_fake_airflow.py``) whose
answers copy real Airflow 2.10.5 and 3.x servers. Nothing reaches a real Airflow.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

import pytest

from fluid_build.cli import _airflow_api as af
from fluid_build.cli._airflow_api import AirflowApi, AirflowApiError, Credentials
from tests.cli._fake_airflow import FakeAirflow

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _no_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    # The fake is on loopback; a proxy in the developer's environment must not see it.
    monkeypatch.setenv("NO_PROXY", "*")


def _connect(fake: FakeAirflow, creds: Optional[Credentials] = None, api: str = "auto", **kw):
    client = AirflowApi(fake.url, creds or Credentials.none(), sleep=lambda s: None, **kw)
    client.connect(api)
    return client


def _raises(fn, event: str) -> AirflowApiError:
    with pytest.raises(AirflowApiError) as info:
        fn()
    assert info.value.event == event, (info.value.event, info.value.message)
    return info.value


# ── which major, which dialect ──────────────────────────────────────────


@pytest.mark.parametrize("version", ["3.0.6", "3.4.0"])
def test_auto_detects_airflow_3_and_speaks_v2(version: str):
    with FakeAirflow(3, version=version) as fake:
        client = _connect(fake, Credentials.from_password("admin", "admin"))
    assert (client.dialect, client.major, client.version) == ("v2", 3, version)
    assert fake.paths()[0] == "GET /api/v2/version"


def test_auto_detects_airflow_2_and_speaks_v1():
    with FakeAirflow(2) as fake:
        client = _connect(fake, Credentials.from_password("admin", "admin"))
    assert (client.dialect, client.major, client.version) == ("v1", 2, "2.10.5")
    # /api/v2/version is an HTML 404 on Airflow 2, then /api/v1/version answers.
    assert fake.paths()[:2] == ["GET /api/v2/version", "GET /api/v1/version"]


def test_pinning_v1_on_airflow_3_says_v1_is_gone():
    with FakeAirflow(3) as fake:
        error = _raises(lambda: _connect(fake, api="v1"), "schedule_sync_airflow_api_mismatch")
    assert "Airflow 3" in error.message and "--airflow-api v2" in error.fix


def test_pinning_v2_on_airflow_2_says_it_is_airflow_2():
    with FakeAirflow(2) as fake:
        error = _raises(lambda: _connect(fake, api="v2"), "schedule_sync_airflow_api_mismatch")
    assert "--airflow-api v1" in error.fix


def test_a_url_that_serves_no_airflow_api():
    with FakeAirflow(2) as fake:
        fake.answer_next = [(404, {}, "nope"), (404, {}, "nope")]
        error = _raises(lambda: _connect(fake), "schedule_sync_airflow_api_not_found")
    assert "/api/v2/version" in error.message and "/api/v1/version" in error.message


def test_a_version_endpoint_that_is_not_airflow_is_a_bad_response():
    with FakeAirflow(3) as fake:
        fake.answer_next = [(200, {"Content-Type": "application/json"}, '{"name": "other"}')]
        _raises(lambda: _connect(fake), "schedule_sync_airflow_bad_response")


# ── auth: v2 token from /auth/token, v1 Basic, a bearer token on both ──


def test_password_on_airflow_3_logs_in_and_sends_the_jwt():
    with FakeAirflow(3) as fake:
        client = _connect(fake, Credentials.from_password("admin", "admin"))
        client.import_errors()
        login = [r for r in fake.requests if r.path == "/auth/token"]
        read = [r for r in fake.requests if r.path == "/api/v2/importErrors"]
    expected = {"username": "admin", "password": "admin"}  # pragma: allowlist secret
    assert login[0].method == "POST" and login[0].body == expected
    assert read[-1].authorization == "Bearer jwt-1-admin"


def test_password_on_airflow_2_is_http_basic():
    with FakeAirflow(2) as fake:
        client = _connect(fake, Credentials.from_password("admin", "admin"))
        client.import_errors()
    assert not [r for r in fake.requests if r.path == "/auth/token"]
    assert fake.requests[-1].authorization.startswith("Basic ")


@pytest.mark.parametrize("major", [2, 3])
def test_a_token_is_sent_as_a_bearer_on_both_majors(major: int):
    with FakeAirflow(major, static_tokens=("issued-elsewhere",)) as fake:
        client = _connect(fake, Credentials.from_token("issued-elsewhere"))
        client.import_errors()
    assert fake.requests[-1].authorization == "Bearer issued-elsewhere"
    assert client.auth_style == "token"


def test_a_wrong_password_on_airflow_3_names_the_login_endpoint():
    with FakeAirflow(3) as fake:
        error = _raises(
            lambda: _connect(fake, Credentials.from_password("admin", "nope")),
            "schedule_sync_airflow_auth_failed",
        )
    assert "/auth/token" in error.message and "401" in error.message
    assert "nope" not in error.message + error.fix


def test_a_wrong_password_on_airflow_2_is_refused_at_connect():
    with FakeAirflow(2) as fake:
        error = _raises(
            lambda: _connect(fake, Credentials.from_password("admin", "nope")),
            "schedule_sync_airflow_auth_failed",
        )
    assert "basic_auth" in error.fix


def test_no_credentials_where_airflow_wants_them():
    with FakeAirflow(3) as fake:
        error = _raises(lambda: _connect(fake), "schedule_sync_airflow_auth_failed")
    assert af.TOKEN_REF_ENV in error.fix and af.CREDENTIAL_REF_ENV in error.fix


def test_an_invalid_jwt_is_an_auth_failure_not_a_permission_problem():
    with FakeAirflow(3) as fake:
        error = _raises(
            lambda: _connect(fake, Credentials.from_token("stale")),
            "schedule_sync_airflow_auth_failed",
        )
    assert "Invalid JWT token" in error.message


def test_an_expired_jwt_is_renewed_once_with_the_password():
    with FakeAirflow(3) as fake:
        client = _connect(fake, Credentials.from_password("admin", "admin"))
        fake.expire_tokens()
        assert client.import_errors() == []
        logins = [r for r in fake.requests if r.path == "/auth/token"]
    assert len(logins) == 2


@pytest.mark.parametrize("major", [2, 3])
def test_forbidden_is_its_own_error(major: int):
    prefix = "/api/v1/importErrors" if major == 2 else "/api/v2/importErrors"
    with FakeAirflow(major, forbidden=(prefix,)) as fake:
        error = _raises(
            lambda: _connect(fake, Credentials.from_password("admin", "admin")),
            "schedule_sync_airflow_forbidden",
        )
    assert "importErrors" in error.message


# ── transport policy ────────────────────────────────────────────────────


def test_a_redirect_is_refused_and_not_followed():
    with FakeAirflow(3) as fake:
        fake.redirect_to = "https://login.example.org/sso?state=s3cr3t"
        error = _raises(lambda: _connect(fake), "schedule_sync_airflow_redirect")
        assert len(fake.requests) == 1
    assert "login.example.org" in error.message and "s3cr3t" not in error.message


def test_an_unreachable_airflow_is_transient_and_tried_again():
    with FakeAirflow(3) as fake:
        url = fake.url
    client = AirflowApi(url, Credentials.none(), sleep=lambda s: None, attempts=3)
    error = _raises(lambda: client.connect("auto"), "schedule_sync_airflow_unreachable")
    assert error.transient


def test_a_5xx_during_connect_is_tried_again():
    with FakeAirflow(3) as fake:
        fake.answer_next = [(503, {}, "starting")]
        client = _connect(fake, Credentials.from_password("admin", "admin"), attempts=3)
    assert client.dialect == "v2"


@pytest.mark.parametrize(
    "raw, event",
    [
        ("ftp://airflow.example.org", "schedule_sync_airflow_bad_url"),
        (
            "https://user:pw@airflow.example.org",  # pragma: allowlist secret
            "schedule_sync_airflow_bad_url",
        ),
        ("https://airflow.example.org/?token=x", "schedule_sync_airflow_bad_url"),
        ("https://airflow.example.org/#x", "schedule_sync_airflow_bad_url"),
        ("airflow.example.org", "schedule_sync_airflow_bad_url"),
        ("http://airflow.internal:8080", "schedule_sync_airflow_insecure_url"),
        ("http://[::1", "schedule_sync_airflow_bad_url"),
    ],
)
def test_url_policy_refusals(raw: str, event: str):
    error = _raises(lambda: af.normalize_url(raw, environ={}), event)
    assert "pw" not in error.message and "token=x" not in error.message


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("https://airflow.example.org/", "https://airflow.example.org"),
        ("https://airflow.example.org/team-a/", "https://airflow.example.org/team-a"),
        ("http://localhost:8080", "http://localhost:8080"),
        ("http://127.0.0.1:8080/", "http://127.0.0.1:8080"),
        ("http://[::1]:8080", "http://[::1]:8080"),
    ],
)
def test_url_policy_accepts(raw: str, expected: str):
    assert af.normalize_url(raw, environ={}) == expected


def test_plain_http_to_another_host_is_an_explicit_opt_in():
    url = af.normalize_url("http://airflow.internal:8080", environ={af.ALLOW_HTTP_ENV: "true"})
    assert url == "http://airflow.internal:8080"


# ── credentials come from secret references ─────────────────────────────


def test_credential_ref_resolves_to_user_and_password(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AF_PASS", "deploy:pa:ss")
    creds = af.credentials_from_env({af.CREDENTIAL_REF_ENV: "env://AF_PASS"})
    assert (creds.style, creds.username, creds.password) == ("password", "deploy", "pa:ss")
    assert "pa:ss" not in repr(creds)


def test_token_ref_resolves_to_a_bearer(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AF_TOKEN", "  tok-123  ")
    creds = af.credentials_from_env({af.TOKEN_REF_ENV: "env://AF_TOKEN"})
    assert (creds.style, creds.token) == ("token", "tok-123")
    assert "tok-123" not in repr(creds)


def test_resolved_secrets_are_registered_for_redaction(monkeypatch: pytest.MonkeyPatch):
    from fluid_build.observability.secret_redactor import redact_secret_text

    monkeypatch.setenv("AF_TOKEN", "tok-redact-me-9f8e7d")
    af.credentials_from_env({af.TOKEN_REF_ENV: "env://AF_TOKEN"})
    assert "tok-redact-me-9f8e7d" not in redact_secret_text("x tok-redact-me-9f8e7d y")


@pytest.mark.parametrize(
    "environ, needle",
    [
        ({af.TOKEN_REF_ENV: "env://A", af.CREDENTIAL_REF_ENV: "env://B"}, "not both"),
        ({af.CREDENTIAL_REF_ENV: "env://AF_NOCOLON"}, "username:password"),
        ({af.TOKEN_REF_ENV: "env://AF_MISSING_VAR_XYZ"}, "did not resolve"),
        ({af.TOKEN_REF_ENV: "env://AF_CTRL"}, "control"),
    ],
)
def test_credential_misconfiguration(monkeypatch: pytest.MonkeyPatch, environ, needle):
    monkeypatch.setenv("AF_NOCOLON", "justatoken")
    monkeypatch.setenv("AF_CTRL", "tok\nInjected: header")
    monkeypatch.delenv("AF_MISSING_VAR_XYZ", raising=False)
    error = _raises(lambda: af.credentials_from_env(environ), "schedule_sync_airflow_credentials")
    assert needle in error.message


def test_no_credential_variables_means_no_credentials():
    assert af.credentials_from_env({}).style == "none"


# ── both dialects read into one shape ───────────────────────────────────


def _dag_and_error(major: int) -> Dict[str, Any]:
    with FakeAirflow(major) as fake:
        fake.parse_ok(
            "sales__dev__orders", "sales__dev/orders.py", version="abc" if major == 3 else None
        )
        fake.parse_fail("sales__dev/broken.py", stack="Traceback\nModuleNotFoundError: nope")
        client = _connect(fake, Credentials.from_password("admin", "admin"))
        return {
            "dag": client.dag("sales__dev__orders"),
            "missing": client.dag("nope__missing"),
            "errors": client.import_errors(),
            "fake_now": fake.now,
        }


@pytest.mark.parametrize("major", [2, 3])
def test_dag_state_and_import_errors_read_the_same_on_both_majors(major: int):
    got = _dag_and_error(major)
    dag = got["dag"]
    assert dag.dag_id == "sales__dev__orders" and dag.active and not dag.has_import_errors
    assert dag.last_parsed_time == got["fake_now"]
    assert dag.path_matches("sales__dev/orders.py")
    assert got["missing"] is None
    (error,) = got["errors"]
    assert error.path_matches("sales__dev/broken.py") and error.timestamp == got["fake_now"]
    assert error.stack_trace.endswith("ModuleNotFoundError: nope")
    if major == 3:
        assert (dag.bundle_name, dag.bundle_version, error.bundle_name) == (
            "dags-folder",
            "abc",
            "dags-folder",
        )
    else:
        assert (dag.bundle_name, dag.bundle_version, error.bundle_name) == (None, None, None)


@pytest.mark.parametrize("version, absolute", [("3.0.6", True), ("3.4.0", False)])
def test_import_error_paths_match_on_both_3_x_lines(version: str, absolute: bool):
    """3.0 names the file absolutely, 3.4 relative to its bundle; both match the pushed path."""
    with FakeAirflow(3, version=version) as fake:
        fake.parse_fail("sales__dev/broken.py")
        client = _connect(fake, Credentials.from_password("admin", "admin"))
        (error,) = client.import_errors()
    assert error.filename.startswith("/") is absolute
    assert error.path_matches("sales__dev/broken.py") and not error.path_matches("broken.py.bak")


def test_a_stale_dag_on_airflow_3_is_not_active():
    with FakeAirflow(3) as fake:
        fake.parse_ok("d1", "p/d1.py")
        fake.parse_fail("p/d1.py")
        client = _connect(fake, Credentials.from_password("admin", "admin"))
        dag = client.dag("d1")
    assert not dag.active and dag.has_import_errors


@pytest.mark.parametrize("major", [2, 3])
def test_import_errors_are_read_across_pages(major: int):
    with FakeAirflow(major, page_cap=2) as fake:
        for i in range(5):
            fake.parse_fail(f"p/f{i}.py")
        client = _connect(fake, Credentials.from_password("admin", "admin"))
        errors = client.import_errors()
    assert sorted(e.filename.rsplit("/", 1)[-1] for e in errors) == [f"f{i}.py" for i in range(5)]


def test_bundles_are_read_where_airflow_serves_them():
    with FakeAirflow(3, bundles_endpoint=True) as fake:
        fake.set_bundle("git-dags", "c0ffee")
        client = _connect(fake, Credentials.from_password("admin", "admin"))
        (bundle,) = client.bundles()
    assert (bundle.name, bundle.version, bundle.last_refreshed) == ("git-dags", "c0ffee", fake.now)


@pytest.mark.parametrize("major, endpoint", [(2, True), (3, False)])
def test_no_bundles_endpoint_is_none(major: int, endpoint: bool):
    with FakeAirflow(major, bundles_endpoint=endpoint) as fake:
        client = _connect(fake, Credentials.from_password("admin", "admin"))
        assert client.bundles() is None


@pytest.mark.parametrize(
    "text, expected",
    [
        ("2026-11-21T22:21:38.986495+00:00", datetime(2026, 11, 21, 22, 21, 38, 986495)),
        ("2026-11-21T22:20:59.091727Z", datetime(2026, 11, 21, 22, 20, 59, 91727)),
        ("2026-11-21T22:20:59Z", datetime(2026, 11, 21, 22, 20, 59)),
        ("2026-11-21T22:20:59.0917Z", datetime(2026, 11, 21, 22, 20, 59, 91700)),
        ("2026-11-21T22:20:59.123456789+01:00", datetime(2026, 11, 21, 21, 20, 59, 123456)),
    ],
)
def test_airflow_timestamps_parse_on_python_3_10(text: str, expected: datetime):
    assert af.parse_timestamp(text) == expected.replace(tzinfo=timezone.utc)


@pytest.mark.parametrize("text", [None, "", "yesterday", "2026-13-01T00:00:00Z", 12])
def test_an_unreadable_timestamp_is_none(text):
    assert af.parse_timestamp(text) is None


# ── what the scan of the first version turned up ────────────────────────


@pytest.mark.parametrize("body", ["[" * 200000, '{"a":' * 100000], ids=["list", "object"])
@pytest.mark.parametrize("major", [2, 3])
def test_a_deeply_nested_body_is_a_bad_response_not_a_crash(major: int, body: str):
    with FakeAirflow(major) as fake:
        client = _connect(fake, Credentials.from_password("admin", "admin"))
        fake.answer_next = [(200, {"Content-Type": "application/json"}, body)]
        _raises(client.import_errors, "schedule_sync_airflow_bad_response")
        fake.answer_next = [(404, {"Content-Type": "application/json"}, body)]
        _raises(client.import_errors, "schedule_sync_airflow_bad_response")


@pytest.mark.parametrize(
    "raw", ["https://airflow..example.org", "https://.example.org", "https://a" + "a" * 70 + ".org"]
)
def test_a_host_name_dns_cannot_hold_is_a_bad_url(raw: str):
    _raises(lambda: af.normalize_url(raw, environ={}), "schedule_sync_airflow_bad_url")


def test_a_host_name_idna_refuses_at_request_time_is_a_bad_url():
    client = AirflowApi("https://xn--.example.org", Credentials.none(), sleep=lambda s: None)
    _raises(lambda: client.connect("auto"), "schedule_sync_airflow_bad_url")


@pytest.mark.parametrize(
    "text", ["0001-01-01T00:00:00+05:00", "9999-12-31T23:59:59-05:00", "0001-01-01T00:00:00Z"]
)
def test_a_timestamp_out_of_range_is_none_not_a_crash(text: str):
    stamp = af.parse_timestamp(text)
    assert stamp is None or stamp.year in (1, 9999)


def test_the_basic_header_form_of_the_password_is_redacted(monkeypatch: pytest.MonkeyPatch):
    import base64

    from fluid_build.observability.secret_redactor import redact_secret_text

    monkeypatch.setenv("AF_PASS", "deploy:s3cret-pw-77")
    af.credentials_from_env({af.CREDENTIAL_REF_ENV: "env://AF_PASS"})
    encoded = base64.b64encode(b"deploy:s3cret-pw-77").decode()
    assert encoded not in redact_secret_text(f"echo: Authorization: Basic {encoded}")
    assert encoded not in redact_secret_text(f"{encoded}")


# ── the source Airflow stored for a file ────────────────────────────────


@pytest.mark.parametrize("major", [2, 3])
def test_the_stored_source_is_read_by_file_token_or_dag_id(major: int):
    with FakeAirflow(major) as fake:
        fake.land("p/a.py", "from airflow.sdk import DAG\nDAG('p__a')\n")
        fake.parse_ok("p__a", "p/a.py")
        fake.parse_ok("p__b", "p/b.py")
        client = _connect(fake, Credentials.from_password("admin", "admin"))
        a, b = client.dag("p__a"), client.dag("p__b")
        assert a is not None and b is not None and a.file_token != b.file_token
        assert client.dag_source(a) == "from airflow.sdk import DAG\nDAG('p__a')\n"
        assert client.dag_source(b) == fake.stored[("dags-folder", "p/b.py")]
        read = [r.path for r in fake.requests if "/dagSources/" in r.path]
    expected = f"/api/v1/dagSources/{a.file_token}" if major == 2 else "/api/v2/dagSources/p__a"
    assert read[0] == expected


@pytest.mark.parametrize("major", [2, 3])
def test_a_source_the_user_may_not_read_is_none(major: int):
    with FakeAirflow(major, forbidden=("/api/v1/dagSources", "/api/v2/dagSources")) as fake:
        fake.parse_ok("p__a", "p/a.py")
        client = _connect(fake, Credentials.from_password("admin", "admin"))
        dag = client.dag("p__a")
        assert dag is not None and client.dag_source(dag) is None


def test_no_stored_source_or_no_file_token_is_none():
    with FakeAirflow(2, store_code=False) as fake:
        fake.parse_ok("p__a", "p/a.py")
        client = _connect(fake, Credentials.from_password("admin", "admin"))
        dag = client.dag("p__a")
        assert dag is not None and client.dag_source(dag) is None
        tokenless = af.DagState("p__a", active=True, has_import_errors=False, last_parsed_time=None)
        assert client.dag_source(tokenless) is None


# ── the preflight reads what the wait reads ─────────────────────────────


@pytest.mark.parametrize(
    "major, path",
    [
        (2, "/api/v1/importErrors"),
        (2, "/api/v1/dags"),
        (3, "/api/v2/importErrors"),
        (3, "/api/v2/dags"),
    ],
)
def test_a_listing_without_its_list_is_refused_at_connect(major: int, path: str):
    """A proxy or another app at --airflow-url answering ``200 {}``: refused before any
    push, not found out in the middle of the wait."""
    with FakeAirflow(major, empty=(path,)) as fake:
        error = _raises(
            lambda: _connect(fake, Credentials.from_password("admin", "admin")),
            "schedule_sync_airflow_bad_response",
        )
    assert path in error.message


# ── a loopback Airflow is never reached through a proxy ─────────────────


class _Proxy:
    """A forward proxy stand-in that records every request it is sent."""

    def __init__(self) -> None:
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        seen = self.seen = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def do_GET(self) -> None:
                seen.append((self.path, self.headers.get("Authorization")))
                self.send_response(502)
                self.send_header("Content-Length", "0")
                self.end_headers()

            do_POST = do_GET

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost"])
@pytest.mark.parametrize("major", [2, 3])
def test_a_loopback_airflow_is_not_sent_through_a_proxy(
    monkeypatch: pytest.MonkeyPatch, major: int, host: str
):
    proxy = _Proxy()
    try:
        for name in ("NO_PROXY", "no_proxy"):
            monkeypatch.delenv(name, raising=False)
        for name in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY"):
            monkeypatch.setenv(name, proxy.url)
        with FakeAirflow(major) as fake:
            url = fake.url.replace("127.0.0.1", host)
            client = AirflowApi(
                url, Credentials.from_password("admin", "admin"), sleep=lambda s: None
            )
            client.connect("auto")
            client.import_errors()
            assert fake.requests
    finally:
        proxy.close()
    assert proxy.seen == []


def test_a_remote_airflow_keeps_the_proxy_from_the_environment():
    """Only loopback skips the environment: a remote Airflow behind a corporate proxy
    still goes through it."""
    remote = af._http_client("https://airflow.example.org", 5.0)
    local = af._http_client("http://127.0.0.1:8080", 5.0)
    try:
        assert remote.trust_env is True and local.trust_env is False
    finally:
        remote.close()
        local.close()
