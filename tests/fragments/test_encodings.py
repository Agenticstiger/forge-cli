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

"""``$ref`` fragments, slice 1: encodings and odd inputs (scenario area N).

What a fragment may contain at the byte level and in YAML 1.1's corners: byte-order
marks, line endings, empty files, size caps, files that are not UTF-8, aliases and
merge keys, duplicate keys, Unicode, odd keys and odd file names.

Scenarios that cite a SPEC GAP (N-01 JSON, N-09, N-13 collision, N-17, N-22
percent-encoding) pin what the code does today, because the spec gives them no
oracle. A test marked ``xfail(strict=True)`` asserts the spec and names the bug it
shows.
"""

from __future__ import annotations

import json
import os
import tarfile
import unicodedata
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

import pytest
import yaml

from fluid_build import loader
from fluid_build.contract_refs import MAX_TEXT_REF_BYTES, ComposeTrace, resolve_file_refs
from fluid_build.loader import RefResolutionError

MIB5 = 5 * 1024 * 1024

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


def _write(path: Path, text: str = "", *, data: Optional[bytes] = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8") if data is None else data)
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


def _rfr(doc: Any, root: Path, **kwargs: Any) -> Any:
    """``RFR``: the file-ref pass alone, as the loader calls it for a contract in ``root``."""
    return resolve_file_refs(
        doc, root, ref_root=root, source=root / "contract.fluid.yaml", **kwargs
    )


def _cli(capsys: pytest.CaptureFixture[str], *argv: str) -> Tuple[int, str]:
    from fluid_build.cli import main

    capsys.readouterr()
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out + captured.err


def _bundle(
    capsys: pytest.CaptureFixture[str], contract: Path, out: Path, *flags: str
) -> Dict[str, Any]:
    code, output = _cli(capsys, "bundle", str(contract), "--out", str(out), *flags)
    assert code == 0, output
    text = out.read_text(encoding="utf-8")
    return json.loads(text) if "json" in flags else yaml.safe_load(text)


def test_n00_import_origin_is_this_checkout() -> None:
    """The tests run against the checkout they sit in, never a stale installed copy."""
    checkout = Path(__file__).resolve().parents[2]
    assert Path(loader.__file__).resolve().is_relative_to(checkout)


# ── N-01 .. N-05: bytes, line endings, empty files, caps, other encodings ──


def test_n01_a_byte_order_mark_is_dropped_from_yaml_and_text_fragments(tmp_path: Path) -> None:
    _write(tmp_path / "bom.yaml", data=b"\xef\xbb\xbfk: v\n")
    _write(tmp_path / "bom.sql", data=b"\xef\xbb\xbfSELECT 1\n")
    _write(tmp_path / "only.md", data=b"\xef\xbb\xbf")
    root = _write(tmp_path / "root.fluid.yaml", data=b"\xef\xbb\xbfid: x\n")

    got = _rfr(
        {
            "y": {"$ref": "./bom.yaml"},
            "t": {"$ref": "./bom.sql"},
            "o": {"$ref": "./only.md"},
        },
        tmp_path,
    )

    assert got == {"y": {"k": "v"}, "t": "SELECT 1\n", "o": ""}
    assert loader.load_contract(root) == {"id": "x"}


def test_n01_a_json_fragment_or_root_with_a_byte_order_mark_is_refused_sg08(
    tmp_path: Path,
) -> None:
    """SG-08 characterization: JSON is not read as ``utf-8-sig``; the error names the file."""
    _write(tmp_path / "bom.json", data=b'\xef\xbb\xbf{"k": "v"}')
    root = _write(tmp_path / "contract.fluid.json", data=b'\xef\xbb\xbf{"id": "x"}')

    with pytest.raises(RefResolutionError, match="BOM") as info:
        _rfr({"x": {"$ref": "./bom.json"}}, tmp_path)
    assert "./bom.json" in str(info.value)
    with pytest.raises(RuntimeError, match="BOM") as root_info:
        loader.load_contract(root)
    assert "contract.fluid.json" in str(root_info.value)


@pytest.mark.parametrize("suffix", [".sql", ".md", ".txt"])
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (b"a\r\nb\r\n", "a\nb\n"),
        (b"a\rb\r", "a\nb\n"),
        (b"a\r\nb\nc\rd\r\n\r\ne", "a\nb\nc\nd\n\ne"),
        (b"\r\n\r\n", "\n\n"),
        (b"\r", "\n"),
        (b"no newline at all", "no newline at all"),
    ],
    ids=["crlf", "lone-cr", "mixed", "blank-crlf", "one-cr", "none"],
)
def test_n02_text_has_lf_line_endings_only(
    tmp_path: Path, suffix: str, raw: bytes, expected: str
) -> None:
    _write(tmp_path / f"t{suffix}", data=raw)

    got = _rfr({"x": {"$ref": f"./t{suffix}"}}, tmp_path)["x"]

    assert got == expected
    assert "\r" not in got


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("e.yaml", {}),
        ("dash.yaml", {}),
        ("comment.yaml", {}),
        ("e.sql", ""),
        ("e.md", ""),
        ("e.avsc", {}),
        ("e.yml", {}),
    ],
)
def test_n03_empty_files(tmp_path: Path, name: str, expected: Any) -> None:
    contents = {"dash.yaml": "---\n", "comment.yaml": "# nothing here\n"}
    _write(tmp_path / name, contents.get(name, ""))

    assert _rfr({"x": {"$ref": f"./{name}"}}, tmp_path) == {"x": expected}


