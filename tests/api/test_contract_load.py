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

"""``fluid_build.api.load_contract*`` returns the contract ``fluid plan`` plans.

The stability contract of the public loader is one equation, held here for
every rewrite the engine makes (alias values, a legacy ``build:``, ``$ref``
composition, an environment overlay, a bundle):

    load_contract(path, env=env).contract == plan.json["contract"]
                                          == load_contract_from_text(text, ...).contract

``plan.json`` comes from the real ``fluid plan`` command ``run()``, so a
change to the engine's loader that this API does not follow fails here.
"""

from __future__ import annotations

import argparse
import ast
import builtins
import copy
import inspect
import json
import logging
import os
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import pytest

import fluid_build.api as api
from fluid_build import _contract_loader
from fluid_build._contract_loader import load_contract_with_overlay
from fluid_build.api import ContractLoadError, LoadedContract
from fluid_build.api import contract as contract_api
from fluid_build.cli import bundle as bundle_cmd
from fluid_build.cli import plan as plan_cmd
from fluid_build.forge.core.plan_digest import coerce_keys_to_str, compute_contract_digest
from fluid_build.util.safe_yaml import load_yaml_safe

pytestmark = [pytest.mark.unit]

LOG = logging.getLogger("test.api.contract_load")

# Alias values in three places the alias table covers: build source kind
# (``pg``), build source mode (``incremental``), expose binding format
# (``iceberg_table``). None of them is a schema enum value; all are planned.
_ALIASES = """\
fluidVersion: "0.7.5"
kind: DataProduct
id: bronze.orders
name: Orders
metadata:
  layer: Bronze
  owner: {team: dp, email: dp@example.com}
builds:
  - id: ingest
    pattern: acquisition
    engine: kafka-connect
    properties:
      source: {kind: pg, mode: incremental}
      sink: {format: iceberg}
exposes:
  - exposeId: orders
    kind: table
    binding:
      platform: aws
      format: iceberg_table
      location: {database: s, table: o}
    contract:
      schema:
        - {name: id, type: integer, required: true}
"""

# The legacy singular ``build:`` (schema-valid), which the engine plans as
# ``builds: [build]``.
_LEGACY_BUILD = """\
fluidVersion: "0.7.5"
kind: DataProduct
id: bronze.orders
name: Orders
metadata:
  layer: Bronze
  owner: {team: dp, email: dp@example.com}
build:
  id: ingest
  pattern: acquisition
  engine: kafka-connect
  properties:
    source: {kind: postgres, mode: incremental_append}
    sink: {format: iceberg}
exposes:
  - exposeId: orders
    kind: table
    binding:
      platform: aws
      format: iceberg
      location: {database: s, table: o}
    contract:
      schema:
        - {name: id, type: integer, required: true}
"""

# ``$ref`` composition plus an overlay that patches a field the ``$ref``
# pulled in, and an alias inside the referenced fragment.
_COMPOSED = """\
fluidVersion: "0.7.5"
kind: DataProduct
id: bronze.orders
name: Orders
metadata:
  layer: Bronze
  owner: {team: dp, email: dp@example.com}
build:
  id: ingest
  pattern: acquisition
  engine: kafka-connect
  properties:
    source: {kind: postgres, mode: incremental_append}
    sink: {format: iceberg}
exposes:
  - {"$ref": "./parts/orders.yaml"}
"""

_ORDERS_FRAGMENT = """\
exposeId: orders
kind: table
binding:
  platform: aws
  format: iceberg-table
  location: {database: s, table: o}
contract:
  schema:
    - {name: id, type: integer, required: true}
"""

_PROD_OVERLAY = """\
name: Orders (prod)
exposes:
  - binding:
      location: {database: prod_s}
"""


def _write(root: Path, files: Dict[str, str]) -> Path:
    for rel, text in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return root / "contract.fluid.yaml"


_CASES: Dict[str, Dict[str, str]] = {
    "aliases": {"contract.fluid.yaml": _ALIASES},
    "legacy_build": {"contract.fluid.yaml": _LEGACY_BUILD},
    "composed": {
        "contract.fluid.yaml": _COMPOSED,
        "parts/orders.yaml": _ORDERS_FRAGMENT,
        "overlays/prod.yaml": _PROD_OVERLAY,
    },
}


