# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The ``vault://`` resolver: KV v2 reads, token sources, run-end revoke, redaction.

Every request goes through a ``httpx.MockTransport``: ``safe_httpx_client`` is
replaced, and ``socket.connect`` is made to fail, so no test can reach a store.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import ssl
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx
import pytest

from fluid_build.observability import secret_redactor
from fluid_build.observability.secret_redactor import redact_secret_text
from fluid_build.secrets import _vault, secret_run
from fluid_build.secrets._vault import VaultResolver, describe
from fluid_build.secrets.errors import SecretResolveError
from fluid_build.secrets.ref import parse_secret_ref
from fluid_build.secrets.resolve import resolve_secret

ADDR = "https://vault.example:8200"
SECRET_VALUE = "correct-horse-battery-staple"  # pragma: allowlist secret
OPERATOR_TOKEN = "s.operator-token-0001"
MINTED_TOKEN = "s.minted-token-0002"
WRAPPED_TOKEN = "s.wrapping-token-0003"
JWT = "jwt-material-header.payload.signature-0004"

REF = parse_secret_ref("vault://kv/app/db#password")
KV_DATA = "/v1/kv/data/app/db"
KV_META = "/v1/kv/metadata/app/db"


@dataclass
class Seen:
    method: str
    path: str
    query: Dict[str, str]
    headers: Dict[str, str]
    body: Any


