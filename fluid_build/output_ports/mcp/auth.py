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

"""Cryptographic identity for the Fluid MCP output port.

Three pluggable schemes that the gateway selects via
``FLUID_MCP_AUTH_MODE``:

* ``shared-token`` (default historical behaviour) — symmetric
  bearer token compared with ``hmac.compare_digest``. One secret,
  every client uses the same value. Cheapest to deploy, weakest
  per-client identity.

* ``jwt`` — RFC 7519 JSON Web Token bearer. The gateway validates
  the signature against an issuer's JWKS endpoint (RS256 / ES256 /
  EdDSA), verifies ``iss`` / ``aud`` / ``exp`` / ``nbf``, and maps
  configured claims into ``caller_attributes`` (so
  ``policy.rowFilters`` ``${caller.<attr>}`` placeholders resolve
  cryptographically rather than via self-attestation). Industry-
  standard pattern — works with Auth0, Okta, Keycloak, AWS Cognito,
  Google IAP, Azure AD.

* ``spiffe`` — verify a SPIFFE SVID JWT against a configured trust
  bundle using the canonical
  `py-spiffe <https://github.com/HewlettPackard/py-spiffe>`_
  ``JwtSvid.parse_and_validate`` API. The issuer is a SPIFFE
  authority (``spiffe://<trust-domain>``); the ``sub`` claim is a
  SPIFFE ID URI; the ``aud`` claim is required and matched against
  ``FLUID_MCP_SPIFFE_AUDIENCE``. Pairs with workload-identity
  systems (SPIRE, Tornjak) so the gateway gets the calling
  workload's cryptographic identity, not a human-issued bearer
  token. Requires ``pip install fluid-build[spiffe]``.

mTLS (client-cert-bound identity) is best handled by the reverse
proxy in front of the gateway — see
``examples/mcp-output-port-docker/proxy/`` for templates. This module
exposes :func:`extract_mtls_identity` to read the proxy-forwarded
``X-Client-CN`` / ``X-Client-Fingerprint`` headers so the gateway
can stamp the client cert into the audit event for cryptographic
attribution alongside JWT claims.

Borrowed-not-built per /borrow-before-build:

* `PyJWT <https://pyjwt.readthedocs.io>`_ for token validation
  (already in venv).
* `cryptography <https://cryptography.io>`_ for JWKS key parsing
  (already in venv).
* `httpx <https://www.python-httpx.org>`_ for JWKS fetch (forge-cli
  core dep).
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

_log = logging.getLogger("fluid.output_port.mcp.auth")


@dataclass(frozen=True)
class AuthDecision:
    """Result of validating a request's auth header.

    ``allowed`` is the gate. ``caller_attributes`` populates the
    same map ``policy.rowFilters`` reads — JWT claims show up here
    as e.g. ``{"sub": "alice@corp.example", "model": "claude-haiku",
    "tenant_id": "acme"}`` per the configured claim mapping.
    ``deny_reason`` is set on a denial so the gateway can return a
    typed 401 with a structured error.
    """

    allowed: bool
    caller_attributes: Mapping[str, Any] = field(default_factory=dict)
    deny_reason: Optional[str] = None
    identity_kind: str = "none"
    """``shared-token`` / ``jwt`` / ``spiffe`` / ``mtls`` / ``none`` —
    surfaced on the audit event so operators can see WHICH layer
    authenticated each call."""


class AuthValidator:
    """Strategy pattern for the four auth modes the gateway supports.

    Resolved once at gateway start from ``FLUID_MCP_AUTH_MODE`` and
    related env vars; subsequent ``validate(request_headers)`` calls
    are stateless. JWKS keys are cached in-process with a TTL so
    every JWT validation doesn't hit the issuer's discovery endpoint.
    """

    def __init__(
        self,
        *,
        mode: str = "shared-token",
        shared_token: Optional[str] = None,
        jwt_issuer: Optional[str] = None,
        jwt_audience: Optional[str] = None,
        jwt_jwks_url: Optional[str] = None,
        jwt_algorithms: Sequence[str] = ("RS256", "ES256", "EdDSA"),
        jwt_claim_mappings: Optional[Mapping[str, str]] = None,
        spiffe_trust_domain: Optional[str] = None,
        spiffe_jwks_url: Optional[str] = None,
        spiffe_audience: Optional[str] = None,
        jwks_cache_ttl_seconds: float = 600.0,
    ) -> None:
        if mode not in {"shared-token", "jwt", "spiffe", "none"}:
            raise ValueError(
                f"unknown FLUID_MCP_AUTH_MODE={mode!r}; expected one of "
                "shared-token / jwt / spiffe / none"
            )
        self.mode = mode
        self.shared_token = shared_token
        self.jwt_issuer = jwt_issuer
        self.jwt_audience = jwt_audience
        self.jwt_jwks_url = jwt_jwks_url
        self.jwt_algorithms = tuple(jwt_algorithms)
        # Map JWT claim → caller_attribute name. Operators configure
        # which claims to surface (e.g. {"sub": "principal",
        # "https://fluid/model": "model", "https://fluid/tenant":
        # "tenant_id"}). The defaults cover the most common shapes.
        self.jwt_claim_mappings: Dict[str, str] = dict(
            jwt_claim_mappings
            or {
                "sub": "sub",
                "model": "model",
                "use_case": "use_case",
                "tenant_id": "tenant_id",
            }
        )
        self.spiffe_trust_domain = spiffe_trust_domain
        self.spiffe_jwks_url = spiffe_jwks_url
        self.spiffe_audience = spiffe_audience
        self.jwks_cache_ttl_seconds = jwks_cache_ttl_seconds
        self._jwks_cache: Dict[str, Tuple[float, Any]] = {}
        self._spiffe_bundle_cache: Dict[str, Tuple[float, Any]] = {}

    @classmethod
    def from_env(cls) -> "AuthValidator":
        """Build a validator from ``FLUID_MCP_AUTH_*`` env vars.

        Defaults to the historical ``shared-token`` mode using
        ``FLUID_MCP_AUTH_TOKEN`` so existing deployments keep
        working. Operators upgrade to JWT / SPIFFE by setting
        ``FLUID_MCP_AUTH_MODE=jwt`` and the related issuer / JWKS
        env vars.
        """
        mode = os.environ.get("FLUID_MCP_AUTH_MODE", "shared-token")
        kwargs: Dict[str, Any] = {"mode": mode}
        if mode == "shared-token":
            kwargs["shared_token"] = os.environ.get("FLUID_MCP_AUTH_TOKEN")
        elif mode == "jwt":
            kwargs.update(
                jwt_issuer=os.environ.get("FLUID_MCP_JWT_ISSUER"),
                jwt_audience=os.environ.get("FLUID_MCP_JWT_AUDIENCE"),
                jwt_jwks_url=os.environ.get("FLUID_MCP_JWT_JWKS_URL"),
            )
            algs = os.environ.get("FLUID_MCP_JWT_ALGORITHMS")
            if algs:
                kwargs["jwt_algorithms"] = tuple(s.strip() for s in algs.split(","))
            mapping_env = os.environ.get("FLUID_MCP_JWT_CLAIM_MAPPING")
            if mapping_env:
                # Format: "sub=principal,https://fluid/model=model,…"
                parsed: Dict[str, str] = {}
                for pair in mapping_env.split(","):
                    if "=" not in pair:
                        continue
                    claim, attr = pair.split("=", 1)
                    parsed[claim.strip()] = attr.strip()
                kwargs["jwt_claim_mappings"] = parsed
        elif mode == "spiffe":
            kwargs.update(
                spiffe_trust_domain=os.environ.get("FLUID_MCP_SPIFFE_TRUST_DOMAIN"),
                spiffe_jwks_url=os.environ.get("FLUID_MCP_SPIFFE_JWKS_URL"),
                spiffe_audience=os.environ.get("FLUID_MCP_SPIFFE_AUDIENCE"),
            )
        return cls(**kwargs)

    # ------------------------------------------------------------------
    # Public surface
    # ------------------------------------------------------------------

    def is_enabled(self) -> bool:
        """True when the validator has enough config to enforce auth.

        ``shared-token`` requires ``FLUID_MCP_AUTH_TOKEN``; JWT
        requires issuer + audience + JWKS URL; SPIFFE requires the
        trust-domain + JWKS URL. Missing config means the gateway
        runs WITHOUT auth — surfaced as a loud startup warning.
        """
        if self.mode == "none":
            return False
        if self.mode == "shared-token":
            return bool(self.shared_token)
        if self.mode == "jwt":
            return bool(self.jwt_issuer and self.jwt_audience and self.jwt_jwks_url)
        if self.mode == "spiffe":
            return bool(self.spiffe_trust_domain and self.spiffe_jwks_url and self.spiffe_audience)
        return False

    def validate(self, headers: Mapping[str, str]) -> AuthDecision:
        """Validate a request's headers and return an
        :class:`AuthDecision`. Headers are case-insensitive — pass
        the dict your HTTP framework hands you (Starlette / FastAPI
        normalise lower-case automatically).

        When auth is disabled (``is_enabled()`` == False), every
        request is allowed but the audit trail will record
        ``identity_kind="none"`` so operators can grep for un-authed
        traffic later.
        """
        if not self.is_enabled():
            return AuthDecision(allowed=True, identity_kind="none")
        if self.mode == "shared-token":
            return self._validate_shared_token(headers)
        if self.mode == "jwt":
            return self._validate_jwt(headers)
        if self.mode == "spiffe":
            return self._validate_spiffe(headers)
        return AuthDecision(allowed=False, deny_reason="unknown-auth-mode")

    # ------------------------------------------------------------------
    # Shared-token (existing v0.7.4 behaviour, kept for compatibility)
    # ------------------------------------------------------------------

    def _validate_shared_token(self, headers: Mapping[str, str]) -> AuthDecision:
        token = _bearer_token(headers)
        if not token:
            return AuthDecision(
                allowed=False,
                deny_reason="missing-bearer-token",
                identity_kind="shared-token",
            )
        # Constant-time compare defeats timing-side-channel guesses.
        if not hmac.compare_digest(token, self.shared_token or ""):
            return AuthDecision(
                allowed=False,
                deny_reason="invalid-bearer-token",
                identity_kind="shared-token",
            )
        return AuthDecision(allowed=True, identity_kind="shared-token")

    # ------------------------------------------------------------------
    # JWT — RFC 7519 bearer against an issuer's JWKS
    # ------------------------------------------------------------------

    def _validate_jwt(self, headers: Mapping[str, str]) -> AuthDecision:
        try:
            import jwt as pyjwt  # type: ignore[import-not-found]
        except ImportError:  # pragma: no cover - PyJWT is a forge-cli dep
            return AuthDecision(
                allowed=False,
                deny_reason="pyjwt-not-installed",
                identity_kind="jwt",
            )

        token = _bearer_token(headers)
        if not token:
            return AuthDecision(
                allowed=False,
                deny_reason="missing-bearer-token",
                identity_kind="jwt",
            )

        if not self.jwt_jwks_url:
            return AuthDecision(
                allowed=False,
                deny_reason="jwks-url-not-configured",
                identity_kind="jwt",
            )

        try:
            # PyJWKClient handles JWKS fetch + key rotation; we wrap
            # in our own TTL cache to avoid hitting the discovery
            # endpoint on every single request.
            client = self._cached_jwks_client(self.jwt_jwks_url)
            signing_key = client.get_signing_key_from_jwt(token).key
            claims = pyjwt.decode(
                token,
                signing_key,
                algorithms=list(self.jwt_algorithms),
                issuer=self.jwt_issuer,
                audience=self.jwt_audience,
                options={"require": ["exp", "iat"]},
            )
        except Exception as exc:  # noqa: BLE001
            return AuthDecision(
                allowed=False,
                deny_reason=f"{type(exc).__name__}: {exc}",
                identity_kind="jwt",
            )

        # Map configured claims → caller_attributes.
        attrs: Dict[str, Any] = {}
        for claim_name, attr_name in self.jwt_claim_mappings.items():
            if claim_name in claims:
                attrs[attr_name] = claims[claim_name]
        # Always carry sub for audit attribution even if the operator
        # didn't map it.
        attrs.setdefault("sub", claims.get("sub"))
        return AuthDecision(allowed=True, caller_attributes=attrs, identity_kind="jwt")

    def _cached_jwks_client(self, jwks_url: str):
        from jwt import PyJWKClient  # type: ignore[import-not-found]

        cached = self._jwks_cache.get(jwks_url)
        now = time.monotonic()
        if cached is not None and now - cached[0] < self.jwks_cache_ttl_seconds:
            return cached[1]
        client = PyJWKClient(jwks_url, cache_keys=True, lifespan=int(self.jwks_cache_ttl_seconds))
        self._jwks_cache[jwks_url] = (now, client)
        return client

    # ------------------------------------------------------------------
    # SPIFFE — JWT-SVID validation via py-spiffe (canonical impl)
    # ------------------------------------------------------------------

    def _validate_spiffe(self, headers: Mapping[str, str]) -> AuthDecision:
        try:
            from spiffe import JwtSvid  # type: ignore[import-not-found]
        except ImportError:
            return AuthDecision(
                allowed=False,
                deny_reason="spiffe-package-not-installed; install fluid-build[spiffe]",
                identity_kind="spiffe",
            )

        token = _bearer_token(headers)
        if not token:
            return AuthDecision(
                allowed=False,
                deny_reason="missing-bearer-token",
                identity_kind="spiffe",
            )

        try:
            jwt_bundle = self._cached_spiffe_bundle()
            jwt_svid = JwtSvid.parse_and_validate(
                token,
                jwt_bundle,
                audience={self.spiffe_audience or ""},
            )
        except Exception as exc:  # noqa: BLE001
            return AuthDecision(
                allowed=False,
                deny_reason=f"{type(exc).__name__}: {exc}",
                identity_kind="spiffe",
            )

        # Defense-in-depth: ``parse_and_validate`` checks signature +
        # audience but does NOT match the SVID's trust domain against
        # the bundle's. Reject SVIDs whose ``sub`` claims membership
        # of a trust domain other than the operator-configured one,
        # even if the signing key was somehow shared across domains.
        configured_domain = self.spiffe_trust_domain or ""
        if not str(jwt_svid.spiffe_id).startswith(f"{configured_domain}/"):
            return AuthDecision(
                allowed=False,
                deny_reason="spiffe-sub-not-under-trust-domain",
                identity_kind="spiffe",
            )

        # Map configured claims → caller_attributes. py-spiffe's
        # JwtSvid only exposes ``spiffe_id`` / ``audience`` / ``expiry``
        # / ``token`` publicly — there's no public ``claims`` accessor
        # — so re-decode the token with PyJWT without re-verifying the
        # signature (parse_and_validate already verified above) to
        # surface custom claims (model, tenant_id, etc.) into the same
        # rowFilter placeholder map the JWT mode populates.
        import jwt as pyjwt  # type: ignore[import-not-found]

        claims = pyjwt.decode(jwt_svid.token, options={"verify_signature": False})
        attrs: Dict[str, Any] = {}
        for claim_name, attr_name in self.jwt_claim_mappings.items():
            if claim_name in claims:
                attrs[attr_name] = claims[claim_name]
        # Always carry the SPIFFE ID under ``sub`` for audit
        # attribution. ``jwt_svid.spiffe_id`` is a parsed SpiffeId;
        # str() returns the canonical ``spiffe://`` URI.
        attrs.setdefault("sub", str(jwt_svid.spiffe_id))
        return AuthDecision(allowed=True, caller_attributes=attrs, identity_kind="spiffe")

    def _cached_spiffe_bundle(self):
        """TTL-cached py-spiffe ``JwtBundle`` parsed from the
        configured JWKS endpoint. Mirrors the
        ``_cached_jwks_client`` pattern so the discovery endpoint
        isn't hit on every request. Bundle keys are immutable for
        the cache lifetime; re-fetch on TTL expiry handles SPIRE
        authority rotation."""
        import httpx  # forge-cli core dep
        from spiffe import JwtBundle, TrustDomain  # type: ignore[import-not-found]

        cached = self._spiffe_bundle_cache.get(self.spiffe_jwks_url or "")
        now = time.monotonic()
        if cached is not None and now - cached[0] < self.jwks_cache_ttl_seconds:
            return cached[1]
        response = httpx.get(self.spiffe_jwks_url or "", timeout=5.0)
        response.raise_for_status()
        # ``TrustDomain`` expects the bare authority name, no
        # ``spiffe://`` prefix — strip if the operator configured the
        # URI form (both shapes are accepted in env var input).
        domain_name = self.spiffe_trust_domain or ""
        if domain_name.startswith("spiffe://"):
            domain_name = domain_name[len("spiffe://") :]
        bundle = JwtBundle.parse(TrustDomain(domain_name), response.content)
        self._spiffe_bundle_cache[self.spiffe_jwks_url or ""] = (now, bundle)
        return bundle


def _bearer_token(headers: Mapping[str, str]) -> Optional[str]:
    """Extract the bearer token from an Authorization header,
    case-insensitive on the header name and the ``Bearer`` scheme.
    Returns None when no Authorization header is present or the
    scheme isn't bearer."""
    for key, value in headers.items():
        if key.lower() == "authorization":
            parts = value.split(None, 1)
            if len(parts) == 2 and parts[0].lower() == "bearer":
                return parts[1].strip()
    return None


def extract_mtls_identity(headers: Mapping[str, str]) -> Dict[str, str]:
    """Read mTLS-from-proxy metadata (CN + fingerprint) into the
    caller_attributes shape so the gateway can stamp the client
    certificate identity onto the audit event alongside JWT claims.

    Both Caddy and nginx forward the verified client cert as
    ``X-Client-CN`` and ``X-Client-Fingerprint`` headers (see the
    proxy templates in ``examples/mcp-output-port-docker/proxy/``).
    Missing headers return an empty dict — operators NOT running
    behind an mTLS-terminating proxy don't see these fields, which
    is correct.
    """
    out: Dict[str, str] = {}
    for header_name, attr_name in [
        ("x-client-cn", "client_cn"),
        ("x-client-fingerprint", "client_fingerprint"),
    ]:
        for key, value in headers.items():
            if key.lower() == header_name and value:
                out[attr_name] = value
                break
    return out
