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
branch, and the tests assert the pairing in both directions: an ERROR marks a
skip the user can fix, a WARNING marks a skip that is deliberate (a catalog
whose Snowflake integration needs a secret the credential-free module cannot
carry), and a clean result means something was emitted.

The emitters and this gate classify ``location.catalog`` through ONE table,
``_iceberg_catalog.catalog_kind_info``. Before it, each side lowered the raw
string and compared it to its own hand-kept list, so ``catalog: lakekeeper``
was Snowflake-managed to the IaC (which built an EXTERNAL VOLUME for it) and
got a false "needs an s3:// or gs:// warehouse" error here, while the
streaming sink wrote the same table over REST. The one check with no skip
branch to mirror is the unknown-kind refusal: every emitter keeps a historic
fallback for a value it does not know, and the fallbacks disagree, so the
gate refuses the value itself.

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

from .provider_match import is_cloud

#: ``binding.format`` values marking an Iceberg-table expose. Shared shape
#: with both IaC emitters.
_ICEBERG_FORMATS = ("iceberg", "iceberg_table")


def _iceberg_exposures(contract: Mapping[str, Any], platform: str):
    """Yield ``(expose_id, binding, location)`` for Iceberg exposes on ``platform``."""
    for exposure in contract.get("exposes") or []:
        if not isinstance(exposure, Mapping):
            continue
        binding = exposure.get("binding") or {}
        # Normalised through the same cloud table the emitters filter on, so
        # this gate cannot go quiet on an exposure they would emit for.
        if not is_cloud(binding, platform):
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
    _check_catalog_kinds(contract, errors)
    _check_snowflake(contract, errors, warnings)
    _check_volume_collisions(contract, errors)
    _check_gcp(contract, errors, warnings)
    return errors, warnings


def _check_catalog_kinds(contract: Mapping[str, Any], errors: List[str]) -> None:
    """A ``location.catalog`` value outside the shared kind table, on any platform.

    No emitter skips an unknown kind; each falls back, and the fallbacks
    disagree. The streaming sink writes it over the REST protocol, while dbt
    ``catalogs.yml`` and the Snowflake IaC treat it as Snowflake-managed and
    create an EXTERNAL VOLUME for it. A typo (``lakekeper``) therefore streams
    into one catalog while dbt writes a second table somewhere else, which is
    the bug class the kind table exists to end. The fallbacks are what make an
    unknown value dangerous, so the gate refuses the value itself, once, and
    the per-platform checks below stay silent about it.

    Confluent exposes are left to ``validate_confluent_binding``, which
    refuses every non-Glue catalog: listing the REST kinds here would point
    the user at values that gate rejects too.
    """
    from fluid_build.providers._iceberg_catalog import (
        FAMILY_UNKNOWN,
        canonical_catalog_kind,
        catalog_kind_info,
        is_iceberg_format,
        known_catalog_kinds,
    )

    for exposure in contract.get("exposes") or []:
        if not isinstance(exposure, Mapping):
            continue
        binding = exposure.get("binding") or {}
        if not isinstance(binding, Mapping) or is_cloud(binding, "confluent"):
            continue
        # The streaming sink's format predicate, which is the widest of the
        # emitters': every spelling some emitter reads a catalog for is checked.
        if not is_iceberg_format(binding.get("format")):
            continue
        loc = binding.get("location") or {}
        raw = loc.get("catalog") if isinstance(loc, Mapping) else None
        if not canonical_catalog_kind(raw):
            continue
        if catalog_kind_info(raw).family == FAMILY_UNKNOWN:
            eid = exposure.get("exposeId") or exposure.get("id") or "?"
            errors.append(
                f"expose '{eid}': binding.location.catalog '{raw}' is not a catalog "
                f"kind FLUID knows. Use one of: {', '.join(known_catalog_kinds())}. "
                "Each emitter guesses differently for an unknown value (the "
                "streaming sink writes over Iceberg REST, dbt and the Snowflake IaC "
                "treat it as Snowflake-managed), so the table would be written to "
                "two different catalogs."
            )


