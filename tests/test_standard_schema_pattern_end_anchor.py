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

"""The other schema checks read a ``pattern``'s ``$`` as ECMA-262 does, too.

``tests/test_schema_pattern_end_anchor.py`` covers the FLUID schemas. fluid also
validates against the vendored ODCS, Bitol ODPS and OPDS schemas, the Ossie
schema, and the schema an installed plugin gives its ``extensions.<key>`` block.
Each of those checks ran jsonschema's own ``pattern``, Python's ``re.search``,
whose ``$`` also matches before a final ``\\n``. So ODCS refused
``schema[].id: "t 1"`` but took ``"t1\\n"``, which an ``id: |`` block produces.

Of the vendored schemas only ODCS has an end-anchored ``pattern`` today, so the
other checks are tested with a schema of the test's own. The tripwires in
``tests/test_schema_pattern_end_anchor.py`` cover the vendored schemas too: what
they would have to start using for a check to leave the rewrite.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any, Dict, Iterator

import jsonschema
import pytest
import yaml

import fluid_build
import fluid_build.extension_schemas as extension_schemas
from fluid_build.forge.core.artifact_validators import validate_odcs, validate_opds
from fluid_build.forge_datamodel.emit import osi_sidecar
from fluid_build.providers.base import ProviderError
from fluid_build.providers.odcs.validation import collect_errors, load_schema
from fluid_build.providers.odps_standard import validation as odps_validation
from fluid_build.providers.opds import validator as opds_validator
from tests.test_jsonschema_dialect import _ODCS_BASE

pytestmark = [pytest.mark.unit]

_LINE_BREAK_HINT = " (it ends in a line break, which a YAML | or > block keeps and |- or >- drops)"
#: A schema of the test's own, for the checks whose vendored schema has no pattern.
_NAME_SCHEMA: Dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {"name": {"type": "string", "pattern": "^[a-z]+$"}},
}


def _odcs(table_id: str) -> Dict[str, Any]:
    return {**_ODCS_BASE, "schema": [{"id": table_id, "name": "orders", "logicalType": "object"}]}


def _odcs_yaml(block: str, table_id: str = "t1") -> str:
    """An ODCS document whose ``schema[0].id`` is a YAML block scalar: ``|`` or ``|-``."""
    body = yaml.safe_dump(_ODCS_BASE)
    return f"{body}schema:\n- id: {block}\n    {table_id}\n  name: orders\n  logicalType: object\n"


# ── ODCS: fluid odcs validate, fluid validate-artifacts ─────────────────────


def test_odcs_refuses_a_table_id_that_ends_in_a_newline() -> None:
    """``collect_errors`` is ``fluid odcs validate``'s jsonschema pass."""
    schema = load_schema()
    assert schema is not None
    assert collect_errors(_odcs("t1"), schema) == []

    errors = [e for e in collect_errors(_odcs("t1\n"), schema) if e["validator"] == "pattern"]

    assert [e["path"] for e in errors] == ["schema.0.id"], errors
    assert errors[0]["message"].endswith(_LINE_BREAK_HINT), errors


def test_fluid_odcs_validate_refuses_a_block_scalar_table_id(tmp_path: Path, capsys: Any) -> None:
    from fluid_build.cli import main

    path = tmp_path / "orders.odcs.yaml"
    path.write_text(_odcs_yaml("|"), encoding="utf-8")
    assert yaml.safe_load(path.read_text(encoding="utf-8"))["schema"][0]["id"] == "t1\n"

    rc = main(["odcs", "validate", str(path), "--no-vowl"])

    text = capsys.readouterr().out
    assert rc == 1, text
    assert "schema.0.id: 't1\\n' does not match" in text, text


def test_fluid_odcs_validate_keeps_the_hint_whole(tmp_path: Path, capsys: Any) -> None:
    """The command cuts each message to 120 characters. Cut inside the hint, a long id's
    message ended ``... block keeps and |``, which reads as advice to use ``|``."""
    from fluid_build.cli import main

    path = tmp_path / "orders.odcs.yaml"
    path.write_text(_odcs_yaml("|", "customer_orders_v1"), encoding="utf-8")

    rc = main(["odcs", "validate", str(path), "--no-vowl"])

    text = " ".join(capsys.readouterr().out.split())  # undo the console's line wrapping
    assert rc == 1, text
    assert f"'customer_orders_v1\\n' does not match '^[A-Za-z0-9_-]+$'{_LINE_BREAK_HINT}" in text


