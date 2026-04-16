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

"""
Snowflake Governance Module - Unified Contract-to-Snowflake Application

This module implements declarative governance application from FLUID contracts to Snowflake.
Reads contract.fluid.yaml and applies ALL metadata in a single operation:
  - DDL generation (CREATE DATABASE, SCHEMA, TABLE)
  - Table-level governance (tags, labels, clustering, retention, change tracking)
  - Column-level governance (descriptions, tags, labels)
  - Masking policies (with built-in templates)
  - RBAC grants (with role detection)
  - Validation and verification

Features:
  - Automatic tag translation (contract.tags + contract.labels → Snowflake tags)
  - Built-in masking policy templates (hash, email_mask, redact, etc.)
  - Better error messages with suggestions
  - Incremental application (detects what's already applied)
  - Validation commands
"""

import logging
from typing import Any, Dict, List, Optional, Tuple

from fluid_build.cli.console import cprint, success
from fluid_build.providers._sql_safety import validate_ident

logger = logging.getLogger("fluid_build.providers.snowflake.governance")


def _qualified_name(*parts: str) -> str:
    """Build a fully-qualified identifier after validating each component."""
    return ".".join(validate_ident(part) for part in parts)


def _parse_qualified_name(full_table: str) -> Tuple[str, str, str]:
    """Parse and validate a fully-qualified 'database.schema.table' string.

    Returns a tuple of validated (database, schema, table) identifiers.
    Raises ValueError if the input is not exactly three dot-separated parts
    or if any part fails identifier validation.
    """
    parts = full_table.split(".")
    if len(parts) != 3:
        raise ValueError(
            f"Expected fully-qualified name 'database.schema.table', got: {full_table!r}"
        )
    return validate_ident(parts[0]), validate_ident(parts[1]), validate_ident(parts[2])


def _validated_ident_list(values: Any) -> List[str]:
    """Normalize a cluster-by style identifier list to validated identifiers."""
    if isinstance(values, list):
        return [validate_ident(str(value)) for value in values]
    return [validate_ident(str(values))]


class MaskingPolicyTemplates:
    """Built-in masking policy templates for common use cases"""

    #: Allowlist of hash functions that may be interpolated into masking policy DDL.
    #: Only single-argument Snowflake hash functions are permitted to keep the
    #: generated SQL well-formed and to prevent function-name injection.
    ALLOWED_HASH_ALGORITHMS = frozenset({"SHA256", "SHA1", "MD5", "HASH"})

    @staticmethod
    def hash_template(column_type: str, algorithm: str = "SHA256") -> str:
        """Hash-based masking - shows hash for non-privileged users"""
        if column_type in ["TIMESTAMP_NTZ", "TIMESTAMP_LTZ", "TIMESTAMP_TZ", "DATE"]:
            return """
                CASE
                    WHEN CURRENT_ROLE() IN ('SYSADMIN', 'DATA_ENGINEER', 'ACCOUNTADMIN')
                        THEN val
                    ELSE TO_TIMESTAMP_NTZ('1970-01-01 00:00:00')
                END
            """
        else:  # STRING or other types
            if (
                not isinstance(algorithm, str)
                or algorithm.upper() not in MaskingPolicyTemplates.ALLOWED_HASH_ALGORITHMS
            ):
                raise ValueError(
                    f"Invalid hash algorithm: {algorithm!r}. "
                    f"Allowed values: {sorted(MaskingPolicyTemplates.ALLOWED_HASH_ALGORITHMS)}"
                )
            safe_algorithm = algorithm.upper()
            return f"""
                CASE
                    WHEN CURRENT_ROLE() IN ('SYSADMIN', 'DATA_ENGINEER', 'ACCOUNTADMIN')
                        THEN val
                    ELSE {safe_algorithm}(val)
                END
            """

    @staticmethod
    def email_mask_template() -> str:
        """Email masking - shows a***@b***.com"""
        return """
            CASE
                WHEN CURRENT_ROLE() IN ('SYSADMIN', 'DATA_ENGINEER', 'ACCOUNTADMIN') 
                    THEN val
                ELSE 
                    CONCAT(
                        SUBSTR(val, 1, 1), 
                        '***@',
                        SUBSTR(SPLIT_PART(val, '@', 2), 1, 1),
                        '***.',
                        SPLIT_PART(SPLIT_PART(val, '@', 2), '.', -1)
                    )
            END
        """

    @staticmethod
    def redact_template() -> str:
        """Full redaction - shows ********"""
        return """
            CASE
                WHEN CURRENT_ROLE() IN ('SYSADMIN', 'DATA_ENGINEER', 'ACCOUNTADMIN') 
                    THEN val
                ELSE '********'
            END
        """

    @staticmethod
    def partial_mask_template(visible_chars: int = 4) -> str:
        """Partial masking - shows last N characters"""
        # bool is a subclass of int in Python, so reject it explicitly.
        if (
            isinstance(visible_chars, bool)
            or not isinstance(visible_chars, int)
            or visible_chars < 0
            or visible_chars > 1000
        ):
            raise ValueError(
                f"Invalid visible_chars: {visible_chars!r}. "
                "Must be a non-negative integer <= 1000."
            )
        return f"""
            CASE
                WHEN CURRENT_ROLE() IN ('SYSADMIN', 'DATA_ENGINEER', 'ACCOUNTADMIN')
                    THEN val
                ELSE CONCAT(REPEAT('*', GREATEST(LENGTH(val) - {visible_chars}, 0)), RIGHT(val, {visible_chars}))
            END
        """

    @staticmethod
    def get_template(
        strategy: str, column_type: str = "VARCHAR", params: Optional[Dict] = None
    ) -> Optional[str]:
        """Get masking policy template by strategy name"""
        params = params or {}

        if strategy == "hash":
            algorithm = params.get("algorithm", "SHA256")
            return MaskingPolicyTemplates.hash_template(column_type, algorithm)
        elif strategy == "email_mask":
            return MaskingPolicyTemplates.email_mask_template()
        elif strategy == "redact":
            return MaskingPolicyTemplates.redact_template()
        elif strategy == "partial_mask":
            visible_chars = params.get("visible_chars", 4)
            return MaskingPolicyTemplates.partial_mask_template(visible_chars)
        else:
            return None


