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

"""``$ref`` fragments, slice 1: keys written beside ``$ref`` (scenario area B).

Spec item 3 of the fragment-composition catalog: keys beside ``$ref`` are applied
to a copy of the target as an RFC 7386 merge patch (objects merge recursively,
lists and scalars replace, ``null`` removes the key), a target that is not an
object takes no siblings, and a ref inside a sibling resolves relative to the file
that holds the sibling and is confined to the ref root.

``RFR`` tests call ``contract_refs.resolve_file_refs`` directly. ``LC`` tests go
through ``loader.load_contract``. B-14 (a pointer in a sibling), B-17 and B-18
belong to other slices.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import random
from pathlib import Path
from typing import Any, Dict, Iterator, List, Tuple

import pytest
import yaml

import fluid_build
from fluid_build import contract_refs, loader
from fluid_build.contract_refs import resolve_file_refs
from fluid_build.loader import RefConfinementError, RefResolutionError

# ── helpers ─────────────────────────────────────────────────────────────


def _clear_warn_once_registries() -> None:
    for name, value in vars(loader).items():
        if name.startswith("_NOTED_") and isinstance(value, set):
            value.clear()


@pytest.fixture(autouse=True)
def _hermetic_ref_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """No ambient ref root, no workspace file and no warn-once state from another test."""
    monkeypatch.delenv("FLUID_REF_ROOT", raising=False)
    monkeypatch.setenv("FLUID_IGNORE_WORKSPACE_REF_ROOT", "1")
    _clear_warn_once_registries()
    yield
    _clear_warn_once_registries()


def _write(path: Path, text: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


def _min_doc() -> Dict[str, Any]:
    """MIN: the smallest 0.7.6 contract that validates, with one build and one expose."""
    return {
        "fluidVersion": "0.7.6",
        "kind": "DataProduct",
        "id": "refs.text",
        "name": "Min product",
        "description": {"purpose": "Minimal.", "usage": "Use it."},
        "domain": "ops",
        "metadata": {
            "layer": "Silver",
            "owner": {"team": "platform-ops", "email": "ops@example.com"},
        },
        "builds": [
            {
                "id": "transform",
                "pattern": "embedded-logic",
                "engine": "sql",
                "properties": {"sql": "SELECT 1 AS id"},
            }
        ],
        "exposes": [
            {
                "exposeId": "out",
                "kind": "table",
                "version": "1.0.0",
                "binding": {
                    "platform": "local",
                    "format": "csv",
                    "location": {"path": "runtime/out/out.csv"},
                },
                "contract": {
                    "schema": [{"name": "id", "type": "integer"}],
                    "quality": [
                        {"rule": "id_present", "expression": "id IS NOT NULL", "severity": "error"}
                    ],
                },
            }
        ],
    }


def _write_contract(root: Path, doc: Dict[str, Any], name: str = "contract.fluid.yaml") -> Path:
    return _write(root / name, yaml.safe_dump(doc, sort_keys=False, allow_unicode=True))


def _rfr(doc: Any, root: Path) -> Any:
    """``RFR``: the file-ref pass alone, as the loader calls it for a contract in ``root``."""
    return resolve_file_refs(doc, root, ref_root=root, source=root / "contract.fluid.yaml")


def _cli(capsys: pytest.CaptureFixture[str], *argv: str) -> Tuple[int, str]:
    from fluid_build.cli import main

    capsys.readouterr()
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out + captured.err


def _spy_on_reads(monkeypatch: pytest.MonkeyPatch) -> List[str]:
    """Every path read through ``pathlib`` while the spy is installed."""
    seen: List[str] = []
    for name in ("read_text", "read_bytes", "open"):
        real = getattr(Path, name)

        def spy(self: Path, *args: Any, _real: Any = real, **kwargs: Any) -> Any:
            seen.append(str(self))
            return _real(self, *args, **kwargs)

        monkeypatch.setattr(Path, name, spy)
    return seen


def test_b00_import_origin_is_this_checkout() -> None:
    """The tests run against the checkout they sit in, never a stale installed copy."""
    checkout = Path(__file__).resolve().parents[2]
    assert Path(fluid_build.__file__).resolve().is_relative_to(checkout)


# ── B. siblings and merge patch (RFC 7386) ──────────────────────────────

_BINDING = "platform: local\nformat: csv\nlocation: {path: a, region: eu}\n"


def test_b01_a_sibling_replaces_one_field_of_a_pointed_column(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    _write(
        tmp_path / "cols.yaml",
        "order_id: {name: order_id, type: string, description: A}\n",
    )
    doc = _min_doc()
    doc["exposes"][0]["contract"]["schema"] = [
        {"$ref": "./cols.yaml#/order_id", "description": "B"}
    ]
    contract = _write_contract(tmp_path, doc)

    column = loader.load_contract(contract)["exposes"][0]["contract"]["schema"][0]

    assert column == {"name": "order_id", "type": "string", "description": "B"}
    assert list(column) == ["name", "type", "description"]
    code, output = _cli(capsys, "validate", str(contract))
    assert code == 0, output


def test_b02_a_column_ref_that_leaves_the_product_needs_the_ref_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Draft 23: 0.24.0 left this sibling-form ref a literal and failed on three schema
    errors; now it is an escape without the widened root and a merged column with it."""
    workspace = tmp_path / "ws"
    _write(
        workspace / "shared" / "columns.yaml",
        "order_id: {name: order_id, type: string, required: true, description: Shared key}\n",
    )
    product = workspace / "products" / "orders"
    doc = _min_doc()
    doc["exposes"][0]["contract"]["schema"] = [
        {
            "$ref": "../../shared/columns.yaml#/order_id",
            "description": "The order this row belongs to",
        }
    ]
    contract = _write_contract(product, doc)
    monkeypatch.chdir(product)

    code, output = _cli(capsys, "validate", str(contract))
    assert code == 1
    assert "escapes the ref root" in output and "../../shared/columns.yaml#/order_id" in output
    assert "is not valid under any of the given schemas" not in output
    with pytest.raises(RefConfinementError):
        loader.load_contract(contract)

    monkeypatch.setenv("FLUID_REF_ROOT", str(workspace))
    code, output = _cli(capsys, "validate", str(contract))
    assert code == 0, output
    assert loader.load_contract(contract)["exposes"][0]["contract"]["schema"] == [
        {
            "name": "order_id",
            "type": "string",
            "required": True,
            "description": "The order this row belongs to",
        }
    ]