def test_fluid_odcs_validate_prints_a_value_with_brackets_literally(
    tmp_path: Path, capsys: Any
) -> None:
    """The message quotes the value, and Rich read ``[/]`` in it as a closing tag: the
    command ended in ``Unexpected error: closing tag '[/]' ... has nothing to close``."""
    from fluid_build.cli import main

    path = tmp_path / "orders.odcs.yaml"
    path.write_text(yaml.safe_dump(_odcs("t[/]1")), encoding="utf-8")

    rc = main(["odcs", "validate", str(path), "--no-vowl"])

    text = " ".join(capsys.readouterr().out.split())
    assert rc == 1, text
    assert "schema.0.id: 't[/]1' does not match" in text, text


def test_validate_artifacts_refuses_a_block_scalar_table_id() -> None:
    """``validate_odcs`` is what ``fluid validate-artifacts`` runs on ``odcs/*.odcs.yaml``."""
    assert validate_odcs("odcs/orders.odcs.yaml", _odcs_yaml("|-").encode()) == []

    issues = validate_odcs("odcs/orders.odcs.yaml", _odcs_yaml("|").encode())

    messages = [i.message for i in issues]
    assert any(m.startswith("at schema/0/id: 't1\\n' does not match ") for m in messages), messages


# ── Bitol ODPS, OPDS, Ossie: no pattern in the vendored schema yet ──────────


def test_odps_validate_reads_a_pattern_as_ecma_does() -> None:
    odps_validation.validate({"name": "orders"}, _NAME_SCHEMA)

    with pytest.raises(ProviderError, match=re.escape("'orders\\n' does not match '^[a-z]+$'")):
        odps_validation.validate({"name": "orders\n"}, _NAME_SCHEMA)


def test_odps_validate_still_checks_the_schema_first() -> None:
    """It still runs ``jsonschema.validate``, now with the rewritten class."""
    with pytest.raises(jsonschema.SchemaError):
        odps_validation.validate({}, {"type": 5})


def test_opds_validation_reads_a_pattern_as_ecma_does(monkeypatch: Any) -> None:
    monkeypatch.setitem(opds_validator._SCHEMA_CACHE, "0.0-test", _NAME_SCHEMA)
    validate = opds_validator.validate_against_opds_schema
    assert validate({"name": "orders"}, "0.0-test") == (True, None, "full_schema")

    valid, errors, kind = validate({"name": "orders\n"}, "0.0-test")

    assert (valid, kind) == (False, "full_schema")
    assert errors == [f"name: 'orders\\n' does not match '^[a-z]+$'{_LINE_BREAK_HINT}"]


def test_an_opds_schema_that_declares_no_dialect_is_2020_12(monkeypatch: Any) -> None:
    """What the check pinned before. Draft 7 has no ``dependentRequired`` and would
    pass this document."""
    schema = {"type": "object", "dependentRequired": {"a": ["b"]}}
    monkeypatch.setitem(opds_validator._SCHEMA_CACHE, "0.0-test", schema)

    valid, errors, _ = opds_validator.validate_against_opds_schema({"a": 1}, "0.0-test")

    assert (valid, errors) == (False, ["root: 'b' is a dependency of 'a'"])


def test_ossie_document_validation_reads_a_pattern_as_ecma_does(monkeypatch: Any) -> None:
    monkeypatch.setattr(osi_sidecar, "_load_ossie_schema", lambda: _NAME_SCHEMA)
    assert osi_sidecar.validate_osi_document({"name": "orders"}) == []

    issues = osi_sidecar.validate_osi_document({"name": "orders\n"})

    assert issues == [f"name: 'orders\\n' does not match '^[a-z]+$'{_LINE_BREAK_HINT}"]


def test_without_jsonschema_each_check_says_it_is_missing(monkeypatch: Any) -> None:
    """The checks read the flag ``schema_validator`` needs. A jsonschema that is found
    but fails to import is reported as missing, not as ``NameError: jsonschema``."""
    monkeypatch.setattr("fluid_build.schema_manager.JSONSCHEMA_AVAILABLE", False)

    assert [i.code for i in validate_odcs("odcs/o.odcs.yaml", b"id: x")] == ["ODCS-LIB-MISSING"]
    assert [i.code for i in validate_opds("opds/o.opds.json", b"{}")] == ["OPDS-LIB-MISSING"]
    assert collect_errors(_odcs("t1\n"), {}) == []
    with pytest.raises(ImportError, match="jsonschema is not available"):
        osi_sidecar.validate_osi_document({})


# ── plugin extension blocks ─────────────────────────────────────────────────

#: The shape of data-product-forge-custom-scaffold's schema: draft-07, "$"-anchored.
_SCAFFOLD_SCHEMA: Dict[str, Any] = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "type": "object",
    "properties": {
        "patterns": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "use": {
                        "type": "string",
                        "pattern": "^[a-zA-Z][a-zA-Z0-9_-]*:[a-zA-Z][a-zA-Z0-9_-]*$",
                    }
                },
            },
        }
    },
}


