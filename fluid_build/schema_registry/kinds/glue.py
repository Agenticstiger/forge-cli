# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The Glue Schema Registry kind: IAM through the AWS SDK, no secret, no dry-run API."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping, Optional, Tuple

from ._base import RegistryKind

#: Registry and schema names: letters, digits and ``_ $ # . -``.
_GLUE_NAME = re.compile(r"^[A-Za-z0-9_$#.-]{1,255}$")


def _aws_region(contract: Mapping[str, Any], environ: Optional[Mapping[str, str]]) -> Optional[str]:
    """The region a Glue registry is read in: where ``fluid apply`` puts the product.

    The AWS emitter's own region (every AWS binding's ``location.region``), else the
    environment in the AWS SDK order (``AWS_REGION``, then ``AWS_DEFAULT_REGION``):
    boto3 1.x reads only the second (the same rule as ``cli/_diff_live.py``).
    """
    import os

    from fluid_build.iac.providers.aws import AwsIacPlugin

    try:
        region = AwsIacPlugin().provider_block_for(contract).get("region")
    except Exception:  # noqa: BLE001 - an unemittable contract: fall back to the env
        region = None
    if region:
        return str(region)
    env = os.environ if environ is None else environ
    return next((env[k] for k in ("AWS_REGION", "AWS_DEFAULT_REGION") if env.get(k)), None)


class GlueKind(RegistryKind):
    name = "glue"
    title = "Glue Schema Registry"
    modes = (
        "NONE",
        "DISABLED",
        "BACKWARD",
        "BACKWARD_ALL",
        "FORWARD",
        "FORWARD_ALL",
        "FULL",
        "FULL_ALL",
    )
    default_compatibility = "BACKWARD_ALL"
    registering_cloud = "aws"
    bind_hint = "bind an AWS expose (the Glue registry's product)"
    subject_pattern = _GLUE_NAME
    fields = frozenset({"registryName"})
    refuses = frozenset({"url", "auth"})
    checks_compatibility = False

    def field_errors(self, spec: Mapping[str, Any], where: str) -> List[str]:
        errors: List[str] = []
        if "url" in spec:
            errors.append(
                f"{where}.url is for kind: confluent; name a Glue registry by registryName"
            )
        if "auth" in spec:
            errors.append(
                f"{where}.auth is for kind: confluent; a Glue registry authenticates with IAM"
            )
        name = spec.get("registryName")
        if name is not None and not (isinstance(name, str) and _GLUE_NAME.fullmatch(name)):
            errors.append(f"{where}.registryName: letters, digits and _ $ # . - (1-255)")
        return errors

    def egress_findings(
        self, subject: Any, *, stage: str, environ: Optional[Mapping[str, str]] = None
    ) -> List[Tuple[str, str]]:
        """None: Glue is reached through the AWS SDK with IAM. A contract names no host
        and no secret for it (``field_errors`` refuses ``url`` and ``auth``)."""
        return []

    def location(self, ref: Any) -> str:
        return str(ref.glue_registry)

    def registry_block(self, ref: Any) -> Dict[str, Any]:
        return {"registryName": ref.registry_name} if ref.registry_name else {}

    def produced_key(self, ref: Any) -> str:
        from fluid_build.iac.naming import safe_ident

        return safe_ident(f"{ref.glue_registry}_{ref.subject}")

    def produced_errors(
        self, contract: Mapping[str, Any], subject: Any, expose: Mapping[str, Any]
    ) -> List[str]:
        from ..produced import owns_registry

        ref = subject.ref
        if not ref.registry_name and owns_registry(contract, expose):
            return [
                f"{ref.where}: packaging.containers.schemaRegistry is isolated, so this "
                "product creates its registry, and the registry has no registryName"
            ]
        return []

    def client(
        self,
        ref: Any,
        *,
        contract: Optional[Mapping[str, Any]] = None,
        environ: Optional[Mapping[str, str]] = None,
        credential: Optional[str] = None,
        factory: Any = None,
    ) -> Any:
        if factory is not None:
            return factory()
        from ..clients import GlueRegistry

        if contract is None:
            return GlueRegistry()
        return GlueRegistry(region=_aws_region(contract, environ))

    def read(self, client: Any, ref: Any) -> Any:
        return client.get_version(ref.glue_registry, ref.subject, ref.version)
