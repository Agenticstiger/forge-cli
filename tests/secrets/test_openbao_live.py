# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The vault:// resolver against a real OpenBao: the round trip the unit tests mock.

Runs in CI's ``OpenBao round trip`` job (``.github/workflows/ci.yml``), which
starts ``openbao/openbao:2.7.2`` as an in-memory dev server and sets
``FLUID_TEST_OPENBAO_ADDR`` and ``FLUID_TEST_OPENBAO_ROOT_TOKEN``. Anywhere
those are unset the module skips: Docker is CI-only for forge, and a
developer's own ``BAO_ADDR`` is never used, because these tests write to the
store they talk to.

What it proves, each against the live API: a KV v2 read with ``#field`` and
``?version=``; a denied path; existence through the metadata endpoint, a
deleted version included; a JWT login whose token is revoked when the run
ends; a response-wrapped token unwrapped once, revoked at run end, and refused
the second time; ``fluid secrets check --resolve`` reading no value; and the
``fluid doctor`` store report.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import textwrap
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

import pytest

ADDR = os.environ.get("FLUID_TEST_OPENBAO_ADDR", "").strip()
ROOT = os.environ.get("FLUID_TEST_OPENBAO_ROOT_TOKEN", "").strip()

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (ADDR and ROOT),
        reason="needs a live OpenBao (FLUID_TEST_OPENBAO_ADDR / _ROOT_TOKEN, set by CI)",
    ),
]

MOUNT = "fluidci"
POLICY = "fluid-ci-read"
JWT_MOUNT = "jwt-ci"
JWT_ROLE = "fluid-ci"
ISSUER = "https://ci.fluid.example"
AUDIENCE = "fluid-ci"


def _admin() -> Any:
    import httpx

    return httpx.Client(base_url=ADDR, headers={"X-Vault-Token": ROOT}, timeout=10.0)


def _ok(response: Any, *codes: int) -> Dict[str, Any]:
    allowed = codes or (200, 204)
    assert response.status_code in allowed, (response.status_code, response.text[:300])
    return response.json() if response.content else {}


def _token(client: Any, *, wrap_ttl: Optional[str] = None) -> Dict[str, Any]:
    headers = {"X-Vault-Wrap-TTL": wrap_ttl} if wrap_ttl else {}
    return _ok(
        client.post(
            "/v1/auth/token/create",
            json={"policies": [POLICY], "ttl": "5m"},
            headers=headers,
        ),
        200,
    )


def _lookup_status(token: str) -> int:
    import httpx

    with httpx.Client(base_url=ADDR, timeout=10.0) as client:
        return client.get(
            "/v1/auth/token/lookup-self", headers={"X-Vault-Token": token}
        ).status_code


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


