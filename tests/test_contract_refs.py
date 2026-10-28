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
import os
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


# ── same-document pointers (draft 23) ──────────────────────────────────


def _doc(tmp_path: Path, text: str) -> Path:
    return _write(tmp_path / "contract.fluid.yaml", text)


def test_a_pointer_names_a_place_in_the_contract(tmp_path: Path) -> None:
    path = _doc(
        tmp_path,
        "defs: {flink: {parallelism: 2, checkpoints: 60s}}\n"
        "builds:\n"
        "  - properties:\n"
        "      parameters:\n"
        '        flinkConfiguration: {$ref: "#/defs/flink", parallelism: 8}\n',
    )

    params = load_contract(path)["builds"][0]["properties"]["parameters"]

    assert params["flinkConfiguration"] == {"parallelism": 8, "checkpoints": "60s"}


def test_a_pointer_in_a_fragment_names_the_composed_contract(tmp_path: Path) -> None:
    """Splitting a contract into files never changes what a pointer names."""
    _write(tmp_path / "fragments" / "b.yaml", 'conf: {$ref: "#/shared/flink"}\n')
    split = _doc(tmp_path, "shared: {flink: {p: 2}}\nbuilds: [{$ref: ./fragments/b.yaml}]\n")
    flat = _write(
        tmp_path / "flat" / "contract.fluid.yaml",
        'shared: {flink: {p: 2}}\nbuilds: [{conf: {$ref: "#/shared/flink"}}]\n',
    )

    assert (
        load_contract(split)
        == load_contract(flat)
        == {
            "shared": {"flink": {"p": 2}},
            "builds": [{"conf": {"p": 2}}],
        }
    )


def test_a_pointer_passes_through_file_refs_and_other_pointers(tmp_path: Path) -> None:
    _write(tmp_path / "parts.yaml", "a: {b: 1}\n")
    path = _doc(
        tmp_path,
        "parts: {$ref: ./parts.yaml}\n"
        'alias: {$ref: "#/parts"}\n'
        'x: {$ref: "#/parts/a/b"}\n'
        'y: {$ref: "#/alias/a/b"}\n',
    )

    contract = load_contract(path)

    assert (contract["x"], contract["y"], contract["alias"]) == (1, 1, {"a": {"b": 1}})


@pytest.mark.parametrize(
    "text",
    [
        'a: {$ref: "#/b"}\nb: {$ref: "#/a"}\n',
        'a: {x: {$ref: "#/a"}}\n',
        'whole: {$ref: "#"}\n',
    ],
)
def test_a_pointer_that_reaches_itself_is_a_cycle(tmp_path: Path, text: str) -> None:
    with pytest.raises(RefResolutionError, match="Circular"):
        load_contract(_doc(tmp_path, text))


def test_a_pointer_to_nothing_names_the_file_it_was_written_in(tmp_path: Path) -> None:
    _write(tmp_path / "fragments" / "b.yaml", 'conf: {$ref: "#/shared/flink"}\n')
    path = _doc(tmp_path, "shared: {}\nbuilds: [{$ref: ./fragments/b.yaml}]\n")

    with pytest.raises(RefResolutionError) as info:
        load_contract(path)

    message = str(info.value)
    assert "'/shared/flink' in the composed contract, which is not there" in message
    assert f"at JSON pointer '/conf' in {(tmp_path / 'fragments' / 'b.yaml').resolve()}" in message
    assert "name the file ('./part.yaml#/key')" in message


def test_a_same_document_ref_must_be_a_json_pointer(tmp_path: Path) -> None:
    with pytest.raises(RefResolutionError, match="is not a JSON pointer"):
        load_contract(_doc(tmp_path, 'defs: {}\nx: {$ref: "#defs"}\n'))


def test_no_ref_survives_in_open_objects(tmp_path: Path) -> None:
    _write(tmp_path / "tables" / "input.yaml", "connector: kafka\ntopic: orders\n")
    path = _doc(
        tmp_path,
        "builds:\n"
        "  - properties:\n"
        "      parameters:\n"
        "        input: {$ref: ./tables/input.yaml, scan.startup.mode: earliest}\n"
        "        shared: {p: 1}\n"
        '        copy: {$ref: "#/builds/0/properties/parameters/shared"}\n'
        "extensions: {x: {$ref: ./tables/input.yaml#/topic}}\n",
    )

    contract = load_contract(path)

    assert contract_refs.find_ref_node(contract) is None
    assert contract["builds"][0]["properties"]["parameters"]["input"] == {
        "connector": "kafka",
        "topic": "orders",
        "scan.startup.mode": "earliest",
    }
    assert contract["extensions"] == {"x": "orders"}


