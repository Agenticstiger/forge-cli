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

"""Extension-routed validators for the 11-stage pipeline stage-2 gate.

Routes files inside a ``.tgz`` bundle to the right validator:

    MANIFEST.json          → Phase-2 validate_manifest (tamper check)
    contract.resolved.yaml → JSON Schema validator, after $source unwrap
    sources/sql/*          → sqlglot parse with dialect from binding.platform
    sources/openapi/*      → openapi-spec-validator

Everything pluggable. ``sqlglot`` and ``openapi_spec_validator`` are
soft-imported — absent = INFO skip unless ``--strict``.

The ``unwrap_source_pointers`` helper is the key move that lets Phase 3
ship without a schema bump: ``{"$source": "sources/..."}`` objects in
``contract.resolved.yaml`` are replaced with the *parsed* content of the
referenced file before JSON Schema validation runs. The schema never
sees the sentinel.
"""

from __future__ import annotations

import copy
import importlib.util
import io
import json
import logging
import tarfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, Optional, Tuple

import yaml

from fluid_build.forge.core.bundle import SOURCE_SENTINEL, validate_manifest

LOG = logging.getLogger("fluid.forge.core.validators")

Severity = Literal["error", "warning", "info"]


@dataclass
class ValidationIssue:
    """One finding from any validator — uniform shape across all tools.

    ``file`` is bundle-relative (``sources/sql/builds_0__x.sql``) so the
    report is portable. ``line``/``column`` are 1-indexed (sqlglot's
    convention); None when the validator can't pinpoint a location.
    """

    file: str
    validator: str
    severity: Severity
    message: str
    line: Optional[int] = None
    column: Optional[int] = None
    code: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "file": self.file,
            "validator": self.validator,
            "severity": self.severity,
            "message": self.message,
        }
        if self.line is not None:
            d["line"] = self.line
        if self.column is not None:
            d["column"] = self.column
        if self.code is not None:
            d["code"] = self.code
        return d


# ---------------------------------------------------------------------------
# $source unwrapping — the schema-change-avoiding trick
# ---------------------------------------------------------------------------


def unwrap_source_pointers(
    doc: Any,
    resolver: Callable[[str], bytes],
) -> Any:
    """Deep-copy ``doc``; replace every ``{"$source": path}`` dict with the
    parsed content of the file at ``path`` (resolved via ``resolver``).

    Per-extension parsing (matches Phase-2 extraction rules):

    * ``.sql`` → UTF-8 string
    * ``.yaml`` / ``.yml`` → ``yaml.safe_load(...)``
    * ``.json`` → ``json.loads(...)``
    * anything else → UTF-8 string (defensive fallback)

    Malformed sentinel shapes raise ``ValueError`` so validation halts
    with a clear error pointing at the bad pointer.
    """
    # Base case: a $source pointer dict.
    if isinstance(doc, dict) and SOURCE_SENTINEL in doc and len(doc) == 1:
        path = doc[SOURCE_SENTINEL]
        if not isinstance(path, str):
            raise ValueError(
                f"{SOURCE_SENTINEL!r} value must be a string; got {type(path).__name__}"
            )
        raw = resolver(path)
        ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
        if ext == "sql":
            return raw.decode("utf-8")
        if ext in ("yaml", "yml"):
            return yaml.safe_load(raw)
        if ext == "json":
            return json.loads(raw.decode("utf-8"))
        return raw.decode("utf-8")

    # Recurse into lists and dicts.
    if isinstance(doc, list):
        return [unwrap_source_pointers(item, resolver) for item in doc]
    if isinstance(doc, dict):
        return {key: unwrap_source_pointers(value, resolver) for key, value in doc.items()}

    # Scalars pass through unchanged.
    return doc


# ---------------------------------------------------------------------------
# Dialect inference
# ---------------------------------------------------------------------------


_BINDING_TO_SQLGLOT_DIALECT: Dict[str, str] = {
    "snowflake": "snowflake",
    "bigquery": "bigquery",
    "gcp": "bigquery",
    "redshift": "redshift",
    "aws": "redshift",
    "postgres": "postgres",
    "postgresql": "postgres",
    "duckdb": "duckdb",
    "local": "duckdb",
    "spark": "spark",
    "databricks": "databricks",
}


def infer_sqlglot_dialect(contract: Dict[str, Any]) -> Optional[str]:
    """Pick the sqlglot dialect from ``binding.platform`` in a resolved contract.

    Unknown / missing platform → ``None`` (sqlglot auto-detects). Reading
    from the UNWRAPPED contract (``$source`` already resolved) is fine
    because ``binding.platform`` is always a plain string field.
    """
    binding = contract.get("binding")
    if not isinstance(binding, dict):
        return None
    platform = str(binding.get("platform", "")).strip().lower()
    return _BINDING_TO_SQLGLOT_DIALECT.get(platform)


