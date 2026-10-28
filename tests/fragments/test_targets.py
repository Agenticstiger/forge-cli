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

"""``$ref`` fragments, slice 1: what a ref may name (scenario area A).

Spec items 1 and 2 of the fragment-composition catalog: a JSON or YAML file may
hold any root value, a file with a text suffix is its text (never parsed), a
pointer cannot select inside text, and the root contract and an overlay stay
objects. ``RFR`` tests call ``contract_refs.resolve_file_refs`` directly; the
loader-level (``LC``) tests, the API and the CLI go through the one composition.

A test named for a SPEC GAP scenario (A-07, A-14 ``#``, A-16, A-20, A-25, A-26)
pins what the code does today, because the spec gives it no oracle. A test
marked ``xfail(strict=True)`` asserts the spec and names the bug it shows.
"""

from __future__ import annotations

import json
import os
import tarfile
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

import pytest
import yaml

import fluid_build
from fluid_build import loader
from fluid_build.contract_refs import resolve_file_refs
from fluid_build.loader import RefResolutionError

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


def _rfr(doc: Any, root: Path) -> Any:
    """``RFR``: the file-ref pass alone, as the loader calls it for a contract in ``root``."""
    return resolve_file_refs(doc, root, ref_root=root, source=root / "contract.fluid.yaml")


def _ref_paths(node: Any, path: Tuple[Any, ...] = ()) -> List[Tuple[Any, ...]]:
    """``NOREF``: where a mapping has the KEY ``$ref``. Keys are walked, text is never searched."""
    found: List[Tuple[Any, ...]] = []
    if isinstance(node, dict):
        if "$ref" in node:
            found.append(path)
        for key, value in node.items():
            found.extend(_ref_paths(value, (*path, key)))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found.extend(_ref_paths(value, (*path, index)))
    return found


def _cli(capsys: pytest.CaptureFixture[str], *argv: str) -> Tuple[int, str]:
    from fluid_build.cli import main

    capsys.readouterr()
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out + captured.err


def _bundle(capsys: pytest.CaptureFixture[str], contract: Path, out: Path) -> Dict[str, Any]:
    code, output = _cli(capsys, "bundle", str(contract), "--out", str(out))
    assert code == 0, output
    return yaml.safe_load(out.read_text(encoding="utf-8"))


def _odcs(capsys: pytest.CaptureFixture[str], contract: Path, out: Path) -> bytes:
    code, output = _cli(
        capsys, "generate", "standard", str(contract), "--format", "odcs", "--out", str(out)
    )
    assert code == 0, output
    return out.read_bytes()


def test_a00_import_origin_is_this_checkout() -> None:
    """The tests run against the checkout they sit in, never a stale installed copy."""
    checkout = Path(__file__).resolve().parents[2]
    assert Path(fluid_build.__file__).resolve().is_relative_to(checkout)


# ── A. target kinds and roots ───────────────────────────────────────────

_RULES = [
    {"rule": "id_present", "expression": "id IS NOT NULL", "severity": "error"},
    {"rule": "id_positive", "expression": "id > 0", "severity": "warning"},
]

_SQL = (
    "-- Revenue per customer: monthly rollup\n"
    "{{ config(materialized='table') }}\n"
    "WITH monthly AS (\n"
    "\tSELECT customer_id,  # hash marks are not comments in every dialect\n"
    "\t       'a: b' AS note\n"
    "\tFROM orders\n"
    ")\n"
    "SELECT * FROM monthly\n"
)

_USAGE = "# Usage\n\nNote: join on `id`.\n\n- first\n- second\n"


def test_a01_an_object_fragment_composes_validates_and_bundles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    owner = {"team": "t", "email": "t@x.io"}
    _write(tmp_path / "owner.yaml", yaml.safe_dump(owner))
    doc = _min_doc()
    doc["metadata"]["owner"] = {"$ref": "./owner.yaml"}
    contract = _write_contract(tmp_path, doc)

    assert loader.load_contract(contract)["metadata"]["owner"] == owner
    code, output = _cli(capsys, "validate", str(contract))
    assert code == 0, output
    bundled = _bundle(capsys, contract, tmp_path / "bundled.yaml")
    assert bundled["metadata"]["owner"] == owner
    assert _ref_paths(bundled) == []


def _quality_product(root: Path, kind: str) -> Path:
    """MIN with its two quality rules inline, or in a list-rooted YAML or JSON fragment."""
    doc = _min_doc()
    quality: Any = [dict(rule) for rule in _RULES]
    if kind == "yaml":
        _write(root / "fragments" / "quality.yaml", yaml.safe_dump(_RULES, sort_keys=False))
        quality = {"$ref": "./fragments/quality.yaml"}
    elif kind == "json":
        _write(root / "fragments" / "quality.json", json.dumps(_RULES))
        quality = {"$ref": "./fragments/quality.json"}
    doc["exposes"][0]["contract"]["quality"] = quality
    return _write_contract(root, doc)


