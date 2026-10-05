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

"""A contract's governance as resource labels, the same on every cloud.

The contract's and the expose's ``labels``, and its classification, jurisdiction,
regulatory framework and residency, written onto the resources a product owns: GCP
labels on the dataset, table and key, AWS tags on the bucket and key and parameters on
the Glue table. So the regulation a product falls under is on the resource and can be
found there, not only in the contract. The values are coerced to GCP's label alphabet
(the stricter of the two), so both clouds carry the same keys and values.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Mapping

_LABEL_KEY_RE = re.compile(r"[^a-z0-9_-]")


def _label(value: Any) -> str:
    """A string as a GCP label value: lowercase letters, digits, ``_`` and ``-``, 63 at most."""
    return _LABEL_KEY_RE.sub("-", str(value).strip().lower())[:63]


def governance_labels(contract: Mapping[str, Any], exposure: Mapping[str, Any]) -> Dict[str, str]:
    """The contract's governance as GCP labels: its labels, classification and regulation.

    The contract's and the expose's ``labels`` (each key and value coerced to GCP's
    label alphabet), plus ``fluid_classification``, ``fluid_jurisdiction``,
    ``fluid_regulation`` and ``fluid_residency`` from the expose's classification and
    the contract's ``sovereignty``. A key that does not start with a letter is
    prefixed ``l_``; empty values are dropped; at most 60 are kept, in key order.
    """
    out: Dict[str, str] = {}
    for source in (contract.get("labels"), exposure.get("labels")):
        if isinstance(source, Mapping):
            for key, value in source.items():
                k = _label(key)
                if k and not k[0].isalpha():
                    k = f"l_{k}"[:63]
                v = _label(value)
                if k and v:
                    out[k] = v
    policy = exposure.get("policy") if isinstance(exposure.get("policy"), Mapping) else {}
    classification = policy.get("classification") or (contract.get("metadata") or {}).get(
        "classification"
    )
    if classification:
        out["fluid_classification"] = _label(classification)
    sovereignty = (
        contract.get("sovereignty") if isinstance(contract.get("sovereignty"), Mapping) else {}
    )
    if sovereignty.get("jurisdiction"):
        out["fluid_jurisdiction"] = _label(sovereignty["jurisdiction"])
    frameworks = sovereignty.get("regulatoryFramework") or sovereignty.get("regulations")
    if isinstance(frameworks, str):
        frameworks = [frameworks]
    if isinstance(frameworks, list) and frameworks:
        out["fluid_regulation"] = _label("_".join(str(f) for f in frameworks))
    if sovereignty.get("dataResidency") is not None:
        out["fluid_residency"] = "true" if sovereignty.get("dataResidency") else "false"
    return {k: out[k] for k in sorted(out)[:60]}


__all__ = ["governance_labels"]
