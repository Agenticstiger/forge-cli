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

"""Deterministic logical → physical type mapping engine (D5).

* **No external files.** The registry is bundled in
  :mod:`fluid_build.forge_datamodel.sql.registry`.
* **Pydantic results** — :class:`MappingResult` /
  :class:`ValidationReport` are Pydantic v2 models so callers can
  round-trip them through the staged store without custom encoders.
* **OSI-aware post-processor** — :meth:`fill_missing_dialects` takes a
  list of OSI ``expression.dialects[]`` the LLM produced and extends it
  with any missing target dialects, deterministically.

The engine is *advisory, not authoritative*: when the LLM has already
emitted a physical type for ``SNOWFLAKE``, :meth:`fill_missing_dialects`
leaves it in place unless the caller passes ``override=True``. The goal
is to back-fill gaps, not to overrule a human/LLM that was given more
context than a static table encodes.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

from fluid_build.forge_datamodel.sql.registry import (
    DEFAULTS,
    DIALECTS,
    LOGICAL_TYPES,
    REGISTRY_VERSION,
    DialectRule,
)

# ----------------------------------------------------------------------
# Public constants
# ----------------------------------------------------------------------


DEFAULT_DIALECTS: Tuple[str, ...] = ("ANSI_SQL", "SNOWFLAKE", "BIGQUERY", "POSTGRES", "DATABRICKS")
"""Dialects :meth:`DialectMapper.fill_missing_dialects` targets when no
explicit list is supplied. Matches the OSI ``expression.dialects[]``
shape the modeler emits for vendor-agnostic contracts."""


_DIALECT_ALIASES: Dict[str, str] = {
    "SNOWFLAKE": "SNOWFLAKE",
    "SF": "SNOWFLAKE",
    "BIGQUERY": "BIGQUERY",
    "BQ": "BIGQUERY",
    "POSTGRES": "POSTGRES",
    "POSTGRESQL": "POSTGRES",
    "PG": "POSTGRES",
    "DATABRICKS": "DATABRICKS",
    "DATABRICKS-SQL": "DATABRICKS",
    "DATABRICKS_SQL": "DATABRICKS",
    "ANSI_SQL": "ANSI_SQL",
    "ANSI": "ANSI_SQL",
    "STANDARD_SQL": "ANSI_SQL",
}


# ----------------------------------------------------------------------
# Result types (Pydantic v2)
# ----------------------------------------------------------------------


class MappingResult(BaseModel):
    """Single logical→physical conversion outcome.

    Uses stable, JSON-friendly field names so mapper results round-trip
    through the staged store without custom encoders.
    """

    logical_type: str
    physical_type: str
    target_dialect: str
    supported: bool
    lossy: bool
    note: Optional[str] = None
    rule_id: Optional[str] = None
    warnings: List[str] = Field(default_factory=list)


class ValidationReport(BaseModel):
    """Aggregate stats for a whole-schema mapping run."""

    total_mappings: int = 0
    successful_mappings: int = 0
    lossy_mappings: int = 0
    unsupported_mappings: int = 0
    warnings: List[str] = Field(default_factory=list)
    errors: List[str] = Field(default_factory=list)


# ----------------------------------------------------------------------
# Engine
# ----------------------------------------------------------------------


class DialectMapper:
    """Stateless engine wrapping the inline registry.

    Instances are cheap; hold a reference at module load if you call the
    mapper on a hot path. The class accepts ``dialects_override`` /
    ``logical_types_override`` so a future ``.fluid/dialects/*.yaml``
    feature can layer user-supplied rules on top of the built-ins
    without subclassing.
    """

    def __init__(
        self,
        *,
        dialects_override: Optional[Dict[str, Dict[str, DialectRule]]] = None,
        logical_types_override: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._dialects: Dict[str, Dict[str, DialectRule]] = dict(DIALECTS)
        # Local alias table starts from the module default and grows as
        # the override registers new dialects — means a user can teach
        # this mapper instance about DUCKDB/MSSQL without patching the
        # module-level alias map.
        self._aliases: Dict[str, str] = dict(_DIALECT_ALIASES)
        if dialects_override:
            for name, rules in dialects_override.items():
                key = name.strip().upper()
                canonical = self._aliases.get(key, key)
                # Overlay — keep built-ins that the override didn't touch.
                merged = dict(self._dialects.get(canonical, {}))
                merged.update(rules)
                self._dialects[canonical] = merged
                # Ensure subsequent lookups (even via alias) resolve.
                self._aliases[key] = canonical
                self._aliases[canonical] = canonical
        self._logical_types: Dict[str, Any] = dict(LOGICAL_TYPES)
        if logical_types_override:
            self._logical_types.update(logical_types_override)

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def registry_version(self) -> str:
        """The registry version this mapper instance is loaded against —
        goes into cache keys so a registry bump invalidates cleanly."""
        return REGISTRY_VERSION

    def supported_dialects(self) -> List[str]:
        """Canonical dialect labels this mapper can emit for — sorted
        for stable iteration in tests and logs."""
        return sorted(self._dialects.keys())

    def get_logical_type_info(self, logical_type: str) -> Optional[Dict[str, Any]]:
        """Return the logical-type spec row or ``None`` if unknown."""
        return self._logical_types.get(logical_type.upper())

    # ------------------------------------------------------------------
    # Core mapping
    # ------------------------------------------------------------------

    def map_type(
        self,
        logical_type: str,
        target_dialect: str,
        qualifiers: Optional[Dict[str, Any]] = None,
    ) -> MappingResult:
        """Map one logical type to the physical type for ``target_dialect``.

        Pass-through behaviour: if the dialect is known but the logical
        type isn't in its mapping table, the
        logical string is returned verbatim as ``physical_type`` with
        ``rule_id="pass-through"`` and a warning. Unknown *dialects*
        fail harder — ``supported=False`` plus an explanatory note —
        because silently emitting ANSI for an unknown target risks
        producing invalid DDL on the other end.
        """
        logical = logical_type.upper()
        try:
            canonical_dialect = self._normalize_dialect(target_dialect)
        except KeyError:
            return MappingResult(
                logical_type=logical,
                physical_type="UNSUPPORTED",
                target_dialect=target_dialect,
                supported=False,
                lossy=True,
                note=f"Dialect '{target_dialect}' not registered",
                warnings=[
                    f"Unknown target dialect '{target_dialect}' — "
                    f"add it to DIALECTS or pass dialects_override."
                ],
            )

        dialect_map = self._dialects[canonical_dialect]
        rule = dialect_map.get(logical)
        if rule is None:
            # Unknown logical type in a known dialect — pass-through.
            return MappingResult(
                logical_type=logical,
                physical_type=logical_type,  # preserve the original casing
                target_dialect=canonical_dialect,
                supported=True,
                lossy=False,
                note=f"Direct pass-through mapping for '{logical}'",
                rule_id="pass-through",
                warnings=[
                    f"Logical type '{logical}' has no explicit mapping "
                    f"in {canonical_dialect}; using pass-through."
                ],
            )

        effective_qualifiers = dict(
            (self._logical_types.get(logical, {}) or {}).get("defaults", {})
        )
        if qualifiers:
            effective_qualifiers.update(qualifiers)

        physical = self._substitute_placeholders(rule["physical"], effective_qualifiers)
        return MappingResult(
            logical_type=logical,
            physical_type=physical,
            target_dialect=canonical_dialect,
            supported=rule.get("supported", True),
            lossy=rule.get("lossy", False),
            note=rule.get("note"),
            rule_id=rule.get("rule_id"),
        )

    def map_table_schema(
        self,
        columns: List[Dict[str, Any]],
        target_dialect: str,
    ) -> Tuple[List[MappingResult], ValidationReport]:
        """Map every column and emit a summary :class:`ValidationReport`.

        ``columns`` is a list of dicts with at least ``name`` and
        ``logical_type`` (or ``type``) keys — mirrors the shape
        ``LogicalDraft`` / ``DataModelDraft`` serialize for downstream
        consumers.
        """
        results: List[MappingResult] = []
        warnings: List[str] = []
        errors: List[str] = []
        lossy_count = 0
        unsupported_count = 0

        for col in columns:
            name = col.get("name", "unknown")
            logical = col.get("logical_type") or col.get("type")
            qualifiers = col.get("qualifiers") or {}

            if not logical:
                errors.append(f"Column '{name}': no logical type specified")
                continue

            result = self.map_type(logical, target_dialect, qualifiers)
            results.append(result)

            if result.lossy:
                lossy_count += 1
                warnings.append(
                    f"Column '{name}': lossy conversion "
                    f"{result.logical_type} → {result.physical_type}"
                )
            if not result.supported:
                unsupported_count += 1
                errors.append(
                    f"Column '{name}': unsupported type "
                    f"{result.logical_type} in {result.target_dialect}"
                )
            if result.warnings:
                warnings.extend(f"Column '{name}': {w}" for w in result.warnings)

        report = ValidationReport(
            total_mappings=len(results),
            successful_mappings=sum(1 for r in results if r.supported),
            lossy_mappings=lossy_count,
            unsupported_mappings=unsupported_count,
            warnings=warnings,
            errors=errors,
        )
        return results, report

    # ------------------------------------------------------------------
    # OSI-aware post-processor
    # ------------------------------------------------------------------

    def fill_missing_dialects(
        self,
        logical_type: str,
        existing: Optional[List[Dict[str, str]]] = None,
        *,
        targets: Optional[List[str]] = None,
        qualifiers: Optional[Dict[str, Any]] = None,
        override: bool = False,
    ) -> List[Dict[str, str]]:
        """Extend an OSI ``expression.dialects[]`` list with missing entries.

        Parameters
        ----------
        logical_type:
            The logical type the field represents (e.g. ``"DECIMAL"``).
        existing:
            Dialect entries the LLM already produced. Each entry has
            the OSI shape ``{"dialect": "...", "expression": "..."}``.
        targets:
            Dialects to guarantee in the output; defaults to
            :data:`DEFAULT_DIALECTS`.
        qualifiers:
            Passed through to :meth:`map_type` for placeholder
            substitution (``length``, ``precision``, ``scale``, …).
        override:
            When ``True``, the deterministic rule **replaces** any
            existing entry for the same dialect. Default ``False``
            preserves LLM-authored expressions (the LLM sometimes has
            table-specific context the static table doesn't).

        Returns a new list; the input is not mutated.
        """
        targets = list(targets) if targets is not None else list(DEFAULT_DIALECTS)
        out: List[Dict[str, str]] = [dict(e) for e in (existing or [])]
        existing_by_dialect = {e.get("dialect"): i for i, e in enumerate(out)}

        for raw in targets:
            try:
                canonical = self._normalize_dialect(raw)
            except KeyError:
                # Silently skip unknowns — we never want this helper to
                # blow up a forge run mid-stage. The mapper already
                # carried the warning once via map_type().
                continue

            existing_idx = existing_by_dialect.get(canonical)
            if existing_idx is not None and not override:
                continue

            result = self.map_type(logical_type, canonical, qualifiers)
            entry = {"dialect": canonical, "expression": result.physical_type}
            if existing_idx is not None:
                out[existing_idx] = entry
            else:
                out.append(entry)
                existing_by_dialect[canonical] = len(out) - 1

        return out

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _normalize_dialect(self, name: str) -> str:
        """Map a caller-supplied dialect string to the canonical label.

        Raises ``KeyError`` when the dialect isn't in the alias table;
        the caller's error surface decides what to do with it (``map_type``
        turns it into an ``UNSUPPORTED`` result, ``fill_missing_dialects``
        silently skips). Aliases live on the instance so
        ``dialects_override`` can teach this mapper about new targets
        (``DUCKDB``, ``MSSQL``…) without monkey-patching the module.
        """
        key = name.strip().upper()
        if key in self._aliases:
            return self._aliases[key]
        raise KeyError(name)

    @staticmethod
    def _substitute_placeholders(physical: str, qualifiers: Dict[str, Any]) -> str:
        """Substitute ``{length}``, ``{precision}``, ``{scale}``, etc.

        Missing qualifiers fall back to the registry-wide
        :data:`DEFAULTS` so ``DECIMAL`` → ``NUMBER(18,4)`` for
        Snowflake without the caller having to thread defaults through
        every call site.
        """
        substitutions = {
            "{length}": str(qualifiers.get("length", DEFAULTS["string_length"])),
            "{precision}": str(qualifiers.get("precision", DEFAULTS["decimal_precision"])),
            "{scale}": str(qualifiers.get("scale", DEFAULTS["decimal_scale"])),
            "{element_type}": str(qualifiers.get("element_type", "STRING")),
            "{key_type}": str(qualifiers.get("key_type", "STRING")),
            "{value_type}": str(qualifiers.get("value_type", "STRING")),
        }
        result = physical
        for placeholder, value in substitutions.items():
            if placeholder in result:
                result = result.replace(placeholder, value)
        return result


__all__ = [
    "DEFAULT_DIALECTS",
    "DialectMapper",
    "MappingResult",
    "ValidationReport",
]
