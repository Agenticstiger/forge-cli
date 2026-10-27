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

"""``fluid generate standard`` applies ``--env`` and accepts a bare ``-o`` file name.

Two defects, both in the file-writing exporters rather than the providers:

* ``--env`` was accepted and ignored. ``_export_odcs`` and ``_export_odps_bitol``
  loaded the contract with ``load_contract(path)`` instead of
  ``load_contract_with_overlay(path, env, logger)``, so ``--format odcs --env prod``
  exported the base contract without a word. Only the odps-v4.1 path applied the
  overlay, which made the symptom easy to miss: ``fluid odps export --env`` and
  ``fluid generate artifacts --env`` wrote the overlay's Kafka topic while
  ``generate standard --format odcs --env`` wrote the base one.
* ``-o out.yaml`` (no directory part) failed with ``[Errno 2] No such file or
  directory: ''``. ``os.path.dirname("")`` is ``""`` and ``os.makedirs("")``
  raises, while ``-o ./out.yaml`` worked, which hid it from anyone who tried a
  path with a slash in it.

Each test drives the command's own argparse surface into ``run()`` and asserts
on the document that lands on disk, never on a log line.
"""

from __future__ import annotations

import argparse
import ast
import logging
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, NamedTuple, Optional

import pytest
import yaml

from fluid_build.cli import generate_standard

pytestmark = pytest.mark.unit

LOG = logging.getLogger("test.generate_standard_env_and_out")

_CONTRACT = """\
fluidVersion: "0.7.5"
kind: DataProduct
id: demo.orders_stream
name: Orders Stream
description: Order events.
domain: sales
metadata:
  layer: Gold
  owner: {team: platform-team, email: platform@example.com}
exposes:
  - exposeId: orders_events
    kind: stream
    version: 1.0.0
    binding:
      platform: kafka
      format: kafka_topic
      location:
        topic: orders.base.v1
        bootstrap: broker:9092
    contract:
      schema:
        - {name: order_id, type: string}
"""

_ENV = "prod"


def _write_project(root: Path, overlay_binding: Optional[Dict[str, Any]] = None) -> Path:
    """Write the contract and, when given, a ``prod`` overlay that rebinds its one expose."""
    root.mkdir(parents=True)
    contract = root / "contract.fluid.yaml"
    contract.write_text(_CONTRACT, encoding="utf-8")
    if overlay_binding is not None:
        (root / "overlays").mkdir()
        overlay = {"exposes": [{"exposeId": "orders_events", "binding": overlay_binding}]}
        (root / "overlays" / f"{_ENV}.yaml").write_text(yaml.safe_dump(overlay), encoding="utf-8")
    return contract


def _run(fmt: str, contract: Path, out: str, env: Optional[str] = None) -> int:
    parser = argparse.ArgumentParser(prog="fluid")
    sub = parser.add_subparsers(dest="cmd")
    generate_standard.register_subcommand(sub)
    argv: List[str] = ["standard", str(contract), "--format", fmt, "-o", out]
    if env:
        argv += ["--env", env]
    return generate_standard.run(parser.parse_args(argv), LOG)


def _odcs_physical_name(out: Path) -> str:
    # A single-port contract collapses to one document in the multi-document stream.
    docs = list(yaml.safe_load_all(out.read_text(encoding="utf-8")))
    assert len(docs) == 1, docs
    return docs[0]["schema"][0]["physicalName"]


def _odps_port_type(out: Path) -> str:
    doc = yaml.safe_load(out.read_text(encoding="utf-8"))
    assert doc["kind"] == "DataProduct", doc
    return doc["outputPorts"][0]["type"]


class _Case(NamedTuple):
    #: What the ``prod`` overlay writes over the expose's ``binding``.
    overlay_binding: Dict[str, Any]
    #: Reads the one binding-derived value this format records out of the export.
    read: Callable[[Path], str]
    base: str
    overlaid: str


# The bare Bitol product records exactly one thing about a binding: its platform,
# as ``outputPorts[].type``. The topic lives in the per-port ODCS contract that
# this file deliberately leaves out, so for ODPS the overlay swaps the platform,
# which is also the idiom the repo's platform-swap examples use. ODCS carries the
# topic itself, as ``schema[].physicalName``.
_ODPS_CASE = _Case({"platform": "snowflake"}, _odps_port_type, "kafka", "snowflake")
_CASES = {
    "odcs": _Case(
        {"location": {"topic": "orders.prod.v1"}},
        _odcs_physical_name,
        "orders.base.v1",
        "orders.prod.v1",
    ),
    "odps": _ODPS_CASE,
    "odps-bitol": _ODPS_CASE,
}


@pytest.mark.parametrize("fmt", _CASES)
def test_env_overlay_is_reflected_in_the_export(fmt: str, tmp_path: Path) -> None:
    case = _CASES[fmt]
    contract = _write_project(tmp_path / "project", case.overlay_binding)
    out = tmp_path / "export.yaml"

    assert _run(fmt, contract, str(out), env=_ENV) == 0

    assert case.read(out) == case.overlaid
    # Not merely "the new value is somewhere": the base one must be gone.
    assert case.base not in out.read_text(encoding="utf-8")


