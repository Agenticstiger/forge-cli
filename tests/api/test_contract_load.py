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
from typing import Any, Callable, Dict, List, Optional, Tuple

import pytest

from fluid_build import _contract_loader, api
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


@pytest.mark.parametrize(
    "shape",
    ["absolute", "parent", "empty", "separator", "backslash", "nul", "dot", "dotdot", "drive"],
)
def test_env_must_be_a_single_path_component(ws: Path, shape: str) -> None:
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
        "backslash": "..\\elsewhere\\creds",
        "nul": "prod\x00",
        "dot": ".",
        "dotdot": "..",
        # A plain file name on POSIX, a drive-relative path on Windows: refused
        # on every platform, so one env never names two different files.
        "drive": "C:prod",
    }[shape]

    with pytest.raises(ContractLoadError) as err:
        api.load_contract(contract_path, env=env)

    assert err.value.event == "contract_env_invalid"
    assert err.value.path == contract_path.resolve()


# Names ``fluid plan --env`` loads although ``fluid publish --env`` refuses
# them: the API refuses only what makes an env a path, so it loads them too.
_ENVS_PLAN_LOADS = ("prod", "Prod.eu-1_a", "_staging", "prod+eu", "eu prod", "e" * 65)


@pytest.mark.parametrize("env", _ENVS_PLAN_LOADS)
def test_every_env_plan_loads_selects_its_overlay(ws: Path, env: str) -> None:
    contract_path = _write(ws, _CASES["composed"])
    (contract_path.parent / "overlays" / f"{env}.yaml").write_text(_PROD_OVERLAY, "utf-8")

    loaded = api.load_contract(contract_path, env=env)

    assert loaded.contract == load_contract_with_overlay(str(contract_path), env, LOG)
    assert loaded.contract["name"] == "Orders (prod)"
    assert loaded.overlay == (contract_path.parent / "overlays" / f"{env}.yaml").resolve()


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


# ── YAML aliases: shared in the parse, separate in the engine ───────────

# The engine's ``$ref`` resolver rebuilds every dict and list of the base
# contract, so a node a YAML alias shares is two objects by the time the
# overlay merge and the rewrites change nodes in place. ``deepcopy`` keeps
# the sharing; without ``base_dir`` the in-memory forms must break it too.
_ALIASED: Dict[str, Dict[str, str]] = {
    # The overlay patches one alias of a shared node.
    "overlay_merge": {
        "contract.fluid.yaml": _ALIASES + "extensions: {a: &x {k: 1}, b: *x}\n",
        "overlays/prod.yaml": "extensions: {a: {k: 2}}\n",
    },
    # The alias rewrite changes ``exposes[0].binding``; its alias in an open
    # block is not a binding, and the engine leaves it as written.
    "alias_rewrite": {
        "contract.fluid.yaml": _ALIASES.replace("    binding:\n", "    binding: &b\n")
        + "extensions: {copy: *b}\n",
        "overlays/prod.yaml": "name: Orders (prod)\n",
    },
}


@pytest.mark.parametrize("shape", sorted(_ALIASED))
def test_in_memory_form_without_base_dir_unshares_yaml_aliases(ws: Path, shape: str) -> None:
    contract_path = _write(ws, _ALIASED[shape])
    text = contract_path.read_text("utf-8")
    overlay = load_yaml_safe((contract_path.parent / "overlays" / "prod.yaml").read_text("utf-8"))
    planned = _plan(contract_path, ws / "plan.json", "prod")["contract"]
    from_file = api.load_contract(contract_path, env="prod")

    from_text = api.load_contract_from_text(text, overlay=overlay)

    assert from_file.contract == planned
    assert from_text.contract == from_file.contract
    assert from_text.digest == from_file.digest
    if shape == "overlay_merge":
        assert from_text.contract["extensions"] == {"a": {"k": 2}, "b": {"k": 1}}
    else:
        assert from_text.contract["exposes"][0]["binding"]["format"] == "iceberg"
        assert from_text.contract["extensions"]["copy"]["format"] == "iceberg_table"
    # No overlay at all: the rewrite alone must not reach the alias either.
    assert api.load_contract_from_text(text).digest == api.load_contract(contract_path).digest


