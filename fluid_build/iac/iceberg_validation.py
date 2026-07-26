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

"""Validate-time gate for Iceberg exposes, the anti-no-op half of the emitters.

The Snowflake and GCP IaC emitters are pure and emit-when-derivable: an
Iceberg expose missing a required input produces no resource rather than a
broken one. That is right for an emitter, but on its own it is silent. The
user gets no external volume, or no bucket, learns nothing at `fluid apply`,
and finds out at `dbt run` when the warehouse rejects the write.

This module is the loud half, following the same shape as
``confluent.validate_confluent_binding``: the emitter stays quiet, the
validator explains. The two MUST agree about what counts as derivable, or
the gate either blocks a contract that would have worked or waves through
one that silently emits nothing. Every check here mirrors a specific skip
branch, and the tests assert the pairing in both directions.

What is deliberately NOT checked: whether a given catalog accepts, requires
or forbids a client-supplied storage location. dbt's own EPIC (dbt-labs/
dbt-core#15265) documents that only for Glue (required), S3 Tables
(server-managed) and R2 (accepted); it records BigLake as "structurally
Horizon-like (undocumented)" and does not detail Horizon or Unity. Encoding
a matrix upstream has not settled would turn a guess into an error message,
which is worse than staying quiet.
"""

from __future__ import annotations

from typing import Any, List, Mapping, Tuple

#: ``binding.format`` values marking an Iceberg-table expose. Shared shape
#: with both IaC emitters.
_ICEBERG_FORMATS = ("iceberg", "iceberg_table")


def _iceberg_exposures(contract: Mapping[str, Any], platform: str):
    """Yield ``(expose_id, binding, location)`` for Iceberg exposes on ``platform``."""
    for exposure in contract.get("exposes") or []:
        if not isinstance(exposure, Mapping):
            continue
        binding = exposure.get("binding") or {}
        if str(binding.get("platform") or "").lower() != platform:
            continue
        if str(binding.get("format") or "").lower() not in _ICEBERG_FORMATS:
            continue
        yield (
            exposure.get("exposeId") or exposure.get("id") or "?",
            binding,
            binding.get("location") or {},
        )


def validate_iceberg_bindings(
    contract: Mapping[str, Any],
) -> Tuple[List[str], List[str]]:
    """Return ``(errors, warnings)`` for every Iceberg expose in ``contract``.

    Errors mark a contract whose Iceberg prerequisites cannot be provisioned,
    so ``fluid apply`` would emit nothing and the failure would surface later
    in the warehouse. Warnings mark a surface that is understood but not
    emitted yet.
    """
    errors: List[str] = []
    warnings: List[str] = []
    _check_snowflake(contract, errors, warnings)
    _check_volume_collisions(contract, errors)
    _check_gcp(contract, errors, warnings)
    return errors, warnings


def _check_volume_collisions(contract: Mapping[str, Any], errors: List[str]) -> None:
    """Two exposes deriving one volume name with different storage.

    ``snowflake._emit_iceberg_prereqs`` raises on this, and its comment says
    the alternative (first-expose-wins) is a data-placement failure that
    "must never be quiet". Raising mid-emit is loud but late: it surfaces at
    ``fluid apply`` rather than ``fluid validate``. Catch it here so the
    contract is rejected before anyone provisions anything.
    """
    from fluid_build.providers._iceberg_catalog import (
        iceberg_external_volume_name,
        iceberg_storage_uri,
    )

    seen: dict = {}
    for eid, binding, loc in _iceberg_exposures(contract, "snowflake"):
        if str(loc.get("catalog") or "").lower():
            continue
        try:
            name = iceberg_external_volume_name(contract, binding)
        except Exception:  # noqa: BLE001 - reported by _check_snowflake
            continue
        storage = iceberg_storage_uri(binding, scheme="s3") or iceberg_storage_uri(
            binding, scheme="gs"
        )
        if not storage:
            continue
        prior_eid, prior_storage = seen.get(name, (None, None))
        if prior_eid is not None and prior_storage != storage:
            errors.append(
                f"exposes '{prior_eid}' and '{eid}' both derive EXTERNAL VOLUME "
                f"'{name}' but point at different storage ({prior_storage!r} vs "
                f"{storage!r}). One expose's data would land in the other's "
                "bucket. Set an explicit binding.icebergConfig.properties."
                "external_volume on one of them."
            )
        else:
            seen[name] = (eid, storage)


