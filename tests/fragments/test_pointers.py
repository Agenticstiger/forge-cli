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

"""Same-document ``$ref`` pointers (``#/a/b``): scenario catalog area C, and B-14.

Test names carry the id of the scenario in the fragments scenario catalog (C-01 ...
C-34, B-14); ``SG-nn`` is its register of questions the spec leaves open.

The spec (catalog section 1, item 4): a ``#/...`` pointer resolves *last*, against
the composed contract (every file inlined, the environment overlay merged),
whatever file it is written in. Pointers may go through other refs; a cycle is an
error; RFC 6901 escapes decode; ``#foo`` is not a pointer. A pointer is a copy of the
place it names, so wherever it is merged it must behave as that value written in place.

C-24 (``fluid split`` keeps pointers meaning the same) is covered with the split tests.
A scenario that cites an open question (SG-01 to SG-06) has no oracle: its test pins
what the code does today, so a deliberate change shows up as a failing test.
"""

from __future__ import annotations

import json
import logging
import time
import tracemalloc
from pathlib import Path
from typing import Any, Callable, Dict, Optional

import pytest
import yaml

from fluid_build import contract_refs, loader
from fluid_build.loader import RefResolutionError, load_contract, load_with_overlay

# ── helpers ─────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _fresh_loader_state(monkeypatch: pytest.MonkeyPatch):
    """Warn-once registries are process-wide; the ref-root variables are the caller's."""

    def clear() -> None:
        for name in dir(loader):
            value = getattr(loader, name)
            if name.startswith("_NOTED_") and isinstance(value, set):
                value.clear()

    clear()
    monkeypatch.delenv("FLUID_REF_ROOT", raising=False)
    monkeypatch.delenv("FLUID_IGNORE_WORKSPACE_REF_ROOT", raising=False)
    yield
    clear()


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _dump(doc: Any) -> str:
    return yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)


def _contract(tmp_path: Path, text: str, name: str = "contract.fluid.yaml") -> Path:
    return _write(tmp_path / name, text)


def _lc(tmp_path: Path, text: str) -> Dict[str, Any]:
    return load_contract(_contract(tmp_path, text))


def _squash(text: str) -> str:
    """Console output wraps long lines anywhere: compare without whitespace."""
    return "".join(text.split())


def _no_ref_key(node: Any) -> bool:
    """NOREF: no mapping *key* ``$ref`` anywhere (text may mention it)."""
    stack = [node]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            if "$ref" in item:
                return False
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return True


def _ref_error(
    call: Callable[[], Any],
    *,
    ref: Optional[str] = None,
    file: Optional[Path] = None,
    pointer: Optional[str] = None,
    has: tuple = (),
) -> str:
    """Run ``call``, expect a ``RefResolutionError``, check what its message names."""
    with pytest.raises(RefResolutionError) as info:
        call()
    message = str(info.value)
    if ref is not None:
        assert ref in message
    if file is not None:
        assert str(file.resolve()) in message
    if pointer is not None:
        assert f"'{pointer}'" in message
    for part in has:
        assert part in message
    return message


_MIN = """\
fluidVersion: "0.7.6"
kind: DataProduct
id: refs.pointers
name: Pointers
description:
  purpose: Pointers.
  usage: Use it.
domain: ops
metadata:
  layer: Silver
  owner: {team: platform-ops, email: ops@example.com}
builds:
  - id: transform
    pattern: embedded-logic
    engine: sql
    properties:
      sql: "SELECT 1 AS id"
exposes:
  - exposeId: out
    kind: table
    version: "1.0.0"
    binding:
      platform: local
      format: csv
      location: {path: runtime/out/out.csv}
    contract:
      schema:
        - {name: id, type: integer}
"""


def _min() -> Dict[str, Any]:
    return yaml.safe_load(_MIN)


def _cli(*argv: str) -> int:
    from fluid_build.cli import main

    return main(list(argv))


def _every_document(root: Path) -> Dict[Path, Any]:
    """Each YAML or JSON file under ``root``, parsed."""
    found: Dict[Path, Any] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix in (".yaml", ".yml", ".json"):
            found[path] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return found


# ── C-01..C-05: what a pointer names, and when ──────────────────────────


