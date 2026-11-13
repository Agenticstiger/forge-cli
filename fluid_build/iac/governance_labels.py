# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A contract's governance as resource labels, the same on every cloud.

The contract's and the expose's ``labels``, and its classification, jurisdiction,
regulatory framework and residency, written onto the resources a product owns: GCP
labels on the dataset, table and key, AWS tags on the bucket and key and parameters on
the Glue table, Unity Catalog tags on the Databricks schema and table. So the
regulation a product falls under is on the resource and can be found there, not only
in the contract. The keys are coerced to GCP's label alphabet (the strictest), so every
cloud carries the same keys, and so are the values unless a caller asks for them
``verbatim_values``: a Unity Catalog tag value keeps case, spaces and punctuation, so
the Databricks module writes the contract's words, not a slug of them.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Mapping, Optional

_LABEL_KEY_RE = re.compile(r"[^a-z0-9_-]")


def _label(value: Any) -> str:
    """A string as a GCP label value: lowercase letters, digits, ``_`` and ``-``, 63 at most."""
    return _LABEL_KEY_RE.sub("-", str(value).strip().lower())[:63]


def _verbatim(value: Any) -> str:
    """A string as written, less the surrounding whitespace a Unity Catalog tag refuses.

    The schema allows only strings; a YAML ``true`` or ``null`` that reaches an emitter
    unvalidated (``fluid generate iac`` has no schema gate) is spelled as YAML spells it,
    and ``null`` makes no tag, rather than Python's ``True`` and ``None``.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value).strip()


def governance_labels(
    contract: Mapping[str, Any],
    exposure: Mapping[str, Any],
    *,
    limit: Optional[int] = 60,
    verbatim_values: bool = False,
) -> Dict[str, str]:
    """The contract's governance as GCP labels: its labels, classification and regulation.

    The contract's and the expose's ``labels`` (each key and value coerced to GCP's
    label alphabet), plus ``fluid_classification``, ``fluid_jurisdiction``,
    ``fluid_regulation`` and ``fluid_residency`` from the expose's classification and
    the contract's ``sovereignty``. A key that does not start with a letter is
    prefixed ``l_``; empty values are dropped; at most ``limit`` (60, GCP's cap) are
    kept, in key order. ``limit=None`` keeps them all, for a caller that must refuse
    a count its target cannot hold rather than drop labels silently.

    ``verbatim_values`` keeps each value as the contract writes it, less surrounding
    whitespace, and joins the regulatory frameworks with ``", "``: for a target whose
    tag values take any text (Unity Catalog), which refuses a value too long for it
    rather than cut it. The keys are coerced either way.
    """
    value_of, separator = (_verbatim, ", ") if verbatim_values else (_label, "_")
    out: Dict[str, str] = {}
    for source in (contract.get("labels"), exposure.get("labels")):
        if isinstance(source, Mapping):
            for key, value in source.items():
                k = _label(key)
                if k and not k[0].isalpha():
                    k = f"l_{k}"[:63]
                v = value_of(value)
                if k and v:
                    out[k] = v
    policy = exposure.get("policy") if isinstance(exposure.get("policy"), Mapping) else {}
    classification = policy.get("classification") or (contract.get("metadata") or {}).get(
        "classification"
    )
    if classification:
        out["fluid_classification"] = value_of(classification)
    sovereignty = (
        contract.get("sovereignty") if isinstance(contract.get("sovereignty"), Mapping) else {}
    )
    if sovereignty.get("jurisdiction"):
        out["fluid_jurisdiction"] = value_of(sovereignty["jurisdiction"])
    frameworks = sovereignty.get("regulatoryFramework") or sovereignty.get("regulations")
    if isinstance(frameworks, str):
        frameworks = [frameworks]
    if isinstance(frameworks, list) and frameworks:
        out["fluid_regulation"] = value_of(separator.join(str(f) for f in frameworks))
    if sovereignty.get("dataResidency") is not None:
        out["fluid_residency"] = "true" if sovereignty.get("dataResidency") else "false"
    keys = sorted(out)
    return {k: out[k] for k in (keys if limit is None else keys[:limit])}


__all__ = ["governance_labels"]