def _check_snowflake(contract: Mapping[str, Any], errors: List[str], warnings: List[str]) -> None:
    """Mirror ``snowflake._emit_iceberg_prereqs``'s skip branches."""
    from fluid_build.providers._iceberg_catalog import (
        EXTERNAL_ICEBERG_CATALOGS,
        iceberg_external_volume_is_override,
        iceberg_external_volume_name,
        iceberg_storage_provider,
    )

    for eid, binding, loc in _iceberg_exposures(contract, "snowflake"):
        catalog = str(loc.get("catalog") or "").lower()

        if catalog == "glue":
            missing = [
                label
                for key, label in (
                    ("iam_role_arn", "binding.location.iam_role_arn"),
                    ("account", "binding.location.account"),
                )
                if not loc.get(key)
            ]
            if missing:
                errors.append(
                    f"expose '{eid}': a Glue-cataloged Iceberg table needs "
                    f"{' and '.join(missing)} so FLUID can create the Snowflake "
                    "CATALOG INTEGRATION (the role Snowflake assumes, plus the "
                    "AWS account id holding the Glue catalog). Without them no "
                    "integration is emitted and dbt cannot read the table."
                )
            continue

        if catalog in EXTERNAL_ICEBERG_CATALOGS:
            warnings.append(
                f"expose '{eid}': catalog '{catalog}' is understood but FLUID does "
                "not emit its Snowflake CATALOG INTEGRATION yet, because that "
                "integration authenticates with an OAuth secret or bearer token "
                "and the emitted module is credential-free. Create it out of band."
            )
            continue

        if iceberg_external_volume_is_override(binding):
            # Operator-owned volume: FLUID emits no CREATE, by design. The name
            # still has to be a legal identifier, because both the dbt
            # catalogs.yml emitter and the IaC side route it through
            # validate_ident and would otherwise raise mid-emit.
            try:
                iceberg_external_volume_name(contract, binding)
            except Exception as exc:  # noqa: BLE001
                errors.append(
                    f"expose '{eid}': binding.icebergConfig.properties."
                    f"external_volume is not a legal Snowflake identifier ({exc}). "
                    "dbt's catalogs.yml and the IaC emitter both reject it."
                )
            continue

        # Snowflake-managed (Horizon): the EXTERNAL VOLUME path. Resolve the
        # provider through the SHARED helper rather than re-deriving it, so a
        # gs:// warehouse alongside a bucket is correctly a GCS volume needing
        # no role, exactly as the emitter treats it.
        provider = iceberg_storage_provider(loc)
        if not provider:
            errors.append(
                f"expose '{eid}': a Snowflake-managed Iceberg table needs "
                "binding.location.warehouse (s3:// or gs://) or "
                "binding.location.bucket so FLUID can create the EXTERNAL "
                "VOLUME dbt's catalogs.yml references. Azure is not supported "
                "yet: the volume needs an azure_tenant_id the contract schema "
                "has no slot for."
            )
        elif provider == "S3" and not loc.get("iam_role_arn"):
            errors.append(
                f"expose '{eid}': an S3-backed EXTERNAL VOLUME needs "
                "binding.location.iam_role_arn. Snowflake rejects an S3 storage "
                "location without a role at CREATE time, so FLUID emits no "
                "volume rather than one that fails on apply."
            )


def _check_gcp(contract: Mapping[str, Any], errors: List[str], warnings: List[str]) -> None:
    """Mirror ``gcp._emit_iceberg_storage``'s skip branch."""
    from fluid_build.providers._iceberg_catalog import iceberg_bucket_name

    for eid, binding, loc in _iceberg_exposures(contract, "gcp"):
        if iceberg_bucket_name(binding):
            continue
        warehouse = str(loc.get("warehouse") or "")
        if warehouse.startswith("gs://"):
            errors.append(
                f"expose '{eid}': binding.location.warehouse is '{warehouse}', which "
                "names no bucket. Use gs://<bucket>/<optional-path> so FLUID can "
                "create the bucket dbt's catalogs.yml points at."
            )
        elif warehouse:
            errors.append(
                f"expose '{eid}': binding.location.warehouse is '{warehouse}', but a "
                "BigQuery Iceberg table is backed by GCS. Use a gs:// warehouse or "
                "binding.location.bucket so FLUID can create the bucket dbt's "
                "catalogs.yml points at."
            )
        else:
            errors.append(
                f"expose '{eid}': a BigQuery Iceberg table needs "
                "binding.location.bucket or a gs:// binding.location.warehouse. "
                "dbt creates the table entry but not the storage behind it, so "
                "without one FLUID emits no bucket and dbt has nowhere to write."
            )