def test_pointers_that_double_the_contract_are_capped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(contract_refs, "MAX_REF_EXPANSION_NODES", 500)
    lines = ["l0: [1, 2, 3, 4]"]
    lines += [f'l{n}: [{{$ref: "#/l{n - 1}"}}, {{$ref: "#/l{n - 1}"}}]' for n in range(1, 12)]

    with pytest.raises(RefResolutionError, match="takes what refs bring into the contract past"):
        load_contract(_doc(tmp_path, "\n".join(lines) + "\n"))


def test_a_contract_without_refs_is_returned_as_parsed(tmp_path: Path) -> None:
    document = {"id": "x", "builds": [{"id": "b", "properties": {"sql": "SELECT 1"}}]}

    assert contract_refs.resolve_pointer_refs(document) is document


# ── one composition for every command (draft 22) ────────────────────────

_STREAM = """\
fluidVersion: "0.7.6"
kind: DataProduct
id: commerce.orders_events
name: Orders Events
description: Order events published to Kafka.
domain: commerce
metadata:
  layer: Bronze
  owner: {team: commerce-data, email: commerce@example.com}
exposes:
  - exposeId: orders_events
    kind: topic
    version: "1.0.0"
    binding:
      platform: kafka
      format: kafka_topic
      location: {topic: orders.dev.events}
    contract:
      schema:
        - {name: order_id, type: string, required: true}
  - exposeId: audit_events
    kind: topic
    version: "1.0.0"
    binding:
      platform: kafka
      format: kafka_topic
      location: {topic: audit.dev.events}
    contract:
      schema:
        - {name: event_id, type: string, required: true}
"""

_PROD_BINDING = "platform: kafka\nformat: kafka_topic\nlocation: {topic: orders.prod.events}\n"


def _stream(tmp_path: Path, overlay: str, **fragments: str) -> Path:
    for name, text in fragments.items():
        _write(tmp_path / "fragments" / name, text)
    _write(tmp_path / "overlays" / "prod.yaml", overlay)
    return _write(tmp_path / "contract.fluid.yaml", _STREAM)


def _topics(document: Dict[str, Any]) -> Dict[str, str]:
    return {e["exposeId"]: e["binding"]["location"]["topic"] for e in document["exposes"]}


_OVERLAYS = {
    # The overlay takes one field from a fragment.
    "binding": (
        "exposes:\n  - exposeId: orders_events\n"
        "    binding: {$ref: ../fragments/prod-binding.yaml}\n",
        {"prod-binding.yaml": _PROD_BINDING},
    ),
    # The overlay takes a whole entry from a fragment: matched by the id the
    # fragment names, among two exposes (a keyed 0.7.6 merge).
    "entry": (
        "exposes:\n  - {$ref: ../fragments/prod-expose.yaml}\n",
        {
            "prod-expose.yaml": (
                "exposeId: orders_events\nbinding: {location: {topic: orders.prod.events}}\n"
            )
        },
    ),
}


@pytest.mark.parametrize("shape", sorted(_OVERLAYS))
def test_every_entry_point_applies_an_overlay_composed_from_fragments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shape: str
) -> None:
    from fluid_build._contract_loader import load_contract_with_overlay
    from fluid_build.api.contract import load_contract as api_load_contract
    from fluid_build.cli import main
    from fluid_build.cli.bundle import materialize_contract
    from fluid_build.loader import load_with_overlay

    monkeypatch.chdir(tmp_path)
    overlay, fragments = _OVERLAYS[shape]
    path = _stream(tmp_path, overlay, **fragments)
    expected = {"orders_events": "orders.prod.events", "audit_events": "audit.dev.events"}
    log = __import__("logging").getLogger("test")

    documents = [
        load_with_overlay(path, "prod"),
        load_contract_with_overlay(str(path), "prod", log),
        materialize_contract(path, "prod", log)[0],
        api_load_contract(path, env="prod").contract,
    ]
    for document in documents:
        assert _topics(document) == expected
        assert contract_refs.find_ref_node(document) is None

    assert main(["validate", str(path), "--env", "prod", "--strict"]) == 0
    assert main(["bundle", str(path), "--env", "prod", "--out", str(tmp_path / "b.yaml")]) == 0
    assert _topics(yaml.safe_load((tmp_path / "b.yaml").read_text("utf-8"))) == expected
    out = tmp_path / "artifacts"
    assert main(["generate", "artifacts", str(path), "--env", "prod", "--out", str(out)]) == 0
    written = "\n".join(p.read_text("utf-8") for p in out.rglob("*") if p.is_file())
    assert "orders.prod.events" in written and "orders.dev.events" not in written
    assert "$ref" not in written