# ---------------------------------------------------------------------------
# Soft-imported validators
# ---------------------------------------------------------------------------


def _sqlglot_available() -> bool:
    return importlib.util.find_spec("sqlglot") is not None


def _openapi_validator_available() -> bool:
    return importlib.util.find_spec("openapi_spec_validator") is not None


def validate_sql(
    path: str,
    content: bytes,
    *,
    dialect: Optional[str],
    strict: bool,
) -> List[ValidationIssue]:
    """Parse SQL with sqlglot; any ParseError becomes a ValidationIssue.

    ``dialect`` is passed through to ``sqlglot.parse`` so warehouse-specific
    syntax (Snowflake variants, BigQuery standard SQL, etc.) parses right.
    """
    text = content.decode("utf-8", errors="replace")

    # Jinja detection runs BEFORE the sqlglot-availability gate so it catches
    # the mistake even on machines without sqlglot installed. Pattern match
    # is a string scan — no parser needed.
    if "{{" in text or "{%" in text:
        return [
            ValidationIssue(
                file=path,
                validator="sqlglot",
                severity="error",
                message=(
                    "SQL fragment contains Jinja template markers ('{{' or '{%') — "
                    "sqlglot cannot parse Jinja. Move this to a dbt project and "
                    "reference it via transformation.dbt.project_dir instead of "
                    "inlining in the contract."
                ),
                code="SQL-JINJA",
                line=1,
            )
        ]

    if not _sqlglot_available():
        sev: Severity = "error" if strict else "info"
        msg = "sqlglot not installed; SQL fragment not validated. Install with: pip install sqlglot"
        return [ValidationIssue(file=path, validator="sqlglot", severity=sev, message=msg)]

    import sqlglot  # type: ignore
    from sqlglot.errors import ParseError  # type: ignore

    issues: List[ValidationIssue] = []
    try:
        sqlglot.parse(text, dialect=dialect)
    except ParseError as exc:
        for err in getattr(exc, "errors", None) or [{"description": str(exc)}]:
            issues.append(
                ValidationIssue(
                    file=path,
                    validator="sqlglot",
                    severity="error",
                    message=str(err.get("description", err)),
                    line=err.get("line"),
                    column=err.get("col"),
                    code="SQL001",
                )
            )
    except Exception as exc:  # pragma: no cover — defensive
        issues.append(
            ValidationIssue(
                file=path,
                validator="sqlglot",
                severity="error",
                message=f"sqlglot internal error: {exc}",
                code="SQL999",
            )
        )
    return issues


def validate_openapi(
    path: str,
    content: bytes,
    *,
    strict: bool,
) -> List[ValidationIssue]:
    """Validate OpenAPI spec bytes via openapi-spec-validator."""
    if not _openapi_validator_available():
        sev: Severity = "error" if strict else "info"
        return [
            ValidationIssue(
                file=path,
                validator="openapi-spec-validator",
                severity=sev,
                message=(
                    "openapi-spec-validator not installed; OpenAPI fragment not validated. "
                    "Install with: pip install openapi-spec-validator"
                ),
            )
        ]

    import openapi_spec_validator  # type: ignore

    # Parse YAML or JSON depending on extension. Accept both — bundle
    # always writes YAML, but nothing stops someone producing JSON.
    try:
        if path.endswith(".json"):
            spec = json.loads(content.decode("utf-8"))
        else:
            spec = yaml.safe_load(content)
    except (yaml.YAMLError, json.JSONDecodeError) as exc:
        return [
            ValidationIssue(
                file=path,
                validator="openapi-spec-validator",
                severity="error",
                message=f"parse failure: {exc}",
                code="OAS-PARSE",
            )
        ]

    issues: List[ValidationIssue] = []
    try:
        # validate_spec (legacy) and validate (current) both exist; prefer
        # whichever is available without importing an exact entry point.
        if hasattr(openapi_spec_validator, "validate"):
            openapi_spec_validator.validate(spec)
        else:
            openapi_spec_validator.validate_spec(spec)  # type: ignore[attr-defined]
    except Exception as exc:
        # openapi-spec-validator raises jsonschema.ValidationError subclasses
        # with .absolute_path + .message. Extract when present.
        abs_path = getattr(exc, "absolute_path", None)
        loc = "/".join(str(p) for p in abs_path) if abs_path else None
        msg = getattr(exc, "message", None) or str(exc)
        if loc:
            msg = f"at {loc}: {msg}"
        issues.append(
            ValidationIssue(
                file=path,
                validator="openapi-spec-validator",
                severity="error",
                message=msg,
                code="OAS001",
            )
        )
    return issues