@pytest.mark.parametrize("fmt", _CASES)
def test_export_without_env_is_the_base_contract(fmt: str, tmp_path: Path) -> None:
    """The overlay sits right next to the contract and must not be picked up unasked."""
    case = _CASES[fmt]
    contract = _write_project(tmp_path / "project", case.overlay_binding)
    out = tmp_path / "export.yaml"

    assert _run(fmt, contract, str(out)) == 0

    assert case.read(out) == case.base
    assert case.overlaid not in out.read_text(encoding="utf-8")


@pytest.mark.parametrize("fmt", generate_standard.SUPPORTED_FORMATS)
def test_a_bare_out_file_name_is_written_to_the_working_directory(
    fmt: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``-o out.yaml`` has an empty directory part, which ``makedirs`` cannot take."""
    contract = _write_project(tmp_path / "project")
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)

    assert _run(fmt, contract, "out.yaml") == 0

    # The file is there, is this product's export, and nothing else was written
    # alongside it (no stray ``runtime/exports`` from the default output path).
    assert [p.name for p in cwd.iterdir()] == ["out.yaml"]
    assert "demo.orders_stream" in (cwd / "out.yaml").read_text(encoding="utf-8")


@pytest.mark.parametrize("fmt", generate_standard.SUPPORTED_FORMATS)
def test_a_nested_out_path_still_has_its_directories_created(
    fmt: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bare-name fix must not cost the directory creation a real path relies on."""
    contract = _write_project(tmp_path / "project")
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)

    assert _run(fmt, contract, "exports/nested/out.yaml") == 0

    exported = cwd / "exports" / "nested" / "out.yaml"
    assert "demo.orders_stream" in exported.read_text(encoding="utf-8")


@pytest.mark.parametrize("fmt", ["odps", "odps-bitol"])
@pytest.mark.parametrize("out", ["out.yaml", "exports/nested/out.yaml"])
def test_the_import_fallback_also_writes_a_bare_out_file_name(
    fmt: str, out: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ``except ImportError`` skeleton path had its own, unguarded ``makedirs``.

    The Bitol exporter falls back to a minimal skeleton when importing
    ``fluid_build.providers.odps_standard`` fails. That branch called
    ``os.makedirs(os.path.dirname(out))`` bare, so ``-o out.yaml`` died with
    ``FileNotFoundError: ''`` there even after the main branch was fixed. A normal
    install never takes it, so the bare-name test above cannot reach it; a ``None``
    entry in ``sys.modules`` makes the import raise ``ImportError`` and forces it.
    """
    contract = _write_project(tmp_path / "project")
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setitem(sys.modules, "fluid_build.providers.odps_standard", None)

    assert _run(fmt, contract, out) == 0

    doc = yaml.safe_load((cwd / out).read_text(encoding="utf-8"))
    # The skeleton, not the full provider output: this is what proves the fallback ran.
    assert doc == {
        "dataProductSpecification": "1.0.0",
        "info": {"title": "Orders Stream", "x-fluidId": "demo.orders_stream"},
    }
    if "/" not in out:
        assert [p.name for p in cwd.iterdir()] == [out]


def _is_dirname_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    return (isinstance(func, ast.Attribute) and func.attr == "dirname") or (
        isinstance(func, ast.Name) and func.id == "dirname"
    )


def _is_makedirs_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call) or not node.args:
        return False
    func = node.func
    return (isinstance(func, ast.Attribute) and func.attr == "makedirs") or (
        isinstance(func, ast.Name) and func.id == "makedirs"
    )


def _guards_the_empty_dirname(arg: ast.AST) -> bool:
    """True for exactly ``os.path.dirname(x) or "."``."""
    return (
        isinstance(arg, ast.BoolOp)
        and isinstance(arg.op, ast.Or)
        and len(arg.values) == 2
        and _is_dirname_call(arg.values[0])
        and isinstance(arg.values[1], ast.Constant)
        and arg.values[1].value == "."
    )


def test_no_makedirs_in_the_module_is_handed_a_bare_dirname() -> None:
    """A fifth ``makedirs(dirname(out))`` site must not reintroduce the bare-name crash.

    One of the four sites was missed once, and a commit message then claimed all four
    were covered. Counting by hand is how that happened, so this reads the module
    and checks every ``makedirs`` whose path comes from ``dirname`` instead.
    """
    source = Path(generate_standard.__file__).read_text(encoding="utf-8")
    sites = [
        node
        for node in ast.walk(ast.parse(source))
        if _is_makedirs_call(node) and any(_is_dirname_call(n) for n in ast.walk(node.args[0]))
    ]
    # A scan that matches nothing would pass vacuously. If the sites were folded into a
    # helper, point this test at the helper's makedirs call instead of deleting it.
    assert sites, "found no makedirs(dirname(...)) call in generate_standard.py"

    bare = sorted(node.lineno for node in sites if not _guards_the_empty_dirname(node.args[0]))
    assert not bare, (
        f"generate_standard.py line(s) {bare} call makedirs on a bare dirname, which is "
        '"" for a file name with no directory part and makes makedirs raise '
        'FileNotFoundError. Write os.makedirs(os.path.dirname(out) or ".", exist_ok=True).'
    )
