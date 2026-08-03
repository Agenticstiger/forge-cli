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

"""Tests for the cryptographic auth strategies — shared-token,
JWT bearer, mTLS-from-proxy. Uses real JWT signing
roundtrip (PyJWT + cryptography, both already in venv) so the
contract isn't just mocked — the validator actually verifies a
freshly-issued token end-to-end.
"""

from __future__ import annotations

import base64
import json
import time
from typing import Any, Dict
from unittest.mock import patch

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from fluid_build.output_ports.mcp.auth import (
    AuthDecision,
    AuthValidator,
    extract_mtls_identity,
)

# ---------------------------------------------------------------------
# Shared-token mode (preserves v0.7.4 behaviour)
# ---------------------------------------------------------------------


def test_shared_token_allows_matching_bearer():
    v = AuthValidator(mode="shared-token", shared_token="s3cret")
    assert v.is_enabled()
    decision = v.validate({"Authorization": "Bearer s3cret"})
    assert decision.allowed is True
    assert decision.identity_kind == "shared-token"


def test_shared_token_rejects_wrong_bearer():
    v = AuthValidator(mode="shared-token", shared_token="s3cret")
    decision = v.validate({"Authorization": "Bearer nope"})
    assert decision.allowed is False
    assert decision.deny_reason == "invalid-bearer-token"


def test_shared_token_rejects_missing_authorization_header():
    v = AuthValidator(mode="shared-token", shared_token="s3cret")
    decision = v.validate({})
    assert decision.allowed is False
    assert decision.deny_reason == "missing-bearer-token"


def test_shared_token_unconfigured_means_disabled():
    v = AuthValidator(mode="shared-token", shared_token=None)
    assert v.is_enabled() is False
    # When disabled, every request is allowed (operator's
    # responsibility to wire mTLS proxy in front).
    assert v.validate({}).allowed is True


# ---------------------------------------------------------------------
# JWT bearer mode — real RSA signing roundtrip
# ---------------------------------------------------------------------


def _generate_keypair():
    """Fresh RSA-2048 keypair for each test. Returns (private_pem,
    public_jwk). We sign tokens with the private key and stub the
    JWKS fetch to return the matching public JWK."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_numbers = key.public_key().public_numbers()
    n = base64.urlsafe_b64encode(public_numbers.n.to_bytes(256, "big")).rstrip(b"=").decode()
    e = base64.urlsafe_b64encode(public_numbers.e.to_bytes(3, "big")).rstrip(b"=").decode()
    jwk = {
        "kty": "RSA",
        "use": "sig",
        "alg": "RS256",
        "kid": "test-key-1",
        "n": n,
        "e": e,
    }
    return private_pem, jwk


def _make_token(private_pem: bytes, claims: Dict[str, Any]) -> str:
    return pyjwt.encode(claims, private_pem, algorithm="RS256", headers={"kid": "test-key-1"})


def _build_jwt_validator(jwk: Dict[str, Any], **overrides) -> AuthValidator:
    v = AuthValidator(
        mode="jwt",
        jwt_issuer="https://issuer.example",
        jwt_audience="fluid-mcp",
        jwt_jwks_url="https://issuer.example/.well-known/jwks.json",
        **overrides,
    )

    # Patch the JWKS fetch so we don't actually hit the network. The
    # PyJWKClient wraps urllib internally; we substitute a fake.
    class _FakeJWKClient:
        def __init__(self, *_, **__):
            self._jwk = jwk

        def get_signing_key_from_jwt(self, token):
            class _Key:
                key = serialization.load_pem_public_key(
                    pyjwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(jwk)).public_bytes(
                        encoding=serialization.Encoding.PEM,
                        format=serialization.PublicFormat.SubjectPublicKeyInfo,
                    )
                )

            return _Key()

    v._cached_jwks_client = lambda url: _FakeJWKClient()
    return v


def test_jwt_validates_signed_token_and_extracts_claims():
    private_pem, jwk = _generate_keypair()
    v = _build_jwt_validator(jwk)
    now = int(time.time())
    token = _make_token(
        private_pem,
        {
            "iss": "https://issuer.example",
            "aud": "fluid-mcp",
            "iat": now,
            "exp": now + 300,
            "sub": "alice@corp.example",
            "model": "claude-haiku-4-5-20251001",
            "use_case": "analysis",
            "tenant_id": "acme",
        },
    )
    decision = v.validate({"Authorization": f"Bearer {token}"})
    assert decision.allowed is True, decision.deny_reason
    assert decision.identity_kind == "jwt"
    # Default claim mapping surfaces sub/model/use_case/tenant_id.
    attrs = dict(decision.caller_attributes)
    assert attrs["sub"] == "alice@corp.example"
    assert attrs["model"] == "claude-haiku-4-5-20251001"
    assert attrs["use_case"] == "analysis"
    assert attrs["tenant_id"] == "acme"


def test_jwt_rejects_expired_token():
    private_pem, jwk = _generate_keypair()
    v = _build_jwt_validator(jwk)
    now = int(time.time())
    token = _make_token(
        private_pem,
        {
            "iss": "https://issuer.example",
            "aud": "fluid-mcp",
            "iat": now - 7200,
            "exp": now - 60,  # expired 1 minute ago
            "sub": "alice",
        },
    )
    decision = v.validate({"Authorization": f"Bearer {token}"})
    assert decision.allowed is False
    assert "Expired" in (decision.deny_reason or "")


def test_jwt_rejects_wrong_audience():
    private_pem, jwk = _generate_keypair()
    v = _build_jwt_validator(jwk)
    now = int(time.time())
    token = _make_token(
        private_pem,
        {
            "iss": "https://issuer.example",
            "aud": "some-other-service",  # not fluid-mcp
            "iat": now,
            "exp": now + 300,
            "sub": "alice",
        },
    )
    decision = v.validate({"Authorization": f"Bearer {token}"})
    assert decision.allowed is False


def test_jwt_rejects_wrong_issuer():
    private_pem, jwk = _generate_keypair()
    v = _build_jwt_validator(jwk)
    now = int(time.time())
    token = _make_token(
        private_pem,
        {
            "iss": "https://attacker.example",  # not the configured issuer
            "aud": "fluid-mcp",
            "iat": now,
            "exp": now + 300,
            "sub": "alice",
        },
    )
    decision = v.validate({"Authorization": f"Bearer {token}"})
    assert decision.allowed is False


def test_jwt_rejects_signature_with_wrong_key():
    """Signed by attacker's key, JWKS only knows our key — must reject."""
    _, our_jwk = _generate_keypair()
    attacker_pem, _ = _generate_keypair()
    v = _build_jwt_validator(our_jwk)
    now = int(time.time())
    token = _make_token(
        attacker_pem,
        {
            "iss": "https://issuer.example",
            "aud": "fluid-mcp",
            "iat": now,
            "exp": now + 300,
            "sub": "alice",
        },
    )
    decision = v.validate({"Authorization": f"Bearer {token}"})
    assert decision.allowed is False