# ---------------------------------------------------------------------------
# JSON Schema wrapper — keeps the existing validator untouched; formats its
# errors into the uniform ValidationIssue shape.
# ---------------------------------------------------------------------------


def validate_json_schema(
    path: str,
    doc: Any,
    *,
    schema_manager: Any,
    fluid_version: Optional[str] = None,
    strict: bool = False,
) -> List[ValidationIssue]:
    """Validate an unwrapped contract against the versioned JSON Schema.

    Delegates to the repo's existing ``FluidSchemaManager`` so we inherit
    version dispatch, overlays, and the shipped bundled schemas. Errors
    are converted to ``ValidationIssue`` entries with ``file``, schema
    path (``line=None`` since jsonschema reports JSON Pointer paths, not
    line numbers), and a stable validator tag.
    """
    issues: List[ValidationIssue] = []
    try:
        version = fluid_version or str(doc.get("fluidVersion") or "")
        if not version:
            return [
                ValidationIssue(
                    file=path,
                    validator="json-schema",
                    severity="error",
                    message="contract missing required field: fluidVersion",
                    code="SCHEMA-MISSING-VERSION",
                )
            ]
        schema = schema_manager.get_schema(version)
        try:
            import jsonschema  # type: ignore
        except ImportError:  # pragma: no cover — jsonschema is a hard dep upstream
            return [
                ValidationIssue(
                    file=path,
                    validator="json-schema",
                    severity="error",
                    message="jsonschema library not available",
                    code="SCHEMA-LIB-MISSING",
                )
            ]

        validator = jsonschema.Draft7Validator(schema)
        errors = sorted(validator.iter_errors(doc), key=lambda e: list(e.absolute_path))
        for err in errors:
            loc = "/".join(str(p) for p in err.absolute_path) or "<root>"
            issues.append(
                ValidationIssue(
                    file=path,
                    validator="json-schema",
                    severity="error",
                    message=f"at {loc}: {err.message}",
                    code="SCHEMA001",
                )
            )
    except Exception as exc:  # pragma: no cover — defensive
        sev: Severity = "error" if strict else "warning"
        issues.append(
            ValidationIssue(
                file=path,
                validator="json-schema",
                severity=sev,
                message=f"schema validation internal error: {exc}",
                code="SCHEMA999",
            )
        )
    return issues


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


@dataclass
class BundleValidationReport:
    """Uniform report emitted regardless of pass/fail."""

    bundle_digest: str
    input_path: str
    strict: bool
    status: Literal["pass", "fail"]
    issues: List[ValidationIssue] = field(default_factory=list)

    @property
    def summary(self) -> Dict[str, int]:
        s = {"total": len(self.issues), "error": 0, "warning": 0, "info": 0}
        for i in self.issues:
            s[i.severity] = s.get(i.severity, 0) + 1
        return s

    def to_dict(self) -> Dict[str, Any]:
        return {
            "bundleDigest": self.bundle_digest,
            "input": self.input_path,
            "strict": self.strict,
            "status": self.status,
            "summary": self.summary,
            "issues": [i.to_dict() for i in self.issues],
        }


def _read_tgz_file(tar: tarfile.TarFile, path: str) -> bytes:
    member = tar.extractfile(path)
    if member is None:
        raise ValueError(f"{path!r} is not a regular file in bundle")
    return member.read()


