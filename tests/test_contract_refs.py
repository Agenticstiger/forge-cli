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

"""One ``$ref`` resolve step: siblings, text targets, fragments of any root.

``fluid_build.contract_refs`` reads a target, applies its pointer, and merges
the keys written beside ``$ref`` over it:

* siblings are a JSON Merge Patch (RFC 7386): objects merge, a list or a
  scalar replaces, ``null`` removes the key; a target that is not an object
  takes no siblings;
* a ``.sql`` / ``.md`` / ... target is its text, never parsed, and a pointer
  cannot select inside it;
* a fragment's root may be any JSON or YAML value, a list included;
* what refs may bring into one contract is capped.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pytest
import yaml

from fluid_build import contract_refs
from fluid_build.loader import (
    RefConfinementError,
    RefResolutionError,
    _resolve_refs,
    compile_contract,
    load_contract,
)


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _contract(tmp_path: Path, body: Dict[str, Any]) -> Path:
    return _write(tmp_path / "contract.fluid.yaml", yaml.safe_dump(body, sort_keys=False))


# ── siblings ────────────────────────────────────────────────────────────


def test_sibling_keys_are_merged_over_the_target(tmp_path: Path) -> None:
    _write(
        tmp_path / "shared" / "columns.yaml",
        "order_id: {name: order_id, type: string, required: true, description: Order key}\n",
    )
    path = _contract(
        tmp_path,
        {
            "schema": [
                {"$ref": "./shared/columns.yaml#/order_id", "description": "The order this row is"}
            ]
        },
    )

    assert load_contract(path)["schema"] == [
        {
            "name": "order_id",
            "type": "string",
            "required": True,
            "description": "The order this row is",
        }
    ]


def test_siblings_merge_objects_and_replace_lists(tmp_path: Path) -> None:
    _write(
        tmp_path / "input.yaml",
        "connector: kafka\noptions: {a: 1, b: 2}\ntags: [x, y]\n",
    )
    path = _contract(
        tmp_path,
        {"table": {"$ref": "./input.yaml", "options": {"b": 3, "c": 4}, "tags": ["z"]}},
    )

    assert load_contract(path)["table"] == {
        "connector": "kafka",
        "options": {"a": 1, "b": 3, "c": 4},
        "tags": ["z"],
    }


def test_a_null_sibling_removes_the_key(tmp_path: Path) -> None:
    _write(tmp_path / "input.yaml", "connector: kafka\noptions: {a: 1, b: 2}\n")
    path = _contract(tmp_path, {"table": {"$ref": "./input.yaml", "options": {"a": None}}})

    assert load_contract(path)["table"] == {"connector": "kafka", "options": {"b": 2}}


def test_siblings_never_reach_another_ref_to_the_same_target(tmp_path: Path) -> None:
    _write(tmp_path / "input.yaml", "options: {a: 1}\n")
    path = _contract(
        tmp_path,
        {
            "first": {"$ref": "./input.yaml", "options": {"a": 2}},
            "second": {"$ref": "./input.yaml"},
        },
    )

    contract = load_contract(path)

    assert contract["first"] == {"options": {"a": 2}}
    assert contract["second"] == {"options": {"a": 1}}


@pytest.mark.parametrize(
    ("name", "text", "kind"),
    [("rules.yaml", "- a\n- b\n", "a list"), ("query.sql", "SELECT 1\n", "text")],
)
def test_siblings_on_a_target_that_is_not_an_object_are_refused(
    tmp_path: Path, name: str, text: str, kind: str
) -> None:
    _write(tmp_path / name, text)
    path = _contract(tmp_path, {"x": {"$ref": f"./{name}", "note": "kept?"}})

    with pytest.raises(RefResolutionError, match=f"the target is {kind}, not an object"):
        load_contract(path)


def test_a_sibling_ref_resolves_relative_to_the_file_that_holds_it(tmp_path: Path) -> None:
    _write(tmp_path / "parts" / "table.yaml", "connector: kafka\n")
    _write(tmp_path / "local.yaml", "mode: earliest\n")
    path = _contract(
        tmp_path, {"table": {"$ref": "./parts/table.yaml", "scan": {"$ref": "./local.yaml"}}}
    )

    assert load_contract(path)["table"] == {"connector": "kafka", "scan": {"mode": "earliest"}}


def test_a_ref_inside_a_sibling_is_confined(tmp_path: Path) -> None:
    _write(tmp_path / "table.yaml", "connector: kafka\n")
    path = _contract(
        tmp_path,
        {"table": {"$ref": "./table.yaml", "extra": {"$ref": "https://example.com/x.yaml"}}},
    )

    with pytest.raises(RefConfinementError, match="is a URL"):
        load_contract(path)


def test_a_ref_with_siblings_must_be_a_string(tmp_path: Path) -> None:
    path = _contract(tmp_path, {"table": {"$ref": 42, "note": "x"}})

    with pytest.raises(RefResolutionError, match="must be a string, got int"):
        load_contract(path)


# ── text targets ────────────────────────────────────────────────────────

_SQL = (
    "-- Revenue per customer: monthly rollup\n"
    "WITH monthly AS (\n"
    "\tSELECT customer_id, 'a: b' AS note\n"
    "\tFROM orders\n"
    ")\n"
    "SELECT * FROM monthly\n"
)


def test_a_sql_target_is_its_text_never_parsed(tmp_path: Path) -> None:
    # ': ', a tab and a comment would each break a YAML parse of this file.
    _write(tmp_path / "sql" / "transform.sql", _SQL)
    path = _contract(
        tmp_path, {"builds": [{"properties": {"sql": {"$ref": "./sql/transform.sql"}}}]}
    )

    assert load_contract(path)["builds"][0]["properties"]["sql"] == _SQL


def test_a_markdown_target_is_its_text(tmp_path: Path) -> None:
    usage = "# Usage\n\nNote: join on `order_id`.\n"
    _write(tmp_path / "docs" / "usage.md", usage)
    path = _contract(tmp_path, {"description": {"usage": {"$ref": "./docs/usage.md"}}})

    assert load_contract(path)["description"] == {"usage": usage}


def test_text_suffixes_match_without_case(tmp_path: Path) -> None:
    _write(tmp_path / "Q.SQL", "SELECT 1")
    path = _contract(tmp_path, {"sql": {"$ref": "./Q.SQL"}})

    assert load_contract(path)["sql"] == "SELECT 1"


def test_text_drops_a_byte_order_mark_and_reads_newlines_as_lf(tmp_path: Path) -> None:
    (tmp_path / "q.sql").write_bytes("﻿SELECT 1\r\nFROM t\r\n".encode("utf-8"))
    path = _contract(tmp_path, {"sql": {"$ref": "./q.sql"}})

    assert load_contract(path)["sql"] == "SELECT 1\nFROM t\n"


def test_an_empty_text_file_is_an_empty_string(tmp_path: Path) -> None:
    _write(tmp_path / "empty.md", "")
    path = _contract(tmp_path, {"usage": {"$ref": "./empty.md"}})

    assert load_contract(path)["usage"] == ""


def test_a_pointer_into_a_text_file_is_refused(tmp_path: Path) -> None:
    _write(tmp_path / "docs" / "usage.md", "# Usage\n")
    path = _contract(tmp_path, {"usage": {"$ref": "./docs/usage.md#install"}})

    with pytest.raises(RefResolutionError, match=r"a \.md file is read as text") as info:
        load_contract(path)
    assert "'#install'" in str(info.value)


def test_a_text_target_outside_the_root_is_refused(tmp_path: Path) -> None:
    _write(tmp_path / "secret.md", "not yours\n")
    path = _write(tmp_path / "product" / "contract.fluid.yaml", "usage: {$ref: ../secret.md}\n")

    with pytest.raises(RefConfinementError, match="escapes the ref root"):
        load_contract(path)


def test_a_text_file_over_the_cap_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(contract_refs, "MAX_TEXT_REF_BYTES", 16)
    _write(tmp_path / "big.sql", "SELECT 1 FROM a_table_with_a_long_name\n")
    path = _contract(tmp_path, {"sql": {"$ref": "./big.sql"}})

    with pytest.raises(RefResolutionError, match="over the 16-byte cap"):
        load_contract(path)


def test_a_text_file_that_is_not_utf8_is_refused(tmp_path: Path) -> None:
    (tmp_path / "q.sql").write_bytes(b"SELECT '\xff'")
    path = _contract(tmp_path, {"sql": {"$ref": "./q.sql"}})

    with pytest.raises(RefResolutionError, match="not UTF-8 text"):
        load_contract(path)


def test_the_same_text_file_twice_is_not_a_cycle(tmp_path: Path) -> None:
    _write(tmp_path / "q.sql", "SELECT 1")
    path = _contract(tmp_path, {"a": {"$ref": "./q.sql"}, "b": [{"$ref": "./q.sql"}]})

    contract = load_contract(path)

    assert (contract["a"], contract["b"]) == ("SELECT 1", ["SELECT 1"])


# ── fragments of any root ───────────────────────────────────────────────


def test_a_yaml_fragment_may_be_a_list_or_a_scalar(tmp_path: Path) -> None:
    _write(tmp_path / "quality.yaml", "- {rule: id_present, expression: id IS NOT NULL}\n")
    _write(tmp_path / "region.yaml", "eu-west-1\n")
    path = _contract(
        tmp_path, {"quality": {"$ref": "./quality.yaml"}, "region": {"$ref": "./region.yaml"}}
    )

    contract = load_contract(path)

    assert contract["quality"] == [{"rule": "id_present", "expression": "id IS NOT NULL"}]
    assert contract["region"] == "eu-west-1"


def test_a_yaml_and_a_json_list_fragment_compose_the_same(tmp_path: Path) -> None:
    rules = [{"rule": "id_present", "expression": "id IS NOT NULL", "severity": "error"}]
    _write(tmp_path / "q.yaml", yaml.safe_dump(rules))
    _write(tmp_path / "q.json", json.dumps(rules))
    path = _contract(tmp_path, {"y": {"$ref": "./q.yaml"}, "j": {"$ref": "./q.json"}})

    contract = load_contract(path)

    assert contract["y"] == contract["j"] == rules


def test_an_empty_yaml_fragment_is_still_an_empty_object(tmp_path: Path) -> None:
    _write(tmp_path / "empty.yaml", "")
    path = _contract(tmp_path, {"x": {"$ref": "./empty.yaml"}})

    assert load_contract(path)["x"] == {}


def test_the_root_contract_must_still_be_an_object(tmp_path: Path) -> None:
    path = _write(tmp_path / "contract.fluid.yaml", "- not\n- a contract\n")

    with pytest.raises(RuntimeError, match="YAML root must be an object/dict"):
        load_contract(path)


# ── pointers and limits ─────────────────────────────────────────────────


def test_a_pointer_decodes_rfc6901_escapes(tmp_path: Path) -> None:
    _write(tmp_path / "f.yaml", yaml.safe_dump({"a/b": {"c~d": 1}}))
    path = _contract(tmp_path, {"x": {"$ref": "./f.yaml#/a~1b/c~0d"}})

    assert load_contract(path)["x"] == 1


def test_refs_that_double_the_contract_are_capped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(contract_refs, "MAX_REF_EXPANSION_NODES", 200)
    _write(tmp_path / "l0.yaml", "[1, 2, 3, 4]\n")
    for level in range(1, 8):
        prev = f"./l{level - 1}.yaml"
        _write(tmp_path / f"l{level}.yaml", f"- {{$ref: {prev}}}\n- {{$ref: {prev}}}\n")
    path = _contract(tmp_path, {"x": {"$ref": "./l7.yaml"}})

    with pytest.raises(RefResolutionError, match="takes what refs bring into the contract past"):
        load_contract(path)


def test_the_trace_lists_every_file_read_once(tmp_path: Path) -> None:
    _write(tmp_path / "q.sql", "SELECT 1")
    _write(tmp_path / "rules.yaml", "- a\n")
    tree = {
        "a": {"$ref": "./q.sql"},
        "b": {"$ref": "./rules.yaml"},
        "c": {"$ref": "./q.sql"},
    }
    trace = contract_refs.ComposeTrace()

    _resolve_refs(tree, tmp_path, _trace=trace)

    assert trace.files == [(tmp_path / "q.sql").resolve(), (tmp_path / "rules.yaml").resolve()]


def test_the_api_lists_text_and_list_fragments_as_composed_files(tmp_path: Path) -> None:
    from fluid_build.api.contract import load_contract as api_load_contract

    _write(tmp_path / "q.sql", "SELECT 1")
    _write(tmp_path / "rules.yaml", "- a\n")
    path = _contract(
        tmp_path, {"id": "x", "sql": {"$ref": "./q.sql"}, "rules": {"$ref": "./rules.yaml"}}
    )

    loaded = api_load_contract(path)

    root = path.resolve().parent
    assert loaded.files == (path.resolve(), root / "q.sql", root / "rules.yaml")


# ── the commands (draft 21 and 24 acceptance) ──────────────────────────

_PRODUCT = """\
fluidVersion: "0.7.6"
kind: DataProduct
id: refs.text
name: Text refs
description:
  purpose: Text refs.
  usage: {usage}