def test_jwt_custom_claim_mapping():
    """Operators can map custom JWT claims to caller_attribute names —
    e.g. an Auth0 namespaced claim ``https://corp/model`` → ``model``."""
    private_pem, jwk = _generate_keypair()
    v = _build_jwt_validator(
        jwk,
        jwt_claim_mappings={
            "sub": "principal",
            "https://corp/model": "model",
            "https://corp/region": "region",
        },
    )
    now = int(time.time())
    token = _make_token(
        private_pem,
        {
            "iss": "https://issuer.example",
            "aud": "fluid-mcp",
            "iat": now,
            "exp": now + 300,
            "sub": "alice",
            "https://corp/model": "claude-haiku",
            "https://corp/region": "us-east",
        },
    )
    decision = v.validate({"Authorization": f"Bearer {token}"})
    assert decision.allowed is True
    attrs = dict(decision.caller_attributes)
    assert attrs["principal"] == "alice"
    assert attrs["model"] == "claude-haiku"
    assert attrs["region"] == "us-east"


# ---------------------------------------------------------------------
# mTLS-from-proxy identity extraction
# ---------------------------------------------------------------------


def test_extract_mtls_identity_reads_proxy_forwarded_headers():
    headers = {
        "X-Client-CN": "CN=alice,O=acme",
        "X-Client-Fingerprint": "deadbeefcafe1234",
        "Authorization": "Bearer xyz",
    }
    attrs = extract_mtls_identity(headers)
    assert attrs == {"client_cn": "CN=alice,O=acme", "client_fingerprint": "deadbeefcafe1234"}


def test_extract_mtls_identity_empty_when_proxy_didnt_forward():
    assert extract_mtls_identity({"Authorization": "Bearer xyz"}) == {}


# ---------------------------------------------------------------------
# from_env() factory contract
# ---------------------------------------------------------------------


def test_from_env_defaults_to_shared_token_with_existing_token_var(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.delenv("FLUID_MCP_AUTH_MODE", raising=False)
    monkeypatch.setenv("FLUID_MCP_AUTH_TOKEN", "tok")
    v = AuthValidator.from_env()
    assert v.mode == "shared-token"
    assert v.shared_token == "tok"
    assert v.is_enabled()


def test_from_env_jwt_mode_pulls_issuer_audience_jwks(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("FLUID_MCP_AUTH_MODE", "jwt")
    monkeypatch.setenv("FLUID_MCP_JWT_ISSUER", "https://issuer.example")
    monkeypatch.setenv("FLUID_MCP_JWT_AUDIENCE", "fluid-mcp")
    monkeypatch.setenv("FLUID_MCP_JWT_JWKS_URL", "https://issuer.example/jwks.json")
    monkeypatch.setenv(
        "FLUID_MCP_JWT_CLAIM_MAPPING",
        "sub=principal,https://fluid/model=model",
    )
    v = AuthValidator.from_env()
    assert v.mode == "jwt"
    assert v.is_enabled()
    assert v.jwt_claim_mappings == {
        "sub": "principal",
        "https://fluid/model": "model",
    }


def test_from_env_unknown_mode_raises():
    with pytest.raises(ValueError, match="unknown FLUID_MCP_AUTH_MODE"):
        AuthValidator(mode="iceberg")
