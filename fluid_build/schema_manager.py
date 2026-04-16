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
Enhanced FLUID Schema Management System

Provides dynamic schema fetching, caching, version detection, and validation
against the official FLUID schema repository.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Union
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import yaml

from .errors import ValidationError as FluidValidationError

# Try to import jsonschema for proper validation
try:
    import jsonschema
    from jsonschema import Draft7Validator, RefResolver

    JSONSCHEMA_AVAILABLE = True
except ImportError as e:
    JSONSCHEMA_AVAILABLE = False
    _JSONSCHEMA_IMPORT_ERROR = e

# Version constraints
VERSION_PATTERN = re.compile(r"^(\d+)\.(\d+)(?:\.(\d+))?(?:-(.+))?$")
SCHEMA_FILE_PATTERN = re.compile(r"^fluid-schema-(\d+\.\d+(?:\.\d+)?)\.json$")
SCHEMA_REPO_BASE = "https://raw.githubusercontent.com/open-data-protocol/fluid/main/schema"
LOG = logging.getLogger(__name__)


def _schema_version_sort_key(version: str) -> tuple[int, int, int]:
    match = VERSION_PATTERN.match(version)
    if not match:
        return (0, 0, 0)
    major, minor, patch_str, _prerelease = match.groups()
    return (int(major), int(minor), int(patch_str) if patch_str else 0)


def _discover_bundled_versions() -> List[str]:
    schemas_dir = Path(__file__).parent / "schemas"
    versions = set()

    if schemas_dir.exists():
        for schema_file in schemas_dir.glob("fluid-schema-*.json"):
            match = SCHEMA_FILE_PATTERN.match(schema_file.name)
            if match:
                versions.add(match.group(1))

    if not versions:
        versions.update(["0.4.0", "0.5.7", "0.7.1", "0.7.2"])

    return sorted(versions, key=_schema_version_sort_key)


@dataclass
class SchemaVersion:
    """Represents a FLUID schema version with metadata."""

    version: str
    major: int
    minor: int
    patch: int
    prerelease: Optional[str] = None
    schema_url: Optional[str] = None
    cached_path: Optional[Path] = None
    last_fetched: Optional[datetime] = None

    def __post_init__(self):
        if not self.schema_url:
            # Construct schema URL based on actual repository structure
            self.schema_url = f"{SCHEMA_REPO_BASE}/fluid-schema-{self.version}.json"

    @classmethod
    def parse(cls, version_str: str) -> SchemaVersion:
        """Parse a version string into components."""
        match = VERSION_PATTERN.match(version_str.strip())
        if not match:
            raise FluidValidationError(
                f"Invalid FLUID version format: {version_str}",
                context={"version": version_str, "expected_format": "X.Y.Z"},
                suggestions=[
                    "Version must follow semantic versioning: major.minor.patch",
                    "Examples: 0.4.0, 0.5.7, 1.0.0",
                    "Prerelease versions: 1.0.0-alpha, 1.0.0-beta.1",
                ],
            )

        major, minor, patch_str, prerelease = match.groups()
        patch = int(patch_str) if patch_str else 0

        return cls(
            version=version_str,
            major=int(major),
            minor=int(minor),
            patch=patch,
            prerelease=prerelease,
        )

    def satisfies(self, constraint: VersionConstraint) -> bool:
        """Check if this version satisfies a constraint."""
        return constraint.matches(self)

    def __str__(self) -> str:
        return self.version

    def __lt__(self, other: SchemaVersion) -> bool:
        return (self.major, self.minor, self.patch) < (other.major, other.minor, other.patch)

    def __le__(self, other: SchemaVersion) -> bool:
        return (self.major, self.minor, self.patch) <= (other.major, other.minor, other.patch)

    def __gt__(self, other: SchemaVersion) -> bool:
        return (self.major, self.minor, self.patch) > (other.major, other.minor, other.patch)

    def __ge__(self, other: SchemaVersion) -> bool:
        return (self.major, self.minor, self.patch) >= (other.major, other.minor, other.patch)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SchemaVersion):
            return False
        return (self.major, self.minor, self.patch) == (other.major, other.minor, other.patch)