def test_a02_a_list_rooted_yaml_fragment_validates_and_exports_like_the_inline_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    results: Dict[str, Tuple[bytes, bytes]] = {}
    for kind in ("inline", "yaml"):
        root = tmp_path / kind
        contract = _quality_product(root, kind)
        monkeypatch.chdir(root)
        code, output = _cli(capsys, "validate", str(contract))
        assert code == 0, output
        _bundle(capsys, contract, root / "bundled.yaml")
        results[kind] = (
            (root / "bundled.yaml").read_bytes(),
            _odcs(capsys, contract, root / "out.odcs.yaml"),
        )

    bundled = yaml.safe_load(results["yaml"][0])
    assert bundled["exposes"][0]["contract"]["quality"] == _RULES
    assert _ref_paths(bundled) == []
    assert results["yaml"][0] == results["inline"][0]
    assert results["yaml"][1] == results["inline"][1]


def test_a03_a_json_list_fragment_bundles_to_the_same_bytes_as_the_yaml_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    bundles = {}
    for kind in ("yaml", "json"):
        root = tmp_path / kind
        contract = _quality_product(root, kind)
        monkeypatch.chdir(root)
        _bundle(capsys, contract, root / "bundled.yaml")
        bundles[kind] = (root / "bundled.yaml").read_bytes()

    assert bundles["json"] == bundles["yaml"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("hello\n", "hello"),
        ("42\n", 42),
        ("true\n", True),
        ("1.5\n", 1.5),
        ('"1.2.0"\n', "1.2.0"),
        ("'007'\n", "007"),
    ],
    ids=["str", "int", "bool", "float", "quoted-version", "quoted-digits"],
)
def test_a04_yaml_scalar_roots_keep_their_python_type(
    tmp_path: Path, text: str, expected: Any
) -> None:
    _write(tmp_path / "s.yaml", text)

    got = _rfr({"x": {"$ref": "./s.yaml"}}, tmp_path)["x"]

    assert got == expected
    assert type(got) is type(expected)


def test_a04_a_scalar_fragment_can_be_an_exposes_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    _write(tmp_path / "v.yaml", '"1.2.0"\n')
    doc = _min_doc()
    doc["exposes"][0]["version"] = {"$ref": "./v.yaml"}
    contract = _write_contract(tmp_path, doc)

    assert loader.load_contract(contract)["exposes"][0]["version"] == "1.2.0"
    code, output = _cli(capsys, "validate", str(contract))
    assert code == 0, output


@pytest.mark.parametrize(
    ("text", "expected"),
    [('"s"', "s"), ("42", 42), ("null", None), ("true", True), ("[]", []), ('"007"', "007")],
    ids=["str", "int", "null", "bool", "empty-list", "digit-string"],
)
def test_a05_json_scalar_roots_keep_their_value(tmp_path: Path, text: str, expected: Any) -> None:
    _write(tmp_path / "s.json", text)

    got = _rfr({"x": {"$ref": "./s.json"}}, tmp_path)["x"]

    assert got == expected
    assert type(got) is type(expected)


@pytest.mark.parametrize(
    "text",
    ["", "# only a comment\n", "---\n", "   \n\n", "---\n# comment after the marker\n"],
    ids=["empty", "comment-only", "document-start", "blank-lines", "marker-and-comment"],
)
def test_a06_an_empty_yaml_fragment_is_an_empty_object(tmp_path: Path, text: str) -> None:
    _write(tmp_path / "e.yaml", text)

    assert _rfr({"x": {"$ref": "./e.yaml"}}, tmp_path) == {"x": {}}


@pytest.mark.parametrize("text", ["null\n", "~\n", "---\n~\n"], ids=["null", "tilde", "marker"])
def test_a07_a_yaml_null_document_is_an_empty_object_sg07(tmp_path: Path, text: str) -> None:
    """SG-07 characterization: a YAML null document is ``{}``; a JSON ``null`` is ``None``."""
    _write(tmp_path / "n.yaml", text)
    _write(tmp_path / "n.json", "null")

    got = _rfr({"y": {"$ref": "./n.yaml"}, "j": {"$ref": "./n.json"}}, tmp_path)

    assert got == {"y": {}, "j": None}