domain: ops
metadata:
  layer: Silver
  owner: {{team: platform-ops, email: ops@example.com}}
builds:
  - id: transform
    pattern: embedded-logic
    engine: sql
    properties:
      sql: {sql}
exposes:
  - exposeId: out
    kind: table
    version: "1.0.0"
    binding:
      platform: local
      format: csv
      location: {{path: runtime/out/out.csv}}
    contract:
      schema:
        - {{name: id, type: integer}}
      quality: {quality}
"""

_USAGE = "# Usage\n\nNote: join on `id`.\n"
_QUALITY = [{"rule": "id_present", "expression": "id IS NOT NULL", "severity": "error"}]


def _product(tmp_path: Path, *, inline: bool) -> Path:
    if inline:
        return _write(
            tmp_path / "contract.fluid.yaml",
            _PRODUCT.format(
                usage=json.dumps(_USAGE), sql=json.dumps(_SQL), quality=json.dumps(_QUALITY)
            ),
        )
    _write(tmp_path / "docs" / "usage.md", _USAGE)
    _write(tmp_path / "sql" / "transform.sql", _SQL)
    _write(tmp_path / "fragments" / "quality.yaml", yaml.safe_dump(_QUALITY, sort_keys=False))
    return _write(
        tmp_path / "contract.fluid.yaml",
        _PRODUCT.format(
            usage="{$ref: ./docs/usage.md}",
            sql="{$ref: ./sql/transform.sql}",
            quality="{$ref: ./fragments/quality.yaml}",
        ),
    )


def test_validate_and_bundle_read_sql_and_markdown_from_their_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fluid_build.cli import main

    monkeypatch.chdir(tmp_path)
    path = _product(tmp_path, inline=False)

    assert main(["validate", str(path)]) == 0
    assert main(["bundle", str(path), "--out", str(tmp_path / "bundled.yaml")]) == 0

    bundled = yaml.safe_load((tmp_path / "bundled.yaml").read_text(encoding="utf-8"))
    assert bundled["builds"][0]["properties"]["sql"] == _SQL
    assert bundled["description"]["usage"] == _USAGE
    assert bundled["exposes"][0]["contract"]["quality"] == _QUALITY


def test_files_and_inline_text_export_the_same_odcs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fluid_build.cli import main

    outputs = []
    for inline in (True, False):
        product = tmp_path / ("inline" if inline else "files")
        monkeypatch.chdir(product.parent)
        path = _product(product, inline=inline)
        out = product / "product.odcs.yaml"
        assert main(["generate", "standard", str(path), "--format", "odcs", "--out", str(out)]) == 0
        outputs.append(out.read_bytes())

    assert outputs[0] == outputs[1]
    assert b"join on" in outputs[0]


def test_compile_contract_matches_load_contract(tmp_path: Path) -> None:
    path = _product(tmp_path, inline=False)

    assert compile_contract(path) == load_contract(path)
