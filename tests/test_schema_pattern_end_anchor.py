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

"""A schema ``pattern`` ends where the value ends, as ECMA-262 reads ``$``.

JSON Schema writes ``pattern`` in the ECMA-262 dialect, where ``$`` (no ``m``
flag) matches only at the end of the input. jsonschema runs it with Python's
``re.search``, and Python's ``$`` also matches before a final ``\\n``. So
``fluid validate`` accepted ``id: "orders\\n"``, which a YAML ``|`` block
scalar produces, while an editor validating against the same schema in
JavaScript refused it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, Iterator, List

import pytest
import yaml

import fluid_build
from fluid_build.schema_manager import FluidSchemaManager, _end_anchored, schema_validator

try:  # Python 3.11+
    from re import _parser as sre_parse
except ImportError:  # Python 3.10
    import sre_parse  # type: ignore[no-redef]

pytestmark = [pytest.mark.unit]

_VERSIONS = list(FluidSchemaManager.BUNDLED_VERSIONS)
_SCHEMA_DIR = Path(fluid_build.__file__).parent / "schemas"
_LINE_BREAK_HINT = " (it ends in a line break, which a YAML | or > block keeps and |- or >- drops)"


def _expose(expose_id: str = "orders") -> Dict[str, Any]:
    return {
        "exposeId": expose_id,
        "kind": "table",
        "binding": {
            "platform": "local",
            "format": "csv",
            "location": {"path": "data/orders.csv"},
        },
        "contract": {"schema": [{"name": "id", "type": "integer"}]},
    }


def _contract(version: str, product_id: str = "orders") -> Dict[str, Any]:
    return {
        "fluidVersion": version,
        "kind": "DataProduct",
        "id": product_id,
        "name": "Orders",
        "domain": "sales",
        "metadata": {"layer": "Bronze", "owner": {"team": "data"}},
        "exposes": [_expose()],
    }


def _errors(contract: Dict[str, Any]) -> List[str]:
    version = contract["fluidVersion"]
    result = FluidSchemaManager().validate_contract(contract, version, offline_only=True)
    return [] if result.is_valid else list(result.errors)


def test_every_bundled_version_is_checked() -> None:
    """Guard the guard: an empty version list would pass every case below."""
    assert len(_VERSIONS) >= 7, _VERSIONS


@pytest.mark.parametrize("version", _VERSIONS)
def test_an_id_that_ends_in_a_newline_is_refused(version: str) -> None:
    assert _errors(_contract(version)) == []
    errors = _errors(_contract(version, "orders\n"))
    assert any(e.startswith("id: ") for e in errors), errors


def test_fluid_validate_refuses_a_block_scalar_id(tmp_path: Path, capsys: Any) -> None:
    """``id: |`` keeps the line break, so the value is ``"orders\\n"``."""
    from fluid_build.cli import main

    body = yaml.safe_dump({k: v for k, v in _contract("0.7.6").items() if k != "id"})
    contract = tmp_path / "contract.fluid.yaml"
    contract.write_text(f"id: |\n  orders\n{body}", encoding="utf-8")
    assert yaml.safe_load(contract.read_text(encoding="utf-8"))["id"] == "orders\n"

    rc = main(["validate", str(contract), "--offline", "--cache-dir", str(tmp_path / "cache")])

    captured = capsys.readouterr()
    text = captured.out + captured.err
    assert rc == 1, text
    assert "id" in text and "does not match" in text, text


def test_fluid_validate_refuses_a_fragment_whose_expose_id_ends_in_a_newline(
    tmp_path: Path, capsys: Any
) -> None:
    from fluid_build.cli import main

    fragment = tmp_path / "fragments" / "orders.yaml"
    fragment.parent.mkdir(parents=True)
    fragment.write_text(yaml.safe_dump(_expose("orders\n")), encoding="utf-8")
    argv = ["validate", "--offline", "--cache-dir", str(tmp_path / "cache")]
    argv += ["--schema-version", "0.7.6", "--fragment", "expose", str(fragment)]

    rc = main(argv)

    captured = capsys.readouterr()
    text = captured.out + captured.err
    assert rc == 1, text
    assert "exposeId" in text and "does not match" in text, text


def test_bundle_validation_refuses_an_id_that_ends_in_a_newline() -> None:
    """``fluid validate <bundle>.tgz`` checks the resolved contract through
    ``forge.core.validators``, not ``FluidSchemaManager.validate_contract``."""
    from fluid_build.forge.core.validators import validate_json_schema

    def issues(product_id: str) -> List[str]:
        found = validate_json_schema(
            "contract.resolved.yaml",
            _contract("0.7.6", product_id),
            schema_manager=FluidSchemaManager(),
        )
        return [i.message for i in found]

    assert issues("orders") == []
    messages = issues("orders\n")
    assert any(m.startswith("at id: ") for m in messages), messages


def test_bundle_validation_without_jsonschema_is_an_error(monkeypatch: Any) -> None:
    """The check reads the flag ``schema_validator`` needs: a jsonschema that is found but
    fails to import is the library-missing error, not a skipped check."""
    import fluid_build.schema_manager as schema_manager
    from fluid_build.forge.core.validators import validate_json_schema

    monkeypatch.setattr(schema_manager, "JSONSCHEMA_AVAILABLE", False)
    issues = validate_json_schema(
        "contract.resolved.yaml", _contract("0.7.6"), schema_manager=FluidSchemaManager()
    )
    assert [(i.code, i.severity) for i in issues] == [("SCHEMA-LIB-MISSING", "error")]


def test_the_error_says_the_value_ends_in_a_line_break() -> None:
    errors = _errors(_contract("0.7.6", "orders\n"))
    assert len(errors) == 1, errors
    assert errors[0].startswith("id: 'orders\\n' does not match ")
    assert errors[0].endswith(_LINE_BREAK_HINT)


@pytest.mark.parametrize("product_id", ["orders!", "orders!\n"])
def test_a_value_wrong_for_another_reason_gets_no_line_break_hint(product_id: str) -> None:
    errors = _errors(_contract("0.7.6", product_id))
    assert errors and not any("line break" in e for e in errors), errors


def test_the_json_schema_test_suite_case() -> None:
    """JSON-Schema-Test-Suite, ``tests/draft2020-12/optional/ecmascript-regex.json``:
    "ECMA 262 regex $ does not match trailing newline"."""
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "string",
        "pattern": "^abc$",
    }
    assert schema_validator(schema).is_valid("abc")
    assert not schema_validator(schema).is_valid("abc\n")


def test_a_not_pattern_reads_the_end_anchor_the_same_way() -> None:
    """``not: {pattern: ...}`` now matches fewer values, as in ECMA-262: a deny rule that
    has to catch a line break must name one (catalogAuth refuses ``[\\x00-\\x1f\\x7f]``)."""
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "not": {"pattern": "^x$"},
    }
    assert not schema_validator(schema).is_valid("x")
    assert schema_validator(schema).is_valid("x\n")


# ── the rewrite ─────────────────────────────────────────────────────────────


def _tree(pattern: str) -> str:
    """Python's own parse: ``[(AT, AT_BEGINNING), (LITERAL, 97), (AT, AT_END)]``."""
    return repr(sre_parse.parse(pattern))