@pytest.mark.parametrize("kind", ["yaml", "json"])
def test_b03_a_sibling_object_merges_into_the_target_object(tmp_path: Path, kind: str) -> None:
    target = {"platform": "local", "format": "csv", "location": {"path": "a", "region": "eu"}}
    _write(tmp_path / f"binding.{kind}", json.dumps(target))
    sibling = {"path": "b"}

    got = _rfr({"b": {"$ref": f"./binding.{kind}", "location": sibling}}, tmp_path)["b"]

    assert got == {"platform": "local", "format": "csv", "location": {"path": "b", "region": "eu"}}
    assert list(got["location"]) == ["path", "region"]


def test_b04_a_sibling_list_replaces_the_target_list_whole(tmp_path: Path) -> None:
    """A positional merge (``loader._deep_merge``) would keep columns two and three, and
    would carry the first column's ``description`` into the new first column."""
    _write(
        tmp_path / "t.yaml",
        "kind: table\n"
        "contract:\n"
        "  schema:\n"
        "    - {name: a, type: string, description: first}\n"
        "    - {name: b, type: string, description: second}\n"
        "    - {name: c, type: string, description: third}\n"
        "  quality: [q1, q2]\n",
    )

    got = _rfr(
        {"x": {"$ref": "./t.yaml", "contract": {"schema": [{"name": "x", "type": "integer"}]}}},
        tmp_path,
    )["x"]

    assert got["contract"]["schema"] == [{"name": "x", "type": "integer"}]
    assert got["contract"]["quality"] == ["q1", "q2"]
    assert got["kind"] == "table"


