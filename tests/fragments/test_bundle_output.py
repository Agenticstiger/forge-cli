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

"""What ``fluid bundle`` writes, and what it says when it cannot (N-17, S2-F05, G-20).

The composed contract is written as YAML (``--out b.yaml``), as JSON, and as a
tgz that carries both ``contract.resolved.yaml`` and ``contract.resolved.json``.
A text that holds NEL (U+0085), LS (U+2028) or PS (U+2029) must read back from
each exactly as the loader composed it: PyYAML folds a bare NEL into a space.
Every other value is written as PyYAML always wrote it.
"""

from __future__ import annotations

import json
import logging
import tarfile
from pathlib import Path
from typing import Any, Dict, List

import pytest
import yaml

from fluid_build import loader

_SEPARATORS = {"nel": "\x85", "ls": "\u2028", "ps": "\u2029"}


@pytest.fixture(autouse=True)
def _no_ambient_ref_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FLUID_REF_ROOT", raising=False)
    monkeypatch.setenv("FLUID_IGNORE_WORKSPACE_REF_ROOT", "1")


def _doc(**extensions: Any) -> Dict[str, Any]:
    return {
        "fluidVersion": "0.7.6",
        "kind": "DataProduct",
        "id": "refs.bundle",
        "name": "Bundle",
        "domain": "ops",
        "metadata": {"layer": "Silver", "owner": {"team": "t", "email": "t@example.com"}},
        "exposes": [
            {
                "exposeId": "out",
                "kind": "table",
                "binding": {
                    "platform": "local",
                    "format": "csv",
                    "location": {"path": "o/out.csv"},
                },
                "contract": {"schema": [{"name": "id", "type": "integer"}]},
            }
        ],
        "extensions": extensions,
    }


def _json_contract(root: Path, document: Dict[str, Any]) -> Path:
    """A JSON contract: the separators are in the file as they are, not escaped by a dumper."""
    root.mkdir(parents=True, exist_ok=True)
    path = root / "contract.fluid.json"
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    return path


def _bundle(capsys: pytest.CaptureFixture, *argv: Any) -> int:
    from fluid_build.cli import main

    capsys.readouterr()
    return main(["bundle", *[str(a) for a in argv]])


@pytest.mark.parametrize("name", sorted(_SEPARATORS))
def test_n17_a_line_separator_in_a_value_or_a_key_is_what_every_form_gives_back(
    tmp_path: Path, capsys: pytest.CaptureFixture, name: str
) -> None:
    sep = _SEPARATORS[name]
    value = f"SELECT 'a{sep}b'\n"
    path = _json_contract(
        tmp_path / "p", _doc(sql=value, nested={f"key{sep}": [f"{sep}first", f"last{sep}"]})
    )
    expected = loader.load_contract(path)["extensions"]
    assert expected["sql"] == value

    assert _bundle(capsys, path, "--out", tmp_path / "b.yaml") == 0
    assert _bundle(capsys, path, "--format", "tgz", "--out", tmp_path / "b.tgz") == 0

    assert yaml.safe_load((tmp_path / "b.yaml").read_text("utf-8"))["extensions"] == expected
    with tarfile.open(tmp_path / "b.tgz") as archive:
        from_yaml = yaml.safe_load(archive.extractfile("contract.resolved.yaml"))  # type: ignore[arg-type]
        from_json = json.load(archive.extractfile("contract.resolved.json"))  # type: ignore[arg-type]
    assert from_yaml["extensions"] == expected
    assert from_yaml == from_json  # the two members of one bundle say one thing


@pytest.mark.parametrize(
    ("name", "escape"), [("nel", "\\N"), ("ls", "\\L"), ("ps", "\\P")], ids=sorted(_SEPARATORS)
)
def test_n17_a_line_separator_is_written_as_an_escape_in_double_quotes(
    name: str, escape: str
) -> None:
    from fluid_build.cli.bundle import _serialize
    from fluid_build.forge.core.bundle import _canonicalize_yaml

    document = {"k": f"a{_SEPARATORS[name]}b"}

    assert _serialize(document, "yaml") == f'k: "a{escape}b"\n'
    assert _canonicalize_yaml(document) == f'k: "a{escape}b"\n'.encode()


_PLAIN = {
    "sql": "SELECT a,\n       b\nFROM t\nWHERE x = 'y'\n",
    "no newline": "SELECT 1",
    "long": "word " * 60 + "\n" + "x" * 300,
    "accents": "Zürich — naïve café ☃ \U0001f600",
    "tab\tand  spaces ": "  padded  ",
    "yes": "no",
    "numbers": ["1", "1.5", "null", "true", "~", ""],
    "nested": {"a": [{"b": "c: d"}, {"e": "- f"}], "g": "# not a comment"},
    "quotes": 'it\'s "both"',
    "backslash": "C:\\path\\n",
    "other controls": "a\x00b\x1b[0m\x7f",
    "bom": "\ufeffstart",
    "nbsp": "a\xa0b",
    "int": 7,
    "float": 1.5,
    "bool": True,
    "none": None,
}


def test_n17_every_other_text_is_written_as_pyyaml_wrote_it() -> None:
    """The bytes of a contract that holds none of the three characters do not change."""
    from fluid_build.cli.bundle import _serialize
    from fluid_build.forge.core.bundle import _canonicalize_yaml

    assert _serialize(_PLAIN, "yaml") == yaml.dump(
        _PLAIN, default_flow_style=False, allow_unicode=True, sort_keys=False, width=120
    )
    assert _canonicalize_yaml(_PLAIN) == yaml.safe_dump(
        _PLAIN,
        default_flow_style=False,
        sort_keys=True,
        allow_unicode=True,
        width=float("inf"),  # type: ignore[arg-type]
    ).encode("utf-8")


def test_n17_a_long_line_with_a_separator_still_reads_back_whole() -> None:
    """Double-quoted scalars fold at the width (``--out``): the fold is not in the text."""
    from fluid_build.cli.bundle import _serialize

    text = ("word " * 80) + "\x85" + ("more " * 80) + "\u2028end"

    assert yaml.safe_load(_serialize({"k": text}, "yaml")) == {"k": text}


def test_the_tgz_is_still_byte_identical_for_one_contract(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    path = _json_contract(tmp_path / "p", _doc(sql="a\x85b\n"))

    assert _bundle(capsys, path, "--format", "tgz", "--out", tmp_path / "one.tgz") == 0
    assert _bundle(capsys, path, "--format", "tgz", "--out", tmp_path / "two.tgz") == 0

    assert (tmp_path / "one.tgz").read_bytes() == (tmp_path / "two.tgz").read_bytes()