def test_validate_checks_what_an_overlay_fragment_holds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The overlay is validated, not dropped: a bad value in its fragment fails."""
    from fluid_build.cli import main

    monkeypatch.chdir(tmp_path)
    overlay, fragments = _OVERLAYS["binding"]
    bad = {"prod-binding.yaml": _PROD_BINDING.replace("kafka_topic", "not_a_format")}
    path = _stream(tmp_path, overlay, **bad)

    assert main(["validate", str(path), "--env", "prod"]) == 1


def test_a_ref_in_the_base_no_longer_drops_the_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fluid_build.api.contract import load_contract as api_load_contract
    from fluid_build.loader import load_with_overlay

    _write(tmp_path / "kafka-props.yaml", "acks: '1'\nlinger.ms: '5'\n")
    _write(
        tmp_path / "overlays" / "prod.yaml",
        "exposes:\n  - exposeId: orders_events\n"
        "    binding: {location: {topic: orders.prod.events}}\n",
    )
    path = _write(
        tmp_path / "contract.fluid.yaml",
        _STREAM.replace(
            "      location: {topic: orders.dev.events}\n",
            "      location: {topic: orders.dev.events}\n"
            "      properties: {$ref: ./kafka-props.yaml, acks: all}\n",
            1,
        ),
    )

    for document in (load_with_overlay(path, "prod"), api_load_contract(path, env="prod").contract):
        assert _topics(document)["orders_events"] == "orders.prod.events"
        assert document["exposes"][0]["binding"]["properties"] == {"acks": "all", "linger.ms": "5"}


def test_a_pointer_resolves_after_the_overlay_is_merged(tmp_path: Path) -> None:
    from fluid_build.loader import load_with_overlay

    _write(tmp_path / "overlays" / "prod.yaml", "shared: {parallelism: 8}\n")
    path = _doc(tmp_path, 'shared: {parallelism: 2}\nflink: {$ref: "#/shared"}\n')

    assert load_with_overlay(path, None)["flink"] == {"parallelism": 2}
    assert load_with_overlay(path, "prod")["flink"] == {"parallelism": 8}


def test_an_overlay_ref_is_confined_to_the_contracts_root(tmp_path: Path) -> None:
    from fluid_build.loader import load_with_overlay

    _write(tmp_path / "outside.yaml", "topic: stolen\n")
    product = tmp_path / "product"
    _write(product / "overlays" / "prod.yaml", "binding: {$ref: ../../outside.yaml}\n")
    path = _write(product / "contract.fluid.yaml", "id: x\n")

    with pytest.raises(RefConfinementError, match="escapes the ref root"):
        load_with_overlay(path, "prod")


def test_an_overlay_only_ref_gets_the_widened_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fluid_build.loader import load_with_overlay

    _write(tmp_path / "shared" / "prod-binding.yaml", "topic: orders.prod.events\n")
    product = tmp_path / "products" / "orders"
    _write(
        product / "overlays" / "prod.yaml", "binding: {$ref: ../../../shared/prod-binding.yaml}\n"
    )
    path = _write(product / "contract.fluid.yaml", "id: x\nbinding: {topic: dev}\n")
    monkeypatch.setenv("FLUID_REF_ROOT", str(tmp_path))

    assert load_with_overlay(path, "prod")["binding"] == {"topic": "orders.prod.events"}


def test_a_ref_that_cannot_be_resolved_fails_every_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    from fluid_build.cli import main

    monkeypatch.chdir(tmp_path)
    path = _stream(
        tmp_path,
        "exposes:\n  - exposeId: orders_events\n    binding: {$ref: ../fragments/missing.yaml}\n",
    )

    assert main(["validate", str(path), "--env", "prod"]) == 1
    assert main(["bundle", str(path), "--env", "prod", "--out", str(tmp_path / "b.yaml")]) == 2
    assert main(["plan", str(path), "--env", "prod", "--out", str(tmp_path / "p.json")]) != 0
    assert not (tmp_path / "b.yaml").exists() and not (tmp_path / "p.json").exists()
    err = capsys.readouterr()
    assert "$ref target not found: ../fragments/missing.yaml" in err.out + err.err
    assert "overlays/prod.yaml" in err.out + err.err


def test_generate_artifacts_reads_a_split_contract_whole_without_an_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fluid_build.cli import main

    monkeypatch.chdir(tmp_path)
    contract = yaml.safe_load(_STREAM)
    _write(tmp_path / "fragments" / "orders.yaml", yaml.safe_dump(contract["exposes"][0]))
    contract["exposes"][0] = {"$ref": "./fragments/orders.yaml"}
    path = _write(tmp_path / "contract.fluid.yaml", yaml.safe_dump(contract, sort_keys=False))

    out = tmp_path / "artifacts"
    assert main(["generate", "artifacts", str(path), "--out", str(out)]) == 0
    written = "\n".join(p.read_text("utf-8") for p in out.rglob("*") if p.is_file())
    assert "orders.dev.events" in written and "$ref" not in written


# ── where a value was written (draft 25) ───────────────────────────────

_EXPOSE_FRAGMENT = """\
# Expose: orders_events

