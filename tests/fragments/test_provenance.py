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

"""Provenance and error messages of ``$ref`` composition (catalog area H).

Two promises, both from the spec the catalog is built on:

* A validation error for a node that came from a fragment or an overlay is
  prefixed with the relative file and line, ``fragments/x.yaml:21 (exposes[0]...)``.
  An error in the root contract is unchanged. ``LoadedContract.origin_of`` tells
  the same to a library caller.
* A ``$ref`` that cannot be resolved is an error that names the ref, the file it
  was written in, and the RFC 6901 pointer of the ``$ref`` node inside that file.

Every line number below is computed from the fixture text, never typed in.
The scenario id (``H-01`` ...) is in each test name; ``E-08`` and ``E-26`` are
here for their message halves only.

A test marked ``xfail(strict=True)`` pins a finding: it asserts the spec and
fails today. Its reason carries the finding id (``P5-nn``):

* P5-01  ``fluid plan`` / ``apply`` print schema errors without the file and line
* P5-02  the latest writer is not preferred over the deepest recorded one: a value an
  overlay, or siblings beside ``$ref``, wrote is named after the base's fragment
* P5-03  a ``#/`` pointer through a list index loses its copy's provenance
* P5-04  the blocked-system-path error does not name the file or pointer of the ref
* P5-05  a ref error on validate carries the generic "check the YAML syntax" hints
* P5-06  an overlay's ``#/`` pointer merged into a base object loses its file
* P5-07  an overlay's text ref over a base text ref is named after the base file
* P5-08  a value inherited through a YAML merge key (``<<: *anchor``) gets no prefix
* P5-09  finding the line of each error is quadratic in the number of errors
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping, NamedTuple, Optional, Sequence, Tuple, Union

import pytest

import fluid_build
from fluid_build import contract_refs, loader

CHECKOUT = Path(__file__).resolve().parents[2]

BAD_TYPE = "42 is not valid under any of the given schemas"

# ── fixtures: a small, valid 0.7.6 contract and the parts it is split into ──


def _indent_item(text: str) -> str:
    """A YAML mapping as one item of a block list indented by two spaces."""
    lines = text.splitlines()
    return "".join(("  - " if i == 0 else "    ") + line + "\n" for i, line in enumerate(lines))


HEAD = (
    'fluidVersion: "0.7.6"\n'
    "kind: DataProduct\n"
    "id: analytics.eu.orders_v1\n"
    "name: Orders\n"
    "description: Orders data product.\n"
    "domain: analytics\n"
    "metadata:\n"
    "  layer: Silver\n"
    "  owner: {team: analytics, email: analytics@example.com}\n"
)

BUILD_ORDERS = (
    "builds:\n"
    "  - id: build_orders\n"
    "    pattern: embedded-logic\n"
    "    engine: sql\n"
    "    properties:\n"
    "      sql: SELECT 1 AS order_id\n"
)

EXPOSE_ORDERS = (
    "exposeId: orders\n"
    "kind: table\n"
    'version: "1.0.0"\n'
    "binding:\n"
    "  platform: local\n"
    "  format: csv\n"
    "  location: {path: runtime/out/orders.csv}\n"
    "contract:\n"
    "  schema:\n"
    "    - {name: order_id, type: string, required: true}\n"
    "    - {name: order_time, type: timestamp, required: true}\n"
    "    - {name: amount, type: number}\n"
)

EXPOSE_CUSTOMERS = (
    "exposeId: customers\n"
    "kind: table\n"
    'version: "1.0.0"\n'
    "binding:\n"
    "  platform: local\n"
    "  format: csv\n"
    "  location: {path: runtime/out/customers.csv}\n"
    "contract:\n"
    "  schema:\n"
    "    - {name: customer_id, type: string, required: true}\n"
)

#: An expose whose ``binding.location.path`` is the one place a test breaks.
EXPOSE_AT_PATH = (
    "exposeId: out\n"
    "kind: table\n"
    'version: "1.0.0"\n'
    "binding:\n"
    "  platform: local\n"
    "  format: csv\n"
    "  location:\n"
    "    path: @NODE@\n"
    "contract:\n"
    "  schema:\n"
    "    - {name: id, type: integer}\n"
)

#: The contract ``fluid split`` is run on: two builds, two exposes.
FLAT_TWO_EXPOSES = (
    HEAD
    + BUILD_ORDERS
    + "  - id: build_customers\n"
    + "    pattern: embedded-logic\n"
    + "    engine: sql\n"
    + "    properties:\n"
    + "      sql: SELECT 1 AS customer_id\n"
    + "exposes:\n"
    + _indent_item(EXPOSE_ORDERS)
    + _indent_item(EXPOSE_CUSTOMERS)
)


# ── harness ─────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _hermetic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Run in tmp_path, with no ref-root override from the environment, and with
    the loader's warn-once registries empty before and after. The code under test
    must be the checkout this file lives in, not another one on the path."""
    assert Path(fluid_build.__file__).resolve().is_relative_to(CHECKOUT), fluid_build.__file__
    monkeypatch.chdir(tmp_path)
    for name in ("FLUID_REF_ROOT", "FLUID_IGNORE_WORKSPACE_REF_ROOT"):
        monkeypatch.delenv(name, raising=False)
    registries = [
        value
        for name, value in vars(loader).items()
        if name.startswith("_NOTED_") and isinstance(value, set)
    ]
    for registry in registries:
        registry.clear()
    yield
    for registry in registries:
        registry.clear()


class Run(NamedTuple):
    code: int
    out: str
    err: str

    @property
    def text(self) -> str:
        return self.out + self.err


def _cli(capsys: pytest.CaptureFixture, *argv: Any) -> Run:
    """``fluid <argv>`` in this process: exit code, stdout, stderr."""
    from fluid_build.cli import main

    code = main([str(a) for a in argv])
    captured = capsys.readouterr()
    return Run(code, captured.out, captured.err)


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _product(
    root: Path,
    exposes: str,
    *,
    builds: str = BUILD_ORDERS,
    extra: str = "",
    files: Optional[Mapping[str, str]] = None,
) -> Path:
    """``root/contract.fluid.yaml`` (head, builds, the given ``exposes`` list
    items, extra top-level text) and the other files, relative to ``root``."""
    contract = _write(root / "contract.fluid.yaml", HEAD + builds + "exposes:\n" + exposes + extra)
    for rel, text in (files or {}).items():
        _write(root / rel, text)
    return contract


def _line_of(path: Path, needle: str) -> int:
    """The 1-based line of the one line of ``path`` holding ``needle``."""
    lines = path.read_text(encoding="utf-8").splitlines()
    hits = [number for number, line in enumerate(lines, 1) if needle in line]
    assert len(hits) == 1, f"{needle!r} is on lines {hits} of {path.name}, not exactly one"
    return hits[0]


def _squash(text: str) -> str:
    """``text`` without whitespace: a message may be wrapped by the terminal layer."""
    return re.sub(r"\s+", "", text)


def _json_doc(text: str) -> Dict[str, Any]:
    """The JSON document ``fluid validate --format json`` printed, whatever logs surround it."""
    decoded, _ = json.JSONDecoder().raw_decode(text[text.index("{\n") :])
    assert isinstance(decoded, dict)
    return decoded


def _validate_errors(capsys: pytest.CaptureFixture, contract: Path, *extra: Any) -> List[str]:
    """The ``errors`` of ``fluid validate --format json``; exit code 1 is asserted."""
    run = _cli(capsys, "validate", contract, "--format", "json", *extra)
    assert run.code == 1, run.text
    doc = _json_doc(run.out)
    assert doc["valid"] is False
    errors = doc["errors"]
    assert isinstance(errors, list)
    return errors


def _prefixed(rel: str, line: Optional[int], path: str, message: str) -> str:
    where = f"{rel}:{line}" if line else rel
    return f"{where} ({path}): {message}"


def _chain(root: Path, refs: int) -> Path:
    """``extensions.x`` is the head of a chain of ``refs`` file refs (``c0.yaml`` ...)."""
    for i in range(refs - 1):
        _write(root / f"c{i}.yaml", f"next: {{$ref: ./c{i + 1}.yaml}}\n")
    _write(root / f"c{refs - 1}.yaml", "end: true\n")
    return _product(
        root, _indent_item(EXPOSE_ORDERS), extra="extensions:\n  x: {$ref: ./c0.yaml}\n"
    )


# ── H-01 .. H-11: the prefix on validate ────────────────────────────────