@dataclass
class FakeStore:
    """A scripted Vault: routes by (method, path), a log of what it was asked."""

    requests: List[Seen] = field(default_factory=list)
    client_kwargs: List[Dict[str, Any]] = field(default_factory=list)
    routes: Dict[Tuple[str, str], Callable[[httpx.Request], httpx.Response]] = field(
        default_factory=dict
    )

    def route(
        self,
        method: str,
        path: str,
        status: int = 200,
        body: Any = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> None:
        def answer(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status, json=body, headers=headers)

        self.routes[(method, path)] = answer

    def route_fn(
        self, method: str, path: str, fn: Callable[[httpx.Request], httpx.Response]
    ) -> None:
        self.routes[(method, path)] = fn

    def dispatch(self, request: httpx.Request) -> httpx.Response:
        raw = request.content.decode() if request.content else ""
        self.requests.append(
            Seen(
                request.method,
                request.url.path,
                dict(request.url.params),
                {k.lower(): v for k, v in request.headers.items()},
                json.loads(raw) if raw else None,
            )
        )
        fn = self.routes.get((request.method, request.url.path))
        if fn is None:
            return httpx.Response(404, json={"errors": []})
        return fn(request)

    def paths(self, method: Optional[str] = None) -> List[str]:
        return [r.path for r in self.requests if method is None or r.method == method]

    def seen(self, method: str, path: str) -> List[Seen]:
        return [r for r in self.requests if r.method == method and r.path == path]


def kv_body(**fields: Any) -> Dict[str, Any]:
    return {"data": {"data": fields, "metadata": {"version": 1}}}


@pytest.fixture(autouse=True)
def _clean_redactor():
    secret_redactor.forget_known_secrets()
    yield
    secret_redactor.forget_known_secrets()


@pytest.fixture
def store(monkeypatch):
    fake = FakeStore()

    def no_network(self, *args, **kwargs):
        raise AssertionError("a test tried to open a real socket")

    monkeypatch.setattr(socket.socket, "connect", no_network)

    def stub_client(base_url="", **kwargs):
        fake.client_kwargs.append({"base_url": base_url, **kwargs})
        return httpx.Client(
            base_url=base_url,
            transport=httpx.MockTransport(fake.dispatch),
            headers=kwargs.get("headers"),
            follow_redirects=False,
        )

    monkeypatch.setattr("fluid_build.util.safe_http.safe_httpx_client", stub_client)
    yield fake
    # Every request that was made went through the stub, and with the pinned options.
    for kwargs in fake.client_kwargs:
        assert kwargs["follow_redirects"] is False
        assert kwargs["allow_private"] is True
        assert kwargs["timeout"] == _vault._TIMEOUT_S


@pytest.fixture
def addr(monkeypatch):
    monkeypatch.setenv("BAO_ADDR", ADDR)
    return ADDR


@pytest.fixture
def token_env(monkeypatch, addr):
    monkeypatch.setenv("BAO_TOKEN", OPERATOR_TOKEN)


def resolve(ref=REF) -> str:
    with secret_run("apply") as run:
        return VaultResolver().resolve(ref, run)


def failure(ref=REF) -> SecretResolveError:
    with pytest.raises(SecretResolveError) as info:
        resolve(ref)
    return info.value


def text_of(exc: SecretResolveError) -> str:
    return " ".join([exc.what, exc.why, exc.fix, json.dumps(exc.extras)])


def serve_kv(store: FakeStore, token: str = OPERATOR_TOKEN, **fields: Any) -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        if request.headers.get("x-vault-token") != token:
            return httpx.Response(403, json={"errors": ["permission denied"]})
        return httpx.Response(200, json=kv_body(**(fields or {"password": SECRET_VALUE})))

    store.route_fn("GET", KV_DATA, answer)


# ── the KV v2 read ───────────────────────────────────────────────────────────


def test_reads_the_field_with_the_request_headers(store, token_env):
    serve_kv(store)
    assert resolve() == SECRET_VALUE
    (call,) = store.requests
    assert (call.method, call.path) == ("GET", KV_DATA)
    assert call.headers["x-vault-token"] == OPERATOR_TOKEN
    assert call.headers["x-vault-request"] == "true"
    assert "x-vault-namespace" not in call.headers
    assert "version" not in call.query
    assert store.client_kwargs[0]["base_url"] == ADDR
    assert store.client_kwargs[0]["verify"] is True


def test_version_is_passed_only_when_pinned(store, token_env):
    serve_kv(store)
    resolve(parse_secret_ref("vault://kv/app/db#password?version=3"))
    assert store.requests[0].query == {"version": "3"}


def test_namespace_header_when_set(store, token_env, monkeypatch):
    monkeypatch.setenv("VAULT_NAMESPACE", "team-a")
    serve_kv(store)
    resolve()
    assert store.requests[0].headers["x-vault-namespace"] == "team-a"


def test_nested_path_is_kept(store, token_env):
    store.route("GET", "/v1/kv/data/team/app/db", body=kv_body(password=SECRET_VALUE))
    assert resolve(parse_secret_ref("vault://kv/team/app/db#password")) == SECRET_VALUE


def test_bao_wins_over_vault_for_address_token_and_namespace(store, monkeypatch):
    monkeypatch.setenv("BAO_ADDR", ADDR)
    monkeypatch.setenv("VAULT_ADDR", "https://other.example:8200")
    monkeypatch.setenv("BAO_TOKEN", OPERATOR_TOKEN)
    monkeypatch.setenv("VAULT_TOKEN", "s.vault-token-ignored")
    monkeypatch.setenv("BAO_NAMESPACE", "bao-ns")
    monkeypatch.setenv("VAULT_NAMESPACE", "vault-ns")
    serve_kv(store)
    assert resolve() == SECRET_VALUE
    assert store.client_kwargs[0]["base_url"] == ADDR
    assert store.requests[0].headers["x-vault-token"] == OPERATOR_TOKEN
    assert store.requests[0].headers["x-vault-namespace"] == "bao-ns"


def test_vault_variables_work_alone(store, monkeypatch):
    monkeypatch.setenv("VAULT_ADDR", ADDR)
    monkeypatch.setenv("VAULT_TOKEN", OPERATOR_TOKEN)
    serve_kv(store)
    assert resolve() == SECRET_VALUE


def test_token_file_is_used_when_no_env_token(store, addr):
    (Path.home() / ".vault-token").write_text(OPERATOR_TOKEN + "\n", encoding="utf-8")
    serve_kv(store)
    assert resolve() == SECRET_VALUE
    assert _vault.auth_method() == "token-helper-file"


def test_env_token_beats_the_token_file(store, token_env):
    (Path.home() / ".vault-token").write_text("s.file-token-ignored", encoding="utf-8")
    serve_kv(store)
    resolve()
    assert store.requests[0].headers["x-vault-token"] == OPERATOR_TOKEN


def test_no_token_is_denied_and_names_bao_login(store, addr):
    exc = failure()
    assert exc.extras["reason"] == "denied"
    assert "bao login" in exc.fix
    assert store.requests == []


# ── the address ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "address",
    [
        "http://vault.example:8200",
        "https://vault.example:8200/ui",
        "https://vault.example:8200/?namespace=x",
        "https://user:pw@vault.example:8200",  # pragma: allowlist secret
        "ftp://vault.example",
        "vault.example:8200",
    ],
)
def test_bad_addresses_are_refused_before_any_request(store, monkeypatch, address):
    monkeypatch.setenv("BAO_ADDR", address)
    monkeypatch.setenv("BAO_TOKEN", OPERATOR_TOKEN)
    exc = failure()
    assert exc.extras["reason"] == "invalid"
    assert "pw" not in text_of(exc).replace("password", "")
    assert store.requests == [] and store.client_kwargs == []


