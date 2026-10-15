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

"""The git credential stays on the endpoint when the upstream redirects.

Real git, real TLS, real ``git http-backend``: a loopback https server that
only redirects, and loopback servers that serve the upstream repository. A
redirect is answered by failing the fetch (``unreachable``), so the header
git was handed for the endpoint never reaches another origin and never
crosses plain http, whatever git configuration the operator already has.

The positive control proves the harness: the same TLS server, used as the
endpoint itself, serves the clone and receives the credential. Without it
a TLS or protocol failure would make the redirect tests pass vacuously.
"""

from __future__ import annotations

import base64
import datetime
import http.server
import ipaddress
import os
import shutil
import ssl
import subprocess
import threading
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional, Tuple

import pytest

from fluid_build.forge import federation
from fluid_build.forge.federation import FederatedWorkspace
from tests.forge.test_federation_resilience import (  # noqa: F401 - pytest fixtures
    local_cache,
    upstream_repo,
)

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(shutil.which("git") is None, reason="git binary not installed"),
]

#: Test value only. Long and distinctive so a substring check is meaningful.
_TOKEN = "fedtest-redirect-not-a-real-credential-51c2"
_REF = "REGISTRY_TOKEN"
_ALLOWLIST_ENV = "FLUID_FEDERATION_SECRET_REF_ALLOWLIST"
_EXPECTED_AUTH = "Basic " + base64.b64encode(f"x-access-token:{_TOKEN}".encode()).decode()


class _Server:
    """A threaded loopback HTTP(S) server that records every request."""

    def __init__(self, respond: Callable[[http.server.BaseHTTPRequestHandler], None]) -> None:
        self.requests: List[Dict[str, object]] = []
        recorded = self.requests

        class Handler(http.server.BaseHTTPRequestHandler):
            def _handle(self) -> None:
                recorded.append(
                    {
                        "method": self.command,
                        "path": self.path,
                        "authorization": self.headers.get("Authorization", ""),
                        "authorizations": self.headers.get_all("Authorization") or [],
                        "headers": [(k.lower(), v) for k, v in self.headers.items()],
                    }
                )
                respond(self)

            do_GET = _handle
            do_POST = _handle

            def log_message(self, *args: object) -> None:  # keep test output quiet
                return None

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def tls(self, context: ssl.SSLContext) -> "_Server":
        self.httpd.socket = context.wrap_socket(self.httpd.socket, server_side=True)
        return self

    def start(self) -> "_Server":
        self._thread.start()
        return self

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def saw_credential(self) -> bool:
        return any(
            _TOKEN in str(r["authorization"]) or r["authorization"] == _EXPECTED_AUTH
            for r in self.requests
        )


def _git_backend(project_root: Path) -> Callable[[http.server.BaseHTTPRequestHandler], None]:
    """Serve ``project_root`` through ``git http-backend`` (smart HTTP, CGI)."""

    def respond(handler: http.server.BaseHTTPRequestHandler) -> None:
        length = int(handler.headers.get("Content-Length") or 0)
        body = handler.rfile.read(length) if length else b""
        path, _, query = handler.path.partition("?")
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(project_root),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_PROJECT_ROOT": str(project_root),
            "GIT_HTTP_EXPORT_ALL": "1",
            "PATH_INFO": path,
            "QUERY_STRING": query,
            "REQUEST_METHOD": handler.command,
            "CONTENT_TYPE": handler.headers.get("Content-Type", ""),
            "CONTENT_LENGTH": str(len(body)),
            "REMOTE_ADDR": "127.0.0.1",
        }
        for header, var in (
            ("Git-Protocol", "GIT_PROTOCOL"),
            ("Content-Encoding", "HTTP_CONTENT_ENCODING"),
        ):
            if handler.headers.get(header):
                env[var] = handler.headers[header]
        out = subprocess.run(
            ["git", "http-backend"], input=body, env=env, capture_output=True, timeout=30
        ).stdout
        head, _, payload = out.partition(b"\r\n\r\n")
        status, headers = 200, []
        for line in head.split(b"\r\n"):
            name, _, value = line.decode("latin-1").partition(":")
            if name.lower() == "status":
                status = int(value.split()[0])
            elif name:
                headers.append((name, value.strip()))
        handler.send_response(status)
        for name, value in headers:
            handler.send_header(name, value)
        handler.send_header("Content-Length", str(len(payload)))
        handler.end_headers()
        handler.wfile.write(payload)

    return respond


def _redirect_to(target: str) -> Callable[[http.server.BaseHTTPRequestHandler], None]:
    def respond(handler: http.server.BaseHTTPRequestHandler) -> None:
        handler.send_response(302)
        handler.send_header("Location", target + handler.path)
        handler.send_header("Content-Length", "0")
        handler.end_headers()

    return respond


