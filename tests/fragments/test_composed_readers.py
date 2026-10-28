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

"""Reads that go through the one loader: what the scenario tests leave open (G-13, G-19, G-20, G-26).

A contract kept in fragments must be read whole wherever it is read.
``test_no_raw_readers.py`` and ``test_one_loader_parity.py`` show it for the composition
rule, the replay walk, the dbt run record, ``diff``, ``generate ci`` and ``product-add``,
with the one case each scenario names. This file pins what those fixes decided beyond it:

* the composition rule's index skips a contract that does not compose, with a debug line,
  and keeps walking;
* so does the replay walk;
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, Sequence, Tuple

import pytest
import yaml

import fluid_build
from fluid_build import loader

_CHECKOUT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module", autouse=True)
def _the_checkout_under_test_is_the_one_imported() -> None:
    imported = Path(fluid_build.__file__).resolve()
    assert imported.is_relative_to(_CHECKOUT), f"{imported} is not inside {_CHECKOUT}"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    for name in ("FLUID_REF_ROOT", "FLUID_ENV", "FLUID_PROVIDER", "FLUID_PROJECT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv(loader.IGNORE_WORKSPACE_REF_ROOT_ENV, raising=False)
    monkeypatch.chdir(tmp_path)
    yield


# ── small helpers ───────────────────────────────────────────────────────


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _dump(data: Any) -> str:
    return yaml.safe_dump(data, sort_keys=False)


def _read(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _expose(
    expose_id: str, columns: Sequence[str] = ("id", "label"), **extra: Any
) -> Dict[str, Any]:
    return {
        "exposeId": expose_id,
        "kind": "table",
        "version": "1.0.0",
        "binding": {
            "platform": "local",
            "format": "csv",
            "location": {"path": f"runtime/out/{expose_id}.csv"},
        },
        "contract": {"schema": [{"name": name, "type": "string"} for name in columns], **extra},
    }


def _contract(**parts: Any) -> Dict[str, Any]:
    document: Dict[str, Any] = {
        "fluidVersion": "0.7.6",
        "kind": "DataProduct",
        "id": "refs.composed",
        "name": "Composed",
        "domain": "ops",
        "metadata": {"layer": "Silver", "owner": {"team": "t", "email": "t@example.com"}},
        "exposes": [_expose("out")],
    }
    document.update(parts)
    return document


def _cli(argv: Sequence[Any], capsys: pytest.CaptureFixture) -> Tuple[int, str, str]:
    """``fluid <argv>`` in process: ``(exit code, stdout, stderr)``."""
    from fluid_build.cli import main

    rc = main([str(a) for a in argv])
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


def _squash(text: str) -> str:
    """``text`` with every whitespace gone: the console wraps a long path anywhere."""
    return "".join(text.split())


def _plain(text: str) -> str:
    """``text`` reduced to the characters of a path, name or value: what a wrapped line
    cannot split."""
    return re.sub(r"[^A-Za-z0-9._/:\\-]", "", text)


# ── the composition rule's index ────────────────────────────────────────


def test_the_composition_index_skips_a_contract_that_does_not_compose_and_says_so(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from fluid_build.forge.product_types import _build_product_type_index

    team = {"team": "t", "email": "t@example.com"}
    _write(
        tmp_path / "gold" / "contract.fluid.yaml",
        _dump(_contract(id="up.gold", metadata={"$ref": "./metadata.yaml"})),
    )
    _write(tmp_path / "gold" / "metadata.yaml", _dump({"layer": "Gold", "owner": team}))
    _write(
        tmp_path / "broken" / "contract.fluid.yaml",
        _dump(_contract(id="up.broken", metadata={"$ref": "./missing.yaml"})),
    )
    _write(tmp_path / "plain" / "contract.fluid.yaml", _dump(_contract(id="up.plain")))

    with caplog.at_level(logging.DEBUG, logger="fluid.forge.product_types"):
        index = _build_product_type_index([tmp_path], None)

    assert index == {"up.gold": "CDP", "up.plain": "ADP"}
    assert "composition_index_skip" in caplog.text and "missing.yaml" in caplog.text


def test_the_composition_index_keeps_a_product_whose_metadata_is_not_a_mapping(
    tmp_path: Path,
) -> None:
    """Its type is unknown, which the rule reports; the walk goes on to the next contract."""
    from fluid_build.forge.product_types import _build_product_type_index

    _write(tmp_path / "odd" / "contract.fluid.yaml", _dump(_contract(id="up.odd", metadata=["x"])))
    _write(tmp_path / "plain" / "contract.fluid.yaml", _dump(_contract(id="up.plain")))

    assert _build_product_type_index([tmp_path], None) == {"up.odd": None, "up.plain": "ADP"}


# ── the replay walk ─────────────────────────────────────────────────────


def _consumer(root: Path, name: str, consumes: Any) -> Path:
    return _write(
        root / name / "contract.fluid.yaml",
        _dump(_contract(id=f"down.{name}", consumes=consumes)),
    )


def test_the_replay_walk_skips_a_contract_that_does_not_compose_and_finds_the_rest(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from fluid_build.build_runners._replay import find_downstream_products

    ws = tmp_path / "ws"
    entry = {"productId": "up.orders", "exposeId": "orders"}
    _consumer(ws, "inline", [entry])
    _write(ws / "split" / "consumes" / "up.yaml", _dump(entry))
    _consumer(ws, "split", [{"$ref": "./consumes/up.yaml"}])
    _consumer(ws, "broken", [{"$ref": "./consumes/missing.yaml"}])

    with caplog.at_level(logging.DEBUG, logger="fluid.build_runners.replay"):
        found = find_downstream_products(ws, "up.orders")

    assert [m["product_id"] for m in found] == ["down.inline", "down.split"]
    assert "downstream_walk_skip" in caplog.text and "missing.yaml" in caplog.text


def test_a_rewound_upstream_cursor_marks_a_downstream_whose_consumes_are_in_a_fragment(
    tmp_path: Path,
) -> None:
    from fluid_build.api.state import Cursor
    from fluid_build.build_runners._replay import list_dirty_products
    from fluid_build.build_runners._state import FileStateStore

    ws = tmp_path / "ws"
    _write(
        ws / "split" / "consumes" / "up.yaml",
        _dump({"productId": "up.orders", "exposeId": "orders"}),
    )
    _consumer(ws, "split", [{"$ref": "./consumes/up.yaml"}])
    store = FileStateStore(ws / ".fluid" / "runs")

    store.set_cursor("up.orders", "main", Cursor(stream="s", value=100, updated_at=""))
    store.set_cursor("up.orders", "main", Cursor(stream="s", value=40, updated_at=""))

    assert [d["product_id"] for d in list_dirty_products(ws)] == ["down.split"]