def test_a08_an_empty_json_fragment_is_an_error_and_an_empty_avsc_is_an_object(
    tmp_path: Path,
) -> None:
    _write(tmp_path / "empty.json", "")
    _write(tmp_path / "blank.json", "  \n")
    _write(tmp_path / "empty.avsc", "")
    _write(tmp_path / "blank.avsc", "  \n")

    for name in ("empty.json", "blank.json"):
        with pytest.raises(RefResolutionError, match="Expecting value") as info:
            _rfr({"x": {"$ref": f"./{name}"}}, tmp_path)
        assert f"./{name}" in str(info.value)
    assert _rfr({"x": {"$ref": "./empty.avsc"}}, tmp_path) == {"x": {}}
    assert _rfr({"x": {"$ref": "./blank.avsc"}}, tmp_path) == {"x": {}}


def test_a09_realistic_sql_is_the_file_text_exactly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """In 0.24.0 a comment, a Jinja header, ``': '``, tabs or ``#`` made this either a YAML
    error or a silent mapping."""
    monkeypatch.chdir(tmp_path)
    _write(tmp_path / "sql" / "t.sql", _SQL)
    doc = _min_doc()
    doc["builds"][0]["properties"]["sql"] = {"$ref": "./sql/t.sql"}
    contract = _write_contract(tmp_path, doc)

    assert loader.load_contract(contract)["builds"][0]["properties"]["sql"] == _SQL
    code, output = _cli(capsys, "validate", str(contract))
    assert code == 0, output
    bundled = _bundle(capsys, contract, tmp_path / "bundled.yaml")
    assert bundled["builds"][0]["properties"]["sql"] == _SQL

    inline_doc = _min_doc()
    inline_doc["builds"][0]["properties"]["sql"] = _SQL
    inline = _write_contract(tmp_path / "inline", inline_doc)
    assert _odcs(capsys, contract, tmp_path / "files.odcs.yaml") == _odcs(
        capsys, inline, tmp_path / "inline.odcs.yaml"
    )


def test_a10_a_markdown_description_reaches_the_odcs_export_as_its_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    twins = {}
    for kind in ("inline", "files"):
        root = tmp_path / kind
        doc = _min_doc()
        doc["description"]["usage"] = _USAGE
        if kind == "files":
            _write(root / "docs" / "usage.md", _USAGE)
            doc["description"]["usage"] = {"$ref": "./docs/usage.md"}
        contract = _write_contract(root, doc)
        bundle = root / "bundled.yaml"
        _bundle(capsys, contract, bundle)
        twins[kind] = (_odcs(capsys, contract, root / "out.odcs.yaml"), bundle.read_bytes())

    assert yaml.safe_load(twins["files"][0])["description"]["usage"] == _USAGE
    assert twins["files"] == twins["inline"]


@pytest.mark.parametrize("suffix", [".markdown", ".txt", ".py", ".scala", ".r"])
def test_a11_every_text_suffix_is_read_as_text_never_parsed(tmp_path: Path, suffix: str) -> None:
    text = "select: 1\nfrom: src\n- not\n  - yaml: [\n"
    _write(tmp_path / f"t{suffix}", text)

    assert _rfr({"x": {"$ref": f"./t{suffix}"}}, tmp_path) == {"x": text}


@pytest.mark.parametrize("name", ["T.SQL", "t.Md", "t.R", "t.TXT", "t.PY", "t.Markdown", "t.ScAlA"])
def test_a12_text_suffixes_match_without_case(tmp_path: Path, name: str) -> None:
    text = "key: value\nother: [1, 2\n"
    _write(tmp_path / name, text)

    assert _rfr({"x": {"$ref": f"./{name}"}}, tmp_path) == {"x": text}