@pytest.fixture(scope="module")
def store() -> Iterator[Dict[str, Any]]:
    """A KV v2 mount with one secret in two versions, a read policy, and JWT auth."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    suffix = uuid.uuid4().hex[:8]
    path = f"app-{suffix}/pg"
    v1 = f"canary-one-{uuid.uuid4().hex}"
    v2 = f"canary-two-{uuid.uuid4().hex}"
    with _admin() as client:
        deadline = time.monotonic() + 30
        while True:
            try:
                if client.get("/v1/sys/health").status_code == 200:
                    break
            except Exception:  # noqa: BLE001 - still starting
                pass
            if time.monotonic() > deadline:
                pytest.fail(f"OpenBao at {ADDR} did not become healthy")
            time.sleep(0.5)
        _ok(
            client.post(
                f"/v1/sys/mounts/{MOUNT}", json={"type": "kv", "options": {"version": "2"}}
            ),
            204,
            400,
        )
        # A fresh KV v2 mount upgrades its storage for a moment before it takes writes.
        deadline = time.monotonic() + 20
        while True:
            response = client.post(
                f"/v1/{MOUNT}/data/{path}", json={"data": {"password": v1, "user": "app"}}
            )
            if response.status_code == 200 or time.monotonic() > deadline:
                break
            time.sleep(0.5)
        _ok(response, 200)
        _ok(client.post(f"/v1/{MOUNT}/data/{path}", json={"data": {"password": v2}}), 200)
        policy = (
            f'path "{MOUNT}/data/app-{suffix}/*" {{ capabilities = ["read"] }}\n'
            f'path "{MOUNT}/metadata/app-{suffix}/*" {{ capabilities = ["read"] }}\n'
        )
        _ok(client.put(f"/v1/sys/policies/acl/{POLICY}", json={"policy": policy}), 204)

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        public_pem = (
            key.public_key()
            .public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
            )
            .decode("ascii")
        )
        _ok(client.post(f"/v1/sys/auth/{JWT_MOUNT}", json={"type": "jwt"}), 204, 400)
        _ok(
            client.post(
                f"/v1/auth/{JWT_MOUNT}/config",
                json={"jwt_validation_pubkeys": [public_pem], "bound_issuer": ISSUER},
            ),
            204,
        )
        _ok(
            client.post(
                f"/v1/auth/{JWT_MOUNT}/role/{JWT_ROLE}",
                json={
                    "role_type": "jwt",
                    "user_claim": "sub",
                    "bound_audiences": [AUDIENCE],
                    "token_policies": [POLICY],
                    "token_ttl": "5m",
                },
            ),
            204,
        )

    def sign_jwt() -> str:
        now = int(time.time())
        header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
        claims = _b64url(
            json.dumps(
                {
                    "iss": ISSUER,
                    "sub": "repo:fluid/ci",
                    "aud": AUDIENCE,
                    "iat": now,
                    "nbf": now - 5,
                    "exp": now + 300,
                }
            ).encode()
        )
        signing_input = f"{header}.{claims}".encode("ascii")
        signature = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
        return f"{header}.{claims}.{_b64url(signature)}"

    yield {"path": path, "suffix": suffix, "v1": v1, "v2": v2, "sign_jwt": sign_jwt}


@pytest.fixture
def operator_token(store, monkeypatch) -> str:
    """BAO_ADDR + a policy-scoped BAO_TOKEN, as an operator's shell would have."""
    with _admin() as client:
        token = _token(client)["auth"]["client_token"]
    monkeypatch.setenv("BAO_ADDR", ADDR)
    monkeypatch.setenv("BAO_TOKEN", token)
    return token


def test_kv_v2_round_trip_with_field_and_version(store, operator_token):
    from fluid_build.secrets import resolve_secret, secret_run

    ref = f"vault://{MOUNT}/{store['path']}"
    with secret_run("apply"):
        assert resolve_secret(f"{ref}#password", host="localhost").get_secret_value() == store["v2"]
        pinned = resolve_secret(f"{ref}#password?version=1", host="localhost")
        assert pinned.get_secret_value() == store["v1"]
        alias = resolve_secret(
            f"openbao://{MOUNT}/{store['path']}#user?version=1", host="localhost"
        )
        assert alias.get_secret_value() == "app"


def test_a_path_outside_the_token_policy_is_denied(store, operator_token):
    from fluid_build.secrets import SecretResolveError, resolve_secret, secret_run

    with secret_run("apply"):
        with pytest.raises(SecretResolveError) as caught:
            resolve_secret(f"vault://{MOUNT}/other-{store['suffix']}/x#k", host="localhost")
    assert caught.value.extras["reason"] == "denied"


def test_existence_through_metadata_reads_no_value(store, operator_token):
    from fluid_build.secrets import SecretResolveError, resolve_secret, secret_exists, secret_run

    # A secret of its own, so deleting a version cannot disturb the other tests.
    doomed = f"app-{store['suffix']}/doomed"
    with _admin() as client:
        for word in ("first", "second"):
            body = {"data": {"password": word}}  # pragma: allowlist secret
            _ok(client.post(f"/v1/{MOUNT}/data/{doomed}", json=body), 200)
    ref = f"vault://{MOUNT}/{doomed}#password"
    assert secret_exists(ref).found is True
    assert secret_exists(f"vault://{MOUNT}/app-{store['suffix']}/absent#x").found is False
    with _admin() as client:
        _ok(client.post(f"/v1/{MOUNT}/delete/{doomed}", json={"versions": [1]}), 204)
    assert secret_exists(f"{ref}?version=1").found is False
    with secret_run("apply"):
        with pytest.raises(SecretResolveError) as caught:
            resolve_secret(f"{ref}?version=1", host="localhost")
    assert caught.value.extras["reason"] == "not_found"
    assert "deleted" in caught.value.why