def test_c01_pointer_into_build_parameters_across_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Draft 23 case c: a Flink build copies its shared configuration by pointer."""
    monkeypatch.chdir(tmp_path)
    shared = {"taskmanager.numberOfTaskSlots": "2", "execution.checkpointing.interval": "60s"}
    doc = _min()
    doc["builds"][0]["properties"]["parameters"] = {
        "sharedFlinkConfiguration": shared,
        "flinkConfiguration": {"$ref": "#/builds/0/properties/parameters/sharedFlinkConfiguration"},
    }
    path = _contract(tmp_path, _dump(doc))
    _write(tmp_path / "overlays" / "prod.yaml", "extensions: {tier: prod}\n")

    assert _cli("validate", str(path)) == 0

    bundle = tmp_path / "bundle.yaml"
    assert _cli("bundle", str(path), "--out", str(bundle)) == 0
    bundled = yaml.safe_load(bundle.read_text(encoding="utf-8"))
    assert bundled["builds"][0]["properties"]["parameters"]["flinkConfiguration"] == shared
    assert _no_ref_key(bundled)

    plan_file = tmp_path / "plan.json"
    assert _cli("plan", str(path), "--env", "prod", "--out", str(plan_file)) == 0
    plan = json.loads(plan_file.read_text(encoding="utf-8"))
    planned = plan["contract"]["builds"][0]["properties"]["parameters"]["flinkConfiguration"]
    assert planned == shared
    assert _no_ref_key(plan), "plan.json (contract and every action's params) holds a $ref key"

    artifacts = tmp_path / "artifacts"
    assert _cli("generate", "artifacts", str(path), "--env", "prod", "--out", str(artifacts)) == 0
    written = _every_document(artifacts)
    assert written, "generate artifacts wrote no YAML or JSON document"
    for file, document in written.items():
        assert _no_ref_key(document), f"{file.name} holds a $ref key"


def test_c02_pointer_written_in_a_fragment_names_the_root_contract(tmp_path: Path) -> None:
    _write(
        tmp_path / "fragments" / "exposes" / "o.yaml",
        'exposeId: o\ndescription: {$ref: "#/description/purpose"}\n',
    )
    contract = _lc(
        tmp_path,
        "description: {purpose: ROOT-PURPOSE}\nexposes:\n  - {$ref: ./fragments/exposes/o.yaml}\n",
    )

    assert contract["exposes"] == [{"exposeId": "o", "description": "ROOT-PURPOSE"}]


def test_c02_pointer_in_a_fragment_never_resolves_inside_that_fragment(tmp_path: Path) -> None:
    """Deliberately not JSON Reference semantics: the fragment's own /description is ignored."""
    fragment = _write(
        tmp_path / "fragments" / "exposes" / "o.yaml",
        'exposeId: o\ndescription: {purpose: LOCAL}\ncopy: {$ref: "#/description/purpose"}\n',
    )
    path = _contract(tmp_path, "id: x\nexposes:\n  - {$ref: ./fragments/exposes/o.yaml}\n")

    _ref_error(
        lambda: load_contract(path),
        ref="#/description/purpose",
        file=fragment,
        pointer="/copy",
    )


def _c03_contract(tmp_path: Path) -> Path:
    doc = _min()
    doc["exposes"][0]["binding"]["location"]["path"] = {"$ref": "#/extensions/paths/out"}
    doc["extensions"] = {"paths": {"out": "runtime/out/dev.csv"}}
    _write(
        tmp_path / "overlays" / "prod.yaml", "extensions: {paths: {out: runtime/out/prod.csv}}\n"
    )
    return _contract(tmp_path, _dump(doc))


def _path_of(document: Dict[str, Any]) -> str:
    return document["exposes"][0]["binding"]["location"]["path"]


def _entry_points() -> Dict[str, Callable[[Path, Optional[str]], Dict[str, Any]]]:
    from fluid_build._contract_loader import load_contract_with_overlay
    from fluid_build.api.contract import load_contract as api_load_contract
    from fluid_build.cli.bundle import materialize_contract

    log = logging.getLogger("test.c03")
    return {
        "loader.load_with_overlay": lambda p, env: load_with_overlay(p, env),
        "cli loader": lambda p, env: load_contract_with_overlay(str(p), env, log),
        "bundle.materialize_contract": lambda p, env: materialize_contract(p, env, log)[0],
        "api.load_contract": lambda p, env: api_load_contract(p, env=env).contract,
    }


def test_c03_pointer_follows_the_overlay_in_every_entry_point(tmp_path: Path) -> None:
    """Pointers run after the overlay merge, so prod sees prod and no env sees the base."""
    path = _c03_contract(tmp_path)

    for name, load in _entry_points().items():
        assert _path_of(load(path, "prod")) == "runtime/out/prod.csv", name
        assert _path_of(load(path, None)) == "runtime/out/dev.csv", name


def test_c03_pointer_follows_the_overlay_in_validate_and_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    path = _c03_contract(tmp_path)

    assert _cli("validate", str(path), "--env", "prod") == 0
    assert _cli("validate", str(path)) == 0
    for env, expected in (("prod", "runtime/out/prod.csv"), (None, "runtime/out/dev.csv")):
        out = tmp_path / f"bundle-{env}.yaml"
        argv = ["bundle", str(path), "--out", str(out)] + (["--env", env] if env else [])
        assert _cli(*argv) == 0
        assert _path_of(yaml.safe_load(out.read_text(encoding="utf-8"))) == expected


def test_c04_pointer_to_a_node_holding_a_file_ref_gets_the_inlined_content(tmp_path: Path) -> None:
    _write(tmp_path / "s.yaml", "k: v\nn: [1, 2]\n")

    contract = _lc(tmp_path, 'shared: {$ref: ./s.yaml}\nx: {$ref: "#/shared"}\n')

    assert contract["shared"] == {"k": "v", "n": [1, 2]}
    assert contract["x"] == {"k": "v", "n": [1, 2]}


def test_c05_pointer_chain_resolves_every_link(tmp_path: Path) -> None:
    contract = _lc(tmp_path, 'a: {$ref: "#/b"}\nb: {$ref: "#/c"}\nc: 1\n')

    assert contract == {"a": 1, "b": 1, "c": 1}


# ── C-06..C-08: cycles ──────────────────────────────────────────────────