exposeId: orders_events
kind: topic
version: "1.0.0"
binding:
  platform: kafka
  format: kafka_topic
  location: {topic: orders.dev.events}
contract:
  schema:
    - {name: order_id, type: string, required: true}
    - name: amount
      type: 42
"""


def _split_stream(tmp_path: Path) -> Path:
    contract = yaml.safe_load(_STREAM)
    contract["exposes"] = [{"$ref": "./fragments/exposes/orders_events.yaml"}]
    _write(tmp_path / "fragments" / "exposes" / "orders_events.yaml", _EXPOSE_FRAGMENT)
    return _write(tmp_path / "contract.fluid.yaml", yaml.safe_dump(contract, sort_keys=False))


def test_validate_names_the_fragment_and_line_of_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    from fluid_build.cli import main

    monkeypatch.chdir(tmp_path)
    path = _split_stream(tmp_path)

    assert main(["validate", str(path)]) == 1

    out = capsys.readouterr().out
    assert (
        "fragments/exposes/orders_events.yaml:14 (exposes[0].contract.schema[1].type): "
        "42 is not valid under any of the given schemas"
    ) in out


def test_an_error_in_the_root_contract_keeps_its_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    from fluid_build.cli import main

    monkeypatch.chdir(tmp_path)
    path = _write(tmp_path / "contract.fluid.yaml", _STREAM.replace("kind: topic", "kind: 42", 1))

    assert main(["validate", str(path)]) == 1

    out = capsys.readouterr().out
    assert "exposes[0].kind: 42 is not one of" in out
    assert "contract.fluid.yaml:" not in out


def test_an_error_the_overlay_brought_in_names_the_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    from fluid_build.cli import main

    monkeypatch.chdir(tmp_path)
    # Listed in the other order than the base: matched by exposeId.
    _write(
        tmp_path / "overlays" / "prod.yaml",
        "exposes:\n"
        "  - exposeId: audit_events\n"
        "    binding: {location: {topic: audit.prod.events}}\n"
        "  - exposeId: orders_events\n"
        "    kind: not_a_kind\n",
    )
    path = _write(tmp_path / "contract.fluid.yaml", _STREAM)

    assert main(["validate", str(path), "--env", "prod"]) == 1

    out = capsys.readouterr().out
    assert "overlays/prod.yaml:5 (exposes[0].kind): 'not_a_kind' is not one of" in out


def test_origins_follow_siblings_pointers_and_text(tmp_path: Path) -> None:
    from fluid_build.loader import compose_contract

    _write(tmp_path / "shared.yaml", "name: order_id\ntype: string\ndescription: shared\n")
    _write(tmp_path / "q.sql", "SELECT 1\n")
    path = _write(
        tmp_path / "contract.fluid.yaml",
        "column:\n"
        "  $ref: ./shared.yaml\n"
        "  description: mine\n"
        "sql: {$ref: ./q.sql}\n"
        'copy: {$ref: "#/column"}\n',
    )

    origins = compose_contract(path).origins
    root, shared, sql = (
        path.resolve(),
        (tmp_path / "shared.yaml").resolve(),
        (tmp_path / "q.sql").resolve(),
    )

    assert origins.locate(["column", "type"]) == (shared, 2)
    assert origins.locate(["column", "description"]) == (root, 3)
    assert origins.locate(["sql"]) == (sql, None)
    assert origins.locate(["copy", "name"]) == (shared, 1)
    # The object was written in shared.yaml; the siblings only patched a key.
    assert origins.locate(["copy"]) == (shared, 1)
    assert origins.locate(["column"], definition=True) == (shared, 1)
    assert origins.describe(["column", "type"]) == "shared.yaml:2"
    assert origins.describe(["column", "description"]) is None


def test_the_api_tells_where_a_value_was_written(tmp_path: Path) -> None:
    from fluid_build.api.contract import load_contract as api_load_contract
    from fluid_build.api.contract import load_contract_from_dict

    path = _split_stream(tmp_path)
    fragment = (tmp_path / "fragments" / "exposes" / "orders_events.yaml").resolve()

    loaded = api_load_contract(path)

    assert loaded.origin_of(["exposes", 0, "contract", "schema", 1, "type"]) == (fragment, 14)
    assert loaded.origin_of(["id"]) == (path.resolve(), 3)
    assert load_contract_from_dict({"id": "x"}).origin_of(["id"]) is None


def test_an_extension_error_names_the_fragment_of_its_block() -> None:
    from fluid_build.cli.validate import _in_fragment

    def locate(path: Any) -> Any:
        return "fragments/ext.yaml:3" if list(path) == ["extensions", "acme"] else None

    assert (
        _in_fragment("extensions.acme: platform: unexpected", locate)
        == "fragments/ext.yaml:3 (extensions.acme): platform: unexpected"
    )
    assert _in_fragment("extensions.other: x", locate) == "extensions.other: x"
    assert _in_fragment("extensions.acme: x", None) == "extensions.acme: x"


def test_a_missing_key_is_named_where_its_object_was_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    from fluid_build.cli import main

    monkeypatch.chdir(tmp_path)
    path = _split_stream(tmp_path)
    fragment = tmp_path / "fragments" / "exposes" / "orders_events.yaml"
    fragment.write_text(
        _EXPOSE_FRAGMENT.replace("kind: topic\n", "").replace("type: 42", "type: number"), "utf-8"
    )
    # The overlay merges a key into the same expose: it did not write the object.
    _write(
        tmp_path / "overlays" / "prod.yaml",
        "exposes:\n  - exposeId: orders_events\n    version: 1.0.1\n",
    )

    assert main(["validate", str(path), "--env", "prod"]) == 1

    out = capsys.readouterr().out
    assert (
        "fragments/exposes/orders_events.yaml:3 (exposes[0]): 'kind' is a required property"
    ) in out


def test_an_unexpected_key_is_named_where_it_was_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    from fluid_build.cli import main

    monkeypatch.chdir(tmp_path)
    path = _split_stream(tmp_path)
    fragment = tmp_path / "fragments" / "exposes" / "orders_events.yaml"
    fragment.write_text(_EXPOSE_FRAGMENT.replace("type: 42", "type: number"), "utf-8")
    _write(
        tmp_path / "overlays" / "prod.yaml",
        "exposes:\n  - exposeId: orders_events\n    binding:\n      bogus: 1\n",
    )

    assert main(["validate", str(path), "--env", "prod"]) == 1

    out = capsys.readouterr().out
    assert "overlays/prod.yaml:4 (exposes[0].binding): Additional properties" in out


@pytest.mark.parametrize("env", [None, "prod"])
def test_generate_artifacts_writes_the_same_files_for_a_split_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env: Any
) -> None:
    from fluid_build.cli import main

    outputs = []
    for layout in ("flat", "split"):
        root = tmp_path / layout
        _write(
            root / "overlays" / "prod.yaml",
            "exposes:\n  - exposeId: orders_events\n"
            "    binding: {location: {topic: orders.prod.events}}\n",
        )
        contract = yaml.safe_load(_STREAM)
        if layout == "split":
            for expose in contract["exposes"]:
                _write(
                    root / "fragments" / f"{expose['exposeId']}.yaml",
                    yaml.safe_dump(expose, sort_keys=False),
                )
            contract["exposes"] = [
                {"$ref": f"./fragments/{e['exposeId']}.yaml"} for e in contract["exposes"]
            ]
        path = _write(root / "contract.fluid.yaml", yaml.safe_dump(contract, sort_keys=False))
        monkeypatch.chdir(root)
        argv = ["generate", "artifacts", str(path), "--out", str(root / "out")]
        assert main(argv + (["--env", env] if env else [])) == 0
        files = sorted(p for p in (root / "out").rglob("*") if p.is_file())
        outputs.append(
            {
                p.relative_to(root / "out").as_posix(): _undated(p.read_text("utf-8"))
                for p in files
                if p.name != "MANIFEST.json"
            }
        )

    assert outputs[0] == outputs[1]


def _undated(text: str) -> str:
    """ODPS v4.1 stamps today's date; the comparison is about content."""
    import re

    return re.sub(r'"(created|updated)": "[^"]*"', r'"\1": "-"', text)