def test_missing_address_is_refused(store, monkeypatch):
    monkeypatch.setenv("BAO_TOKEN", OPERATOR_TOKEN)
    exc = failure()
    assert exc.extras["reason"] == "invalid"
    assert "BAO_ADDR" in exc.fix


@pytest.mark.parametrize("address", ["http://127.0.0.1:8200", "http://localhost:8200"])
def test_plain_http_to_loopback_is_allowed(store, monkeypatch, address):
    monkeypatch.setenv("BAO_ADDR", address)
    monkeypatch.setenv("BAO_TOKEN", OPERATOR_TOKEN)
    serve_kv(store)
    assert resolve() == SECRET_VALUE
    assert store.client_kwargs[0]["base_url"] == address


def test_trailing_slash_on_the_address_is_accepted(store, monkeypatch):
    monkeypatch.setenv("BAO_ADDR", ADDR + "/")
    monkeypatch.setenv("BAO_TOKEN", OPERATOR_TOKEN)
    serve_kv(store)
    assert resolve() == SECRET_VALUE
    assert store.client_kwargs[0]["base_url"] == ADDR


def test_ca_bundle_becomes_an_ssl_context(store, token_env, monkeypatch):
    certifi = pytest.importorskip("certifi")
    monkeypatch.setenv("BAO_CACERT", certifi.where())
    serve_kv(store)
    resolve()
    assert isinstance(store.client_kwargs[0]["verify"], ssl.SSLContext)


def test_unreadable_ca_bundle_is_refused(store, token_env, monkeypatch, tmp_path):
    monkeypatch.setenv("VAULT_CACERT", str(tmp_path / "missing.pem"))
    exc = failure()
    assert exc.extras["reason"] == "invalid"
    assert "CA bundle" in exc.why
    assert store.requests == []


# ── status handling ──────────────────────────────────────────────────────────


def test_404_is_not_found(store, token_env):
    exc = failure()
    assert exc.extras["reason"] == "not_found"
    assert "no secret exists" in exc.why


@pytest.mark.parametrize(
    "metadata, word",
    [({"deletion_time": "2026-01-01T00:00:00Z"}, "deleted"), ({"destroyed": True}, "destroyed")],
)
def test_404_with_metadata_names_the_version_state(store, token_env, metadata, word):
    store.route("GET", KV_DATA, 404, {"data": {"metadata": metadata}, "errors": []})
    exc = failure()
    assert exc.extras["reason"] == "not_found"
    assert word in exc.why