@dataclass
class VersionConstraint:
    """Represents version constraints like >=0.4.0, ~0.5.0, etc."""

    operator: str
    version: SchemaVersion

    @classmethod
    def parse(cls, constraint_str: str) -> VersionConstraint:
        """Parse constraint string like '>=0.4.0' or '~0.5.0'."""
        constraint_str = constraint_str.strip()

        # Extract operator and version
        if constraint_str.startswith(">="):
            op, version_str = ">=", constraint_str[2:].strip()
        elif constraint_str.startswith(">"):
            op, version_str = ">", constraint_str[1:].strip()
        elif constraint_str.startswith("<="):
            op, version_str = "<=", constraint_str[2:].strip()
        elif constraint_str.startswith("<"):
            op, version_str = "<", constraint_str[1:].strip()
        elif constraint_str.startswith("~"):
            op, version_str = "~", constraint_str[1:].strip()
        elif constraint_str.startswith("="):
            op, version_str = "=", constraint_str[1:].strip()
        else:
            # Default to exact match
            op, version_str = "=", constraint_str

        version = SchemaVersion.parse(version_str)
        return cls(operator=op, version=version)

    def matches(self, version: SchemaVersion) -> bool:
        """Check if a version matches this constraint."""
        if self.operator == ">=":
            return version >= self.version
        elif self.operator == ">":
            return version > self.version
        elif self.operator == "<=":
            return version <= self.version
        elif self.operator == "<":
            return version < self.version
        elif self.operator == "~":
            # Compatible within minor version
            return (
                version.major == self.version.major
                and version.minor == self.version.minor
                and version >= self.version
            )
        else:  # '=' or default
            return version == self.version


@dataclass
class ValidationResult:
    """Result of schema validation."""

    is_valid: bool
    schema_version: Optional[SchemaVersion] = None
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    validation_time: Optional[float] = None

    def add_error(self, error: str) -> None:
        """Add a validation error."""
        self.errors.append(error)
        self.is_valid = False

    def add_warning(self, warning: str) -> None:
        """Add a validation warning."""
        self.warnings.append(warning)

    def get_summary(self) -> str:
        """Get a human-readable summary."""
        if self.is_valid:
            summary = f"✅ Valid FLUID contract (schema v{self.schema_version})"
            if self.warnings:
                summary += f"\n⚠️  {len(self.warnings)} warning(s)"
        else:
            summary = f"❌ Invalid FLUID contract ({len(self.errors)} error(s))"
            if self.schema_version:
                summary += f" (schema v{self.schema_version})"

        if self.validation_time:
            summary += f"\nValidation completed in {self.validation_time:.3f}s"

        return summary


class SchemaCache:
    """Manages local caching of FLUID schemas."""

    def __init__(self, cache_dir: Optional[Path] = None):
        self.cache_dir = cache_dir or Path.home() / ".fluid" / "schema_cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.cache_index_file = self.cache_dir / "index.json"
        self._cache_index: Dict[str, Dict[str, Any]] = {}
        self._load_cache_index()

    def _load_cache_index(self) -> None:
        """Load cache index from disk."""
        if self.cache_index_file.exists():
            try:
                with open(self.cache_index_file) as f:
                    self._cache_index = json.load(f)
            except (json.JSONDecodeError, OSError):
                self._cache_index = {}

    def _save_cache_index(self) -> None:
        """Save cache index to disk."""
        try:
            with open(self.cache_index_file, "w") as f:
                json.dump(self._cache_index, f, indent=2, default=str)
        except OSError as exc:
            LOG.debug(
                "schema_cache_index_write_failed: path=%s error=%s",
                self.cache_index_file,
                exc,
            )

    def get_cached_schema(
        self, version: SchemaVersion, max_age_hours: int = 24
    ) -> Optional[Dict[str, Any]]:
        """Get a cached schema if it exists and is fresh enough."""
        version_key = str(version)
        if version_key not in self._cache_index:
            return None

        entry = self._cache_index[version_key]
        cached_file = self.cache_dir / entry["filename"]

        if not cached_file.exists():
            # Cache entry exists but file is missing
            del self._cache_index[version_key]
            self._save_cache_index()
            return None

        # Check age
        last_fetched = datetime.fromisoformat(entry["last_fetched"])
        if datetime.now() - last_fetched > timedelta(hours=max_age_hours):
            return None

        try:
            with open(cached_file) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return None

    def cache_schema(self, version: SchemaVersion, schema: Dict[str, Any]) -> None:
        """Cache a schema to disk."""
        version_key = str(version)
        filename = f"contract_v{version.major}_{version.minor}_{version.patch}.schema.json"
        cached_file = self.cache_dir / filename

        try:
            with open(cached_file, "w") as f:
                json.dump(schema, f, indent=2)

            self._cache_index[version_key] = {
                "filename": filename,
                "last_fetched": datetime.now().isoformat(),
                "schema_url": version.schema_url,
            }
            self._save_cache_index()
        except OSError as exc:
            LOG.debug(
                "schema_cache_write_failed: version=%s path=%s error=%s",
                version_key,
                cached_file,
                exc,
            )

    def clear_cache(self) -> int:
        """Clear all cached schemas. Returns number of files removed."""
        removed = 0
        for file in self.cache_dir.glob("*.schema.json"):
            try:
                file.unlink()
                removed += 1
            except OSError as exc:
                LOG.debug("schema_cache_remove_failed: path=%s error=%s", file, exc)

        self._cache_index.clear()
        self._save_cache_index()
        return removed

    def list_cached_versions(self) -> List[str]:
        """List all cached schema versions."""
        return list(self._cache_index.keys())