@pytest.mark.parametrize("kind", ["yaml", "json"])
def test_b05_a_null_sibling_removes_the_key(tmp_path: Path, kind: str) -> None:
    _write(tmp_path / f"d.{kind}", json.dumps({"name": "n", "description": "d"}))

    got = _rfr({"x": {"$ref": f"./d.{kind}", "description": None}}, tmp_path)["x"]

    assert got == {"name": "n"}
    assert "description" not in got


def test_b05_a_null_sibling_reaches_loaded_contracts_too(tmp_path: Path) -> None:
    _write(tmp_path / "d.yaml", "name: n\ndescription: d\n")
    contract = _write_contract(tmp_path, {"x": {"$ref": "./d.yaml", "description": None}})

    assert loader.load_contract(contract) == {"x": {"name": "n"}}


def test_b06_null_for_an_absent_key_adds_nothing_and_a_nested_null_removes_one_key(
    tmp_path: Path,
) -> None:
    _write(tmp_path / "d.yaml", "name: n\ndescription: d\n")
    _write(tmp_path / "binding.yaml", _BINDING)

    absent = _rfr({"x": {"$ref": "./d.yaml", "nokey": None}}, tmp_path)["x"]
    nested = _rfr({"x": {"$ref": "./binding.yaml", "location": {"region": None}}}, tmp_path)["x"]

    assert absent == {"name": "n", "description": "d"}
    assert nested == {"platform": "local", "format": "csv", "location": {"path": "a"}}


def test_b07_an_object_sibling_over_a_scalar_replaces_it_and_drops_its_nulls(
    tmp_path: Path,
) -> None:
    """RFC 7386: a patch object over a non-object target starts from ``{}``."""
    _write(tmp_path / "v.yaml", '{version: "1.0.0", other: 1}\n')

    got = _rfr({"x": {"$ref": "./v.yaml", "version": {"major": 1, "minor": None}}}, tmp_path)["x"]

    assert got == {"version": {"major": 1}, "other": 1}


@pytest.mark.parametrize("kind", ["yaml", "json"])
def test_b08_siblings_on_a_list_target_are_refused_naming_them(tmp_path: Path, kind: str) -> None:
    _write(tmp_path / f"rules.{kind}", json.dumps(["a", "b"]))

    with pytest.raises(RefResolutionError) as info:
        _rfr({"x": {"$ref": f"./rules.{kind}", "note": 1, "other": 2}}, tmp_path)

    message = str(info.value)
    assert "'note'" in message and "'other'" in message
    assert "the target is a list, not an object" in message
    assert f"./rules.{kind}" in message


@pytest.mark.parametrize(
    ("name", "text", "kind"),
    [
        ("s.yaml", "hello\n", "text"),
        ("i.yaml", "5\n", "int"),
        ("b.yaml", "true\n", "bool"),
        ("f.yaml", "1.5\n", "float"),
        ("n.json", "null", "null"),
        ("s.json", '"a string"', "text"),
        ("i.json", "7", "int"),
        ("q.sql", "SELECT 1", "text"),
    ],
    ids=[
        "yaml-str",
        "yaml-int",
        "yaml-bool",
        "yaml-float",
        "json-null",
        "json-str",
        "json-int",
        "sql",
    ],
)
def test_b09_siblings_on_a_scalar_target_are_refused(
    tmp_path: Path, name: str, text: str, kind: str
) -> None:
    _write(tmp_path / name, text)

    with pytest.raises(RefResolutionError) as info:
        _rfr({"x": {"$ref": f"./{name}", "extra": 1}}, tmp_path)

    message = str(info.value)
    assert "'extra'" in message and "not an object" in message
    assert kind in message
    assert f"./{name}" in message