def test_403_is_denied(store, token_env):
    store.route("GET", KV_DATA, 403, {"errors": ["permission denied"]})
    exc = failure()
    assert exc.extras["reason"] == "denied"
    assert "permission denied" in exc.why


@pytest.mark.parametrize("status", [503, 429, 472, 473])
def test_sealed_or_standby_is_unreachable(store, token_env, status):
    store.route("GET", KV_DATA, status, {"errors": ["Vault is sealed"]})
    exc = failure()
    assert exc.extras["reason"] == "unreachable"
    assert "sealed" in exc.why


def test_other_status_is_an_error(store, token_env):
    store.route("GET", KV_DATA, 500, {"errors": ["internal error"]})
    exc = failure()
    assert exc.extras["reason"] == "error"
    assert "500" in exc.why


def test_redirect_is_refused_and_never_followed(store, token_env):
    store.route("GET", KV_DATA, 307, None, {"Location": "https://evil.example/v1/kv/data/app/db"})
    exc = failure()
    assert exc.extras["reason"] == "unreachable"
    assert "redirect" in exc.why
    assert len(store.requests) == 1


def test_200_without_kv_v2_data_is_invalid(store, token_env):
    store.route("GET", KV_DATA, 200, {"data": {"password": SECRET_VALUE}})
    exc = failure()
    assert exc.extras["reason"] == "invalid"
    assert "KV v2" in exc.why
    assert SECRET_VALUE not in text_of(exc)


def test_missing_field_is_not_found(store, token_env):
    store.route("GET", KV_DATA, 200, kv_body(user=SECRET_VALUE))
    exc = failure()
    assert exc.extras["reason"] == "not_found"
    assert SECRET_VALUE not in text_of(exc)


def test_non_string_field_is_invalid(store, token_env):
    store.route("GET", KV_DATA, 200, kv_body(password=12345678))
    exc = failure()
    assert exc.extras["reason"] == "invalid"
    assert "int" in exc.why


@pytest.mark.parametrize(
    "error",
    [httpx.ConnectError("boom"), httpx.ReadTimeout("slow"), httpx.RemoteProtocolError("bad")],
)
def test_transport_errors_are_unreachable(store, token_env, error):
    def boom(request):
        raise error

    store.route_fn("GET", KV_DATA, boom)
    exc = failure()
    assert exc.extras["reason"] == "unreachable"


def test_timeout_message_names_the_timeout(store, token_env):
    def slow(request):
        raise httpx.ReadTimeout("slow", request=request)

    store.route_fn("GET", KV_DATA, slow)
    assert "did not answer" in failure().why


def test_store_errors_text_is_redacted(store, token_env):
    secret_redactor.register_secret("leaked-credential-xyz")
    store.route(
        "GET",
        KV_DATA,
        403,
        {"errors": [f"bad token {OPERATOR_TOKEN} and leaked-credential-xyz", "second"]},
    )
    exc = failure()
    assert "bad token" in exc.why and "second" in exc.why
    assert OPERATOR_TOKEN not in text_of(exc)
    assert "leaked-credential-xyz" not in text_of(exc)


# ── JWT login ────────────────────────────────────────────────────────────────


@pytest.fixture
def jwt_env(monkeypatch, addr, tmp_path):
    path = tmp_path / "jwt"
    path.write_text(JWT + "\n", encoding="utf-8")
    monkeypatch.setenv("FLUID_VAULT_JWT_ROLE", "ci-role")
    monkeypatch.setenv("FLUID_VAULT_JWT_FILE", str(path))


def serve_login(store: FakeStore, mount: str = "jwt") -> None:
    store.route("POST", f"/v1/auth/{mount}/login", 200, {"auth": {"client_token": MINTED_TOKEN}})
    store.route("POST", "/v1/auth/token/revoke-self", 204)


