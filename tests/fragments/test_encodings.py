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

Scenarios that cite a SPEC GAP (N-01 JSON, N-13 collision, N-17, N-22
percent-encoding) pin what the code does today, because the spec gives them no
oracle. N-09 (SG-18) is decided: a key written twice is refused. A test marked
``xfail(strict=True)`` asserts the spec and names the bug it shows.
"""

from __future__ import annotations

import json
import logging
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


def _flat(text: str) -> str:
    """Whitespace-normalised: Rich wraps long lines in the CLI's error panel."""
    return " ".join(text.split())


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


def _unique(first: int) -> str:
    """The tail of a YAML duplicate-key error whose first occurrence is at line ``first``."""
    return f"(first at line {first}; YAML 1.2 requires unique keys); remove one of them"


def test_n09_a_duplicate_key_in_a_fragment_is_refused_naming_file_line_and_key_sg18(
    tmp_path: Path,
) -> None:
    """SG-18, decided: PyYAML keeps the last value and drops the other without a word; every
    file the composition reads refuses a key written twice instead."""
    dup = _write(tmp_path / "dup.yaml", "a: 1\nb: {c: 1}\na: 2\n")

    with pytest.raises(RefResolutionError) as info:
        _rfr({"x": {"$ref": "./dup.yaml"}}, tmp_path)

    message = str(info.value)
    assert f"{dup}:3: duplicate key 'a' {_unique(1)}" in message
    assert "$ref target './dup.yaml' at JSON pointer '/x'" in message