def test_jwt_login_token_is_revoked_when_the_run_ends(store, monkeypatch):
    from fluid_build.secrets import resolve_secret, secret_run

    monkeypatch.setenv("BAO_ADDR", ADDR)
    monkeypatch.setenv("FLUID_VAULT_JWT_ROLE", JWT_ROLE)
    monkeypatch.setenv("FLUID_VAULT_JWT_MOUNT", JWT_MOUNT)
    monkeypatch.setenv("FLUID_VAULT_JWT", store["sign_jwt"]())
    with secret_run("apply") as state:
        value = resolve_secret(f"vault://{MOUNT}/{store['path']}#password", host="localhost")
        assert value.get_secret_value() == store["v2"]
        (minted,) = state.scratch["vault.tokens"].values()
        assert _lookup_status(minted) == 200
    assert _lookup_status(minted) == 403


def test_a_wrapped_token_works_once_and_is_revoked_at_run_end(store, monkeypatch):
    from fluid_build.secrets import SecretResolveError, _vault, resolve_secret, secret_run

    with _admin() as client:
        wrapping = _token(client, wrap_ttl="60s")["wrap_info"]["token"]
    monkeypatch.setenv("BAO_ADDR", ADDR)
    monkeypatch.setenv("FLUID_VAULT_WRAPPED_TOKEN", wrapping)
    with secret_run("apply") as state:
        value = resolve_secret(f"vault://{MOUNT}/{store['path']}#password", host="localhost")
        assert value.get_secret_value() == store["v2"]
        (unwrapped,) = state.scratch["vault.tokens"].values()
    assert _lookup_status(unwrapped) == 403
    # A new process holding the same wrapping token: the store refuses the second unwrap.
    monkeypatch.setattr(_vault, "_UNWRAPPED", set())
    with secret_run("apply"):
        with pytest.raises(SecretResolveError) as caught:
            resolve_secret(f"vault://{MOUNT}/{store['path']}#password", host="localhost")
    assert "exposed" in caught.value.why


def test_secrets_check_resolve_reads_no_value(store, operator_token, tmp_path):
    contract = tmp_path / "contract.fluid.yaml"
    contract.write_text(
        textwrap.dedent(
            f"""\
            fluidVersion: "0.7.7"
            kind: DataProduct
            id: ci.secret_check
            name: Secret check
            builds:
              - id: ingest
                pattern: acquisition
                engine: duckdb
                properties:
                  source:
                    kind: postgres
                    connection:
                      host: localhost
                      port: 5432
                      database: app
                      user: app
                      secretRef: vault://{MOUNT}/{store['path']}#password
            """
        ),
        encoding="utf-8",
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith(("FLUID_VAULT_",))}
    env.update(BAO_ADDR=ADDR, BAO_TOKEN=operator_token, COLUMNS="200")
    fluid = Path(sys.executable).with_name("fluid")
    result = subprocess.run(
        [str(fluid), "secrets", "check", str(contract), "--resolve", "--json"],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["references"][0]["exists"] is True
    for value in (store["v1"], store["v2"]):
        assert value not in result.stdout + result.stderr


def test_doctor_reports_the_store_and_token_ttl(store, operator_token):
    from fluid_build.secrets._vault import describe

    info = describe()
    assert info["configured"] is True
    assert info["reachable"] is True
    assert info["sealed"] is False
    assert info["auth_method"] == "token:BAO_TOKEN"
    assert int(info["token_ttl_seconds"]) > 0
    assert operator_token not in json.dumps(info)