def validate_bundle(
    tgz_path: Path,
    *,
    schema_manager: Any,
    strict: bool = False,
    fail_fast: bool = False,
) -> BundleValidationReport:
    """Top-level orchestrator for ``fluid validate X.tgz``.

    Flow:
      1. ``validate_manifest`` (tamper gate) — raises on mismatch.
      2. Iterate MANIFEST files in sorted order; dispatch by path pattern.
      3. ``contract.resolved.yaml``: unwrap ``$source`` pointers, run JSON
         Schema, also check for ``transformation.dbt.project_dir`` to emit
         the "validate dbt separately" INFO.
      4. ``sources/sql/*``: sqlglot parse.
      5. ``sources/openapi/*``: openapi-spec-validator.

    On ``fail_fast``, stop at first error-severity issue. Warnings and
    infos never stop traversal regardless of flag (they're advisory).
    ``strict`` escalates warnings to errors in the final status.
    """
    # 1. Tamper gate. Any mismatch here short-circuits — no point validating
    #    per-file content if the bundle was tampered with.
    try:
        validate_manifest(tgz_path)
    except ValueError as exc:
        return BundleValidationReport(
            bundle_digest="",
            input_path=str(tgz_path),
            strict=strict,
            status="fail",
            issues=[
                ValidationIssue(
                    file=str(tgz_path),
                    validator="manifest",
                    severity="error",
                    message=str(exc),
                    code="MANIFEST-TAMPER",
                )
            ],
        )

    issues: List[ValidationIssue] = []

    with tarfile.open(tgz_path, "r:gz") as tar:
        manifest_bytes = _read_tgz_file(tar, "MANIFEST.json")
        manifest = json.loads(manifest_bytes.decode("utf-8"))
        digest = str(manifest.get("digest", ""))
        declared_files = sorted((manifest.get("files") or {}).keys())

        # Pre-cache source bytes so $source unwrapping can read cheaply.
        source_cache: Dict[str, bytes] = {}

        def _resolve(path: str) -> bytes:
            if path not in source_cache:
                try:
                    source_cache[path] = _read_tgz_file(tar, path)
                except (KeyError, ValueError) as exc:
                    raise ValueError(f"$source points at missing bundle file {path!r}") from exc
            return source_cache[path]

        # Parse the resolved contract once — we need binding.platform for
        # dialect inference AND we'll JSON-schema-validate it.
        resolved_contract: Optional[Dict[str, Any]] = None
        dialect: Optional[str] = None
        if "contract.resolved.yaml" in declared_files:
            try:
                raw_doc = yaml.safe_load(_read_tgz_file(tar, "contract.resolved.yaml"))
                resolved_contract = unwrap_source_pointers(raw_doc, _resolve)
                dialect = infer_sqlglot_dialect(resolved_contract)
            except (yaml.YAMLError, ValueError) as exc:
                issues.append(
                    ValidationIssue(
                        file="contract.resolved.yaml",
                        validator="json-schema",
                        severity="error",
                        message=f"parse or $source-unwrap failure: {exc}",
                        code="RESOLVED-PARSE",
                    )
                )

        # Iterate files deterministically; dispatch per pattern.
        for path in declared_files:
            if path == "MANIFEST.json":
                continue  # already re-verified
            if path == "contract.resolved.json":
                # The YAML twin already carries the authoritative validation;
                # skip the JSON twin to avoid duplicate issues.
                continue
            if path == "contract.resolved.yaml":
                if resolved_contract is not None:
                    issues.extend(
                        validate_json_schema(
                            path,
                            resolved_contract,
                            schema_manager=schema_manager,
                            strict=strict,
                        )
                    )
                continue
            if path.startswith("sources/sql/"):
                issues.extend(validate_sql(path, _resolve(path), dialect=dialect, strict=strict))
            elif path.startswith("sources/openapi/"):
                issues.extend(validate_openapi(path, _resolve(path), strict=strict))
            else:
                issues.append(
                    ValidationIssue(
                        file=path,
                        validator="manifest",
                        severity="warning",
                        message=(
                            f"unexpected file in bundle: {path!r} — not a recognised "
                            f"sources/ fragment or canonical bundle entry"
                        ),
                        code="BUNDLE-UNEXPECTED",
                    )
                )
            if fail_fast and any(i.severity == "error" for i in issues):
                break

        # Emit the dbt-external-project INFO if applicable. Read from the
        # unwrapped contract so it fires regardless of which build uses it.
        if resolved_contract is not None and not any(i.code == "DBT-EXTERNAL" for i in issues):
            for build in resolved_contract.get("builds") or []:
                if not isinstance(build, dict):
                    continue
                transformation = build.get("transformation")
                if isinstance(transformation, dict):
                    dbt = transformation.get("dbt")
                    if isinstance(dbt, dict) and dbt.get("project_dir"):
                        issues.append(
                            ValidationIssue(
                                file="contract.resolved.yaml",
                                validator="dbt",
                                severity="info",
                                message=(
                                    f"contract references external dbt project at "
                                    f"{dbt['project_dir']!r}; fluid validate does not "
                                    f"parse dbt SQL. Run `dbt parse --project-dir "
                                    f"{dbt['project_dir']}` separately in the team's CI."
                                ),
                                code="DBT-EXTERNAL",
                            )
                        )
                        break

    # Status: strict escalates warnings to errors; otherwise only errors fail.
    has_error = any(i.severity == "error" for i in issues)
    has_warning = any(i.severity == "warning" for i in issues)
    status: Literal["pass", "fail"] = "fail" if has_error or (strict and has_warning) else "pass"

    return BundleValidationReport(
        bundle_digest=digest,
        input_path=str(tgz_path),
        strict=strict,
        status=status,
        issues=issues,
    )


__all__ = [
    "BundleValidationReport",
    "Severity",
    "ValidationIssue",
    "infer_sqlglot_dialect",
    "unwrap_source_pointers",
    "validate_bundle",
    "validate_json_schema",
    "validate_openapi",
    "validate_sql",
]