def test_aliases_inside_an_overlay_stay_shared_as_in_the_engine(ws: Path) -> None:
    """The engine parses an overlay file without the resolver, so sharing *inside*
    the overlay survives the merge, and a rewrite reaches every alias of the
    node it changes. The in-memory forms keep it the same way. (The base has
    no ``exposes``, so the merge places the overlay's own node.)"""
    contract_path = _write(
        ws,
        {
            "contract.fluid.yaml": "id: x\nname: Orders\n",
            "overlays/prod.yaml": (
                "exposes:\n  - exposeId: e\n    binding: &b {platform: aws, format: kafka}\n"
                "extensions: {copy: *b}\n"
            ),
        },
    )
    overlay = load_yaml_safe((contract_path.parent / "overlays" / "prod.yaml").read_text("utf-8"))
    from_file = api.load_contract(contract_path, env="prod")

    from_text = api.load_contract_from_text(contract_path.read_text("utf-8"), overlay=overlay)

    assert from_file.contract["extensions"]["copy"]["format"] == "kafka_topic"
    assert from_text.contract == from_file.contract
    assert from_text.digest == from_file.digest


def test_a_document_that_contains_itself_fails_as_the_engine_fails(ws: Path) -> None:
    contract_path = _write(ws, {"contract.fluid.yaml": _ALIASES + "extensions: &e {self: *e}\n"})
    with pytest.raises(ContractLoadError) as from_file:
        api.load_contract(contract_path)

    with pytest.raises(ContractLoadError) as from_text:
        api.load_contract_from_text(contract_path.read_text("utf-8"))

    assert from_file.value.event == "contract_load_failed"
    assert from_text.value.event == from_file.value.event


def test_unsharing_is_iterative_and_keeps_order() -> None:
    deep: Dict[str, Any] = {}
    node = deep
    for _ in range(5000):  # far past the interpreter's recursion limit
        node["n"] = {}
        node = node["n"]
    shared = {"z": 1, "a": [1, {"k": "v"}]}
    document = {"id": "x", "deep": deep, "p": shared, "q": shared, "r": [shared, shared]}

    copied = contract_api._unshare(document)

    depth, node, original = 0, copied["deep"], deep
    while node:  # walked, not compared: ``==`` itself recurses
        assert node is not original and list(node) == ["n"]
        depth, node, original = depth + 1, node["n"], original["n"]
    assert depth == 5000
    assert {k: v for k, v in copied.items() if k != "deep"} == {
        k: v for k, v in document.items() if k != "deep"
    }
    assert list(copied) == list(document) and list(copied["p"]) == ["z", "a"]
    containers = [copied["p"], copied["q"], copied["r"][0], copied["r"][1]]
    assert len({id(c) for c in containers}) == 4
    assert len({id(c["a"]) for c in containers}) == 4


# ── the in-memory replay cannot fall behind the engine loader ───────────


_ENGINE_SOURCE = inspect.getsource(_contract_loader.load_contract_with_overlay)


def _is_contract(node: ast.AST) -> bool:
    return isinstance(node, ast.Name) and node.id == "contract"


def _rooted_at_contract(node: ast.AST) -> bool:
    while isinstance(node, (ast.Subscript, ast.Attribute)):
        node = node.value
    return _is_contract(node)