def test_n03_an_empty_json_file_is_an_error_naming_it(tmp_path: Path) -> None:
    _write(tmp_path / "e.json", "")

    with pytest.raises(RefResolutionError, match="Expecting value") as info:
        _rfr({"x": {"$ref": "./e.json"}}, tmp_path)

    assert "./e.json" in str(info.value)


def test_n04_a_text_file_is_accepted_at_5_mib_and_refused_one_byte_over(tmp_path: Path) -> None:
    assert MAX_TEXT_REF_BYTES == MIB5
    _write(tmp_path / "exact.md", data=b"a" * MIB5)
    _write(tmp_path / "over.md", data=b"a" * (MIB5 + 1))

    assert len(_rfr({"x": {"$ref": "./exact.md"}}, tmp_path)["x"]) == MIB5
    with pytest.raises(RefResolutionError, match="over the 5,242,880-byte cap") as info:
        _rfr({"x": {"$ref": "./over.md"}}, tmp_path)
    assert "./over.md" in str(info.value)


def test_n04_a_yaml_fragment_is_accepted_at_5_mib_and_refused_one_byte_over(
    tmp_path: Path,
) -> None:
    _write(tmp_path / "exact.yaml", "k: " + "a" * (MIB5 - 4) + "\n")
    _write(tmp_path / "over.yaml", "k: " + "a" * (MIB5 - 3) + "\n")
    assert (tmp_path / "exact.yaml").stat().st_size == MIB5

    assert len(_rfr({"x": {"$ref": "./exact.yaml"}}, tmp_path)["x"]["k"]) == MIB5 - 4
    with pytest.raises(RefResolutionError, match="over the 5242880-byte safety cap") as info:
        _rfr({"x": {"$ref": "./over.yaml"}}, tmp_path)
    assert "./over.yaml" in str(info.value)


def test_n04_a_json_fragment_over_5_mib_is_refused_sg09(tmp_path: Path) -> None:
    """SG-09, decided (D-38): JSON is held to the 5 MiB cap of every file the composition
    reads, as YAML is, on its size, before it is read (it used to be read whole, and only
    the expansion budget limited it)."""
    _write(tmp_path / "big.json", json.dumps({"k": "a" * (MIB5 + 1)}))

    with pytest.raises(RefResolutionError, match="over the 5242880-byte safety cap") as info:
        _rfr({"x": {"$ref": "./big.json"}}, tmp_path)
    assert "./big.json" in str(info.value)


@pytest.mark.parametrize(
    ("name", "raw"),
    [
        ("latin1.sql", "SELECT 'caf\xe9'".encode("latin-1")),
        ("latin1.yaml", "k: caf\xe9\n".encode("latin-1")),
        ("utf16.sql", "SELECT 1".encode("utf-16")),
        ("utf16.yaml", "k: v\n".encode("utf-16")),
        ("utf16.json", '{"k": 1}'.encode("utf-16")),
        ("latin1.md", b"caf\xe9\n"),
    ],
)
def test_n05_a_file_that_is_not_utf8_is_refused_naming_the_file(
    tmp_path: Path, name: str, raw: bytes
) -> None:
    _write(tmp_path / name, data=raw)

    with pytest.raises(RefResolutionError) as info:
        _rfr({"x": {"$ref": f"./{name}"}}, tmp_path)

    assert f"./{name}" in str(info.value)
    assert "utf-8" in str(info.value).lower()
    assert not isinstance(info.value, UnicodeError)


