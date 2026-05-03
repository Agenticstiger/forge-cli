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

# fluid_build/providers/odps/serializer.py
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Tuple


def _owner_from_metadata(metadata: Dict[str, Any]) -> Dict[str, Any]:
    owner = metadata.get("owner", {}) if isinstance(metadata, dict) else {}
    out: Dict[str, Any] = {}
    if isinstance(owner, dict):
        team = owner.get("team")
        email = owner.get("email") or owner.get("contact") or owner.get("owner_email")
        if team:
            out["name"] = team
        if email:
            out["email"] = email
    elif isinstance(owner, str):
        out["name"] = owner
    return out


def _interfaces_from_fluid(contract: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    """Extract interfaces from a v0.7.x contract.

    Reads canonical fields only — ``consumes[].productId``,
    ``exposes[].exposeId`` / ``kind`` / ``binding.location``. Pre-0.7
    field names (``ref``, ``id``, ``type``, top-level ``location``)
    are no longer accepted.
    """
    inputs: List[Dict[str, Any]] = []
    for c in contract.get("consumes") or []:
        ref = c.get("productId")
        inputs.append(
            {
                "id": c.get("exposeId") or "input",
                "ref": ref,
                "description": c.get("description") or c.get("purpose"),
                "type": "reference",
                "x-fluid": {
                    k: v
                    for k, v in c.items()
                    if k not in {"exposeId", "productId", "description", "purpose"}
                },
            }
        )

    outputs: List[Dict[str, Any]] = []
    for e in contract.get("exposes") or []:
        expose_id = e.get("exposeId") or "output"
        expose_type = e.get("kind") or "dataset"
        binding = e.get("binding") if isinstance(e.get("binding"), dict) else {}
        location = binding.get("location") if isinstance(binding, dict) else None

        outputs.append(
            {
                "id": expose_id,
                "type": expose_type,
                "description": e.get("description"),
                "location": location,
                "schema": e.get("schema") or e.get("contract", {}).get("schema"),
                "privacy": e.get("privacy"),
                "quality": e.get("quality") or e.get("contract", {}).get("dq"),
                "semantics": e.get("semantics"),
                "x-fluid": {
                    "mappings": e.get("mappings"),
                    "tags": e.get("tags"),
                },
            }
        )

    return {"inputs": inputs, "outputs": outputs}


def _slo_from_fluid(contract: Dict[str, Any]) -> Dict[str, Any]:
    return contract.get("slo") or contract.get("operations", {}).get("sla") or {}


def _policies_from_fluid(contract: Dict[str, Any]) -> Dict[str, Any]:
    return contract.get("accessPolicy") or {}


def _build_from_fluid(contract: Dict[str, Any]) -> Dict[str, Any]:
    """Extract the primary build from a v0.7.x ``builds[]`` array.

    Pre-0.7 contracts that used a single-object ``build`` field are no
    longer supported.
    """
    builds = contract.get("builds") or []
    b = builds[0] if builds else {}
    t = b.get("transformation", {}) if isinstance(b, dict) else {}
    return {
        "pattern": t.get("pattern"),
        "engine": t.get("engine"),
        "properties": t.get("properties"),
        "execution": b.get("execution"),
    }


def fluid_to_odps_document(contract: Dict[str, Any]) -> Dict[str, Any]:
    """Convert a FLUID v0.7.x contract to a minimal ODPS document.

    Emits the ODPS core surface (``info`` / ``product`` / ``interfaces``
    / ``policies`` / ``slo``) and preserves full fidelity under
    ``x-fluid`` for round-tripping.
    """
    fluid_version = contract.get("fluidVersion") or "0.7.3"
    metadata = contract.get("metadata", {}) or {}
    owner = _owner_from_metadata(metadata)
    product_id = contract.get("id", "unknown_product")
    product_name = contract.get("name", product_id)
    description = contract.get("description", "")
    domain = contract.get("domain", "Unknown")
    status = metadata.get("status") or "Development"
    tags = metadata.get("tags") or []

    doc: Dict[str, Any] = {
        # "$schema": "https://opendataproducts.org/spec/odps-1.0.json",  # optional
        "odpsVersion": "1.0.0",  # conservative placeholder
        "info": {
            "title": product_name,
            "version": fluid_version,
            "description": description,
            "owner": owner,
        },
        "product": {
            "id": product_id,
            "domain": domain,
            "status": status,
            "tags": tags,
        },
        "interfaces": _interfaces_from_fluid(contract),
        "policies": _policies_from_fluid(contract),
        "slo": _slo_from_fluid(contract),
        "x-fluid": {
            "fluidVersion": contract.get("fluidVersion"),
            "kind": contract.get("kind"),
            "metadata": metadata,
            "build": _build_from_fluid(contract),
            "governance": contract.get("governance"),
            "operations": contract.get("operations"),
            "security": contract.get("security"),
            "lineage": contract.get("lineage"),
            "detailed_specifications": contract.get("detailed_specifications"),
        },
    }
    return doc


# ---------- Signatures for plan diffs & contract tests ----------


def _schema_signature(schema: List[Dict[str, Any]]) -> Tuple[Tuple[str, str, bool], ...]:
    cols: List[Tuple[str, str, bool]] = []
    for col in schema or []:
        name = str(col.get("name"))
        typ = str(col.get("type"))
        nullable = bool(col.get("nullable", True))
        cols.append((name, typ, nullable))
    cols.sort(key=lambda x: x[0])  # stable order
    return tuple(cols)


def json_dumps_min(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def hashlib_sha256(obj: Any) -> str:
    payload = json_dumps_min(obj).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def build_interface_signatures(odps_doc: Dict[str, Any]) -> Dict[str, Any]:
    out = {}
    outputs = (odps_doc.get("interfaces") or {}).get("outputs") or []
    for o in outputs:
        rid = o.get("id", "output")
        sig = _schema_signature(o.get("schema") or [])
        out[rid] = {
            "schema": sig,
            "schemaHash": hashlib_sha256(sig),
        }
    return out