def test_n09_two_ref_keys_in_one_mapping_are_a_duplicate_key_and_no_file_is_read_sg18(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SG-18, decided. The parser used to drop the first ``$ref``, so the file a reviewer
    reads first was never read and nothing said so; now neither is read."""
    _write(tmp_path / "a.yaml", "from: a\n")
    _write(tmp_path / "b.yaml", "from: b\n")
    contract = _write(tmp_path / "contract.fluid.yaml", "x:\n  $ref: ./a.yaml\n  $ref: ./b.yaml\n")
    reads: List[str] = []
    from fluid_build.util import safe_yaml

    real = safe_yaml.read_bounded_file

    def spy(path: Path, *args: Any, **kwargs: Any) -> Any:
        reads.append(Path(path).name)
        return real(path, *args, **kwargs)

    monkeypatch.setattr(safe_yaml, "read_bounded_file", spy)

    with pytest.raises(RuntimeError) as info:
        loader.compose_contract(contract)

    assert str(info.value) == f"{contract}:3: duplicate key '$ref' {_unique(2)}"
    assert reads == ["contract.fluid.yaml"]


_DUP_YAML = "a: 1\nnested:\n  k: 1\n  k: 2\n"


@pytest.mark.parametrize(
    ("where", "name", "text", "shown"),
    [
        ("root", "contract.fluid.yaml", "id: x\nname: n\nid: y\n", ":3: duplicate key 'id'"),
        ("root", "contract.fluid.json", '{"id": "x", "id": "y"}', ": duplicate key 'id'"),
        ("overlay", "overlays/dev.yaml", "id: x\nid: y\n", ":2: duplicate key 'id'"),
        ("fragment", "f.yaml", _DUP_YAML, ":4: duplicate key 'k'"),
        ("fragment", "f.json", '{"a": {"k": 1, "k": 2}}', ": duplicate key 'k'"),
        ("fragment", "f.avsc", '{"type": "record", "type": "enum"}', ": duplicate key 'type'"),
        ("fragment", "f.avsc", "# YAML, not JSON\ntype: record\ntype: enum\n", ":3:"),
    ],
    ids=[
        "yaml-root",
        "json-root",
        "overlay",
        "yaml-fragment",
        "json-fragment",
        "avsc",
        "avsc-yaml",
    ],
)
def test_n09_every_file_the_composition_reads_refuses_a_duplicate_key_sg18(
    tmp_path: Path, where: str, name: str, text: str, shown: str
) -> None:
    file = _write(tmp_path / name, text)
    contract = tmp_path / "contract.fluid.yaml"
    if where == "fragment":
        _write(contract, f"id: x\nx: {{$ref: ./{name}}}\n")
    elif where == "overlay":
        _write(contract, "id: x\n")
    else:
        contract = file

    with pytest.raises((RuntimeError, RefResolutionError)) as info:
        loader.compose_contract(contract, "dev" if where == "overlay" else None)

    message = str(info.value)
    assert f"{file}{shown}" in message
    assert "remove one of them" in message
    if name.endswith(".json") or (name.endswith(".avsc") and "{" in text):
        assert "RFC 8259" in message  # JSON gives no line
    else:
        assert "YAML 1.2 requires unique keys" in message


def test_n09_contract_text_and_a_bundle_refuse_a_duplicate_key_sg18(tmp_path: Path) -> None:
    """``parse_contract_text`` reads the library's in-memory text and a bundle's resolved
    contract: the same rule, the error naming the bundle member it was read from."""
    from fluid_build.api.contract import ContractLoadError, load_contract, load_contract_from_text
    from fluid_build.forge.core.bundle import build_manifest, write_tgz

    with pytest.raises(ContractLoadError) as text_info:
        load_contract_from_text("id: x\nid: y\n")
    assert text_info.value.event == "contract_parse_failed"
    assert f"line 2: duplicate key 'id' {_unique(1)}" in str(text_info.value)
    assert "Failed to parse contract text" not in str(text_info.value), "said once"

    files = {"contract.resolved.yaml": b"id: x\nkind: DataProduct\nid: y\n"}
    tgz = tmp_path / "b.tgz"
    write_tgz(tgz, {**files, "MANIFEST.json": json.dumps(build_manifest(files)).encode()})
    with pytest.raises(ContractLoadError) as bundle_info:
        load_contract(tgz)
    assert bundle_info.value.event == "bundle_load_failed"
    assert f"contract.resolved.yaml:3: duplicate key 'id' {_unique(1)}" in str(bundle_info.value)
    assert "Failed to parse contract text" not in str(bundle_info.value)


def test_n09_validate_and_bundle_fail_on_a_duplicate_key_naming_it_sg18(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    doc = _min_doc()
    doc["metadata"]["owner"] = {"$ref": "./owner.yaml"}
    contract = _write_contract(tmp_path, doc)
    owner = _write(tmp_path / "owner.yaml", "team: a\nemail: a@example.com\nteam: b\n")
    out = tmp_path / "b.yaml"

    code, output = _cli(capsys, "validate", str(contract))
    assert code == 1, output
    assert f"{owner}:3: duplicate key 'team'" in _flat(output)

    code, output = _cli(capsys, "bundle", str(contract), "--out", str(out))
    assert code == 2, output
    assert f"{owner}:3: duplicate key 'team'" in _flat(output)
    assert not out.exists()


@pytest.mark.parametrize("emit", ["odcs", "odps-bitol", "opds"])
def test_n09_generate_artifacts_refuses_a_duplicate_key_in_the_root_contract_sg18(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], emit: str
) -> None:
    """``fluid generate artifacts`` read a contract without ``$ref`` with PyYAML's
    last-wins: the second ``accessPolicy``, granting everyone read and write, was the one
    ``--emit odcs`` and ``odps-bitol`` exported while ``validate`` refused the file, and
    ``--emit opds`` ended in an unhandled exception. Each now fails as the others do."""
    text = yaml.safe_dump(_min_doc(), sort_keys=False)
    text += "accessPolicy: {grants: [{principal: 'group:analysts', permissions: [read]}]}\n"
    text += "accessPolicy: {grants: [{principal: '*', permissions: [read, write]}]}\n"
    contract = _write(tmp_path / "contract.fluid.yaml", text)
    out = tmp_path / "artifacts"
    second = text.count("\n")

    code, output = _cli(
        capsys, "generate", "artifacts", str(contract), "--emit", emit, "--out", str(out)
    )

    assert code == 1, output
    assert "generate_artifacts_failed" in output
    assert f"{contract}:{second}: duplicate key 'accessPolicy' (first at line {second - 1};" in (
        _flat(output)
    )
    assert [p for p in out.rglob("*") if p.is_file()] == [], "nothing is exported"


def test_n09_a_merged_key_written_again_is_an_override_not_a_duplicate_sg18(
    tmp_path: Path,
) -> None:
    """The keys a mapping writes are checked before ``<<`` expands, so overriding a merged
    default is not a duplicate, also for a merge source written inline and used again; and
    a ``<<`` is a merge directive, not a data key, so a mapping may write two."""
    _write(
        tmp_path / "m.yaml",
        "base: &base {a: 1, b: 1}\n"
        "top:\n  <<: &mid\n    <<: *base\n    a: 2\n  b: 3\n"
        "again: *mid\n",
    )

    _write(tmp_path / "two.yaml", "a: &a {x: 1}\nb: &b {y: 2}\nboth:\n  <<: *a\n  <<: *b\n  z: 3\n")

    got = _rfr({"x": {"$ref": "./m.yaml"}, "y": {"$ref": "./two.yaml"}}, tmp_path)

    assert got["x"]["top"] == {"a": 2, "b": 3}
    assert got["x"]["again"] == {"a": 2, "b": 1}
    assert got["y"]["both"] == {"x": 1, "y": 2, "z": 3}, "two << lines: both merges apply"


def test_n09_a_workspace_file_with_a_duplicate_ref_root_is_refused_naming_the_key_sg18(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """SG-18, decided: ``fluid.workspace.yaml`` is a file the composition reads, held to
    unique keys. A ``ref_root`` written twice is refused as a file that cannot be parsed
    is: the contract keeps its own directory, and the WARNING and the escape error name
    the file, the line and the key, where the last value used to win without a word."""
    monkeypatch.delenv("FLUID_IGNORE_WORKSPACE_REF_ROOT", raising=False)
    ws = tmp_path / "ws"
    _write(ws / "fluid.workspace.yaml", "ref_root: nowhere\nref_root: .\n")
    _write(ws / "shared" / "team.yaml", "team: shared\n")
    contract = _write(ws / "p" / "contract.fluid.yaml", "x: {$ref: ../shared/team.yaml}\n")
    named = f"fluid.workspace.yaml:2: duplicate key 'ref_root' {_unique(1)}"

    with caplog.at_level(logging.WARNING, logger="fluid.loader"):
        with pytest.raises(RefResolutionError, match="escapes the ref root") as info:
            loader.load_contract(contract)

    assert named in str(info.value)
    (warning,) = [r for r in caplog.records if r.event == "workspace_ref_root_ignored"]  # type: ignore[attr-defined]
    assert named in warning.getMessage()


def test_n09_a_workspace_that_declares_environments_with_a_duplicate_key_refuses_sg18(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The workspace's ``expected-environments`` decide whether ``--env`` without an overlay
    is refused. Written with a key twice, which environments it declares cannot be told:
    the duplicate is the error, never "nothing declared", which would let the env pass."""
    monkeypatch.delenv("FLUID_IGNORE_WORKSPACE_REF_ROOT", raising=False)
    ws = tmp_path / "ws"
    workspace = _write(
        ws / "fluid.workspace.yaml",
        "expected-environments:\n  p: [prod]\n  p: [staging]\n",
    )
    contract = _write_contract(ws / "p", _min_doc())

    with pytest.raises(RuntimeError) as info:
        loader.load_with_overlay(contract, "prod")

    assert str(info.value) == f"{workspace}:3: duplicate key 'p' {_unique(2)}"


def test_n09_readers_off_the_composition_path_keep_the_last_value(tmp_path: Path) -> None:
    """Only what the composition reads is held to unique keys. The other readers of YAML
    keep PyYAML's last-wins, among them the workspace defaults ``fluid init`` and ``fluid
    forge`` read (``cli.workspace_config``), which parse the file with ``yaml.safe_load``."""
    from fluid_build.cli.workspace_config import load_workspace_config
    from fluid_build.util.safe_yaml import load_yaml_safe

    _write(tmp_path / "fluid.workspace.yaml", "name: first\nname: last\n")

    assert load_yaml_safe("a: 1\na: 2\n") == {"a": 2}
    assert load_workspace_config(tmp_path).name == "last"


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


_YAML_11_KEYS = "200: a\nyes: b\nnull: d\n1.5: e\n"


def test_n13_yaml_1_1_keys_in_a_fragment_equal_the_same_keys_written_inline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    expected = {200: "a", True: "b", None: "d", 1.5: "e"}
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


def test_n13_two_yaml_1_1_spellings_of_one_key_are_a_duplicate_inline_and_in_a_fragment_sg18(
    tmp_path: Path,
) -> None:
    """``yes`` and ``on`` are both the key ``True`` in YAML 1.1: a dict keeps one of them, so
    the pair is refused like any key written twice, naming both spellings."""
    _write(tmp_path / "keys.yaml", "yes: b\non: c\n")
    inline = _write(tmp_path / "contract.fluid.yaml", "id: x\nx:\n  yes: b\n  on: c\n")

    with pytest.raises(RefResolutionError) as fragment_info:
        _rfr({"x": {"$ref": "./keys.yaml"}}, tmp_path)
    with pytest.raises(RuntimeError) as inline_info:
        loader.load_contract(inline)

    same = "duplicate key 'on' (the same key as 'yes' at line"
    assert f"keys.yaml:2: {same} 1;" in str(fragment_info.value)
    assert f"{inline}:4: {same} 3;" in str(inline_info.value)


def test_n13_a_string_sibling_does_not_merge_with_an_int_key_sg03(tmp_path: Path) -> None:
    """SG-03 characterization: ``"200"`` and ``200`` are different keys, so both stay."""
    _write(tmp_path / "k200.yaml", "200: a\nother: 1\n")

    got = _rfr({"x": {"$ref": "./k200.yaml", "200": "x"}}, tmp_path)["x"]

    assert got == {200: "a", "other": 1, "200": "x"}


@pytest.mark.parametrize(
    ("keys", "shown", "name"),
    [
        ("200: a\n    '200': b", "200 and '200'", '"200"'),
        ("true: a\n    'true': b", "true and 'true'", '"true"'),
        ("'null': a\n    null: b", "'null' and null", '"null"'),
    ],
    ids=["int", "bool", "null"],
)
def test_n13_bundle_json_refuses_two_keys_json_writes_as_one_name(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], keys: str, shown: str, name: str
) -> None:
    """YAML keeps ``200`` and ``"200"`` apart, and so does the composition; JSON writes both
    as the name ``"200"``. ``fluid bundle --format json`` wrote that name twice, so every
    reader of contracts refused the bundle's own output (0.25.0 read one key and dropped
    the other). It is refused at the writer, naming the keys and where they are, and
    nothing is written; ``--format yaml`` keeps both keys."""
    from fluid_build.cli import main

    base = yaml.safe_dump(_min_doc(), sort_keys=False)
    contract = _write(
        tmp_path / "contract.fluid.yaml", base + f"extensions:\n  status:\n    {keys}\n"
    )
    out = tmp_path / "b.json"
    refused = (
        f"the mapping at JSON pointer '/extensions/status' has the keys {shown}, which JSON "
        f"writes as one name, {name}; a reader keeps only one of them"
    )

    code, output = _cli(capsys, "bundle", str(contract), "--format", "json", "--out", str(out))
    assert code == 2, output
    assert refused in _flat(output) and "Use --format yaml" in output
    assert not out.exists()

    capsys.readouterr()
    assert main(["bundle", str(contract), "--format", "json"]) == 2
    assert '"fluidVersion"' not in capsys.readouterr().out, "nothing written to stdout"

    kept = _bundle(capsys, contract, tmp_path / "b.yaml")["extensions"]["status"]
    assert kept == loader.load_contract(contract)["extensions"]["status"]
    assert len(kept) == 2
    assert loader.load_contract(tmp_path / "b.yaml")["extensions"]["status"] == kept