def test_c06_pointer_into_its_own_subtree_is_a_cycle(tmp_path: Path) -> None:
    path = _contract(tmp_path, 'a: {b: {$ref: "#/a"}}\n')

    message = _ref_error(
        lambda: load_contract(path), ref="#/a", file=path, pointer="/a/b", has=("Circular",)
    )
    assert "RecursionError" not in message


def test_c07_whole_document_pointer_from_inside_the_document_is_a_cycle(tmp_path: Path) -> None:
    path = _contract(tmp_path, 'x: {$ref: "#"}\n')

    _ref_error(lambda: load_contract(path), ref="#", file=path, pointer="/x", has=("Circular",))


def test_c08_two_pointers_naming_each_other_are_a_cycle_not_a_recursion_error(
    tmp_path: Path,
) -> None:
    path = _contract(tmp_path, 'a: {$ref: "#/b"}\nb: {$ref: "#/a"}\n')

    message = _ref_error(lambda: load_contract(path), file=path, has=("Circular",))
    assert "'#/a'" in message or "'#/b'" in message
    assert "'/a'" in message or "'/b'" in message


# ── C-09..C-15: finding the place ───────────────────────────────────────


def test_c09_pointer_to_nothing_names_ref_file_location_and_the_keys_there(tmp_path: Path) -> None:
    path = _contract(tmp_path, 'extensions: {a: 1, b: 2}\nx: {$ref: "#/extensions/nope"}\n')

    message = _ref_error(
        lambda: load_contract(path), ref="#/extensions/nope", file=path, pointer="/x"
    )
    assert "nope" in message
    assert "'a'" in message and "'b'" in message, "the keys available at the failing step"


def test_c10_list_index_in_range_resolves_and_out_of_range_is_an_error(tmp_path: Path) -> None:
    ok = _lc(tmp_path, 'builds:\n  - id: transform\nx: {$ref: "#/builds/0/id"}\n')
    assert ok["x"] == "transform"

    path = _contract(tmp_path, 'builds:\n  - id: transform\nx: {$ref: "#/builds/9/id"}\n')
    _ref_error(lambda: load_contract(path), ref="#/builds/9/id", has=("not a valid list index",))


def test_c11_rfc6901_escapes_decode_in_the_right_order(tmp_path: Path) -> None:
    """``~1`` is ``/`` and ``~0`` is ``~``; ``~01`` is the two characters ``~1``."""
    doc = {
        "a/b": 1,
        "c~d": 2,
        "~1": 3,
        "x1": {"$ref": "#/a~1b"},
        "x2": {"$ref": "#/c~0d"},
        "x3": {"$ref": "#/~01"},
    }

    contract = _lc(tmp_path, _dump(doc))

    assert (contract["x1"], contract["x2"], contract["x3"]) == (1, 2, 3)


def test_c12_bare_fragment_without_a_slash_is_not_a_json_pointer(tmp_path: Path) -> None:
    path = _contract(tmp_path, 'foo: 1\nx: {$ref: "#foo"}\n')

    _ref_error(
        lambda: load_contract(path),
        ref="#foo",
        file=path,
        pointer="/x",
        has=("not a JSON pointer",),
    )


def test_c12_file_ref_with_a_bare_fragment_stays_lenient_for_released_contracts(
    tmp_path: Path,
) -> None:
    """Characterisation, deliberate: ``file.yaml#foo`` reads key ``foo`` (not reported)."""
    _write(tmp_path / "x.yaml", "foo: 5\nbar: {baz: 6}\n")

    assert _lc(tmp_path, 'a: {$ref: "./x.yaml#foo"}\n')["a"] == 5
    assert _lc(tmp_path, 'a: {$ref: "./x.yaml#bar/baz"}\n')["a"] == 6


def test_c13_trailing_and_lone_slash_pointers_sg01(tmp_path: Path) -> None:
    """SG-01 characterisation. RFC 6901 reads ``/`` as the key ``""``; the resolver strips
    the slash: ``#/`` names the whole document and ``#/bar/`` names ``bar``."""
    _write(tmp_path / "x.yaml", "'': EMPTY-KEY\nbar: {'': INNER, z: 1}\nq: 3\n")

    whole = _lc(tmp_path, 'a: {$ref: "./x.yaml#/"}\n')["a"]
    assert whole == {"": "EMPTY-KEY", "bar": {"": "INNER", "z": 1}, "q": 3}
    assert _lc(tmp_path, 'a: {$ref: "./x.yaml#/bar/"}\n')["a"] == {"": "INNER", "z": 1}

    path = _contract(tmp_path, 'x: {$ref: "#/"}\n')
    _ref_error(lambda: load_contract(path), file=path, has=("Circular",))


_FILE_LIST = "l: [a, b, c]\n"


def _list_pointer(tmp_path: Path, where: str, token: str) -> Any:
    """The value at ``/l/<token>``, by file pointer or by same-document pointer."""
    if where == "file":
        _write(tmp_path / "x.yaml", _FILE_LIST)
        doc = {"a": {"$ref": f"./x.yaml#/l/{token}"}}
    else:
        doc = {"l": ["a", "b", "c"], "a": {"$ref": f"#/l/{token}"}}
    return load_contract(_contract(tmp_path, _dump(doc)))["a"]