def test_generate_ci_reads_a_split_contract_whole(tmp_path: Path) -> None:
    from fluid_build.cli import generate_ci

    contract = yaml.safe_load(_STREAM)
    contract["builds"] = [{"$ref": "./fragments/b.yaml"}]
    _write(
        tmp_path / "fragments" / "b.yaml",
        "id: ingest\npattern: hybrid-reference\nengine: dbt\n",
    )
    path = _write(tmp_path / "contract.fluid.yaml", yaml.safe_dump(contract, sort_keys=False))

    assert generate_ci._contract_is_reference_only(str(path)) is True
    assert generate_ci._extract_engine_context(str(path))["engine"] == "dbt"


# ── what a ref may not read, and how much it may bring ─────────────────


def test_a_directory_is_not_a_target(tmp_path: Path) -> None:
    (tmp_path / "parts").mkdir()
    path = _contract(tmp_path, {"x": {"$ref": "./parts"}})

    with pytest.raises(RefResolutionError, match="which is not a regular file"):
        load_contract(path)


@pytest.mark.skipif(not hasattr(__import__("os"), "mkfifo"), reason="no FIFOs here")
def test_a_fifo_is_refused_before_it_is_read(tmp_path: Path) -> None:
    import os

    os.mkfifo(tmp_path / "pipe.sql")
    path = _contract(tmp_path, {"sql": {"$ref": "./pipe.sql"}})

    with pytest.raises(RefResolutionError, match="which is not a regular file"):
        load_contract(path)