def test_jwt_login_mints_a_token_used_for_the_read(store, jwt_env):
    serve_login(store)
    serve_kv(store, token=MINTED_TOKEN)
    assert resolve() == SECRET_VALUE
    (login,) = store.seen("POST", "/v1/auth/jwt/login")
    assert login.body == {"role": "ci-role", "jwt": JWT}
    assert login.headers.get("x-vault-token") is None
    assert store.seen("GET", KV_DATA)[0].headers["x-vault-token"] == MINTED_TOKEN


def test_jwt_mount_is_configurable(store, jwt_env, monkeypatch):
    monkeypatch.setenv("FLUID_VAULT_JWT_MOUNT", "gitlab")
    serve_login(store, "gitlab")
    serve_kv(store, token=MINTED_TOKEN)
    assert resolve() == SECRET_VALUE
    assert len(store.seen("POST", "/v1/auth/gitlab/login")) == 1


def test_inline_jwt_works(store, addr, monkeypatch):
    monkeypatch.setenv("FLUID_VAULT_JWT_ROLE", "ci-role")
    monkeypatch.setenv("FLUID_VAULT_JWT", JWT)
    serve_login(store)
    serve_kv(store, token=MINTED_TOKEN)
    assert resolve() == SECRET_VALUE
    assert store.seen("POST", "/v1/auth/jwt/login")[0].body["jwt"] == JWT


def test_one_login_and_one_client_per_run_then_revoke_at_exit(store, jwt_env):
    serve_login(store)
    serve_kv(store, token=MINTED_TOKEN)
    with secret_run("apply") as run:
        resolver = VaultResolver()
        assert resolver.resolve(REF, run) == SECRET_VALUE
        assert resolver.resolve(REF, run) == SECRET_VALUE
        assert store.seen("POST", "/v1/auth/token/revoke-self") == []
    assert len(store.seen("POST", "/v1/auth/jwt/login")) == 1
    assert len(store.seen("GET", KV_DATA)) == 2
    assert len(store.client_kwargs) == 1
    (revoke,) = store.seen("POST", "/v1/auth/token/revoke-self")
    assert revoke.headers["x-vault-token"] == MINTED_TOKEN
    assert store.requests[-1] is revoke


def test_refused_login_names_the_role_and_never_the_jwt(store, jwt_env):
    store.route("POST", "/v1/auth/jwt/login", 400, {"errors": ["claim bound_audiences mismatch"]})
    exc = failure()
    assert exc.extras["reason"] == "denied"
    assert "ci-role" in exc.why
    assert "bound_audiences" in exc.why
    assert JWT not in text_of(exc)
    assert store.seen("GET", KV_DATA) == []


def test_jwt_role_without_a_jwt_is_invalid(store, addr, monkeypatch):
    monkeypatch.setenv("FLUID_VAULT_JWT_ROLE", "ci-role")
    exc = failure()
    assert exc.extras["reason"] == "invalid"
    assert store.requests == []


def test_failed_revoke_is_logged_not_raised(store, jwt_env, caplog):
    serve_login(store)
    serve_kv(store, token=MINTED_TOKEN)
    store.route("POST", "/v1/auth/token/revoke-self", 500, {"errors": ["boom"]})
    caplog.set_level(logging.DEBUG)
    assert resolve() == SECRET_VALUE
    assert any("could not revoke" in r.getMessage() for r in caplog.records)
    assert MINTED_TOKEN not in caplog.text


# ── response-wrapped token ───────────────────────────────────────────────────


@pytest.fixture
def wrapped_env(monkeypatch, addr):
    monkeypatch.setenv("FLUID_VAULT_WRAPPED_TOKEN", WRAPPED_TOKEN)


def serve_unwrap(store: FakeStore) -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        if request.headers.get("x-vault-token") != WRAPPED_TOKEN:
            return httpx.Response(400, json={"errors": ["wrapping token is not valid"]})
        return httpx.Response(200, json={"auth": {"client_token": MINTED_TOKEN}})

    store.route_fn("POST", "/v1/sys/wrapping/unwrap", answer)
    store.route("POST", "/v1/auth/token/revoke-self", 204)