@pytest.mark.parametrize("where", ["file", "doc"])
@pytest.mark.parametrize(
    "token",
    ["-", "١", "²", "+1", " 1", "1 ", "-1", "3"],
    ids=[
        "dash",
        "arabic-indic-1",
        "superscript-2",
        "plus-1",
        "lead-space",
        "trail-space",
        "-1",
        "3",
    ],
)
def test_c14_non_ascii_signed_and_out_of_range_list_indexes_are_clean_errors(
    tmp_path: Path, where: str, token: str
) -> None:
    """Never a bare ValueError (a traceback): ``RefResolutionError`` for every one."""
    with pytest.raises(RefResolutionError, match="not a valid list index"):
        _list_pointer(tmp_path, where, token)


@pytest.mark.parametrize("where", ["file", "doc"])
def test_c14_leading_zero_index_is_read_as_a_number_sg04(tmp_path: Path, where: str) -> None:
    """SG-04 characterisation: RFC 6901 forbids ``01``; today it names index 1 (``00``: 0)."""
    assert _list_pointer(tmp_path, where, "01") == "b"
    assert _list_pointer(tmp_path, where, "00") == "a"


def test_c15_invalid_escapes_find_nothing_sg04(tmp_path: Path) -> None:
    """SG-04 characterisation: ``~2`` and a trailing ``~`` are not decoded, so they name
    no key: an error naming the pointer (RFC 6901 calls it an error too)."""
    _write(tmp_path / "y.yaml", "foo: 1\na: 2\n")
    for pointer in ("/fo~2o", "/fo~", "/a~"):
        path = _contract(tmp_path, _dump({"v": {"$ref": f"./y.yaml#{pointer}"}}))
        _ref_error(lambda: load_contract(path), has=(pointer,))
    assert _lc(tmp_path, _dump({"fo~2o": 7, "v": {"$ref": "#/fo~2o"}}))["v"] == 7


# ── C-16..C-18: siblings and what a pointer lands on ────────────────────


def test_c16_siblings_merge_over_a_copy_and_the_source_is_unchanged(tmp_path: Path) -> None:
    contract = _lc(
        tmp_path,
        "extensions: {conf: {parallelism: 2, checkpoints: 60s}}\n"
        'x: {$ref: "#/extensions/conf", parallelism: 8}\n',
    )

    assert contract["x"] == {"parallelism": 8, "checkpoints": "60s"}
    assert contract["extensions"]["conf"] == {"parallelism": 2, "checkpoints": "60s"}


@pytest.mark.parametrize("order", ["pointer-first", "target-first"])
def test_c17_siblings_over_a_pointer_to_a_list_are_refused(tmp_path: Path, order: str) -> None:
    pointer = 'x: {$ref: "#/rules", extra: 1}\n'
    text = pointer + "rules: [a, b]\n" if order == "pointer-first" else "rules: [a, b]\n" + pointer
    path = _contract(tmp_path, text)

    _ref_error(
        lambda: load_contract(path),
        ref="#/rules",
        file=path,
        pointer="/x",
        has=("extra", "a list, not an object"),
    )


def test_c18_pointer_to_text_is_the_string_and_cannot_be_traversed(tmp_path: Path) -> None:
    _write(tmp_path / "q.sql", "SELECT 1")
    head = "builds:\n  - id: t\n    properties: {sql: {$ref: ./q.sql}}\n"

    contract = _lc(tmp_path, head + 'x: {$ref: "#/builds/0/properties/sql"}\n')
    assert contract["x"] == "SELECT 1"

    path = _contract(tmp_path, head + 'x: {$ref: "#/builds/0/properties/sql/0"}\n')
    _ref_error(lambda: load_contract(path), has=("Cannot traverse into str",))


# ── C-19, C-20: overlays ────────────────────────────────────────────────


def test_c19_pointer_to_an_overlay_only_node_needs_the_env(tmp_path: Path) -> None:
    path = _contract(tmp_path, 'id: x\nextensions: {}\ny: {$ref: "#/extensions/prodOnly"}\n')
    _write(tmp_path / "overlays" / "prod.yaml", "extensions: {prodOnly: {a: 1}}\n")

    assert load_with_overlay(path, "prod")["y"] == {"a": 1}
    # SG-39: every env-less load of this contract fails
    _ref_error(
        lambda: load_with_overlay(path, None),
        ref="#/extensions/prodOnly",
        file=path,
        pointer="/y",
    )