def test_a13_text_that_would_parse_stays_a_string(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """0.24.0 gave a mapping, the unquoted string and ``{}``; text is now never parsed."""
    monkeypatch.chdir(tmp_path)
    texts = {"kv": "select: 1\nfrom: src\n", "jsonstr": '"SELECT 1"', "empty": ""}
    for key, text in texts.items():
        _write(tmp_path / f"{key}.sql", text)

    refs = {key: {"$ref": f"./{key}.sql"} for key in texts}
    assert _rfr(refs, tmp_path) == texts

    doc = _min_doc()
    doc["extensions"] = refs
    contract = _write_contract(tmp_path, doc)
    bundled = _bundle(capsys, contract, tmp_path / "bundled.yaml")
    assert bundled["extensions"] == texts


def test_a14_a_pointer_into_a_text_file_is_refused(tmp_path: Path) -> None:
    _write(tmp_path / "t.sql", "SELECT 1\n")

    with pytest.raises(RefResolutionError) as info:
        _rfr({"x": {"$ref": "./t.sql#/a"}}, tmp_path)

    message = str(info.value)
    assert "./t.sql#/a" in message
    assert "contract.fluid.yaml" in message
    assert "read as text" in message


def test_a14_an_empty_fragment_on_a_text_ref_returns_the_text_sg17(tmp_path: Path) -> None:
    """SG-17 characterization: ``./t.sql#`` is not refused; it names the whole file."""
    _write(tmp_path / "t.sql", "SELECT 1\n")

    assert _rfr({"x": {"$ref": "./t.sql#"}}, tmp_path) == {"x": "SELECT 1\n"}


def test_a15_siblings_on_a_text_target_are_refused_naming_them(tmp_path: Path) -> None:
    _write(tmp_path / "t.sql", "SELECT 1\n")

    with pytest.raises(RefResolutionError) as info:
        _rfr({"x": {"$ref": "./t.sql", "description": "x", "other": 1}}, tmp_path)

    message = str(info.value)
    assert "'description'" in message and "'other'" in message
    assert "text, not an object" in message
    assert "./t.sql" in message


def test_a16_unlisted_text_like_suffixes_are_parsed_as_yaml_sg16(tmp_path: Path) -> None:
    """SG-16 characterization. Only the seven listed suffixes are text; any other name is
    JSON or YAML. A ``.hql`` file whose text has a ``': '`` therefore becomes a one-key
    mapping, silently; text that is not a YAML object is refused with the file named."""
    _write(tmp_path / "q.hql", "SELECT 'a: b' FROM t")
    assert _rfr({"x": {"$ref": "./q.hql"}}, tmp_path) == {"x": {"SELECT 'a": "b' FROM t"}}

    for name, text in [("notes", "Hello\nworld # tail"), ("run.sh", "echo hi\n")]:
        _write(tmp_path / name, text)
        with pytest.raises(RefResolutionError, match="must be an object") as info:
            _rfr({"x": {"$ref": f"./{name}"}}, tmp_path)
        assert f"./{name}" in str(info.value)

    _write(tmp_path / "t.j2", "{{ a }}: b\n")
    with pytest.raises(RefResolutionError, match="Failed to parse") as info:
        _rfr({"x": {"$ref": "./t.j2"}}, tmp_path)
    assert "t.j2" in str(info.value)


@pytest.mark.parametrize(
    "text",
    [
        "# a comment\n{type: record, name: A, fields: [{name: a, type: string},],}\n",
        '# a comment\n{"type": "record", "name": "A", "fields": [{"name": "a", "type": "string"},],}\n',
        "# a comment\ntype: record\nname: A\nfields:\n  - {name: a, type: string}\n",
    ],
    ids=["flow-with-trailing-commas", "json-with-trailing-commas", "block"],
)
def test_a17_an_avsc_that_is_not_json_is_parsed_leniently_as_yaml(
    tmp_path: Path, text: str
) -> None:
    _write(tmp_path / "s.avsc", text)

    assert _rfr({"x": {"$ref": "./s.avsc"}}, tmp_path) == {
        "x": {"type": "record", "name": "A", "fields": [{"name": "a", "type": "string"}]}
    }


@pytest.mark.parametrize(
    ("name", "text"),
    [
        ("contract.fluid.yaml", "- a\n"),
        ("contract.fluid.yaml", "just a string\n"),
        ("contract.fluid.json", '[{"$ref": "./x.yaml"}]'),
        ("contract.fluid.json", '"just a string"'),
        ("contract.fluid.json", "42"),
        ("contract.fluid.json", "null"),
    ],
    ids=["yaml-list", "yaml-scalar", "json-list", "json-string", "json-int", "json-null"],
)
def test_a18_a_root_that_is_not_an_object_is_refused_by_every_entry_point(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    name: str,
    text: str,
) -> None:
    from fluid_build.api.contract import ContractLoadError, load_contract

    monkeypatch.chdir(tmp_path)
    contract = _write(tmp_path / name, text)
    _write(tmp_path / "x.yaml", "a: 1\n")

    entry_points = {
        "load_contract": lambda: loader.load_contract(contract),
        "compile_contract": lambda: loader.compile_contract(contract),
        "load_with_overlay": lambda: loader.load_with_overlay(contract, "prod"),
        "load_with_overlay-no-env": lambda: loader.load_with_overlay(contract),
        "compose_contract": lambda: loader.compose_contract(contract),
    }
    for label, call in entry_points.items():
        with pytest.raises((RuntimeError, ValueError)) as info:
            call()
        assert "root must be an object" in str(info.value), label
        assert name in str(info.value), label

    with pytest.raises(ContractLoadError) as api_info:
        load_contract(contract)
    assert api_info.value.event == "contract_not_a_mapping"
    assert name in str(api_info.value)

    for argv in (
        ("validate", str(contract)),
        ("bundle", str(contract), "--out", str(tmp_path / "b.yaml")),
    ):
        code, output = _cli(capsys, *argv)
        assert code in (1, 2), (argv[0], output)
        assert "root must be an object" in output, argv[0]
        assert name in output, argv[0]
        assert "AttributeError" not in output and "Traceback" not in output, argv[0]
    assert not (tmp_path / "b.yaml").exists()


@pytest.mark.xfail(
    strict=True,
    reason="BUG A18-raw-json-root: _parse_file accepts a JSON root that is not an object, so "
    "resolve_refs=False hands it back (or dies in dict() with no file named)",
)
def test_a18_a_raw_load_of_a_json_root_that_is_not_an_object_is_refused(tmp_path: Path) -> None:
    """The YAML twin of this file raises ``YAML root must be an object/dict`` naming the
    file on every entry point; the JSON one only does when refs are resolved."""
    contract = _write(tmp_path / "contract.fluid.json", '[{"$ref": "./x.yaml"}]')
    _write(tmp_path / "overlays" / "prod.yaml", "id: prod\n")

    raw_calls = {
        "load_contract": lambda: loader.load_contract(contract, resolve_refs=False),
        "compile_contract": lambda: loader.compile_contract(contract, resolve_refs=False),
        "load_with_overlay": lambda: loader.load_with_overlay(contract, resolve_refs=False),
        "load_with_overlay-env": lambda: loader.load_with_overlay(
            contract, "prod", resolve_refs=False
        ),
    }
    for label, call in raw_calls.items():
        with pytest.raises((RuntimeError, ValueError), match="root must be an object") as info:
            call()
        assert "contract.fluid.json" in str(info.value), label


@pytest.mark.parametrize(
    ("name", "text"),
    [
        ("prod.yaml", "- a\n"),
        ("prod.yaml", "just text\n"),
        ("prod.json", '[{"a": 1}]'),
        ("prod.json", '"text"'),
    ],
    ids=["yaml-list", "yaml-scalar", "json-list", "json-scalar"],
)
def test_a19_an_overlay_root_that_is_not_an_object_is_refused_naming_the_overlay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    name: str,
    text: str,
) -> None:
    from fluid_build.api.contract import ContractLoadError, load_contract

    monkeypatch.chdir(tmp_path)
    contract = _write_contract(tmp_path, _min_doc())
    _write(tmp_path / "overlays" / name, text)

    for label, call in {
        "load_with_overlay": lambda: loader.load_with_overlay(contract, "prod"),
        "compose_contract": lambda: loader.compose_contract(contract, "prod"),
    }.items():
        with pytest.raises((RuntimeError, ValueError)) as info:
            call()
        assert "root must be an object" in str(info.value), label
        assert name in str(info.value), label
    with pytest.raises(ContractLoadError) as api_info:
        load_contract(contract, env="prod")
    assert api_info.value.event == "contract_not_a_mapping"
    assert name in str(api_info.value)

    for argv in (
        ("validate", str(contract), "--env", "prod"),
        ("bundle", str(contract), "--env", "prod", "--out", str(tmp_path / "b.yaml")),
    ):
        code, output = _cli(capsys, *argv)
        assert code in (1, 2), (argv[0], output)
        assert "root must be an object" in output and name in output, argv[0]
        assert "AttributeError" not in output and "Traceback" not in output, argv[0]