class SnowflakeGovernanceError(Exception):
    """Custom exception for governance errors with helpful messages"""

    def __init__(
        self, message: str, suggestion: Optional[str] = None, details: Optional[Dict] = None
    ):
        self.message = message
        self.suggestion = suggestion
        self.details = details or {}
        super().__init__(self.format_message())

    def format_message(self) -> str:
        """Format error message with suggestion"""
        msg = f"❌ {self.message}"
        if self.suggestion:
            msg += f"\n💡 Suggestion: {self.suggestion}"
        if self.details:
            msg += f"\n📋 Details: {self.details}"
        return msg


class GovernanceValidator:
    """Validates governance application and provides status"""

    def __init__(self, cursor, database: str, schema: str, table: str):
        self.cursor = cursor
        self.database = validate_ident(database)
        self.schema = validate_ident(schema)
        self.table = validate_ident(table)
        self.full_table = _qualified_name(self.database, self.schema, self.table)

    def validate_table_exists(self) -> bool:
        """Check if table exists"""
        try:
            self.cursor.execute(
                """
                SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES
                WHERE TABLE_CATALOG = %s 
                  AND TABLE_SCHEMA = %s 
                  AND TABLE_NAME = %s
            """,
                (self.database, self.schema, self.table),
            )
            result = self.cursor.fetchone()
            return result and result[0] > 0
        except Exception as e:
            logger.error(f"Error checking table existence: {e}")
            return False

    def validate_column_descriptions(self) -> Tuple[int, int]:
        """Validate column descriptions - returns (applied, total)"""
        try:
            self.cursor.execute(
                """
                SELECT COUNT(*) as total,
                       SUM(CASE WHEN COMMENT IS NOT NULL AND COMMENT != '' THEN 1 ELSE 0 END) as with_desc
                FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_SCHEMA = %s
                  AND TABLE_NAME = %s
            """,
                (self.schema, self.table),
            )
            result = self.cursor.fetchone()
            if result:
                return (result[1] or 0, result[0] or 0)
            return (0, 0)
        except Exception as e:
            logger.error(f"Error validating column descriptions: {e}")
            return (0, 0)

    def validate_table_tags(self) -> List[Dict[str, str]]:
        """Get all table-level tags"""
        try:
            self.cursor.execute(
                f"""
                SELECT TAG_NAME, TAG_VALUE
                FROM TABLE(
                    INFORMATION_SCHEMA.TAG_REFERENCES_ALL_COLUMNS(
                        '{self.full_table}', 'TABLE'
                    )
                )
                WHERE LEVEL = 'TABLE'
                ORDER BY TAG_NAME
            """
            )
            return [{"name": row[0], "value": row[1]} for row in self.cursor.fetchall()]
        except Exception as e:
            logger.error(f"Error validating table tags: {e}")
            return []

    def validate_column_tags(self) -> Dict[str, int]:
        """Get column tag counts - returns {column_name: tag_count}"""
        try:
            self.cursor.execute(
                f"""
                SELECT COLUMN_NAME, COUNT(*) as tag_count
                FROM TABLE(
                    INFORMATION_SCHEMA.TAG_REFERENCES_ALL_COLUMNS(
                        '{self.full_table}', 'TABLE'
                    )
                )
                WHERE LEVEL = 'COLUMN'
                GROUP BY COLUMN_NAME
                ORDER BY COLUMN_NAME
            """
            )
            return {row[0]: row[1] for row in self.cursor.fetchall()}
        except Exception as e:
            logger.error(f"Error validating column tags: {e}")
            return {}

    def validate_masking_policies(self) -> List[Dict[str, str]]:
        """Get masking policies on columns"""
        try:
            self.cursor.execute(
                """
                SELECT COLUMN_NAME, POLICY_NAME
                FROM INFORMATION_SCHEMA.POLICY_REFERENCES
                WHERE POLICY_DB = %s
                  AND REF_DATABASE_NAME = %s
                  AND REF_SCHEMA_NAME = %s
                  AND REF_ENTITY_NAME = %s
                  AND POLICY_KIND = 'MASKING_POLICY'
            """,
                (self.database, self.database, self.schema, self.table),
            )
            return [{"column": row[0], "policy": row[1]} for row in self.cursor.fetchall()]
        except Exception as e:
            # Try alternative query
            try:
                self.cursor.execute(f"DESC TABLE {self.full_table}")
                # This is a fallback - not all Snowflake versions support policy queries
                return []
            except Exception:
                logger.error(f"Error validating masking policies: {e}")
                return []

    def get_table_properties(self) -> Dict[str, Any]:
        """Get table properties (clustering, retention, etc.)"""
        try:
            self.cursor.execute(
                "SHOW TABLES LIKE '{}' IN {}".format(
                    self.table.replace("_", "\\_"),
                    _qualified_name(self.database, self.schema),
                )
            )
            result = self.cursor.fetchone()
            if result:
                # Parse result - varies by Snowflake version
                return {
                    "exists": True,
                    "clustering_key": result[11] if len(result) > 11 else None,
                    "retention_time": result[7] if len(result) > 7 else None,
                }
            return {"exists": False}
        except Exception as e:
            logger.error(f"Error getting table properties: {e}")
            return {"exists": False}


