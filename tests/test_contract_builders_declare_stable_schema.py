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

"""Code that builds a contract declares the newest stable bundled schema.

The five ``fluid import`` importers, ``fluid product new``, the discover
emitter, the streaming preview, the JDBC data-model scaffolder,
``forge/product_types`` and ``forge/templates/_v073_builder`` declared
``0.7.3`` as a literal while 0.7.5 was current: a version typed into code goes
stale every release (#619 measured it). They read
``FluidSchemaManager.latest_bundled_version()`` now, so a promotion moves them
too. The behaviour tests check that builders follow that function, not a
value; the fence keeps a literal from coming back.
"""

from __future__ import annotations

import argparse
import ast
import json
import logging
import pathlib
import re
from typing import Iterator

import pytest

from fluid_build.schema_manager import FluidSchemaManager

LOG = logging.getLogger("test.contract_builders_declare_stable_schema")

#: A bundled version that is neither the old literal (0.7.3) nor today's newest
#: stable one, so a builder that still types either is caught.
PINNED = "0.7.4"


@pytest.fixture
def pinned(monkeypatch: pytest.MonkeyPatch) -> str:
    assert PINNED in FluidSchemaManager.BUNDLED_VERSIONS
    monkeypatch.setattr(FluidSchemaManager, "latest_bundled_version", classmethod(lambda c: PINNED))
    return PINNED


def test_the_streaming_preview(pinned: str) -> None:
    from fluid_build.cli._streaming_contract_preview import shape_contract_from_context

    assert shape_contract_from_context({})["fluidVersion"] == pinned


def test_a_product_type_answer_and_the_contract_it_shapes(pinned: str) -> None:
    from fluid_build.forge.product_types import ProductTypeAnswer, shape_contract

    answer = ProductTypeAnswer(product_type="CDP", name="orders")
    assert answer.fluid_version == pinned  # a default_factory, not a frozen default
    assert shape_contract(answer)["fluidVersion"] == pinned


def test_the_forge_template_builder(pinned: str) -> None:
    from fluid_build.forge.templates._v073_builder import TemplateSpec, build_contract

    spec = TemplateSpec(template_name="analytics", product_type="ADP")
    assert build_contract(spec=spec, project_config={})["fluidVersion"] == pinned
    # An explicit version still wins.
    explicit = build_contract(spec=spec, project_config={}, fluid_version="0.7.2")
    assert explicit["fluidVersion"] == "0.7.2"


def test_fluid_product_new(pinned: str, tmp_path: pathlib.Path) -> None:
    from fluid_build.cli.product_new import SAMPLE, run

    args = argparse.Namespace(id="gold.orders_v1", out_dir=str(tmp_path))
    assert run(args, LOG) == 0
    written = json.loads((tmp_path / "gold_orders_v1" / "contract.fluid.json").read_text())
    assert written["fluidVersion"] == pinned
    assert list(written)[:3] == ["fluidVersion", "kind", "id"]
    assert written["id"] == "gold.orders_v1"
    assert "fluidVersion" not in SAMPLE and SAMPLE["id"] == "gold.customer360_v1"  # untouched


def test_the_discover_emitter(pinned: str) -> None:
    from fluid_build.cli.discover.emitter import emit_contract
    from fluid_build.cli.discover.registry import DiscoveredStream

    contract = emit_contract(
        product_id="bronze.orders",
        name="orders",
        domain="sales",
        owner_team="data",
        owner_email="data@example.com",
        engine="duckdb",
        source_kind="filesystem",
        connection={},
        streams=[DiscoveredStream(name="orders.csv", columns=[])],
    )
    assert contract["fluidVersion"] == pinned


# ── The fence ──────────────────────────────────────────────────────────────

ROOT = pathlib.Path(__file__).resolve().parents[1] / "fluid_build"

#: Files that may keep a version literal, each with its reason.
ALLOWED = {
    # A public fixture third-party runners test their implementations against
    # (``fluid_build.api.conformance``): its declared version is part of the
    # input they were written for.
    "api/conformance/fixtures/minimal.py",
}

_VERSION = re.compile(r"^\d+\.\d+\.\d+$")


def _is_version(node: object) -> bool:
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and bool(_VERSION.match(node.value))
    )


def _is_key(node: object) -> bool:
    return isinstance(node, ast.Constant) and node.value == "fluidVersion"


def _literals(tree: ast.AST) -> Iterator[int]:
    """Lines where code sets a contract's version to a literal: a ``fluidVersion``
    key in a dict display, ``x["fluidVersion"] = ...``, a ``fluidVersion=``
    keyword, or a ``fluid_version`` default."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=True):
                if _is_key(key) and _is_version(value):
                    yield value.lineno
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Subscript) and _is_key(target.slice):
                    if _is_version(node.value):
                        yield node.lineno
        elif isinstance(node, ast.Call):
            for keyword in node.keywords:
                if keyword.arg == "fluidVersion" and _is_version(keyword.value):
                    yield keyword.value.lineno
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == "fluid_version":
                if _is_version(node.value):
                    yield node.lineno
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args
            positional = args.posonlyargs + args.args
            with_default = positional[len(positional) - len(args.defaults) :]
            pairs = list(zip(with_default, args.defaults, strict=True))
            pairs += [
                (a, d)
                for a, d in zip(args.kwonlyargs, args.kw_defaults, strict=True)
                if d is not None
            ]
            for arg, default in pairs:
                if arg.arg == "fluid_version" and _is_version(default):
                    yield default.lineno


def test_no_contract_builder_types_a_version() -> None:
    found = []
    for path in sorted(ROOT.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if rel in ALLOWED:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        found += [f"fluid_build/{rel}:{line}" for line in _literals(tree)]
    assert not found, (
        "these set a contract's fluidVersion to a literal, which goes stale every "
        "release; use FluidSchemaManager.latest_bundled_version(): " + ", ".join(found)
    )


@pytest.mark.parametrize(
    "source",
    [
        'c = {"fluidVersion": "0.7.3"}',
        'c["fluidVersion"] = "0.7.3"',
        'c = dict(fluidVersion="0.7.3")',
        "class A:\n    fluid_version: str = '0.7.3'",
        "def f(*, fluid_version='0.7.3'): pass",
        "def f(fluid_version='0.7.3'): pass",
    ],
)
def test_the_fence_sees_each_form(source: str) -> None:
    assert list(_literals(ast.parse(source)))


def test_the_allowed_files_still_exist() -> None:
    for rel in ALLOWED:
        assert (ROOT / rel).is_file(), rel
