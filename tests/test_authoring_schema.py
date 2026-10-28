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

"""Editors can check a fragment-first contract, and a fragment on its own (draft 26).

* ``fluid-authoring-schema-0.7.6.json`` is the canonical 0.7.6 schema with a
  ``{$ref}`` accepted at every value position (generated; the canonical
  schema, which commands validate and Command Center generates from, is
  unchanged).
* ``fluid validate --fragment KIND FILE`` checks one fragment against the
  ``$defs`` entry it stands for, with the errors validating the whole
  contract reports.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, List

import jsonschema
import pytest
import yaml

from fluid_build import schema_authoring

_SCHEMAS = Path(schema_authoring.__file__).parent / "schemas"

_CONTRACT = """\
fluidVersion: "0.7.6"
kind: DataProduct
id: commerce.orders
name: Orders
description:
  purpose: One row per order.
  usage: Join on order_id.
domain: commerce
metadata:
  layer: Silver
  owner: {team: commerce-data, email: commerce@example.com}
sovereignty:
  jurisdiction: EU
  dataResidency: true
builds:
  - id: transform
    pattern: embedded-logic
    engine: sql
    properties:
      sql: |
        SELECT order_id FROM src
exposes:
  - exposeId: orders
    kind: table
    binding: {platform: local, format: csv, location: {path: out/orders.csv}}
    contract:
      schema:
        - {name: order_id, type: string, required: true}
        - name: amount
          type: number
      quality:
        - {rule: id_present, expression: order_id IS NOT NULL, severity: error}
"""


def _errors(schema: Any, document: Any) -> List[str]:
    validator = jsonschema.validators.validator_for(schema)(schema)
    return sorted(e.message for e in validator.iter_errors(document))


def test_the_bundled_authoring_schema_is_generated_from_the_canonical_one() -> None:
    for version in schema_authoring.AUTHORING_VERSIONS:
        assert schema_authoring.authoring_schema_path(version).read_text("utf-8") == (
            schema_authoring.render(version)
        ), "regenerate with: python -m fluid_build.schema_authoring"


def test_the_name_stays_out_of_the_canonical_schema_glob() -> None:
    """Version discovery, tests and the docs generator read fluid-schema-*.json."""
    assert not list(_SCHEMAS.glob("fluid-schema-*authoring*"))
    from fluid_build.schema_manager import FluidSchemaManager

    assert all("authoring" not in v for v in FluidSchemaManager.BUNDLED_VERSIONS)


def test_an_editor_reports_nothing_on_a_contract_split_into_every_part(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fluid_build.cli import main

    monkeypatch.chdir(tmp_path)
    path = tmp_path / "contract.fluid.yaml"
    path.write_text(_CONTRACT, encoding="utf-8")
    parts = "sovereignty,builds,exposes,sql,quality,schema,description"
    assert main(["split", str(path), "--parts", parts]) == 0
    authoring = json.loads(schema_authoring.authoring_schema_path("0.7.6").read_text("utf-8"))
    canonical = json.loads((_SCHEMAS / "fluid-schema-0.7.6.json").read_text("utf-8"))

    root = yaml.safe_load(path.read_text("utf-8"))
    assert _errors(authoring, root) == []
    assert _errors(canonical, root) != []  # the canonical schema still wants it composed
    expose = yaml.safe_load((tmp_path / "fragments" / "exposes" / "orders.yaml").read_text("utf-8"))
    one_def = {"$schema": authoring["$schema"], "$defs": authoring["$defs"]}
    assert _errors({**one_def, "$ref": "#/$defs/expose"}, expose) == []
    # A ref with siblings is a fragment reference too.
    root["sovereignty"] = {"$ref": "./fragments/sovereignty.yaml", "dataResidency": False}
    assert _errors(authoring, root) == []


def test_a_value_that_is_not_a_ref_keeps_the_canonical_message() -> None:
    authoring = json.loads(schema_authoring.authoring_schema_path("0.7.6").read_text("utf-8"))
    canonical = json.loads((_SCHEMAS / "fluid-schema-0.7.6.json").read_text("utf-8"))
    broken = yaml.safe_load(_CONTRACT.replace("kind: table", "kind: 42"))

    assert _errors(authoring, broken) == _errors(canonical, broken) != []


# ── fluid validate --fragment ───────────────────────────────────────────


@pytest.fixture
def split(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from fluid_build.cli import main

    monkeypatch.chdir(tmp_path)
    path = tmp_path / "contract.fluid.yaml"
    path.write_text(_CONTRACT, encoding="utf-8")
    assert main(["split", str(path), "--parts", "exposes,quality"]) == 0
    return path


def test_a_valid_fragment_validates_as_what_it_stands_for(split: Path, capsys: Any) -> None:
    from fluid_build.cli import main

    assert main(["validate", "--fragment", "expose", "fragments/exposes/orders.yaml"]) == 0
    assert "Valid expose fragment (schema v0.7.6)" in capsys.readouterr().out
    # A list fragment, against its list's schema.
    assert main(["validate", "--fragment", "quality", "fragments/quality/orders.yaml"]) == 0


def test_a_fragment_reports_the_errors_the_whole_contract_does(split: Path, capsys: Any) -> None:
    from fluid_build.cli import main

    fragment = split.parent / "fragments" / "exposes" / "orders.yaml"
    fragment.write_text(fragment.read_text("utf-8").replace("type: number", "type: 42"), "utf-8")
    lines = fragment.read_text("utf-8").splitlines()
    line = next(n for n, text in enumerate(lines, 1) if text.strip() == "type: 42")

    assert main(["validate", str(split)]) == 1
    whole = capsys.readouterr().out
    assert main(["validate", "--fragment", "expose", "fragments/exposes/orders.yaml"]) == 1
    alone = capsys.readouterr().out

    message = "42 is not valid under any of the given schemas"
    assert f"fragments/exposes/orders.yaml:{line} (exposes[0].contract.schema[1].type): " in whole
    assert f"fragments/exposes/orders.yaml:{line} (contract.schema[1].type): {message}" in alone


def test_the_schema_version_comes_from_the_flag_or_the_nearest_contract(
    split: Path, capsys: Any
) -> None:
    from fluid_build.cli import main

    args = ["validate", "--fragment", "expose", "fragments/exposes/orders.yaml"]
    assert main([*args, "--schema-version", "0.7.5"]) == 0
    assert "(schema v0.7.5)" in capsys.readouterr().out


def test_an_unknown_kind_lists_the_kinds(split: Path, capsys: Any) -> None:
    from fluid_build.cli import main

    assert main(["validate", "--fragment", "nonsense", "fragments/exposes/orders.yaml"]) == 1
    out = capsys.readouterr()
    assert "'nonsense' is not a kind of fragment in schema 0.7.6; kinds:" in out.out + out.err


def test_a_fragment_with_a_pointer_asks_for_the_contract(split: Path, capsys: Any) -> None:
    from fluid_build.cli import main

    fragment = split.parent / "fragments" / "exposes" / "orders.yaml"
    fragment.write_text(fragment.read_text("utf-8") + 'tags: {$ref: "#/tags"}\n', "utf-8")

    assert main(["validate", "--fragment", "expose", "fragments/exposes/orders.yaml"]) == 1
    out = capsys.readouterr()
    assert "same-document pointers" in out.out + out.err
