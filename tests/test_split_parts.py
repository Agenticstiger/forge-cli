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

"""``fluid split``: choose the parts, keep the comments, never change the contract.

The invariant every case checks: the split contract composes to the same
document as the original (``fluid bundle`` of the result equals the
original's). The compatibility gates: the default parts write the same files
as before, the dict-level splitter ``fluid forge`` uses writes them the same
way, and every shipped example survives a split unchanged.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any, Dict, List

import pytest
import yaml

from fluid_build.loader import compose_contract

REPO = Path(__file__).resolve().parents[1]

_COMMENTED = """\
# Orders product — owned by commerce-data.
fluidVersion: "0.7.6"   # preview schema
kind: DataProduct
id: commerce.orders
name: Orders
description:
  purpose: One row per order.   # what it is for
  usage: |
    Join on `order_id`.
    Note: amounts are in EUR.
domain: commerce
metadata:
  layer: Silver
  owner: {team: commerce-data, email: commerce@example.com}

sovereignty:  # EU only
  jurisdiction: EU
  dataResidency: true

builds:
  # the one transform
  - id: transform
    pattern: embedded-logic
    engine: sql
    properties:
      sql: |
        -- monthly: rollup
        SELECT order_id, 'a: b' AS note
        FROM src

  # --- end of builds ---
exposes:
  - exposeId: orders
    kind: table
    binding: {platform: local, format: csv, location: {path: out/orders.csv}}
    contract:
      schema:
      - {name: order_id, type: string, required: true}
      quality:
        - rule: id_present
          expression: order_id IS NOT NULL
          severity: error
extensions:
  acme: {region: eu, tiers: [gold]}
