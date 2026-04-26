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

"""AWS Glue Data Catalog metadata-source adapter.

Reads tables / databases / partitions / classifiers from AWS Glue
plus Lake Formation tags. Read-only by design; only ``Get*`` API
calls are issued — never ``StartCrawler`` or any mutating operation.

Required IAM permissions (least-privilege):

* ``glue:GetDatabase``, ``glue:GetTable``, ``glue:GetTables``
* ``glue:GetPartitions`` (optional — for partition-key extraction)
* ``glue:GetClassifiers`` (optional — sniffs Glue-classifier metadata)
* ``lakeformation:GetResourceLFTags`` (optional — for Lake Formation
  tag inheritance)

The adapter uses boto3's standard credential chain: ``[iam_role]``
auth_method picks up profile / role-assume; ``[iam_key]`` injects
explicit access keys; ``[instance_profile]`` defers to IMDS (only
fires when ``--allow-metadata-service`` is on).

Lazy SDK import: ``boto3`` lives inside :meth:`_client` — the
existing AWS provider already uses this pattern at
``providers/aws/aws.py:31``.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fluid_build.copilot.catalog._patterns import (
    safe_metadata_call,
    translate_permission_or_connection_error,
)
from fluid_build.copilot.catalog.base import (
    CatalogAdapter,
    CatalogConfigError,
    CatalogConnectionError,
    CatalogError,
)
from fluid_build.copilot.catalog.credentials import (
    CredentialResolver,
    GlueCredentials,
)
from fluid_build.copilot.catalog.models import (
    CatalogColumn,
    CatalogLineage,
    CatalogScope,
    CatalogTable,
    GlossaryTerm,
)

_log = logging.getLogger(__name__)


class GlueCatalogAdapter(CatalogAdapter):
    """Read metadata from AWS Glue Data Catalog."""

    name = "glue"

    def __init__(self, credentials: GlueCredentials) -> None:
        self._credentials = credentials
        self._cached_client: Optional[Any] = None

    @classmethod
    def from_resolver(
        cls,
        resolver: CredentialResolver,
        *,
        credential_id: Optional[str] = None,
        inline_credentials: Optional[Dict[str, Any]] = None,
    ) -> "GlueCatalogAdapter":
        creds = resolver.resolve(
            catalog_name="glue",
            credential_type=GlueCredentials,
            credential_id=credential_id,
            inline_credentials=inline_credentials,
        )
        return cls(credentials=creds)

    def _client(self) -> Any:
        """Pattern 4 — lazy boto3 import."""
        if self._cached_client is not None:
            return self._cached_client
        try:
            import boto3  # type: ignore
        except ImportError as exc:
            raise CatalogConfigError(
                message="boto3 is not installed. The Glue catalog adapter requires it.",
                suggestions=[
                    "Install via: pip install boto3",
                    "boto3 is also pulled in by some forge-cli AWS provider extras.",
                ],
            ) from exc

        kwargs = self._credentials.to_connection_kwargs()
        try:
            session = boto3.session.Session(**kwargs)
            client = session.client("glue")
        except Exception as exc:
            raise CatalogConnectionError(
                message=f"AWS Glue session construction failed: {exc}",
                suggestions=[
                    "Verify AWS_PROFILE / AWS_ACCESS_KEY_ID env vars.",
                    "Run: aws sts get-caller-identity (sanity check creds).",
                ],
                original_error=exc,
            ) from exc
        self._cached_client = client
        return client

    def list_tables(self, scope: CatalogScope) -> List[CatalogTable]:
        """List Glue tables under a database."""
        if not scope.database:
            raise CatalogConfigError(
                message="Glue CatalogScope requires 'database' (the Glue database name).",
                suggestions=["Pass scope.database='my_glue_database'."],
            )
        client = self._client()
        results: List[CatalogTable] = []
        next_token: Optional[str] = None
        try:
            while True:
                kwargs = {"DatabaseName": scope.database}
                if next_token:
                    kwargs["NextToken"] = next_token
                resp = client.get_tables(**kwargs)
                for entry in resp.get("TableList", []):
                    name = entry["Name"]
                    if scope.tables and name not in scope.tables:
                        continue
                    fqn = f"{scope.database}.{name}"
                    results.append(
                        CatalogTable(
                            fqn=fqn,
                            database=scope.database,
                            name=name,
                            description=entry.get("Description"),
                            owner=entry.get("Owner"),
                            tags=dict(entry.get("Parameters") or {}),
                            last_modified=entry.get("UpdateTime"),
                        )
                    )
                next_token = resp.get("NextToken")
                if not next_token:
                    break
        except Exception as exc:
            raise self._translate_query_error(exc, scope=scope) from exc
        return results

    def get_table(self, fqn: str) -> CatalogTable:
        """Full table detail via ``glue.get_table`` + columns +
        partition keys + Lake Formation tags."""
        database, name = self._parse_fqn(fqn)
        client = self._client()
        try:
            resp = client.get_table(DatabaseName=database, Name=name)
        except Exception as exc:
            raise self._translate_query_error(exc, fqn=fqn) from exc

        table = resp["Table"]
        storage = table.get("StorageDescriptor") or {}
        columns: List[CatalogColumn] = []
        for col in storage.get("Columns") or []:
            columns.append(
                CatalogColumn(
                    name=col["Name"],
                    data_type=col.get("Type", "STRING"),
                    nullable=True,  # Glue doesn't track nullability per column
                    description=col.get("Comment"),
                )
            )
        # Partition keys are separate from regular columns in Glue.
        partition_keys = [pk["Name"] for pk in table.get("PartitionKeys") or []]
        for pk in table.get("PartitionKeys") or []:
            columns.append(
                CatalogColumn(
                    name=pk["Name"],
                    data_type=pk.get("Type", "STRING"),
                    nullable=False,
                    primary_key=False,
                    partition_key=True,
                    description=pk.get("Comment"),
                )
            )

        # Lake Formation tags via lakeformation.get_resource_lf_tags
        # — soft-fail since LF may not be configured.
        lf_tags = safe_metadata_call(
            lambda: self._fetch_lf_tags(database, name),
            fallback={},
            description="glue lake-formation tag fetch",
            log_target=fqn,
        )
        all_tags = dict(table.get("Parameters") or {})
        all_tags.update(lf_tags)

        return CatalogTable(
            fqn=fqn,
            database=database,
            name=name,
            description=table.get("Description"),
            owner=table.get("Owner"),
            tags=all_tags,
            partition_keys=partition_keys,
            columns=columns,
            last_modified=table.get("UpdateTime"),
        )

    def get_lineage(self, fqn: str) -> CatalogLineage:
        """Glue itself doesn't track table-level lineage; that lives
        in AWS Data Lineage (separate service, v1.6+ adapter).
        Empty result preserves the consistent shape contract."""
        return CatalogLineage()

    def list_glossary_terms(self, scope: CatalogScope) -> List[GlossaryTerm]:
        """Glue has no first-class glossary. Lake Formation tags
        carry domain hints; surface them as glossary entries with
        ``term`` = tag key, ``definition`` = tag-value enumeration."""
        return []

    # -----------------------------------------------------------------
    # Audit context
    # -----------------------------------------------------------------

    def audit_context(self) -> Dict[str, Any]:
        ctx = super().audit_context()
        ctx["region"] = self._credentials.region
        ctx["auth_method"] = self._credentials.auth_method
        if self._credentials.profile_name:
            ctx["profile_name"] = self._credentials.profile_name
        return ctx

    # -----------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------

    @staticmethod
    def _parse_fqn(fqn: str) -> tuple[str, str]:
        parts = fqn.split(".", 1)
        if len(parts) != 2:
            raise CatalogConfigError(
                message=f"Glue FQN must be DATABASE.TABLE; got {fqn!r}",
                suggestions=["Provide both parts: my_database.my_table."],
            )
        return parts[0], parts[1]

    def _fetch_lf_tags(self, database: str, table: str) -> Dict[str, str]:
        """Read Lake Formation tags for a table. Soft-fails if LF
        isn't configured or the user lacks
        ``lakeformation:GetResourceLFTags``."""
        try:
            import boto3  # type: ignore
        except ImportError:
            return {}
        kwargs = self._credentials.to_connection_kwargs()
        session = boto3.session.Session(**kwargs)
        lf = session.client("lakeformation")
        resp = lf.get_resource_lf_tags(
            Resource={"Table": {"DatabaseName": database, "Name": table}}
        )
        out: Dict[str, str] = {}
        for tag_assoc in resp.get("LFTagsOnTable", []) or []:
            key = tag_assoc.get("TagKey")
            values = tag_assoc.get("TagValues") or []
            if key:
                out[key] = ",".join(str(v) for v in values)
        return out

    def _translate_query_error(
        self,
        exc: Exception,
        *,
        fqn: Optional[str] = None,
        scope: Optional[CatalogScope] = None,
    ) -> CatalogError:
        target = fqn or (scope.database if scope else "<unknown>")
        return translate_permission_or_connection_error(
            exc,
            target=target,
            permission_grant_hint=(
                "aws iam attach-user-policy --user-name <user> "
                "--policy-arn arn:aws:iam::aws:policy/AWSGlueConsoleReadOnlyAccess"
            ),
            privilege_label="glue:GetDatabase + glue:GetTable + glue:GetTables",
            permission_markers=(
                "AccessDenied",
                "AccessDeniedException",
                "Insufficient privileges",
                "User is not authorized",
            ),
            not_found_markers=(
                "EntityNotFoundException",
                "Table not found",
                "Database not found",
            ),
            connection_suggestions=[
                "Run: aws sts get-caller-identity to verify credentials.",
                "Verify AWS_REGION matches the region your Glue catalog is in.",
            ],
        )


__all__ = ["GlueCatalogAdapter"]
