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

"""The authoring variant of a FLUID schema: the canonical one, with a ``$ref`` allowed anywhere.

An editor validates the files people write, and a contract written in
fragments holds ``{$ref: ...}`` where the canonical schema wants a build, an
expose, a column or SQL text. The canonical schema describes the composed
contract: what every command validates, and what Command Center generates its
models from. So it stays as it is, and this variant is derived from it by
wrapping every value position the way the OpenAPI 3.1 meta-schema admits a
Reference Object::

    {"if": {"type": "object", "required": ["$ref"]},
     "then": {"$ref": "#/$defs/fragmentRef"},
     "else": <the canonical subschema>}

A value that is not a ``$ref`` gets exactly the canonical checks, and their
messages. Conditions (``if``, ``not``) are left as they are: they test the
contract, not a place a fragment stands.

Generated, never edited by hand: ``python -m fluid_build.schema_authoring``
rewrites each bundled authoring schema from its canonical one, and a test
fails when one drifts.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any, Dict

__all__ = ["AUTHORING_VERSIONS", "authoring_schema", "authoring_schema_path"]

#: Schema versions that ship an authoring variant.
AUTHORING_VERSIONS = ("0.7.6",)

_SCHEMAS = Path(__file__).parent / "schemas"

_FRAGMENT_REF: Dict[str, Any] = {
    "type": "object",
    "required": ["$ref"],
    "properties": {
        "$ref": {
            "type": "string",
            "minLength": 1,
            "description": (
                "A file relative to this one (./part.yaml, ./sql/transform.sql), "
                "optionally with a JSON pointer (./part.yaml#/rules), or a "
                "same-document pointer (#/builds/0/properties)."
            ),
        }
    },
    "description": (
        "A part of the contract kept elsewhere. Keys beside $ref are merged over "
        "what it names (RFC 7386). fluid validate checks the composed contract."
    ),
}

_IS_REF = {"type": "object", "required": ["$ref"]}


def authoring_schema_path(version: str) -> Path:
    """Where the authoring schema for ``version`` is bundled."""
    return _SCHEMAS / f"fluid-authoring-schema-{version}.json"


def authoring_schema(schema: Dict[str, Any]) -> Dict[str, Any]:
    """``schema`` with every value position also accepting a fragment ``$ref``."""
    out = _walk(copy.deepcopy(schema))
    if isinstance(out.get("$id"), str):
        out["$id"] = out["$id"].replace("fluid-schema-", "fluid-authoring-schema-")
    if isinstance(out.get("title"), str):
        out["title"] = f"{out['title']} (authoring: fragments allowed)"
    out["description"] = (
        "Generated from the canonical schema of the same version for editors: any "
        "value may also be a {$ref: ...} to a fragment. Commands validate the "
        "composed contract against the canonical schema. " + str(schema.get("description") or "")
    ).strip()
    defs = out.setdefault("$defs", {})
    defs["fragmentRef"] = copy.deepcopy(_FRAGMENT_REF)
    return out


def _wrap(subschema: Any) -> Any:
    if not isinstance(subschema, dict):
        return subschema  # a boolean schema: nothing to describe
    return {
        "if": copy.deepcopy(_IS_REF),
        "then": {"$ref": "#/$defs/fragmentRef"},
        "else": _walk(subschema),
    }


def _walk(node: Any) -> Any:
    """``node`` with the value positions under it wrapped (not ``node`` itself)."""
    if not isinstance(node, dict):
        return node
    for key in ("properties", "patternProperties"):
        if isinstance(node.get(key), dict):
            node[key] = {name: _wrap(sub) for name, sub in node[key].items()}
    for key in ("items", "additionalProperties", "unevaluatedProperties"):
        if isinstance(node.get(key), dict):
            node[key] = _wrap(node[key])
    if isinstance(node.get("prefixItems"), list):
        node["prefixItems"] = [_wrap(sub) for sub in node["prefixItems"]]
    for key in ("allOf", "anyOf", "oneOf"):
        if isinstance(node.get(key), list):
            node[key] = [_walk(sub) for sub in node[key]]
    for key in ("then", "else"):
        if isinstance(node.get(key), dict):
            node[key] = _walk(node[key])
    if isinstance(node.get("$defs"), dict):
        node["$defs"] = {name: _walk(sub) for name, sub in node["$defs"].items()}
    return node


def render(version: str) -> str:
    """The authoring schema for ``version``, as the bundled file holds it."""
    canonical = json.loads((_SCHEMAS / f"fluid-schema-{version}.json").read_text("utf-8"))
    return json.dumps(authoring_schema(canonical), indent=2, ensure_ascii=False) + "\n"


def main(argv: Any = None) -> int:
    """Rewrite every bundled authoring schema from its canonical one."""
    for version in AUTHORING_VERSIONS:
        path = authoring_schema_path(version)
        path.write_text(render(version), encoding="utf-8")
        sys.stdout.write(f"wrote {path}\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