def _self_signed(tmp_path: Path) -> tuple[Path, Path]:
    """A loopback certificate valid for ``localhost`` and ``127.0.0.1``."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(hours=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("localhost"),
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                ]
            ),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = tmp_path / "loopback.pem", tmp_path / "loopback.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


@pytest.fixture
def hermetic_git(monkeypatch) -> None:
    """No system, user or inherited config can change redirect handling."""
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    for name in ("GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("GIT_CONFIG_PARAMETERS", raising=False)
    monkeypatch.setenv("FLUID_FEDERATION_TIMEOUT_SECONDS", "20")
    monkeypatch.setenv(_REF, _TOKEN)
    monkeypatch.setenv(_ALLOWLIST_ENV, f"{_REF}@localhost")


@pytest.fixture
def tls_context(tmp_path: Path, monkeypatch, hermetic_git: None) -> ssl.SSLContext:
    """Server-side TLS context; git trusts its certificate through the env."""
    cert_path, key_path = _self_signed(tmp_path)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    monkeypatch.setenv("GIT_SSL_CAINFO", str(cert_path))
    return context


@pytest.fixture
def servers() -> Iterator[List[_Server]]:
    started: List[_Server] = []
    yield started
    for server in started:
        server.stop()


def _start(
    servers: List[_Server],
    respond: Callable[[http.server.BaseHTTPRequestHandler], None],
    context: Optional[ssl.SSLContext],
) -> _Server:
    server = _Server(respond)
    if context is not None:
        server.tls(context)
    servers.append(server.start())
    return server


def _workspace(endpoint: str) -> FederatedWorkspace:
    return FederatedWorkspace(
        id="redir",
        kind="git_registry",
        endpoint=endpoint,
        auth_mode="github_token",
        auth_secret_ref=_REF,
    )


def test_endpoint_origin_receives_the_credential_and_serves_the_clone(
    upstream_repo: Path, local_cache: Path, tls_context: ssl.SSLContext, servers: List[_Server]
):
    """Positive control: TLS, the CGI bridge and the header all work."""
    backend = _start(servers, _git_backend(upstream_repo.parent), tls_context)
    ws = _workspace(f"https://localhost:{backend.port}/{upstream_repo.name}")

    text = federation._git_read_contract(ws, "telco.orders")

    assert text == (upstream_repo / "telco.orders" / "contract.fluid.yaml").read_text()
    assert backend.saw_credential(), backend.requests
    assert any(r["method"] == "POST" for r in backend.requests), backend.requests


@pytest.mark.parametrize("target", ["https-other-origin", "http-downgrade"])
def test_redirect_never_carries_the_credential_to_the_target(
    target: str,
    upstream_repo: Path,
    local_cache: Path,
    tls_context: ssl.SSLContext,
    servers: List[_Server],
):
    if target == "https-other-origin":
        backend = _start(servers, _git_backend(upstream_repo.parent), tls_context)
        location = f"https://127.0.0.1:{backend.port}"
    else:
        backend = _start(servers, _git_backend(upstream_repo.parent), None)
        location = f"http://localhost:{backend.port}"
    redirector = _start(servers, _redirect_to(location), tls_context)
    ws = _workspace(f"https://localhost:{redirector.port}/{upstream_repo.name}")

    text = federation._git_read_contract(ws, "telco.orders")

    assert redirector.requests, "git must have reached the endpoint over TLS"
    assert redirector.saw_credential(), "the endpoint origin is sent the credential"
    assert not backend.saw_credential(), backend.requests
    assert text is None, "a redirected authenticated fetch is reported unreachable"


def test_refresh_redirect_never_carries_the_credential_to_the_target(
    upstream_repo: Path, local_cache: Path, tls_context: ssl.SSLContext, servers: List[_Server]
):
    """The refresh (``git fetch`` on an existing cache) takes the same path."""
    backend = _start(servers, _git_backend(upstream_repo.parent), tls_context)
    ws = _workspace(f"https://localhost:{backend.port}/{upstream_repo.name}")
    assert federation._git_read_contract(ws, "telco.orders")

    other = _start(servers, _git_backend(upstream_repo.parent), tls_context)
    redirector = _start(servers, _redirect_to(f"https://127.0.0.1:{other.port}"), tls_context)
    moved = _workspace(f"https://localhost:{redirector.port}/{upstream_repo.name}")

    assert federation._git_read_contract(moved, "telco.orders") is None
    assert redirector.saw_credential()
    assert not other.saw_credential(), other.requests


#: Ways an operator can have redirects turned on for the endpoint: where the
#: setting lives, and which ``http.<url>`` key it is under. git uses the key
#: that matches the remote URL most specifically -- host, then the length of
#: the matched path -- and the last one read among equals (git-config(1),
#: ``http.<url>.*``). ``repository//`` is the most specific key git 2.47
#: matches: it scores the path with one trailing ``/`` more than the URL.
_REDIRECTS_ON: List[Tuple[str, str]] = [
    ("file", "bare"),
    ("file", "origin"),
    ("file", "repository"),
    ("file", "repository/"),
    ("file", "repository//"),
    ("count", "repository"),
    ("parameters", "origin"),
    ("parameters", "repository"),
    ("parameters", "repository//"),
]
_REDIRECTS_ON_IDS = [f"{source}-{scope}" for source, scope in _REDIRECTS_ON]


def _operator_config(
    source: str, scope: str, endpoint: str, name: str, value: str, tmp_path: Path, monkeypatch
) -> None:
    """Set ``http[.<url>].<name> = value`` the way an operator might: in their
    global config file, as a GIT_CONFIG_COUNT pair, or in GIT_CONFIG_PARAMETERS."""
    origin = endpoint[: endpoint.index("/", len("https://")) + 1]
    url = {
        "bare": None,
        "origin": origin,
        "repository": endpoint,
        "repository/": endpoint + "/",
        "repository//": endpoint + "//",
    }[scope]
    key = f"http.{name}" if url is None else f"http.{url}.{name}"
    if source == "file":
        section = "[http]" if url is None else f'[http "{url}"]'
        config = tmp_path / "operator.gitconfig"
        config.write_text(f"{section}\n\t{name} = {value}\n", encoding="utf-8")
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    elif source == "count":
        monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
        monkeypatch.setenv("GIT_CONFIG_KEY_0", key)
        monkeypatch.setenv("GIT_CONFIG_VALUE_0", value)
    else:
        monkeypatch.setenv("GIT_CONFIG_PARAMETERS", f"'{key}'='{value}'")


def _turn_redirects_on(source: str, scope: str, endpoint: str, tmp_path: Path, monkeypatch) -> None:
    _operator_config(source, scope, endpoint, "followRedirects", "true", tmp_path, monkeypatch)


def _follow_redirects_for(url: str, env: Dict[str, str], cwd: Path) -> str:
    """``http.followRedirects`` as git resolves it for ``url`` (the matcher
    git's http.c uses, through ``git config --get-urlmatch``)."""
    return subprocess.run(
        ["git", "config", "--get-urlmatch", "http.followRedirects", url],
        env=env,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()


@pytest.mark.parametrize("source, scope", _REDIRECTS_ON, ids=_REDIRECTS_ON_IDS)
def test_git_resolves_follow_redirects_to_false_for_the_endpoint(
    source: str, scope: str, tmp_path: Path, hermetic_git: None, monkeypatch
):
    endpoint = "https://localhost:8443/acme/mesh.git"
    _turn_redirects_on(source, scope, endpoint, tmp_path, monkeypatch)
    # remote-curl matches the remote URL with a trailing slash added.
    url = endpoint + "/"
    before = _follow_redirects_for(url, dict(os.environ), tmp_path)
    assert before == "true", "the operator's setting is in force for the endpoint"

    env = {**os.environ, **federation._git_auth_env(_workspace(endpoint))}
    assert _follow_redirects_for(url, env, tmp_path) == "false"


@pytest.mark.parametrize("target", ["https-other-origin", "http-downgrade"])
@pytest.mark.parametrize("source, scope", _REDIRECTS_ON, ids=_REDIRECTS_ON_IDS)
def test_operator_config_that_follows_redirects_does_not_reopen_the_leak(
    source: str,
    scope: str,
    target: str,
    tmp_path: Path,
    upstream_repo: Path,
    local_cache: Path,
    tls_context: ssl.SSLContext,
    servers: List[_Server],
    monkeypatch,
):
    """An operator's ``followRedirects = true`` -- bare, for the origin or for
    the repository itself, in a config file, ``GIT_CONFIG_COUNT`` or
    ``GIT_CONFIG_PARAMETERS`` -- loses to the setting federation hands git."""
    if target == "https-other-origin":
        backend = _start(servers, _git_backend(upstream_repo.parent), tls_context)
        location = f"https://127.0.0.1:{backend.port}"
    else:
        backend = _start(servers, _git_backend(upstream_repo.parent), None)
        location = f"http://localhost:{backend.port}"
    redirector = _start(servers, _redirect_to(location), tls_context)
    endpoint = f"https://localhost:{redirector.port}/{upstream_repo.name}"
    _turn_redirects_on(source, scope, endpoint, tmp_path, monkeypatch)

    assert federation._git_read_contract(_workspace(endpoint), "telco.orders") is None
    assert redirector.saw_credential()
    assert not backend.saw_credential(), backend.requests


@pytest.mark.parametrize("source, scope", [("parameters", "repository"), ("file", "repository//")])
def test_refresh_redirect_with_operator_config_that_follows_redirects(
    source: str,
    scope: str,
    tmp_path: Path,
    upstream_repo: Path,
    local_cache: Path,
    tls_context: ssl.SSLContext,
    servers: List[_Server],
    monkeypatch,
):
    """The refresh (``git fetch`` on an existing cache) holds against it too."""
    backend = _start(servers, _git_backend(upstream_repo.parent), tls_context)
    ws = _workspace(f"https://localhost:{backend.port}/{upstream_repo.name}")
    assert federation._git_read_contract(ws, "telco.orders")

    other = _start(servers, _git_backend(upstream_repo.parent), tls_context)
    redirector = _start(servers, _redirect_to(f"https://127.0.0.1:{other.port}"), tls_context)
    endpoint = f"https://localhost:{redirector.port}/{upstream_repo.name}"
    _turn_redirects_on(source, scope, endpoint, tmp_path, monkeypatch)

    assert federation._git_read_contract(_workspace(endpoint), "telco.orders") is None
    assert redirector.saw_credential()
    assert not other.saw_credential(), other.requests


#: Where an Authorization extraHeader for the endpoint may already be set:
#: the CI pattern ``git config --global http.https://github.com/.extraheader
#: "AUTHORIZATION: basic ..."`` (private submodules, Go modules), and the
#: other scopes and layers git collects extra headers from.
_HEADER_ALREADY_SET: List[Tuple[str, str]] = [
    ("file", "origin"),
    ("file", "bare"),
    ("file", "repository"),
    ("file", "repository//"),
    ("count", "origin"),
    ("parameters", "repository"),
]


@pytest.mark.parametrize(
    "source, scope", _HEADER_ALREADY_SET, ids=[f"{a}-{b}" for a, b in _HEADER_ALREADY_SET]
)
def test_the_endpoint_gets_exactly_one_authorization_header_and_it_is_ours(
    source: str,
    scope: str,
    tmp_path: Path,
    upstream_repo: Path,
    local_cache: Path,
    tls_context: ssl.SSLContext,
    servers: List[_Server],
    monkeypatch,
):
    """git sends every extraHeader that applies, so an Authorization header the
    operator already configured plus federation's made two, which GitHub
    answers with 400. An empty extraHeader resets git's list (git-config(1),
    ``http.extraHeader``); federation sets one at its own scope before its
    header, so the endpoint sees one Authorization header: federation's."""
    backend = _start(servers, _git_backend(upstream_repo.parent), tls_context)
    endpoint = f"https://localhost:{backend.port}/{upstream_repo.name}"
    stale = "basic " + base64.b64encode(b"x-access-token:operator-fake-value").decode()
    _operator_config(
        source, scope, endpoint, "extraHeader", f"AUTHORIZATION: {stale}", tmp_path, monkeypatch
    )
    expected = (upstream_repo / "telco.orders" / "contract.fluid.yaml").read_text()

    anonymous = FederatedWorkspace(id="anon", kind="git_registry", endpoint=endpoint)
    assert federation._git_read_contract(anonymous, "telco.orders") == expected
    assert backend.requests and all(
        r["authorizations"] == [stale] for r in backend.requests
    ), "the operator's header is in force for the endpoint"

    for attempt in ("clone", "refresh"):
        backend.requests.clear()
        assert federation._git_read_contract(_workspace(endpoint), "telco.orders") == expected
        assert backend.requests, attempt
        assert all(r["authorizations"] == [_EXPECTED_AUTH] for r in backend.requests), (
            attempt,
            backend.requests,
        )


def test_an_insteadof_rewrite_of_the_endpoint_path_is_fetched_without_the_token(
    tmp_path: Path,
    upstream_repo: Path,
    local_cache: Path,
    tls_context: ssl.SSLContext,
    servers: List[_Server],
    monkeypatch,
):
    """The header is keyed to the endpoint URL, so an operator rewrite to
    another path on the same origin is fetched anonymously."""
    backend = _start(servers, _git_backend(upstream_repo.parent), tls_context)
    origin = f"https://localhost:{backend.port}/"
    config = tmp_path / "operator.gitconfig"
    config.write_text(
        f'[url "{origin}{upstream_repo.name}"]\n\tinsteadOf = {origin}moved.git\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))

    text = federation._git_read_contract(_workspace(f"{origin}moved.git"), "telco.orders")

    assert text == (upstream_repo / "telco.orders" / "contract.fluid.yaml").read_text()
    assert backend.requests, "git fetched the rewritten URL"
    assert not backend.saw_credential(), backend.requests


def _gateway(
    header: str, value: str, inner: Callable[[http.server.BaseHTTPRequestHandler], None]
) -> Callable[[http.server.BaseHTTPRequestHandler], None]:
    """An access gateway in front of the git server (Google IAP, Cloudflare
    Access, a corporate WAF): 403 unless the request carries ``header: value``."""

    def respond(handler: http.server.BaseHTTPRequestHandler) -> None:
        if handler.headers.get_all(header) == [value]:
            inner(handler)
            return
        length = int(handler.headers.get("Content-Length") or 0)
        if length:
            handler.rfile.read(length)
        handler.send_response(403)
        handler.send_header("Content-Length", "0")
        handler.end_headers()

    return respond


def _header_values(request: Dict[str, object], name: str) -> List[str]:
    return [v for k, v in request["headers"] if k == name.lower()]  # type: ignore[union-attr]


#: An access gateway's header in the operator's git config: where it is set,
#: the ``http.<url>`` scope it is under, and the header itself.
_GATEWAY_HEADERS: List[Tuple[str, str, str, str]] = [
    ("file", "origin", "Proxy-Authorization", "Bearer gateway-fake-assertion"),
    ("file", "bare", "X-Corp-Client", "fluid-ci"),
    ("file", "repository//", "cf-access-token", "gateway-fake-token"),
    ("count", "repository", "cf-access-token", "gateway-fake-token"),
    ("parameters", "origin", "X-Corp-Client", "fluid-ci"),
]


@pytest.mark.parametrize(
    "source, scope, header, value",
    _GATEWAY_HEADERS,
    ids=[f"{a}-{b}-{c}" for a, b, c, _ in _GATEWAY_HEADERS],
)
def test_a_git_server_behind_an_access_gateway_gets_the_gateway_header_and_ours(
    source: str,
    scope: str,
    header: str,
    value: str,
    tmp_path: Path,
    upstream_repo: Path,
    local_cache: Path,
    tls_context: ssl.SSLContext,
    servers: List[_Server],
    monkeypatch,
):
    """The reset that keeps federation's Authorization header the only one must
    not take the operator's other extra headers with it: a gateway in front of
    the git server answers 403 without its own header."""
    backend = _start(
        servers, _gateway(header, value, _git_backend(upstream_repo.parent)), tls_context
    )
    endpoint = f"https://localhost:{backend.port}/{upstream_repo.name}"
    _operator_config(
        source, scope, endpoint, "extraHeader", f"{header}: {value}", tmp_path, monkeypatch
    )
    expected = (upstream_repo / "telco.orders" / "contract.fluid.yaml").read_text()

    for attempt in ("clone", "refresh"):
        backend.requests.clear()
        text = federation._git_read_contract(_workspace(endpoint), "telco.orders")
        assert text == expected, (attempt, backend.requests)
        assert backend.requests, attempt
        for request in backend.requests:
            assert request["authorizations"] == [_EXPECTED_AUTH], (attempt, request)
            assert _header_values(request, header) == [value], (attempt, request)


def test_a_gateway_header_next_to_a_configured_authorization_header(
    tmp_path: Path,
    upstream_repo: Path,
    local_cache: Path,
    tls_context: ssl.SSLContext,
    servers: List[_Server],
    monkeypatch,
):
    """A CI job's Authorization extraheader and a gateway header for the same
    host: the stale Authorization is replaced by federation's, and the gateway
    header still goes out, once."""
    backend = _start(
        servers,
        _gateway("X-Corp-Client", "fluid-ci", _git_backend(upstream_repo.parent)),
        tls_context,
    )
    origin = f"https://localhost:{backend.port}/"
    endpoint = f"{origin}{upstream_repo.name}"
    stale = "basic " + base64.b64encode(b"x-access-token:operator-fake-value").decode()
    config = tmp_path / "operator.gitconfig"
    config.write_text(
        f'[http "{origin}"]\n'
        f"\textraHeader = AUTHORIZATION: {stale}\n"
        "\textraHeader = X-Corp-Client: fluid-ci\n"
        "[http]\n"
        "\textraHeader = authorization: bearer operator-fake-bare\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))

    for attempt in ("clone", "refresh"):
        backend.requests.clear()
        assert federation._git_read_contract(_workspace(endpoint), "telco.orders"), attempt
        assert backend.requests, attempt
        for request in backend.requests:
            assert request["authorizations"] == [_EXPECTED_AUTH], (attempt, request)
            assert _header_values(request, "X-Corp-Client") == ["fluid-ci"], (attempt, request)


@pytest.mark.parametrize(
    "scope",
    ["other-host", "other-user", "other-repository", "after-a-reset"],
)
def test_extra_headers_that_do_not_apply_to_the_endpoint_are_not_sent(
    scope: str,
    tmp_path: Path,
    upstream_repo: Path,
    local_cache: Path,
    tls_context: ssl.SSLContext,
    servers: List[_Server],
    monkeypatch,
):
    """Federation re-sends only the operator's headers git would have sent to
    the endpoint itself, decided by git's own URL matching."""
    backend = _start(servers, _git_backend(upstream_repo.parent), tls_context)
    origin = f"https://localhost:{backend.port}/"
    endpoint = f"{origin}{upstream_repo.name}"
    elsewhere = "X-Elsewhere: not-for-this-endpoint"
    section = {
        "other-host": '[http "https://other.example/"]',
        "other-user": f'[http "https://someone@localhost:{backend.port}/"]',
        "other-repository": f'[http "{origin}other.git"]',
    }.get(scope)
    if section is not None:
        text = f"{section}\n\textraHeader = {elsewhere}\n"
    else:
        # Cleared by an empty value in a more specific scope.
        text = f"[http]\n\textraHeader = {elsewhere}\n" f'[http "{origin}"]\n\textraHeader =\n'
    config = tmp_path / "operator.gitconfig"
    config.write_text(text, encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))

    assert federation._git_read_contract(_workspace(endpoint), "telco.orders")
    assert backend.requests
    for request in backend.requests:
        assert request["authorizations"] == [_EXPECTED_AUTH], request
        assert _header_values(request, "X-Elsewhere") == [], request


def test_git_is_handed_the_operator_header_after_each_reset(
    tmp_path: Path, hermetic_git: None, monkeypatch
):
    """Each scope federation writes is complete on its own: the reset, the
    operator's other headers for the endpoint, then federation's. Whichever
    of the scopes git applies, it sends the same list."""
    endpoint = "https://localhost:8443/acme/mesh.git"
    _operator_config(
        "file", "origin", endpoint, "extraHeader", "X-Corp-Client: fluid-ci", tmp_path, monkeypatch
    )

    params = federation._git_auth_env(_workspace(endpoint))["GIT_CONFIG_PARAMETERS"]

    ours = "Authorization: " + _EXPECTED_AUTH
    for scope in (endpoint, endpoint + "//"):
        assert (
            f"'http.{scope}.extraHeader'='' "
            f"'http.{scope}.extraHeader'='X-Corp-Client: fluid-ci' "
            f"'http.{scope}.extraHeader'='{ours}' "
            f"'http.{scope}.followRedirects'='false'"
        ) in params


@pytest.mark.parametrize("suffix", ["", "/", "//"])
@pytest.mark.parametrize("target", ["https-other-origin", "http-downgrade"])
def test_a_rewrite_that_adds_a_user_keeps_redirects_off_for_that_user(
    target: str,
    suffix: str,
    tmp_path: Path,
    upstream_repo: Path,
    local_cache: Path,
    tls_context: ssl.SSLContext,
    servers: List[_Server],
    monkeypatch,
):
    """``url.<base>.insteadOf`` that only adds ``user@`` keeps the scheme, host,
    port and path, so the endpoint still gets the token. git ranks an
    ``http.<url>`` key that names the user above one that does not, so the
    operator's ``followRedirects = true`` for that ``user@`` URL would outrank
    federation's user-less setting; federation keys its settings to the
    ``user@`` URL as well."""
    if target == "https-other-origin":
        backend = _start(servers, _git_backend(upstream_repo.parent), tls_context)
        location = f"https://127.0.0.1:{backend.port}"
    else:
        backend = _start(servers, _git_backend(upstream_repo.parent), None)
        location = f"http://localhost:{backend.port}"
    redirector = _start(servers, _redirect_to(location), tls_context)
    origin = f"https://localhost:{redirector.port}/"
    user_origin = f"https://deploy@localhost:{redirector.port}/"
    config = tmp_path / "operator.gitconfig"
    config.write_text(
        f'[url "{user_origin}"]\n\tinsteadOf = {origin}\n'
        f'[http "{user_origin}{upstream_repo.name}{suffix}"]\n\tfollowRedirects = true\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))

    text = federation._git_read_contract(
        _workspace(f"{origin}{upstream_repo.name}"), "telco.orders"
    )

    assert redirector.saw_credential(), "the rewrite keeps the endpoint, so the token goes there"
    assert not backend.saw_credential(), backend.requests
    assert text is None


def test_a_rewrite_that_only_adds_userinfo_still_sends_one_header_ours(
    tmp_path: Path,
    upstream_repo: Path,
    local_cache: Path,
    tls_context: ssl.SSLContext,
    servers: List[_Server],
    monkeypatch,
):
    """The Go-modules pattern: ``url."https://ci-token:<job token>@host/".insteadOf
    "https://host/"`` set for other tools. The rewritten URL is the endpoint
    with userinfo added, so federation's token still authenticates it."""
    backend = _start(servers, _git_backend(upstream_repo.parent), tls_context)
    origin = f"https://localhost:{backend.port}/"
    config = tmp_path / "operator.gitconfig"
    config.write_text(
        f'[url "https://ci-token:fake-job-value@localhost:{backend.port}/"]\n'  # pragma: allowlist secret
        f"\tinsteadOf = {origin}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    endpoint = f"{origin}{upstream_repo.name}"

    for attempt in ("clone", "refresh"):
        backend.requests.clear()
        assert federation._git_read_contract(_workspace(endpoint), "telco.orders"), attempt
        assert backend.requests, attempt
        for request in backend.requests:
            assert request["authorizations"] == [_EXPECTED_AUTH], (attempt, request)


def test_a_rewrite_to_a_path_below_the_endpoint_is_fetched_without_the_token(
    tmp_path: Path,
    upstream_repo: Path,
    local_cache: Path,
    tls_context: ssl.SSLContext,
    servers: List[_Server],
    monkeypatch,
):
    """A key for the endpoint URL also matches every path below it, so the
    rewritten URL is compared with the endpoint itself: another path means
    no token, as the docs say, even when it lies under the endpoint's."""
    backend = _start(servers, _git_backend(upstream_repo.parent), tls_context)
    origin = f"https://localhost:{backend.port}/"
    config = tmp_path / "operator.gitconfig"
    config.write_text(
        f'[url "{origin}{upstream_repo.name}"]\n\tinsteadOf = {origin}\n', encoding="utf-8"
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))

    text = federation._git_read_contract(_workspace(origin), "telco.orders")

    assert text == (upstream_repo / "telco.orders" / "contract.fluid.yaml").read_text()
    assert backend.requests, "git fetched the rewritten URL"
    assert not backend.saw_credential(), backend.requests


#: An operator ``http.extraHeader`` value with a line break in it. git hands
#: curl the value as it is, and curl writes it into the request verbatim
#: (CURLOPT_HTTPHEADER(3): "libcurl passes on the verbatim strings you give
#: it, without any filter or other safe guards"). A server reads a CR LF, or a
#: bare LF (RFC 9112 section 2.2), as the end of a header line, so the text
#: after the break is a header of its own, here a second Authorization
#: header, and a trailing break ends the header block before federation's.
_LINE_BREAK_EVENT = "federation_git_extra_header_line_break"
_LINE_BREAK_VALUES = {
    "crlf": "X-Fgw: gw-7d1e\r\nAuthorization: Basic smuggled-5c0a",
    "lf": "X-Fgw: gw-7d1e\nAuthorization: Basic smuggled-5c0a",
    "cr": "X-Fgw: gw-7d1e\rAuthorization: Basic smuggled-5c0a",
    "trailing-lf": "X-Fgw: gw-7d1e\n",
}


def _line_break_warnings(caplog) -> List[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == "fluid.forge.federation" and r.getMessage().startswith(_LINE_BREAK_EVENT)
    ]


def _authorizations(backend: _Server) -> List[Tuple[str, ...]]:
    return sorted({tuple(r["authorizations"]) for r in backend.requests})  # type: ignore[arg-type]


def _assert_fetched_without_the_token(backend: _Server, endpoint: str) -> None:
    """The fetch is the one git makes without federation: the token is not
    sent, and the server sees the same Authorization headers, if any, as on a
    fetch of a workspace with no credential (git sends the operator's own
    header as it is configured; federation does not rewrite it)."""
    assert federation._git_auth_env(_workspace(endpoint)) == {}
    anonymous = FederatedWorkspace(id="anon", kind="git_registry", endpoint=endpoint)
    backend.requests.clear()
    federation._git_read_contract(anonymous, "telco.orders")
    without_federation = _authorizations(backend)
    for attempt in ("clone", "refresh"):
        backend.requests.clear()
        federation._git_read_contract(_workspace(endpoint), "telco.orders")
        assert backend.requests, f"{attempt}: git still fetches the endpoint, anonymously"
        assert not backend.saw_credential(), (attempt, backend.requests)
        assert _EXPECTED_AUTH.split()[1] not in repr(backend.requests), attempt
        assert _authorizations(backend) == without_federation, (attempt, backend.requests)


@pytest.mark.parametrize("source", ["count", "parameters"])
@pytest.mark.parametrize("value", list(_LINE_BREAK_VALUES.values()), ids=list(_LINE_BREAK_VALUES))
def test_an_operator_header_with_a_line_break_means_the_fetch_carries_no_credential(
    source: str,
    value: str,
    tmp_path: Path,
    upstream_repo: Path,
    local_cache: Path,
    tls_context: ssl.SSLContext,
    servers: List[_Server],
    monkeypatch,
    caplog,
):
    """An authenticated fetch carries one Authorization header, federation's.
    Next to a header line federation cannot see, that does not hold, so the
    fetch is not authenticated: git fetches the endpoint as it would without
    federation, the token is not sent, and the warning names the setting,
    never its value."""
    backend = _start(servers, _git_backend(upstream_repo.parent), tls_context)
    endpoint = f"https://localhost:{backend.port}/{upstream_repo.name}"
    _operator_config(source, "origin", endpoint, "extraHeader", value, tmp_path, monkeypatch)

    with caplog.at_level("WARNING", logger="fluid.forge.federation"):
        _assert_fetched_without_the_token(backend, endpoint)

    warnings = _line_break_warnings(caplog)
    assert warnings, caplog.text
    assert f"http.https://localhost:{backend.port}/.extraHeader" in warnings[0], warnings[0]
    assert "gw-7d1e" not in caplog.text and "smuggled-5c0a" not in caplog.text


def test_a_line_break_written_as_an_escape_in_a_config_file_counts_too(
    tmp_path: Path,
    upstream_repo: Path,
    local_cache: Path,
    tls_context: ssl.SSLContext,
    servers: List[_Server],
    monkeypatch,
    caplog,
):
    """A config file cannot hold a raw line break in a value, but git reads
    ``\\n`` inside a quoted value as one (git-config(1), Syntax)."""
    backend = _start(servers, _git_backend(upstream_repo.parent), tls_context)
    endpoint = f"https://localhost:{backend.port}/{upstream_repo.name}"
    config = tmp_path / "operator.gitconfig"
    config.write_text(
        "[http]\n" '\textraHeader = "X-Fgw: gw-7d1e\\nAuthorization: Basic smuggled-5c0a"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))

    with caplog.at_level("WARNING", logger="fluid.forge.federation"):
        _assert_fetched_without_the_token(backend, endpoint)

    warnings = _line_break_warnings(caplog)
    assert warnings and "http.extraHeader" in warnings[0], caplog.text
    assert "gw-7d1e" not in caplog.text and "smuggled-5c0a" not in caplog.text


@pytest.mark.parametrize("scope", ["other-host", "after-a-reset"])
def test_a_line_break_in_a_header_git_does_not_send_to_the_endpoint_changes_nothing(
    scope: str,
    tmp_path: Path,
    upstream_repo: Path,
    local_cache: Path,
    tls_context: ssl.SSLContext,
    servers: List[_Server],
    monkeypatch,
    caplog,
):
    """Only the headers git would send to the endpoint count: one for another
    host, or one an empty value clears, never reaches curl."""
    backend = _start(servers, _git_backend(upstream_repo.parent), tls_context)
    origin = f"https://localhost:{backend.port}/"
    endpoint = f"{origin}{upstream_repo.name}"
    broken = '"X-Elsewhere: e-1\\nAuthorization: Basic smuggled-5c0a"'
    if scope == "other-host":
        text = f'[http "https://other.example/"]\n\textraHeader = {broken}\n'
    else:
        text = f"[http]\n\textraHeader = {broken}\n" f'[http "{origin}"]\n\textraHeader =\n'
    config = tmp_path / "operator.gitconfig"
    config.write_text(text, encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))

    with caplog.at_level("WARNING", logger="fluid.forge.federation"):
        assert federation._git_read_contract(_workspace(endpoint), "telco.orders")
    assert backend.requests
    for request in backend.requests:
        assert request["authorizations"] == [_EXPECTED_AUTH], request
    assert not _line_break_warnings(caplog), caplog.text


def test_the_line_break_warning_names_the_setting_without_its_userinfo(
    tmp_path: Path, hermetic_git: None, monkeypatch, caplog
):
    """The ``http.<url>`` of the setting can name a user (here added by an
    ``insteadOf`` rewrite), and a user name can itself be a token; the warning
    leaves the userinfo out."""
    endpoint = "https://localhost:8443/acme/mesh.git"
    user_origin = "https://deploy-3e9a@localhost:8443/"
    config = tmp_path / "operator.gitconfig"
    config.write_text(
        f'[url "{user_origin}"]\n\tinsteadOf = https://localhost:8443/\n'
        f'[http "{user_origin}"]\n\textraHeader = "X-Fgw: gw-7d1e\\nX-Other: o"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))

    with caplog.at_level("WARNING", logger="fluid.forge.federation"):
        assert federation._git_auth_env(_workspace(endpoint)) == {}

    warnings = _line_break_warnings(caplog)
    assert len(warnings) == 1, caplog.text
    assert "http.https://localhost:8443/.extraHeader" in warnings[0], warnings[0]
    assert "deploy-3e9a" not in caplog.text and "gw-7d1e" not in caplog.text
