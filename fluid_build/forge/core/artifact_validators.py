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

"""Per-format validators + orchestrator for ``fluid validate artifacts``.

Stage-4 gate of the 11-stage pipeline. Reads a directory produced by
stage 3 (`fluid generate artifacts`), re-verifies every file's SHA-256
against MANIFEST.json, then dispatches per-path-prefix to format-specific
validators:

    MANIFEST.json          → SHA-256 re-verify (tamper gate)
    odcs/*.odcs.yaml       → JSON Schema (vendored ODCS v3.1.0)
    odps-bitol/*.yaml      → JSON Schema (vendored ODPS-Bitol v1.0.0)
    odps/*.odps.json       → (opt-in) ODPS v4.1 (LF/ODPI) validation — pending fix
    opds/*.opds.json       → (back-compat) same as odps/ — kept for legacy callers
    schedule/dags/*.py     → py_compile
    schedule/flows/*.py    → py_compile
    policy/bindings.json   → key-check + OPA conftest (optional)
    <dir>/dbt/             → dbt parse (optional, for speed-transformation output)

Reuses ``ValidationIssue`` / ``BundleValidationReport`` from Phase 3's
stage-2 validator so the report format is identical — one jq, one
dashboard, one mental model across stages 2 and 4.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import yaml

from fluid_build.forge.core.validators import (
    BundleValidationReport,
    ValidationIssue,
    validate_sql,  # reused for any SQL artifacts that might land
)

LOG = logging.getLogger("fluid.forge.core.artifact_validators")

# Vendored schema paths. Each provider dir owns its upstream-vendored schema,
# matching the ODCS convention (``providers/odcs/odcs-schema-v3.1.0.json``).
# Kept OUT of ``fluid_build/schemas/`` — that namespace is for the FLUID
# *contract* schemas (fluid-schema-0.7.x.json), not third-party specs.
#
# Both pinned to explicit version files (NOT the moving ``-latest.json``
# symlinks) so validation behavior is stable across upstream drift. The
# schema-drift CI workflow (separate Trello card) re-checks these against
# upstream on a schedule and opens issues when a new version is tagged.
_REPO_ROOT: Path = Path(__file__).parent.parent.parent
_ODCS_SCHEMA_PATH: Path = _REPO_ROOT / "providers" / "odcs" / "odcs-schema-v3.1.0.json"
_ODPS_BITOL_SCHEMA_PATH: Path = (
    _REPO_ROOT / "providers" / "odps_standard" / "schemas" / "odps-product-v1.0.0.json"
)


# ---------------------------------------------------------------------------
# Schema cache — load once per process
# ---------------------------------------------------------------------------


_schema_cache: Dict[str, Any] = {}


def _load_schema(path: Path) -> Optional[Dict[str, Any]]:
    """Load a vendored JSON Schema from disk, cached by path.

    Returns ``None`` when the schema file isn't present (e.g., fresh
    checkout before the vendor step ran). Callers emit an INFO issue in
    that case rather than crashing — tests can run without hitting the
    network to verify schemas exist.
    """
    key = str(path)
    if key in _schema_cache:
        return _schema_cache[key]
    if not path.exists():
        _schema_cache[key] = None
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            schema = json.load(fh)
        _schema_cache[key] = schema
        return schema
    except (OSError, json.JSONDecodeError) as exc:
        LOG.warning("Failed to load vendored schema at %s: %s", path, exc)
        _schema_cache[key] = None
        return None


# ---------------------------------------------------------------------------
# Soft-import helpers
# ---------------------------------------------------------------------------


def _jsonschema_available() -> bool:
    return importlib.util.find_spec("jsonschema") is not None


def _conftest_available() -> bool:
    """``conftest`` binary on PATH — independent of python package install."""
    return shutil.which("conftest") is not None


def _dbt_available() -> bool:
    return shutil.which("dbt") is not None


# ---------------------------------------------------------------------------
# MANIFEST re-verification for a directory (stage-4 analog of Phase-2 tgz check)
# ---------------------------------------------------------------------------


def validate_manifest_dir(
    artifacts_dir: Path, manifest_path: Optional[Path] = None
) -> List[ValidationIssue]:
    """Re-verify every file in ``artifacts_dir`` against MANIFEST.json.

    Matches Phase-2's ``validate_manifest`` semantics but for a directory
    rather than a tgz. Catches:

    - Missing MANIFEST.json itself
    - Corrupt / non-JSON MANIFEST
    - Declared file absent from disk
    - File on disk absent from MANIFEST (undeclared extra)
    - SHA-256 mismatch on any declared file
    - Merkle root mismatch

    Returns a list of ``ValidationIssue`` — empty list = pass.
    """
    manifest_path = manifest_path or (artifacts_dir / "MANIFEST.json")
    issues: List[ValidationIssue] = []

    if not manifest_path.exists():
        issues.append(
            ValidationIssue(
                file=str(manifest_path),
                validator="manifest",
                severity="error",
                message=f"MANIFEST.json missing at {manifest_path}",
                code="MANIFEST-MISSING",
            )
        )
        return issues

    try:
        with open(manifest_path, "r", encoding="utf-8") as fh:
            manifest = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        issues.append(
            ValidationIssue(
                file=str(manifest_path),
                validator="manifest",
                severity="error",
                message=f"MANIFEST parse failure: {exc}",
                code="MANIFEST-PARSE",
            )
        )
        return issues

    declared = manifest.get("files") or {}
    expected_paths = set(declared.keys())

    # Actual files on disk (recursive, relative to artifacts_dir; exclude
    # MANIFEST.json itself and anything starting with .).
    actual_paths: set = set()
    for fp in artifacts_dir.rglob("*"):
        if not fp.is_file():
            continue
        rel = fp.relative_to(artifacts_dir).as_posix()
        if rel == manifest_path.relative_to(artifacts_dir).as_posix():
            continue
        if rel.startswith(".") or "/." in rel:
            continue  # hidden files (.DS_Store etc.)
        actual_paths.add(rel)

    missing = expected_paths - actual_paths
    extra = actual_paths - expected_paths
    for path in sorted(missing):
        issues.append(
            ValidationIssue(
                file=path,
                validator="manifest",
                severity="error",
                message=(f"declared in MANIFEST but absent from {artifacts_dir}"),
                code="MANIFEST-MISSING-FILE",
            )
        )
    for path in sorted(extra):
        issues.append(
            ValidationIssue(
                file=path,
                validator="manifest",
                severity="warning",
                message=(f"present in {artifacts_dir} but not declared in MANIFEST"),
                code="MANIFEST-UNDECLARED-FILE",
            )
        )

    # Per-file SHA re-verify + merkle input rebuild. Sort for deterministic
    # merkle reconstruction (matches Phase-2 bundle builder order).
    merkle_input = ""
    for path in sorted(expected_paths):
        fp = artifacts_dir / path
        if not fp.exists():
            continue  # already flagged above
        actual_hash = "sha256:" + hashlib.sha256(fp.read_bytes()).hexdigest()
        expected = declared[path]
        if actual_hash != expected:
            issues.append(
                ValidationIssue(
                    file=path,
                    validator="manifest",
                    severity="error",
                    message=(f"SHA-256 mismatch: expected {expected}, got {actual_hash}"),
                    code="MANIFEST-SHA-MISMATCH",
                )
            )
        merkle_input += f"{path}:{actual_hash}\n"

    expected_merkle = manifest.get("digest", "")
    actual_merkle = "sha256:" + hashlib.sha256(merkle_input.encode("utf-8")).hexdigest()
    if missing:
        # Merkle check is meaningful only when the file set matches. Skip to
        # avoid a second confusing error on top of the missing-file one.
        pass
    elif actual_merkle != expected_merkle:
        issues.append(
            ValidationIssue(
                file=str(manifest_path),
                validator="manifest",
                severity="error",
                message=(f"merkle root mismatch: expected {expected_merkle}, got {actual_merkle}"),
                code="MANIFEST-MERKLE-MISMATCH",
            )
        )

    return issues


# ---------------------------------------------------------------------------
# Per-format validators
# ---------------------------------------------------------------------------


def _validate_against_schema(
    artifact_path: str,
    content: bytes,
    schema: Optional[Dict[str, Any]],
    *,
    validator_name: str,
    code_prefix: str,
) -> List[ValidationIssue]:
    """Common path for JSON Schema validation of a YAML or JSON artifact."""
    if schema is None:
        return [
            ValidationIssue(
                file=artifact_path,
                validator=validator_name,
                severity="info",
                message=("schema not vendored; skipped. Expected at one of the schemas/ paths."),
                code=f"{code_prefix}-SCHEMA-MISSING",
            )
        ]

    # Parse YAML or JSON depending on extension.
    # SECURITY (billion-laughs DoS): generated/vendored artifacts are
    # untrusted content — route YAML through ``load_yaml_safe`` (50-alias +
    # 5 MiB caps) instead of bare ``yaml.safe_load`` so a hostile anchor-
    # expansion payload can't OOM the artifact validator.
    from fluid_build.util.safe_yaml import UnsafeYamlError, load_yaml_safe

    try:
        if artifact_path.endswith(".json"):
            doc = json.loads(content.decode("utf-8"))
        else:
            doc = load_yaml_safe(content)
    except (yaml.YAMLError, json.JSONDecodeError, UnsafeYamlError) as exc:
        return [
            ValidationIssue(
                file=artifact_path,
                validator=validator_name,
                severity="error",
                message=f"parse failure: {exc}",
                code=f"{code_prefix}-PARSE",
            )
        ]

    if not _jsonschema_available():
        return [
            ValidationIssue(
                file=artifact_path,
                validator=validator_name,
                severity="info",
                message=(
                    "jsonschema library not available; skipping schema "
                    "validation. Install with: pip install jsonschema"
                ),
                code=f"{code_prefix}-LIB-MISSING",
            )
        ]

    import jsonschema  # type: ignore

    issues: List[ValidationIssue] = []
    try:
        validator = jsonschema.Draft7Validator(schema)
    except Exception:
        # ODPS-Bitol uses draft 2019-09 — fall back to generic Validator.
        validator = jsonschema.validators.validator_for(schema)(schema)

    errors = sorted(validator.iter_errors(doc), key=lambda e: list(e.absolute_path))
    for err in errors:
        loc = "/".join(str(p) for p in err.absolute_path) or "<root>"
        issues.append(
            ValidationIssue(
                file=artifact_path,
                validator=validator_name,
                severity="error",
                message=f"at {loc}: {err.message}",
                code=f"{code_prefix}001",
            )
        )
    return issues


def validate_odcs(path: str, content: bytes) -> List[ValidationIssue]:
    """Validate an ODCS YAML artifact against the vendored ODCS v3.1.0 schema."""
    return _validate_against_schema(
        path,
        content,
        _load_schema(_ODCS_SCHEMA_PATH),
        validator_name="odcs",
        code_prefix="ODCS",
    )


def validate_odps_bitol(path: str, content: bytes) -> List[ValidationIssue]:
    """Validate an ODPS-Bitol YAML artifact against the vendored v1.0.0 schema."""
    return _validate_against_schema(
        path,
        content,
        _load_schema(_ODPS_BITOL_SCHEMA_PATH),
        validator_name="odps-bitol",
        code_prefix="ODPS",
    )


def validate_dag_python(path: str, content: bytes) -> List[ValidationIssue]:
    """``python -m py_compile`` on a DAG file; any SyntaxError flagged with line.

    Uses :data:`sys.executable` rather than a hard-coded ``"python"`` so
    the test runs portably on systems where only ``python3`` is on
    ``$PATH`` (modern macOS / Debian 12+ / venvs that don't shim the
    unsuffixed name).
    """
    import sys
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".py", delete=False) as fh:
        fh.write(content)
        tmp_path = fh.name

    try:
        result = subprocess.run(
            [sys.executable, "-m", "py_compile", tmp_path],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            return []

        # py_compile writes "File <tmp>, line N" — try to extract line number.
        stderr = result.stderr.strip()
        line_num: Optional[int] = None
        for token in stderr.split():
            if token.isdigit():
                line_num = int(token)
                break

        return [
            ValidationIssue(
                file=path,
                validator="py_compile",
                severity="error",
                message=stderr or f"py_compile exited {result.returncode}",
                line=line_num,
                code="PY001",
            )
        ]
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [
            ValidationIssue(
                file=path,
                validator="py_compile",
                severity="error",
                message=f"py_compile invocation failed: {exc}",
                code="PY999",
            )
        ]
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def validate_bindings_json(path: str, content: bytes) -> List[ValidationIssue]:
    """Structural key-check on ``policy/bindings.json``.

    The compiled-bindings file must have a ``bindings`` top-level key
    pointing at a list; each entry needs ``provider`` (so stage-8
    policy-apply can dispatch) + ``principal``. Missing keys flagged with
    path so the operator can locate the bad binding quickly.
    """
    try:
        doc = json.loads(content.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return [
            ValidationIssue(
                file=path,
                validator="bindings",
                severity="error",
                message=f"parse failure: {exc}",
                code="BINDINGS-PARSE",
            )
        ]

    issues: List[ValidationIssue] = []
    if not isinstance(doc, dict):
        return [
            ValidationIssue(
                file=path,
                validator="bindings",
                severity="error",
                message=f"expected object at root, got {type(doc).__name__}",
                code="BINDINGS-SHAPE",
            )
        ]

    bindings = doc.get("bindings")
    if bindings is None:
        issues.append(
            ValidationIssue(
                file=path,
                validator="bindings",
                severity="error",
                message="missing required key: bindings[]",
                code="BINDINGS-MISSING-ARRAY",
            )
        )
        return issues

    if not isinstance(bindings, list):
        issues.append(
            ValidationIssue(
                file=path,
                validator="bindings",
                severity="error",
                message=f"'bindings' must be a list, got {type(bindings).__name__}",
                code="BINDINGS-SHAPE",
            )
        )
        return issues

    for idx, entry in enumerate(bindings):
        if not isinstance(entry, dict):
            issues.append(
                ValidationIssue(
                    file=path,
                    validator="bindings",
                    severity="error",
                    message=f"bindings[{idx}] must be an object",
                    code="BINDINGS-SHAPE",
                )
            )
            continue
        if "provider" not in entry:
            issues.append(
                ValidationIssue(
                    file=path,
                    validator="bindings",
                    severity="error",
                    message=f"bindings[{idx}] missing required key: provider",
                    code="BINDINGS-MISSING-KEY",
                )
            )
        if "principal" not in entry:
            issues.append(
                ValidationIssue(
                    file=path,
                    validator="bindings",
                    severity="error",
                    message=f"bindings[{idx}] missing required key: principal",
                    code="BINDINGS-MISSING-KEY",
                )
            )

    return issues


def validate_opa_conftest(
    bindings_path: Path,
    policy_dir: Path,
    *,
    strict: bool,
) -> List[ValidationIssue]:
    """Run ``conftest test <bindings.json> --policy <policy_dir>``.

    ``policy_dir`` is expected to contain ``*.rego`` files. If the dir
    doesn't exist or is empty, this is a no-op (not an error) — not every
    product has an OPA suite. When conftest binary is absent, emit INFO
    (non-strict) or ERROR (strict).
    """
    # No policy dir → silent skip; OPA is opt-in per product.
    if not policy_dir.exists() or not policy_dir.is_dir():
        return []
    rego_files = list(policy_dir.glob("*.rego"))
    if not rego_files:
        return []

    if not _conftest_available():
        sev = "error" if strict else "info"
        return [
            ValidationIssue(
                file=str(bindings_path),
                validator="opa-conftest",
                severity=sev,
                message=(
                    "conftest binary not on PATH; OPA policy tests skipped. "
                    "Install with: brew install conftest  # or go install github.com/open-policy-agent/conftest@latest"
                ),
                code="OPA-MISSING",
            )
        ]

    try:
        result = subprocess.run(
            [
                "conftest",
                "test",
                str(bindings_path),
                "--policy",
                str(policy_dir),
                "--output",
                "json",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [
            ValidationIssue(
                file=str(bindings_path),
                validator="opa-conftest",
                severity="error",
                message=f"conftest invocation failed: {exc}",
                code="OPA-EXEC",
            )
        ]

    # conftest exits 0 on pass, 1 on policy failures, 2 on invocation errors.
    if result.returncode == 0:
        return []

    issues: List[ValidationIssue] = []
    try:
        # conftest --output json emits a list of file-result objects.
        payload = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        # Fallback: surface stderr + stdout as a single error.
        return [
            ValidationIssue(
                file=str(bindings_path),
                validator="opa-conftest",
                severity="error",
                message=(result.stdout + result.stderr).strip() or "conftest failed",
                code="OPA-FAIL",
            )
        ]

    for file_result in payload if isinstance(payload, list) else [payload]:
        for failure in file_result.get("failures") or []:
            issues.append(
                ValidationIssue(
                    file=str(bindings_path),
                    validator="opa-conftest",
                    severity="error",
                    message=failure.get("msg", "policy violation"),
                    code=str(failure.get("metadata", {}).get("rule", "OPA-DENY")),
                )
            )
        for warn in file_result.get("warnings") or []:
            issues.append(
                ValidationIssue(
                    file=str(bindings_path),
                    validator="opa-conftest",
                    severity="warning",
                    message=warn.get("msg", "policy warning"),
                    code=str(warn.get("metadata", {}).get("rule", "OPA-WARN")),
                )
            )

    # If conftest exited non-zero but we found no structured failures, surface
    # the raw output so nothing slips through silently.
    if result.returncode != 0 and not issues:
        issues.append(
            ValidationIssue(
                file=str(bindings_path),
                validator="opa-conftest",
                severity="error",
                message=(
                    f"conftest exit {result.returncode}: {(result.stderr or result.stdout).strip()}"
                ),
                code="OPA-FAIL",
            )
        )
    return issues


def validate_dbt_parse(project_dir: Path, *, strict: bool) -> List[ValidationIssue]:
    """Run ``dbt parse --no-partial-parse --project-dir <dir>``.

    Phase 3 deferred dbt parsing to stage 4 because stage-3 may emit a
    dbt project (from ``fluid generate speed-transformation``) that
    stage-4 should verify. Absent project dir = silent no-op. Missing
    dbt binary = INFO (non-strict) or ERROR (strict).
    """
    if not project_dir.exists() or not project_dir.is_dir():
        return []
    if not (project_dir / "dbt_project.yml").exists():
        return []

    if not _dbt_available():
        sev = "error" if strict else "info"
        return [
            ValidationIssue(
                file=str(project_dir),
                validator="dbt-parse",
                severity=sev,
                message=(
                    "dbt binary not on PATH; dbt parse skipped. "
                    "Install with: pip install dbt-core dbt-<adapter>"
                ),
                code="DBT-MISSING",
            )
        ]

    try:
        result = subprocess.run(
            [
                "dbt",
                "parse",
                "--no-partial-parse",
                "--project-dir",
                str(project_dir),
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [
            ValidationIssue(
                file=str(project_dir),
                validator="dbt-parse",
                severity="error",
                message=f"dbt parse invocation failed: {exc}",
                code="DBT-EXEC",
            )
        ]

    if result.returncode == 0:
        return []

    return [
        ValidationIssue(
            file=str(project_dir),
            validator="dbt-parse",
            severity="error",
            message=(
                result.stderr.strip()
                or result.stdout.strip()
                or f"dbt parse exit {result.returncode}"
            ),
            code="DBT-PARSE",
        )
    ]


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def _dispatch_by_prefix(path: str, content: bytes) -> Callable[[str, bytes], List[ValidationIssue]]:
    """Return the validator function for a bundle-relative path.

    Note: the ``odps-bitol/`` bundle is intentionally
    **self-contained** — it ships the ODPS product file (``*.odps.*``)
    alongside its sibling ODCS contracts (``*.odcs.*``) so every
    ``contractId`` resolves to a co-located file (see
    :func:`fluid_build.forge.core.artifact_fanout._emit_odps_bitol`).
    The dispatcher therefore checks the file-name suffix within the
    Bitol bundle, not just the directory prefix.
    """
    if path.startswith("odcs/"):
        return validate_odcs
    if path.startswith("odps-bitol/"):
        # Sibling ODCS contracts in the Bitol bundle must be validated
        # as ODCS, not as ODPS — they're an ODCS-shaped document.
        lower = path.lower()
        if any(lower.endswith(ext) for ext in (".odcs.yaml", ".odcs.yml", ".odcs.json")):
            return validate_odcs
        return validate_odps_bitol
    if path.startswith("schedule/") and path.endswith(".py"):
        return validate_dag_python
    if path == "policy/bindings.json":
        return validate_bindings_json
    # Unknown — bind a no-op validator that flags it as a warning.
    return _flag_unexpected


def _flag_unexpected(path: str, content: bytes) -> List[ValidationIssue]:
    return [
        ValidationIssue(
            file=path,
            validator="manifest",
            severity="warning",
            message=(
                f"unexpected artifact: no validator registered for {path!r} "
                "(not a recognised odcs/odps-bitol/schedule/policy entry)"
            ),
            code="ARTIFACT-UNEXPECTED",
        )
    ]


def validate_artifacts(
    artifacts_dir: Path,
    *,
    manifest_path: Optional[Path] = None,
    opa_policy_dir: Optional[Path] = None,
    strict: bool = False,
    fail_fast: bool = False,
) -> BundleValidationReport:
    """Top-level orchestrator for ``fluid validate artifacts``.

    Flow:
      1. ``validate_manifest_dir`` (tamper gate — SHA-256 + merkle check)
         On any MANIFEST-level error, short-circuit: per-file validators
         don't run because the bytes we'd feed them are untrusted.
      2. For each file declared in MANIFEST: dispatch to the per-format
         validator by path prefix.
      3. If ``policy/bindings.json`` is present + ``opa_policy_dir`` has
         ``*.rego`` files: run ``conftest test``.
      4. If ``<artifacts_dir>/dbt/dbt_project.yml`` exists: run ``dbt
         parse --no-partial-parse``.

    ``strict=True`` escalates warnings to error-status for the final
    ``status`` field. ``fail_fast=True`` stops traversal at the first
    error-severity issue.
    """
    artifacts_dir = Path(artifacts_dir)
    manifest_path = manifest_path or (artifacts_dir / "MANIFEST.json")

    # 1. Tamper gate.
    manifest_issues = validate_manifest_dir(artifacts_dir, manifest_path)
    manifest_errors = [i for i in manifest_issues if i.severity == "error"]

    # Load the digest from MANIFEST for the report header, best-effort.
    digest = ""
    if manifest_path.exists():
        try:
            with open(manifest_path, "r", encoding="utf-8") as fh:
                manifest_doc = json.load(fh)
            digest = str(manifest_doc.get("digest", ""))
        except (OSError, json.JSONDecodeError):
            pass

    if manifest_errors:
        # Short-circuit: don't validate untrusted bytes.
        return BundleValidationReport(
            bundle_digest=digest,
            input_path=str(artifacts_dir),
            strict=strict,
            status="fail",
            issues=manifest_issues,
        )

    issues: List[ValidationIssue] = list(manifest_issues)  # keep any warnings

    # 2. Per-file dispatch.
    with open(manifest_path, "r", encoding="utf-8") as fh:
        manifest_doc = json.load(fh)
    declared_files = sorted((manifest_doc.get("files") or {}).keys())

    for path in declared_files:
        fp = artifacts_dir / path
        if not fp.exists():
            continue  # missing files already flagged in phase 1
        content = fp.read_bytes()
        validator = _dispatch_by_prefix(path, content)
        issues.extend(validator(path, content))
        if fail_fast and any(i.severity == "error" for i in issues):
            break

    # 3. OPA conftest — only if bindings.json is present AND policy_dir has rego
    bindings_path = artifacts_dir / "policy" / "bindings.json"
    if bindings_path.exists() and opa_policy_dir:
        issues.extend(validate_opa_conftest(bindings_path, Path(opa_policy_dir), strict=strict))

    # 4. dbt parse — only if stage 3 emitted a dbt project alongside
    dbt_project_dir = artifacts_dir / "dbt"
    issues.extend(validate_dbt_parse(dbt_project_dir, strict=strict))

    # Compose final status: error-severity or (strict && warning-severity) → fail.
    has_error = any(i.severity == "error" for i in issues)
    has_warning = any(i.severity == "warning" for i in issues)
    status = "fail" if has_error or (strict and has_warning) else "pass"

    return BundleValidationReport(
        bundle_digest=digest,
        input_path=str(artifacts_dir),
        strict=strict,
        status=status,
        issues=issues,
    )


__all__ = [
    "validate_artifacts",
    "validate_bindings_json",
    "validate_dag_python",
    "validate_dbt_parse",
    "validate_manifest_dir",
    "validate_odcs",
    "validate_odps_bitol",
    "validate_opa_conftest",
]
