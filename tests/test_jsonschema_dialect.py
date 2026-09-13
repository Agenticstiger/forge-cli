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

"""Validate each schema with the dialect it declares, not a hardcoded one.

Draft 7 does not reject keywords it does not recognise — it IGNORES them. So
validating a 2019-09 or 2020-12 schema with `Draft7Validator` does not fail
loudly; it silently drops every constraint expressed in a newer keyword and
passes the document. There is no error, no warning, and no way to tell from the
outside that a rule stopped being enforced.

That was live on the ODCS path. The vendored `odcs-schema-v3.1.0.json` declares
2019-09 and guards nine objects with `unevaluatedProperties: false`; under
Draft 7 a server entry carrying an unexpected key validated with ZERO errors,
reachable from `fluid validate-artifacts`.

The FLUID schemas were the same bug not yet triggered: they declare 2020-12 but
have so far only used `$defs`, which Draft 7 happens to resolve as an ordinary
JSON pointer. The first 2020-12-only keyword added to a FLUID schema would have
been ignored, and `fluid validate` would have accepted documents the published
standard rejects.

So the test that matters here is not "does a known-bad document fail" — it is
the invariant: the validator's dialect matches what each schema asks for.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterator, List, Tuple

import jsonschema
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DIRS = (
    REPO_ROOT / "fluid_build" / "schemas",
    REPO_ROOT / "fluid_build" / "providers",
)

#: Keywords that exist only after Draft 7. Draft 7 ignores each one silently.
POST_DRAFT7_KEYWORDS = (
    "unevaluatedProperties",
    "unevaluatedItems",
    "prefixItems",
    "dependentRequired",
    "dependentSchemas",
    "minContains",
    "maxContains",
    "$dynamicRef",
    "$dynamicAnchor",
    "$recursiveRef",
)


def _shipped_schemas() -> Iterator[Tuple[str, Dict[str, Any]]]:
    for directory in SCHEMA_DIRS:
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*.json")):
            try:
                doc = json.loads(path.read_text())
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if isinstance(doc, dict) and isinstance(doc.get("$schema"), str):
                yield str(path.relative_to(REPO_ROOT)), doc


SHIPPED = list(_shipped_schemas())


def _keywords_in(schema: Any) -> List[str]:
    found: List[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in POST_DRAFT7_KEYWORDS:
                    found.append(key)
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(schema)
    return found


def test_there_are_schemas_to_check() -> None:
    """Guard the guard: a glob that finds nothing would pass every test below."""
    assert len(SHIPPED) >= 5, f"only found {len(SHIPPED)} shipped schemas — the glob is wrong"


@pytest.mark.parametrize("name,schema", SHIPPED, ids=[n for n, _ in SHIPPED])
def test_validator_dialect_matches_the_declared_schema(name: str, schema: Dict[str, Any]) -> None:
    """`validator_for` must resolve each schema to the dialect it declares."""
    chosen = jsonschema.validators.validator_for(schema)
    assert chosen.META_SCHEMA["$schema"].rstrip("#") == schema["$schema"].rstrip(
        "#"
    ), f"{name} declares {schema['$schema']} but resolves to {chosen.__name__}"


@pytest.mark.parametrize("name,schema", SHIPPED, ids=[n for n, _ in SHIPPED])
def test_no_shipped_schema_is_silently_downgraded_by_draft7(
    name: str, schema: Dict[str, Any]
) -> None:
    """The regression itself, stated as the property rather than one example.

    If a schema uses a post-Draft-7 keyword, Draft 7 cannot enforce it. This
    asserts the pairing that used to be broken: either the schema stays inside
    Draft 7, or the code no longer validates it with Draft 7. Since the fix
    makes the second half true for every schema, this passes — and it is the
    test that goes red if anyone pins a validator again.
    """
    used = _keywords_in(schema)
    if not used:
        pytest.skip(f"{name} uses no post-Draft-7 keyword")
    chosen = jsonschema.validators.validator_for(schema)
    assert chosen is not jsonschema.Draft7Validator, (
        f"{name} uses {sorted(set(used))} but resolves to Draft7Validator, which "
        "ignores them — every constraint they express would be silently dropped"
    )


# ---------------------------------------------------------------------------
# The concrete bug, pinned. This is the case that was accepted in production.
# ---------------------------------------------------------------------------

_ODCS_SCHEMA_PATH = REPO_ROOT / "fluid_build" / "providers" / "odcs" / "odcs-schema-v3.1.0.json"

_ODCS_BASE: Dict[str, Any] = {
    "apiVersion": "v3.1.0",
    "kind": "DataContract",
    "id": "x",
    "status": "active",
    "version": "1.0.0",
}


@pytest.mark.skipif(not _ODCS_SCHEMA_PATH.exists(), reason="vendored ODCS schema not present")
def test_an_unexpected_key_in_an_odcs_server_is_rejected() -> None:
    """Under Draft 7 this document validated with ZERO errors.

    `servers[]` is guarded by `unevaluatedProperties: false` and, unlike the
    document root, carries no `additionalProperties` — so it is the one place
    where the dialect is the only thing standing between a typo and acceptance.
    """
    schema = json.loads(_ODCS_SCHEMA_PATH.read_text())
    doc = {
        **_ODCS_BASE,
        "servers": [
            {
                "server": "prod",
                "type": "kafka",
                "host": "h",
                "format": "json",
                "TOTALLY_BOGUS_KEY": "junk",
            }
        ],
    }

    validator = jsonschema.validators.validator_for(schema)(schema)
    errors = list(validator.iter_errors(doc))
    assert errors, "an unexpected key in servers[] was accepted"
    assert any(e.validator == "unevaluatedProperties" for e in errors)

    # The control that makes the assertion above mean something: the SAME
    # document under the old hardcoded validator. If this ever starts finding
    # errors, the test above stopped proving anything about the dialect.
    assert not list(
        jsonschema.Draft7Validator(schema).iter_errors(doc)
    ), "Draft 7 now rejects this too, so it no longer demonstrates the downgrade"


@pytest.mark.skipif(not _ODCS_SCHEMA_PATH.exists(), reason="vendored ODCS schema not present")
def test_a_clean_odcs_document_still_passes() -> None:
    """The negative control. Without it, 'rejects the bad one' is satisfied by rejecting all."""
    schema = json.loads(_ODCS_SCHEMA_PATH.read_text())
    doc = {
        **_ODCS_BASE,
        "servers": [{"server": "prod", "type": "kafka", "host": "h", "format": "json"}],
    }
    validator = jsonschema.validators.validator_for(schema)(schema)
    assert not list(validator.iter_errors(doc))


def test_the_odcs_schema_still_declares_2019_09() -> None:
    """If the vendored schema is ever re-vendored at a different dialect, notice here."""
    schema = json.loads(_ODCS_SCHEMA_PATH.read_text())
    assert schema["$schema"] == "https://json-schema.org/draft/2019-09/schema"
    assert len(_keywords_in(schema)) >= 9


# ---------------------------------------------------------------------------
# Through the SHIPPED code path. Everything above this line passes against the
# unfixed code too — those tests assert things about `validator_for` itself,
# which was never the broken part. The bug lived in which validator
# `_validate_against_schema` chose, so only a test that calls the real entry
# point can see it. Checked: reverting artifact_validators.py to the old
# version turns the two tests below red and leaves every test above green.
# ---------------------------------------------------------------------------

_ODCS_BASE_SERVER: Dict[str, Any] = {
    "server": "prod",
    "type": "kafka",
    "host": "h",
    "format": "json",
}


def _odcs_issues(server: Dict[str, Any]) -> List[Any]:
    """Run the real `fluid validate-artifacts` ODCS validator over one document."""
    yaml = pytest.importorskip("yaml")
    from fluid_build.forge.core.artifact_validators import validate_odcs

    doc = {**_ODCS_BASE, "servers": [server]}
    issues = validate_odcs("odcs/demo.odcs.yaml", yaml.safe_dump(doc).encode())
    return [i for i in issues if i.severity == "error"]


@pytest.mark.skipif(not _ODCS_SCHEMA_PATH.exists(), reason="vendored ODCS schema not present")
def test_validate_odcs_rejects_an_unexpected_key() -> None:
    """The regression, through the shipped entry point.

    Before the dialect fix this returned ZERO errors: `fluid validate-artifacts`
    reported an ODCS document clean while the published ODCS standard rejects it.
    """
    errors = _odcs_issues({**_ODCS_BASE_SERVER, "TOTALLY_BOGUS_KEY": "junk"})
    assert errors, (
        "validate_odcs accepted a server entry with an unexpected key — the "
        "schema's unevaluatedProperties guard is being ignored again, which "
        "means something pinned the validator dialect back to Draft 7"
    )
    assert any("Unevaluated properties" in e.message for e in errors)


@pytest.mark.skipif(not _ODCS_SCHEMA_PATH.exists(), reason="vendored ODCS schema not present")
def test_validate_odcs_still_accepts_a_clean_document() -> None:
    """The negative control, and not a formality.

    A validator that rejected everything would satisfy the test above. This is
    also what would catch the opposite over-correction: 2019-09 enforces
    keywords Draft 7 ignored, so tightening the dialect could plausibly have
    started rejecting documents that are in fact valid.
    """
    assert _odcs_issues(dict(_ODCS_BASE_SERVER)) == []
