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

"""The ODCS `description` block must be emitted as ODCS declares it.

ODCS models `description` as an object of string fields — `purpose`,
`limitations`, `usage`. FLUID models it as a single string. Converting between
them therefore needs a type check in BOTH directions, and only one direction
had it.

The importer type-checked: a Mapping had its `purpose` unwrapped into FLUID's
`metadata.description`, a str was taken as-is. The exporter wrapped
unconditionally — `{"purpose": description}` — so handing it a Mapping produced

    {"purpose": {"purpose": ..., "limitations": ..., "usage": ...}}

an object where the ODCS schema declares a string. Invalid ODCS, emitted with
no error, because nothing downstream checked.

These tests pin both directions and both input shapes, so the asymmetry cannot
come back on either side.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import jsonschema
import pytest

from fluid_build.providers.odcs import OdcsProvider

REPO_ROOT = Path(__file__).resolve().parents[1]
_ODCS_SCHEMA_PATH = REPO_ROOT / "fluid_build" / "providers" / "odcs" / "odcs-schema-v3.1.0.json"

_ODCS_DESCRIPTION: Dict[str, str] = {
    "purpose": "Order data for downstream fulfilment",
    "limitations": "PII columns require approval before consumption",
    "usage": "Consume via the analytics warehouse only",
}


def _contract(description: Any) -> Dict[str, Any]:
    """The smallest contract that carries a description through the exporter."""
    return {
        "version": "1.0.0",
        "apiVersion": "v3.1.0",
        "kind": "DataContract",
        "id": "data-product.orders",
        "status": "active",
        "name": "orders",
        "description": description,
    }


def _render(description: Any) -> Dict[str, Any]:
    return OdcsProvider().render(_contract(description))


# ---------------------------------------------------------------------------
# The regression
# ---------------------------------------------------------------------------


def test_a_description_object_is_emitted_as_siblings_not_nested() -> None:
    """The bug: the whole object was landing inside `purpose`."""
    emitted = _render(dict(_ODCS_DESCRIPTION))["description"]
    assert emitted == _ODCS_DESCRIPTION, (
        "a description object was not passed through as sibling fields. If "
        f"`purpose` now holds a dict, the exporter is wrapping again: {emitted!r}"
    )
    assert isinstance(emitted["purpose"], str)


def test_a_description_string_is_still_wrapped_in_purpose() -> None:
    """The negative control, and the case that actually occurs in practice.

    FLUID declares root `description` as a string, so this is the normal path.
    Without this test, 'stop wrapping' would be satisfied by never wrapping —
    which would emit a bare string where ODCS declares an object.
    """
    assert _render("A plain string")["description"] == {"purpose": "A plain string"}


@pytest.mark.parametrize("empty", ["", None, {}])
def test_an_absent_description_emits_no_description_key(empty: Any) -> None:
    """Neither shape may produce an empty object; ODCS would rather it be absent."""
    assert "description" not in _render(empty)


# ---------------------------------------------------------------------------
# Against the real schema, so the assertions above are not merely self-consistent
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _ODCS_SCHEMA_PATH.exists(), reason="vendored ODCS schema not present")
@pytest.mark.parametrize(
    "description", [dict(_ODCS_DESCRIPTION), "A plain string"], ids=["object", "string"]
)
def test_the_emitted_document_validates_against_the_odcs_schema(description: Any) -> None:
    """What the shape assertions are a proxy for: is the output valid ODCS?

    This is the assertion that would have caught the bug on its own. The old
    exporter emitted a document that failed here on
    `description.purpose: not of type 'string'`.
    """
    schema = json.loads(_ODCS_SCHEMA_PATH.read_text())
    validator = jsonschema.validators.validator_for(schema)(schema)
    errors = [e for e in validator.iter_errors(_render(description))]
    assert not errors, "emitted invalid ODCS: " + "; ".join(
        f"{'/'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message}" for e in errors
    )


# ---------------------------------------------------------------------------
# The other direction, so the symmetry this restores is itself pinned
# ---------------------------------------------------------------------------


def test_the_importer_unwraps_purpose_to_a_plain_string() -> None:
    """The behaviour the exporter now mirrors. If this changes, so must the exporter.

    The mapper writes `metadata.description`, but a later normalise step
    promotes it to the contract root — which is where FLUID declares it, and
    where the exporter's `fluid.get("description")` reads it from. Asserting
    the root rather than the mapper's intermediate is what makes this a test of
    the importer's contract instead of its internals.
    """
    fluid = OdcsProvider().import_contract(_contract(dict(_ODCS_DESCRIPTION)))
    assert fluid["description"] == _ODCS_DESCRIPTION["purpose"]
    assert isinstance(fluid["description"], str)


def test_an_object_description_survives_a_full_round_trip() -> None:
    """End to end: ODCS -> FLUID -> ODCS must return the object unchanged.

    The round-trip already passed before the fix, because the importer stores
    the original object in passthrough and the exporter's `if` branch reads it.
    That is exactly why the bug hid: the path everyone tests took the branch
    that worked, and the broken `else` was only reached by a caller that had
    not imported first.
    """
    provider = OdcsProvider()
    back = provider.render(provider.import_contract(_contract(dict(_ODCS_DESCRIPTION))))
    assert back["description"] == _ODCS_DESCRIPTION