def test_a20_a_root_level_ref_composes_an_object_sg19(tmp_path: Path) -> None:
    """SG-19 characterization: the root contract may itself be ``{$ref, ...siblings}``."""
    base = _min_doc()
    _write(tmp_path / "base.yaml", yaml.safe_dump(base, sort_keys=False))
    contract = _write(tmp_path / "contract.fluid.yaml", "$ref: ./base.yaml\nid: x.override\n")

    composed = loader.load_contract(contract)

    assert composed == {**base, "id": "x.override"}
    assert list(composed) == list(base)


def test_a20_a_root_level_ref_that_composes_a_list_is_not_a_contract_sg19(tmp_path: Path) -> None:
    _write(tmp_path / "list.yaml", "- a\n- b\n")
    plain = _write(tmp_path / "plain.fluid.yaml", "$ref: ./list.yaml\n")
    with_siblings = _write(tmp_path / "siblings.fluid.yaml", "$ref: ./list.yaml\nid: x\n")

    with pytest.raises(ValueError, match="root must be an object") as info:
        loader.load_contract(plain)
    assert "plain.fluid.yaml" in str(info.value) and "list" in str(info.value)
    with pytest.raises(RefResolutionError, match="the target is a list, not an object"):
        loader.load_contract(with_siblings)


