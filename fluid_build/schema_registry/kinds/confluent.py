# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The Confluent-API registry kind: an http(s) REST endpoint, optional Basic auth."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping, Optional, Tuple

from ._base import RegistryKind

_URL = re.compile(r"^https?://[^/?#@\s]+(?:/[^?#\s]*)?$")


def _same_endpoint(a: str, b: str) -> bool:
    """Whether two registry URLs are one endpoint: scheme, host, port (default filled
    in) and path, ignoring case and a trailing slash. An unreadable URL is no match."""
    from urllib.parse import urlsplit

    def key(url: str) -> Any:
        try:
            parts = urlsplit(url.strip())
            scheme = parts.scheme.lower()
            port = parts.port or {"https": 443, "http": 80}.get(scheme)
        except ValueError:  # an unreadable port or IPv6 literal
            return None
        return scheme, (parts.hostname or "").lower(), port, parts.path.rstrip("/")

    first = key(a)
    return first is not None and first == key(b)


def _operator_endpoint(
    subject: Any,
    environ: Optional[Mapping[str, str]],
    errors: List[str],
    warnings: List[str],
) -> None:
    """A produced Confluent subject lands in the operator's ``SCHEMA_REGISTRY_REST_ENDPOINT``.

    OpenTofu registers it in the provider-level registry, so a contract naming one
    registry and an environment naming another would register the schema somewhere
    the contract never said: refused. Unset is a warning: apply needs it.
    """
    import os

    from ..policy import TOFU_REGISTRY_ENDPOINT_ENV

    env = os.environ if environ is None else environ
    endpoint = env.get(TOFU_REGISTRY_ENDPOINT_ENV)
    ref = subject.ref
    if not endpoint:
        warnings.append(
            f"{ref.where}: OpenTofu registers subject {ref.subject!r} in the registry "
            f"{TOFU_REGISTRY_ENDPOINT_ENV} names, and it is unset; fluid apply needs it set "
            f"to {ref.url} with SCHEMA_REGISTRY_ID, SCHEMA_REGISTRY_API_KEY and "
            "SCHEMA_REGISTRY_API_SECRET"
        )
    elif not _same_endpoint(endpoint, str(ref.url)):
        errors.append(
            f"{ref.where}: the contract registers subject {ref.subject!r} in {ref.url}, and "
            f"{TOFU_REGISTRY_ENDPOINT_ENV} points OpenTofu at another registry"
        )


class ConfluentKind(RegistryKind):
    name = "confluent"
    title = "Confluent"
    modes = (
        "NONE",
        "BACKWARD",
        "BACKWARD_TRANSITIVE",
        "FORWARD",
        "FORWARD_TRANSITIVE",
        "FULL",
        "FULL_TRANSITIVE",
    )
    default_compatibility = "BACKWARD_TRANSITIVE"
    registering_cloud = "confluent"
    bind_hint = "bind the topic with platform: confluent and format: kafka_topic"
    fields = frozenset({"url", "auth", "rules"})
    refuses = frozenset({"registryName"})
    # policy.TOFU_REGISTRY_SECRET_ENV (spelled here: kinds/ cannot import policy at module
    # level); tests/schema_registry/test_egress_floor.py pins that the two agree.
    operator_credential_env = "SCHEMA_REGISTRY_API_SECRET"  # pragma: allowlist secret - a name
    checks_compatibility = True
    # Confluent Schema Registry has confluent/type/decimal.proto built in (ProtobufSchema
    # KNOWN_DEPENDENCIES), so the subject needs no schema reference.
    protobuf_decimal = ("confluent.type.Decimal", "confluent/type/decimal.proto")

    def field_errors(self, spec: Mapping[str, Any], where: str) -> List[str]:
        errors: List[str] = []
        url = spec.get("url")
        if not isinstance(url, str) or not _URL.fullmatch(url):
            errors.append(
                f"{where}.url: a confluent registry needs its http(s) REST endpoint, "
                "with no credentials, query or fragment"
            )
        if "registryName" in spec:
            errors.append(f"{where}.registryName is for kind: glue; a Confluent registry has a url")
        if "rules" in spec and not isinstance(spec["rules"], bool):
            errors.append(f"{where}.rules: expected true or false")
        return errors

    def location(self, ref: Any) -> Optional[str]:
        return ref.url

    def registry_block(self, ref: Any) -> Dict[str, Any]:
        return {"url": ref.url}

    def egress_findings(
        self, subject: Any, *, stage: str, environ: Optional[Mapping[str, str]] = None
    ) -> List[Tuple[str, str]]:
        from .. import policy

        ref = subject.ref
        findings = policy.static_host_findings(ref)
        if findings:
            return findings
        if subject.role == "produced":
            if ref.secret_ref:
                findings.append(
                    (
                        "error",
                        f"{ref.where}.auth: OpenTofu registers a produced subject with the "
                        f"operator's {policy.TOFU_REGISTRY_KEY_ENV} / "
                        f"{policy.TOFU_REGISTRY_SECRET_ENV}, not a contract secret; remove auth",
                    )
                )
            findings.extend(
                policy.credential_host_findings(
                    ref, policy.TOFU_REGISTRY_SECRET_ENV, stage, environ
                )
            )
            return findings
        if ref.secret_ref:
            findings.extend(policy.secret_ref_findings(ref, stage, environ))
        elif stage == "plan":
            findings.extend(policy.resolved_host_findings(ref, environ))
        return findings

    def produced_checks(
        self, subject: Any, *, stage: str, environ: Optional[Mapping[str, str]] = None
    ) -> Tuple[List[str], List[str]]:
        errors: List[str] = []
        found: List[str] = []
        _operator_endpoint(subject, environ, errors, found)
        # Apply is where OpenTofu registers it: an unset endpoint cannot register.
        warnings: List[str] = []
        (errors if stage == "apply" else warnings).extend(found)
        return errors, warnings

    def client(
        self,
        ref: Any,
        *,
        contract: Optional[Mapping[str, Any]] = None,
        environ: Optional[Mapping[str, str]] = None,
        credential: Optional[str] = None,
        factory: Any = None,
    ) -> Any:
        if factory is None:
            from ..clients import ConfluentRegistry

            factory = ConfluentRegistry
        if credential is None:
            return factory(ref.url)
        return factory(ref.url, credential=credential)

    def read(self, client: Any, ref: Any) -> Any:
        return client.get_version(ref.subject, ref.version)

    def mode(self, client: Any, ref: Any) -> Optional[str]:
        return client.compatibility_level(ref.subject)

    def verdict(
        self, client: Any, ref: Any, version: Any, schema_text: str, data_format: str
    ) -> Tuple[bool, List[str]]:
        return client.check_compatibility(ref.subject, version, schema_text, data_format)