# ── N-06 .. N-10: YAML aliases, merge keys, duplicates, tabs ───────────


def test_n06_aliases_in_a_fragment_expand_to_independent_copies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    _write(
        tmp_path / "al.yaml",
        "base: &b {x: 1, y: [1, 2]}\nuse1: *b\nuse2: *b\nlst: [*b, *b]\n",
    )
    written_out = {
        "base": {"x": 1, "y": [1, 2]},
        "use1": {"x": 1, "y": [1, 2]},
        "use2": {"x": 1, "y": [1, 2]},
        "lst": [{"x": 1, "y": [1, 2]}, {"x": 1, "y": [1, 2]}],
    }
    doc = _min_doc()
    doc["extensions"] = {"acme": {"$ref": "./al.yaml"}}
    contract = _write_contract(tmp_path, doc)

    composed = loader.load_contract(contract)["extensions"]["acme"]
    assert composed == written_out
    assert composed["use1"] is not composed["use2"] and composed["use1"] is not composed["base"]
    composed["use1"]["y"].append(3)
    assert composed["use2"]["y"] == [1, 2]

    out = tmp_path / "bundled.yaml"
    assert _bundle(capsys, contract, out)["extensions"]["acme"] == written_out
    text = out.read_text(encoding="utf-8")
    assert "&id" not in text and "*id" not in text


def test_n07_an_alias_to_an_anchor_in_another_file_is_a_yaml_error_naming_the_fragment(
    tmp_path: Path,
) -> None:
    _write(tmp_path / "defs.yaml", "defaults: &d {a: 1}\n")
    _write(tmp_path / "use.yaml", "k: *d\n")

    with pytest.raises(RefResolutionError, match="undefined alias") as info:
        _rfr({"x": {"$ref": "./defs.yaml"}, "y": {"$ref": "./use.yaml"}}, tmp_path)

    assert "use.yaml" in str(info.value)


def test_n08_a_merge_key_in_a_fragment_is_applied(tmp_path: Path) -> None:
    _write(
        tmp_path / "merge.yaml",
        "defaults: &d {a: 1, b: 2}\nitem:\n  <<: *d\n  b: 3\n  c: 4\n",
    )

    got = _rfr({"x": {"$ref": "./merge.yaml"}}, tmp_path)["x"]

    assert got["item"] == {"a": 1, "b": 3, "c": 4}


def test_n09_a_duplicate_key_keeps_the_last_value_silently_sg18(tmp_path: Path) -> None:
    """SG-18 characterization: PyYAML's last-wins applies inside fragments."""
    _write(tmp_path / "dup.yaml", "a: 1\na: 2\n")

    assert _rfr({"x": {"$ref": "./dup.yaml"}}, tmp_path) == {"x": {"a": 2}}


def test_n09_two_ref_keys_in_one_mapping_read_only_the_last_file_sg18(tmp_path: Path) -> None:
    """SG-18 characterization. The first ``$ref`` is dropped by the YAML parser, so the file
    a reviewer reads first is never read and nothing says so."""
    _write(tmp_path / "a.yaml", "from: a\n")
    _write(tmp_path / "b.yaml", "from: b\n")
    contract = _write(tmp_path / "contract.fluid.yaml", "x:\n  $ref: ./a.yaml\n  $ref: ./b.yaml\n")

    composed = loader.compose_contract(contract)

    assert composed.document == {"x": {"from": "b"}}
    assert [path.name for path in composed.files] == ["contract.fluid.yaml", "b.yaml"]


def test_n10_tabs_in_a_yaml_fragment_are_an_error_naming_the_file_and_line(
    tmp_path: Path,
) -> None:
    _write(tmp_path / "tabs.yaml", "a:\n\tb: 1\n")

    with pytest.raises(RefResolutionError, match="Failed to parse") as info:
        _rfr({"x": {"$ref": "./tabs.yaml"}}, tmp_path)

    message = str(info.value)
    assert '"tabs.yaml", line 2' in message
    assert "<unicode string>" not in message