def test_a21_a_ref_inside_a_fragment_is_relative_to_that_fragment(tmp_path: Path) -> None:
    _write(tmp_path / "sql" / "b.sql", "SELECT 7\n")
    _write(
        tmp_path / "fragments" / "builds" / "b.yaml",
        "id: b\nproperties:\n  sql: {$ref: ../../sql/b.sql}\n",
    )
    _write(
        tmp_path / "fragments" / "builds" / "wrong.yaml",
        "id: b\nproperties:\n  sql: {$ref: ./sql/b.sql}\n",
    )
    good = _write_contract(tmp_path, {"builds": [{"$ref": "./fragments/builds/b.yaml"}]})
    bad = _write_contract(
        tmp_path, {"builds": [{"$ref": "./fragments/builds/wrong.yaml"}]}, "bad.fluid.yaml"
    )

    assert loader.load_contract(good) == {
        "builds": [{"id": "b", "properties": {"sql": "SELECT 7\n"}}]
    }
    with pytest.raises(RefResolutionError, match="not found") as info:
        loader.load_contract(bad)
    message = str(info.value)
    assert "./sql/b.sql" in message
    assert str(tmp_path.resolve() / "fragments" / "builds" / "sql" / "b.sql") in message
    assert "wrong.yaml" in message


def test_a22_a_pointer_into_a_fragment_resolves_nested_refs_beside_that_fragment(
    tmp_path: Path,
) -> None:
    _write(tmp_path / "dir" / "f.yaml", "x:\n  $ref: ./g.yaml\ny: 2\n")
    _write(tmp_path / "dir" / "g.yaml", "from: dir/g\n")
    _write(tmp_path / "g.yaml", "from: root/g\n")
    contract = _write_contract(tmp_path, {"v": {"$ref": "./dir/f.yaml#/x"}})

    assert loader.load_contract(contract) == {"v": {"from": "dir/g"}}


def test_a23_a_missing_target_fails_the_same_way_with_and_without_siblings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    plain = _write_contract(tmp_path, {"a": {"$ref": "./missing.yaml"}}, "plain.fluid.yaml")
    sibling = _write_contract(
        tmp_path, {"a": {"$ref": "./missing.yaml", "x": 1}}, "sibling.fluid.yaml"
    )
    messages = []
    for contract in (plain, sibling):
        with pytest.raises(RefResolutionError, match="not found") as info:
            loader.load_contract(contract)
        messages.append(str(info.value).replace(contract.name, "<contract>"))
    assert messages[0] == messages[1]
    assert "./missing.yaml" in messages[0]
    assert str(tmp_path.resolve() / "missing.yaml") in messages[0]
    assert "at JSON pointer '/a'" in messages[0]

    for contract in (plain, sibling):
        code, output = _cli(capsys, "validate", str(contract))
        assert code == 1 and "./missing.yaml" in output and "not found" in output
        code, output = _cli(capsys, "bundle", str(contract), "--out", str(tmp_path / "b.yaml"))
        assert code == 2 and "./missing.yaml" in output and "not found" in output
    assert not (tmp_path / "b.yaml").exists()


def _prepare_failures(root: Path) -> Dict[str, Tuple[Dict[str, Any], str]]:
    """One ref that cannot be resolved per failure class of this area: ``(node, ref)``."""
    _write(root / "t.sql", "SELECT 1\n")
    _write(root / "l.yaml", "- a\n- b\n")
    _write(root / "latin.sql", data="SELECT 'caf\xe9'".encode("latin-1"))
    _write(root / "latin.yaml", data="k: caf\xe9\n".encode("latin-1"))
    _write(root / "bad.json", '{"k": ')
    _write(root / "tabs.yaml", "a:\n\tb: 1\n")
    _write(root / "multi.yaml", "a: 1\n---\nb: 2\n")
    _write(root / "empty.json", "")
    _write(root / "obj.yaml", "k: 1\n")
    _write(root / "big.md", data=b"a" * (5 * 1024 * 1024 + 1))
    (root / "dir").mkdir()
    return {
        "missing": ({"$ref": "./missing.yaml"}, "./missing.yaml"),
        "directory": ({"$ref": "./dir"}, "./dir"),
        "pointer-into-text": ({"$ref": "./t.sql#/a"}, "./t.sql#/a"),
        "siblings-on-text": ({"$ref": "./t.sql", "x": 1}, "./t.sql"),
        "siblings-on-list": ({"$ref": "./l.yaml", "x": 1}, "./l.yaml"),
        "text-not-utf8": ({"$ref": "./latin.sql"}, "./latin.sql"),
        "yaml-not-utf8": ({"$ref": "./latin.yaml"}, "./latin.yaml"),
        "invalid-json": ({"$ref": "./bad.json"}, "./bad.json"),
        "yaml-tabs": ({"$ref": "./tabs.yaml"}, "./tabs.yaml"),
        "multi-document": ({"$ref": "./multi.yaml"}, "./multi.yaml"),
        "empty-json": ({"$ref": "./empty.json"}, "./empty.json"),
        "pointer-segment-missing": ({"$ref": "./obj.yaml#/nope"}, "./obj.yaml#/nope"),
        "text-over-cap": ({"$ref": "./big.md"}, "./big.md"),
    }