def test_b10_siblings_on_an_empty_yaml_target_are_the_siblings_without_their_nulls(
    tmp_path: Path,
) -> None:
    _write(tmp_path / "empty.yaml", "")

    got = _rfr(
        {"x": {"$ref": "./empty.yaml", "a": 1, "b": None, "c": {"d": None, "e": 2}}}, tmp_path
    )["x"]

    assert got == {"a": 1, "c": {"e": 2}}


def test_b11_two_refs_to_one_file_each_get_their_own_siblings(tmp_path: Path) -> None:
    template = (
        "exposeId: template\n"
        "kind: table\n"
        'version: "1.0.0"\n'
        "binding: {platform: local, format: csv, location: {path: p.csv}}\n"
    )
    _write(tmp_path / "e.yaml", template)
    contract = _write_contract(
        tmp_path,
        {"exposes": [{"$ref": "./e.yaml", "exposeId": "a"}, {"$ref": "./e.yaml", "exposeId": "b"}]},
    )

    first, second = loader.load_contract(contract)["exposes"]

    assert (first["exposeId"], second["exposeId"]) == ("a", "b")
    assert {k: v for k, v in first.items() if k != "exposeId"} == {
        k: v for k, v in second.items() if k != "exposeId"
    }
    assert first["binding"] is not second["binding"]
    assert (tmp_path / "e.yaml").read_text(encoding="utf-8") == template


def test_b11_composition_neither_changes_nor_shares_its_input(tmp_path: Path) -> None:
    _write(tmp_path / "t.yaml", "a: 1\n")
    doc = {
        "x": {"$ref": "./t.yaml", "k": [1, 2], "o": {"z": [3]}},
        "y": {"$ref": "./t.yaml", "k": [1, 2]},
    }
    before = copy.deepcopy(doc)

    got = _rfr(doc, tmp_path)

    assert doc == before
    assert got["x"]["k"] is not doc["x"]["k"]
    assert got["x"]["o"]["z"] is not doc["x"]["o"]["z"]
    assert got["x"]["k"] is not got["y"]["k"]
    got["x"]["k"].append(3)
    assert got["y"]["k"] == [1, 2] and doc["x"]["k"] == [1, 2]


def test_b12_a_sibling_ref_is_relative_to_the_file_that_holds_it(tmp_path: Path) -> None:
    _write(tmp_path / "frag" / "t.yaml", "a: 1\n")
    _write(tmp_path / "frag" / "x.yaml", "from: frag/x\n")
    _write(tmp_path / "x.yaml", "from: root/x\n")
    _write(tmp_path / "frag" / "f.yaml", "$ref: ../t0.yaml\nextra: {$ref: ./x.yaml}\n")
    _write(tmp_path / "t0.yaml", "base: 0\n")
    contract = _write_contract(
        tmp_path,
        {
            "c": {"$ref": "./frag/t.yaml", "extra": {"$ref": "./x.yaml"}},
            "d": {"$ref": "./frag/f.yaml"},
        },
    )

    got = loader.load_contract(contract)

    assert got["c"] == {"a": 1, "extra": {"from": "root/x"}}
    assert got["d"] == {"base": 0, "extra": {"from": "frag/x"}}


def test_b13_an_escape_in_a_sibling_is_refused_before_the_file_is_opened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    product = tmp_path / "p"
    _write(product / "t.yaml", "a: 1\n")
    outside = _write(tmp_path / "out.yaml", "secret: SECRET-B13\n")
    contract = _write_contract(
        product, {"c": {"$ref": "./t.yaml", "extra": {"$ref": "../out.yaml"}}}
    )
    reads = _spy_on_reads(monkeypatch)

    with pytest.raises(RefConfinementError) as info:
        loader.load_contract(contract)

    error = info.value
    assert error.ref == "../out.yaml"
    assert error.pointer == "/c/extra"
    assert "escapes the ref root" in str(error)
    assert "SECRET-B13" not in str(error)
    assert str(outside) not in reads and str(outside.resolve()) not in reads
    assert not any(Path(read).name == "out.yaml" for read in reads)