def test_unwrap_then_read_then_revoke(store, wrapped_env):
    serve_unwrap(store)
    serve_kv(store, token=MINTED_TOKEN)
    assert resolve() == SECRET_VALUE
    (unwrap,) = store.seen("POST", "/v1/sys/wrapping/unwrap")
    assert unwrap.headers["x-vault-token"] == WRAPPED_TOKEN
    assert store.seen("GET", KV_DATA)[0].headers["x-vault-token"] == MINTED_TOKEN
    (revoke,) = store.seen("POST", "/v1/auth/token/revoke-self")
    assert revoke.headers["x-vault-token"] == MINTED_TOKEN


def test_second_run_gets_already_unwrapped_without_a_request(store, wrapped_env):
    serve_unwrap(store)
    serve_kv(store, token=MINTED_TOKEN)
    resolve()
    before = len(store.requests)
    exc = failure()
    assert "already unwrapped" in exc.why
    assert exc.extras["reason"] == "denied"
    assert len(store.requests) == before
    assert len(store.seen("POST", "/v1/sys/wrapping/unwrap")) == 1


def test_refused_unwrap_says_to_treat_credentials_as_exposed(store, wrapped_env):
    store.route("POST", "/v1/sys/wrapping/unwrap", 400, {"errors": ["wrapping token is not valid"]})
    exc = failure()
    assert exc.extras["reason"] == "denied"
    assert "HTTP 400" in exc.why
    assert "exposed" in exc.why
    assert WRAPPED_TOKEN not in text_of(exc)


def test_wrapped_beats_jwt_beats_env_token(store, wrapped_env, jwt_env, monkeypatch):
    monkeypatch.setenv("BAO_TOKEN", OPERATOR_TOKEN)
    assert _vault.auth_method() == "wrapped-token"
    serve_unwrap(store)
    serve_kv(store, token=MINTED_TOKEN)
    resolve()
    assert store.seen("POST", "/v1/auth/jwt/login") == []
    monkeypatch.delenv("FLUID_VAULT_WRAPPED_TOKEN")
    assert _vault.auth_method() == "jwt"
    serve_login(store)
    resolve()
    assert len(store.seen("POST", "/v1/auth/jwt/login")) == 1
    monkeypatch.delenv("FLUID_VAULT_JWT_ROLE")
    assert _vault.auth_method() == "token:BAO_TOKEN"


# ── the token fluid ship handed this stage ───────────────────────────────────


def test_a_ship_token_comes_first_and_is_never_revoked(store, wrapped_env, jwt_env, monkeypatch):
    monkeypatch.setenv("FLUID_VAULT_SHIP_TOKEN", MINTED_TOKEN)
    monkeypatch.setenv("BAO_TOKEN", OPERATOR_TOKEN)
    assert _vault.auth_method() == "ship-token"
    serve_unwrap(store)
    serve_login(store)
    serve_kv(store, token=MINTED_TOKEN)
    assert resolve() == SECRET_VALUE
    assert resolve() == SECRET_VALUE  # a second run of the same stage uses it too
    assert store.seen("POST", "/v1/sys/wrapping/unwrap") == []
    assert store.seen("POST", "/v1/auth/jwt/login") == []
    assert store.seen("POST", "/v1/auth/token/revoke-self") == []
    assert store.seen("GET", KV_DATA)[0].headers["x-vault-token"] == MINTED_TOKEN
    assert "FLUID_VAULT_SHIP_TOKEN" not in os.environ
    assert MINTED_TOKEN not in redact_secret_text(f"a {MINTED_TOKEN} b")


# ── operator tokens and redaction ────────────────────────────────────────────


def test_operator_tokens_are_not_revoked(store, token_env):
    serve_kv(store)
    resolve()
    assert store.seen("POST", "/v1/auth/token/revoke-self") == []


