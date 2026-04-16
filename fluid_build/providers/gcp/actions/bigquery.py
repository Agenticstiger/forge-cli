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

# fluid_build/providers/gcp/actions/bigquery.py
"""
BigQuery actions for GCP provider.

Implements idempotent BigQuery operations including:
- Dataset creation and management
- Table creation with schema evolution
- View creation and updates
- Routine (UDF/stored procedure) management
"""

import time
from typing import Any, Dict, List

from fluid_build.cli.console import cprint, success, warning
from fluid_build.providers.base import ProviderError

from ..util.logging import duration_ms
from ..util.names import normalize_dataset_name, normalize_table_name


def ensure_dataset(action: Dict[str, Any]) -> Dict[str, Any]:
    """
    Ensure BigQuery dataset exists with specified configuration.

    Creates dataset if it doesn't exist, updates configuration if changed.
    Idempotent operation - safe to run multiple times.

    Args:
        action: Dataset action configuration

    Returns:
        Action result with status and details
    """
    start_time = time.time()

    try:
        from google.cloud import bigquery
        from google.cloud.exceptions import Conflict, NotFound
    except ImportError:
        return {
            "status": "error",
            "error": "google-cloud-bigquery library not available. Install with: pip install google-cloud-bigquery",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    project = action.get("project")
    dataset = action.get("dataset")
    location = action.get("location", "US")
    description = action.get("description", "")
    labels = action.get("labels", {})

    if not project or not dataset:
        return {
            "status": "error",
            "error": "Both 'project' and 'dataset' are required",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    try:
        # Initialize BigQuery client
        client = bigquery.Client(project=project)

        # Normalize dataset name
        normalized_dataset = normalize_dataset_name(dataset)
        dataset_id = f"{project}.{normalized_dataset}"

        changed = False

        try:
            # Check if dataset exists
            existing_dataset = client.get_dataset(dataset_id)

            # Compare and update if necessary
            update_needed = False

            if existing_dataset.description != description:
                existing_dataset.description = description
                update_needed = True

            if existing_dataset.location != location:
                # Location cannot be changed after creation
                return {
                    "status": "error",
                    "error": f"Dataset location cannot be changed from {existing_dataset.location} to {location}",
                    "duration_ms": duration_ms(start_time),
                    "changed": False,
                }

            # Update labels if different
            existing_labels = existing_dataset.labels or {}
            if existing_labels != labels:
                existing_dataset.labels = labels
                update_needed = True

            if update_needed:
                client.update_dataset(existing_dataset, ["description", "labels"])
                changed = True

            return {
                "status": "changed" if changed else "ok",
                "dataset_id": dataset_id,
                "location": existing_dataset.location,
                "description": existing_dataset.description,
                "labels": existing_dataset.labels,
                "created": existing_dataset.created,
                "duration_ms": duration_ms(start_time),
                "changed": changed,
            }

        except NotFound:
            # Dataset doesn't exist, create it
            dataset_obj = bigquery.Dataset(dataset_id)
            dataset_obj.location = location
            dataset_obj.description = description
            dataset_obj.labels = labels

            # Set default table expiration if specified
            default_table_expiration_ms = action.get("default_table_expiration_ms")
            if default_table_expiration_ms:
                dataset_obj.default_table_expiration_ms = default_table_expiration_ms

            # Set default partition expiration if specified
            default_partition_expiration_ms = action.get("default_partition_expiration_ms")
            if default_partition_expiration_ms:
                dataset_obj.default_partition_expiration_ms = default_partition_expiration_ms

            created_dataset = client.create_dataset(dataset_obj)

            return {
                "status": "changed",
                "dataset_id": created_dataset.dataset_id,
                "location": created_dataset.location,
                "description": created_dataset.description,
                "labels": created_dataset.labels,
                "created": created_dataset.created,
                "duration_ms": duration_ms(start_time),
                "changed": True,
            }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "dataset_id": dataset_id if "dataset_id" in locals() else None,
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }


def ensure_table(action: Dict[str, Any]) -> Dict[str, Any]:
    """
    Ensure BigQuery table exists with specified schema.

    Creates table if it doesn't exist, updates schema if changed (additive only).
    Supports partitioning, clustering, and other table configurations.

    Args:
        action: Table action configuration

    Returns:
        Action result with status and details
    """
    start_time = time.time()

    try:
        from google.cloud import bigquery
        from google.cloud.exceptions import NotFound
    except ImportError:
        return {
            "status": "error",
            "error": "google-cloud-bigquery library not available. Install with: pip install google-cloud-bigquery",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    project = action.get("project")
    dataset = action.get("dataset")
    table = action.get("table")
    schema = action.get("schema", [])
    description = action.get("description", "")
    labels = action.get("labels", {})
    location = action.get("location", "US")

    if not all([project, dataset, table]):
        return {
            "status": "error",
            "error": "Project, dataset, and table are all required",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    try:
        # Initialize BigQuery client
        client = bigquery.Client(project=project)

        # Normalize names
        normalized_dataset = normalize_dataset_name(dataset)
        normalized_table = normalize_table_name(table)
        table_id = f"{project}.{normalized_dataset}.{normalized_table}"

        # PHASE 1: Ensure policy tags exist in Data Catalog (if schema has policy tags)
        taxonomy_uris = {}
        if schema and project and location:
            cprint("\n🏷️  Phase 1: Ensuring policy tags...")
            taxonomy_uris = _ensure_policy_tags(project, location, schema)

        changed = False

        try:
            # Check if table exists
            existing_table = client.get_table(table_id)

            # Compare schema and update if necessary
            if schema:
                bq_schema = _convert_schema_to_bq(schema, project, location, taxonomy_uris)
                schema_changed = _is_schema_different(existing_table.schema, bq_schema)

                # Check if policy tags are present in new schema
                has_policy_tags = any(
                    field.policy_tags and field.policy_tags.names for field in bq_schema
                )

                # If policy tags are present, always update (they're metadata, not structure)
                if has_policy_tags and taxonomy_uris:
                    existing_table.schema = bq_schema
                    client.update_table(existing_table, ["schema"])
                    changed = True
                    cprint("   ✅ Updated table schema with policy tags")
                elif schema_changed:
                    # For structural changes, only allow additive changes
                    if _is_additive_schema_change(existing_table.schema, bq_schema):
                        existing_table.schema = bq_schema
                        client.update_table(existing_table, ["schema"])
                        changed = True
                    else:
                        return {
                            "status": "error",
                            "error": "Non-additive schema changes not allowed. Use contract-tests to validate breaking changes.",
                            "duration_ms": duration_ms(start_time),
                            "changed": False,
                        }

            # Update description and labels if different
            update_fields = []

            if existing_table.description != description:
                existing_table.description = description
                update_fields.append("description")
                changed = True

            existing_labels = existing_table.labels or {}
            if existing_labels != labels:
                existing_table.labels = labels
                update_fields.append("labels")
                changed = True

            if update_fields:
                client.update_table(existing_table, update_fields)

            # PHASE 2: Apply data masking policies (for existing table)
            contract = action.get("contract", {})
            if contract:
                policy = contract.get("policy", {})
                privacy = policy.get("privacy", {})
                masking_rules = privacy.get("masking", [])

                if masking_rules:
                    cprint("\n🔒 Phase 2: Applying data masking policies...")
                    _apply_data_masking(
                        project, location, normalized_dataset, normalized_table, masking_rules
                    )

                # PHASE 3: Apply column access restrictions (for existing table)
                authz = policy.get("authz", {})
                column_restrictions = authz.get("columnRestrictions", [])

                if column_restrictions:
                    cprint("\n🛡️  Phase 3: Applying column access restrictions...")
                    _apply_column_restrictions(
                        project, normalized_dataset, normalized_table, schema, column_restrictions
                    )

            return {
                "status": "changed" if changed else "ok",
                "table_id": table_id,
                "schema_fields": len(existing_table.schema),
                "num_rows": existing_table.num_rows,
                "num_bytes": existing_table.num_bytes,
                "created": existing_table.created,
                "modified": existing_table.modified,
                "duration_ms": duration_ms(start_time),
                "changed": changed,
            }

        except NotFound:
            # Table doesn't exist, create it
            table_obj = bigquery.Table(table_id)
            table_obj.description = description
            table_obj.labels = labels

            # Set schema if provided (with policy tags)
            if schema:
                table_obj.schema = _convert_schema_to_bq(schema, project, location, taxonomy_uris)

            # Configure partitioning if specified
            partitioning = action.get("partitioning")
            if partitioning:
                table_obj.time_partitioning = _configure_partitioning(partitioning)

            # Configure clustering if specified
            clustering = action.get("clustering")
            if clustering:
                table_obj.clustering_fields = clustering

            # Set table expiration if specified
            expires = action.get("expires")
            if expires:
                from datetime import datetime

                table_obj.expires = datetime.fromisoformat(expires.replace("Z", "+00:00"))

            created_table = client.create_table(table_obj)

            # PHASE 2: Apply data masking policies (for new table)
            contract = action.get("contract", {})
            if contract:
                policy = contract.get("policy", {})
                privacy = policy.get("privacy", {})
                masking_rules = privacy.get("masking", [])

                if masking_rules:
                    cprint("\n🔒 Phase 2: Applying data masking policies...")
                    _apply_data_masking(
                        project, location, normalized_dataset, normalized_table, masking_rules
                    )

                # PHASE 3: Apply column access restrictions (for new table)
                authz = policy.get("authz", {})
                column_restrictions = authz.get("columnRestrictions", [])

                if column_restrictions:
                    cprint("\n🛡️  Phase 3: Applying column access restrictions...")
                    _apply_column_restrictions(
                        project, normalized_dataset, normalized_table, schema, column_restrictions
                    )

            return {
                "status": "changed",
                "table_id": created_table.table_id,
                "schema_fields": len(created_table.schema),
                "created": created_table.created,
                "duration_ms": duration_ms(start_time),
                "changed": True,
            }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "table_id": table_id if "table_id" in locals() else None,
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }


def ensure_view(action: Dict[str, Any]) -> Dict[str, Any]:
    """
    Ensure BigQuery view exists with specified query.

    Creates view if it doesn't exist, updates query if changed.

    Args:
        action: View action configuration

    Returns:
        Action result with status and details
    """
    start_time = time.time()

    try:
        from google.cloud import bigquery
        from google.cloud.exceptions import NotFound
    except ImportError:
        return {
            "status": "error",
            "error": "google-cloud-bigquery library not available. Install with: pip install google-cloud-bigquery",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    project = action.get("project")
    dataset = action.get("dataset")
    view = action.get("view")
    query = action.get("query")
    description = action.get("description", "")
    labels = action.get("labels", {})

    if not all([project, dataset, view, query]):
        return {
            "status": "error",
            "error": "Project, dataset, view, and query are all required",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    try:
        # Initialize BigQuery client
        client = bigquery.Client(project=project)

        # Normalize names
        normalized_dataset = normalize_dataset_name(dataset)
        normalized_view = normalize_table_name(view)  # Views use same naming rules as tables
        view_id = f"{project}.{normalized_dataset}.{normalized_view}"

        changed = False

        try:
            # Check if view exists
            existing_view = client.get_table(view_id)

            if not existing_view.table_type == "VIEW":
                return {
                    "status": "error",
                    "error": f"Object {view_id} exists but is not a view",
                    "duration_ms": duration_ms(start_time),
                    "changed": False,
                }

            # Compare and update if necessary
            update_fields = []

            # Check if query changed (normalize whitespace for comparison)
            existing_query = " ".join(existing_view.view_query.split())
            new_query = " ".join(query.split())

            if existing_query != new_query:
                existing_view.view_query = query
                update_fields.append("view_query")
                changed = True

            if existing_view.description != description:
                existing_view.description = description
                update_fields.append("description")
                changed = True

            existing_labels = existing_view.labels or {}
            if existing_labels != labels:
                existing_view.labels = labels
                update_fields.append("labels")
                changed = True

            if update_fields:
                client.update_table(existing_view, update_fields)

            return {
                "status": "changed" if changed else "ok",
                "view_id": view_id,
                "query_length": len(query),
                "created": existing_view.created,
                "modified": existing_view.modified,
                "duration_ms": duration_ms(start_time),
                "changed": changed,
            }

        except NotFound:
            # View doesn't exist, create it
            view_obj = bigquery.Table(view_id)
            view_obj.view_query = query
            view_obj.description = description
            view_obj.labels = labels

            created_view = client.create_table(view_obj)

            return {
                "status": "changed",
                "view_id": created_view.table_id,
                "query_length": len(query),
                "created": created_view.created,
                "duration_ms": duration_ms(start_time),
                "changed": True,
            }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "view_id": view_id if "view_id" in locals() else None,
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }


def ensure_routine(action: Dict[str, Any]) -> Dict[str, Any]:
    """
    Ensure BigQuery routine (UDF/stored procedure) exists.

    Creates routine if it doesn't exist, updates definition if changed.

    Args:
        action: Routine action configuration

    Returns:
        Action result with status and details
    """
    start_time = time.time()

    try:
        from google.cloud import bigquery
        from google.cloud.exceptions import NotFound
    except ImportError:
        return {
            "status": "error",
            "error": "google-cloud-bigquery library not available. Install with: pip install google-cloud-bigquery",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    project = action.get("project")
    dataset = action.get("dataset")
    routine_name = action.get("routine")
    routine_type = action.get("routine_type", "SCALAR_FUNCTION")
    language = action.get("language", "SQL")
    definition = action.get("definition")
    arguments = action.get("arguments", [])
    return_type = action.get("return_type")

    if not all([project, dataset, routine_name, definition]):
        return {
            "status": "error",
            "error": "Project, dataset, routine, and definition are all required",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    try:
        # Initialize BigQuery client
        client = bigquery.Client(project=project)

        # Normalize names
        normalized_dataset = normalize_dataset_name(dataset)
        routine_id = f"{project}.{normalized_dataset}.{routine_name}"

        changed = False

        try:
            # Check if routine exists
            existing_routine = client.get_routine(routine_id)

            # Compare and update if necessary
            update_needed = False

            # Check definition
            if existing_routine.body != definition:
                existing_routine.body = definition
                update_needed = True

            # Check return type
            if return_type and existing_routine.return_type != _parse_bq_type(return_type):
                existing_routine.return_type = _parse_bq_type(return_type)
                update_needed = True

            # Check arguments
            if arguments:
                expected_args = [_convert_argument_to_bq(arg) for arg in arguments]
                if existing_routine.arguments != expected_args:
                    existing_routine.arguments = expected_args
                    update_needed = True

            if update_needed:
                client.update_routine(existing_routine, ["body", "return_type", "arguments"])
                changed = True

            return {
                "status": "changed" if changed else "ok",
                "routine_id": routine_id,
                "routine_type": existing_routine.type_,
                "language": existing_routine.language,
                "created": existing_routine.created_time,
                "modified": existing_routine.last_modified_time,
                "duration_ms": duration_ms(start_time),
                "changed": changed,
            }

        except NotFound:
            # Routine doesn't exist, create it
            routine_obj = bigquery.Routine(routine_id)
            routine_obj.type_ = routine_type
            routine_obj.language = language
            routine_obj.body = definition

            if return_type:
                routine_obj.return_type = _parse_bq_type(return_type)

            if arguments:
                routine_obj.arguments = [_convert_argument_to_bq(arg) for arg in arguments]

            created_routine = client.create_routine(routine_obj)

            return {
                "status": "changed",
                "routine_id": created_routine.routine_id,
                "routine_type": created_routine.type_,
                "language": created_routine.language,
                "created": created_routine.created_time,
                "duration_ms": duration_ms(start_time),
                "changed": True,
            }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "routine_id": routine_id if "routine_id" in locals() else None,
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }


def execute_sql(action: Dict[str, Any]) -> Dict[str, Any]:
    """
    Execute SQL statement against BigQuery.

    Supports both DDL and DML operations with optional dry run.

    Args:
        action: SQL execution action configuration

    Returns:
        Action result with execution details
    """
    start_time = time.time()

    try:
        from google.cloud import bigquery
    except ImportError:
        return {
            "status": "error",
            "error": "google-cloud-bigquery library not available. Install with: pip install google-cloud-bigquery",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    project = action.get("project")
    sql = action.get("sql")
    location = action.get("location", "US")
    dry_run = action.get("dry_run", False)
    use_legacy_sql = action.get("use_legacy_sql", False)
    timeout = action.get("timeout", 600)  # 10 minutes default

    if not all([project, sql]):
        return {
            "status": "error",
            "error": "Both 'project' and 'sql' are required",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    try:
        # Initialize BigQuery client
        client = bigquery.Client(project=project)

        # Configure job
        job_config = bigquery.QueryJobConfig()
        job_config.dry_run = dry_run
        job_config.use_legacy_sql = use_legacy_sql

        # Execute query
        query_job = client.query(sql, job_config=job_config, location=location)

        if dry_run:
            # For dry run, return validation results
            return {
                "status": "ok",
                "dry_run": True,
                "bytes_processed": query_job.total_bytes_processed,
                "bytes_billed": query_job.total_bytes_billed,
                "cache_hit": query_job.cache_hit,
                "duration_ms": duration_ms(start_time),
                "changed": False,
            }
        else:
            # Wait for job completion with timeout
            try:
                results = query_job.result(timeout=timeout)

                return {
                    "status": "ok",
                    "job_id": query_job.job_id,
                    "bytes_processed": query_job.total_bytes_processed,
                    "bytes_billed": query_job.total_bytes_billed,
                    "cache_hit": query_job.cache_hit,
                    "num_dml_affected_rows": query_job.num_dml_affected_rows,
                    "rows_returned": results.total_rows if results else 0,
                    "duration_ms": duration_ms(start_time),
                    "changed": True,  # Assume SQL execution makes changes
                }

            except Exception as e:
                return {
                    "status": "error",
                    "error": f"Query execution failed: {str(e)}",
                    "job_id": query_job.job_id if query_job else None,
                    "duration_ms": duration_ms(start_time),
                    "changed": False,
                }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }


# Helper functions


def _convert_schema_to_bq(
    schema: List[Dict[str, Any]],
    project: str = None,
    location: str = None,
    taxonomy_uris: Dict[str, Dict[str, str]] = None,
) -> List:
    """
    Convert FLUID schema to BigQuery SchemaField objects.

    Enhanced in v0.5.7 to support:
    - Default values from column labels
    - Policy tags from column labels (with full Data Catalog URIs if available)
    - Constraint hints in descriptions

    Args:
        schema: List of field definitions from contract
        project: GCP project (needed for policy tag resolution)
        location: Location (needed for policy tag resolution)
        taxonomy_uris: Pre-resolved policy tag URIs from _ensure_policy_tags
    """
    try:
        from google.cloud import bigquery
    except ImportError:
        raise ProviderError("google-cloud-bigquery library not available")

    bq_fields = []

    for field in schema:
        name = field.get("name")
        field_type = field.get("type", "STRING")

        # Determine mode (REQUIRED/NULLABLE/REPEATED)
        if "mode" in field:
            mode = field.get("mode")
        else:
            mode = "REQUIRED" if field.get("required") else "NULLABLE"

        description = field.get("description", "")
        labels = field.get("labels", {})

        # Extract default value from labels (v0.5.7 feature)
        default_value = labels.get("default")

        # Extract policy tags from labels (v0.5.7 feature) - ENHANCED WITH FULL IMPLEMENTATION
        policy_tags = None
        if "policyTag" in labels and "taxonomy" in labels:
            policy_tag_name = labels["policyTag"]
            taxonomy_name = labels["taxonomy"]

            # If we have resolved taxonomy URIs, attach the full policy tag
            if taxonomy_uris and taxonomy_name in taxonomy_uris:
                if policy_tag_name in taxonomy_uris[taxonomy_name]:
                    policy_tag_uri = taxonomy_uris[taxonomy_name][policy_tag_name]
                    try:
                        policy_tags = bigquery.PolicyTagList(names=[policy_tag_uri])
                        # Also add to description for visibility
                        description = f"{description}\n[Policy Tag: {taxonomy_name}.{policy_tag_name}]".strip()
                    except Exception:
                        # Fallback to description only
                        description = f"{description}\n[Policy Tag: {taxonomy_name}.{policy_tag_name}]".strip()
            else:
                # No URIs available, just document in description
                description = (
                    f"{description}\n[Policy Tag: {taxonomy_name}.{policy_tag_name}]".strip()
                )

        elif "datacatalog_tag" in labels:
            # Direct BigQuery Data Catalog URI provided
            try:
                policy_tags = bigquery.PolicyTagList(names=[labels["datacatalog_tag"]])
            except Exception:
                # If PolicyTagList not available or fails, continue without it
                pass

        # Enhance description with constraint hints (v0.5.7 feature)
        semantic_type = field.get("semanticType", "")
        constraint = labels.get("constraint", "")

        if semantic_type in ["identifier", "primary_key", "id"] or constraint == "primary_key":
            description = f"{description}\n[PRIMARY KEY]".strip()

        if labels.get("unique") == "true" or constraint == "unique":
            description = f"{description}\n[UNIQUE]".strip()

        if "foreign_key_table" in labels:
            fk_table = labels["foreign_key_table"]
            fk_column = labels.get("foreign_key_column", name)
            description = f"{description}\n[FOREIGN KEY -> {fk_table}({fk_column})]".strip()

        # Handle nested fields for RECORD types
        fields = field.get("fields", [])
        nested_fields = (
            _convert_schema_to_bq(fields, project, location, taxonomy_uris) if fields else ()
        )

        # Build BigQuery field with all enhancements
        try:
            bq_field = bigquery.SchemaField(
                name=name,
                field_type=field_type,
                mode=mode,
                description=description,
                fields=nested_fields,
                policy_tags=policy_tags,
                default_value_expression=default_value,
            )
        except TypeError:
            # Older google-cloud-bigquery version doesn't support policy_tags or default_value_expression
            bq_field = bigquery.SchemaField(
                name=name,
                field_type=field_type,
                mode=mode,
                description=description,
                fields=nested_fields,
            )

        bq_fields.append(bq_field)

    return bq_fields


def _is_schema_different(existing_schema: List, new_schema: List) -> bool:
    """Compare two BigQuery schemas for differences."""
    if len(existing_schema) != len(new_schema):
        return True

    # Create lookup dictionaries for comparison
    existing_fields = {field.name: field for field in existing_schema}
    new_fields = {field.name: field for field in new_schema}

    # Check for field differences
    for name, new_field in new_fields.items():
        if name not in existing_fields:
            return True  # New field added

        existing_field = existing_fields[name]

        # Compare field properties
        if (
            existing_field.field_type != new_field.field_type
            or existing_field.mode != new_field.mode
            or existing_field.description != new_field.description
        ):
            return True

    return False


def _is_additive_schema_change(existing_schema: List, new_schema: List) -> bool:
    """Check if schema change is additive (safe to apply)."""
    existing_fields = {field.name: field for field in existing_schema}

    # Type aliases - BigQuery SQL standard vs legacy names
    TYPE_EQUIVALENTS = {
        ("FLOAT", "FLOAT64"),
        ("INTEGER", "INT64"),
        ("BOOLEAN", "BOOL"),
        ("BYTES", "BINARY"),
    }

    def types_are_equivalent(type1: str, type2: str) -> bool:
        """Check if two types are equivalent (e.g., FLOAT and FLOAT64)."""
        if type1 == type2:
            return True
        return (type1, type2) in TYPE_EQUIVALENTS or (type2, type1) in TYPE_EQUIVALENTS

    for new_field in new_schema:
        if new_field.name in existing_fields:
            existing_field = existing_fields[new_field.name]

            # Field exists - check if properties changed
            if (
                not types_are_equivalent(existing_field.field_type, new_field.field_type)
                or existing_field.mode != new_field.mode
            ):
                return False  # Non-additive change

        # New fields are always additive if NULLABLE
        elif new_field.mode == "REQUIRED":
            return False  # Adding required field is not additive

    return True


def _configure_partitioning(partitioning_config: Dict[str, Any]):
    """Configure BigQuery table partitioning."""
    try:
        from google.cloud import bigquery
    except ImportError:
        raise ProviderError("google-cloud-bigquery library not available")

    partition_type = partitioning_config.get("type", "TIME")
    field = partitioning_config.get("field")

    if partition_type == "TIME":
        return bigquery.TimePartitioning(
            type_=bigquery.TimePartitioningType.DAY,
            field=field,
        )
    elif partition_type == "RANGE":
        range_config = partitioning_config.get("range", {})
        return bigquery.RangePartitioning(
            field=field,
            range_=bigquery.PartitionRange(
                start=range_config.get("start"),
                end=range_config.get("end"),
                interval=range_config.get("interval", 1),
            ),
        )
    else:
        raise ProviderError(f"Unsupported partitioning type: {partition_type}")


def _parse_bq_type(type_str: str):
    """Parse BigQuery type string to StandardSqlDataType."""
    try:
        from google.cloud import bigquery
        from google.cloud.bigquery import StandardSqlDataType, StandardSqlTypeNames
    except ImportError:
        raise ProviderError("google-cloud-bigquery library not available")

    # Simple type mapping - extend as needed
    type_mapping = {
        "STRING": StandardSqlTypeNames.STRING,
        "INT64": StandardSqlTypeNames.INT64,
        "FLOAT64": StandardSqlTypeNames.FLOAT64,
        "BOOL": StandardSqlTypeNames.BOOL,
        "DATE": StandardSqlTypeNames.DATE,
        "TIMESTAMP": StandardSqlTypeNames.TIMESTAMP,
    }

    type_name = type_mapping.get(type_str.upper())
    if type_name:
        return StandardSqlDataType(type_kind=type_name)
    else:
        raise ProviderError(f"Unsupported BigQuery type: {type_str}")


def _convert_argument_to_bq(arg_config: Dict[str, Any]):
    """Convert argument configuration to BigQuery RoutineArgument."""
    try:
        from google.cloud import bigquery
    except ImportError:
        raise ProviderError("google-cloud-bigquery library not available")

    return bigquery.RoutineArgument(
        name=arg_config.get("name"),
        data_type=_parse_bq_type(arg_config.get("type", "STRING")),
        kind=arg_config.get("kind", "FIXED_TYPE"),
        mode=arg_config.get("mode", "IN"),
    )


# ============================================================================
# PHASE 1: Policy Tags Implementation
# ============================================================================


def _ensure_policy_tags(
    project: str, location: str, schema: List[Dict[str, Any]]
) -> Dict[str, Dict[str, str]]:
    """
    Create Data Catalog taxonomies and policy tags from schema.

    Extracts policy tag references from field labels and ensures they exist
    in Google Cloud Data Catalog. Creates taxonomies and tags as needed.

    Args:
        project: GCP project ID
        location: Data Catalog location (e.g., "us", "eu")
        schema: List of field configurations from contract

    Returns:
        Dictionary mapping taxonomy names to policy tag URIs
        Format: {taxonomy_name: {tag_name: full_uri}}
    """
    try:
        from google.cloud import datacatalog_v1
    except ImportError:
        # Graceful degradation - just log warning and continue
        warning(
            "google-cloud-datacatalog not installed, policy tags will be documented in descriptions only"
        )
        return {}

    try:
        dc_client = datacatalog_v1.PolicyTagManagerClient()

        # Normalize location to lowercase and map to Data Catalog regions
        # Data Catalog uses multi-region locations (us, eu, asia-northeast1, etc.)
        location = location.lower()

        # Map specific BigQuery regions to Data Catalog multi-regions
        datacatalog_location = location
        if location.startswith("europe-") or location.startswith("eu-"):
            datacatalog_location = "eu"
        elif location.startswith("us-") or location == "us":
            datacatalog_location = "us"
        elif location.startswith("asia-"):
            # Data Catalog supports specific Asia regions
            datacatalog_location = location

        cprint(
            f"   Using Data Catalog location: {datacatalog_location} (from BigQuery location: {location})"
        )

        # Step 1: Collect unique taxonomies and tags from schema
        taxonomies = {}
        for field in schema:
            labels = field.get("labels", {})
            if "policyTag" in labels and "taxonomy" in labels:
                taxonomy_name = labels["taxonomy"]
                policy_tag_name = labels["policyTag"]

                if taxonomy_name not in taxonomies:
                    taxonomies[taxonomy_name] = set()
                taxonomies[taxonomy_name].add(policy_tag_name)

        if not taxonomies:
            return {}

        # Step 2: List existing taxonomies first
        taxonomy_uris = {}
        parent = f"projects/{project}/locations/{datacatalog_location}"

        # List all existing taxonomies
        existing_taxonomies = {}
        try:
            for taxonomy in dc_client.list_taxonomies(parent=parent):
                existing_taxonomies[taxonomy.display_name] = taxonomy
        except Exception as e:
            cprint(f"   ℹ️  Could not list existing taxonomies: {e}")

        for taxonomy_name, tags in taxonomies.items():
            try:
                # Check if taxonomy already exists by display name
                if taxonomy_name in existing_taxonomies:
                    taxonomy = existing_taxonomies[taxonomy_name]
                    success(f"Found existing taxonomy: {taxonomy_name}")
                else:
                    # Create new taxonomy
                    taxonomy = datacatalog_v1.Taxonomy(
                        display_name=taxonomy_name,
                        description=f"FLUID policy taxonomy: {taxonomy_name}",
                        activated_policy_types=[
                            datacatalog_v1.Taxonomy.PolicyType.FINE_GRAINED_ACCESS_CONTROL
                        ],
                    )
                    taxonomy = dc_client.create_taxonomy(parent=parent, taxonomy=taxonomy)
                    success(f"Created taxonomy: {taxonomy_name} ({taxonomy.name})")

                taxonomy_uris[taxonomy_name] = {}

                # Step 3: List existing policy tags in this taxonomy
                existing_tags = {}
                try:
                    for policy_tag in dc_client.list_policy_tags(parent=taxonomy.name):
                        existing_tags[policy_tag.display_name] = policy_tag
                except Exception as e:
                    cprint(f"   ℹ️  Could not list policy tags: {e}")

                # Step 4: Create policy tags within taxonomy
                for tag_name in tags:
                    try:
                        # Check if tag already exists by display name
                        if tag_name in existing_tags:
                            policy_tag = existing_tags[tag_name]
                            cprint(f"   ✅ Found policy tag: {tag_name}")
                        else:
                            # Create new tag
                            policy_tag = datacatalog_v1.PolicyTag(
                                display_name=tag_name, description=f"FLUID policy tag: {tag_name}"
                            )
                            policy_tag = dc_client.create_policy_tag(
                                parent=taxonomy.name, policy_tag=policy_tag
                            )
                            cprint(f"   ✅ Created policy tag: {tag_name} ({policy_tag.name})")

                        taxonomy_uris[taxonomy_name][tag_name] = policy_tag.name

                    except Exception as e:
                        cprint(f"   ⚠️  Failed to create policy tag {tag_name}: {e}")
                        continue

            except Exception as e:
                cprint(f"⚠️  Failed to create taxonomy {taxonomy_name}: {e}")
                continue

        return taxonomy_uris

    except Exception as e:
        cprint(f"⚠️  Policy tag creation failed: {e}")
        return {}


# ============================================================================
# PHASE 2: Data Masking Implementation
# ============================================================================


def _apply_data_masking(
    project: str, location: str, dataset_id: str, table_id: str, masking_rules: List[Dict[str, Any]]
) -> bool:
    """
    Create BigQuery data masking policies from contract.

    Uses the BigQuery DataPolicyService API to create column-level masking
    policies that automatically mask data based on user permissions.

    Args:
        project: GCP project ID
        location: BigQuery location
        dataset_id: Dataset name
        table_id: Table name
        masking_rules: List of masking rules from contract.policy.privacy.masking

    Returns:
        True if policies were created successfully, False otherwise
    """
    if not masking_rules:
        return True

    try:
        from google.cloud import bigquerydatapolicy_v1
    except ImportError:
        warning(
            "google-cloud-bigquery-datapolicies not installed, masking policies will not be created"
        )
        cprint("   Install with: pip install google-cloud-bigquery-datapolicies")
        return False

    try:
        client = bigquerydatapolicy_v1.DataPolicyServiceClient()
        parent = f"projects/{project}/locations/{location}"

        for rule in masking_rules:
            column = rule.get("column")
            strategy = rule.get("strategy")
            params = rule.get("params", {})

            if not column or not strategy:
                continue

            try:
                # Map FLUID strategy to BigQuery masking expression
                masking_expression = _build_masking_expression(strategy, params)

                if not masking_expression:
                    cprint(f"   ⚠️  Unsupported masking strategy: {strategy}")
                    continue

                # Create data policy
                policy = bigquerydatapolicy_v1.DataPolicy(
                    data_masking_policy=bigquerydatapolicy_v1.DataMaskingPolicy(
                        predefined_expression=masking_expression
                    )
                )

                # Create unique policy ID
                policy_id = f"{dataset_id}_{table_id}_{column}_mask".replace("-", "_")

                # Try to create the policy
                try:
                    client.create_data_policy(
                        parent=parent, data_policy_id=policy_id, data_policy=policy
                    )
                    success(f"Created masking policy for {table_id}.{column}: {strategy}")
                except Exception as create_error:
                    if "already exists" in str(create_error).lower():
                        cprint(f"   ℹ️  Masking policy already exists for {table_id}.{column}")
                    else:
                        cprint(
                            f"   ⚠️  Failed to create masking policy for {column}: {create_error}"
                        )

            except Exception as e:
                cprint(f"   ⚠️  Failed to process masking rule for {column}: {e}")
                continue

        return True

    except Exception as e:
        cprint(f"⚠️  Data masking setup failed: {e}")
        return False


def _build_masking_expression(strategy: str, params: Dict[str, Any]):
    """
    Map FLUID masking strategies to BigQuery predefined expressions.

    Args:
        strategy: FLUID masking strategy name
        params: Strategy parameters

    Returns:
        BigQuery PredefinedExpression enum value
    """
    try:
        from google.cloud import bigquerydatapolicy_v1
    except ImportError:
        return None

    # Map FLUID strategies to BigQuery expressions
    strategy_map = {
        "hash": bigquerydatapolicy_v1.DataMaskingPolicy.PredefinedExpression.SHA256,
        "nullify": bigquerydatapolicy_v1.DataMaskingPolicy.PredefinedExpression.DEFAULT_MASKING_VALUE,
        "mask_show_last_4": bigquerydatapolicy_v1.DataMaskingPolicy.PredefinedExpression.LAST_FOUR_CHARACTERS,
        "mask_show_first_4": bigquerydatapolicy_v1.DataMaskingPolicy.PredefinedExpression.FIRST_FOUR_CHARACTERS,
        "tokenize": bigquerydatapolicy_v1.DataMaskingPolicy.PredefinedExpression.MASKED,
        "mask_default": bigquerydatapolicy_v1.DataMaskingPolicy.PredefinedExpression.DEFAULT_MASKING_VALUE,
        "email_mask": bigquerydatapolicy_v1.DataMaskingPolicy.PredefinedExpression.EMAIL_MASK,
        "date_year_mask": bigquerydatapolicy_v1.DataMaskingPolicy.PredefinedExpression.DATE_YEAR_MASK,
    }

    return strategy_map.get(strategy)


# ============================================================================
# PHASE 3: Column Access Control Implementation
# ============================================================================


def _apply_column_restrictions(
    project: str,
    dataset_id: str,
    table_id: str,
    schema: List[Dict[str, Any]],
    restrictions: List[Dict[str, Any]],
) -> bool:
    """
    Apply column-level access control via authorized views.

    Since BigQuery doesn't have direct column-level IAM, we create authorized
    views that exclude restricted columns for specific principals.

    Args:
        project: GCP project ID
        dataset_id: Dataset name
        table_id: Table name
        schema: Table schema
        restrictions: List of column restrictions from contract.policy.authz.columnRestrictions

    Returns:
        True if authorized views were created successfully, False otherwise
    """
    if not restrictions:
        return True

    try:
        from google.cloud import bigquery
    except ImportError:
        warning("google-cloud-bigquery not available")
        return False

    try:
        client = bigquery.Client(project=project)

        # Get all column names from schema
        all_columns = [field.get("name") for field in schema]

        for restriction in restrictions:
            principal = restriction.get("principal")
            restricted_columns = restriction.get("columns", [])
            access = restriction.get("access", "deny")

            if not principal or not restricted_columns or access != "deny":
                continue

            try:
                # Create view ID from principal (sanitize for BigQuery naming)
                sanitized_principal = (
                    principal.replace(":", "_")
                    .replace("@", "_at_")
                    .replace(".", "_")
                    .replace("-", "_")
                )
                view_id = f"{table_id}_{sanitized_principal}_restricted"
                view_ref = f"{project}.{dataset_id}.{view_id}"

                # Build SELECT statement excluding restricted columns
                allowed_columns = [c for c in all_columns if c not in restricted_columns]

                if not allowed_columns:
                    cprint(f"   ⚠️  No columns remaining for {principal}, skipping view")
                    continue

                table_ref = f"`{project}.{dataset_id}.{table_id}`"
                view_query = f"SELECT {', '.join(allowed_columns)} FROM {table_ref}"

                # Create or update view
                view = bigquery.Table(view_ref)
                view.view_query = view_query
                view.description = (
                    f"Restricted view for {principal} (excludes: {', '.join(restricted_columns)})"
                )

                # Set labels
                view.labels = {
                    "fluid_managed": "true",
                    "restriction_type": "column_access",
                    "principal": sanitized_principal,
                }

                # Create or update the view
                view = client.create_table(view, exists_ok=True)

                # Note: IAM policy binding for the principal would be done via separate IAM actions
                success(f"Created restricted view: {view_id} for {principal}")
                cprint(f"   Excluded columns: {', '.join(restricted_columns)}")

            except Exception as e:
                cprint(f"   ⚠️  Failed to create restricted view for {principal}: {e}")
                continue

        return True

    except Exception as e:
        cprint(f"⚠️  Column restriction setup failed: {e}")
        return False