@pytest.mark.skipif(
    not hasattr(os, "geteuid") or os.geteuid() == 0, reason="permission bits do not bind root"
)
def test_b13_an_unreadable_file_outside_the_root_still_reads_as_an_escape(
    tmp_path: Path,
) -> None:
    product = tmp_path / "p"
    _write(product / "t.yaml", "a: 1\n")
    outside = _write(tmp_path / "out.yaml", "secret: SECRET-B13\n")
    contract = _write_contract(
        product, {"c": {"$ref": "./t.yaml", "extra": {"$ref": "../out.yaml"}}}
    )
    outside.chmod(0)
    try:
        with pytest.raises(RefConfinementError, match="escapes the ref root"):
            loader.load_contract(contract)
    finally:
        outside.chmod(0o600)


def test_b15_the_targets_own_refs_resolve_before_the_sibling_is_merged(tmp_path: Path) -> None:
    _write(tmp_path / "t.yaml", "a: {$ref: ./u.yaml}\nb: 1\n")
    _write(tmp_path / "u.yaml", "j: 1\nk: 1\n")
    contract = _write_contract(tmp_path, {"c": {"$ref": "./t.yaml", "a": {"k": 2}}})

    assert loader.load_contract(contract) == {"c": {"a": {"j": 1, "k": 2}, "b": 1}}


def test_b16_one_build_fragment_can_serve_two_builds_by_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Siblings are a merge patch over a copy, never ``_deep_merge``'s keyed overlay merge."""
    monkeypatch.chdir(tmp_path)
    _write(
        tmp_path / "b.yaml",
        "id: template\npattern: embedded-logic\nengine: sql\nproperties: {sql: SELECT 1}\n",
    )
    doc = _min_doc()
    doc["builds"] = [{"$ref": "./b.yaml", "id": "b2"}, {"$ref": "./b.yaml", "id": "b3"}]
    contract = _write_contract(tmp_path, doc)

    with caplog.at_level(logging.DEBUG):
        composed = loader.load_contract(contract)

    assert [build["id"] for build in composed["builds"]] == ["b2", "b3"]
    assert not [r for r in caplog.records if "overlay" in r.getMessage().lower()]
    assert not [r for r in caplog.records if str(getattr(r, "event", "")).startswith("overlay")]
    code, output = _cli(capsys, "validate", str(contract))
    assert code == 0, output
    assert "overlay" not in output.lower()