class FluidSchemaManager:
    """
    Enhanced FLUID Schema Manager with dynamic fetching, caching, and validation.
    """

    # Bundled schema versions (embedded in package)
    BUNDLED_VERSIONS = _discover_bundled_versions()

    @classmethod
    def latest_bundled_version(cls) -> str:
        """Return the newest bundled FLUID schema version."""
        if cls.BUNDLED_VERSIONS:
            return cls.BUNDLED_VERSIONS[-1]
        return "0.7.2"

    def __init__(
        self,
        cache_dir: Optional[Path] = None,
        logger: Optional[logging.Logger] = None,
        timeout: int = 30,
    ):
        self.cache = SchemaCache(cache_dir)
        self.logger = logger or logging.getLogger(__name__)
        self.timeout = timeout
        self._bundled_schemas: Dict[str, Dict[str, Any]] = {}
        self._load_bundled_schemas()

    def _load_bundled_schemas(self) -> None:
        """Load bundled schemas from package resources."""
        # Get the directory containing this module
        module_dir = Path(__file__).parent
        schemas_dir = module_dir / "schemas"

        if not schemas_dir.exists():
            self.logger.warning(f"Bundled schemas directory not found: {schemas_dir}")
            return

        # Load available schema files
        for schema_file in schemas_dir.glob("fluid-schema-*.json"):
            try:
                # Extract version from canonical filenames like fluid-schema-0.7.2.json
                version_match = SCHEMA_FILE_PATTERN.match(schema_file.name)
                if not version_match:
                    continue

                version = version_match.group(1)

                with open(schema_file) as f:
                    schema = json.load(f)

                self._bundled_schemas[version] = schema
                self.logger.debug(f"Loaded bundled schema for v{version}")

            except (json.JSONDecodeError, OSError) as e:
                self.logger.warning(f"Failed to load bundled schema {schema_file}: {e}")

        # Fallback: ensure we have at least 0.5.7 with minimal schema
        if "0.5.7" not in self._bundled_schemas:
            self._bundled_schemas["0.5.7"] = {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "description": "FLUID v0.5.7 Contract Schema (minimal fallback)",
            }

    def detect_version(self, contract: Dict[str, Any]) -> Optional[SchemaVersion]:
        """Detect FLUID version from contract."""
        fluid_version = contract.get("fluidVersion")
        if not fluid_version:
            return None

        try:
            return SchemaVersion.parse(str(fluid_version))
        except ValueError:
            self.logger.warning(f"Invalid fluidVersion format: {fluid_version}")
            return None

    def get_schema(
        self,
        version: Union[str, SchemaVersion],
        force_refresh: bool = False,
        offline_only: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """
        Get schema for a specific version, trying various sources.

        Args:
            version: Version string or SchemaVersion object
            force_refresh: Skip cache and fetch fresh schema
            offline_only: Only use bundled/cached schemas, don't fetch

        Returns:
            Schema dict or None if not found
        """
        if isinstance(version, str):
            try:
                version = SchemaVersion.parse(version)
            except ValueError:
                self.logger.error(f"Invalid version format: {version}")
                return None

        # Try cache first (unless force refresh)
        if not force_refresh:
            cached = self.cache.get_cached_schema(version)
            if cached:
                self.logger.debug(f"Using cached schema for v{version}")
                return cached

        # Try bundled schemas
        bundled = self._bundled_schemas.get(str(version))
        if bundled:
            self.logger.debug(f"Using bundled schema for v{version}")
            return bundled

        # Try to fetch from remote (unless offline only)
        if not offline_only:
            fetched = self._fetch_schema(version)
            if fetched:
                self.cache.cache_schema(version, fetched)
                return fetched

        self.logger.error(f"Schema not found for version {version}")
        return None

    def _fetch_schema(self, version: SchemaVersion) -> Optional[Dict[str, Any]]:
        """Fetch schema from remote repository."""
        if not version.schema_url:
            return None

        try:
            self.logger.info(f"Fetching schema from {version.schema_url}")
            req = Request(version.schema_url)
            req.add_header("User-Agent", "FLUID-Build-Tool/1.0")

            with urlopen(req, timeout=self.timeout) as response:
                if response.status == 200:
                    content = response.read().decode("utf-8")
                    schema = json.loads(content)
                    self.logger.debug(f"Successfully fetched schema for v{version}")
                    return schema
                else:
                    self.logger.warning(f"HTTP {response.status} fetching schema for v{version}")

        except HTTPError as e:
            self.logger.warning(f"HTTP error fetching schema for v{version}: {e}")
        except URLError as e:
            self.logger.warning(f"URL error fetching schema for v{version}: {e}")
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            self.logger.warning(f"Invalid schema content for v{version}: {e}")
        except Exception as e:
            self.logger.warning(f"Unexpected error fetching schema for v{version}: {e}")

        return None

    def find_compatible_version(
        self,
        constraint: Union[str, VersionConstraint],
        available_versions: Optional[List[str]] = None,
    ) -> Optional[SchemaVersion]:
        """
        Find the best compatible version for a constraint.

        Args:
            constraint: Version constraint like '>=0.4.0'
            available_versions: List of available versions to check

        Returns:
            Best matching SchemaVersion or None
        """
        if isinstance(constraint, str):
            constraint = VersionConstraint.parse(constraint)

        if available_versions is None:
            # Use bundled versions + any cached versions
            available_versions = list(self.BUNDLED_VERSIONS) + self.cache.list_cached_versions()

        # Parse and filter compatible versions
        compatible = []
        for version_str in available_versions:
            try:
                version = SchemaVersion.parse(version_str)
                if constraint.matches(version):
                    compatible.append(version)
            except ValueError:
                continue

        # Return the highest compatible version
        return max(compatible) if compatible else None

    def validate_contract(
        self,
        contract: Dict[str, Any],
        schema_version: Optional[Union[str, SchemaVersion]] = None,
        strict: bool = True,
        offline_only: bool = False,
    ) -> ValidationResult:
        """
        Validate a FLUID contract against appropriate schema.

        Args:
            contract: Contract dictionary to validate
            schema_version: Specific version to validate against (auto-detect if None)
            strict: Whether to fail on warnings
            offline_only: Only use local schemas

        Returns:
            ValidationResult with details
        """
        import time

        start_time = time.time()

        result = ValidationResult(is_valid=True)

        # Determine schema version
        if schema_version is None:
            detected = self.detect_version(contract)
            if detected is None:
                result.add_error("No fluidVersion specified in contract")
                return result
            schema_version = detected
        elif isinstance(schema_version, str):
            try:
                schema_version = SchemaVersion.parse(schema_version)
            except ValueError as e:
                result.add_error(f"Invalid schema version: {e}")
                return result

        result.schema_version = schema_version

        # Get appropriate schema
        schema = self.get_schema(schema_version, offline_only=offline_only)
        if schema is None:
            result.add_error(f"Schema not available for version {schema_version}")
            return result

        # Perform validation
        try:
            if JSONSCHEMA_AVAILABLE and "$schema" in schema:
                # Use JSON Schema validation
                is_valid = self._validate_with_jsonschema(contract, schema, result)
            else:
                # Fallback to existing FLUID validator for 0.4.x or when jsonschema unavailable
                is_valid = self._validate_with_fluid_validator(contract, schema_version, result)

            if not is_valid:
                result.is_valid = False

        except Exception as e:
            result.add_error(f"Validation failed with error: {str(e)}")

        # Version compatibility warnings
        if schema_version.major == 0 and schema_version.minor < 4:
            result.add_warning(f"Schema version {schema_version} is deprecated")

        result.validation_time = time.time() - start_time
        return result

    def _validate_with_jsonschema(
        self, contract: Dict[str, Any], schema: Dict[str, Any], result: ValidationResult
    ) -> bool:
        """Validate using JSON Schema library."""
        try:
            # Create validator
            validator = jsonschema.Draft7Validator(schema)

            # Validate and collect errors
            errors = sorted(validator.iter_errors(contract), key=lambda e: e.path)

            for error in errors:
                # Build path string
                path_parts = []
                for part in error.path:
                    if isinstance(part, int):
                        path_parts.append(f"[{part}]")
                    else:
                        if path_parts:
                            path_parts.append(f".{part}")
                        else:
                            path_parts.append(str(part))

                path_str = "".join(path_parts) if path_parts else "root"
                error_msg = f"{path_str}: {error.message}"
                result.add_error(error_msg)

            return len(errors) == 0

        except Exception as e:
            result.add_error(f"JSON Schema validation error: {str(e)}")
            return False

    def _validate_with_fluid_validator(
        self, contract: Dict[str, Any], schema_version: SchemaVersion, result: ValidationResult
    ) -> bool:
        """Validate using existing FLUID validator (fallback)."""
        if str(schema_version).startswith("0.4") or str(schema_version).startswith("0.5"):
            # Use JSON schema validator for 0.4.x and 0.5.x
            try:
                from fluid_build.schema import validate_contract

                is_valid, error_msg = validate_contract(contract)
                if not is_valid and error_msg:
                    for error in error_msg.split("\n"):
                        if error.strip():
                            result.add_error(error.strip())
                return is_valid
            except ImportError as e:
                result.add_error(f"FLUID validator not available: {str(e)}")
                return False
        else:
            # For other versions, basic validation
            result.add_warning(f"Using basic validation for v{schema_version}")

            # Basic validation
            required_fields = [
                "fluidVersion",
                "kind",
                "id",
                "name",
                "domain",
                "metadata",
                "exposes",
            ]
            for field in required_fields:
                if field not in contract:
                    result.add_error(f"Missing required field: {field}")

            return len(result.errors) == 0

    def list_available_versions(self, include_remote: bool = False) -> List[str]:
        """List all available schema versions."""
        versions = set(self.BUNDLED_VERSIONS)
        versions.update(self.cache.list_cached_versions())

        if include_remote:
            # In a real implementation, this would query the GitHub API
            # for available schema versions
            pass

        return sorted(versions, key=lambda v: SchemaVersion.parse(v))

    def clear_cache(self) -> int:
        """Clear schema cache."""
        return self.cache.clear_cache()


# Convenience functions for CLI usage
def create_schema_manager(
    cache_dir: Optional[Path] = None, logger: Optional[logging.Logger] = None
) -> FluidSchemaManager:
    """Create a schema manager instance."""
    return FluidSchemaManager(cache_dir=cache_dir, logger=logger)


# Backward-compatible alias for older call sites.
SchemaManager = FluidSchemaManager


def validate_contract_file(
    file_path: str,
    schema_version: Optional[str] = None,
    strict: bool = True,
    offline_only: bool = False,
    logger: Optional[logging.Logger] = None,
) -> ValidationResult:
    """
    Validate a FLUID contract file.

    Args:
        file_path: Path to contract file
        schema_version: Specific version to validate against
        strict: Whether to fail on warnings
        offline_only: Only use local schemas
        logger: Logger instance

    Returns:
        ValidationResult
    """
    try:
        # Load contract
        with open(file_path) as f:
            if file_path.endswith(".yaml") or file_path.endswith(".yml"):
                contract = yaml.safe_load(f)
            else:
                contract = json.load(f)

        # Validate
        manager = create_schema_manager(logger=logger)
        return manager.validate_contract(
            contract, schema_version=schema_version, strict=strict, offline_only=offline_only
        )

    except FileNotFoundError:
        result = ValidationResult(is_valid=False)
        result.add_error(f"Contract file not found: {file_path}")
        return result
    except (yaml.YAMLError, json.JSONDecodeError) as e:
        result = ValidationResult(is_valid=False)
        result.add_error(f"Invalid contract file format: {e}")
        return result
    except Exception as e:
        result = ValidationResult(is_valid=False)
        result.add_error(f"Error reading contract file: {e}")
        return result