def _callee(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    return func.id if isinstance(func, ast.Name) else ast.dump(func)


def _engine_post_load_effects(source: str = _ENGINE_SOURCE) -> Tuple[List[str], List[str]]:
    """What ``load_contract_with_overlay`` does to ``contract``, in source order,
    outside its bundle branch (the in-memory forms never load a bundle).

    Returns ``(steps, writes)``. ``steps``: every call handed ``contract`` or a
    part of it (``contract["builds"]``), positionally or by keyword.
    ``writes``: every statement that changes ``contract``, as the callee whose
    result is assigned to it, or as ``<...>`` for any other change (an
    assignment from a non-call, an item or attribute write, an augmented
    assignment, ``del``, a method call on it, a rebinding as a loop or tuple
    target), any return of something other than ``contract`` itself, and any
    other read of ``contract``: an alias, a part of it passed on, a loop over
    it, a container or expression holding it. A read is accounted for only as
    a whole-``contract`` argument of a call (listed in ``steps``) or as the
    returned value; anything else could change the contract unseen, so it is
    reported, and the guard fails closed.
    """
    tree = ast.parse(source)
    function = tree.body[0]
    assert isinstance(function, ast.FunctionDef)
    parents = {
        child: parent for parent in ast.walk(function) for child in ast.iter_child_nodes(parent)
    }
    steps: List[str] = []
    writes: List[str] = []

    def _argument(node: ast.AST) -> ast.AST:
        return node.value if isinstance(node, ast.Starred) else node

    def _record_write(target: ast.AST, value: Optional[ast.AST]) -> None:
        if _is_contract(target):
            writes.append(_callee(value) if isinstance(value, ast.Call) else "<assign>")
        elif _rooted_at_contract(target):
            writes.append("<item or attribute write>")

    class _Effects(ast.NodeVisitor):
        def visit_If(self, node: ast.If) -> None:
            test = node.test
            if isinstance(test, ast.Call) and getattr(test.func, "id", "") == "_is_bundle_path":
                return
            self.generic_visit(node)

        def visit_Call(self, node: ast.Call) -> None:
            self.generic_visit(node)  # inner calls first: they run first
            func = node.func
            if isinstance(func, ast.Attribute) and _rooted_at_contract(func.value):
                writes.append(f"<method call {func.attr}>")
            if any(_rooted_at_contract(_argument(a)) for a in node.args) or any(
                _rooted_at_contract(k.value) for k in node.keywords
            ):
                steps.append(_callee(node))

        def visit_Assign(self, node: ast.Assign) -> None:
            self.generic_visit(node)
            for target in node.targets:
                _record_write(target, node.value)

        def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
            self.generic_visit(node)
            _record_write(node.target, node.value)

        def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
            self.generic_visit(node)
            _record_write(node.target, node.value)

        def visit_AugAssign(self, node: ast.AugAssign) -> None:
            self.generic_visit(node)
            if _rooted_at_contract(node.target):
                writes.append("<augmented assignment>")

        def visit_Delete(self, node: ast.Delete) -> None:
            self.generic_visit(node)
            if any(_rooted_at_contract(t) for t in node.targets):
                writes.append("<del>")

        def visit_Return(self, node: ast.Return) -> None:
            self.generic_visit(node)
            if node.value is None or not _is_contract(node.value):
                writes.append("<return of something other than contract>")

        def visit_Name(self, node: ast.Name) -> None:
            if node.id != "contract":
                return
            parent = parents[node]
            if isinstance(node.ctx, ast.Store):
                # A plain or annotated assignment, a walrus or an augmented
                # assignment is recorded above; any other binding is not.
                if not isinstance(
                    parent, (ast.Assign, ast.AnnAssign, ast.NamedExpr, ast.AugAssign)
                ):
                    writes.append("<rebinding>")
                return
            if isinstance(node.ctx, ast.Del):
                return  # recorded by ``visit_Delete``
            whole_argument = (isinstance(parent, ast.Call) and node in parent.args) or isinstance(
                parent, ast.keyword
            )
            if whole_argument or isinstance(parent, ast.Return):
                return
            writes.append("<other read of contract>")

    _Effects().visit(function)
    return steps, writes


# The engine loads (``load_with_overlay``, or ``load_contract`` for a loader
# without it), then runs the auto-bundle step and the rewrites.
_EXPECTED_STEPS = ["_auto_bundle_if_needed", *contract_api._ENGINE_REWRITES]
_EXPECTED_WRITES = ["load_with_overlay", "load_contract", *_EXPECTED_STEPS]


def test_in_memory_forms_replay_every_engine_loader_step() -> None:
    """The file form calls the engine loader; the in-memory forms replay its steps
    (``_replay_overlay`` for the auto-bundle decision, then
    ``_ENGINE_REWRITES`` by name). A step added to, removed from or moved in
    the engine fails here until the in-memory forms follow it."""
    assert _engine_post_load_effects() == (_EXPECTED_STEPS, _EXPECTED_WRITES)
    for name in contract_api._ENGINE_REWRITES:
        assert callable(getattr(_contract_loader, name))


@pytest.mark.parametrize(
    "added",
    [
        "contract = _normalize_new(contract)",
        "_normalize_builds_in_place(contract['builds'])",
        "_normalize_x(builds=contract['builds'])",
        "_normalize_x(*contract['builds'])",
        "c = contract\n    c['x'] = 1",
        "c = contract['builds']\n    c.append({})",
        "for b in contract['builds']:\n        b['x'] = 1",
        "[b.update(x=1) for b in contract['builds']]",
        "_mutate_all([contract])",
        "for contract in [{}]:\n        pass",
        "contract, _ = {}, None",
        "if contract.get('x'):\n        pass",
        "contract = resolve_contract_env_templates(value=contract)",
        "_mutate_in_place(contract=contract)",
        "contract = {**contract, 'x': 1}",
        "contract = dict(contract, x=1)",
        "contract['x'] = 1",
        "contract['a']['b'] = 1",
        "contract.update(x=1)",
        "contract['a'].setdefault('b', 1)",
        "contract |= {'x': 1}",
        "del contract['x']",
        "_ = (contract := {})",
        "contract: dict = {}",
    ],
)
def test_the_step_guard_sees_every_way_the_engine_can_change_the_contract(added: str) -> None:
    """Negative control for the guard above: the engine's real source, with one
    more change to ``contract`` before it returns, no longer matches."""
    head, sep, tail = _ENGINE_SOURCE.rpartition("    return contract\n")
    assert sep, "the engine loader no longer ends with `return contract`"
    mutated = f"{head}    {added}\n{sep}{tail}"

    assert _engine_post_load_effects(mutated) != (_EXPECTED_STEPS, _EXPECTED_WRITES)


@pytest.mark.parametrize(
    "returned",
    ["{**contract, 'x': 1}", "dict(contract, x=1)", "_normalize_new(contract)", "None", ""],
)
def test_the_step_guard_sees_a_changed_return(returned: str) -> None:
    """Negative control: the engine's real source returning anything but
    ``contract`` itself no longer matches."""
    head, sep, tail = _ENGINE_SOURCE.rpartition("    return contract\n")
    assert sep, "the engine loader no longer ends with `return contract`"
    mutated = f"{head}    return {returned}".rstrip() + f"\n{tail}"

    assert _engine_post_load_effects(mutated) != (_EXPECTED_STEPS, _EXPECTED_WRITES)


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


@pytest.mark.parametrize(
    "root", ['[{"a": 1}]', '[{"a": 1, "b": 2}]', '[["k", "v"]]', "[]", "1", "null", "ref"]
)
def test_a_json_root_that_is_not_an_object_is_contract_not_a_mapping_with_or_without_an_overlay(
    tmp_path: Path, root: str
) -> None:
    """The loader checks a YAML root but not a JSON one (nor one a root ``$ref``
    composes), and merges an overlay into ``dict(base)``: a list root then
    loads as a dict, or fails with a message that names no root. One file,
    one event, whether or not an overlay exists for the env."""
    contract_path = tmp_path / "c.json"
    if root == "ref":
        (tmp_path / "frag.json").write_text('[{"a": 1}]', encoding="utf-8")
        root = '{"$ref": "./frag.json"}'
    contract_path.write_text(root, encoding="utf-8")
    (tmp_path / "overlays").mkdir()
    (tmp_path / "overlays" / "prod.yaml").write_text("x: 1\n", encoding="utf-8")

    for env in (None, "prod", "staging"):  # no env, an overlay, no overlay
        with pytest.raises(ContractLoadError) as err:
            api.load_contract(contract_path, env=env)
        assert (err.value.event, err.value.path) == (
            "contract_not_a_mapping",
            contract_path.resolve(),
        ), env


def test_with_an_env_a_base_that_fails_to_load_keeps_its_own_event(tmp_path: Path) -> None:
    """The root check before an overlay merge reads the base first; a base that
    cannot be read still fails with the event the engine's load gives it."""
    (tmp_path / "overlays").mkdir()
    (tmp_path / "overlays" / "prod.yaml").write_text("x: 1\n", encoding="utf-8")
    broken = tmp_path / "broken.json"
    broken.write_text("[1,", encoding="utf-8")
    dangling = tmp_path / "dangling.json"
    dangling.write_text('{"id": "x", "m": {"$ref": "./absent.yaml"}}', encoding="utf-8")

    events = []
    for contract_path in (tmp_path / "absent.json", broken, dangling):
        with pytest.raises(ContractLoadError) as err:
            api.load_contract(contract_path, env="prod")
        events.append(err.value.event)

    assert events == ["contract_not_found", "contract_parse_failed", "contract_ref_unresolved"]


def test_a_plain_value_error_that_is_not_a_root_check_is_contract_load_failed(
    tmp_path: Path,
) -> None:
    """``Path.resolve`` raises a plain ``ValueError`` for a ``$ref`` holding a NUL
    byte. Only the loader's root checks mean ``contract_not_a_mapping``."""
    contract_path = tmp_path / "contract.fluid.yaml"
    contract_path.write_text('id: x\nname: base\nmeta: {"$ref": "./a\\0b.yaml"}\n', "utf-8")

    with pytest.raises(ContractLoadError) as from_file:
        api.load_contract(contract_path)
    with pytest.raises(ContractLoadError) as from_dict:
        api.load_contract_from_dict(
            {"id": "x", "meta": {"$ref": "./a\x00b.yaml"}}, base_dir=tmp_path
        )

    assert from_file.value.event == "contract_load_failed"
    assert from_dict.value.event == "contract_load_failed"
    assert isinstance(from_file.value.__cause__, ValueError)


def test_a_path_no_file_can_have_is_contract_not_found(tmp_path: Path) -> None:
    with pytest.raises(ContractLoadError) as from_path:
        api.load_contract(f"{tmp_path}/c\x00.fluid.yaml")
    with pytest.raises(ContractLoadError) as from_base_dir:
        api.load_contract_from_dict({"id": "x"}, base_dir=f"{tmp_path}/d\x00")

    for err in (from_path, from_base_dir):
        assert err.value.event == "contract_not_found"
        assert err.value.path is None
        assert isinstance(err.value.__cause__, ValueError)


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
