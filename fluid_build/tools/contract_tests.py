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

import json


def schema_signature(contract: dict):
    out = []
    for exp in contract.get("exposes", []):
        cols = [
            (c.get("name"), c.get("type"), bool(c.get("nullable", True)))
            for c in exp.get("schema", [])
        ]
        out.append((exp.get("id"), tuple(cols)))
    return tuple(out)


def check_compat(new_contract: dict, baseline_path: str) -> dict:
    with open(baseline_path, encoding="utf-8") as f:
        baseline = json.load(f)
    new_sig = schema_signature(new_contract)
    if str(new_sig) != baseline.get("signature"):
        return {"compatible": False, "reason": "schema_signature_differs", "new": str(new_sig)}
    return {"compatible": True}


def write_baseline(contract: dict, out: str):
    obj = {"signature": str(schema_signature(contract))}
    with open(out, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2)
    return out