def test_b19_new_keys_follow_the_targets_keys_and_bundles_repeat_byte_for_byte(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    _write(tmp_path / "o.yaml", "b: 1\na: 2\nc: 3\n")
    sibling = {"$ref": "./o.yaml", "d": 4, "a": 9}

    got = _rfr({"x": sibling}, tmp_path)["x"]
    assert list(got) == ["b", "a", "c", "d"]
    assert got["a"] == 9

    doc = _min_doc()
    doc["extensions"] = {"x": sibling}
    contract = _write_contract(tmp_path, doc)
    outputs = []
    for run in ("first", "second"):
        out = tmp_path / f"{run}.yaml"
        code, output = _cli(capsys, "bundle", str(contract), "--out", str(out))
        assert code == 0, output
        outputs.append(out.read_bytes())
    assert outputs[0] == outputs[1]
    assert list(yaml.safe_load(outputs[0])["extensions"]["x"]) == ["b", "a", "c", "d"]


def test_b20_dollar_prefixed_siblings_are_ordinary_keys(tmp_path: Path) -> None:
    _write(tmp_path / "t.yaml", "k: 1\n")

    got = _rfr({"x": {"$ref": "./t.yaml", "$id": "i", "$schema": "s", "$comment": "c"}}, tmp_path)[
        "x"
    ]

    assert got == {"k": 1, "$id": "i", "$schema": "s", "$comment": "c"}


def test_b21_a_sibling_may_name_the_file_its_ref_names(tmp_path: Path) -> None:
    """Siblings resolve outside the target's ancestry: no false cycle."""
    _write(tmp_path / "t.yaml", "a: 1\n")

    got = _rfr({"x": {"$ref": "./t.yaml", "extra": {"$ref": "./t.yaml"}}}, tmp_path)["x"]

    assert got == {"a": 1, "extra": {"a": 1}}


@pytest.mark.parametrize("value", [42, 1.5, True, None, ["a"], {"a": 1}], ids=lambda v: repr(v))
def test_b22_a_ref_value_that_is_not_a_string_is_refused_even_with_siblings(
    tmp_path: Path, value: Any
) -> None:
    with pytest.raises(RefResolutionError, match="must be a string") as info:
        _rfr({"x": {"$ref": value, "x": 1}}, tmp_path)

    message = str(info.value)
    assert f"got {type(value).__name__}" in message
    assert "at JSON pointer '/x'" in message and "contract.fluid.yaml" in message


@pytest.mark.parametrize(
    ("ref", "reason"),
    [
        ("./nope.yaml", "not found"),
        ("/etc/hosts", "must be a relative path"),
        ("https://e.x/a.yaml", "is a URL"),
    ],
    ids=["missing", "absolute", "url"],
)
def test_b23_sibling_form_refs_that_0_24_never_read_are_now_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    ref: str,
    reason: str,
) -> None:
    """Pinned compatibility break: 0.24.0 passed these by never reading a sibling-form ref."""
    monkeypatch.chdir(tmp_path)
    doc = _min_doc()
    doc["builds"][0]["properties"]["parameters"] = {"p": {"$ref": ref, "x": 1}}
    contract = _write_contract(tmp_path, doc)

    code, output = _cli(capsys, "validate", str(contract))

    assert code == 1
    assert reason in output and ref in output
    assert "/builds/0/properties/parameters/p" in output


def test_b24_a_sibling_empty_object_replaces_a_scalar_and_a_list_replaces_an_object(
    tmp_path: Path,
) -> None:
    _write(tmp_path / "t.yaml", "k: 1\nsub: {a: 1}\nn: 5\n")

    got = _rfr({"x": {"$ref": "./t.yaml", "n": {}, "sub": [1, 2]}}, tmp_path)["x"]

    assert got == {"k": 1, "sub": [1, 2], "n": {}}


@pytest.mark.parametrize(
    ("original", "patch", "result"),
    [
        ({"a": "b"}, {"a": "c"}, {"a": "c"}),
        ({"a": "b"}, {"b": "c"}, {"a": "b", "b": "c"}),
        ({"a": "b"}, {"a": None}, {}),
        ({"a": "b", "b": "c"}, {"a": None}, {"b": "c"}),
        ({"a": ["b"]}, {"a": "c"}, {"a": "c"}),
        ({"a": "c"}, {"a": ["b"]}, {"a": ["b"]}),
        ({"a": {"b": "c"}}, {"a": {"b": "d", "c": None}}, {"a": {"b": "d"}}),
        ({"a": [{"b": "c"}]}, {"a": [1]}, {"a": [1]}),
        (["a", "b"], ["c", "d"], ["c", "d"]),
        ({"a": "b"}, ["c"], ["c"]),
        ({"a": "foo"}, None, None),
        ({"a": "foo"}, "bar", "bar"),
        ({"e": None}, {"a": 1}, {"e": None, "a": 1}),
        ([1, 2], {"a": "b", "c": None}, {"a": "b"}),
        ({}, {"a": {"bb": {"ccc": None}}}, {"a": {"bb": {}}}),
    ],
    ids=[f"rfc7386-a{n}" for n in range(1, 16)],
)
def test_b24_rfc7386_appendix_a_vectors(original: Any, patch: Any, result: Any) -> None:
    before = copy.deepcopy(original)

    assert contract_refs.merge_patch(original, patch) == result
    assert original == before