@pytest.mark.parametrize(
    "case",
    [
        "missing",
        "directory",
        "pointer-into-text",
        "siblings-on-text",
        "siblings-on-list",
        "text-not-utf8",
        "yaml-not-utf8",
        "invalid-json",
        "yaml-tabs",
        "multi-document",
        "empty-json",
        "pointer-segment-missing",
        "text-over-cap",
    ],
)
def test_a23_every_ref_failure_names_the_ref_its_file_and_its_json_pointer(
    tmp_path: Path, case: str
) -> None:
    node, ref = _prepare_failures(tmp_path)[case]

    with pytest.raises(RefResolutionError) as info:
        _rfr({"a": [{"b": node}]}, tmp_path)

    message = str(info.value)
    assert ref in message
    assert "at JSON pointer '/a/0/b'" in message
    assert "contract.fluid.yaml" in message
    assert not isinstance(info.value, (UnicodeError, KeyError, AttributeError, TypeError))


@pytest.mark.parametrize("ref", [".", "./fragments/", "./fragments", "./dir.yaml"])
def test_a24_a_directory_is_never_a_target(tmp_path: Path, ref: str) -> None:
    (tmp_path / "fragments").mkdir()
    (tmp_path / "dir.yaml").mkdir()

    with pytest.raises(RefResolutionError, match="not a regular file") as info:
        _rfr({"x": {"$ref": ref}}, tmp_path)

    assert f"'{ref}'" in str(info.value)


def test_a25_the_suffix_is_the_last_one_and_a_dotfile_has_none_sg16(tmp_path: Path) -> None:
    """SG-16 characterization: ``x.sql.yaml`` is YAML, ``x.yaml.sql`` is text, and a file named
    exactly ``.sql`` has no suffix, so it is parsed (``Path('.sql').suffix == ''``)."""
    for name in ("x.sql.yaml", "x.yaml.sql", ".sql"):
        _write(tmp_path / name, "k: v\n")

    got = _rfr(
        {
            "a": {"$ref": "./x.sql.yaml"},
            "b": {"$ref": "./x.yaml.sql"},
            "c": {"$ref": "./.sql"},
        },
        tmp_path,
    )

    assert got == {"a": {"k": "v"}, "b": "k: v\n", "c": {"k": "v"}}


def test_a26_a_symlink_is_read_by_the_suffix_of_its_target_sg16(tmp_path: Path) -> None:
    """SG-16 characterization: the suffix of the resolved target decides, not the written name."""
    _write(tmp_path / "data.yaml", "k: v\n")
    _write(tmp_path / "plain.sql", "SELECT 2")
    try:
        os.symlink("data.yaml", tmp_path / "q.sql")
        os.symlink("plain.sql", tmp_path / "d.yaml")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available here")

    got = _rfr({"a": {"$ref": "./q.sql"}, "b": {"$ref": "./d.yaml"}}, tmp_path)

    assert got == {"a": {"k": "v"}, "b": "SELECT 2"}


def test_a27_text_where_an_object_belongs_composes_and_fails_validation_by_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    meta = "# Meta\n\nowner: me\n"
    _write(tmp_path / "meta.md", meta)
    doc = _min_doc()
    doc["metadata"] = {"$ref": "./meta.md"}
    contract = _write_contract(tmp_path, doc)

    assert loader.load_contract(contract)["metadata"] == meta

    code, output = _cli(capsys, "validate", str(contract))
    assert code == 1
    assert "meta.md" in output and "metadata" in output
    assert "is not of type 'object'" in output

    assert _bundle(capsys, contract, tmp_path / "b.yaml")["metadata"] == meta

    code, output = _cli(
        capsys,
        "generate",
        "standard",
        str(contract),
        "--format",
        "odcs",
        "--out",
        str(tmp_path / "x.odcs.yaml"),
    )
    assert code in (0, 1)
    assert "Traceback" not in output and "cli_unhandled_exception" not in output