def test_n10_tabs_in_a_sql_file_are_verbatim(tmp_path: Path) -> None:
    _write(tmp_path / "tabs.sql", "SELECT\n\ta,\n\t\tb\n")

    assert _rfr({"x": {"$ref": "./tabs.sql"}}, tmp_path) == {"x": "SELECT\n\ta,\n\t\tb\n"}


# ── N-11 .. N-13: Unicode, odd keys, YAML 1.1 keys ─────────────────────


def test_n11_unicode_keys_and_values_keep_their_normal_form(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    nfc = unicodedata.normalize("NFC", "caf\u00e9")
    nfd = unicodedata.normalize("NFD", "caf\u00e9")
    assert nfc != nfd
    data = {
        nfc: "NFC-value-" + nfc,
        nfd: "NFD-value-" + nfd,
        "ключ": "значение",
        "键": "值",
        "😀": "emoji \U0001f600",
        "e\u0301": "combining",
    }
    _write(tmp_path / "u.yaml", yaml.safe_dump(data, allow_unicode=True, sort_keys=False))
    _write(tmp_path / "u.json", json.dumps(data, ensure_ascii=False))
    doc = _min_doc()
    doc["extensions"] = {"y": {"$ref": "./u.yaml"}, "j": {"$ref": "./u.json"}}
    contract = _write_contract(tmp_path, doc)

    composed = loader.load_contract(contract)["extensions"]
    assert composed["y"] == data and composed["j"] == data
    assert len(composed["y"]) == 6
    assert [list(map(ord, key)) for key in composed["y"]] == [list(map(ord, k)) for k in data]

    bundled = _bundle(capsys, contract, tmp_path / "bundled.yaml")
    assert bundled["extensions"]["y"] == data
    assert (
        _bundle(capsys, contract, tmp_path / "bundled.json", "--format", "json")["extensions"]["j"]
        == data
    )


def test_n12_keys_with_slash_and_tilde_are_literal_and_escape_in_pointers(
    tmp_path: Path,
) -> None:
    data = {"a/b": 1, "c~d": 2, "~1": 3, "~0": 4, "x": {"y/z": 5}}
    _write(tmp_path / "k.yaml", yaml.safe_dump(data))

    assert _rfr({"x": {"$ref": "./k.yaml"}}, tmp_path) == {"x": data}
    pointed = {
        "slash": {"$ref": "./k.yaml#/a~1b"},
        "tilde": {"$ref": "./k.yaml#/c~0d"},
        "literal-tilde-one": {"$ref": "./k.yaml#/~01"},
        "literal-tilde-zero": {"$ref": "./k.yaml#/~00"},
        "nested": {"$ref": "./k.yaml#/x/y~1z"},
    }
    assert _rfr(pointed, tmp_path) == {
        "slash": 1,
        "tilde": 2,
        "literal-tilde-one": 3,
        "literal-tilde-zero": 4,
        "nested": 5,
    }
    with pytest.raises(RefResolutionError) as info:
        _rfr({"a/b": {"~k": {"$ref": 5}}}, tmp_path)
    assert "at JSON pointer '/a~1b/~0k'" in str(info.value)


_YAML_11_KEYS = "200: a\nyes: b\non: c\nnull: d\n1.5: e\n"


def test_n13_yaml_1_1_keys_in_a_fragment_equal_the_same_keys_written_inline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    expected = {200: "a", True: "c", None: "d", 1.5: "e"}
    outputs = {}
    for kind in ("inline", "fragment"):
        root = tmp_path / kind
        base = yaml.safe_dump(_min_doc(), sort_keys=False)
        if kind == "fragment":
            _write(root / "keys.yaml", _YAML_11_KEYS)
            tail = "extensions:\n  x:\n    $ref: ./keys.yaml\n"
        else:
            tail = "extensions:\n  x:\n" + "".join(
                f"    {line}\n" for line in _YAML_11_KEYS.splitlines()
            )
        contract = _write(root / "contract.fluid.yaml", base + tail)
        composed = loader.load_contract(contract)["extensions"]["x"]
        assert composed == expected
        assert {type(key) for key in composed} == {int, bool, type(None), float}
        _bundle(capsys, contract, root / "bundled.yaml")
        _bundle(capsys, contract, root / "bundled.json", "--format", "json")
        outputs[kind] = ((root / "bundled.yaml").read_bytes(), (root / "bundled.json").read_bytes())

    assert outputs["fragment"] == outputs["inline"]


def test_n13_a_string_sibling_does_not_merge_with_an_int_key_sg03(tmp_path: Path) -> None:
    """SG-03 characterization: ``"200"`` and ``200`` are different keys, so both stay."""
    _write(tmp_path / "k200.yaml", "200: a\nother: 1\n")

    got = _rfr({"x": {"$ref": "./k200.yaml", "200": "x"}}, tmp_path)["x"]

    assert got == {200: "a", "other": 1, "200": "x"}


# ── N-14 .. N-16: ref values, nesting, look-alike keys ─────────────────


@pytest.mark.parametrize("value", [42, 1.5, True, False, None, ["a"], {"a": 1}, []], ids=repr)
def test_n14_a_ref_value_that_is_not_a_string_is_refused_naming_the_place(
    tmp_path: Path, value: Any
) -> None:
    with pytest.raises(RefResolutionError, match="must be a string") as info:
        _rfr({"x": [{"$ref": value}]}, tmp_path)

    message = str(info.value)
    assert f"got {type(value).__name__}" in message
    assert "at JSON pointer '/x/0'" in message and "contract.fluid.yaml" in message


def test_n14_a_yaml_ref_with_no_value_and_a_bad_ref_inside_a_fragment_name_their_own_file(
    tmp_path: Path,
) -> None:
    root = _write(tmp_path / "contract.fluid.yaml", "x:\n  $ref:\n")
    _write(tmp_path / "bad.yaml", "k:\n  - $ref: 42\n")

    with pytest.raises(RefResolutionError, match="got NoneType") as root_info:
        loader.load_contract(root)
    assert "at JSON pointer '/x'" in str(root_info.value)

    with pytest.raises(RefResolutionError, match="got int") as fragment_info:
        _rfr({"y": {"$ref": "./bad.yaml"}}, tmp_path)
    assert "at JSON pointer '/k/0'" in str(fragment_info.value)
    assert "bad.yaml" in str(fragment_info.value)


def test_n15_refs_inside_lists_of_lists_and_inside_siblings_resolve(tmp_path: Path) -> None:
    _write(tmp_path / "t.yaml", "k: 1\n")
    deep: Any = {"$ref": "./t.yaml"}
    for _ in range(6):
        deep = [deep]

    got = _rfr(
        {
            "lol": [[{"$ref": "./t.yaml"}]],
            "sib": {"$ref": "./t.yaml", "more": [{"$ref": "./t.yaml"}, [[{"$ref": "./t.yaml"}]]]},
            "deep": deep,
        },
        tmp_path,
    )

    assert got["lol"] == [[{"k": 1}]]
    assert got["sib"] == {"k": 1, "more": [{"k": 1}, [[{"k": 1}]]]}
    assert got["deep"] == [[[[[[{"k": 1}]]]]]]


def test_n16_look_alike_keys_are_ordinary_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    look_alikes = {"$REF": "./x.yaml", "$Ref": "./y.yaml", "$ref ": "./z.yaml", "＄ref": "./w.yaml"}
    doc = _min_doc()
    doc["extensions"] = dict(look_alikes)
    contract = _write_contract(tmp_path, doc)

    assert loader.load_contract(contract)["extensions"] == look_alikes
    code, output = _cli(capsys, "validate", str(contract))
    assert code == 0, output

    closed = _min_doc()
    closed["exposes"][0]["$REF"] = "./x.yaml"
    closed_contract = _write_contract(tmp_path, closed, "closed.fluid.yaml")
    code, output = _cli(capsys, "validate", str(closed_contract))
    assert code == 1
    assert "'$REF' was unexpected" in output


# ── N-17, N-18: control characters and whitespace ──────────────────────

_CONTROL_TEXT = "SELECT 1\x00\x01\x07\x1b[0m\x7f\n"


def _forms_carrying_a_text_ref(
    capsys: pytest.CaptureFixture[str], root: Path, text: str
) -> Dict[str, str]:
    """The SQL a build gets from ``./t.sql`` (holding ``text``) in every form it is carried in:
    the loader, ``fluid bundle`` as YAML, as JSON and as a tgz, and the plan of the contract
    and of that tgz."""
    _write(root / "t.sql", data=text.encode("utf-8"))
    doc = _min_doc()
    doc["builds"][0]["properties"]["sql"] = {"$ref": "./t.sql"}
    contract = _write_contract(root, doc)

    def sql_of(contract_doc: Dict[str, Any]) -> str:
        return contract_doc["builds"][0]["properties"]["sql"]

    carried = {"loader": sql_of(loader.load_contract(contract))}
    carried["bundle-yaml"] = sql_of(_bundle(capsys, contract, root / "bundled.yaml"))
    carried["bundle-json"] = sql_of(
        _bundle(capsys, contract, root / "bundled.json", "--format", "json")
    )
    code, output = _cli(
        capsys, "bundle", str(contract), "--format", "tgz", "--out", str(root / "b.tgz")
    )
    assert code == 0, output
    with tarfile.open(root / "b.tgz") as archive:
        carried["tgz-yaml"] = sql_of(yaml.safe_load(archive.extractfile("contract.resolved.yaml")))  # type: ignore[arg-type]
        carried["tgz-json"] = sql_of(json.load(archive.extractfile("contract.resolved.json")))  # type: ignore[arg-type]
    for label, source in (("plan-of-contract", contract), ("plan-of-tgz", root / "b.tgz")):
        code, output = _cli(capsys, "plan", str(source), "--out", str(root / f"{label}.json"))
        assert code == 0, output
        carried[label] = sql_of(json.loads((root / f"{label}.json").read_text())["contract"])
    return carried


def test_n17_control_characters_in_text_are_kept_and_survive_every_form_sg35(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """SG-35 characterization: NUL and the other C0 controls are not refused, and every form
    of the composed contract carries them exactly."""
    monkeypatch.chdir(tmp_path)

    carried = _forms_carrying_a_text_ref(capsys, tmp_path, _CONTROL_TEXT)

    assert carried == dict.fromkeys(carried, _CONTROL_TEXT)
    code, output = _cli(capsys, "validate", str(tmp_path / "contract.fluid.yaml"))
    assert code == 0, output


def test_n17_a_next_line_character_in_text_survives_every_form(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The loader keeps U+0085 and so does every JSON form; the YAML forms (and the plan
    made from them) turn it into a space."""
    monkeypatch.chdir(tmp_path)
    text = "SELECT 'a\x85b'\n"

    carried = _forms_carrying_a_text_ref(capsys, tmp_path, text)

    assert carried["loader"] == text
    assert carried == dict.fromkeys(carried, text)


@pytest.mark.parametrize(
    "text",
    [
        "SELECT 1",
        "SELECT 1   \n  \n",
        "\n\n  SELECT 1\n",
        "  leading and trailing  ",
        "a\n\n\nb\n\n",
        " \n",
        "\t\n",
        "word " * 60 + "\n",
        "a  b   c    " * 40 + "\n",
    ],
    ids=[
        "no-final-newline",
        "trailing-spaces",
        "leading-blank-lines",
        "padded",
        "blank-runs",
        "space-newline",
        "tab-newline",
        "long-line",
        "long-line-with-runs-of-spaces",
    ],
)
def test_n18_whitespace_in_text_is_preserved_exactly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], text: str
) -> None:
    monkeypatch.chdir(tmp_path)
    _write(tmp_path / "t.sql", text)
    assert _rfr({"x": {"$ref": "./t.sql"}}, tmp_path) == {"x": text}

    doc = _min_doc()
    doc["extensions"] = {"x": {"$ref": "./t.sql"}}
    contract = _write_contract(tmp_path, doc)
    assert _bundle(capsys, contract, tmp_path / "bundled.yaml")["extensions"]["x"] == text
    assert (
        _bundle(capsys, contract, tmp_path / "bundled.json", "--format", "json")["extensions"]["x"]
        == text
    )


# ── N-19 .. N-21: text as a version, multi-document, implicit types ────


def test_n19_a_version_read_from_text_keeps_its_newline_and_fails_the_schema_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Pinned for the docs: ``0.7.6\\n`` is not ``0.7.6``. The remote schema fetch is stubbed
    (the unknown version would otherwise go to the network)."""
    import fluid_build.util.safe_http as safe_http

    requested: List[str] = []

    def refuse(url: str, **kwargs: Any) -> Any:
        requested.append(url)
        raise RuntimeError("no network in tests")

    monkeypatch.setattr(safe_http, "fetch_bytes", refuse)
    monkeypatch.chdir(tmp_path)
    _write(tmp_path / "version.txt", "0.7.6\n")
    doc = _min_doc()
    doc["fluidVersion"] = {"$ref": "./version.txt"}
    contract = _write_contract(tmp_path, doc)

    assert loader.load_contract(contract)["fluidVersion"] == "0.7.6\n"
    code, output = _cli(capsys, "validate", str(contract))

    assert code == 1
    assert "0.7.6" in output
    assert "Traceback" not in output
    assert all(url.startswith("https://raw.githubusercontent.com/") for url in requested)


def test_n20_a_multi_document_yaml_fragment_is_refused_naming_the_file(tmp_path: Path) -> None:
    _write(tmp_path / "multi.yaml", "a: 1\n---\nb: 2\n")

    with pytest.raises(RefResolutionError, match="expected a single document") as info:
        _rfr({"x": {"$ref": "./multi.yaml"}}, tmp_path)

    assert "multi.yaml" in str(info.value)


def test_n21_yaml_1_1_implicit_types_in_a_fragment_equal_the_inline_ones(tmp_path: Path) -> None:
    import datetime

    from fluid_build.api.contract import ContractLoadError, load_contract

    block = "d: 2026-01-01\nflag: no\nts: 2026-01-01 10:00:00\n"
    _write(tmp_path / "files" / "types.yaml", block)
    files_doc = _min_doc()
    files_doc["extensions"] = {"t": {"$ref": "./types.yaml"}}
    files = _write_contract(tmp_path / "files", files_doc)
    inline = _write(
        tmp_path / "inline" / "contract.fluid.yaml",
        yaml.safe_dump(_min_doc(), sort_keys=False)
        + "extensions:\n  t:\n"
        + "".join(f"    {line}\n" for line in block.splitlines()),
    )

    from_files = load_contract(files)
    from_inline = load_contract(inline)

    assert from_files.contract["extensions"] == from_inline.contract["extensions"]
    assert from_files.contract["extensions"]["t"] == {
        "d": datetime.date(2026, 1, 1),
        "flag": False,
        "ts": datetime.datetime(2026, 1, 1, 10, 0),
    }
    for loaded in (from_files, from_inline):
        with pytest.raises(ContractLoadError) as info:
            _ = loaded.digest
        assert info.value.event == "contract_not_serialisable"


# ── N-22 .. N-25: odd file names ───────────────────────────────────────


def test_n22_file_names_with_spaces_and_unicode_resolve(tmp_path: Path) -> None:
    _write(tmp_path / "fragments" / "ördér items.yaml", "k: unicode\n")
    _write(tmp_path / "my frag.yaml", "k: space\n")
    _write(tmp_path / "data 日本.sql", "SELECT 'x'\n")

    got = _rfr(
        {
            "u": {"$ref": "./fragments/ördér items.yaml"},
            "s": {"$ref": "./my frag.yaml"},
            "j": {"$ref": "./data 日本.sql"},
        },
        tmp_path,
    )

    assert got == {"u": {"k": "unicode"}, "s": {"k": "space"}, "j": "SELECT 'x'\n"}


def test_n22_a_percent_encoded_ref_names_a_file_literally_sg02(tmp_path: Path) -> None:
    """SG-02 characterization: refs are paths, not URIs, so ``%20`` is three characters."""
    _write(tmp_path / "my frag.yaml", "k: space\n")

    with pytest.raises(RefResolutionError, match="not found") as info:
        _rfr({"x": {"$ref": "./my%20frag.yaml"}}, tmp_path)
    assert "./my%20frag.yaml" in str(info.value)

    _write(tmp_path / "pct%20name.yaml", "k: literal\n")
    assert _rfr({"x": {"$ref": "./pct%20name.yaml"}}, tmp_path) == {"x": {"k": "literal"}}


def test_n23_redundant_path_segments_are_collapsed(tmp_path: Path) -> None:
    _write(tmp_path / "a" / "x.yaml", "k: a/x\n")
    _write(tmp_path / "fragments" / "x.yaml", "k: fragments/x\n")
    _write(tmp_path / "x.yaml", "k: root/x\n")

    got = _rfr(
        {
            "a": {"$ref": "./a/../a/x.yaml"},
            "b": {"$ref": "fragments//x.yaml"},
            "c": {"$ref": "x.yaml/../x.yaml"},
            "d": {"$ref": "./fragments/./x.yaml"},
        },
        tmp_path,
    )

    assert got == {
        "a": {"k": "a/x"},
        "b": {"k": "fragments/x"},
        "c": {"k": "root/x"},
        "d": {"k": "fragments/x"},
    }


@pytest.mark.parametrize("ref", ["./x.yaml/", "./x.yaml/."])
def test_n23_a_trailing_slash_does_not_name_a_regular_file(tmp_path: Path, ref: str) -> None:
    """``Path`` drops the ending, but the file system reads it as 'a directory'."""
    _write(tmp_path / "x.yaml", "k: root/x\n")

    with pytest.raises(RefResolutionError, match="names a directory") as info:
        _rfr({"x": {"$ref": ref}}, tmp_path)

    assert ref in str(info.value)


def test_n24_a_file_name_with_a_hash_cannot_be_named_because_the_ref_splits_at_the_hash_sg02(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SG-02 characterization: ``./a#b.yaml`` is file ``./a`` plus pointer ``b.yaml``."""
    _write(tmp_path / "a#b.yaml", "secret: HASH-FILE-N24\n")
    reads: List[str] = []
    real_read_text = Path.read_text

    def spy(self: Path, *args: Any, **kwargs: Any) -> str:
        reads.append(self.name)
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", spy)

    with pytest.raises(RefResolutionError, match="not found") as info:
        _rfr({"x": {"$ref": "./a#b.yaml"}}, tmp_path)

    assert "./a#b.yaml" in str(info.value)
    assert "HASH-FILE-N24" not in str(info.value)
    assert "a#b.yaml" not in reads


def test_n24_a_file_refs_pointer_without_a_slash_names_a_key_for_compatibility(
    tmp_path: Path,
) -> None:
    """Characterization (N24, decided): a file ref's fragment need not start with '/'.
    ``./a#b.yaml`` reads key ``b.yaml`` of file ``a``, and ``./x.yaml#key`` key ``key``,
    as contracts 0.24.0 loaded did; only a same-document ``#key`` is refused (C-12)."""
    _write(tmp_path / "a", "b.yaml: KEY-OF-A\nother: 1\n")
    _write(tmp_path / "x.yaml", "key: KEY-OF-X\n")

    assert _rfr({"x": {"$ref": "./a#b.yaml"}}, tmp_path) == {"x": "KEY-OF-A"}
    assert _rfr({"x": {"$ref": "./x.yaml#key"}}, tmp_path) == {"x": "KEY-OF-X"}


@pytest.mark.skipif(os.name == "nt", reason="a ? cannot be part of a Windows file name")
def test_n25_a_query_string_is_part_of_the_file_name(tmp_path: Path) -> None:
    _write(tmp_path / "x.yaml?v=1", "k: query\n")

    assert _rfr({"x": {"$ref": "./x.yaml?v=1"}}, tmp_path) == {"x": {"k": "query"}}
    with pytest.raises(RefResolutionError, match="not found") as info:
        _rfr({"x": {"$ref": "./x.yaml?v=2"}}, tmp_path)
    assert "./x.yaml?v=2" in str(info.value)


def test_n09_the_trace_lists_each_fragment_read_in_order(tmp_path: Path) -> None:
    """The trace and the API agree on which files a composition read (used by N-09)."""
    _write(tmp_path / "a.yaml", "k: 1\n")
    _write(tmp_path / "b.sql", "SELECT 1")
    trace = ComposeTrace()

    _rfr({"x": {"$ref": "./a.yaml"}, "y": {"$ref": "./b.sql"}}, tmp_path, trace=trace)

    assert [path.name for path in trace.files] == ["a.yaml", "b.sql"]