class UnifiedGovernanceApplicator:
    """
    Unified governance application from FLUID contract to Snowflake.

    Applies all governance in a single pass:
      1. Create infrastructure (database, schema, table)
      2. Apply table-level governance (tags, clustering, retention)
      3. Apply column-level governance (descriptions, tags, labels)
      4. Apply security policies (masking, row access)
      5. Validate and report
    """

    def __init__(self, cursor, contract: Dict[str, Any], dry_run: bool = False):
        self.cursor = cursor
        self.contract = contract
        self.dry_run = dry_run
        self.stats = {
            "tables_created": 0,
            "table_tags_applied": 0,
            "column_descriptions_applied": 0,
            "column_tags_applied": 0,
            "masking_policies_created": 0,
            "masking_policies_applied": 0,
            "errors": [],
        }

    def apply_all(self) -> Dict[str, Any]:
        """Apply all governance from contract"""
        try:
            # Get expose configuration
            exposes = self.contract.get("exposes") or []
            if not exposes:
                raise SnowflakeGovernanceError(
                    "Missing expose configuration",
                    "Ensure the contract defines at least one expose with Snowflake binding.location.",
                )

            expose = exposes[0]
            binding = expose.get("binding", {})
            location = binding.get("location", {})

            database = location.get("database")
            schema = location.get("schema")
            table = location.get("table")

            if not all([database, schema, table]):
                raise SnowflakeGovernanceError(
                    "Missing required location fields",
                    "Ensure contract has binding.location with database, schema, and table",
                )

            database = validate_ident(database)
            schema = validate_ident(schema)
            table = validate_ident(table)
            full_table = _qualified_name(database, schema, table)

            cprint(f"\n🎯 Applying governance to {full_table}\n")
            cprint("=" * 90)

            # Step 1: Create infrastructure
            self._create_infrastructure(database, schema, table, expose)

            # Step 2: Apply table-level governance
            self._apply_table_governance(full_table, expose)

            # Step 3: Apply column-level governance
            self._apply_column_governance(full_table, expose)

            # Step 4: Apply security policies
            self._apply_security_policies(database, schema, full_table, expose)

            # Step 5: Validate
            validator = GovernanceValidator(self.cursor, database, schema, table)
            self._validate_application(validator)

            cprint("\n" + "=" * 90)
            success("GOVERNANCE APPLICATION COMPLETE")
            cprint("=" * 90)

            return {
                "status": "success",
                "table": full_table,
                "stats": self.stats,
                "dry_run": self.dry_run,
            }

        except SnowflakeGovernanceError as e:
            cprint(f"\n{e.format_message()}\n")
            self.stats["errors"].append(str(e))
            return {"status": "error", "error": str(e), "stats": self.stats}
        except Exception as e:
            logger.exception("Unexpected error during governance application")
            self.stats["errors"].append(str(e))
            return {"status": "error", "error": str(e), "stats": self.stats}

    def _create_infrastructure(self, database: str, schema: str, table: str, expose: Dict):
        """Create database, schema, and table if they don't exist"""
        cprint("\n📦 Creating Infrastructure...\n")

        # Create database
        if not self.dry_run:
            try:
                self.cursor.execute(f"CREATE DATABASE IF NOT EXISTS {database}")
                cprint(f"   ✅ Database: {database}")
            except Exception as e:
                raise SnowflakeGovernanceError(
                    f"Failed to create database {database}",
                    f"Check permissions for role {self.cursor.connection.role}",
                    {"error": str(e)},
                )
        else:
            cprint(f"   🔍 [DRY RUN] Would create database: {database}")

        # Create schema
        if not self.dry_run:
            try:
                self.cursor.execute(
                    f"CREATE SCHEMA IF NOT EXISTS {_qualified_name(database, schema)}"
                )
                cprint(f"   ✅ Schema: {database}.{schema}")
            except Exception:
                raise SnowflakeGovernanceError(
                    f"Failed to create schema {schema}",
                    "Ensure database exists and you have CREATE SCHEMA privilege",
                )
        else:
            cprint(f"   🔍 [DRY RUN] Would create schema: {database}.{schema}")

        # Create table with all columns and properties
        table_ddl = self._generate_create_table_ddl(database, schema, table, expose)

        if not self.dry_run:
            try:
                self.cursor.execute(table_ddl)
                cprint(f"   ✅ Table: {database}.{schema}.{table}")
                self.stats["tables_created"] += 1
            except Exception as e:
                if "already exists" in str(e).lower():
                    cprint(f"   ℹ️  Table exists: {database}.{schema}.{table}")
                else:
                    raise SnowflakeGovernanceError(
                        f"Failed to create table {table}",
                        "Check DDL syntax and column types",
                        {"ddl": table_ddl, "error": str(e)},
                    )
        else:
            cprint(f"   🔍 [DRY RUN] Would execute:\n{table_ddl}\n")

    def _generate_create_table_ddl(
        self, database: str, schema: str, table: str, expose: Dict
    ) -> str:
        """Generate CREATE TABLE DDL from contract schema"""
        schema_fields = expose.get("contract", {}).get("schema", [])
        properties = expose.get("binding", {}).get("properties", {})

        ddl_parts = [f"CREATE TABLE IF NOT EXISTS {_qualified_name(database, schema, table)} ("]

        # Add columns with descriptions
        column_defs = []
        for field in schema_fields:
            col_name = validate_ident(field["name"].upper())
            col_type = self._map_type(field.get("type", "VARCHAR"))
            description = field.get("description")
            escaped_description = str(description).replace("'", "''") if description else ""
            comment = f" COMMENT '{escaped_description}'" if description else ""

            column_defs.append(f"  {col_name} {col_type}{comment}")

        ddl_parts.append(",\n".join(column_defs))
        ddl_parts.append(")")

        # Add table properties
        if properties.get("cluster_by"):
            cluster_str = ", ".join(_validated_ident_list(properties["cluster_by"]))
            ddl_parts.append(f"CLUSTER BY ({cluster_str})")

        if properties.get("comment"):
            comment = properties["comment"].replace("'", "''")  # Escape quotes
            ddl_parts.append(f"COMMENT = '{comment}'")

        return " ".join(ddl_parts)

    def _map_type(self, fluid_type: str) -> str:
        """Map FLUID types to Snowflake types"""
        type_map = {
            "STRING": "VARCHAR",
            "INTEGER": "NUMBER",
            "FLOAT": "FLOAT",
            "BOOLEAN": "BOOLEAN",
            "TIMESTAMP": "TIMESTAMP_NTZ",
            "TIMESTAMP_NTZ": "TIMESTAMP_NTZ",
            "DATE": "DATE",
            "NUMBER": "NUMBER",
            "VARCHAR": "VARCHAR",
        }
        return type_map.get(fluid_type.upper(), fluid_type)

    def _apply_table_governance(self, full_table: str, expose: Dict):
        """Apply table-level tags, clustering, retention, etc."""
        cprint("\n🏷️  Applying Table-Level Governance...\n")

        # Extract all table tags from contract.tags and contract.labels
        table_tags = {}

        # From top-level tags
        for tag in self.contract.get("tags", []):
            tag_name = tag.replace("-", "_").replace(" ", "_").upper()
            table_tags[tag_name] = tag

        # From top-level labels
        for key, value in self.contract.get("labels", {}).items():
            tag_name = key.replace("-", "_").replace(" ", "_").upper()
            table_tags[tag_name] = value

        # Create and apply tags
        if table_tags:
            self._create_tags(
                expose.get("binding", {}).get("location", {}).get("schema"), table_tags
            )
            self._apply_tags_to_table(full_table, table_tags)

        # Apply clustering, retention, change tracking
        properties = expose.get("binding", {}).get("properties", {})

        if properties.get("cluster_by"):
            self._apply_clustering(full_table, properties["cluster_by"])

        if properties.get("data_retention_time_in_days"):
            self._apply_retention(full_table, properties["data_retention_time_in_days"])

        if properties.get("change_tracking"):
            self._apply_change_tracking(full_table, properties["change_tracking"])

    def _apply_column_governance(self, full_table: str, expose: Dict):
        """Apply column descriptions, tags, and labels"""
        cprint("\n📝 Applying Column-Level Governance...\n")

        schema_name = expose.get("binding", {}).get("location", {}).get("schema")
        schema_fields = expose.get("contract", {}).get("schema", [])

        # Collect all unique column tags and labels
        all_col_tags = set()
        all_col_labels = set()

        for field in schema_fields:
            for tag in field.get("tags", []):
                all_col_tags.add(tag.replace("-", "_").replace(" ", "_").upper())
            for label_key in field.get("labels", {}).keys():
                all_col_labels.add(label_key.replace("-", "_").replace(" ", "_").upper())

        # Create all column tags
        all_tags_to_create = all_col_tags | all_col_labels
        if all_tags_to_create:
            for tag_name in sorted(all_tags_to_create):
                self._create_tag(schema_name, tag_name)

        # Apply to each column
        for field in schema_fields:
            col_name = validate_ident(field["name"].upper())

            # Apply tags
            for tag in field.get("tags", []):
                tag_name = tag.replace("-", "_").replace(" ", "_").upper()
                self._apply_column_tag(full_table, col_name, tag_name, tag)
                self.stats["column_tags_applied"] += 1

            # Apply labels as tags
            for label_key, label_value in field.get("labels", {}).items():
                tag_name = label_key.replace("-", "_").replace(" ", "_").upper()
                self._apply_column_tag(full_table, col_name, tag_name, label_value)
                self.stats["column_tags_applied"] += 1

    def _apply_security_policies(self, database: str, schema: str, full_table: str, expose: Dict):
        """Apply masking policies and row access policies"""
        cprint("\n🔒 Applying Security Policies...\n")

        # Get masking rules from contract
        masking_rules = expose.get("policy", {}).get("privacy", {}).get("masking", [])

        for rule in masking_rules:
            column_name = validate_ident(rule["column"].upper())
            strategy = rule.get("strategy", "hash")
            params = rule.get("params", {})

            # Get column type
            col_type = self._get_column_type(full_table, column_name)

            # Create masking policy from template
            policy_name = validate_ident(f"{column_name}_{strategy.upper()}_MASK")
            self._create_masking_policy(schema, policy_name, col_type, strategy, params)

            # Apply to column
            self._apply_masking_policy(full_table, column_name, policy_name)

    def _create_tag(self, schema: str, tag_name: str):
        """Create a single tag"""
        schema = validate_ident(schema)
        tag_name = validate_ident(tag_name)
        if not self.dry_run:
            try:
                self.cursor.execute(f"CREATE TAG IF NOT EXISTS {_qualified_name(schema, tag_name)}")
            except Exception as e:
                logger.warning(f"Could not create tag {tag_name}: {e}")
        else:
            cprint(f"   🔍 [DRY RUN] Would create tag: {_qualified_name(schema, tag_name)}")

    def _create_tags(self, schema: str, tags: Dict[str, str]):
        """Create all tags"""
        for tag_name in tags.keys():
            self._create_tag(schema, tag_name)

    def _apply_tags_to_table(self, full_table: str, tags: Dict[str, str]):
        """Apply tags to table"""
        for tag_name, tag_value in tags.items():
            safe_tag_name = validate_ident(tag_name)
            safe_tag_value = str(tag_value).replace("'", "''")
            if not self.dry_run:
                try:
                    self.cursor.execute(
                        f"ALTER TABLE {full_table} SET TAG {safe_tag_name} = '{safe_tag_value}'"
                    )
                    cprint(f"   ✅ {safe_tag_name} = {tag_value}")
                    self.stats["table_tags_applied"] += 1
                except Exception as e:
                    logger.warning(f"Could not apply tag {safe_tag_name}: {e}")
            else:
                cprint(f"   🔍 [DRY RUN] Would set tag: {safe_tag_name} = {tag_value}")

    def _apply_clustering(self, full_table: str, cluster_by):
        """Apply clustering to table"""
        if not self.dry_run:
            try:
                cluster_str = ", ".join(_validated_ident_list(cluster_by))
                self.cursor.execute(f"ALTER TABLE {full_table} CLUSTER BY ({cluster_str})")
                cprint(f"   ✅ Clustering: {cluster_str}")
            except Exception as e:
                logger.warning(f"Could not apply clustering: {e}")
        else:
            cprint(f"   🔍 [DRY RUN] Would cluster by: {cluster_by}")

    def _apply_retention(self, full_table: str, days: int):
        """Apply data retention"""
        if not self.dry_run:
            try:
                self.cursor.execute(
                    f"ALTER TABLE {full_table} SET DATA_RETENTION_TIME_IN_DAYS = {days}"
                )
                cprint(f"   ✅ Retention: {days} days")
            except Exception as e:
                logger.warning(f"Could not apply retention: {e}")
        else:
            cprint(f"   🔍 [DRY RUN] Would set retention: {days} days")

    def _apply_change_tracking(self, full_table: str, enabled: bool):
        """Apply change tracking"""
        if not self.dry_run:
            try:
                value = "TRUE" if enabled else "FALSE"
                self.cursor.execute(f"ALTER TABLE {full_table} SET CHANGE_TRACKING = {value}")
                cprint(f"   ✅ Change tracking: {value}")
            except Exception as e:
                logger.warning(f"Could not apply change tracking: {e}")
        else:
            cprint(f"   🔍 [DRY RUN] Would set change tracking: {enabled}")

    def _apply_column_tag(self, full_table: str, column_name: str, tag_name: str, tag_value: str):
        """Apply tag to column"""
        safe_column_name = validate_ident(column_name)
        safe_tag_name = validate_ident(tag_name)
        safe_tag_value = str(tag_value).replace("'", "''")
        if not self.dry_run:
            try:
                self.cursor.execute(
                    f"""
                    ALTER TABLE {full_table}
                    MODIFY COLUMN {safe_column_name}
                    SET TAG {safe_tag_name} = '{safe_tag_value}'
                """
                )
            except Exception as e:
                logger.warning(
                    f"Could not apply tag {safe_tag_name} to column {safe_column_name}: {e}"
                )

    def _get_column_type(self, full_table: str, column_name: str) -> str:
        """Get column data type"""
        try:
            _, schema, table = _parse_qualified_name(full_table)
            self.cursor.execute(
                """
                SELECT DATA_TYPE
                FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_SCHEMA = %s
                  AND TABLE_NAME = %s
                  AND COLUMN_NAME = %s
            """,
                (schema, table, validate_ident(column_name)),
            )
            result = self.cursor.fetchone()
            return result[0] if result else "VARCHAR"
        except Exception:
            return "VARCHAR"

    def _create_masking_policy(
        self, schema: str, policy_name: str, col_type: str, strategy: str, params: Dict
    ):
        """Create masking policy from template"""
        schema = validate_ident(schema)
        policy_name = validate_ident(policy_name)
        template = MaskingPolicyTemplates.get_template(strategy, col_type, params)

        if not template:
            logger.warning(f"No template found for strategy: {strategy}")
            return

        if not self.dry_run:
            try:
                # Map Snowflake types to policy signature
                policy_type = "TIMESTAMP_NTZ" if "TIMESTAMP" in col_type else "VARCHAR"

                ddl = f"""
                    CREATE MASKING POLICY IF NOT EXISTS {_qualified_name(schema, policy_name)} 
                    AS (val {policy_type}) RETURNS {policy_type} ->
                    {template}
                """
                self.cursor.execute(ddl)
                cprint(f"   ✅ Created masking policy: {policy_name}")
                self.stats["masking_policies_created"] += 1
            except Exception as e:
                if "already exists" not in str(e).lower():
                    logger.warning(f"Could not create masking policy {policy_name}: {e}")
        else:
            cprint(f"   🔍 [DRY RUN] Would create masking policy: {policy_name}")

    def _apply_masking_policy(self, full_table: str, column_name: str, policy_name: str):
        """Apply masking policy to column"""
        safe_column_name = validate_ident(column_name)
        safe_policy_name = validate_ident(policy_name)
        if not self.dry_run:
            try:
                _, schema, _ = _parse_qualified_name(full_table)
                self.cursor.execute(
                    f"""
                    ALTER TABLE {full_table}
                    MODIFY COLUMN {safe_column_name}
                    SET MASKING POLICY {_qualified_name(schema, safe_policy_name)}
                """
                )
                cprint(f"   ✅ Applied {safe_policy_name} to {safe_column_name}")
                self.stats["masking_policies_applied"] += 1
            except Exception as e:
                logger.warning(f"Could not apply masking policy to {safe_column_name}: {e}")
        else:
            cprint(f"   🔍 [DRY RUN] Would apply {safe_policy_name} to {safe_column_name}")

    def _validate_application(self, validator: GovernanceValidator):
        """Validate that governance was applied correctly"""
        cprint("\n\n✅ VALIDATION RESULTS\n")
        cprint("=" * 90)

        # Check table exists
        if validator.validate_table_exists():
            cprint("   ✅ Table exists")
        else:
            cprint("   ❌ Table does not exist")
            return

        # Check column descriptions
        desc_applied, desc_total = validator.validate_column_descriptions()
        cprint(f"   ✅ Column descriptions: {desc_applied}/{desc_total} applied")

        # Check table tags
        table_tags = validator.validate_table_tags()
        cprint(f"   ✅ Table-level tags: {len(table_tags)} applied")

        # Check column tags
        column_tags = validator.validate_column_tags()
        total_col_tags = sum(column_tags.values())
        cprint(
            f"   ✅ Column-level tags: {total_col_tags} applied across {len(column_tags)} columns"
        )

        # Check masking policies
        masking = validator.validate_masking_policies()
        cprint(f"   ✅ Masking policies: {len(masking)} columns protected")
