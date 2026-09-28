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

"""Schema-signature baselines behind ``fluid contract-tests``.

A signature is, per expose, its id and the ordered ``(name, type, nullable)``
of each column. A baseline is that signature saved as JSON
(``{"signature": "<repr of the tuple>"}``, the format
``examples/customer360/baseline.schema.json`` uses). A contract passes when
its signature equals the baseline's: any removed, added, retyped, reordered
or re-nulled column fails, and each difference is named.
"""

import ast
import json
from typing import Any, Dict, List, Mapping, Optional, Tuple

Column = Tuple[Optional[str], Optional[str], bool]
Signature = Tuple[Tuple[Optional[str], Tuple[Column, ...]], ...]


def _expose_columns(expose: Mapping[str, Any]) -> List[Any]:
    """Columns of *expose*: ``contract.schema`` (0.7.x), else the older
    top-level ``schema``, either one a list or a ``{"fields": [...]}`` wrapper.

    The lookup ``cli._verify_reconcile._expose_schema_columns`` makes, kept
    here rather than imported because importing any ``fluid_build.cli``
    module loads the whole CLI package. Entries are not filtered, so a
    baseline written before 0.7 keeps its signature.
    """
    section = expose.get("contract")
    columns = section.get("schema") if isinstance(section, Mapping) else None
    if columns is None:
        columns = expose.get("schema")
    if isinstance(columns, Mapping):
        columns = columns.get("fields")
    return columns if isinstance(columns, list) else []


def _nullable(column: Mapping[str, Any]) -> bool:
    """0.7.x marks a non-null column ``required: true``; older contracts
    wrote ``nullable: false``. Either one makes the column non-nullable."""
    if column.get("required") is True:
        return False
    return bool(column.get("nullable", True))


def schema_signature(contract: Mapping[str, Any]) -> Signature:
    out = []
    for exp in contract.get("exposes") or []:
        cols = tuple((c.get("name"), c.get("type"), _nullable(c)) for c in _expose_columns(exp))
        out.append((exp.get("exposeId") or exp.get("id"), cols))
    return tuple(out)


def _as_tuples(value: Any) -> Any:
    if isinstance(value, (list, tuple)):
        return tuple(_as_tuples(v) for v in value)
    return value


def load_baseline(baseline_path: str) -> Signature:
    """The signature saved in *baseline_path*.

    Raises ``ValueError`` (``json.JSONDecodeError`` is one) when the file is
    not a baseline, so a wrong file is reported rather than compared.
    """
    with open(baseline_path, encoding="utf-8") as f:
        baseline = json.load(f)
    raw = baseline.get("signature") if isinstance(baseline, dict) else None
    if not isinstance(raw, str):
        raise ValueError(f"{baseline_path} has no 'signature' string")
    try:
        parsed = ast.literal_eval(raw)
    except (ValueError, SyntaxError) as e:
        raise ValueError(f"{baseline_path}: 'signature' is not a schema signature ({e})") from e
    signature = _as_tuples(parsed)
    if not isinstance(signature, tuple):
        raise ValueError(f"{baseline_path}: 'signature' is not a schema signature")
    return signature  # type: ignore[no-any-return]


def _describe(nullable: Any) -> str:
    return "nullable" if nullable else "required"


def _column_reasons(expose_id: Any, old: Tuple[Any, ...], new: Tuple[Any, ...]) -> List[str]:
    old_by_name = {c[0]: c for c in old}
    new_by_name = {c[0]: c for c in new}
    reasons = []
    for name, col in old_by_name.items():
        if name not in new_by_name:
            reasons.append(f"{expose_id}.{name}: column removed")
            continue
        now = new_by_name[name]
        if col[1] != now[1]:
            reasons.append(f"{expose_id}.{name}: type changed {col[1]} -> {now[1]}")
        if col[2] != now[2]:
            reasons.append(f"{expose_id}.{name}: {_describe(col[2])} -> {_describe(now[2])}")
    for name in new_by_name:
        if name not in old_by_name:
            reasons.append(f"{expose_id}.{name}: column added")
    if not reasons and old != new:
        reasons.append(f"{expose_id}: column order changed")
    return reasons


def compare_signatures(baseline: Signature, current: Signature) -> List[str]:
    """Every difference between two signatures, empty when they are equal."""
    if baseline == current:
        return []
    old = {eid: cols for eid, cols in baseline}
    new = {eid: cols for eid, cols in current}
    reasons = []
    for eid, cols in old.items():
        if eid not in new:
            reasons.append(f"expose {eid!r} is in the baseline but not in the contract")
        else:
            reasons.extend(_column_reasons(eid, cols, new[eid]))
    for eid in new:
        if eid not in old:
            reasons.append(f"expose {eid!r} is not in the baseline")
    return reasons or ["schema signature differs from the baseline"]


def run_tests(contract: Mapping[str, Any], baseline_path: str) -> Dict[str, Any]:
    """Compare *contract* with the baseline at *baseline_path*.

    Returns ``{"compatible": bool, "reasons": [str, ...]}``.
    """
    reasons = compare_signatures(load_baseline(baseline_path), schema_signature(contract))
    return {"compatible": not reasons, "reasons": reasons}


def check_compat(new_contract: dict, baseline_path: str) -> dict:
    with open(baseline_path, encoding="utf-8") as f:
        baseline = json.load(f)
    new_sig = schema_signature(new_contract)
    if str(new_sig) != baseline.get("signature"):
        return {"compatible": False, "reason": "schema_signature_differs", "new": str(new_sig)}
    return {"compatible": True}


def write_baseline(contract: Mapping[str, Any], out: str) -> str:
    obj = {"signature": str(schema_signature(contract))}
    with open(out, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2)
        f.write("\n")
    return out