def _check_volume_collisions(contract: Mapping[str, Any], errors: List[str]) -> None:
    """Two exposes deriving one volume name with different storage.

    ``snowflake._emit_iceberg_prereqs`` raises on this, and its comment says
    the alternative (first-expose-wins) is a data-placement failure that
    "must never be quiet". Raising mid-emit is loud but late: it surfaces at
    ``fluid apply`` rather than ``fluid validate``. Catch it here so the
    contract is rejected before anyone provisions anything.

    Only a kind the emitter builds a volume for is checked, which is every
    ``built_in`` row: absent, ``snowflake`` and an unknown value alike. This
    used to skip ANY non-empty ``catalog``, so two ``catalog: snowflake``
    exposes on different storage passed validate and then raised mid-emit.
    """
    from fluid_build.providers._iceberg_catalog import (
        binding_catalog_kind,
        catalog_kind_info,
        iceberg_external_volume_name,
        iceberg_storage_uri,
    )

    seen: dict = {}
    for eid, binding, loc in _iceberg_exposures(contract, "snowflake"):
        if catalog_kind_info(binding_catalog_kind(binding)).snowflake_catalog_type != "built_in":
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
    """Mirror ``snowflake._emit_iceberg_prereqs``'s skip branches.

    Both sides switch on the same row of the kind table: Glue gets the
    integration checks, ``iceberg_rest`` kinds the deferred warning, a kind
    Snowflake cannot integrate at all an error, and ``built_in`` the EXTERNAL
    VOLUME checks.
    """
    from fluid_build.providers._iceberg_catalog import (
        FAMILY_GLUE,
        FAMILY_UNKNOWN,
        binding_catalog_kind,
        catalog_kind_info,
        iceberg_external_volume_is_override,
        iceberg_external_volume_name,
        iceberg_storage_provider,
    )

    for eid, binding, loc in _iceberg_exposures(contract, "snowflake"):
        catalog = str(loc.get("catalog") or "").lower()
        kind = catalog_kind_info(binding_catalog_kind(binding))

        if kind.family == FAMILY_GLUE:
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

        if kind.family == FAMILY_UNKNOWN:
            # Refused once by _check_catalog_kinds. The managed checks below
            # would also ask for storage that a mistyped external catalog
            # (``lakekeper``) does not need, which reads as a second problem.
            continue

        if kind.snowflake_catalog_type == "iceberg_rest":
            # rest / lakekeeper / polaris / unity / nessie / bigquery. A
            # Lakekeeper warehouse is a catalog NAME, so the managed checks
            # below used to reject it with a false "needs an s3:// or gs://
            # warehouse" error while the emitter built a volume nobody used.
            warnings.append(
                f"expose '{eid}': catalog '{catalog}' is understood but FLUID does "
                "not emit its Snowflake CATALOG INTEGRATION yet, because that "
                "integration authenticates with an OAuth secret or bearer token "
                "and the emitted module is credential-free. Create it out of band."
            )
            continue

        if kind.snowflake_catalog_type is None:
            # hive / jdbc / hadoop / dynamodb: no CATALOG_SOURCE exists for
            # them, so there is nothing to defer. The emitter emits nothing.
            errors.append(
                f"expose '{eid}': Snowflake has no catalog integration for a "
                f"{kind.name} catalog (it integrates Glue, object storage, Open "
                "Catalog/Polaris and Iceberg REST catalogs), so no table Snowflake "
                "can read is reachable through it. Use catalog: glue, an Iceberg "
                "REST catalog (rest, lakekeeper, polaris, unity), or omit catalog "
                "for a Snowflake-managed table."
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
    """Mirror ``gcp._emit_iceberg_storage``'s skip branch.

    The BigLake errors (a warehouse that is not GCS, or no storage at all)
    hold only for a BigQuery-cataloged table, the absent-``catalog`` default
    included, because there dbt names the bucket and FLUID must create it.
    An external catalog (``lakekeeper``, ``rest``, ``nessie``...) owns its
    storage, and its warehouse is often a catalog NAME (``demo``), so those
    errors used to reject a correct contract. For it, only an object-store
    scheme other than ``gs://`` is wrong: a ``platform: gcp`` table cannot
    live in S3 or ADLS. The kind is read from ``location.catalog`` itself,
    not :func:`binding_catalog_kind`, whose non-AWS default is ``rest``.
    """
    from fluid_build.providers._iceberg_catalog import (
        FAMILY_UNKNOWN,
        canonical_catalog_kind,
        catalog_kind_info,
        iceberg_bucket_name,
        is_object_store_uri,
    )

    for eid, binding, loc in _iceberg_exposures(contract, "gcp"):
        if iceberg_bucket_name(binding):
            continue
        warehouse = str(loc.get("warehouse") or "")
        kind = canonical_catalog_kind(loc.get("catalog"))
        external = bool(kind) and kind != "bigquery"
        if external and catalog_kind_info(kind).family == FAMILY_UNKNOWN:
            # Refused once by _check_catalog_kinds.
            continue
        if warehouse.startswith("gs://"):
            errors.append(
                f"expose '{eid}': binding.location.warehouse is '{warehouse}', which "
                "names no bucket. Use gs://<bucket>/<optional-path> so FLUID can "
                "create the bucket dbt's catalogs.yml points at."
            )
        elif external:
            # gcs:// is the alternate GCS spelling the shared FileIO table
            # (_iceberg_catalog._OBJECT_STORE_FILE_IO) maps to GCSFileIO.
            if is_object_store_uri(warehouse) and not warehouse.lower().startswith(
                ("gs://", "gcs://")
            ):
                errors.append(
                    f"expose '{eid}': binding.location.warehouse is '{warehouse}', "
                    "which is not Google Cloud Storage. A platform: gcp Iceberg table "
                    f"in a {kind} catalog takes the catalog's warehouse name (the "
                    "catalog owns the storage) or a gs://<bucket>/<optional-path> "
                    "location FLUID creates the bucket for."
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