def test_h01_split_fragment_error_names_file_line_and_path(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """Draft 25 claim 1, on the layout ``fluid split`` really writes (header included)."""
    contract = _write(tmp_path / "p" / "contract.fluid.yaml", FLAT_TWO_EXPOSES)
    assert _cli(capsys, "split", contract).code == 0
    fragment = tmp_path / "p" / "fragments" / "exposes" / "orders.yaml"
    assert "type: timestamp" in fragment.read_text(encoding="utf-8")
    fragment.write_text(
        fragment.read_text(encoding="utf-8").replace("type: timestamp", "type: 42"),
        encoding="utf-8",
    )
    line = _line_of(fragment, "type: 42")
    assert line > 2, "split starts a fragment with a header; the error is below it"

    run = _cli(capsys, "validate", contract)

    assert run.code == 1, run.text
    expected = _prefixed(
        "fragments/exposes/orders.yaml", line, "exposes[0].contract.schema[1].type", BAD_TYPE
    )
    assert f" 1. {expected}\n" in run.text
    assert "(1 error(s))" in run.text


def test_h01_workspace_validate_names_each_products_fragments_relative_to_that_product(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``fluid validate`` with no argument walks the workspace: same prefix as the explicit form."""
    workspace = tmp_path / "ws"
    _write(workspace / "fluid.workspace.yaml", "name: demo\n")
    products = {}
    for name, text in (
        ("orders", EXPOSE_ORDERS.replace("type: timestamp", "type: 42")),
        ("customers", EXPOSE_ORDERS),
    ):
        products[name] = _product(
            workspace / "products" / name,
            "  - $ref: ./fragments/exposes/orders.yaml\n",
            files={"fragments/exposes/orders.yaml": text},
        )
    line = _line_of(
        workspace / "products" / "orders" / "fragments" / "exposes" / "orders.yaml", "type: 42"
    )
    monkeypatch.chdir(workspace)

    run = _cli(capsys, "validate")

    assert run.code == 1, run.text
    assert (
        _prefixed(
            "fragments/exposes/orders.yaml", line, "exposes[0].contract.schema[1].type", BAD_TYPE
        )
        in run.text
    )
    explicit = _validate_errors(capsys, products["orders"])
    assert explicit[0] in run.text, "the explicit form says the same"


@pytest.mark.slow
def test_h01_error_lookup_cost_is_linear_in_the_number_of_errors(tmp_path: Path) -> None:
    """Naming the file and line of each error must not turn validate into a quadratic job:
    a contract is untrusted input, and 0.24.0 reported the same errors in a fraction of a second."""
    import time

    from fluid_build.schema_manager import FluidSchemaManager

    errors = 20_000
    contract = _product(
        tmp_path / "p",
        _indent_item(EXPOSE_ORDERS),
        extra="labels:\n" + "".join(f"  k{i}: 42\n" for i in range(errors)),
    )
    composed = loader.compose_contract(contract)
    manager = FluidSchemaManager()
    manager.validate_contract(composed.document, offline_only=True)  # warm the schema cache

    start = time.perf_counter()
    plain = manager.validate_contract(composed.document, offline_only=True)
    without_lines = time.perf_counter() - start
    start = time.perf_counter()
    located = manager.validate_contract(
        composed.document, offline_only=True, locate=composed.origins.describe
    )
    with_lines = time.perf_counter() - start

    assert len(plain.errors) == len(located.errors) == errors
    assert with_lines <= 10 * without_lines + 1.0, (without_lines, with_lines)


@pytest.mark.parametrize("fmt", ["text", "json"])
def test_h02_inline_error_is_exactly_the_old_unprefixed_message(
    tmp_path: Path, capsys: pytest.CaptureFixture, fmt: str
) -> None:
    """Errors in the root file read as they did in 0.24.0, byte for byte."""
    contract = _write(
        tmp_path / "p" / "contract.fluid.yaml",
        FLAT_TWO_EXPOSES.replace("type: timestamp", "type: 42"),
    )
    old = f"exposes[0].contract.schema[1].type: {BAD_TYPE}"

    run = _cli(capsys, "validate", contract, "--format", fmt)

    assert run.code == 1, run.text
    if fmt == "json":
        assert _json_doc(run.out)["errors"] == [old]
    else:
        assert f" 1. {old}\n" in run.out
        assert "(1 error(s))" in run.out
    assert "contract.fluid.yaml" not in run.out


def test_h02_json_root_keeps_its_own_errors_unprefixed_and_names_its_fragments(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """The root contract is not named when it is JSON either; a JSON fragment is."""
    inline = {
        "exposeId": "orders",
        "kind": "table",
        "version": "1.0.0",
        "binding": {"platform": "local", "format": "csv", "location": {"path": "a.csv"}},
        "contract": {"schema": [{"name": "id", "type": 42}]},
    }
    fragment = json.loads(json.dumps(inline))
    fragment.update({"exposeId": "extra", "contract": {"schema": [{"name": "id", "type": 43}]}})
    root = tmp_path / "p"
    contract = _write(
        root / "contract.fluid.json",
        json.dumps(
            {
                "fluidVersion": "0.7.6",
                "kind": "DataProduct",
                "id": "analytics.eu.orders_v1",
                "name": "Orders",
                "description": "x",
                "domain": "analytics",
                "metadata": {"layer": "Silver", "owner": {"team": "t", "email": "t@example.com"}},
                "exposes": [inline, {"$ref": "./extra.json"}],
            },
            indent=2,
        ),
    )
    _write(root / "extra.json", json.dumps(fragment, indent=2))
    line = _line_of(root / "extra.json", '"type": 43')

    errors = _validate_errors(capsys, contract)

    assert errors == [
        "exposes[0].contract.schema[0].type: 42 is not valid under any of the given schemas",
        _prefixed(
            "extra.json",
            line,
            "exposes[1].contract.schema[0].type",
            "43 is not valid under any of the given schemas",
        ),
    ]


def _overlay_text(kind: str, line_marker: str) -> str:
    if kind == "json":
        doc = {
            "exposes": [
                {
                    "exposeId": "orders",
                    "contract": {
                        "schema": [{"name": "order_id"}, {"name": "order_time", "type": 42}]
                    },
                }
            ]
        }
        return json.dumps(doc, indent=2) + "\n"
    return (
        "exposes:\n"
        "  - exposeId: orders\n"
        "    contract:\n"
        "      schema:\n"
        "        - {name: order_id}\n"
        f"        - {{name: order_time, type: 42}}  {line_marker}\n"
    )


@pytest.mark.parametrize(
    "rel,kind",
    [
        ("overlays/prod.yaml", "yaml"),
        ("overlays/prod.json", "json"),
        ("contract.fluid.prod.yaml", "yaml"),
    ],
)
def test_h03_error_set_by_the_overlay_names_the_overlay_file_and_line(
    tmp_path: Path, capsys: pytest.CaptureFixture, rel: str, kind: str
) -> None:
    root = tmp_path / "p"
    contract = _product(root, _indent_item(EXPOSE_ORDERS))
    overlay = _write(root / rel, _overlay_text(kind, "# the bad one"))
    line = _line_of(overlay, '"type": 42' if kind == "json" else "# the bad one")

    # Negative control: the base alone is valid.
    assert _cli(capsys, "validate", contract).code == 0
    errors = _validate_errors(capsys, contract, "--env", "prod")

    assert errors == [_prefixed(rel, line, "exposes[0].contract.schema[1].type", BAD_TYPE)]


@pytest.mark.xfail(
    strict=True,
    reason="BUG P5-02: the overlay's bad column type is named after the base's list fragment "
    "(schemas/orders.yaml, which holds the column too), not after overlays/prod.yaml",
)
def test_h03_overlay_value_over_a_column_the_base_keeps_in_a_list_fragment(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """``fluid split --parts schema`` leaves the columns in a file of their own."""
    root = tmp_path / "p"
    expose = EXPOSE_ORDERS.replace(
        "  schema:\n"
        "    - {name: order_id, type: string, required: true}\n"
        "    - {name: order_time, type: timestamp, required: true}\n"
        "    - {name: amount, type: number}\n",
        "  schema: {$ref: ../../schemas/orders.yaml}\n",
    )
    columns = (
        "- {name: order_id, type: string, required: true}\n"
        "- {name: order_time, type: timestamp, required: true}\n"
        "- {name: amount, type: number}\n"
    )
    contract = _product(
        root,
        "  - $ref: ./fragments/exposes/orders.yaml\n",
        files={
            "fragments/exposes/orders.yaml": expose,
            "schemas/orders.yaml": columns,
            "overlays/prod.yaml": _overlay_text("yaml", "# the bad one"),
        },
    )
    line = _line_of(root / "overlays" / "prod.yaml", "# the bad one")

    assert _cli(capsys, "validate", contract).code == 0, "the base alone is valid"
    errors = _validate_errors(capsys, contract, "--env", "prod")

    assert errors == [
        _prefixed("overlays/prod.yaml", line, "exposes[0].contract.schema[1].type", BAD_TYPE)
    ]


@pytest.mark.parametrize(
    "fragment,message,path",
    [
        (
            "platform: local\nformat: csv\nlocation: {path: runtime/out/prod.csv}\nbogus: 1\n",
            "Additional properties are not allowed ('bogus' was unexpected)",
            "exposes[0].binding",
        ),
        (
            "platform: local\nformat: csv\nlocation:\n  path: 42\n",
            "42 is not of type 'string'",
            "exposes[0].binding.location.path",
        ),
    ],
    ids=["unexpected-key", "bad-value"],
)
def test_h04_error_from_the_overlays_fragment_names_that_fragment(
    tmp_path: Path, capsys: pytest.CaptureFixture, fragment: str, message: str, path: str
) -> None:
    root = tmp_path / "p"
    contract = _product(
        root,
        _indent_item(EXPOSE_ORDERS),
        files={
            "overlays/prod.yaml": (
                "exposes:\n  - exposeId: orders\n"
                "    binding: {$ref: ../fragments/prod-binding.yaml}\n"
            ),
            "fragments/prod-binding.yaml": fragment,
        },
    )
    frag = root / "fragments" / "prod-binding.yaml"
    line = _line_of(frag, "bogus" if "bogus" in fragment else "path: 42")

    errors = _validate_errors(capsys, contract, "--env", "prod")

    assert errors == [_prefixed("fragments/prod-binding.yaml", line, path, message)]


def test_h04_overlay_entry_given_as_a_ref_names_the_fragment(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    root = tmp_path / "p"
    contract = _product(
        root,
        _indent_item(EXPOSE_ORDERS),
        files={
            "overlays/prod.yaml": "exposes:\n  - $ref: ../fragments/prod-expose.yaml\n",
            "fragments/prod-expose.yaml": "exposeId: orders\nkind: not_a_kind\n",
        },
    )
    line = _line_of(root / "fragments" / "prod-expose.yaml", "kind:")

    errors = _validate_errors(capsys, contract, "--env", "prod")

    assert len(errors) == 1
    assert errors[0].startswith(f"fragments/prod-expose.yaml:{line} (exposes[0].kind): ")


@pytest.mark.parametrize("as_ref", [False, True], ids=["inline", "ref"])
def test_h04_entry_the_overlay_appends_is_named_after_the_file_that_wrote_it(
    tmp_path: Path, capsys: pytest.CaptureFixture, as_ref: bool
) -> None:
    """An overlay entry with an id the base lacks is appended: it lands at a new index."""
    root = tmp_path / "p"
    extra = EXPOSE_ORDERS.replace("exposeId: orders", "exposeId: extra").replace(
        "type: timestamp", "type: 42"
    )
    if as_ref:
        files = {
            "overlays/prod.yaml": "exposes:\n  - $ref: ../fragments/extra.yaml\n",
            "fragments/extra.yaml": extra,
        }
        holder = "fragments/extra.yaml"
    else:
        files = {"overlays/prod.yaml": "exposes:\n" + _indent_item(extra)}
        holder = "overlays/prod.yaml"
    contract = _product(root, _indent_item(EXPOSE_ORDERS), files=files)
    line = _line_of(root / holder, "type: 42")

    errors = _validate_errors(capsys, contract, "--env", "prod")

    assert errors == [_prefixed(holder, line, "exposes[1].contract.schema[1].type", BAD_TYPE)]


def test_h05_leaf_from_the_overlay_parent_mapping_from_the_whole_expose_fragment(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """The overlay's leaf answers for itself; the parent mapping is the fragment's."""
    root = tmp_path / "p"
    contract = _product(
        root,
        "  - $ref: ./fragments/exposes/orders.yaml\n",
        files={
            "fragments/exposes/orders.yaml": EXPOSE_ORDERS,
            "overlays/prod.yaml": (
                "exposes:\n"
                "  - exposeId: orders\n"
                "    binding:\n"
                "      location:\n"
                "        path: 42  # the bad one\n"
            ),
        },
    )
    line = _line_of(root / "overlays" / "prod.yaml", "# the bad one")

    errors = _validate_errors(capsys, contract, "--env", "prod")

    assert errors == [
        _prefixed(
            "overlays/prod.yaml",
            line,
            "exposes[0].binding.location.path",
            "42 is not of type 'string'",
        )
    ]


@pytest.mark.xfail(
    strict=True,
    reason="BUG P5-02: a value an overlay wrote over what a ref brought in is named after the "
    "base fragment (origins prefer the deepest recorded path, not the latest writer)",
)
def test_h05_leaf_from_the_overlay_parent_mapping_from_a_deeper_fragment(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """The base expose keeps its binding in a fragment of its own; the overlay sets one leaf."""
    root = tmp_path / "p"
    base_expose = EXPOSE_ORDERS.replace(
        "binding:\n  platform: local\n  format: csv\n  location: {path: runtime/out/orders.csv}\n",
        "binding: {$ref: ./fragments/binding.yaml}\n",
    )
    assert "fragments/binding.yaml" in base_expose
    contract = _product(
        root,
        _indent_item(base_expose),
        files={
            "fragments/binding.yaml": (
                "platform: local\nformat: csv\nlocation:\n  path: runtime/out/orders.csv\n"
            ),
            "overlays/prod.yaml": (
                "exposes:\n"
                "  - exposeId: orders\n"
                "    binding:\n"
                "      location:\n"
                "        path: 42  # the bad one\n"
            ),
        },
    )
    line = _line_of(root / "overlays" / "prod.yaml", "# the bad one")

    errors = _validate_errors(capsys, contract, "--env", "prod")

    assert errors == [
        _prefixed(
            "overlays/prod.yaml",
            line,
            "exposes[0].binding.location.path",
            "42 is not of type 'string'",
        )
    ]


@pytest.mark.xfail(
    strict=True,
    reason="BUG P5-02: the overlay's team: 42 is named after shared/team.yaml, the base's owner fragment",
)
def test_h05_overlay_value_over_an_owner_fragment_is_named_after_the_overlay(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    root = tmp_path / "p"
    contract = _product(
        root,
        _indent_item(EXPOSE_ORDERS),
        files={
            "shared/team.yaml": "team: platform-ops\nemail: ops@example.com\n",
            "overlays/prod.yaml": "metadata:\n  owner:\n    team: 42  # the bad one\n",
        },
    )
    text = contract.read_text(encoding="utf-8").replace(
        "  owner: {team: analytics, email: analytics@example.com}\n",
        "  owner: {$ref: ./shared/team.yaml}\n",
    )
    contract.write_text(text, encoding="utf-8")
    line = _line_of(root / "overlays" / "prod.yaml", "# the bad one")

    errors = _validate_errors(capsys, contract, "--env", "prod")

    assert errors == [
        _prefixed("overlays/prod.yaml", line, "metadata.owner.team", "42 is not of type 'string'")
    ]


def test_h06_sibling_merged_column_names_each_key_after_the_file_that_wrote_it(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """``type`` is the target fragment's; ``description`` was written beside ``$ref`` in C."""
    root = tmp_path / "p"
    expose = EXPOSE_ORDERS.replace(
        "    - {name: order_time, type: timestamp, required: true}\n",
        "    - {$ref: ./cols.yaml#/order_time, description: 5}\n",
    )
    contract = _product(
        root,
        _indent_item(expose),
        files={"cols.yaml": "order_time:\n  name: order_time\n  type: 42\n  required: true\n"},
    )
    line = _line_of(root / "cols.yaml", "type: 42")

    errors = _validate_errors(capsys, contract)

    assert sorted(errors) == sorted(
        [
            "exposes[0].contract.schema[1].description: 5 is not of type 'string'",
            _prefixed("cols.yaml", line, "exposes[0].contract.schema[1].type", BAD_TYPE),
        ]
    )


@pytest.mark.xfail(
    strict=True,
    reason="BUG P5-02: a sibling written beside $ref over a value the target took from a deeper "
    "ref is named after that deeper file, not after the contract that wrote the sibling",
)
def test_h06_sibling_over_a_value_the_target_took_from_a_deeper_ref_names_the_sibling(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    root = tmp_path / "p"
    expose = EXPOSE_ORDERS.replace(
        "binding:\n  platform: local\n  format: csv\n  location: {path: runtime/out/orders.csv}\n",
        "binding: {$ref: ./binding.yaml, location: {path: 42}}\n",
    )
    contract = _product(
        root,
        _indent_item(expose),
        files={
            "binding.yaml": "platform: local\nformat: csv\nlocation: {$ref: ./location.yaml}\n",
            "location.yaml": "path: runtime/out/orders.csv\n",
        },
    )

    errors = _validate_errors(capsys, contract)

    # The 42 was written beside the $ref in the root contract: no prefix.
    assert errors == ["exposes[0].binding.location.path: 42 is not of type 'string'"]


def test_h07_pointer_into_a_file_names_the_line_inside_the_pointed_subtree(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    root = tmp_path / "p"
    expose = EXPOSE_ORDERS.replace(
        "    - {name: order_time, type: timestamp, required: true}\n",
        "    - {$ref: ./cols.yaml#/order_time}\n",
    )
    contract = _product(
        root,
        _indent_item(expose),
        files={
            "cols.yaml": (
                "other:\n  name: other\n  type: 7\n"
                "order_time:\n  name: order_time\n  type: 42\n  required: true\n"
            )
        },
    )
    line = _line_of(root / "cols.yaml", "type: 42")
    assert line > _line_of(root / "cols.yaml", "type: 7")

    errors = _validate_errors(capsys, contract)

    assert errors == [_prefixed("cols.yaml", line, "exposes[0].contract.schema[1].type", BAD_TYPE)]


@pytest.mark.parametrize("layout", ["pretty", "compact", "tabs"])
def test_h08_json_fragment_error_names_the_file_and_the_line_json_gives(
    tmp_path: Path, capsys: pytest.CaptureFixture, layout: str
) -> None:
    """SPEC GAP SG-26, characterised: a JSON fragment is read for its line as YAML,
    which reads pretty and compact JSON; JSON YAML cannot read (tab indentation)
    is named by its file alone. No layout is a crash."""
    root = tmp_path / "p"
    expose = {
        "exposeId": "orders",
        "kind": "table",
        "version": "1.0.0",
        "binding": {"platform": "local", "format": "csv", "location": {"path": "a.csv"}},
        "contract": {"schema": [{"name": "a", "type": "string"}, {"name": "b", "type": 42}]},
    }
    text = {
        "pretty": json.dumps(expose, indent=2) + "\n",
        "compact": json.dumps(expose),
        "tabs": json.dumps(expose, indent="\t") + "\n",
    }[layout]
    contract = _product(
        root,
        "  - $ref: ./fragments/out.json\n",
        files={"fragments/out.json": text},
    )
    fragment = root / "fragments" / "out.json"

    errors = _validate_errors(capsys, contract)

    path = "exposes[0].contract.schema[1].type"
    if layout == "pretty":
        expected = _prefixed("fragments/out.json", _line_of(fragment, '"type": 42'), path, BAD_TYPE)
    elif layout == "compact":
        expected = _prefixed("fragments/out.json", 1, path, BAD_TYPE)
    else:
        expected = _prefixed("fragments/out.json", None, path, BAD_TYPE)
    assert errors == [expected]


def test_h08_text_file_at_an_object_position_is_named_by_its_file_without_a_line(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """SPEC GAP SG-26, characterised: a text file is one value, so it has no line."""
    root = tmp_path / "p"
    contract = _product(
        root, _indent_item(EXPOSE_ORDERS), files={"meta.md": "# Meta\n\nSome text\n"}
    )
    text = contract.read_text(encoding="utf-8").replace(
        "metadata:\n  layer: Silver\n  owner: {team: analytics, email: analytics@example.com}\n",
        "metadata: {$ref: ./meta.md}\n",
    )
    contract.write_text(text, encoding="utf-8")

    errors = _validate_errors(capsys, contract)

    assert len(errors) == 1
    assert errors[0].startswith("meta.md (metadata): ")
    assert errors[0].endswith("is not of type 'object'")


def test_h09_fragment_outside_the_contract_directory_is_named_relative_never_absolute(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SPEC GAP SG-26 (path form), characterised: relative to the root contract's directory."""
    workspace = tmp_path / "ws"
    _write(
        workspace / "shared" / "columns.yaml",
        "order_id:\n  name: order_id\n  type: 42\n",
    )
    expose = EXPOSE_ORDERS.replace(
        "    - {name: order_id, type: string, required: true}\n",
        "    - {$ref: ../../shared/columns.yaml#/order_id, description: from the product}\n",
    )
    contract = _product(workspace / "products" / "orders", _indent_item(expose))
    monkeypatch.setenv("FLUID_REF_ROOT", str(workspace))
    line = _line_of(workspace / "shared" / "columns.yaml", "type: 42")

    errors = _validate_errors(capsys, contract)

    assert errors == [
        _prefixed("../../shared/columns.yaml", line, "exposes[0].contract.schema[0].type", BAD_TYPE)
    ]
    assert not any(os.path.isabs(e.split(":", 1)[0]) for e in errors)
    assert str(tmp_path) not in "\n".join(errors)


def test_h10_build_fragment_with_query_for_sql_names_the_key_and_the_object(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """SPEC GAP SG-26 (which line), characterised: an unexpected key is named where it
    is written; a missing one at the object that lacks it (the ``properties:`` line)."""
    root = tmp_path / "p"
    contract = _product(
        root,
        _indent_item(EXPOSE_ORDERS),
        builds=BUILD_ORDERS + "  - $ref: ./fragments/builds/b.yaml\n",
        files={
            "fragments/builds/b.yaml": (
                "# Build: b\n\nid: b\npattern: embedded-logic\nengine: sql\n"
                "properties:\n  query: SELECT 2\n"
            )
        },
    )
    fragment = root / "fragments" / "builds" / "b.yaml"

    errors = _validate_errors(capsys, contract)

    rel = "fragments/builds/b.yaml"
    assert sorted(errors) == sorted(
        [
            _prefixed(
                rel,
                _line_of(fragment, "query:"),
                "builds[1].properties",
                "Additional properties are not allowed ('query' was unexpected)",
            ),
            _prefixed(
                rel,
                _line_of(fragment, "properties:"),
                "builds[1].properties",
                "'sql' is a required property",
            ),
        ]
    )


def test_h11_json_output_carries_the_prefix_inside_the_error_strings(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """SPEC GAP SG-26, characterised: no new field; ``valid`` and ``schema_version`` as before."""
    root = tmp_path / "p"
    contract = _product(
        root,
        "  - $ref: ./fragments/exposes/orders.yaml\n",
        files={
            "fragments/exposes/orders.yaml": EXPOSE_ORDERS.replace("type: timestamp", "type: 42")
        },
    )
    line = _line_of(root / "fragments" / "exposes" / "orders.yaml", "type: 42")

    run = _cli(capsys, "validate", contract, "--format", "json")

    doc = _json_doc(run.out)
    assert run.code == 1
    assert doc["valid"] is False
    assert doc["schema_version"] == "0.7.6"
    assert doc["errors"] == [
        _prefixed(
            "fragments/exposes/orders.yaml", line, "exposes[0].contract.schema[1].type", BAD_TYPE
        )
    ]
    assert doc["warnings"] == []
    assert set(doc) == {"valid", "schema_version", "errors", "warnings", "validation_time"}


# ── H-12: every failure class names the ref, the file and the pointer ───


class Expect(NamedTuple):
    #: What the message must hold: the ref exactly as written.
    tokens: Tuple[str, ...]
    #: The file the failing ``$ref`` is written in, relative to the product.
    holder: str
    #: The RFC 6901 pointer of that ``$ref`` node inside ``holder``.
    pointer: str


CLASSES = [
    "not-found",
    "escape",
    "url",
    "absolute",
    "deny-list",
    "cycle",
    "depth",
    "budget",
    "non-string",
    "pointer-into-text",
    "siblings-on-text",
    "siblings-on-list",
    "yaml-parse-error",
    "bad-pointer",
    "bad-pointer-in-file",
    "pointer-pass-failure",
    "directory",
    "not-utf8-text",
    "text-over-the-cap",
    "ref-in-a-sibling",
    "list-index-out-of-range",
]


def _failing_product(
    name: str, where: str, root: Path, monkeypatch: pytest.MonkeyPatch
) -> Tuple[Path, Expect]:
    """A product whose composition fails with the ``name`` class of error.

    ``where`` puts the failing ``$ref`` node in a fragment (``fragments/exposes/
    out.yaml``, pointer ``/binding/location/path`` inside it) or in the root
    contract itself (pointer ``/exposes/0/binding/location/path``). Files a
    case needs are written beside the file that holds the failing ref."""
    fragment = where == "fragment"
    base_rel = "fragments/exposes" if fragment else ""
    base = root / base_rel if base_rel else root
    holder = "fragments/exposes/out.yaml" if fragment else "contract.fluid.yaml"
    pointer = "/binding/location/path" if fragment else "/exposes/0/binding/location/path"
    up = "../" * ((2 if fragment else 0) + 1)  # one step past the product root
    files: Dict[str, Union[str, bytes]] = {}
    tokens: Tuple[str, ...] = ()
    if name == "not-found":
        node, tokens = "{$ref: ./missing.txt}", ("./missing.txt",)
    elif name == "escape":
        node, tokens = f"{{$ref: {up}outside.txt}}", (f"{up}outside.txt",)
        _write(root.parent / "outside.txt", "SECRET-1\n")
    elif name == "url":
        node, tokens = "{$ref: 'https://example.com/x.yaml'}", ("https://example.com/x.yaml",)
    elif name == "absolute":
        node, tokens = "{$ref: /nonexistent/abs/x.yaml}", ("/nonexistent/abs/x.yaml",)
    elif name == "deny-list":
        if sys.platform == "win32":
            pytest.skip("the system-path deny list is POSIX")
        hosts = os.path.relpath("/etc/hosts", base)
        node, tokens = f"{{$ref: {hosts}}}", (hosts, "blocked system path")
        monkeypatch.setenv("FLUID_REF_ROOT", "/")
    elif name == "cycle":
        files = {"cycle-a.yaml": "next: {$ref: ./cycle-a.yaml}\n"}
        node, tokens = "{$ref: ./cycle-a.yaml}", ("Circular", "cycle-a.yaml")
        holder, pointer = f"{base_rel}/cycle-a.yaml".lstrip("/"), "/next"
    elif name == "depth":
        # The ref that crosses the limit is l<k> -> l<k+1>: the refs above it are
        # root -> out.yaml (a fragment only) and the failing node's own ref.
        k = contract_refs.MAX_REF_DEPTH - 1 - (1 if fragment else 0)
        files = {f"l{i}.yaml": f"next: {{$ref: ./l{i + 1}.yaml}}\n" for i in range(k + 1)}
        node, tokens = "{$ref: ./l0.yaml}", (f"./l{k + 1}.yaml",)
        holder, pointer = f"{base_rel}/l{k}.yaml".lstrip("/"), "/next"
    elif name == "budget":
        files = {"big.txt": "x" * 2_000}
        monkeypatch.setattr(contract_refs, "MAX_REF_EXPANSION_CHARS", 1_000)
        node, tokens = "{$ref: ./big.txt}", ("./big.txt",)
    elif name == "non-string":
        node = "{$ref: 42}"
    elif name == "pointer-into-text":
        files = {"q.sql": "SELECT 1\n"}
        node, tokens = "{$ref: ./q.sql#/a}", ("./q.sql#/a",)
    elif name == "siblings-on-text":
        files = {"q.sql": "SELECT 1\n"}
        node, tokens = "{$ref: ./q.sql, description: x}", ("./q.sql", "description")
    elif name == "siblings-on-list":
        files = {"rules.yaml": "- a\n- b\n"}
        node, tokens = "{$ref: ./rules.yaml, description: x}", ("./rules.yaml", "description")
    elif name == "yaml-parse-error":
        files = {"broken.yaml": "a: [1, 2\nb: 3\n"}
        node, tokens = "{$ref: ./broken.yaml}", ("./broken.yaml", "broken.yaml")
    elif name == "bad-pointer":
        node, tokens = '{$ref: "#foo"}', ("#foo",)
    elif name == "bad-pointer-in-file":
        files = {"x.yaml": "present: 1\n"}
        node, tokens = "{$ref: ./x.yaml#/nope}", ("./x.yaml#/nope",)
    elif name == "pointer-pass-failure":
        node, tokens = '{$ref: "#/extensions/nope"}', ("#/extensions/nope",)
    elif name == "directory":
        files = {"parts/part.yaml": "a: 1\n"}
        node, tokens = "{$ref: ./parts}", ("./parts",)
    elif name == "not-utf8-text":
        files = {"latin1.sql": b"SELECT 'caf\xe9'\n"}
        node, tokens = "{$ref: ./latin1.sql}", ("./latin1.sql",)
    elif name == "text-over-the-cap":
        files = {"long.md": "x" * 100}
        monkeypatch.setattr(contract_refs, "MAX_TEXT_REF_BYTES", 10)
        node, tokens = "{$ref: ./long.md}", ("./long.md",)
    elif name == "ref-in-a-sibling":
        # The failing ref is the sibling's own: one level below the node that holds $ref.
        files = {"t.yaml": "k: 1\n"}
        node, tokens = "{$ref: ./t.yaml, extra: {$ref: ./missing.txt}}", ("./missing.txt",)
        pointer += "/extra"
    elif name == "list-index-out-of-range":
        files = {"l.yaml": "items: [a, b]\n"}
        node, tokens = "{$ref: ./l.yaml#/items/9}", ("./l.yaml#/items/9",)
    else:  # pragma: no cover - a typo in CLASSES
        raise AssertionError(name)

    body = EXPOSE_AT_PATH.replace("@NODE@", node)
    if fragment:
        _write(root / "fragments" / "exposes" / "out.yaml", body)
        contract = _product(root, "  - $ref: ./fragments/exposes/out.yaml\n")
    else:
        contract = _product(root, _indent_item(body))
    for rel, data in files.items():
        if isinstance(data, bytes):
            (base / rel).parent.mkdir(parents=True, exist_ok=True)
            (base / rel).write_bytes(data)
        else:
            _write(base / rel, data)
    return contract, Expect(tokens, holder, pointer)


def _fail(
    command: str, contract: Path, capsys: pytest.CaptureFixture, tmp_path: Path, *extra: Any
) -> str:
    """What ``command`` (LC, V or B) says about a contract that cannot be composed."""
    if command == "LC":
        env = extra[0] if extra else None
        with pytest.raises(loader.RefResolutionError) as info:
            if env:
                loader.load_with_overlay(contract, env)
            else:
                loader.load_contract(contract)
        return str(info.value)
    if command == "V":
        run = _cli(capsys, "validate", contract, *extra)
        assert run.code == 1, run.text
        return run.text
    out = tmp_path / "bundled.yaml"
    run = _cli(capsys, "bundle", contract, "--out", out, *extra)
    assert run.code == 2, run.text
    assert not out.exists(), "a contract that cannot be composed writes no bundle"
    return run.text


def _assert_names(text: str, expect: Expect, *, location: bool = True) -> None:
    flat = _squash(text)
    assert "cli_unhandled_exception" not in text
    for token in expect.tokens:
        assert _squash(token) in flat, f"{token!r} is not named in: {text}"
    if location:
        assert re.search(
            r"(?<![\w/~.])" + re.escape(expect.pointer) + r"(?![\w/~])", flat
        ), f"the pointer {expect.pointer} is not named in: {text}"
        assert expect.holder in flat, f"{expect.holder} is not named in: {text}"


@pytest.mark.parametrize("command", ["LC", "V", "B"])
@pytest.mark.parametrize("where", ["fragment", "root"])
@pytest.mark.parametrize("name", CLASSES)
def test_h12_every_failure_class_names_the_ref_the_file_and_the_pointer(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    where: str,
    command: str,
) -> None:
    contract, expect = _failing_product(name, where, tmp_path / "p", monkeypatch)

    text = _fail(command, contract, capsys, tmp_path)

    _assert_names(text, expect, location=name != "deny-list")
    assert "SECRET-1" not in text


@pytest.mark.parametrize("command", ["LC", "V", "B"])
@pytest.mark.parametrize("where", ["fragment", "root"])
def test_h12_deny_list_error_names_the_file_and_the_pointer(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    where: str,
    command: str,
) -> None:
    contract, expect = _failing_product("deny-list", where, tmp_path / "p", monkeypatch)

    text = _fail(command, contract, capsys, tmp_path)

    _assert_names(text, expect, location=True)


def _overlay_product(root: Path, overlay: str) -> Path:
    return _product(
        root,
        "  - $ref: ./fragments/exposes/orders.yaml\n",
        files={"fragments/exposes/orders.yaml": EXPOSE_ORDERS, "overlays/prod.yaml": overlay},
    )


OVERLAY_CLASSES = {
    "ref-not-found": (
        "exposes:\n  - exposeId: orders\n    binding:\n      location:\n"
        "        path: {$ref: ../missing-prod.txt}\n",
        Expect(("../missing-prod.txt",), "overlays/prod.yaml", "/exposes/0/binding/location/path"),
    ),
    "escape": (
        "exposes:\n  - exposeId: orders\n    binding:\n      location:\n"
        "        path: {$ref: ../../outside.txt}\n",
        Expect(("../../outside.txt",), "overlays/prod.yaml", "/exposes/0/binding/location/path"),
    ),
    "pointer-to-nothing": (
        "exposes:\n  - exposeId: orders\n    description: {$ref: '#/extensions/nope'}\n",
        Expect(("#/extensions/nope",), "overlays/prod.yaml", "/exposes/0/description"),
    ),
    "top-level-pointer-to-nothing": (
        "extensions:\n  x: {$ref: '#/extensions/nope'}\n",
        Expect(("#/extensions/nope",), "overlays/prod.yaml", "/extensions/x"),
    ),
}


@pytest.mark.parametrize("command", ["LC", "V", "B"])
@pytest.mark.parametrize("name", sorted(OVERLAY_CLASSES))
def test_h12_overlay_ref_failure_names_the_overlay_file_and_its_own_pointer(
    tmp_path: Path, capsys: pytest.CaptureFixture, name: str, command: str
) -> None:
    overlay, expect = OVERLAY_CLASSES[name]
    contract = _overlay_product(tmp_path / "p", overlay)
    extra = ("prod",) if command == "LC" else ("--env", "prod")

    text = _fail(command, contract, capsys, tmp_path, *extra)

    _assert_names(text, expect)


_MERGED_POINTER = (
    "exposes:\n  - exposeId: orders\n    binding:\n      location: {$ref: '#/extensions/nope'}\n"
)


@pytest.mark.parametrize("command", ["LC", "V", "B"])
def test_h12_overlay_pointer_merged_into_a_base_object_is_still_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture, command: str
) -> None:
    contract = _overlay_product(tmp_path / "p", _MERGED_POINTER)
    extra = ("prod",) if command == "LC" else ("--env", "prod")

    text = _fail(command, contract, capsys, tmp_path, *extra)

    assert "#/extensions/nope" in _squash(text)
    assert "cli_unhandled_exception" not in text


@pytest.mark.parametrize("command", ["LC", "V", "B"])
def test_h12_overlay_pointer_merged_into_a_base_object_names_the_overlay_file(
    tmp_path: Path, capsys: pytest.CaptureFixture, command: str
) -> None:
    """Decided: a pointer the overlay writes over an object of the base is refused, and
    the refusal names the overlay file, the pointer's place in it and the remedy."""
    contract = _overlay_product(tmp_path / "p", _MERGED_POINTER)
    extra = ("prod",) if command == "LC" else ("--env", "prod")

    text = _fail(command, contract, capsys, tmp_path, *extra)

    flat = _squash(text)
    assert "overlays/prod.yaml" in flat
    assert _squash("at JSON pointer '/exposes/0/binding/location'") in flat
    assert _squash("cannot be merged into an object") in flat
    assert _squash("name a file ('./part.yaml#/...')") in flat


def test_h12_pointer_escapes_slash_and_tilde_in_keys(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """The pointer is RFC 6901: a key ``a/b~c`` is ``/a~1b~0c``, and the file is the fragment."""
    root = tmp_path / "p"
    contract = _product(
        root,
        _indent_item(EXPOSE_ORDERS),
        extra="extensions:\n  odd: {$ref: ./fragments/odd.yaml}\n",
        files={"fragments/odd.yaml": "a/b~c:\n  - {$ref: ./missing.txt}\n"},
    )

    for command in ("LC", "V", "B"):
        text = _fail(command, contract, capsys, tmp_path)
        _assert_names(
            text, Expect(("./missing.txt",), "fragments/odd.yaml", "/a~1b~0c/0"), location=True
        )


@pytest.mark.parametrize("ref", ["./missing.txt", "#/extensions/nope"], ids=["file", "pointer"])
def test_h12_a_ref_nested_in_a_pointed_subtree_reports_its_pointer_in_that_file(
    tmp_path: Path, capsys: pytest.CaptureFixture, ref: str
) -> None:
    """``./x.yaml#/section`` brings in a subtree; a failing ref in it is at its own place in x.yaml."""
    root = tmp_path / "p"
    contract = _product(
        root,
        _indent_item(EXPOSE_ORDERS),
        extra="extensions:\n  s: {$ref: './x.yaml#/section'}\n",
        files={"x.yaml": f"other: 1\nsection:\n  inner: {{$ref: '{ref}'}}\n"},
    )

    for command in ("LC", "V", "B"):
        text = _fail(command, contract, capsys, tmp_path)
        _assert_names(text, Expect((ref,), "x.yaml", "/section/inner"))


# ── H-13, H-14, H-15: the load-failure text ─────────────────────────────


def test_h13_yaml_syntax_error_in_a_fragment_names_it_with_line_and_column(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    root = tmp_path / "p"
    broken = EXPOSE_ORDERS.replace(
        "  location: {path: runtime/out/orders.csv}\n",
        "  location: {path: runtime/out/orders.csv\n",
    )
    contract = _product(
        root,
        "  - $ref: ./fragments/exposes/orders.yaml\n",
        files={"fragments/exposes/orders.yaml": broken},
    )
    fragment = root / "fragments" / "exposes" / "orders.yaml"
    line = _line_of(fragment, "location: {path")
    column = fragment.read_text(encoding="utf-8").splitlines()[line - 1].index("{") + 1

    for command in ("LC", "V"):
        text = _fail(command, contract, capsys, tmp_path)
        assert "fragments/exposes/orders.yaml" in text  # the ref, as written, relative
        assert 'in "orders.yaml"' in text
        assert f"line {line}, column {column}" in text
        assert "<unicode string>" not in text
        assert text.count("Failed to parse") == 1, "one wrapper, not a nest of them"


def test_h13_yaml_syntax_error_in_the_root_or_an_overlay_names_the_file_and_line(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    root = tmp_path / "p"
    contract = _product(root, _indent_item(EXPOSE_ORDERS))
    overlay = _write(root / "overlays" / "prod.yaml", "exposes: [\n  - exposeId: orders\n")

    for argv, file in (
        (("validate", contract, "--env", "prod"), overlay),
        (
            ("validate", _write(root / "broken.fluid.yaml", "id: [unclosed\nname: x\n")),
            root / "broken.fluid.yaml",
        ),
    ):
        run = _cli(capsys, *argv)
        assert run.code == 1, run.text
        assert _squash(str(file)) in _squash(run.text)
        assert re.search(r"line \d+, column \d+", run.text), run.text
        assert "cli_unhandled_exception" not in run.text


def test_h14_escape_error_is_a_ref_error_on_validate(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Precondition of H-14: the refusal itself is right."""
    contract, expect = _failing_product("escape", "fragment", tmp_path / "p", monkeypatch)

    text = _fail("V", contract, capsys, tmp_path)

    assert "escapes the ref root" in text
    _assert_names(text, expect)


@pytest.mark.xfail(
    strict=True,
    reason="BUG P5-05: a ref error on validate gets the generic contract_load_failed hints: "
    "'Verify the YAML/JSON syntax is valid' and the circular \"Run 'fluid validate'\"",
)
def test_h14_escape_error_carries_no_misleading_hint(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    contract, _ = _failing_product("escape", "fragment", tmp_path / "p", monkeypatch)

    text = _fail("V", contract, capsys, tmp_path)

    assert "Verify the YAML/JSON syntax is valid" not in text
    assert "Run 'fluid validate" not in text


@pytest.mark.parametrize("name", ["escape", "cycle", "pointer-pass-failure"])
def test_h15_one_ref_failure_reads_the_same_in_every_command(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    """validate, bundle, plan, generate standard and the library say the same core
    message; only the label around it and the exit code differ."""
    from fluid_build.api.contract import ContractLoadError
    from fluid_build.api.contract import load_contract as api_load_contract

    contract, expect = _failing_product(name, "fragment", tmp_path / "p", monkeypatch)
    core = _fail("LC", contract, capsys, tmp_path)
    _assert_names(core, expect)

    runs = {
        "validate": _cli(capsys, "validate", contract),
        "bundle": _cli(capsys, "bundle", contract, "--out", tmp_path / "bundled.yaml"),
        "plan": _cli(capsys, "plan", contract, "--out", tmp_path / "plan.json"),
        "generate standard": _cli(
            capsys,
            "generate",
            "standard",
            contract,
            "--format",
            "odcs",
            "--out",
            tmp_path / "odcs.yaml",
        ),
    }
    for label, run in runs.items():
        assert run.code != 0, label
        assert "cli_unhandled_exception" not in run.text, label
        assert _squash(core) in _squash(run.text), f"{label} does not carry: {core}\n{run.text}"
    with pytest.raises(ContractLoadError) as info:
        api_load_contract(contract)
    assert info.value.event == "contract_ref_unresolved"
    assert _squash(core) in _squash(info.value.message)


# ── H-16 .. H-20: what the prefix must not leak, break, or miss ─────────

_CATALOG_AUTH_SECRET = "spark:2OR3eRvYfSZzzZ16MlPd95jhLnOaLM52"  # pragma: allowlist secret

_ICEBERG_BUILD = (
    "# Build: ingest_events\n"
    "\n"
    "id: ingest_events\n"
    "pattern: acquisition\n"
    "engine: kafka-connect\n"
    "properties:\n"
    "  source:\n"
    "    kind: postgres\n"
    "    mode: incremental_append\n"
    "    streams: [public.events]\n"
    "  sink:\n"
    "    format: iceberg\n"
    "    catalogAuth:\n"
    "      type: oauth2\n"
    '      credentialEnv: "@SECRET@"\n'
    "      oauth2ServerUri: http://keycloak:8080/realms/iceberg/protocol/openid-connect/token\n"
    "      scope: lakekeeper\n"
    "  kafka-connect: {iceberg_sink_enabled: true}\n"
)

_ICEBERG_EXPOSE = (
    "exposeId: events\n"
    "kind: table\n"
    'version: "1.0.0"\n'
    "binding:\n"
    "  platform: local\n"
    "  format: iceberg\n"
    "  location:\n"
    "    catalog: lakekeeper\n"
    "    uri: http://lakekeeper:8181/catalog\n"
    "    warehouse: demo\n"
    "    database: bronze\n"
    "    table: events\n"
    "contract:\n"
    "  schema:\n"
    "    - {name: id, type: integer, required: true}\n"
)


def test_h16_prefixed_credential_message_still_hides_the_value(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    root = tmp_path / "p"
    contract = _product(
        root,
        _indent_item(_ICEBERG_EXPOSE),
        builds="builds:\n  - $ref: ./fragments/builds/ingest_events.yaml\n",
        files={
            "fragments/builds/ingest_events.yaml": _ICEBERG_BUILD.replace(
                "@SECRET@", _CATALOG_AUTH_SECRET
            )
        },
    )
    line = _line_of(root / "fragments" / "builds" / "ingest_events.yaml", "credentialEnv:")

    for argv in (
        ("validate", contract),
        ("validate", contract, "--format", "json"),
        ("plan", contract, "--out", tmp_path / "plan.json"),
    ):
        run = _cli(capsys, *argv)
        assert run.code == 1, run.text
        assert _CATALOG_AUTH_SECRET not in run.text
        assert "2OR3eRvYfSZzz" not in run.text
    errors = _validate_errors(capsys, contract)
    prefix = _prefixed(
        "fragments/builds/ingest_events.yaml",
        line,
        "builds[0].properties.sink.catalogAuth.credentialEnv",
        "",
    )
    schema_errors = [e for e in errors if e.startswith(prefix)]
    assert len(schema_errors) == 1, errors
    assert "(value not shown)" in schema_errors[0]


def test_h17_brackets_in_the_path_survive_the_text_output(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    root = tmp_path / "p"
    contract = _product(
        root,
        "  - $ref: ./fragments/exposes/orders.yaml\n",
        files={
            "fragments/exposes/orders.yaml": EXPOSE_ORDERS.replace("type: timestamp", "type: 42")
        },
    )

    run = _cli(capsys, "validate", contract)

    assert run.code == 1
    assert "(exposes[0].contract.schema[1].type)" in run.out
    assert "exposes[0]" in run.out and "schema[1]" in run.out


@pytest.mark.parametrize("directory", ["[wip]", "[bold red]x[/]", "ördér items"])
def test_h17_brackets_and_unicode_in_a_fragment_path_survive_the_prefix_and_ref_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture, directory: str
) -> None:
    """A path like ``fragments/[wip]/x.yaml`` is data: no markup reads it, no text is lost."""
    root = tmp_path / "p"
    rel = f"fragments/{directory}/orders.yaml"
    contract = _product(root, f"  - $ref: './{rel}'\n")
    fragment = _write(root / rel, EXPOSE_ORDERS.replace("type: timestamp", "type: 42"))
    line = _line_of(fragment, "type: 42")
    expected = _prefixed(rel, line, "exposes[0].contract.schema[1].type", BAD_TYPE)

    assert _validate_errors(capsys, contract) == [expected]
    assert f" 1. {expected}\n" in _cli(capsys, "validate", contract).out

    fragment.write_text(
        EXPOSE_AT_PATH.replace("@NODE@", "{$ref: './missing[1].txt'}"), encoding="utf-8"
    )
    for command in ("V", "B"):
        text = _fail(command, contract, capsys, tmp_path)
        flat = _squash(text)
        assert "missing[1].txt" in flat
        assert _squash(rel) in flat
        assert "'/binding/location/path'" in flat


_LINE_FRAGMENT = (
    "# Expose: orders\n"
    "\n"
    "exposeId: orders\n"
    "kind: table\n"
    'version: "1.0.0"\n'
    "binding:\n"
    "  platform: local\n"
    "  format: csv\n"
    "  location: {path: runtime/out/orders.csv}\n"
    "contract:\n"
    "  schema:\n"
    "    - {name: order_id, type: string, required: true}\n"
    "    - name: order_time\n"
    "      type: 42\n"
)


@pytest.mark.parametrize(
    "ending,bom",
    [("\n", False), ("\r\n", False), ("\r", False), ("\n", True), ("\r\n", True)],
    ids=["lf", "crlf", "cr", "bom-lf", "bom-crlf"],
)
def test_h18_line_numbers_are_right_for_crlf_cr_and_a_byte_order_mark(
    tmp_path: Path, capsys: pytest.CaptureFixture, ending: str, bom: bool
) -> None:
    root = tmp_path / "p"
    contract = _product(root, "  - $ref: ./fragments/exposes/orders.yaml\n")
    fragment = root / "fragments" / "exposes" / "orders.yaml"
    fragment.parent.mkdir(parents=True)
    data = _LINE_FRAGMENT.replace("\n", ending).encode("utf-8")
    fragment.write_bytes((b"\xef\xbb\xbf" if bom else b"") + data)
    line = _line_of(fragment, "type: 42")
    assert line == _LINE_FRAGMENT.splitlines().index("      type: 42") + 1

    errors = _validate_errors(capsys, contract)

    assert errors == [
        _prefixed(
            "fragments/exposes/orders.yaml", line, "exposes[0].contract.schema[1].type", BAD_TYPE
        )
    ]


def test_h19_a_value_reached_through_an_alias_is_named_at_the_anchors_line(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    root = tmp_path / "p"
    fragment = EXPOSE_ORDERS.replace(
        "    - {name: order_id, type: string, required: true}\n"
        "    - {name: order_time, type: timestamp, required: true}\n"
        "    - {name: amount, type: number}\n",
        "    - &bad\n      name: order_id\n      type: 42  # the anchor\n    - *bad\n",
    )
    assert "*bad" in fragment
    contract = _product(
        root,
        "  - $ref: ./fragments/exposes/orders.yaml\n",
        files={"fragments/exposes/orders.yaml": fragment},
    )
    line = _line_of(root / "fragments" / "exposes" / "orders.yaml", "# the anchor")

    errors = _validate_errors(capsys, contract)

    rel = "fragments/exposes/orders.yaml"
    assert errors == [
        _prefixed(rel, line, "exposes[0].contract.schema[0].type", BAD_TYPE),
        _prefixed(rel, line, "exposes[0].contract.schema[1].type", BAD_TYPE),
    ]


@pytest.mark.xfail(
    strict=True,
    reason="BUG P5-08: a value a fragment inherits through a YAML merge key (<<: *anchor) "
    "gets no prefix, which reads as an error in the root contract",
)
def test_h19_a_value_inherited_through_a_merge_key_is_still_named_after_the_fragment(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    root = tmp_path / "p"
    fragment = EXPOSE_ORDERS.replace(
        "    - {name: order_id, type: string, required: true}\n"
        "    - {name: order_time, type: timestamp, required: true}\n"
        "    - {name: amount, type: number}\n",
        "    - &col\n      name: order_id\n      type: 42\n    - <<: *col\n      name: other\n",
    )
    contract = _product(
        root,
        "  - $ref: ./fragments/exposes/orders.yaml\n",
        files={"fragments/exposes/orders.yaml": fragment},
    )

    errors = _validate_errors(capsys, contract)

    assert len(errors) == 2
    inherited = [e for e in errors if "schema[1].type" in e]
    assert len(inherited) == 1
    assert inherited[0].startswith("fragments/exposes/orders.yaml:"), errors


_DENIED_EXPOSE = EXPOSE_ORDERS.replace(
    "  platform: local\n  format: csv\n  location: {path: runtime/out/orders.csv}\n",
    "  platform: gcp\n  format: bigquery_table\n"
    "  location: {project: p, dataset: d, table: t, region: us-east1}\n",
)


def test_h20_rule_error_for_fragment_content_is_not_prefixed(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """SPEC GAP SG-26, characterised: the contract-level rules (here a region the
    sovereignty fragment denies) name the expose, not the fragment."""
    root = tmp_path / "p"
    contract = _product(
        root,
        _indent_item(_DENIED_EXPOSE),
        extra="sovereignty:\n  $ref: ./fragments/sovereignty.yaml\n",
        files={
            "fragments/sovereignty.yaml": "enforcementMode: strict\ndeniedRegions: [us-east1]\n"
        },
    )

    errors = _validate_errors(capsys, contract)

    assert len(errors) == 1
    assert errors[0].startswith(
        "❌ [orders] Region 'us-east1' is explicitly denied by sovereignty policy"
    )
    assert "fragments/sovereignty.yaml" not in errors[0]
    text = _cli(capsys, "validate", contract)
    assert "[orders] Region 'us-east1' is explicitly denied" in text.out, "markup eats no brackets"


def test_h20_extension_validator_error_names_the_fragment_of_its_block(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SPEC GAP SG-26, characterised: a plugin's text is kept; the block it is about
    is named, at the block's first line."""
    root = tmp_path / "p"
    contract = _product(
        root,
        _indent_item(EXPOSE_ORDERS),
        extra="extensions:\n  acme: {$ref: ./ext/acme.yaml}\n",
        files={"ext/acme.yaml": "# Acme block\n\nplatform: azure\nowner: x\n"},
    )
    monkeypatch.setattr(
        "fluid_build.extension_schemas.run_extension_validators",
        lambda contract, logger: ["extensions.acme: platform: unexpected"],
    )
    line = _line_of(root / "ext" / "acme.yaml", "platform:")

    errors = _validate_errors(capsys, contract)

    assert errors == [f"ext/acme.yaml:{line} (extensions.acme): platform: unexpected"]


def test_h20_role_validator_finding_for_fragment_content_is_not_prefixed(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SPEC GAP SG-26, characterised: a Validator plugin's finding is plugin text."""
    root = tmp_path / "p"
    contract = _product(
        root,
        "  - $ref: ./fragments/exposes/orders.yaml\n",
        files={"fragments/exposes/orders.yaml": EXPOSE_ORDERS},
    )
    finding = {
        "plugin": "acme-rules",
        "code": "R1",
        "severity": "error",
        "message": "bad thing",
        "path": "exposes[0].binding",
    }
    monkeypatch.setattr(
        "fluid_build.plugin_manager.collect_validator_findings", lambda contract, logger: [finding]
    )

    errors = _validate_errors(capsys, contract)

    assert errors == ["[acme-rules] R1: bad thing (at exposes[0].binding)"]


# ── H-21: the library tells the same ────────────────────────────────────

PARITY_ROOT = (
    'fluidVersion: "0.7.6"\n'
    "kind: DataProduct\n"
    "id: parity.orders\n"
    "name: Parity Orders\n"
    "description:\n"
    "  purpose: P-TEXT\n"
    "  usage: {$ref: ./docs/usage.md}\n"
    "  limitations: none\n"
    "domain: ops\n"
    "metadata:\n"
    "  layer: Silver\n"
    "  owner: {$ref: ./shared/team.yaml}\n"
    "builds:\n"
    "  - $ref: ./fragments/builds/transform.yaml\n"
    "exposes:\n"
    "  - $ref: ./fragments/exposes/out.yaml\n"
    "extensions:\n"
    "  acme: {$ref: ./ext/acme.yaml}\n"
    "  paths: {out: runtime/out/dev.csv}\n"
)

PARITY_FILES = {
    "shared/team.yaml": "team: platform-ops\nemail: ops@example.com\n",
    "shared/params.yaml": "retries: 3\nmode: base\n",
    "ext/acme.yaml": "owner: acme\nnested:\n  a: 1\n",
    "docs/usage.md": "# Usage\n\nS5-USAGE: join on `id`.\n",
    "sql/transform.sql": "-- S2-DEV\nSELECT 1 AS id\n",
    "sql/transform.prod.sql": "-- S2-PROD\nSELECT 1 AS id\n",
    "fragments/builds/transform.yaml": (
        "# Build: transform\n\n"
        "id: transform\n"
        "pattern: embedded-logic\n"
        "engine: sql\n"
        "properties:\n"
        "  sql: {$ref: ../../sql/transform.sql}\n"
        "  parameters: {$ref: ../../shared/params.yaml, mode: dev}\n"
    ),
    "fragments/exposes/out.yaml": (
        "# Expose: out\n\n"
        "exposeId: out\n"
        "kind: table\n"
        'version: "1.0.0"\n'
        'description: {$ref: "#/description/purpose"}\n'
        "binding:\n"
        "  platform: local\n"
        "  format: csv\n"
        "  location:\n"
        '    path: {$ref: "#/extensions/paths/out"}\n'
        "contract:\n"
        "  schema: {$ref: ../../schemas/out-columns.yaml}\n"
        "  quality: {$ref: ../../quality/out.yaml}\n"
    ),
    "schemas/out-columns.yaml": (
        "# Columns: out\n\n- {name: id, type: integer, required: true}\n- {name: label, type: string}\n"
    ),
    "quality/out.yaml": (
        '# Quality rules: out\n\n- {rule: id_present, expression: "id IS NOT NULL", severity: error}\n'
    ),
    "overlays/prod.yaml": (
        "extensions:\n"
        "  paths: {out: runtime/out/S1-prod.csv}\n"
        "builds:\n"
        "  - id: transform\n"
        "    properties:\n"
        "      sql: {$ref: ../sql/transform.prod.sql}\n"
        "      parameters: {mode: S3-prod}\n"
        "exposes:\n"
        "  - exposeId: out\n"
        "    binding: {$ref: ../fragments/prod-binding.yaml}\n"
    ),
    "fragments/prod-binding.yaml": 'location: {path: {$ref: "#/extensions/paths/out"}}\n',
}


def _parity(root: Path) -> Path:
    """FX-PARITY: every mechanism at once, with an overlay for ``prod``."""
    contract = _write(root / "contract.fluid.yaml", PARITY_ROOT)
    for rel, text in PARITY_FILES.items():
        _write(root / rel, text)
    return contract


def _origin(loaded: Any, root: Path, *path: Union[str, int]) -> Optional[Tuple[str, Optional[int]]]:
    """``(relative file, line)`` the library gives for ``path``, or ``None``."""
    found = loaded.origin_of(list(path))
    if found is None:
        return None
    file, line = found
    assert file.is_absolute()
    return file.relative_to(root.resolve()).as_posix(), line


def test_h21_origin_of_names_file_and_line_on_the_parity_fixture_under_prod(
    tmp_path: Path,
) -> None:
    from fluid_build.api.contract import load_contract

    root = tmp_path / "p"
    contract = _parity(root)
    loaded = load_contract(contract, env="prod")

    def line(rel: str, needle: str) -> int:
        return _line_of(root / rel, needle)

    assert _origin(loaded, root, "id") == (
        "contract.fluid.yaml",
        line("contract.fluid.yaml", "id: parity.orders"),
    )
    # A column came from the list fragment; the expose fragment only holds the ref.
    assert _origin(loaded, root, "exposes", 0, "contract", "schema", 1, "type") == (
        "schemas/out-columns.yaml",
        line("schemas/out-columns.yaml", "name: label"),
    )
    assert _origin(loaded, root, "exposes", 0, "kind") == (
        "fragments/exposes/out.yaml",
        line("fragments/exposes/out.yaml", "kind: table"),
    )
    # The path is the overlay's (through the pointer) or the prod-binding fragment's.
    assert _origin(loaded, root, "exposes", 0, "binding", "location", "path") in {
        ("overlays/prod.yaml", line("overlays/prod.yaml", "out: runtime/out/S1-prod.csv")),
        ("fragments/prod-binding.yaml", line("fragments/prod-binding.yaml", "location:")),
    }
    assert _origin(loaded, root, "description", "usage") == ("docs/usage.md", None)
    assert _origin(loaded, root, "metadata", "owner", "team") == ("shared/team.yaml", 1)
    assert _origin(loaded, root, "builds", 0, "properties", "parameters", "retries") == (
        "shared/params.yaml",
        line("shared/params.yaml", "retries: 3"),
    )
    assert _origin(loaded, root, "extensions", "acme", "owner") == ("ext/acme.yaml", 1)
    assert _origin(loaded, root, "no", "such", "path") is None
    assert loaded.overlay == (root / "overlays" / "prod.yaml").resolve()


def test_h21_origin_of_without_an_env_names_the_base_files(tmp_path: Path) -> None:
    from fluid_build.api.contract import load_contract

    root = tmp_path / "p"
    contract = _parity(root)
    loaded = load_contract(contract)

    assert _origin(loaded, root, "exposes", 0, "binding", "location", "path") == (
        "contract.fluid.yaml",
        _line_of(contract, "paths: {out: runtime/out/dev.csv}"),
    )
    assert _origin(loaded, root, "builds", 0, "properties", "sql") == ("sql/transform.sql", None)
    assert _origin(loaded, root, "builds", 0, "properties", "parameters", "mode") == (
        "fragments/builds/transform.yaml",
        _line_of(root / "fragments" / "builds" / "transform.yaml", "parameters:"),
    )


@pytest.mark.xfail(
    strict=True,
    reason="BUG P5-07: a text ref the overlay replaces a base text ref with is skipped as a "
    "'patch', so the value (per-env SQL) is named after the base file",
)
def test_h21_origin_of_per_env_sql_names_the_overlays_text_file(tmp_path: Path) -> None:
    from fluid_build.api.contract import load_contract

    root = tmp_path / "p"
    contract = _parity(root)
    loaded = load_contract(contract, env="prod")

    assert loaded.contract["builds"][0]["properties"]["sql"].startswith("-- S2-PROD")
    assert _origin(loaded, root, "builds", 0, "properties", "sql") == (
        "sql/transform.prod.sql",
        None,
    )


@pytest.mark.xfail(
    strict=True,
    reason="BUG P5-02: the overlay's mode: S3-prod is named after the base build fragment, "
    "which holds a ref (with siblings) deeper than the overlay's entry",
)
def test_h21_origin_of_a_key_the_overlay_overrode_names_the_overlay(tmp_path: Path) -> None:
    from fluid_build.api.contract import load_contract

    root = tmp_path / "p"
    contract = _parity(root)
    loaded = load_contract(contract, env="prod")

    assert loaded.contract["builds"][0]["properties"]["parameters"]["mode"] == "S3-prod"
    assert _origin(loaded, root, "builds", 0, "properties", "parameters", "mode") == (
        "overlays/prod.yaml",
        _line_of(root / "overlays" / "prod.yaml", "parameters: {mode: S3-prod}"),
    )


def test_h21_origin_of_takes_a_path_of_keys_and_indexes(tmp_path: Path) -> None:
    """SPEC GAP SG-27, characterised: the argument is a list of keys and integer list
    indexes. A JSON pointer string, or an index written as text, matches nothing
    (``None``), and is not an error."""
    from fluid_build.api.contract import load_contract

    root = tmp_path / "p"
    loaded = load_contract(_parity(root))

    assert loaded.origin_of(["exposes", 0, "kind"]) is not None
    assert loaded.origin_of(("exposes", 0, "kind")) == loaded.origin_of(["exposes", 0, "kind"])
    assert loaded.origin_of("/exposes/0/kind") is None
    assert loaded.origin_of(["exposes", "0", "kind"]) is None
    line = loaded.origin_of(["exposes", 0, "kind"])
    assert line is not None and isinstance(line[0], Path) and isinstance(line[1], int)
    assert line[1] >= 1, "lines are 1-based"


def test_h21_in_memory_loads_know_their_fragments_only_with_a_base_dir(tmp_path: Path) -> None:
    """SPEC GAP SG-22/SG-27, characterised: with a ``base_dir`` the fragments read are
    located; the in-memory root itself is a document with no file, so it is not."""
    from fluid_build.api.contract import load_contract_from_dict, load_contract_from_text

    root = tmp_path / "p"
    _parity(root)
    document = {
        "id": "x",
        "description": {"purpose": "P"},
        "extensions": {"paths": {"out": "runtime/out/dev.csv"}},
        "exposes": [{"$ref": "./fragments/exposes/out.yaml"}],
    }

    without = load_contract_from_dict(dict(document))
    with_dir = load_contract_from_dict(dict(document), base_dir=root)
    from_text = load_contract_from_text(
        "id: x\ndescription: {purpose: P}\nextensions: {paths: {out: runtime/out/dev.csv}}\n"
        "exposes:\n  - $ref: ./fragments/exposes/out.yaml\n",
        base_dir=root,
    )

    assert without.origin_of(["id"]) is None
    for loaded in (with_dir, from_text):
        assert _origin(loaded, root, "exposes", 0, "kind") == (
            "fragments/exposes/out.yaml",
            _line_of(root / "fragments" / "exposes" / "out.yaml", "kind: table"),
        )
        assert loaded.origin_of(["id"]) is None, "an in-memory root has no file"


def test_h21_collect_origins_hands_back_the_origins_of_each_composition(tmp_path: Path) -> None:
    root = tmp_path / "p"
    contract = _parity(root)

    with loader.collect_origins() as collected:
        loader.load_with_overlay(contract, "prod")
        loader.load_contract(contract)
    loader.load_contract(contract)  # outside any block nothing is collected, and nothing breaks

    assert len(collected) == 2
    assert all(isinstance(o, loader.ContractOrigins) for o in collected)
    prod, base = collected
    assert prod.describe(["exposes", 0, "kind"]) == (
        f"fragments/exposes/out.yaml:{_line_of(root / 'fragments/exposes/out.yaml', 'kind: table')}"
    )
    assert prod.describe(["id"]) is None, "the root contract is not named"
    assert base.locate(["exposes", 0, "kind"]) is not None


# ── H-22, H-23: bundles and plan ────────────────────────────────────────


def _bad_split_product(root: Path) -> Path:
    return _product(
        root,
        "  - $ref: ./fragments/exposes/orders.yaml\n",
        files={
            "fragments/exposes/orders.yaml": EXPOSE_ORDERS.replace("type: timestamp", "type: 42")
        },
    )


def test_h22_validating_a_bundle_has_no_provenance_and_keeps_the_old_wording(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    contract = _bad_split_product(tmp_path / "p")
    tgz = tmp_path / "bad.tgz"
    assert _cli(capsys, "bundle", contract, "--format", "tgz", "--out", tgz).code == 0

    run = _cli(capsys, "validate", tgz)

    flat = _squash(run.text)
    assert run.code == 1
    assert (
        _squash(f"contract.resolved.yaml: at exposes/0/contract/schema/1/type: {BAD_TYPE}") in flat
    )
    assert "fragments/exposes/orders.yaml" not in run.text


def test_h22_the_library_has_no_origins_for_a_bundle(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    from fluid_build.api.contract import load_contract

    contract = _bad_split_product(tmp_path / "p")
    tgz = tmp_path / "bad.tgz"
    assert _cli(capsys, "bundle", contract, "--format", "tgz", "--out", tgz).code == 0

    loaded = load_contract(tgz)

    assert loaded.origin == "bundle"
    assert loaded.origin_of(["exposes", 0, "kind"]) is None


@pytest.mark.xfail(
    strict=True,
    reason="BUG P5-01: fluid plan (and apply) check the schema without the composition's "
    "origins, so their contract_schema_error detail has no fragment or overlay prefix",
)
def test_h23_plan_schema_error_is_the_string_validate_prints(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    contract = _bad_split_product(tmp_path / "p")
    prefixed = _validate_errors(capsys, contract)
    assert len(prefixed) == 1 and prefixed[0].startswith("fragments/exposes/orders.yaml:")

    run = _cli(capsys, "plan", contract, "--out", tmp_path / "plan.json")

    assert run.code != 0
    assert "contract_schema_error" in run.text
    assert prefixed[0] in run.text


def test_h23_plan_refuses_a_schema_invalid_split_contract(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """Precondition of H-23: plan stops on the same error validate reports."""
    contract = _bad_split_product(tmp_path / "p")

    run = _cli(capsys, "plan", contract, "--out", tmp_path / "plan.json")

    assert run.code != 0
    assert "contract_schema_error" in run.text
    assert BAD_TYPE in run.text
    assert not (tmp_path / "plan.json").exists()


# ── H-24: a pointer's copy of a fragment's node ─────────────────────────

_BINDING_WITH_BOGUS = (
    "platform: local\nformat: csv\nlocation: {path: runtime/out/a.csv}\nbogus: 1\n"
)


def _pointer_copies(root: Path) -> Path:
    """Three exposes, one binding written by ``fragments/binding.yaml``:
    ``exposes[0]`` refs the file, ``exposes[1]`` copies it with ``#/exposes/0/binding``
    (a pointer through a list index), ``exposes[2]`` copies it with
    ``#/extensions/shared/binding`` (a pointer of keys, to a ref in ``extensions``)."""

    def expose(number: int, binding: str) -> str:
        text = EXPOSE_ORDERS.replace("exposeId: orders", f"exposeId: orders{number}")
        head, _, tail = text.partition("binding:\n")
        _, _, rest = tail.partition("contract:\n")
        return _indent_item(f"{head}binding: {binding}\ncontract:\n{rest}")

    return _product(
        root,
        expose(0, "{$ref: ./fragments/binding.yaml}")
        + expose(1, '{$ref: "#/exposes/0/binding"}')
        + expose(2, '{$ref: "#/extensions/shared/binding"}'),
        extra="extensions:\n  shared:\n    binding: {$ref: ./fragments/binding.yaml}\n",
        files={"fragments/binding.yaml": _BINDING_WITH_BOGUS},
    )


def _bogus_errors(errors: Sequence[str]) -> Dict[int, str]:
    """The 'bogus' error of each expose, by its index."""
    found: Dict[int, str] = {}
    for error in errors:
        match = re.search(r"exposes\[(\d)\]\.binding\)?: Additional properties", error)
        if match:
            found[int(match.group(1))] = error
    return found


def _site(error: str) -> str:
    """What names the file in an error: the text before ``(exposes[...``, or nothing."""
    return error.split(" (exposes[", 1)[0] if " (exposes[" in error else ""


def test_h24_a_pointer_copy_through_keys_is_named_after_the_node_it_copies(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """SPEC GAP SG-26 (pointer site or target site), characterised for a pointer made of keys:
    the copy is named after the fragment that wrote the node, as the file ref itself is."""
    root = tmp_path / "p"
    contract = _pointer_copies(root)
    line = _line_of(root / "fragments" / "binding.yaml", "bogus")

    errors = _bogus_errors(_validate_errors(capsys, contract))

    where = f"fragments/binding.yaml:{line}"
    assert set(errors) == {0, 1, 2}
    assert _site(errors[0]) == where
    assert _site(errors[2]) == where


@pytest.mark.xfail(
    strict=True,
    reason="BUG P5-03: a '#/' pointer that passes through a list index (#/exposes/0/...) is "
    "recorded with text tokens ('0') the origin table does not match, so its copy is not "
    "named after the node it copies, while the same copy through keys is",
)
def test_h24_a_pointer_copy_through_a_list_index_is_attributed_like_one_through_keys(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """Whichever site SG-26 settles on, the two pointer shapes must agree."""
    contract = _pointer_copies(tmp_path / "p")

    errors = _bogus_errors(_validate_errors(capsys, contract))

    assert set(errors) == {0, 1, 2}
    assert _site(errors[1]) == _site(errors[2]), errors


# ── H-25: lines are the file's own, whatever header `fluid split` wrote ─

_FLAT_ALL_PARTS = (
    "# Header comment\n"
    + HEAD.replace(
        "description: Orders data product.\n", "description:\n  purpose: Orders.\n  usage: Join.\n"
    )
    + "sovereignty:\n"
    + "  enforcementMode: advisory\n"
    + "  allowedRegions: [europe-west1]\n"
    + "accessPolicy:\n"
    + "  grants:\n"
    + "    - principal: group:analysts@example.com\n"
    + "      permissions: [read]\n"
    + "builds:\n"
    + "  - id: build_orders\n"
    + "    pattern: embedded-logic\n"
    + "    engine: sql\n"
    + "    properties:\n"
    + "      sql: |\n"
    + "        SELECT 1 AS order_id\n"
    + "        FROM src\n"
    + "exposes:\n"
    + "  - exposeId: orders\n"
    + "    kind: table\n"
    + '    version: "1.0.0"\n'
    + "    binding:\n"
    + "      platform: local\n"
    + "      format: csv\n"
    + "      location: {path: runtime/out/orders.csv}\n"
    + "    contract:\n"
    + "      schema:\n"
    + "        - {name: order_id, type: string, required: true}\n"
    + "        - {name: order_time, type: timestamp, required: true}\n"
    + "        - {name: amount, type: number}\n"
    + "      quality:\n"
    + '        - {rule: id_present, expression: "order_id IS NOT NULL", severity: error}\n'
    + "  - exposeId: customers\n"
    + "    kind: table\n"
    + '    version: "1.0.0"\n'
    + "    binding:\n"
    + "      platform: local\n"
    + "      format: csv\n"
    + "      location: {path: runtime/out/customers.csv}\n"
    + "    contract:\n"
    + "      schema:\n"
    + "        - {name: customer_id, type: string, required: true}\n"
)

_ALL_PARTS = "sovereignty,accessPolicy,builds,exposes,sql,quality,schema,description"

#: (fragment, text to break, what it becomes, the path the error is at)
_BREAKS = [
    (
        "fragments/schemas/orders.yaml",
        "type: timestamp",
        "type: 42",
        "exposes[0].contract.schema[1].type",
    ),
    (
        "fragments/quality/orders.yaml",
        "severity: error",
        "severity: 42",
        "exposes[0].contract.quality[0].severity",
    ),
    (
        "fragments/builds/build_orders.yaml",
        "pattern: embedded-logic",
        "pattern: 42",
        "builds[0].pattern",
    ),
    (
        "fragments/sovereignty.yaml",
        "allowedRegions: [europe-west1]",
        "allowedRegions: 7",
        "sovereignty.allowedRegions",
    ),
    (
        "fragments/access-policy.yaml",
        "permissions: [read]",
        "permissions: 5",
        "accessPolicy.grants[0].permissions",
    ),
    ("fragments/exposes/customers.yaml", "kind: table", "kind: 42", "exposes[1].kind"),
]


def test_h25_provenance_lines_match_the_real_lines_of_every_split_fragment(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    root = tmp_path / "p"
    contract = _write(root / "contract.fluid.yaml", _FLAT_ALL_PARTS)
    assert _cli(capsys, "validate", contract).code == 0, "the flat contract is valid"
    assert _cli(capsys, "split", contract, "--parts", _ALL_PARTS).code == 0
    assert _cli(capsys, "validate", contract).code == 0, "so is the split one"
    for rel, old, new, _ in _BREAKS:
        fragment = root / rel
        text = fragment.read_text(encoding="utf-8")
        assert old in text, f"{old!r} is not in {rel}: {text!r}"
        fragment.write_text(text.replace(old, new), encoding="utf-8")

    errors = _validate_errors(capsys, contract)

    for rel, _, new, path in _BREAKS:
        line = _line_of(root / rel, new)
        assert any(e.startswith(f"{rel}:{line} ({path}): ") for e in errors), (rel, line, errors)
    assert len(errors) >= len(_BREAKS)
    assert all(re.match(r"(fragments/[\w./-]+\.yaml):\d+ \(", e) for e in errors), errors


# ── E-08, E-26: cycle, depth and cap errors — the message halves ────────


def _followed(document: Mapping[str, Any], steps: int) -> Any:
    """The value ``steps`` levels down a ``next`` chain."""
    node: Any = document["extensions"]["x"]
    for _ in range(steps):
        node = node["next"]
    return node


def test_e08_a_chain_of_20_file_refs_composes(tmp_path: Path) -> None:
    contract = _chain(tmp_path / "p", contract_refs.MAX_REF_DEPTH)

    document = loader.load_contract(contract)

    assert _followed(document, contract_refs.MAX_REF_DEPTH - 1) == {"end": True}


def test_e08_the_21st_file_ref_names_the_ref_the_file_and_the_pointer_where_it_stopped(
    tmp_path: Path,
) -> None:
    refs = contract_refs.MAX_REF_DEPTH + 1
    contract = _chain(tmp_path / "p", refs)

    with pytest.raises(loader.RefResolutionError) as info:
        loader.load_contract(contract)

    message = str(info.value)
    assert f"nesting depth exceeded {contract_refs.MAX_REF_DEPTH}" in message
    # Ref number 21 is c19 -> c20, written at /next of c19.yaml.
    _assert_names(
        message, Expect((f"./c{refs - 1}.yaml",), f"c{refs - 2}.yaml", "/next"), location=True
    )


def _int_list(count: int) -> str:
    return "[" + ", ".join(str(i) for i in range(count)) + "]"


def _alias_bomb() -> str:
    """Nine levels of nine aliases each: 9**9 nodes from 72 aliases."""
    lines = ['a0: &a0 ["lol", "lol", "lol", "lol", "lol", "lol", "lol", "lol", "lol"]']
    for i in range(1, 9):
        lines.append(f"a{i}: &a{i} [{', '.join([f'*a{i - 1}'] * 9)}]")
    return "\n".join(lines) + "\n"


def _e26_product(name: str, root: Path, monkeypatch: pytest.MonkeyPatch) -> Tuple[Path, Expect]:
    """A product whose composition stops with the ``name`` error of area E, and where."""
    exposes = _indent_item(EXPOSE_ORDERS)
    files: Dict[str, str] = {}
    root_file = "contract.fluid.yaml"
    if name == "file-cycle":
        files = {"a.yaml": "x: {$ref: ./a.yaml}\n"}
        extra, expect = "extensions:\n  e: {$ref: ./a.yaml}\n", Expect(
            ("Circular",), "a.yaml", "/x"
        )
    elif name == "two-file-cycle":
        files = {"a.yaml": "x: {$ref: ./b.yaml}\n", "b.yaml": "y: {$ref: ./a.yaml}\n"}
        extra, expect = "extensions:\n  e: {$ref: ./a.yaml}\n", Expect(
            ("Circular",), "b.yaml", "/y"
        )
    elif name == "cycle-through-a-sibling":
        files = {"a.yaml": "x: {$ref: ./b.yaml, back: {$ref: ./a.yaml}}\n", "b.yaml": "k: 1\n"}
        extra = "extensions:\n  e: {$ref: ./a.yaml}\n"
        expect = Expect(("Circular",), "a.yaml", "/x/back")
    elif name == "pointer-cycle":
        extra = 'extensions:\n  p: {$ref: "#/extensions/q"}\n  q: {$ref: "#/extensions/p"}\n'
        expect = Expect(("Circular", "#/extensions/"), root_file, "/extensions/q")
    elif name == "pointer-into-own-subtree":
        extra = 'extensions:\n  a:\n    b: {$ref: "#/extensions/a"}\n'
        expect = Expect(("Circular", "#/extensions/a"), root_file, "/extensions/a/b")
    elif name == "whole-document-pointer":
        extra = 'extensions:\n  x: {$ref: "#"}\n'
        expect = Expect(("Circular", "'#'"), root_file, "/extensions/x")
    elif name == "depth":
        contract = _chain(root, contract_refs.MAX_REF_DEPTH + 1)
        return contract, Expect(
            (f"./c{contract_refs.MAX_REF_DEPTH}.yaml",),
            f"c{contract_refs.MAX_REF_DEPTH - 1}.yaml",
            "/next",
        )
    elif name == "file-cap":
        files = {"big.yaml": f"{_int_list(300)}\n"}
        monkeypatch.setattr(contract_refs, "MAX_REF_EXPANSION_NODES", 800)
        # 301 values each: the third ref (c) takes the total past 800.
        extra = "extensions:\n" + "".join(f"  {k}: {{$ref: ./big.yaml}}\n" for k in "abcd")
        expect = Expect(("./big.yaml",), root_file, "/extensions/c")
    elif name == "pointer-cap":
        monkeypatch.setattr(contract_refs, "MAX_REF_EXPANSION_NODES", 800)
        extra = f"extensions:\n  a: {_int_list(300)}\n" + "".join(
            f'  {k}: {{$ref: "#/extensions/a"}}\n' for k in "bcd"
        )
        expect = Expect(("#/extensions/a",), root_file, "/extensions/d")  # a is inline: b, c, d
    elif name == "key-characters-cap":
        files = {"keys.yaml": "{" + "k" * 2_000 + ": 1}\n"}
        monkeypatch.setattr(contract_refs, "MAX_REF_EXPANSION_CHARS", 1_000)
        extra = "extensions:\n  e: {$ref: ./keys.yaml}\n"
        expect = Expect(("./keys.yaml",), root_file, "/extensions/e")
    elif name == "alias-copies-cap":
        copies = ", ".join(["*b"] * 40)
        files = {"aliases.yaml": f"base: &b [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]\ncopies: [{copies}]\n"}
        monkeypatch.setattr(contract_refs, "MAX_REF_EXPANSION_NODES", 300)
        extra = "extensions:\n  e: {$ref: ./aliases.yaml}\n"
        expect = Expect(("./aliases.yaml",), root_file, "/extensions/e")
    elif name == "file-and-pointer-cap":
        # One budget for the whole composition: two file refs (802 values) leave room
        # for two pointer copies of the same list, and the third crosses 2,000.
        files = {"f.yaml": f"{_int_list(400)}\n"}
        monkeypatch.setattr(contract_refs, "MAX_REF_EXPANSION_NODES", 2_000)
        extra = (
            "extensions:\n  a: {$ref: ./f.yaml}\n  b: {$ref: ./f.yaml}\n"
            '  c: {$ref: "#/extensions/a"}\n  d: {$ref: "#/extensions/a"}\n'
            '  e: {$ref: "#/extensions/a"}\n'
        )
        expect = Expect(("#/extensions/a",), root_file, "/extensions/e")
    elif name == "alias-bomb":
        files = {"bomb.yaml": _alias_bomb()}
        extra, expect = "extensions:\n  e: {$ref: ./bomb.yaml}\n", Expect(
            ("bomb.yaml",), root_file, "/extensions/e"
        )
    else:  # pragma: no cover - a typo in E26_CLASSES
        raise AssertionError(name)
    contract = _product(root, exposes, extra=extra, files=files)
    return contract, expect


E26_CLASSES = [
    "file-cycle",
    "two-file-cycle",
    "cycle-through-a-sibling",
    "pointer-cycle",
    "pointer-into-own-subtree",
    "whole-document-pointer",
    "depth",
    "file-cap",
    "pointer-cap",
    "key-characters-cap",
    "alias-copies-cap",
    "file-and-pointer-cap",
    "alias-bomb",
]


@pytest.mark.parametrize("name", E26_CLASSES)
def test_e26_cycle_depth_and_cap_errors_are_ref_errors_never_an_unhandled_exception(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    contract, expect = _e26_product(name, tmp_path / "p", monkeypatch)

    with pytest.raises(loader.RefResolutionError) as info:
        loader.load_contract(contract)
    _assert_names(str(info.value), expect)
    validate = _fail("V", contract, capsys, tmp_path)  # exit code 1
    bundle = _fail("B", contract, capsys, tmp_path)  # exit code 2, nothing written

    for text in (validate, bundle):
        _assert_names(text, expect)
        assert "Traceback" not in text
    assert "$ref resolution error" in bundle


def test_e26_a_yaml_alias_that_contains_itself_is_a_clean_error_but_not_a_ref_error(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """E-15 on the CLI, characterised: the document cannot be written out, which the
    loader says is 'not a ref failure'; validate exits 1 as for any load failure, and
    bundle says it could not compile (exit 1), not '$ref resolution error' (exit 2)."""
    root = tmp_path / "p"
    contract = _product(
        root,
        _indent_item(EXPOSE_ORDERS),
        extra="extensions:\n  e: {$ref: ./loop.yaml}\n",
        files={"loop.yaml": "a: &a\n  b: *a\n"},
    )

    validate = _cli(capsys, "validate", contract)
    bundle = _cli(capsys, "bundle", contract, "--out", tmp_path / "bundled.yaml")

    assert validate.code == 1 and bundle.code == 1
    for run in (validate, bundle):
        assert "cli_unhandled_exception" not in run.text
        assert "Traceback" not in run.text
        flat = _squash(run.text)
        assert "loop.yaml" in flat
        assert "containsitself" in flat
        assert "'/a/b'" in flat, "the pointer inside the fragment"