def _parse(register: Callable[[Any], None], argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="fluid")
    parser.add_argument("--provider", default=None)
    parser.add_argument("--project", default=None)
    parser.add_argument("--region", default=None)
    sub = parser.add_subparsers(dest="cmd")
    register(sub)
    return parser.parse_args(argv)


def _plan(src: Path, out: Path, env: Optional[str] = None) -> Dict[str, Any]:
    argv = ["plan", str(src), "--out", str(out)] + (["--env", env] if env else [])
    assert plan_cmd.run(_parse(plan_cmd.register, argv), LOG) == 0
    return json.loads(out.read_text(encoding="utf-8"))


@pytest.fixture
def ws(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    return tmp_path


# ── the stability contract: equal to what plan plans ──────────────────


@pytest.mark.parametrize(
    "case, env",
    [
        ("aliases", None),
        ("legacy_build", None),
        ("composed", None),
        ("composed", "prod"),
    ],
)
def test_load_contract_equals_the_contract_plan_embeds(
    ws: Path, case: str, env: Optional[str]
) -> None:
    contract_path = _write(ws / case, _CASES[case])
    planned = _plan(contract_path, ws / "plan.json", env)["contract"]

    loaded = api.load_contract(contract_path, env=env)

    assert loaded.contract == planned
    assert loaded.digest == compute_contract_digest(planned)


@pytest.mark.parametrize(
    "case, env",
    [
        ("aliases", None),
        ("legacy_build", None),
        ("composed", None),
        ("composed", "prod"),
    ],
)
def test_text_form_equals_the_path_form(ws: Path, case: str, env: Optional[str]) -> None:
    contract_path = _write(ws / case, _CASES[case])
    overlay_file = contract_path.parent / "overlays" / f"{env}.yaml"
    overlay = load_yaml_safe(overlay_file.read_text("utf-8")) if env else None

    from_text = api.load_contract_from_text(
        contract_path.read_text("utf-8"), base_dir=contract_path.parent, overlay=overlay
    )

    assert from_text.contract == api.load_contract(contract_path, env=env).contract
    assert from_text.digest == api.load_contract(contract_path, env=env).digest


def test_the_rewrites_plan_relies_on_are_applied(ws: Path) -> None:
    """The pin above is only meaningful if the fixtures exercise each rewrite."""
    aliases = api.load_contract(_write(ws / "a", _CASES["aliases"])).contract
    props = aliases["builds"][0]["properties"]
    assert props["source"] == {"kind": "postgres", "mode": "incremental_append"}
    assert aliases["exposes"][0]["binding"]["format"] == "iceberg"

    legacy = api.load_contract(_write(ws / "l", _CASES["legacy_build"])).contract
    assert "build" not in legacy and legacy["builds"][0]["id"] == "ingest"

    composed = api.load_contract(_write(ws / "c", _CASES["composed"]), env="prod").contract
    expose = composed["exposes"][0]
    assert composed["name"] == "Orders (prod)"
    assert expose["binding"]["format"] == "iceberg"  # alias inside the fragment
    assert expose["binding"]["location"] == {"database": "prod_s", "table": "o"}


def test_alias_under_a_legacy_build_matches_the_engine_loader(ws: Path) -> None:
    """The engine rewrites aliases BEFORE promoting ``build:`` to ``builds:``,
    so an alias under a legacy ``build:`` is not rewritten. This API mirrors
    the engine, order included, in both forms (plan then refuses the value at
    the schema gate, as it does on the CLI)."""
    text = _LEGACY_BUILD.replace("kind: postgres, mode: incremental_append", "kind: pg")
    contract_path = _write(ws, {"contract.fluid.yaml": text})
    engine = load_contract_with_overlay(str(contract_path), None, LOG)

    assert api.load_contract(contract_path).contract == engine
    assert api.load_contract_from_text(text).contract == engine
    assert engine["builds"][0]["properties"]["source"]["kind"] == "pg"


def test_a_bundle_loads_as_plan_loads_it(ws: Path) -> None:
    contract_path = _write(ws / "c", _CASES["composed"])
    tgz = ws / "bundle.tgz"
    argv = ["bundle", str(contract_path), "--format", "tgz", "--out", str(tgz), "--env", "prod"]
    assert bundle_cmd.run(_parse(bundle_cmd.register, argv), LOG) == 0
    planned = _plan(tgz, ws / "plan.json", "prod")["contract"]

    loaded = api.load_contract(tgz, env="prod")

    assert loaded.contract == planned
    assert loaded.origin == "bundle"
    assert loaded.source == tgz.resolve()
    assert loaded.files == (tgz.resolve(),)
    assert loaded.overlay is None
    assert loaded.contract["name"] == "Orders (prod)"


def test_a_bundle_refuses_an_env_it_was_not_built_for(ws: Path) -> None:
    contract_path = _write(ws / "c", _CASES["composed"])
    tgz = ws / "bundle.tgz"
    argv = ["bundle", str(contract_path), "--format", "tgz", "--out", str(tgz), "--env", "prod"]
    assert bundle_cmd.run(_parse(bundle_cmd.register, argv), LOG) == 0

    with pytest.raises(ContractLoadError) as err:
        api.load_contract(tgz, env="staging")
    assert err.value.event == "bundle_env_mismatch"


def test_overlay_provenance_matches_what_plan_applied(ws: Path, caplog: Any) -> None:
    """An overlay holding a ``$ref`` is dropped by the engine's auto-bundle step
    today (``fluid plan --env prod`` plans the base). Whatever the engine
    does, the contract equals plan's, and ``overlay`` / ``files`` name the
    overlay exactly when it reached the contract."""
    files = dict(_CASES["composed"])
    files["overlays/prod.yaml"] = 'name: Orders (prod)\ndescription: {"$ref": "./parts/d.yaml"}\n'
    files["parts/d.yaml"] = "text: prod\n"
    contract_path = _write(ws, files)
    planned = _plan(contract_path, ws / "plan.json", "prod")["contract"]

    with caplog.at_level(logging.WARNING, logger="fluid.api.contract"):
        loaded = api.load_contract(contract_path, env="prod")

    overlay_file = contract_path.resolve().parent / "overlays" / "prod.yaml"
    applied = loaded.contract["name"] == "Orders (prod)"
    assert loaded.contract == planned
    assert (loaded.overlay == overlay_file) is applied
    assert (overlay_file in loaded.files) is applied
    warned = any("contract_overlay_not_applied" in r.getMessage() for r in caplog.records)
    assert warned is not applied


# ── provenance ──────────────────────────────────────────────────────────


def test_provenance_names_every_file_composed(ws: Path) -> None:
    contract_path = _write(ws, _CASES["composed"])

    loaded = api.load_contract(contract_path, env="prod")

    root = contract_path.resolve()
    assert loaded.origin == "file"
    assert loaded.source == root
    assert loaded.env == "prod"
    assert loaded.overlay == root.parent / "overlays" / "prod.yaml"
    assert loaded.files == (
        root,
        root.parent / "parts" / "orders.yaml",
        root.parent / "overlays" / "prod.yaml",
    )
    assert loaded.unresolved_refs == ()


def test_provenance_without_env_or_refs(ws: Path) -> None:
    contract_path = _write(ws, _CASES["aliases"])

    loaded = api.load_contract(str(contract_path))

    assert loaded.overlay is None
    assert loaded.env is None
    assert loaded.files == (contract_path.resolve(),)


def test_nested_and_pointer_refs_are_listed_once_in_read_order(ws: Path) -> None:
    contract_path = _write(
        ws,
        {
            "contract.fluid.yaml": (
                "id: x\n"
                'metadata: {"$ref": "./meta.yaml#/inner"}\n'
                'exposes: [{"$ref": "./e.yaml"}, {"$ref": "./e.yaml"}]\n'
            ),
            "meta.yaml": 'unused: {"$ref": "./never.yaml"}\ninner: {"$ref": "./owner.yaml"}\n',
            "owner.yaml": "owner: {team: dp}\n",
            "e.yaml": "exposeId: e\n",
        },
    )

    loaded = api.load_contract(contract_path)

    root = contract_path.resolve().parent
    assert loaded.contract["metadata"] == {"owner": {"team": "dp"}}
    # never.yaml sits outside the pointer's subtree: the resolver never
    # reads it (it does not even exist), so it is not provenance.
    assert loaded.files == (
        root / "contract.fluid.yaml",
        root / "meta.yaml",
        root / "owner.yaml",
        root / "e.yaml",
    )


def test_a_same_document_pointer_is_reported_unresolved(ws: Path) -> None:
    contract_path = _write(
        ws,
        {"contract.fluid.yaml": 'id: x\ndefs: {n: 1}\ndescription: {"$ref": "#/defs/n"}\n'},
    )

    loaded = api.load_contract(contract_path)

    assert loaded.contract["description"] == {"$ref": "#/defs/n"}
    assert loaded.unresolved_refs == ("#/defs/n",)


# ── in-memory forms ─────────────────────────────────────────────────────


def test_dict_form_without_base_dir_reads_no_file(monkeypatch: pytest.MonkeyPatch) -> None:
    document = load_yaml_safe(_COMPOSED)
    # Warm every lazy import first: importing a module stats files.
    api.load_contract_from_dict(document)

    def _no_fs(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError(f"filesystem touched: {args!r}")

    try:
        for name in ("stat", "lstat", "open", "listdir", "scandir"):
            monkeypatch.setattr(os, name, _no_fs)
        monkeypatch.setattr(builtins, "open", _no_fs)
        loaded = api.load_contract_from_dict(document)
        from_text = api.load_contract_from_text(_COMPOSED)
    finally:
        # Restore before anything else runs: pytest itself stats files.
        monkeypatch.undo()
    assert loaded.contract["exposes"] == [{"$ref": "./parts/orders.yaml"}]
    assert loaded.unresolved_refs == ("./parts/orders.yaml",)
    assert loaded.origin == "memory"
    assert loaded.files == () and loaded.source is None
    assert from_text.contract == loaded.contract
    assert loaded.contract["builds"][0]["id"] == "ingest"  # rewrites still applied


def test_dict_form_with_base_dir_composes_refs(ws: Path) -> None:
    contract_path = _write(ws, _CASES["composed"])

    loaded = api.load_contract_from_dict(load_yaml_safe(_COMPOSED), base_dir=contract_path.parent)

    assert loaded.contract == api.load_contract(contract_path).contract
    assert loaded.files == (contract_path.resolve().parent / "parts" / "orders.yaml",)
    assert loaded.unresolved_refs == ()


def test_inputs_are_never_modified_and_results_are_independent() -> None:
    document = load_yaml_safe(_ALIASES)
    overlay = {"exposes": [{"binding": {"format": "kafka"}}]}
    before_doc, before_overlay = copy.deepcopy(document), copy.deepcopy(overlay)

    first = api.load_contract_from_dict(document, overlay=overlay)
    first.contract["exposes"][0]["binding"]["format"] = "mutated"
    second = api.load_contract_from_dict(document, overlay=overlay)

    assert document == before_doc and overlay == before_overlay
    assert second.contract["exposes"][0]["binding"]["format"] == "kafka_topic"


def test_json_text_is_supported() -> None:
    document = load_yaml_safe(_ALIASES)
    loaded = api.load_contract_from_text(json.dumps(document), suffix=".json")
    assert loaded.contract == api.load_contract_from_dict(document).contract


def test_loaded_contract_is_frozen() -> None:
    loaded = api.load_contract_from_text(_ALIASES)
    assert isinstance(loaded, LoadedContract)
    with pytest.raises(AttributeError):
        loaded.origin = "file"  # type: ignore[misc]


# ── typed failures ──────────────────────────────────────────────────────


def test_missing_file_is_contract_not_found(tmp_path: Path) -> None:
    with pytest.raises(ContractLoadError) as err:
        api.load_contract(tmp_path / "absent.fluid.yaml")
    assert err.value.event == "contract_not_found"
    assert err.value.path == (tmp_path / "absent.fluid.yaml").resolve()
    assert isinstance(err.value.__cause__, FileNotFoundError)


def test_unresolvable_ref_is_contract_ref_unresolved(ws: Path) -> None:
    contract_path = _write(
        ws, {"contract.fluid.yaml": 'id: x\nexposes: [{"$ref": "./gone.yaml"}]\n'}
    )
    with pytest.raises(ContractLoadError) as err:
        api.load_contract(contract_path)
    assert err.value.event == "contract_ref_unresolved"

    with pytest.raises(ContractLoadError) as err:
        api.load_contract_from_text(contract_path.read_text("utf-8"), base_dir=ws)
    assert err.value.event == "contract_ref_unresolved"


@pytest.mark.parametrize(
    "text, suffix, event",
    [
        ("id: [unclosed\n", ".yaml", "contract_parse_failed"),
        ("{not json", ".json", "contract_parse_failed"),
        ("- a\n- b\n", ".yaml", "contract_not_a_mapping"),
        ("[1, 2]", ".json", "contract_not_a_mapping"),
    ],
)
def test_bad_text_fails_typed(text: str, suffix: str, event: str) -> None:
    with pytest.raises(ContractLoadError) as err:
        api.load_contract_from_text(text, suffix=suffix)
    assert err.value.event == event


def test_bad_file_fails_typed(tmp_path: Path) -> None:
    broken = tmp_path / "broken.fluid.yaml"
    broken.write_text("id: [unclosed\n", encoding="utf-8")
    with pytest.raises(ContractLoadError) as err:
        api.load_contract(broken)
    assert err.value.event == "contract_parse_failed"

    array = tmp_path / "array.json"
    array.write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(ContractLoadError) as err:
        api.load_contract(array)
    assert err.value.event == "contract_not_a_mapping"


def test_non_mapping_inputs_fail_typed() -> None:
    with pytest.raises(ContractLoadError) as err:
        api.load_contract_from_dict(["not", "a", "mapping"])  # type: ignore[arg-type]
    assert err.value.event == "contract_not_a_mapping"
    with pytest.raises(ContractLoadError) as err:
        api.load_contract_from_dict({"id": "x"}, overlay=["nope"])  # type: ignore[arg-type]
    assert err.value.event == "contract_not_a_mapping"


# ── env is a name, never a path ─────────────────────────────────────────


@pytest.mark.parametrize("shape", ["absolute", "parent", "empty", "separator"])
def test_env_must_be_an_environment_name(ws: Path, shape: str) -> None:
    """The engine builds overlay paths from ``env`` (``<dir>/<env>.json`` among
    them), so an absolute or ``..`` env would merge a file from anywhere into
    the returned contract. The API refuses it before reading a file."""
    contract_path = _write(ws / "ws", _CASES["aliases"])
    secret = ws / "elsewhere" / "creds.json"
    secret.parent.mkdir()
    secret.write_text('{"auths": {"registry": {"auth": "c2VjcmV0"}}}', encoding="utf-8")
    env = {
        "absolute": str(secret.with_suffix("")),
        "parent": "../elsewhere/creds",
        "empty": "",
        "separator": "prod/eu",
    }[shape]

    with pytest.raises(ContractLoadError) as err:
        api.load_contract(contract_path, env=env)

    assert err.value.event == "contract_env_invalid"
    assert err.value.path == contract_path.resolve()


def test_a_valid_env_name_still_selects_its_overlay(ws: Path) -> None:
    contract_path = _write(ws, _CASES["composed"])
    for env in ("prod", "Prod.eu-1_a"):
        (contract_path.parent / "overlays" / f"{env}.yaml").write_text(_PROD_OVERLAY, "utf-8")
        assert api.load_contract(contract_path, env=env).contract["name"] == "Orders (prod)"


# ── the in-memory overlay follows the engine's auto-bundle step ─────────

# Two shapes in which ``fluid plan --env prod`` drops the overlay, because a
# ``$ref`` survives the merge and the auto-bundle step reloads the base file.
_OVERLAY_DROPPED: Dict[str, Dict[str, str]] = {
    # A same-document pointer in the base contract (in an open block, so the
    # contract still plans).
    "same_document_pointer": {
        "contract.fluid.yaml": _ALIASES + 'extensions: {n: 1, copy: {"$ref": "#/extensions/n"}}\n',
        "overlays/prod.yaml": "name: Orders (prod)\n",
    },
    # A ``$ref`` inside the overlay itself.
    "ref_in_overlay": {
        **_CASES["composed"],
        "overlays/prod.yaml": 'name: Orders (prod)\ndescription: {"$ref": "./parts/d.yaml"}\n',
        "parts/d.yaml": "text: prod\n",
    },
}


@pytest.mark.parametrize("shape", sorted(_OVERLAY_DROPPED))
def test_in_memory_overlay_matches_the_file_form_when_the_engine_drops_it(
    ws: Path, shape: str, caplog: Any
) -> None:
    contract_path = _write(ws, _OVERLAY_DROPPED[shape])
    planned = _plan(contract_path, ws / "plan.json", "prod")["contract"]
    overlay = load_yaml_safe((contract_path.parent / "overlays" / "prod.yaml").read_text("utf-8"))
    from_file = api.load_contract(contract_path, env="prod")

    with caplog.at_level(logging.WARNING, logger="fluid.api.contract"):
        caplog.clear()
        from_text = api.load_contract_from_text(
            contract_path.read_text("utf-8"), base_dir=contract_path.parent, overlay=overlay
        )

    assert from_file.contract == planned
    assert from_text.contract == from_file.contract
    assert from_text.digest == from_file.digest
    dropped = from_file.contract["name"] == "Orders"
    warned = any("contract_overlay_not_applied" in r.getMessage() for r in caplog.records)
    assert warned is dropped


def test_in_memory_overlay_without_base_dir_follows_a_same_document_pointer(ws: Path) -> None:
    """No file ``$ref``: the engine's decision is knowable without a directory."""
    contract_path = _write(ws, _OVERLAY_DROPPED["same_document_pointer"])
    overlay = load_yaml_safe((contract_path.parent / "overlays" / "prod.yaml").read_text("utf-8"))

    from_text = api.load_contract_from_text(contract_path.read_text("utf-8"), overlay=overlay)

    assert from_text.contract == api.load_contract(contract_path, env="prod").contract


def test_in_memory_overlay_without_base_dir_refuses_an_undecidable_document() -> None:
    """With file ``$ref`` values unresolved, whether the engine applies the overlay
    depends on what the fragments hold; the API says so instead of guessing."""
    with pytest.raises(ContractLoadError) as err:
        api.load_contract_from_text(_COMPOSED, overlay=load_yaml_safe(_PROD_OVERLAY))
    assert err.value.event == "contract_overlay_needs_base_dir"


def test_in_memory_overlay_logs_to_the_callers_logger() -> None:
    records: List[logging.LogRecord] = []

    class _Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    mine = logging.getLogger("test.api.contract_load.caller")
    mine.addHandler(_Collect())
    document = {"id": "x", "name": "base", "defs": {"n": 1}, "d": {"$ref": "#/defs/n"}}

    loaded = api.load_contract_from_dict(document, overlay={"name": "prod"}, logger=mine)

    assert loaded.contract["name"] == "base"
    assert [getattr(r, "event", None) for r in records] == ["contract_overlay_not_applied"]


# ── the in-memory replay cannot fall behind the engine loader ───────────


def _engine_post_load_steps() -> List[str]:
    """Calls ``load_contract_with_overlay`` makes on ``contract``, in source order,
    outside its bundle branch (the in-memory forms never load a bundle)."""
    tree = ast.parse(inspect.getsource(_contract_loader.load_contract_with_overlay))
    function = tree.body[0]
    assert isinstance(function, ast.FunctionDef)
    steps: List[str] = []

    class _Calls(ast.NodeVisitor):
        def visit_If(self, node: ast.If) -> None:
            test = node.test
            if isinstance(test, ast.Call) and getattr(test.func, "id", "") == "_is_bundle_path":
                return
            self.generic_visit(node)

        def visit_Call(self, node: ast.Call) -> None:
            self.generic_visit(node)  # inner calls first: they run first
            if any(isinstance(a, ast.Name) and a.id == "contract" for a in node.args):
                func = node.func
                steps.append(func.attr if isinstance(func, ast.Attribute) else func.id)

    _Calls().visit(function)
    return steps


def test_in_memory_forms_replay_every_engine_loader_step() -> None:
    """The file form calls the engine loader; the in-memory forms replay its steps
    (``_replay_overlay`` for the auto-bundle decision, then
    ``_ENGINE_REWRITES`` by name). A step added to, removed from or moved in
    the engine fails here until the in-memory forms follow it."""
    assert _engine_post_load_steps() == [
        "_auto_bundle_if_needed",
        *contract_api._ENGINE_REWRITES,
    ]
    for name in contract_api._ENGINE_REWRITES:
        assert callable(getattr(_contract_loader, name))


# ── typed failures, file form ───────────────────────────────────────────


def test_a_yaml_list_root_file_is_contract_not_a_mapping(tmp_path: Path) -> None:
    """The same input gives the same event through the file and text forms."""
    listed = tmp_path / "list.fluid.yaml"
    listed.write_text("- a\n- b\n", encoding="utf-8")

    with pytest.raises(ContractLoadError) as err:
        api.load_contract(listed)

    assert err.value.event == "contract_not_a_mapping"
    with pytest.raises(ContractLoadError) as text_err:
        api.load_contract_from_text(listed.read_text("utf-8"))
    assert text_err.value.event == err.value.event


def test_a_list_root_overlay_is_contract_not_a_mapping(ws: Path) -> None:
    contract_path = _write(ws, {**_CASES["aliases"], "overlays/prod.yaml": "- a\n"})
    with pytest.raises(ContractLoadError) as err:
        api.load_contract(contract_path, env="prod")
    assert err.value.event == "contract_not_a_mapping"


def test_a_file_that_is_not_utf8_is_contract_parse_failed(tmp_path: Path) -> None:
    """``UnicodeDecodeError`` is a ``ValueError``; it is still a parse failure."""
    binary = tmp_path / "binary.fluid.yaml"
    binary.write_bytes(b"id: \xff\xfe\n")
    with pytest.raises(ContractLoadError) as err:
        api.load_contract(binary)
    assert err.value.event == "contract_parse_failed"


def test_a_missing_bundle_is_the_engines_bundle_not_found(tmp_path: Path) -> None:
    with pytest.raises(ContractLoadError) as err:
        api.load_contract(tmp_path / "absent.tgz")
    assert err.value.event == "bundle_not_found"


# ── keys and the digest ─────────────────────────────────────────────────


def test_a_magic_word_key_equals_plan_once_keys_are_strings(ws: Path) -> None:
    """``plan.json`` writes keys as strings; ``contract`` keeps the engine's
    ``bool`` key. The documented equation holds after that coercion, and the
    digest agrees without it."""
    contract_path = _write(ws, {"contract.fluid.yaml": _ALIASES + "extensions: {on: x}\n"})
    planned = _plan(contract_path, ws / "plan.json")["contract"]

    loaded = api.load_contract(contract_path)

    assert loaded.contract["extensions"] == {True: "x"}
    assert coerce_keys_to_str(loaded.contract) == planned
    assert loaded.digest == compute_contract_digest(planned)
    assert api.load_contract_from_text(_ALIASES + "extensions: {on: x}\n").digest == loaded.digest


def test_digest_of_an_unserialisable_contract_fails_typed(ws: Path) -> None:
    contract_path = _write(
        ws, {"contract.fluid.yaml": _ALIASES + "extensions: {created: 2024-01-01}\n"}
    )
    loaded = api.load_contract(contract_path)  # loading is fine; plan is not

    with pytest.raises(ContractLoadError) as err:
        _ = loaded.digest

    assert err.value.event == "contract_not_serialisable"
    assert err.value.path == contract_path.resolve()
    assert isinstance(err.value.__cause__, TypeError)


def test_digest_is_the_planned_contracts_not_the_raw_files(ws: Path) -> None:
    """Documented: ``.digest`` hashes the normalised contract, so it differs
    from ``fluid contract digest`` (the raw parse) whenever a rewrite applies."""
    contract_path = _write(ws, _CASES["aliases"])
    raw = compute_contract_digest(load_yaml_safe(contract_path.read_text("utf-8")))
    assert api.load_contract(contract_path).digest != raw
