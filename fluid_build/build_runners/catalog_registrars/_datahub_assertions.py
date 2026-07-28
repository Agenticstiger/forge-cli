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

"""ODCS quality-rule → DataHub Assertion translator.

DataHub's ``DataContract`` entity carries lists of assertion URNs (in
``dataContractProperties.schema``, ``.freshness``, ``.dataQuality``).
The contract page renders each linked Assertion as a check the
operator can click into. Until we wire actual ODCS quality rules
through this translator, those lists are empty arrays — the
DataContract entity exists but it has no enforceable expectations.

This module bridges that gap: per asset, it walks the per-field
``quality`` arrays on the rendered ODCS contract and emits one
``Assertion`` entity per rule the translator understands. Each
emission is:

* a stable URN ``urn:li:assertion:<product>.<expose>.<rule-hash>``
  (deterministic so re-publishes upsert, not duplicate),
* an ``assertionInfo`` MCP body matching the
  ``com.linkedin.assertion.AssertionInfo`` shape, and
* a bucket label (``schema`` / ``freshness`` / ``dataQuality``) so
  the caller can route URNs into the right DataContract slot.

ODCS v3.1.0 rules we translate today:

* ``required: true`` on a field → ``FIELD`` assertion (NOT_NULL).
* ``unique: true`` on a field → ``FIELD`` assertion (UNIQUE_COUNT
  equals row count, modelled via ``FIELD_VALUES`` metric).
* per-field ``quality[]`` entries with ``library`` rules ``notNull``
  / ``unique`` (same translation as the bool flags above) — handled
  so contracts can author either style.

We intentionally **do not** translate raw SQL rules in this pass:
DataHub's ``SQL`` assertion takes a complete SELECT + operator +
parameters; mapping the open ODCS shape is a follow-up. The bucket
goes to ``dataQuality`` regardless of rule type, matching what the
DataHub UI expects.

Why hash-based ids: if the contract changes (e.g. a quality rule is
removed), the OLD assertion URN no longer maps to any rule and gets
orphaned. Re-running the publish converges the state but leaves
stale entities. That's acceptable for v1; a sweep step that deletes
old assertions belongs in a `unregister`-style flow.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class AssertionEmission:
    """One assertion to upsert. Decouples the translator (this module,
    pure-data) from the HTTP layer (registrar) so testing the
    translation doesn't require respx."""

    urn: str
    bucket: str  # one of "schema" | "freshness" | "dataQuality"
    info: Dict[str, Any]  # the assertionInfo aspect body


def odcs_to_assertions(
    odcs: Dict[str, Any],
    product_id: str,
    expose_id: str,
    dataset_urn: str,
) -> List[AssertionEmission]:
    """Walk *odcs* and produce one ``AssertionEmission`` per
    translatable rule.

    ``odcs`` is the per-asset ODCS contract as a dict (the same body
    we PUT to ``/api/datacontracts/{product}.{expose}`` on DMM). The
    translator pulls rules from the schema's property-level
    ``required`` / ``unique`` flags and per-property ``quality[]``.

    Returns an empty list when the ODCS body has no understood rules.
    Never raises on malformed ODCS — best-effort, log+skip semantics
    mirror the registrar's overall non-fatal posture.
    """
    if not isinstance(odcs, dict):
        return []
    schema = odcs.get("schema") or []
    if not isinstance(schema, list):
        return []

    emissions: List[AssertionEmission] = []
    for schema_obj in schema:
        if not isinstance(schema_obj, dict):
            continue
        properties = schema_obj.get("properties") or []
        if not isinstance(properties, list):
            continue
        for prop in properties:
            if not isinstance(prop, dict):
                continue
            field_name = prop.get("name")
            if not field_name:
                continue
            emissions.extend(
                _emissions_for_field(
                    field_name=str(field_name),
                    prop=prop,
                    product_id=product_id,
                    expose_id=expose_id,
                    dataset_urn=dataset_urn,
                )
            )
    return emissions