# end of file
"""


def _split(path: Path, *args: str) -> int:
    from fluid_build.cli import main

    return main(["split", str(path), *args])


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _files(root: Path) -> Dict[str, str]:
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def _comments(text: str) -> List[str]:
    return [line.split("#", 1)[1].strip() for line in text.splitlines() if "#" in line]


@pytest.fixture
def product(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    return _write(tmp_path / "p" / "contract.fluid.yaml", _COMMENTED)


# ── the default parts: what fluid split always did ─────────────────────


def test_the_default_split_writes_the_files_it_always_wrote(product: Path) -> None:
    before = compose_contract(product).document

    assert _split(product) == 0

    assert sorted(_files(product.parent)) == [
        "contract.fluid.yaml",
        "fragments/builds/transform.yaml",
        "fragments/exposes/orders.yaml",
        "fragments/sovereignty.yaml",
    ]
    assert compose_contract(product).document == before
    root = product.read_text(encoding="utf-8")
    assert "  $ref: ./fragments/sovereignty.yaml" in root
    assert "  - $ref: ./fragments/builds/transform.yaml" in root
    assert (
        (product.parent / "fragments" / "builds" / "transform.yaml")
        .read_text("utf-8")
        .startswith("# Build: transform\n\nid: transform\n")
    )


def test_every_comment_of_the_root_is_kept(product: Path) -> None:
    assert _split(product) == 0

    root = product.read_text(encoding="utf-8")
    kept = _comments(root)
    moved = "".join((product.parent / "fragments" / "builds" / "transform.yaml").read_text())
    # Comments outside the moved parts stay in the root, in their places.
    for comment in (
        "Orders product — owned by commerce-data.",
        "preview schema",
        "what it is for",
        "EU only",
        "the one transform",
        "--- end of builds ---",
        "end of file",
    ):
        assert comment in kept
    assert root.startswith("# Orders product")
    assert 'fluidVersion: "0.7.6"   # preview schema\n' in root
    assert "-- monthly: rollup" in moved


def test_the_dict_splitter_fluid_forge_uses_is_unchanged() -> None:
    """``fluid forge`` writes fragments through this function: same paths, same
    header, same dump, as before parts existed."""
    from fluid_build.cli.forge_contract_fragments import split_contract_to_fragments

    contract = yaml.safe_load(_COMMENTED)
    root, files = split_contract_to_fragments(contract)

    assert root["sovereignty"] == {"$ref": "./fragments/sovereignty.yaml"}
    assert root["builds"] == [{"$ref": "./fragments/builds/transform.yaml"}]
    assert root["exposes"] == [{"$ref": "./fragments/exposes/orders.yaml"}]
    assert files["fragments/sovereignty.yaml"] == (
        "# Data sovereignty rules.\n\n"
        + yaml.dump(
            contract["sovereignty"], default_flow_style=False, sort_keys=False, allow_unicode=True
        )
    )
    assert files["fragments/exposes/orders.yaml"].startswith(
        "# Expose: orders\n\nexposeId: orders\n"
    )


@pytest.mark.parametrize(
    "example",
    sorted(
        str(p.relative_to(REPO))
        for p in (REPO / "examples").rglob("contract.fluid.yaml")
        if "generated" not in p.parts
    ),
)
def test_every_shipped_example_survives_a_split(
    example: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = REPO / example
    copy = tmp_path / "example"
    shutil.copytree(source.parent, copy)
    path = copy / source.name
    try:
        before = compose_contract(path).document
    except Exception:  # noqa: BLE001 - an example that does not compose is not split's
        pytest.skip("the example does not compose on its own")
    monkeypatch.chdir(tmp_path)

    assert _split(path) == 0

    assert compose_contract(path).document == before


# ── choosing the parts ──────────────────────────────────────────────────


def test_every_part_moves_to_its_own_file_and_the_contract_stays_the_same(
    product: Path,
) -> None:
    before = compose_contract(product).document

    assert (
        _split(product, "--parts", "builds,exposes,sql,quality,schema,description,extensions") == 0
    )

    files = _files(product.parent)
    for name in (
        "sql/transform.sql",
        "docs/purpose.md",
        "docs/usage.md",
        "fragments/quality/orders.yaml",
        "fragments/schemas/orders.yaml",
        "fragments/extensions/acme.yaml",
    ):
        assert name in files, name
    assert compose_contract(product).document == before
    build = (product.parent / "fragments" / "builds" / "transform.yaml").read_text("utf-8")
    assert "sql: {$ref: ../../sql/transform.sql}" in build
    assert (product.parent / "sql" / "transform.sql").read_text("utf-8") == (
        "-- monthly: rollup\nSELECT order_id, 'a: b' AS note\nFROM src\n"
    )
    assert yaml.safe_load(
        (product.parent / "fragments" / "quality" / "orders.yaml").read_text("utf-8")
    ) == [{"rule": "id_present", "expression": "order_id IS NOT NULL", "severity": "error"}]


def test_a_nested_part_of_an_entry_left_inline_is_referenced_from_the_root(product: Path) -> None:
    before = compose_contract(product).document

    assert _split(product, "--parts", "sql,schema") == 0

    root = product.read_text(encoding="utf-8")
    assert "sql: {$ref: ./sql/transform.sql}" in root
    # A block sequence at its key's own indentation still becomes a nested ref.
    assert yaml.safe_load(root)["exposes"][0]["contract"]["schema"] == {
        "$ref": "./fragments/schemas/orders.yaml"
    }
    assert compose_contract(product).document == before


def test_an_unknown_part_is_refused_with_the_list(product: Path, capsys: Any) -> None:
    original = product.read_text(encoding="utf-8")

    assert _split(product, "--parts", "builds,bogus") == 1

    assert (
        "unknown part 'bogus'; the parts are sovereignty, accessPolicy" in capsys.readouterr().err
    )
    assert product.read_text(encoding="utf-8") == original


# ── safety ──────────────────────────────────────────────────────────────


def test_splitting_twice_changes_nothing(product: Path, capsys: Any) -> None:
    assert _split(product, "--parts", "builds,exposes,sql,quality") == 0
    first = _files(product.parent)

    assert _split(product, "--parts", "builds,exposes,sql,quality") == 0

    assert _files(product.parent) == first
    assert "Nothing to split" in capsys.readouterr().err


def test_a_file_that_exists_is_never_written_over(product: Path) -> None:
    mine = _write(product.parent / "fragments" / "builds" / "transform.yaml", "# mine\nkeep: me\n")
    before = compose_contract(product).document

    assert _split(product) == 0

    assert mine.read_text(encoding="utf-8") == "# mine\nkeep: me\n"
    assert (product.parent / "fragments" / "builds" / "transform-2.yaml").exists()
    assert "$ref: ./fragments/builds/transform-2.yaml" in product.read_text("utf-8")
    assert compose_contract(product).document == before


def test_two_ids_with_one_slug_get_a_file_each(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.chdir(tmp_path)
    contract = {
        "id": "x",
        "exposes": [
            {"exposeId": "orders.events", "kind": "topic"},
            {"exposeId": "orders-events", "kind": "topic"},
        ],
    }
    path = _write(tmp_path / "contract.fluid.yaml", yaml.safe_dump(contract, sort_keys=False))

    assert _split(path) == 0

    assert compose_contract(path).document == contract
    assert {
        "fragments/exposes/orders-events.yaml",
        "fragments/exposes/orders-events-2.yaml",
    } <= set(_files(tmp_path))


def test_a_json_root_stays_json(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.chdir(tmp_path)
    contract = yaml.safe_load(_COMMENTED)
    path = _write(tmp_path / "contract.fluid.json", json.dumps(contract, indent=2))

    assert _split(path) == 0

    assert json.loads(path.read_text("utf-8"))["builds"] == [
        {"$ref": "./fragments/builds/transform.yaml"}
    ]
    assert compose_contract(path).document == contract


def test_yaml_aliases_split_as_data_and_say_the_comments_were_not_kept(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    monkeypatch.chdir(tmp_path)
    path = _write(
        tmp_path / "contract.fluid.yaml",
        "# a comment\nid: x\nsovereignty: &s {jurisdiction: EU}\nextensions: {copy: *s}\n",
    )
    before = compose_contract(path).document

    assert _split(path) == 0

    assert "Comments in the root were not kept: the contract uses YAML aliases" in (
        capsys.readouterr().err
    )
    assert compose_contract(path).document == before


def test_a_crlf_root_keeps_its_line_ends(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "contract.fluid.yaml"
    path.write_bytes(_COMMENTED.replace("\n", "\r\n").encode("utf-8"))
    before = compose_contract(path).document

    assert _split(path) == 0

    data = path.read_bytes()
    assert b"\r\n" in data and b"\n" not in data.replace(b"\r\n", b"")
    assert compose_contract(path).document == before


def test_text_a_file_cannot_give_back_stays_inline(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.chdir(tmp_path)
    contract = {"id": "x", "builds": [{"id": "t", "properties": {"sql": "SELECT 1\r\nFROM t"}}]}
    path = _write(tmp_path / "contract.fluid.yaml", yaml.safe_dump(contract))

    assert _split(path, "--parts", "sql") == 0

    assert not (tmp_path / "sql").exists()


def test_a_split_that_would_change_the_contract_is_taken_back(
    product: Path, monkeypatch: Any, capsys: Any
) -> None:
    from fluid_build.cli import _split_text

    original = product.read_text(encoding="utf-8")
    real = _split_text.split_yaml_text

    def _wrong(text: str, sites: Any, contract: Any) -> Any:
        root, fragments = real(text, sites, contract)
        return root + "extra: 1\n", fragments

    monkeypatch.setattr(_split_text, "split_yaml_text", _wrong)

    assert _split(product) == 1

    assert "does not compose to the original one; nothing was changed" in capsys.readouterr().err
    assert product.read_text(encoding="utf-8") == original
    assert not (product.parent / "fragments").exists() or not any(
        (product.parent / "fragments").rglob("*.yaml")
    )