def test_token_file_token_is_not_revoked(store, addr):
    (Path.home() / ".vault-token").write_text(OPERATOR_TOKEN, encoding="utf-8")
    serve_kv(store)
    resolve()
    assert store.seen("POST", "/v1/auth/token/revoke-self") == []


def test_tokens_and_jwts_are_registered_with_the_redactor(store, jwt_env, monkeypatch):
    serve_login(store)
    serve_kv(store, token=MINTED_TOKEN)
    resolve()
    for secret in (MINTED_TOKEN, JWT):
        assert secret not in redact_secret_text(f"failed with {secret} in it")
    monkeypatch.delenv("FLUID_VAULT_JWT_ROLE")
    monkeypatch.setenv("BAO_TOKEN", OPERATOR_TOKEN)
    serve_kv(store)
    resolve()
    assert OPERATOR_TOKEN not in redact_secret_text(f"x {OPERATOR_TOKEN} y")


def test_wrapping_token_is_registered_with_the_redactor(store, wrapped_env):
    serve_unwrap(store)
    serve_kv(store, token=MINTED_TOKEN)
    resolve()
    assert WRAPPED_TOKEN not in redact_secret_text(f"a {WRAPPED_TOKEN} b")


def test_nothing_secret_reaches_errors_or_debug_logs(store, jwt_env, caplog, monkeypatch):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv("FLUID_SECRET_SCHEMES", "vault")
    serve_login(store)
    serve_kv(store, token=MINTED_TOKEN)
    with secret_run("apply"):
        value = resolve_secret(REF, host="localhost").get_secret_value()
    assert value == SECRET_VALUE
    # a failing read in the same process too
    store.route("GET", KV_DATA, 403, {"errors": [f"token {MINTED_TOKEN} denied"]})
    with secret_run("apply"):
        with pytest.raises(SecretResolveError) as info:
            resolve_secret(REF, host="localhost")
    blob = caplog.text + text_of(info.value)
    for secret in (SECRET_VALUE, MINTED_TOKEN, JWT):
        assert secret not in blob
    assert SECRET_VALUE not in redact_secret_text(f"x {SECRET_VALUE}")


def test_resolve_secret_through_the_public_entry_point(store, token_env, monkeypatch):
    serve_kv(store)
    with secret_run("apply"):
        secret = resolve_secret("openbao://kv/app/db#password", host="localhost")
    assert secret.get_secret_value() == SECRET_VALUE
    assert SECRET_VALUE not in repr(secret)


# ── exists() ─────────────────────────────────────────────────────────────────


def meta(current: int = 3, **versions: Dict[str, Any]) -> Dict[str, Any]:
    table = {"1": {"deletion_time": "", "destroyed": False}}
    table.update({"3": {"deletion_time": "", "destroyed": False}})
    table.update({k.lstrip("v"): v for k, v in versions.items()})
    return {"data": {"current_version": current, "versions": table}}


def exists(ref=REF):
    with secret_run("apply") as run:
        return VaultResolver().exists(ref, run)


def test_exists_current_version(store, token_env):
    store.route("GET", KV_META, 200, meta())
    result = exists()
    assert result.found is True
    assert store.seen("GET", KV_META)[0].headers["x-vault-token"] == OPERATOR_TOKEN
    assert store.seen("GET", KV_DATA) == []


@pytest.mark.parametrize("state", [{"deletion_time": "2026-01-01T00:00:00Z"}, {"destroyed": True}])
def test_pinned_deleted_or_destroyed_version_does_not_exist(store, token_env, state):
    store.route("GET", KV_META, 200, meta(v2=state))
    assert exists(parse_secret_ref("vault://kv/app/db#password?version=2")).found is False


def test_pinned_missing_version_does_not_exist(store, token_env):
    store.route("GET", KV_META, 200, meta())
    assert exists(parse_secret_ref("vault://kv/app/db#password?version=9")).found is False


def test_pinned_existing_version_exists(store, token_env):
    store.route("GET", KV_META, 200, meta())
    assert exists(parse_secret_ref("vault://kv/app/db#password?version=1")).found is True