def test_a28_every_file_is_listed_once_in_first_read_order(tmp_path: Path) -> None:
    from fluid_build.api.contract import load_contract

    _write(tmp_path / "q.sql", "SELECT 1")
    _write(tmp_path / "rules.yaml", "- a\n- b\n")
    _write(tmp_path / "scalar.yaml", "hello\n")
    _write(tmp_path / "sub" / "deep.yaml", "k: v\n")
    doc = {
        "id": "x",
        "a": {"$ref": "./q.sql"},
        "b": {"$ref": "./rules.yaml"},
        "c": {"$ref": "./scalar.yaml"},
        "a2": {"$ref": "./q.sql"},
        "b2": {"$ref": "./sub/../rules.yaml"},
        "c2": {"$ref": "scalar.yaml"},
        "d": [{"$ref": "./sub/deep.yaml"}, {"$ref": "./sub/deep.yaml"}],
    }
    contract = _write_contract(tmp_path, doc)

    files = load_contract(contract).files

    root = tmp_path.resolve()
    assert files == (
        contract.resolve(),
        root / "q.sql",
        root / "rules.yaml",
        root / "scalar.yaml",
        root / "sub" / "deep.yaml",
    )


def test_a29_a_tgz_of_text_refs_is_the_inline_twins_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    manifests = {}
    for kind in ("inline", "files"):
        root = tmp_path / kind
        doc = _min_doc()
        doc["builds"][0]["properties"]["sql"] = _SQL
        doc["description"]["usage"] = _USAGE
        if kind == "files":
            _write(root / "sql" / "t.sql", _SQL)
            _write(root / "docs" / "usage.md", _USAGE)
            doc["builds"][0]["properties"]["sql"] = {"$ref": "./sql/t.sql"}
            doc["description"]["usage"] = {"$ref": "./docs/usage.md"}
        contract = _write_contract(root, doc)
        out = root / "bundle.tgz"
        code, output = _cli(capsys, "bundle", str(contract), "--format", "tgz", "--out", str(out))
        assert code == 0, output
        with tarfile.open(out) as archive:
            members = sorted(member.name for member in archive.getmembers())
            manifest = json.load(archive.extractfile("MANIFEST.json"))  # type: ignore[arg-type]
            resolved = yaml.safe_load(archive.extractfile("contract.resolved.yaml"))  # type: ignore[arg-type]
        assert members == ["MANIFEST.json", "contract.resolved.json", "contract.resolved.yaml"]
        assert resolved["builds"][0]["properties"]["sql"] == _SQL
        assert resolved["description"]["usage"] == _USAGE
        assert _ref_paths(resolved) == []
        manifests[kind] = manifest

    assert manifests["files"]["digest"] == manifests["inline"]["digest"]
    assert manifests["files"]["files"] == manifests["inline"]["files"]


@pytest.mark.parametrize("name", ["a.yml", "b.YAML", "c.Yml", "d.JSON", "e.Json"])
def test_a30_yaml_and_json_are_chosen_by_suffix_without_case(tmp_path: Path, name: str) -> None:
    _write(tmp_path / name, '{"k": 1}' if name.lower().endswith("json") else "k: 1\n")

    assert _rfr({"x": {"$ref": f"./{name}"}}, tmp_path) == {"x": {"k": 1}}


def test_a31_text_that_mentions_a_ref_stays_verbatim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    sql = "-- see $ref: ./x.yaml\nSELECT '$ref: ./y.yaml' AS s\n"
    usage = "Use `$ref: ./x.yaml` to compose. {$ref: ./z.yaml}\n"
    _write(tmp_path / "t.sql", sql)
    _write(tmp_path / "usage.md", usage)
    doc = _min_doc()
    doc["builds"][0]["properties"]["sql"] = {"$ref": "./t.sql"}
    doc["description"]["usage"] = {"$ref": "./usage.md"}
    contract = _write_contract(tmp_path, doc)

    loaded = loader.load_contract(contract)
    assert loaded["builds"][0]["properties"]["sql"] == sql
    assert loaded["description"]["usage"] == usage
    bundled = _bundle(capsys, contract, tmp_path / "bundled.yaml")
    assert bundled["builds"][0]["properties"]["sql"] == sql
    assert bundled["description"]["usage"] == usage
    assert _ref_paths(bundled) == []


@pytest.mark.parametrize(
    ("language", "suffix"),
    [
        ("sql", ".sql"),
        ("flink_sql", ".sql"),
        ("pyspark", ".py"),
        ("scala", ".scala"),
        ("python", ".py"),
        ("r", ".r"),
    ],
)
def test_a32_every_build_language_validates_and_bundles_its_text_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    language: str,
    suffix: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    code_text = f"-- {language}: code with: a colon\n\tindented line # not a comment\n"
    _write(tmp_path / f"code{suffix}", code_text)
    doc = _min_doc()
    doc["builds"][0]["properties"]["sql"] = {"$ref": f"./code{suffix}"}
    doc["builds"][0]["properties"]["language"] = language
    contract = _write_contract(tmp_path, doc)

    code, output = _cli(capsys, "validate", str(contract))
    assert code == 0, output
    bundled = _bundle(capsys, contract, tmp_path / "bundled.yaml")
    assert bundled["builds"][0]["properties"]["sql"] == code_text
