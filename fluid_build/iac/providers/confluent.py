# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Confluent Cloud IaC plugin — FLUID contract → Tableflow managed Kafka→Iceberg.

Translates a confluent-bound Iceberg exposure into the ``confluentinc/confluent``
provider's managed Kafka→Iceberg control plane: a Tableflow topic that
materializes a Kafka topic into an Iceberg table, the AWS Glue catalog
integration that publishes it, and the provider (storage) integration that
grants Confluent access to the customer's bucket. Confluent owns compaction and
snapshot-expiry — the gap every self-managed Connect sink leaves to the operator
(RFC-streaming-extension §15). v1 is restricted to the ``byob_aws`` + ``aws_glue``
topology (§15.1). A pure function of the contract; no credentials, no network.

Two operator prerequisites the emitter cannot encode (documented, not silent):

* **Two-phase IAM.** ``confluent_provider_integration`` exports ``aws.external_id``
  only *after* it is created, and the customer's AWS IAM trust policy must then be
  updated with that external id. A single ``tofu apply`` cannot bind the AWS-side
  trust update to a value that does not yet exist, so the AWS role + trust policy
  are a manual prerequisite; the emitter consumes the role's ARN via
  ``binding.location.confluent_role_arn`` (§15 pt5).
* **Pre-existing Glue database.** Tableflow publishes into an existing Glue
  database (no ``glue:CreateDatabase`` in its IAM contract) — it does not create
  it. The emitter passes ``binding.location.database`` as ``custom_database``.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Tuple

from ..importer import ImportBlock
from ..naming import safe_ident, tofu_ref
from ..versions import required_providers

# Iceberg expose formats a confluent binding may carry (the alias normalises to
# ``iceberg`` upstream, but accept both so the validator/emitter agree).
_ICEBERG_FORMATS = ("iceberg", "iceberg_table")


def _confluent_exposures(
    contract: Mapping[str, Any],
) -> Iterable[Tuple[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]]]:
    """Yield ``(exposure, binding, location)`` for every confluent-bound expose."""
    for exposure in contract.get("exposes") or []:
        binding = exposure.get("binding") or {}
        if str(binding.get("platform") or "").lower() == "confluent":
            yield exposure, binding, (binding.get("location") or {})


class ConfluentIacPlugin:
    """``IacProviderPlugin`` for Confluent Cloud Tableflow."""

    name = "confluent"
    required_providers = required_providers("confluent")
    # The confluentinc/confluent provider reads CONFLUENT_CLOUD_API_KEY/SECRET
    # from the environment; Tableflow resources additionally read a Tableflow
    # API key/secret when set. `tofu` self-configures from these, so the emitted
    # `.tf.json` stays credential-free.
    credential_env_vars = (
        "CONFLUENT_CLOUD_API_KEY",
        "CONFLUENT_CLOUD_API_SECRET",
        "TABLEFLOW_API_KEY",
        "TABLEFLOW_API_SECRET",
    )

    def emit(
        self, contract: Mapping[str, Any], actions: Iterable[Mapping[str, Any]] = ()
    ) -> Dict[str, Any]:
        resources: Dict[str, Dict[str, Any]] = {}
        cid = safe_ident(contract.get("id") or contract.get("name") or "product")
        for exposure, _binding, loc in _confluent_exposures(contract):
            _emit_tableflow(resources, loc, exposure, cid)
        return resources

    def emit_data(
        self, contract: Mapping[str, Any], actions: Iterable[Mapping[str, Any]] = ()
    ) -> Dict[str, Any]:
        """Confluent emits only ``resource`` blocks — no ``data`` sub-tree."""
        return {}

    def credential_env(self, env: Mapping[str, str]) -> Dict[str, str]:
        """The provider reads its standard ``CONFLUENT_CLOUD_*`` vars directly —
        nothing to rename or derive."""
        return {}

    def discover_imports(
        self, contract: Mapping[str, Any], actions: Iterable[Mapping[str, Any]] = ()
    ) -> List[ImportBlock]:
        """Tableflow / integration ids are environment-scoped and only known
        post-create; no stable brownfield import identifier to offer."""
        return []

    def provider_block(self) -> Dict[str, Any]:
        """The provider self-configures from the environment — no static block."""
        return {}