def test_exists_404_is_false(store, token_env):
    assert exists().found is False


def test_exists_403_is_unknown_with_a_metadata_hint(store, token_env):
    store.route("GET", KV_META, 403, {"errors": ["denied"]})
    result = exists()
    assert result.found is None
    assert "metadata" in result.detail


# ── describe() ───────────────────────────────────────────────────────────────


def test_describe_unconfigured():
    assert describe() == {"configured": False}


def serve_health(store: FakeStore) -> None:
    store.route(
        "GET", "/v1/sys/health", 200, {"initialized": True, "sealed": False, "version": "2.1.0"}
    )


def test_describe_static_token_reports_ttl_and_policies_never_the_token(store, token_env):
    serve_health(store)
    store.route(
        "GET",
        "/v1/auth/token/lookup-self",
        200,
        {"data": {"ttl": 3600, "policies": ["default", "fluid-read"]}},
    )
    out = describe()
    assert out["configured"] is True
    assert out["reachable"] is True
    assert out["auth_method"] == "token:BAO_TOKEN"
    assert out["sealed"] is False and out["version"] == "2.1.0"
    assert out["token_ttl_seconds"] == 3600
    assert out["token_policies"] == ["default", "fluid-read"]
    assert OPERATOR_TOKEN not in json.dumps(out)
    assert store.seen("GET", "/v1/auth/token/lookup-self")[0].headers["x-vault-token"] == (
        OPERATOR_TOKEN
    )


def test_describe_unreachable_store(store, token_env):
    def boom(request):
        raise httpx.ConnectError("down", request=request)

    store.route_fn("GET", "/v1/sys/health", boom)
    out = describe()
    assert out["reachable"] is False
    assert "error" in out


def test_describe_bad_address_reports_the_error(store, monkeypatch):
    monkeypatch.setenv("BAO_ADDR", "http://vault.example:8200")
    out = describe()
    assert out["configured"] is True and "error" in out
    assert store.requests == []


def test_describe_never_unwraps(store, wrapped_env):
    serve_health(store)
    out = describe()
    assert out["auth_method"] == "wrapped-token"
    assert store.paths() == ["/v1/sys/health"]
    assert WRAPPED_TOKEN not in json.dumps(out)


def test_describe_never_logs_in(store, jwt_env):
    serve_health(store)
    out = describe()
    assert out["auth_method"] == "jwt"
    assert store.paths() == ["/v1/sys/health"]
    assert JWT not in json.dumps(out)


def test_lookups_outside_a_stage_unwrap_once_and_revoke_at_exit(store, wrapped_env):
    # The credential chain in a command that is not a pipeline stage: two keys,
    # one process. The single-use wrapped token is unwrapped once, its token is
    # reused, and it is revoked when the process ends, not after each lookup.
    from fluid_build.secrets import resolve_credential, run

    serve_unwrap(store)
    serve_kv(store, token=MINTED_TOKEN, password=SECRET_VALUE, user="app")
    assert resolve_credential("vault://kv/app/db#password").get_secret_value() == SECRET_VALUE
    assert resolve_credential("vault://kv/app/db#user").get_secret_value() == "app"
    assert len(store.seen("POST", "/v1/sys/wrapping/unwrap")) == 1
    assert store.seen("POST", "/v1/auth/token/revoke-self") == []
    run._close_orphan()  # what atexit runs
    (revoke,) = store.seen("POST", "/v1/auth/token/revoke-self")
    assert revoke.headers["x-vault-token"] == MINTED_TOKEN


def test_lookups_outside_a_stage_keep_no_value(store, token_env):
    from fluid_build.secrets import resolve_credential

    serve_kv(store)
    for _ in range(2):
        assert resolve_credential("vault://kv/app/db#password").get_secret_value() == SECRET_VALUE
    assert len(store.seen("GET", KV_DATA)) == 2  # fetched each time: nothing was cached