def _emissions_for_field(
    *,
    field_name: str,
    prop: Dict[str, Any],
    product_id: str,
    expose_id: str,
    dataset_urn: str,
) -> List[AssertionEmission]:
    """Translate a single ODCS property into 0+ assertion emissions."""
    out: List[AssertionEmission] = []

    # 1. ``required: true`` → NOT_NULL field assertion.
    if prop.get("required") is True:
        out.append(
            _field_assertion(
                kind="not_null",
                field_name=field_name,
                product_id=product_id,
                expose_id=expose_id,
                dataset_urn=dataset_urn,
                description=(f"{field_name} must not contain NULL values " f"(ODCS required:true)"),
            )
        )

    # 2. ``unique: true`` → UNIQUE field assertion.
    if prop.get("unique") is True:
        out.append(
            _field_assertion(
                kind="unique",
                field_name=field_name,
                product_id=product_id,
                expose_id=expose_id,
                dataset_urn=dataset_urn,
                description=(
                    f"{field_name} values must be unique across the " f"dataset (ODCS unique:true)"
                ),
            )
        )

    # 3. Per-field ``quality[]`` library rules. ODCS supports multiple
    # authoring styles — we accept all of them for the same library
    # check so contracts hand-authored against the Bitol docs and
    # contracts auto-expanded by ``OdcsProvider.render`` both
    # translate cleanly:
    #
    #   a) ``{type: library, rule: notNull}`` — pure Bitol shape
    #   b) ``{type: library, metric: nullValues, mustBe: 0}`` —
    #       what ``OdcsProvider`` emits when expanding ``required: true``
    #   c) ``{type: library, rule: unique}`` — pure Bitol shape
    #   d) ``{type: library, metric: duplicateValues, mustBe: 0}`` —
    #       what some ODCS renderers emit for ``unique: true``
    #
    # Duplicate emissions are deduplicated downstream (same
    # ``(field, kind)`` tuple → same hashed URN → DataHub upserts).
    for rule in prop.get("quality") or []:
        if not isinstance(rule, dict):
            continue
        if str(rule.get("type") or "").lower() != "library":
            # SQL / text / custom rules — out of scope this pass.
            continue
        rule_desc = rule.get("description") or ""
        library_rule = str(rule.get("rule") or "").lower()
        library_metric = str(rule.get("metric") or "").lower()
        must_be = rule.get("mustBe")
        # NOT NULL — match by rule name OR by metric:nullValues mustBe:0.
        is_not_null = library_rule in ("notnull", "not_null") or (
            library_metric in ("nullvalues", "null_values") and _is_zero(must_be)
        )
        # UNIQUE — match by rule name OR by metric:duplicateValues mustBe:0.
        is_unique = library_rule == "unique" or (
            library_metric in ("duplicatevalues", "duplicate_values") and _is_zero(must_be)
        )
        if is_not_null:
            out.append(
                _field_assertion(
                    kind="not_null",
                    field_name=field_name,
                    product_id=product_id,
                    expose_id=expose_id,
                    dataset_urn=dataset_urn,
                    description=str(rule_desc) or f"{field_name} must not be null",
                )
            )
        elif is_unique:
            out.append(
                _field_assertion(
                    kind="unique",
                    field_name=field_name,
                    product_id=product_id,
                    expose_id=expose_id,
                    dataset_urn=dataset_urn,
                    description=str(rule_desc) or f"{field_name} values must be unique",
                )
            )

    # Dedupe by URN — ``required: true`` and the ODCS-provider-expanded
    # ``metric: nullValues / mustBe: 0`` will produce the SAME
    # assertion URN (hash includes (product, expose, field, kind))
    # so a second emission collides on insertion. Belt-and-suspenders:
    # keep the first occurrence, drop any later duplicate.
    seen: set[str] = set()
    deduped: List[AssertionEmission] = []
    for em in out:
        if em.urn in seen:
            continue
        seen.add(em.urn)
        deduped.append(em)
    return deduped


def _is_zero(value: Any) -> bool:
    """Best-effort check for the ODCS ``mustBe`` field being literal 0
    (int / float / string). Avoids matching empty strings or None as
    accidental zero."""
    if value is None:
        return False
    try:
        return float(value) == 0.0
    except (TypeError, ValueError):
        return False


def _field_assertion(
    *,
    kind: str,
    field_name: str,
    product_id: str,
    expose_id: str,
    dataset_urn: str,
    description: str,
) -> AssertionEmission:
    """Build one FIELD-type AssertionEmission.

    URN derivation: hash the (product, expose, field, kind) tuple so
    re-publishes always upsert the same assertion. Truncated to 16
    hex chars for readability — collision space is ample for
    per-contract scale.

    Body shape: ``assertionInfo.type = FIELD`` with a
    ``fieldAssertion`` sub-record. The sub-record's ``type`` is
    ``FIELD_VALUES`` for both not-null and unique (both are
    value-distribution properties); the operator + parameters
    differ between the two kinds.
    """
    digest = _stable_hash(f"{product_id}.{expose_id}.{field_name}.{kind}")
    urn = f"urn:li:assertion:{product_id}.{expose_id}.{kind}.{field_name}.{digest}"

    field_path = field_name
    if kind == "not_null":
        field_sub = {
            "type": "FIELD_VALUES",
            "field": {"path": field_path, "type": "STRING", "nativeType": "VARCHAR"},
            "fieldValuesAssertion": {
                "operator": "NOT_NULL",
                "parameters": {},
                "failThreshold": {"type": "COUNT", "value": 0},
                "excludeNulls": False,
            },
        }
    elif kind == "unique":
        # DataHub's FIELD_METRIC + UNIQUE_PERCENTAGE EQUAL_TO 100 is
        # the canonical way to express "every value is unique".
        field_sub = {
            "type": "FIELD_METRIC",
            "field": {"path": field_path, "type": "STRING", "nativeType": "VARCHAR"},
            "fieldMetricAssertion": {
                "metric": "UNIQUE_PERCENTAGE",
                "operator": "EQUAL_TO",
                "parameters": {
                    "value": {"type": "NUMBER", "value": "100"},
                },
            },
        }
    else:  # pragma: no cover — defensive
        raise ValueError(f"unknown FIELD assertion kind: {kind!r}")

    info: Dict[str, Any] = {
        "type": "FIELD",
        "description": description,
        "lastUpdated": {
            "time": 0,  # caller fills in audit stamp if needed
            "actor": "urn:li:corpuser:fluid",
        },
        "fieldAssertion": {"entity": dataset_urn, **field_sub},
    }
    return AssertionEmission(urn=urn, bucket="dataQuality", info=info)


def _stable_hash(text: str) -> str:
    """16-char hex digest. Deterministic, collision-resistant enough
    for per-contract scale; intentionally short so URNs stay readable."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