def _only_end_anchors_changed(pattern: str) -> bool:
    """Python's parser reads the rewrite as ``pattern`` with each ``$`` (``AT_END``)
    made ``\\Z`` (``AT_END_STRING``), and nothing else changed."""
    expected = re.sub(r"\bAT_END\b", "AT_END_STRING", _tree(pattern))
    return _tree(_end_anchored(pattern)) == expected


@pytest.mark.parametrize(
    "pattern, rewritten",
    [
        (r"^abc$", r"^abc\Z"),
        (r"^a$|^b$", r"^a\Z|^b\Z"),
        (r"(?:^|_)[A-Za-z0-9]{20,}(?:_|$)", r"(?:^|_)[A-Za-z0-9]{20,}(?:_|\Z)"),
        (r"^P(?!$)(\d+D)?$", r"^P(?!\Z)(\d+D)?\Z"),
        (r"^\$\{$", r"^\$\{\Z"),  # an escaped "$" is a dollar sign
        (r"^a\\$", r"^a\\\Z"),  # after an escaped backslash it is the anchor
        (r"^[$]+$", r"^[$]+\Z"),  # in a set it is a dollar sign
        (r"^[\]$]+$", r"^[\]$]+\Z"),
        (r"^[]$]+$", r"^[]$]+\Z"),  # a "]" first in a set does not close it
        (r"^[^]$]+$", r"^[^]$]+\Z"),
        (r"^[a-]$", r"^[a-]\Z"),
    ],
)
def test_the_rewrite_changes_only_the_end_anchors(pattern: str, rewritten: str) -> None:
    assert _end_anchored(pattern) == rewritten
    assert _only_end_anchors_changed(pattern)


def _nodes(node: Any) -> Iterator[Dict[str, Any]]:
    """Every object in a schema document, the root first."""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _nodes(value)
    elif isinstance(node, list):
        for value in node:
            yield from _nodes(value)


def test_every_bundled_schema_pattern_is_rewritten_anchor_for_anchor() -> None:
    """The scan reads escapes and sets as Python does, checked against Python's own
    parser for every ``pattern`` the bundled schemas hold."""
    patterns = {
        node["pattern"]
        for path in _SCHEMA_DIR.glob("fluid-*schema-*.json")
        for node in _nodes(json.loads(path.read_text(encoding="utf-8")))
        if isinstance(node.get("pattern"), str)
    }
    assert len(patterns) >= 20, patterns  # the glob found the schemas
    assert [p for p in patterns if not _only_end_anchors_changed(p)] == []


@pytest.mark.parametrize(
    "path", sorted(_SCHEMA_DIR.glob("fluid-schema-*.json")), ids=lambda path: path.name
)
def test_every_pattern_of_the_schema_is_checked_with_the_rewrite(path: Path) -> None:
    """jsonschema picks the validator class again for each subschema it enters, by
    that subschema's ``$schema``. One that declares it, or a ``$ref`` to the root,
    which does, would be checked with jsonschema's own ``pattern`` again.
    ``patternProperties`` keys are not rewritten (``schema_validator`` says why), so
    a schema that starts to use the keyword has to come back here."""
    below_root = list(_nodes(json.loads(path.read_text(encoding="utf-8"))))[1:]
    assert [n for n in below_root if isinstance(n.get("$schema"), str)] == []
    assert [n for n in below_root if n.get("$ref") == "#"] == []
    assert [n for n in below_root if "patternProperties" in n] == []
