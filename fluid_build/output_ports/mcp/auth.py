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

Two pluggable schemes that the gateway selects via
``FLUID_MCP_AUTH_MODE`` (plus ``none`` to opt out):

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

mTLS (client-cert-bound identity) is best handled by the reverse
proxy in front of the gateway — see
``examples/mcp-output-port-docker/proxy/`` for templates. This module
exposes :func:`extract_mtls_identity` to read the proxy-forwarded
``X-Client-CN`` / ``X-Client-Fingerprint`` headers so the gateway
can stamp the client cert into the audit event for cryptographic
attribution alongside JWT claims. Those headers are honoured only when
the TCP peer is a proxy listed in ``FLUID_MCP_TRUSTED_PROXIES``
(:func:`parse_trusted_proxies`); from anyone else they are ignored.

Borrowed-not-built per /borrow-before-build:

* `PyJWT <https://pyjwt.readthedocs.io>`_ for token validation
  (already in venv).
* `cryptography <https://cryptography.io>`_ for JWKS key parsing
  (already in venv).
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import os
import time
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Dict, FrozenSet, Mapping, Optional, Sequence, Tuple, Union

#: Claim -> caller-attribute mappings applied when an operator configures none.
#:
#: These are not decoration. ``model`` and ``use_case`` are the inputs to the
#: agentPolicy gate (``decide()`` in fluid_build.policy.decision), and ``sub`` /
#: ``tenant_id`` are what ``${caller.*}`` row filters interpolate. Losing any of
#: them does not fail — it silently widens access, which is why
#: ``FLUID_MCP_JWT_CLAIM_MAPPING`` MERGES over these rather than replacing them.
#:
#: ``jurisdiction`` is the exception to that sentence and is worth knowing:
#: losing it does NOT widen access, it closes the gate completely. A contract
#: that pins ``sovereignty.jurisdiction`` refuses every caller whose
#: jurisdiction is unknown, so an operator who maps their IdP's claim to some
#: other attribute name locks themselves out rather than letting strangers in.
#: That is the correct direction for a sovereignty control, but it fails loudly
#: and the mapping is where to look.
DEFAULT_JWT_CLAIM_MAPPINGS: Mapping[str, str] = MappingProxyType(
    {
        "sub": "sub",
        "model": "model",
        "use_case": "use_case",
        "tenant_id": "tenant_id",
        "jurisdiction": "jurisdiction",
    }
)

#: Every value ``FLUID_MCP_AUTH_MODE`` accepts. mTLS is not a mode: it
#: terminates at the reverse proxy, which forwards the verified certificate
#: to a gateway that trusts that proxy via ``FLUID_MCP_TRUSTED_PROXIES``.
VALID_AUTH_MODES: Tuple[str, ...] = ("shared-token", "jwt", "none")

#: Proxy-forwarded client-certificate headers -> caller attribute names.
MTLS_IDENTITY_HEADERS: Tuple[Tuple[str, str], ...] = (
    ("x-client-cn", "client_cn"),
    ("x-client-fingerprint", "client_fingerprint"),
)

#: Caller attributes that name a client certificate. Like ``jurisdiction``
#: they bind from the verified channel only, never from what a client says
#: about itself: the one party able to vouch for a certificate is the trusted
#: proxy that terminated it. See ``OutputPortMcpServer._resolve_request_identity``.
MTLS_IDENTITY_ATTRS: FrozenSet[str] = frozenset(name for _, name in MTLS_IDENTITY_HEADERS)

TrustedNetwork = Union[ipaddress.IPv4Network, ipaddress.IPv6Network]


class AuthConfigError(ValueError):
    """The gateway's auth environment is invalid, so it must not start.

    A ``ValueError`` subclass so callers that already catch ``ValueError``
    keep working. ``env_var`` / ``value`` / ``expected`` let the CLI render
    a typed error instead of a traceback.
    """

    def __init__(self, message: str, *, env_var: str, value: str, expected: str) -> None:
        super().__init__(message)
        self.env_var = env_var
        self.value = value
        self.expected = expected


#: The caller attribute a verified jurisdiction claim lands in. Read from the
#: cryptographically-verified channel ONLY — see
#: ``OutputPortMcpServer._resolve_verified_jurisdiction``.
JURISDICTION_ATTR = "jurisdiction"


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
    """``shared-token`` / ``jwt`` / ``mtls`` / ``none`` —
    surfaced on the audit event so operators can see WHICH layer
    authenticated each call."""