@pytest.mark.parametrize("target", ["/proc/self/environ", "/sys/kernel/x", "/dev/zero"])
def test_kernel_trees_stay_blocked_with_any_root(target: str) -> None:
    with pytest.raises(RefResolutionError, match="blocked system path"):
        contract_refs._refuse_system_path(Path(target), target)


def test_a_file_without_a_known_suffix_must_still_hold_an_object(tmp_path: Path) -> None:
    """A dotfile inside the root reads as a YAML string; a ref must not hand it out."""
    _write(tmp_path / ".env", "API_TOKEN=s3cr3t\n")
    _write(tmp_path / "schema.avsc", '[{"type": "record", "name": "A"}]')
    leaky = _contract(tmp_path, {"x": {"$ref": "./.env"}})

    with pytest.raises(RefResolutionError, match="YAML root must be an object/dict"):
        load_contract(leaky)
    fine = _contract(tmp_path, {"x": {"$ref": "./schema.avsc"}})
    assert load_contract(fine)["x"] == [{"type": "record", "name": "A"}]


def test_a_fragment_that_contains_itself_is_refused(tmp_path: Path) -> None:
    _write(tmp_path / "loop.yaml", "a: &a\n  b: *a\n")
    path = _contract(tmp_path, {"x": {"$ref": "./loop.yaml"}})

    with pytest.raises(ValueError, match="contains itself through a YAML alias"):
        load_contract(path)


def test_a_root_that_contains_itself_fails_fast(tmp_path: Path) -> None:
    """The ref-root gate walks the root before composition; it must end on a cycle."""
    path = tmp_path / "contract.fluid.yaml"
    _write(path, "id: x\nextensions: &e\n  self: *e\n  part: {$ref: ./part.yaml}\n")
    _write(tmp_path / "part.yaml", "a: 1\n")

    with pytest.raises(ValueError, match="contains itself through a YAML alias"):
        load_contract(path)
    assert contract_refs.find_ref_node(yaml.safe_load(path.read_text("utf-8"))) is not None