def test_c19_validate_passes_with_the_env_and_names_the_pointer_without(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.chdir(tmp_path)
    doc = _min()
    doc["extensions"] = {"x": {"$ref": "#/extensions/prodOnly"}}
    path = _contract(tmp_path, _dump(doc))
    _write(tmp_path / "overlays" / "prod.yaml", "extensions: {prodOnly: {a: 1}}\n")

    assert _cli("validate", str(path), "--env", "prod") == 0
    capsys.readouterr()
    assert _cli("validate", str(path)) == 1
    seen = capsys.readouterr()
    assert _squash("#/extensions/prodOnly") in _squash(seen.out + seen.err)


def test_c20_pointer_written_inside_an_overlay_sees_base_and_overlay(tmp_path: Path) -> None:
    path = _contract(tmp_path, "id: x\nextensions: {a: 0, b: base}\n")
    _write(
        tmp_path / "overlays" / "prod.yaml",
        'extensions: {a: 1, copyA: {$ref: "#/extensions/a"}, copyB: {$ref: "#/extensions/b"}}\n',
    )

    prod = load_with_overlay(path, "prod")["extensions"]

    assert prod == {"a": 1, "b": "base", "copyA": 1, "copyB": "base"}
    assert load_with_overlay(path, None)["extensions"] == {"a": 0, "b": "base"}


# ── C-21..C-23: copies, bombs and where pointers may sit ────────────────


def test_c21_bundle_has_no_yaml_anchors_for_pointers_or_file_refs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _write(tmp_path / "f.yaml", "k: [1, 2]\n")
    doc = _min()
    doc["extensions"] = {
        "node": {"a": [1, 2]},
        "p1": {"$ref": "#/extensions/node"},
        "p2": {"$ref": "#/extensions/node"},
        "f1": {"$ref": "./f.yaml"},
        "f2": {"$ref": "./f.yaml"},
    }
    path = _contract(tmp_path, _dump(doc))
    out = tmp_path / "bundle.yaml"

    assert _cli("bundle", str(path), "--out", str(out)) == 0

    text = out.read_text(encoding="utf-8")
    events = list(yaml.parse(text))
    assert not [e for e in events if isinstance(e, yaml.AliasEvent)], "an alias (*id) was written"
    assert not [e for e in events if getattr(e, "anchor", None)], "an anchor (&id) was written"
    ext = yaml.safe_load(text)["extensions"]
    assert ext["p1"] == ext["p2"] == ext["node"] == {"a": [1, 2]}
    assert ext["f1"] == ext["f2"] == {"k": [1, 2]}


def test_c21_two_pointers_and_two_file_refs_share_no_container(tmp_path: Path) -> None:
    _write(tmp_path / "f.yaml", "k: [1, 2]\n")
    contract = _lc(
        tmp_path,
        'node: {a: [1, 2]}\np1: {$ref: "#/node"}\np2: {$ref: "#/node"}\n'
        "f1: {$ref: ./f.yaml}\nf2: {$ref: ./f.yaml}\n",
    )
    sites = [contract["node"], contract["p1"], contract["p2"], contract["f1"], contract["f2"]]
    inner = [contract["node"]["a"], contract["p1"]["a"], contract["p2"]["a"]]
    inner += [contract["f1"]["k"], contract["f2"]["k"]]
    assert len({id(s) for s in sites}) == len(sites)
    assert len({id(i) for i in inner}) == len(inner)

    contract["p1"]["a"].append(99)
    contract["f1"]["k"].append(98)

    assert contract["node"] == {"a": [1, 2]}
    assert contract["p2"] == {"a": [1, 2]}
    assert contract["f2"] == {"k": [1, 2]}


def _pointer_bomb(tmp_path: Path) -> Path:
    """``p0`` is 1 KiB and every ``pN`` holds two pointers to ``p(N-1)``, 30 levels."""
    lines = ["extensions:", "  p0: " + json.dumps("x" * 1024)]
    lines += [
        f'  p{n}: [{{$ref: "#/extensions/p{n - 1}"}}, {{$ref: "#/extensions/p{n - 1}"}}]'
        for n in range(1, 31)
    ]
    return _contract(tmp_path, "\n".join(lines) + "\n")


def test_c22_pointer_bomb_in_one_file_is_refused_by_the_cap_in_seconds(tmp_path: Path) -> None:
    path = _pointer_bomb(tmp_path)

    started = time.monotonic()
    message = _ref_error(lambda: load_contract(path), file=path, has=("#/extensions/p",))
    elapsed = time.monotonic() - started

    assert "'/extensions/p" in message, "the location of the pointer that crossed the cap"
    assert f"{contract_refs.MAX_REF_EXPANSION_NODES:,}" in message
    assert elapsed < 5, f"the cap took {elapsed:.1f}s to refuse a pointer bomb"


def test_c22_pointer_bomb_is_refused_with_bounded_memory(tmp_path: Path) -> None:
    path = _pointer_bomb(tmp_path)

    tracemalloc.start()
    try:
        _ref_error(lambda: load_contract(path), file=path)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert peak < 64 * 2**20, f"peak {peak / 2**20:.0f} MiB while refusing a pointer bomb"


def test_c23_pointers_in_open_objects_resolve_and_leave_no_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pointers inside ``properties.parameters``, ``labels``, ``extensions`` and
    ``binding.properties`` all resolve; none survives into the bundle."""
    monkeypatch.chdir(tmp_path)
    doc = _min()
    doc["labels"] = {
        "cost": {"$ref": "#/extensions/shared/cost"},
        "team": {"$ref": "#/extensions/shared/team"},
    }
    doc["extensions"] = {
        "shared": {"cost": "cc-42", "team": "ops", "jobs": {"parallelism": 4}},
        "copy": {"$ref": "#/extensions/shared"},
    }
    props = doc["builds"][0]["properties"]
    props["parameters"] = {
        "shared": {"k": "v"},
        "mirrored": {"$ref": "#/builds/0/properties/parameters/shared"},
    }
    doc["exposes"][0]["binding"]["properties"] = {"p": {"$ref": "#/extensions/shared/jobs"}}
    path = _contract(tmp_path, _dump(doc))
    out = tmp_path / "bundle.yaml"

    assert _cli("validate", str(path)) == 0
    assert _cli("bundle", str(path), "--out", str(out)) == 0

    bundled = yaml.safe_load(out.read_text(encoding="utf-8"))
    assert _no_ref_key(bundled)
    assert bundled["labels"] == {"cost": "cc-42", "team": "ops"}
    assert bundled["extensions"]["copy"] == bundled["extensions"]["shared"]
    assert bundled["builds"][0]["properties"]["parameters"]["mirrored"] == {"k": "v"}
    assert bundled["exposes"][0]["binding"]["properties"] == {"p": {"parallelism": 4}}


# ── C-25..C-34: where pointers go wrong ─────────────────────────────────


def test_c25_pointer_inside_a_file_ref_target_resolves_from_the_root(tmp_path: Path) -> None:
    """``./frag.yaml#/x`` holds ``x: {$ref: "#/y"}``: the ``y`` is the root's, not frag's."""
    _write(tmp_path / "frag.yaml", 'x: {$ref: "#/y"}\ny: LOCAL\n')

    assert _lc(tmp_path, "y: ROOT\nz: {$ref: ./frag.yaml#/x}\n") == {"y": "ROOT", "z": "ROOT"}


def test_c25_without_a_root_target_the_fragments_own_key_is_not_used(tmp_path: Path) -> None:
    fragment = _write(tmp_path / "frag.yaml", 'x: {$ref: "#/y"}\ny: LOCAL\n')
    path = _contract(tmp_path, "z: {$ref: ./frag.yaml#/x}\n")

    _ref_error(lambda: load_contract(path), ref="#/y", file=fragment, pointer="/x")


def test_c25_whole_fragment_keeps_its_own_key_and_resolves_its_pointer_from_the_root(
    tmp_path: Path,
) -> None:
    _write(tmp_path / "frag.yaml", 'x: {$ref: "#/y"}\ny: LOCAL\n')

    contract = _lc(tmp_path, "y: ROOT\nz: {$ref: ./frag.yaml}\n")

    assert contract["z"] == {"x": "ROOT", "y": "LOCAL"}


def test_c26_literal_keys_with_dots_spaces_and_unicode_resolve(tmp_path: Path) -> None:
    doc = {
        "scan.startup.mode": "earliest",
        "with space": "SP",
        "ключ": "CY",
        "\U0001f600": "EM",
        "a": {"$ref": "#/scan.startup.mode"},
        "b": {"$ref": "#/with space"},
        "c": {"$ref": "#/ключ"},
        "d": {"$ref": "#/\U0001f600"},
    }

    contract = _lc(tmp_path, _dump(doc))

    assert [contract[k] for k in "abcd"] == ["earliest", "SP", "CY", "EM"]


def test_c27_percent_escapes_are_not_decoded_sg02(tmp_path: Path) -> None:
    """SG-02 characterisation: ``%20`` and ``%2F`` are ordinary characters of the key."""
    for pointer in ("#/with%20space", "#/a%2Fb"):
        path = _contract(tmp_path, _dump({"with space": 1, "a/b": 2, "x": {"$ref": pointer}}))
        _ref_error(lambda: load_contract(path), ref=pointer, file=path, pointer="/x")

    assert (
        _lc(tmp_path, _dump({"with%20space": "PCT", "x": {"$ref": "#/with%20space"}}))["x"] == "PCT"
    )


@pytest.mark.parametrize("token", ["200", "true", "True", "on"])
def test_c28_a_pointer_token_never_matches_a_non_string_key_sg03(
    tmp_path: Path, token: str
) -> None:
    """SG-03 characterisation: YAML ``200:`` is the int 200 and ``on:`` is True, and a
    pointer token is text, so neither can be named. A clean error, listing the keys."""
    path = _contract(tmp_path, f'x: {{200: TWO, on: ON}}\na: {{$ref: "#/x/{token}"}}\n')
    assert list(yaml.safe_load(path.read_text(encoding="utf-8"))["x"]) == [200, True]

    _ref_error(lambda: load_contract(path), ref=f"#/x/{token}", file=path, pointer="/a")


def test_c29_pointer_to_a_key_a_sibling_null_removed_is_an_error(tmp_path: Path) -> None:
    _write(tmp_path / "t.yaml", "a: 1\nb: 2\n")
    path = _contract(tmp_path, 'x: {$ref: ./t.yaml, b: null}\ny: {$ref: "#/x/b"}\n')

    _ref_error(lambda: load_contract(path), ref="#/x/b", file=path, pointer="/y")
    assert _lc(tmp_path, 'x: {$ref: ./t.yaml, b: null}\ny: {$ref: "#/x/a"}\n')["y"] == 1


def test_c30_pointer_to_a_sibling_merged_ref_node_gets_the_merged_value(tmp_path: Path) -> None:
    _write(tmp_path / "t.yaml", "a: 1\nb: 2\n")

    from_file = _lc(tmp_path, 'x: {$ref: ./t.yaml, c: 3, b: 9}\ny: {$ref: "#/x"}\n')
    assert from_file["x"] == from_file["y"] == {"a": 1, "b": 9, "c": 3}

    from_pointer = _lc(
        tmp_path, 'base: {a: 1, b: 2}\nx: {$ref: "#/base", c: 3, b: 9}\ny: {$ref: "#/x"}\n'
    )
    assert from_pointer["y"] == {"a": 1, "b": 9, "c": 3}
    assert from_pointer["base"] == {"a": 1, "b": 2}


def _pointer_chain(length: int) -> str:
    lines = ["p0: 1"] + [f'p{n}: {{$ref: "#/p{n - 1}"}}' for n in range(1, length + 1)]
    return "\n".join(lines) + "\n"


def test_c31_twenty_five_chained_pointers_end_cleanly_sg06(tmp_path: Path) -> None:
    """Either a value or a capped error: never a RecursionError."""
    path = _contract(tmp_path, _pointer_chain(25))

    try:
        contract = load_contract(path)
    except RefResolutionError as error:
        assert "depth" in str(error)
    else:
        assert contract["p25"] == 1


def test_c31_pointer_chains_share_the_file_ref_depth_limit_sg06(tmp_path: Path) -> None:
    """SG-06 characterisation: a chain of MAX_REF_DEPTH pointers resolves, one more is capped."""
    limit = contract_refs.MAX_REF_DEPTH
    assert _lc(tmp_path, _pointer_chain(limit))[f"p{limit}"] == 1

    path = _contract(tmp_path, _pointer_chain(limit + 1))
    _ref_error(lambda: load_contract(path), has=(f"depth exceeded {limit}",))


_C32_PROPERTIES = {
    "json-schema-defs": {
        "jsonSchema": {
            "$defs": {"Order": {"type": "object"}},
            "properties": {"o": {"$ref": "#/$defs/Order"}},
        }
    },
    "openapi-components": {
        "openapi": {
            "components": {"schemas": {"Order": {"type": "object"}}},
            "paths": {
                "/orders": {"get": {"responses": {"200": {"schema": {"$ref": "#/components/x"}}}}}
            },
        }
    },
    "property-named-ref": {
        "jsonSchema": {"type": "object", "properties": {"$ref": {"type": "string"}}}
    },
}


@pytest.mark.parametrize("variant", sorted(_C32_PROPERTIES))
def test_c32_embedded_schema_refs_resolve_against_the_contract_and_fail_sg05(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
    variant: str,
) -> None:
    """SG-05 characterisation: an embedded JSON Schema / OpenAPI ``$ref`` is a contract ref
    (and a property named ``$ref`` is a ref with a non-string value). Both commands fail
    with a ref error that names the pointer; nothing is written, nothing crashes."""
    monkeypatch.chdir(tmp_path)
    doc = _min()
    doc["exposes"][0]["binding"]["properties"] = _C32_PROPERTIES[variant]
    path = _contract(tmp_path, _dump(doc))
    out = tmp_path / "bundle.yaml"

    assert _cli("validate", str(path)) == 1
    seen = capsys.readouterr()
    assert _cli("bundle", str(path), "--out", str(out)) == 2
    seen_bundle = capsys.readouterr()

    assert not out.exists()
    for text in (seen.out + seen.err, seen_bundle.out + seen_bundle.err):
        squashed = _squash(text)
        assert "/exposes/0/binding/properties/" in squashed
        assert "Traceback" not in text and "cli_unhandled_exception" not in text


def test_c33_pointer_keys_are_case_sensitive(tmp_path: Path) -> None:
    path = _contract(tmp_path, 'builds: [{id: a}]\nx: {$ref: "#/Builds"}\n')

    _ref_error(lambda: load_contract(path), ref="#/Builds", file=path, pointer="/x")


def test_c34_empty_ref_is_read_as_the_whole_document_and_so_a_cycle_sg01(tmp_path: Path) -> None:
    """SG-01 characterisation: ``$ref: ""`` is the same document, like ``#``."""
    path = _contract(tmp_path, 'a: 1\nx: {$ref: ""}\n')

    _ref_error(lambda: load_contract(path), file=path, pointer="/x", has=("Circular",))


# ── B-14: a pointer beside a file ref ───────────────────────────────────


def test_b14_pointer_sibling_is_resolved_after_composition_and_sees_the_env(
    tmp_path: Path,
) -> None:
    _write(tmp_path / "t.yaml", "engine: flink\nparallelism: 1\n")
    _write(tmp_path / "overlays" / "prod.yaml", "extensions: {defaults: {parallelism: 16}}\n")
    path = _contract(
        tmp_path,
        "id: x\nextensions: {defaults: {parallelism: 2}}\n"
        'build: {$ref: ./t.yaml, parallelism: {$ref: "#/extensions/defaults/parallelism"}}\n',
    )

    assert load_with_overlay(path, None)["build"] == {"engine": "flink", "parallelism": 2}
    assert load_with_overlay(path, "prod")["build"] == {"engine": "flink", "parallelism": 16}
    assert load_contract(path)["build"]["parallelism"] == 2


def test_b14_pointer_sibling_written_in_a_fragment_also_sees_the_env(tmp_path: Path) -> None:
    _write(tmp_path / "t.yaml", "engine: flink\nparallelism: 1\n")
    _write(
        tmp_path / "frag" / "b.yaml",
        '{$ref: ../t.yaml, parallelism: {$ref: "#/extensions/defaults/parallelism"}}\n',
    )
    _write(tmp_path / "overlays" / "prod.yaml", "extensions: {defaults: {parallelism: 16}}\n")
    path = _contract(
        tmp_path,
        "id: x\nextensions: {defaults: {parallelism: 2}}\nbuild: {$ref: ./frag/b.yaml}\n",
    )

    assert load_with_overlay(path, "prod")["build"]["parallelism"] == 16
    assert load_with_overlay(path, None)["build"]["parallelism"] == 2


# ── B-14, C-20: a pointer is a copy of its place, wherever it is merged ──
#
# A pointer sits in the document until the last pass, but it must behave as the value
# it names. The twin of every case below writes that value in place instead, and the
# docs say what happens to it: objects merge key by key, a scalar replaces, null removes.

_B14_TARGET = "engine: flink\nparallelism: {min: 1, max: 4, extra: 9}\n"


def test_b14_control_object_and_file_ref_siblings_merge_over_an_object_key(tmp_path: Path) -> None:
    """The oracle of the next tests: the same override, written inline or taken from a file."""
    _write(tmp_path / "t.yaml", _B14_TARGET)
    _write(tmp_path / "p.yaml", "min: 2\nmax: 8\n")

    inline = _lc(tmp_path, "build: {$ref: ./t.yaml, parallelism: {min: 2, max: 8}}\n")
    from_file = _lc(tmp_path, "build: {$ref: ./t.yaml, parallelism: {$ref: ./p.yaml}}\n")
    scalar = _lc(tmp_path, "build: {$ref: ./t.yaml, parallelism: 7}\n")

    assert inline["build"]["parallelism"] == {"min": 2, "max": 8, "extra": 9}
    assert from_file["build"]["parallelism"] == {"min": 2, "max": 8, "extra": 9}
    assert scalar["build"]["parallelism"] == 7


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG B14-hybrid: a pointer used as a sibling is merged into an object-valued key of "
        "the file-ref target as a literal {$ref} object, so the target's own keys end up "
        "beside the pointer and beat the values it names (the sibling is silently ignored)"
    ),
)
def test_b14_pointer_sibling_over_an_object_key_merges_like_its_inline_twin(
    tmp_path: Path,
) -> None:
    _write(tmp_path / "t.yaml", _B14_TARGET)
    contract = _lc(
        tmp_path,
        "defaults: {p: {min: 2, max: 8}}\n"
        'build: {$ref: ./t.yaml, parallelism: {$ref: "#/defaults/p"}}\n',
    )

    assert contract["build"]["parallelism"] == {"min": 2, "max": 8, "extra": 9}


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG B14-hybrid: a pointer sibling over a pointer held by the target replaces it "
        "instead of merging over it, so the target's other keys are lost"
    ),
)
def test_b14_pointer_sibling_over_a_pointer_in_the_target_merges_like_its_inline_twin(
    tmp_path: Path,
) -> None:
    _write(tmp_path / "t.yaml", 'parallelism: {$ref: "#/defaults/a"}\n')
    contract = _lc(
        tmp_path,
        "defaults: {a: {x: 1, y: 2}, b: {y: 20}}\n"
        'build: {$ref: ./t.yaml, parallelism: {$ref: "#/defaults/b"}}\n',
    )

    assert contract["build"]["parallelism"] == {"x": 1, "y": 20}


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG B14-scalar: a pointer sibling that names a scalar, over an object-valued key of "
        "the target, is refused with 'has sibling keys (...) but the target is a int', listing "
        "the target's own keys as the siblings; the inline twin (parallelism: 7) composes"
    ),
)
def test_b14_pointer_sibling_naming_a_scalar_replaces_an_object_key(tmp_path: Path) -> None:
    _write(tmp_path / "t.yaml", _B14_TARGET)
    contract = _lc(
        tmp_path,
        'defaults: {p: 7}\nbuild: {$ref: ./t.yaml, parallelism: {$ref: "#/defaults/p"}}\n',
    )

    assert contract["build"]["parallelism"] == 7