def _assemble(monkeypatch: Any, schema: Dict[str, Any], block: Any) -> Dict[str, Any]:
    monkeypatch.setattr(extension_schemas, "iter_extension_schemas", lambda *a, **k: {"x": schema})
    return extension_schemas.assemble_proposed_extensions({"x": block})


def test_an_extension_block_whose_value_ends_in_a_newline_is_dropped(monkeypatch: Any) -> None:
    kept = {"patterns": [{"use": "ci:basic"}]}
    assert _assemble(monkeypatch, _SCAFFOLD_SCHEMA, kept) == {"x": kept}
    assert _assemble(monkeypatch, _SCAFFOLD_SCHEMA, {"patterns": [{"use": "ci:basic\n"}]}) == {}


def test_an_extension_schema_is_read_in_the_dialect_it_declares(monkeypatch: Any) -> None:
    """Draft 7 ignores ``unevaluatedProperties``, so the extra key used to be kept."""
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {"libraries": {"type": "array"}},
        "unevaluatedProperties": False,
    }
    assert _assemble(monkeypatch, schema, {"libraries": []}) == {"x": {"libraries": []}}
    assert _assemble(monkeypatch, schema, {"libraries": [], "librarys": []}) == {}


def test_an_extension_schema_that_cannot_be_applied_drops_only_its_block(
    monkeypatch: Any, caplog: Any
) -> None:
    """A malformed plugin schema neither stops the run nor lets its block through."""
    schemas = {"bad": {"$schema": 7, "type": "string"}, "x": _SCAFFOLD_SCHEMA}
    monkeypatch.setattr(extension_schemas, "iter_extension_schemas", lambda *a, **k: schemas)
    kept = {"patterns": [{"use": "ci:basic"}]}

    assert extension_schemas.assemble_proposed_extensions({"bad": "a", "x": kept}) == {"x": kept}
    assert "extension schema 'bad' unusable" in caplog.text


def test_an_extension_schema_that_declares_no_dialect_is_draft_07(monkeypatch: Any) -> None:
    """The plugin contract says draft-07. 2020-12, jsonschema's default, has no
    ``dependencies`` keyword and would keep this block."""
    schema = {"type": "object", "dependencies": {"output": ["patterns"]}}
    kept = {"output": "x", "patterns": []}
    assert _assemble(monkeypatch, schema, kept) == {"x": kept}
    assert _assemble(monkeypatch, schema, {"output": "x"}) == {}


# ── one way to build a validator ────────────────────────────────────────────


def _validator_builds(path: Path) -> Iterator[str]:
    """Spellings in ``path`` that build a jsonschema validator without ``schema_validator``.
    It goes by names, so a class first bound to another name (``V = Draft7Validator``)
    gets past it."""
    text = path.read_text(encoding="utf-8")
    if not any(word in text for word in ("jsonschema", "Validator", "validator_for")):
        return
    tree = ast.parse(text)
    modules = {"jsonschema"}  # names that hold jsonschema or jsonschema.validators
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules |= {a.asname for a in node.names if a.name == "jsonschema" and a.asname}
        elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith("jsonschema"):
            modules |= {a.asname or a.name for a in node.names if a.name == "validators"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("jsonschema"):
            for a in node.names:
                builder = a.name == "validator_for" or re.fullmatch(r"Draft\d+Validator", a.name)
                if a.name == "validate" or (a.asname and builder):
                    yield f"{path.name}:{node.lineno}: from {node.module} import {a.name}"
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name == "validator_for" or re.fullmatch(r"Draft\d+Validator", name):
            yield f"{path.name}:{node.lineno}: {name}()"
        elif name in ("validate", "extend", "create") and isinstance(func, ast.Attribute):
            root = func.value
            while isinstance(root, ast.Attribute):
                root = root.value
            cls = next((k.value for k in node.keywords if k.arg == "cls"), None)
            routed = cls is not None and "schema_validator" in ast.unparse(cls)
            if isinstance(root, ast.Name) and root.id in modules and not routed:
                yield f"{path.name}:{node.lineno}: {ast.unparse(func)}()"


def test_every_validator_is_built_by_schema_validator() -> None:
    """A check that builds its own validator reads ``$`` as Python does again; #799 and
    this change each found such checks one at a time. Give ``schema_validator`` the
    dialect class as ``default=`` instead of building it."""
    package = Path(fluid_build.__file__).parent
    paths = [p for p in sorted(package.rglob("*.py")) if p != package / "schema_manager.py"]
    assert len(paths) > 500, len(paths)  # the glob found the package
    assert [build for path in paths for build in _validator_builds(path)] == []