def test_n13_a_plain_equals_key_loads_as_before_and_the_contract_validates(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``=`` is YAML 1.1's "value" key, which PyYAML reads as the string ``"="``:
    ``{=: eq}`` is ``{"=": "eq"}``, in the contract and in a fragment, as 0.25.0 read
    it. The unique-key check constructed the key before PyYAML retags it, and refused it."""
    _write(tmp_path / "ops.yaml", "=: eq\n'!=': ne\n")
    doc = _min_doc()
    doc["builds"][0]["properties"]["parameters"] = {
        "operators": "INLINE",
        "more": {"$ref": "./ops.yaml"},
    }
    text = yaml.safe_dump(doc, sort_keys=False).replace("operators: INLINE", "operators: {=: eq}")
    assert "operators: {=: eq}" in text
    contract = _write(tmp_path / "contract.fluid.yaml", text)

    parameters = loader.load_contract(contract)["builds"][0]["properties"]["parameters"]

    assert parameters == {"operators": {"=": "eq"}, "more": {"=": "eq", "!=": "ne"}}
    code, output = _cli(capsys, "validate", str(contract))
    assert code == 0, output


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


# ── SG-05: a payload's own $ref keys, written $$ref ─────────────────────

#: A JSON Schema payload with refs of its own. ``#/$defs/...`` names nothing in the
#: contract, so resolving any of them as a contract ref would fail.
_PAYLOAD = {
    "$$ref": "#/$defs/Order",
    "$defs": {"Order": {"type": "object", "properties": {"id": {"$$ref": "#/$defs/Id"}}}},
}
_PAYLOAD_COMPOSED = {
    "$ref": "#/$defs/Order",
    "$defs": {"Order": {"type": "object", "properties": {"id": {"$ref": "#/$defs/Id"}}}},
}


def _payload_contract(root: Path) -> Path:
    """MIN with the payload in ``parameters`` (and a ``$$$ref`` and a list), in a fragment
    and in the dev overlay."""
    doc = _min_doc()
    doc["builds"][0]["properties"]["parameters"] = {
        "payloadSchema": _PAYLOAD,
        "literal": {"$$$ref": "two dollars"},
        "list": [{"$$ref": "./missing.yaml"}],
    }
    doc["extensions"] = {"fragment": {"$ref": "./ext.yaml"}}
    _write(root / "ext.yaml", "schema:\n  $$ref: ./nowhere.yaml\n")
    _write(root / "overlays" / "dev.yaml", 'extensions: {overlay: {$$ref: "#/none"}}\n')
    return _write_contract(root, doc)


def test_n16_a_double_dollar_key_is_a_literal_ref_key_and_is_never_resolved_sg05(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """SG-05, decided: ``$$ref`` is the literal key ``$ref`` in the composed contract, in
    the root, a fragment and an overlay alike; one ``$`` is dropped per level."""
    from fluid_build.api.contract import load_contract

    monkeypatch.chdir(tmp_path)
    contract = _payload_contract(tmp_path)

    composed = loader.compose_contract(contract, "dev")

    params = composed.document["builds"][0]["properties"]["parameters"]
    assert params == {
        "payloadSchema": _PAYLOAD_COMPOSED,
        "literal": {"$$ref": "two dollars"},
        "list": [{"$ref": "./missing.yaml"}],
    }
    assert composed.document["extensions"] == {
        "fragment": {"schema": {"$ref": "./nowhere.yaml"}},
        "overlay": {"$ref": "#/none"},
    }
    assert [p.name for p in composed.files] == ["contract.fluid.yaml", "ext.yaml", "dev.yaml"]
    loaded = load_contract(contract, env="dev")
    assert loaded.contract == composed.document
    assert loaded.unresolved_refs == (), "a payload's $ref is no unresolved reference"
    assert loaded.origin_of(["builds", 0, "properties", "parameters", "payloadSchema", "$ref"])
    code, output = _cli(capsys, "validate", str(contract), "--env", "dev")
    assert code == 0, output


def test_n16_a_payload_ref_is_still_a_contract_reference_unless_escaped_sg05(
    tmp_path: Path,
) -> None:
    doc = _min_doc()
    doc["builds"][0]["properties"]["parameters"] = {"payloadSchema": {"$ref": "#/$defs/Order"}}
    contract = _write_contract(tmp_path, doc)

    with pytest.raises(RefResolutionError, match="'/\\$defs/Order' in the composed contract"):
        loader.load_contract(contract)


def test_n16_the_escape_is_read_last_on_every_path_of_the_composition_sg05() -> None:
    """After the check that no ``$ref`` is left, also for a document without any ``$ref``
    (the pointer pass returns early), and only for a document that is composed."""
    from fluid_build.api.contract import load_contract_from_dict

    plain = load_contract_from_dict({"id": "x", "p": {"$$ref": "#/q", "$$$ref": 1}})
    assert plain.contract == {"id": "x", "p": {"$ref": "#/q", "$$ref": 1}}
    assert plain.unresolved_refs == ()

    pointed = load_contract_from_dict(
        {"s": {"$$ref": "#/x", "k": 1}, "copy": {"$ref": "#/s"}, "key": {"$ref": "#/s/$$ref"}}
    )
    assert pointed.contract["copy"] == {"$ref": "#/x", "k": 1}
    assert pointed.contract["key"] == "#/x", "a pointer names an escaped key as written"
    assert pointed.unresolved_refs == ()

    names_files = load_contract_from_dict({"x": {"$ref": "./f.yaml"}, "p": {"$$ref": "#/q"}})
    assert names_files.contract == {"x": {"$ref": "./f.yaml"}, "p": {"$$ref": "#/q"}}
    assert names_files.unresolved_refs == ("./f.yaml",), "nothing is composed, nor unescaped"


def _bundle_text(capsys: pytest.CaptureFixture[str], contract: Path, out: Path, *flags: str) -> str:
    code, output = _cli(capsys, "bundle", str(contract), "--out", str(out), *flags)
    assert code == 0, output
    return out.read_text(encoding="utf-8")


def test_n16_a_bundle_writes_the_escape_back_and_composes_to_the_same_contract_sg05(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A bundle stays contract input: bundle(original) composed again == compose(original),
    in YAML, JSON and the tgz, and a plan made from any of them carries the payload's
    ``$ref`` as ``$ref``."""
    from fluid_build.api.contract import load_contract

    monkeypatch.chdir(tmp_path)
    root = tmp_path / "src"
    contract = _payload_contract(root)
    original = loader.compose_contract(contract, "dev").document

    for name, flags in (("b.yaml", ()), ("b.json", ("--format", "json"))):
        text = _bundle_text(capsys, contract, tmp_path / name, "--env", "dev", *flags)
        written = json.loads(text) if name.endswith(".json") else yaml.safe_load(text)
        assert written["builds"][0]["properties"]["parameters"]["payloadSchema"] == _PAYLOAD
        assert written["builds"][0]["properties"]["parameters"]["literal"] == {
            "$$$ref": "two dollars"
        }
        assert loader.compose_contract(tmp_path / name).document == original

    code, output = _cli(
        capsys, "bundle", str(contract), "--env", "dev", "--format", "tgz", "--out", "b.tgz"
    )
    assert code == 0, output
    with tarfile.open(tmp_path / "b.tgz") as archive:
        for member in ("contract.resolved.yaml", "contract.resolved.json"):
            text = archive.extractfile(member).read().decode()  # type: ignore[union-attr]
            assert '"#/$defs/Order"' in text or "'#/$defs/Order'" in text
            assert "$$ref" in text and "$$$ref" in text
    loaded = load_contract(tmp_path / "b.tgz", env="dev")
    assert loaded.contract == original
    assert loaded.unresolved_refs == ()
    code, output = _cli(capsys, "validate", str(tmp_path / "b.tgz"))
    assert code == 0, output

    for label, source in (("contract", contract), ("tgz", tmp_path / "b.tgz")):
        code, output = _cli(capsys, "plan", str(source), "--env", "dev", "--out", f"{label}.json")
        assert code == 0, output
        planned = json.loads((tmp_path / f"{label}.json").read_text(encoding="utf-8"))
        assert planned["contract"]["builds"][0]["properties"]["parameters"] == (
            original["builds"][0]["properties"]["parameters"]
        )


def test_n16_an_openapi_document_moved_to_sources_keeps_its_own_refs_sg05(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``fluid bundle --format tgz`` moves an expose's inline OpenAPI to ``sources/openapi``
    after it composes, so that file holds the document as OpenAPI reads it: ``$ref``."""
    from fluid_build.api.contract import load_contract

    doc = _min_doc()
    doc["exposes"][0]["openapi"] = {
        "openapi": "3.1.0",
        "paths": {"/o": {"get": {"responses": {"200": {"$$ref": "#/components/responses/Ok"}}}}},
    }
    contract = _write_contract(tmp_path, doc)
    tgz = tmp_path / "b.tgz"

    code, output = _cli(capsys, "bundle", str(contract), "--format", "tgz", "--out", str(tgz))
    assert code == 0, output

    with tarfile.open(tgz) as archive:
        (moved,) = [n for n in archive.getnames() if n.startswith("sources/openapi/")]
        source = yaml.safe_load(archive.extractfile(moved))  # type: ignore[arg-type]
        resolved = yaml.safe_load(archive.extractfile("contract.resolved.yaml"))  # type: ignore[arg-type]
    ok = {"$ref": "#/components/responses/Ok"}
    assert source["paths"]["/o"]["get"]["responses"]["200"] == ok
    assert resolved["exposes"][0]["openapi"] == {"$source": moved}
    assert load_contract(tgz).contract["exposes"][0]["openapi"]["paths"]["/o"]["get"] == {
        "responses": {"200": ok}
    }


def _hand_made_bundle(tmp_path: Path, resolved: str) -> Path:
    """A tgz with a valid MANIFEST whose ``contract.resolved.yaml`` is ``resolved``."""
    from fluid_build.forge.core.bundle import build_manifest, write_tgz

    files = {"contract.resolved.yaml": resolved.encode()}
    tgz = tmp_path / "hand.tgz"
    write_tgz(tgz, {**files, "MANIFEST.json": json.dumps(build_manifest(files)).encode()})
    return tgz


def test_n16_a_bare_ref_in_a_hand_made_bundle_is_listed_and_one_beside_its_escape_refused(
    tmp_path: Path,
) -> None:
    """``fluid bundle`` never writes a bare ``$ref``: one a bundle holds was written by hand
    (or by a release before the escape) and resolves nothing. It stays, and is listed."""
    from fluid_build.api.contract import ContractLoadError, load_contract

    frozen = load_contract(
        _hand_made_bundle(tmp_path, "id: x\na: {$ref: ./x.yaml}\nb: {$$ref: y}\n")
    )
    assert frozen.contract == {"id": "x", "a": {"$ref": "./x.yaml"}, "b": {"$ref": "y"}}
    assert frozen.unresolved_refs == ("./x.yaml",)

    with pytest.raises(ContractLoadError) as info:
        load_contract(_hand_made_bundle(tmp_path, "id: x\na: {$ref: ./x.yaml, $$ref: y}\n"))
    assert info.value.event == "bundle_load_failed"
    assert "'/a' holds both '$$ref' and '$ref', which both read as the key '$ref'" in str(
        info.value
    )


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


def test_n24_a_file_refs_pointer_without_a_slash_names_a_key_and_is_deprecated(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """N-24, decided: a file ref's fragment need not start with '/' yet. ``./a#b.yaml``
    reads key ``b.yaml`` of file ``a``, and ``./x.yaml#key`` key ``key``, as contracts
    0.24.0 loaded did, each with a deprecation WARNING naming the fix; only a
    same-document ``#key`` is refused (C-12)."""
    _write(tmp_path / "a", "b.yaml: KEY-OF-A\nother: 1\n")
    _write(tmp_path / "x.yaml", "key: KEY-OF-X\n")

    with caplog.at_level(logging.WARNING, logger="fluid.loader"):
        assert _rfr({"x": {"$ref": "./a#b.yaml"}}, tmp_path) == {"x": "KEY-OF-A"}
        assert _rfr({"x": {"$ref": "./x.yaml#key"}}, tmp_path) == {"x": "KEY-OF-X"}

    deprecated = {
        r.ref: r.getMessage()  # type: ignore[attr-defined]
        for r in caplog.records
        if getattr(r, "event", None) == "ref_pointer_without_slash"
    }
    assert set(deprecated) == {"./a#b.yaml", "./x.yaml#key"}
    assert "Write './a#/b.yaml'" in deprecated["./a#b.yaml"]
    assert "Write './x.yaml#/key'" in deprecated["./x.yaml#key"]
    assert all("a later minor release will refuse it" in m for m in deprecated.values())


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