class AuthValidator:
    """Strategy pattern for the auth modes the gateway supports
    (``shared-token`` / ``jwt`` / ``none``).

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
        jwks_cache_ttl_seconds: float = 600.0,
        trusted_proxies: Sequence[TrustedNetwork] = (),
    ) -> None:
        if mode not in VALID_AUTH_MODES:
            raise AuthConfigError(
                f"unknown FLUID_MCP_AUTH_MODE={mode!r}; expected one of "
                f"{' / '.join(VALID_AUTH_MODES)}",
                env_var="FLUID_MCP_AUTH_MODE",
                value=mode,
                expected=", ".join(VALID_AUTH_MODES),
            )
        self.mode = mode
        # Networks whose X-Client-CN / X-Client-Fingerprint headers are
        # believed. Empty (the default) means no peer is trusted.
        self.trusted_proxies: Tuple[TrustedNetwork, ...] = tuple(trusted_proxies)
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
            jwt_claim_mappings if jwt_claim_mappings is not None else DEFAULT_JWT_CLAIM_MAPPINGS
        )
        self.jwks_cache_ttl_seconds = jwks_cache_ttl_seconds
        self._jwks_cache: Dict[str, Tuple[float, Any]] = {}

    @classmethod
    def from_env(cls) -> "AuthValidator":
        """Build a validator from ``FLUID_MCP_AUTH_*`` env vars.

        Defaults to the historical ``shared-token`` mode using
        ``FLUID_MCP_AUTH_TOKEN`` so existing deployments keep
        working. Operators upgrade to JWT by setting
        ``FLUID_MCP_AUTH_MODE=jwt`` and the related issuer / JWKS
        env vars.
        """
        mode = os.environ.get("FLUID_MCP_AUTH_MODE", "shared-token")
        kwargs: Dict[str, Any] = {
            "mode": mode,
            "trusted_proxies": parse_trusted_proxies(os.environ.get("FLUID_MCP_TRUSTED_PROXIES")),
        }
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
                # MERGE over the defaults, never replace them. Assigning `parsed`
                # wholesale meant an operator mapping one extra claim silently
                # dropped sub/model/use_case/tenant_id — turning off the
                # agentPolicy model and use-case gates and emptying every
                # ${caller.*} row filter, with no error and a server still
                # reporting healthy. An operator who really wants a default gone
                # can still map it away explicitly; what they can no longer do is
                # lose it by accident while adding something unrelated.
                kwargs["jwt_claim_mappings"] = {**DEFAULT_JWT_CLAIM_MAPPINGS, **parsed}
        return cls(**kwargs)

    # ------------------------------------------------------------------
    # Public surface
    # ------------------------------------------------------------------

    def is_enabled(self) -> bool:
        """True when the validator has enough config to enforce auth.

        ``shared-token`` requires ``FLUID_MCP_AUTH_TOKEN``; JWT
        requires issuer + audience + JWKS URL. Missing config means
        the gateway runs WITHOUT auth — surfaced as a loud startup
        warning.
        """
        if self.mode == "none":
            return False
        if self.mode == "shared-token":
            return bool(self.shared_token)
        if self.mode == "jwt":
            return bool(self.jwt_issuer and self.jwt_audience and self.jwt_jwks_url)
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


def parse_trusted_proxies(value: Optional[str]) -> Tuple[TrustedNetwork, ...]:
    """Parse ``FLUID_MCP_TRUSTED_PROXIES``: comma-separated IPs / CIDRs.

    Same shape as uvicorn's ``--forwarded-allow-ips`` (``_TrustedHosts`` in
    ``uvicorn/middleware/proxy_headers.py``), with three deliberate
    differences. uvicorn keeps an unparseable entry as a literal string,
    which its own source notes makes a typo impossible to notice; here a
    malformed entry stops the gateway with :class:`AuthConfigError`.
    ``*`` is refused: trusting every peer is exactly the posture this
    setting exists to prevent, so an operator who means it must write
    ``0.0.0.0/0,::/0``. And a host address with a prefix
    (``10.0.4.17/8``) is refused rather than silently widened to its
    network (``10.0.0.0/8``): it is almost always a typo for ``/32``, and
    the widened reading trusts every peer in that range.

    Unset or blank returns ``()`` — no peer is trusted.
    """
    if value is None or not value.strip():
        return ()
    networks = []
    for raw in value.split(","):
        entry = raw.strip()
        try:
            if not entry or entry == "*":
                raise ValueError(entry)
            network = ipaddress.ip_network(entry, strict=False)
        except ValueError:
            raise AuthConfigError(
                f"FLUID_MCP_TRUSTED_PROXIES entry {entry!r} is not an IP address or "
                "CIDR network; expected a comma-separated list of your proxies' "
                "own addresses, such as '127.0.0.1' or '127.0.0.1,10.0.4.17/32'",
                env_var="FLUID_MCP_TRUSTED_PROXIES",
                value=value,
                expected="comma-separated IP addresses or CIDR networks",
            ) from None
        if "/" in entry and network.network_address != ipaddress.ip_interface(entry).ip:
            host = entry.split("/", 1)[0]
            raise AuthConfigError(
                f"FLUID_MCP_TRUSTED_PROXIES entry {entry!r} is a host address with a "
                f"prefix, which would trust every peer in {str(network)!r}. Write "
                f"{host!r} to trust only that proxy, or {str(network)!r} if you mean "
                "the whole range",
                env_var="FLUID_MCP_TRUSTED_PROXIES",
                value=value,
                expected="comma-separated IP addresses or CIDR networks",
            )
        networks.append(network)
    return tuple(networks)


def is_trusted_proxy(peer_host: Optional[str], trusted_proxies: Sequence[TrustedNetwork]) -> bool:
    """True when ``peer_host`` (the TCP peer address) is inside a trusted
    network. An unknown or non-IP peer is never trusted."""
    if not peer_host or not trusted_proxies:
        return False
    try:
        address = ipaddress.ip_address(peer_host)
    except ValueError:
        return False
    return any(address in network for network in trusted_proxies)


def _is_header_safe(value: str) -> bool:
    return not any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value)


def extract_mtls_identity(
    headers: Mapping[str, str],
    *,
    peer_host: Optional[str] = None,
    trusted_proxies: Sequence[TrustedNetwork] = (),
) -> Dict[str, str]:
    """Read mTLS-from-proxy metadata (CN + fingerprint) into the
    caller_attributes shape so the gateway can stamp the client
    certificate identity onto the audit event alongside JWT claims.

    Both Caddy and nginx forward the verified client cert as
    ``X-Client-CN`` and ``X-Client-Fingerprint`` headers (see the
    proxy templates in ``examples/mcp-output-port-docker/proxy/``).

    The headers are believed ONLY when ``peer_host`` — the TCP peer, never
    an ``X-Forwarded-For`` value — falls inside ``trusted_proxies``. Any
    other caller could have written them itself, so for anyone else this
    returns ``{}``; the defaults trust nobody. Values carrying control
    characters are dropped too. Missing headers return ``{}``.
    """
    if not is_trusted_proxy(peer_host, trusted_proxies):
        return {}
    out: Dict[str, str] = {}
    for header_name, attr_name in MTLS_IDENTITY_HEADERS:
        for key, value in headers.items():
            if key.lower() == header_name and value and _is_header_safe(value):
                out[attr_name] = value
                break
    return out


def has_mtls_identity_headers(headers: Mapping[str, str]) -> bool:
    """True when any proxy-forwarded certificate header is present."""
    names = {name for name, _ in MTLS_IDENTITY_HEADERS}
    return any(key.lower() in names for key in headers)


def ignored_mtls_headers_reason(
    headers: Mapping[str, str],
    *,
    peer_host: Optional[str],
    trusted_proxies: Sequence[TrustedNetwork],
) -> str:
    """Why :func:`extract_mtls_identity` returned nothing for headers that
    :func:`has_mtls_identity_headers` saw: ``"unknown_peer"``,
    ``"untrusted_peer"``, ``"empty"`` (a trusted proxy sent blank values) or
    ``"control_characters"``. Mirrors the checks in
    :func:`extract_mtls_identity` so the operator log names the real cause."""
    if not peer_host:
        return "unknown_peer"
    if not is_trusted_proxy(peer_host, trusted_proxies):
        return "untrusted_peer"
    names = {name for name, _ in MTLS_IDENTITY_HEADERS}
    values = [value for key, value in headers.items() if key.lower() in names]
    if any(value and not _is_header_safe(value) for value in values):
        return "control_characters"
    return "empty"