def test_b24_the_rfc7386_vectors_hold_for_siblings_over_a_file(tmp_path: Path) -> None:
    """The same rules through ``$ref`` siblings: a null in the target is kept, and an object
    patch over an absent parent still creates the parent."""
    _write(tmp_path / "e.yaml", "e: null\nkeep: [1]\n")

    got = _rfr({"x": {"$ref": "./e.yaml", "a": 1, "deep": {"bb": {"ccc": None}}}}, tmp_path)["x"]

    assert got == {"e": None, "keep": [1], "a": 1, "deep": {"bb": {}}}


def test_b25_nested_siblings_apply_innermost_first(tmp_path: Path) -> None:
    _write(tmp_path / "u.yaml", "j: 1\nk: 1\n")
    _write(tmp_path / "t.yaml", "$ref: ./u.yaml\na: 1\nk: 2\n")
    contract = _write_contract(tmp_path, {"x": {"$ref": "./t.yaml", "b": 2, "k": 3}})

    got = loader.load_contract(contract)["x"]

    assert got == {"j": 1, "k": 3, "a": 1, "b": 2}
    assert list(got) == ["j", "k", "a", "b"]


def test_b26_siblings_on_a_pointer_to_a_list_are_refused(tmp_path: Path) -> None:
    _write(tmp_path / "cols.yaml", "list: [1, 2]\nobj: {a: 1}\n")

    with pytest.raises(RefResolutionError) as info:
        _rfr({"x": {"$ref": "./cols.yaml#/list", "x": 1}}, tmp_path)

    message = str(info.value)
    assert "the target is a list, not an object" in message
    assert "./cols.yaml#/list" in message and "'x'" in message
    assert _rfr({"x": {"$ref": "./cols.yaml#/obj", "x": 1}}, tmp_path) == {"x": {"a": 1, "x": 1}}


# ── an independent model of targets, pointers and RFC 7386 siblings ─────

_MODEL_FILES = {
    "obj.yaml": "a: 1\nb: {c: [1, 2], d: x}\nl: [{p: 1}, {p: 2}]\n",
    "list.yaml": "- {p: 1}\n- {p: 2}\n",
    "scalar.yaml": "42\n",
    "text.sql": "SELECT 1\r\nFROM t\n",
    "nested.yaml": "inner: {$ref: ./obj.yaml}\nt: {$ref: ./text.sql}\nl: [{$ref: ./list.yaml}]\nkeep: [1]\n",
    "sib.yaml": "$ref: ./obj.yaml\nb: {d: y, e: null}\nextra: {$ref: ./scalar.yaml}\n",
    "o.json": '{"a": null, "b": [1, {"c": 2}], "z": {"y": {"x": 1}}}',
    "empty.yaml": "",
    "deep.yaml": "x: {$ref: ./nested.yaml#/inner}\ny: {$ref: ./sib.yaml, a: 5}\nz: {$ref: ./o.json#/z}\n",
    "sub/o.yaml": "from: sub\nn: {$ref: ../obj.yaml#/b}\nm: {$ref: ./o2.yaml}\n",
    "sub/o2.yaml": "sub2: 1\nback: {$ref: ../scalar.yaml}\n",
}

#: (file, pointer, kind of what it names): every ref the model draws is resolvable.
_MODEL_TARGETS = [
    ("obj.yaml", "", "object"),
    ("obj.yaml", "#/b", "object"),
    ("obj.yaml", "#/b/c", "list"),
    ("obj.yaml", "#/a", "scalar"),
    ("obj.yaml", "#/l/0", "object"),
    ("list.yaml", "", "list"),
    ("list.yaml", "#/1", "object"),
    ("scalar.yaml", "", "scalar"),
    ("text.sql", "", "text"),
    ("nested.yaml", "", "object"),
    ("nested.yaml", "#/inner", "object"),
    ("nested.yaml", "#/t", "text"),
    ("nested.yaml", "#/l", "list"),
    ("sib.yaml", "", "object"),
    ("o.json", "", "object"),
    ("o.json", "#/b", "list"),
    ("o.json", "#/z/y", "object"),
    ("empty.yaml", "", "object"),
    ("deep.yaml", "", "object"),
    ("deep.yaml", "#/x", "object"),
    ("deep.yaml", "#/z", "object"),
    ("sub/o.yaml", "", "object"),
    ("sub/o.yaml", "#/n", "object"),
    ("sub/o2.yaml", "", "object"),
]
_MODEL_TEXT_SUFFIXES = {".sql", ".md", ".markdown", ".txt", ".py", ".scala", ".r"}