def test_external_refs_are_listed_once_in_a_document_that_contains_itself() -> None:
    from fluid_build.util.ref_confinement import iter_external_refs

    loop: Dict[str, Any] = {"part": {"$ref": "./part.yaml"}}
    loop["self"] = loop
    assert list(iter_external_refs({"x": loop})) == [("/x/part", "./part.yaml")]


def test_a_file_reached_by_two_names_is_parsed_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The parse cache is keyed by the file, not the name: hard links (and letter case on a
    case-insensitive volume) cannot make one 5 MiB fragment parse once per ref."""
    _write(tmp_path / "part.yaml", "a: 1\n")
    os.link(tmp_path / "part.yaml", tmp_path / "link.yaml")
    parsed = []
    real = contract_refs._parse
    monkeypatch.setattr(
        contract_refs, "_parse", lambda target, *a: parsed.append(target) or real(target, *a)
    )

    path = _contract(tmp_path, {"x": {"$ref": "./part.yaml"}, "y": {"$ref": "./link.yaml"}})
    assert load_contract(path)["y"] == {"a": 1}
    assert len(parsed) == 1


def test_a_denied_directory_is_denied_in_any_letter_case() -> None:
    """On a case-insensitive volume ``/USR`` is ``/usr`` and keeps its letters through
    resolve(): the deny check compares the directories themselves, not their spelling."""
    usr = Path("/usr")
    if not usr.is_dir() or not Path("/USR").exists():
        pytest.skip("needs /usr on a case-insensitive volume")
    assert contract_refs._is_system_path(Path("/USR/bin").resolve())


def test_the_cap_counts_keys_and_alias_copies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(contract_refs, "MAX_REF_EXPANSION_CHARS", 1_000)
    _write(tmp_path / "keys.yaml", yaml.safe_dump({"k" * 2_000: 1}))
    with pytest.raises(RefResolutionError, match="takes what refs bring"):
        load_contract(_contract(tmp_path, {"x": {"$ref": "./keys.yaml"}}))

    monkeypatch.setattr(contract_refs, "MAX_REF_EXPANSION_NODES", 300)
    _write(
        tmp_path / "aliases.yaml",
        "base: &b [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]\n"
        "copies: [*b, *b, *b, *b, *b, *b, *b, *b, *b, *b, *b, *b, *b, *b, *b, *b, *b, *b, *b, *b,"
        " *b, *b, *b, *b, *b, *b, *b, *b, *b, *b]\n",
    )
    with pytest.raises(RefResolutionError, match="takes what refs bring"):
        load_contract(_contract(tmp_path, {"y": {"$ref": "./aliases.yaml"}}))


def test_a_list_index_is_ascii_digits(tmp_path: Path) -> None:
    _write(tmp_path / "l.yaml", "items: [a, b, c]\n")
    path = _contract(tmp_path, {"x": {"$ref": "./l.yaml#/items/²"}})

    with pytest.raises(RefResolutionError, match="is not a valid list index"):
        load_contract(path)


def test_the_depth_error_names_the_ref_that_went_too_deep(tmp_path: Path) -> None:
    for n in range(contract_refs.MAX_REF_DEPTH + 2):
        _write(tmp_path / f"l{n}.yaml", f"next: {{$ref: ./l{n + 1}.yaml}}\n")
    _write(tmp_path / f"l{contract_refs.MAX_REF_DEPTH + 2}.yaml", "end: true\n")
    path = _contract(tmp_path, {"x": {"$ref": "./l0.yaml"}})

    with pytest.raises(RefResolutionError, match="depth exceeded") as info:
        load_contract(path)
    assert "at JSON pointer '/next' in" in str(info.value)


def test_an_overlay_must_compose_to_an_object(tmp_path: Path) -> None:
    from fluid_build.loader import load_with_overlay

    _write(tmp_path / "rules.yaml", "- a\n")
    _write(tmp_path / "overlays" / "prod.yaml", "$ref: ../rules.yaml\n")
    path = _write(tmp_path / "contract.fluid.yaml", "id: x\n")

    with pytest.raises(ValueError, match="Overlay root must be an object/dict"):
        load_with_overlay(path, "prod")