def _emit_tableflow(
    resources: Dict[str, Any],
    loc: Mapping[str, Any],
    exposure: Mapping[str, Any],
    cid: str,
) -> None:
    """Emit the three Tableflow resources for one confluent-bound exposure.

    Skips silently when a hard input is absent — ``validate_confluent_binding``
    surfaces a clean error at validate time, and plan-binding guarantees a
    validated contract by apply, so this only guards a partial/unvalidated dict.
    """
    environment_id = loc.get("environment_id")
    cluster_id = loc.get("kafka_cluster_id")
    bucket = loc.get("bucket")
    role_arn = loc.get("confluent_role_arn")
    if not (environment_id and cluster_id and bucket and role_arn):
        return

    database = loc.get("database")
    table = loc.get("table") or exposure.get("exposeId")
    topic = loc.get("topic") or table or "topic"
    name = safe_ident(f"{cid}_{topic}")

    # 1. Provider (storage) integration — exports external_id + iam_role_arn
    #    (computed, post-apply); two-phase IAM, see the module docstring.
    resources.setdefault("confluent_provider_integration", {})[name] = {
        "display_name": safe_ident(f"fluid_{name}"),
        "environment": {"id": environment_id},
        "aws": {"customer_role_arn": role_arn},
    }
    pi_ref = tofu_ref(f"confluent_provider_integration.{name}.id")

    # 2. Tableflow topic — materializes the Kafka topic to an Iceberg table in
    #    the customer's own bucket (byob_aws), table format ICEBERG.
    resources.setdefault("confluent_tableflow_topic", {})[name] = {
        "environment": {"id": environment_id},
        "kafka_cluster": {"id": cluster_id},
        "display_name": topic,
        "table_formats": ["ICEBERG"],
        "byob_aws": {"bucket_name": bucket, "provider_integration_id": pi_ref},
    }

    # 3. Catalog integration — publishes the Iceberg table to AWS Glue. The Glue
    #    database must pre-exist (Tableflow does not create it).
    glue: Dict[str, Any] = {"provider_integration_id": pi_ref}
    if database:
        glue["custom_database"] = database
    resources.setdefault("confluent_catalog_integration", {})[name] = {
        "environment": {"id": environment_id},
        "kafka_cluster": {"id": cluster_id},
        "display_name": safe_ident(f"{name}_glue"),
        "aws_glue": glue,
    }


def validate_confluent_binding(contract: Mapping[str, Any]) -> Tuple[List[str], List[str]]:
    """Plan-time gate for confluent-bound exposures (RFC §15 pt3, anti-no-op).

    The Tableflow emitter has hard required inputs that have no other home in the
    contract — without them it would silently emit an incomplete module. Surface
    a clean error at validate time instead. Returns ``(errors, warnings)``.
    """
    errors: List[str] = []
    warnings: List[str] = []
    for exposure, binding, loc in _confluent_exposures(contract):
        eid = exposure.get("exposeId") or "?"
        fmt = str(binding.get("format") or "").lower()
        if fmt not in _ICEBERG_FORMATS:
            errors.append(
                f"expose '{eid}': platform=confluent requires binding.format=iceberg "
                f"(Tableflow materializes Kafka→Iceberg), got '{binding.get('format')}'"
            )
        for key, label in (
            ("environment_id", "binding.location.environment_id"),
            ("kafka_cluster_id", "binding.location.kafka_cluster_id"),
            ("bucket", "binding.location.bucket (the byob_aws storage bucket)"),
            (
                "confluent_role_arn",
                "binding.location.confluent_role_arn (the pre-created AWS role; two-phase IAM)",
            ),
        ):
            if not loc.get(key):
                errors.append(f"expose '{eid}': platform=confluent requires {label}")
        if not loc.get("database"):
            warnings.append(
                f"expose '{eid}': no binding.location.database — the AWS Glue database must "
                f"pre-exist and be named as custom_database so Tableflow publishes there"
            )
    return errors, warnings