def _model_merge_patch(target: Any, patch: Any) -> Any:
    """RFC 7386, section 2, written out from the RFC's pseudo-code."""
    if not isinstance(patch, dict):
        return copy.deepcopy(patch)
    result = copy.deepcopy(target) if isinstance(target, dict) else {}
    for name, value in patch.items():
        if value is None:
            result.pop(name, None)
        else:
            result[name] = _model_merge_patch(result.get(name), value)
    return result


def _model_pointer(value: Any, pointer: str) -> Any:
    """RFC 6901 for the pointers the model draws (always ``/``-led, well-formed)."""
    if pointer in ("", "#"):
        return value
    for token in pointer[2:].split("/"):
        value = value[int(token)] if isinstance(value, list) else value[token]
    return value


def _model_resolve(node: Any, base: Path) -> Any:
    if isinstance(node, list):
        return [_model_resolve(item, base) for item in node]
    if not isinstance(node, dict):
        return node
    if "$ref" not in node:
        return {key: _model_resolve(value, base) for key, value in node.items()}
    siblings = {k: _model_resolve(v, base) for k, v in node.items() if k != "$ref"}
    file_part, _, pointer = node["$ref"].partition("#")
    target = (base / file_part).resolve()
    if target.suffix.lower() in _MODEL_TEXT_SUFFIXES:
        value: Any = target.read_bytes().decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    else:
        text = target.read_text(encoding="utf-8")
        value = json.loads(text) if target.suffix == ".json" else yaml.safe_load(text)
        value = {} if value is None else value
        value = _model_resolve(
            _model_pointer(value, "#" + pointer if pointer else ""), target.parent
        )
    return _model_merge_patch(value, siblings) if siblings else value


def _random_model_ref(rnd: random.Random, depth: int = 0) -> Dict[str, Any]:
    file, pointer, kind = rnd.choice(_MODEL_TARGETS)
    node: Dict[str, Any] = {"$ref": rnd.choice(["./", "", "./sub/../"]) + file + pointer}
    if kind != "object":
        return node  # only an object takes siblings
    for _ in range(rnd.choice([0, 0, 1, 2])):
        value: Any = (
            _random_model_ref(rnd, depth + 1)
            if depth < 2 and rnd.random() < 0.35
            else rnd.choice(
                [None, 1, "s", [1], [{"p": 9}], {"d": "q"}, {"c": [3]}, {}, {"d": None}]
                + [{"b": {"d": None, "n": 1}}]
            )
        )
        node[rnd.choice(["a", "b", "z", "l", "new", "d", "inner", "extra", "p"])] = value
    return node


def test_b24_random_sibling_compositions_agree_with_an_independent_model(
    tmp_path: Path,
) -> None:
    """Differential test. The model is RFC 7386 and the spec's reading rules written afresh
    (siblings resolve beside the file that holds them, a target's own refs first, text and
    lists and scalars as targets); 250 seeded documents of nested refs must compose alike."""
    for name, text in _MODEL_FILES.items():
        _write(tmp_path / name, text)
    rnd = random.Random(7386)

    for case in range(250):
        doc = {
            "id": "x",
            "r": _random_model_ref(rnd),
            "l": [_random_model_ref(rnd), _random_model_ref(rnd)],
            "n": {"m": _random_model_ref(rnd)},
        }
        before = copy.deepcopy(doc)

        got = _rfr(doc, tmp_path)

        assert got == _model_resolve(before, tmp_path), (case, before)
        assert doc == before