def _overlay_binding(tmp_path: Path, location: Dict[str, Any]) -> Dict[str, Any]:
    """prod's override of the ``out`` expose's binding location, from the overlay's text."""
    path = _contract(tmp_path, _dump(_min()))
    overlay = {
        "extensions": {"prodLocation": {"path": "runtime/out/prod.csv"}},
        "exposes": [{"exposeId": "out", "binding": {"location": location}}],
    }
    _write(tmp_path / "overlays" / "prod.yaml", _dump(overlay))
    return load_with_overlay(path, "prod")["exposes"][0]["binding"]["location"]


def test_c20_control_overlay_object_overrides_the_base_binding_location(tmp_path: Path) -> None:
    assert _overlay_binding(tmp_path, {"path": "runtime/out/prod.csv"}) == {
        "path": "runtime/out/prod.csv"
    }


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG C20-hybrid: an overlay value that is a pointer, over an object the base has, is "
        "merged in as a literal {$ref} object, so the base's keys end up beside the pointer "
        "and beat the values it names: the overlay silently fails to override them"
    ),
)
def test_c20_overlay_pointer_over_a_base_object_key_merges_like_its_inline_twin(
    tmp_path: Path,
) -> None:
    location = _overlay_binding(tmp_path, {"$ref": "#/extensions/prodLocation"})

    assert location == {"path": "runtime/out/prod.csv"}
