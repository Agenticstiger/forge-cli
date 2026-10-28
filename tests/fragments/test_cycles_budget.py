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

"""Cycles, nesting depth and the expansion budget: scenario catalog area E.

Test names carry the id of the scenario in the fragments scenario catalog (E-01 ...
E-26); ``SG-nn`` is its register of questions the spec leaves open.

The spec (catalog section 1, items 1, 4, 6 and 7): cycles are errors, diamonds are
fine; file refs nest at most 20 deep; after composition no ``$ref`` remains, and one
that cannot be resolved is an error naming the ref, the file it was written in and its
JSON pointer; refs may bring at most 1,000,000 values or 50 MiB of characters into one
composition, files and pointers together.

The caps are monkeypatched wherever a test needs one to trip quickly. The wording of the
depth and CLI errors (E-08, E-26) is pinned by the message tests: here they pin the
boundary, the exit codes and that the run ends. A scenario that cites an open question
(SG-34) pins what the code does today.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

import pytest
import yaml

import fluid_build
from fluid_build import contract_refs, loader
from fluid_build.contract_refs import RefBudget, resolve_file_refs, resolve_pointer_refs
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


def _contract(root: Path, text: str, name: str = "contract.fluid.yaml") -> Path:
    return _write(root / name, text)


def _squash(text: str) -> str:
    """Console output wraps long lines anywhere: compare without whitespace."""
    return "".join(text.split())


def _ref_error(
    call: Callable[[], Any],
    *,
    file: Optional[Path] = None,
    pointer: Optional[str] = None,
    has: tuple = (),
) -> str:
    """Run ``call``, expect a ``RefResolutionError``, check what its message names."""
    with pytest.raises(RefResolutionError) as info:
        call()
    message = str(info.value)
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
id: refs.cycles
name: Cycles
description:
  purpose: Cycles.
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


def _min_with_extensions(extensions: Dict[str, Any]) -> str:
    doc = yaml.safe_load(_MIN)
    doc["extensions"] = extensions
    return _dump(doc)


def _cli(*argv: str) -> int:
    from fluid_build.cli import main

    return main(list(argv))


def _fan_in_bomb(root: Path, levels: int, leaf: str) -> None:
    """``l0.yaml`` is ``leaf``; ``lN.yaml`` holds two refs to ``l(N-1).yaml``."""
    _write(root / "l0.yaml", leaf + "\n")
    for n in range(1, levels + 1):
        _write(root / f"l{n}.yaml", f"- {{$ref: ./l{n - 1}.yaml}}\n- {{$ref: ./l{n - 1}.yaml}}\n")


def _ref_chain(root: Path, refs: int) -> Path:
    """A contract whose first ref starts a chain of ``refs`` nested file refs in all."""
    for i in range(refs - 1):
        _write(root / f"f{i}.yaml", f"next: {{$ref: ./f{i + 1}.yaml}}\n")
    _write(root / f"f{refs - 1}.yaml", "end: true\n")
    return _contract(root, "r: {$ref: ./f0.yaml}\n")


def _nested_flow(levels: int, inner: str) -> str:
    text = inner
    for _ in range(levels):
        text = "{k: " + text + "}"
    return text


def _depth_of(node: Any, key: str) -> int:
    levels = 0
    while isinstance(node, dict) and key in node:
        node = node[key]
        levels += 1
    return levels


# ── E-01..E-07: cycles and diamonds ─────────────────────────────────────


def test_e01_file_that_refs_itself_is_a_cycle(tmp_path: Path) -> None:
    a = _write(tmp_path / "a.yaml", "x: {$ref: ./a.yaml}\n")
    path = _contract(tmp_path, "r: {$ref: ./a.yaml}\n")

    _ref_error(lambda: load_contract(path), file=a, pointer="/x", has=("Circular $ref detected",))


def test_e02_two_files_that_ref_each_other_are_a_cycle(tmp_path: Path) -> None:
    a = _write(tmp_path / "a.yaml", "x: {$ref: ./b.yaml}\n")
    b = _write(tmp_path / "b.yaml", "y: {$ref: ./a.yaml}\n")
    path = _contract(tmp_path, "r: {$ref: ./a.yaml}\n")

    message = _ref_error(
        lambda: load_contract(path), file=b, pointer="/y", has=("Circular $ref detected",)
    )
    assert str(a.resolve()) in message, "the file the cycle comes back to"


@pytest.mark.parametrize("entry", ["#/x", "#/y", ""], ids=["into-x", "into-y", "whole-file"])
def test_e03_pointers_into_one_file_that_name_each_other_are_a_cycle(
    tmp_path: Path, entry: str
) -> None:
    a = _write(tmp_path / "a.yaml", 'x: {$ref: "./a.yaml#/y"}\ny: {$ref: "./a.yaml#/x"}\n')
    path = _contract(tmp_path, _dump({"r": {"$ref": f"./a.yaml{entry}"}}))

    _ref_error(lambda: load_contract(path), file=a, has=("Circular $ref detected",))


def test_e04_cycle_through_a_sibling_is_detected(tmp_path: Path) -> None:
    a = _write(tmp_path / "a.yaml", "x: {$ref: ./b.yaml, back: {$ref: ./a.yaml}}\n")
    _write(tmp_path / "b.yaml", "k: 1\n")
    path = _contract(tmp_path, "r: {$ref: ./a.yaml}\n")

    _ref_error(lambda: load_contract(path), file=a, pointer="/x/back", has=("Circular",))


def test_e05_diamond_is_fine_and_the_shared_content_appears_twice(tmp_path: Path) -> None:
    _write(tmp_path / "b.yaml", "inner: {$ref: ./d.yaml}\n")
    _write(tmp_path / "c.yaml", "inner: {$ref: ./d.yaml}\n")
    _write(tmp_path / "d.yaml", "leaf: value\nitems: [1, 2]\n")
    path = _contract(tmp_path, "b: {$ref: ./b.yaml}\nc: {$ref: ./c.yaml}\n")

    contract = load_contract(path)

    expected = {"inner": {"leaf": "value", "items": [1, 2]}}
    assert contract == {"b": expected, "c": expected}
    assert contract["b"]["inner"] is not contract["c"]["inner"], "copies, not one shared object"


def test_e06_one_sql_file_shared_by_two_builds_is_fine(tmp_path: Path) -> None:
    _write(tmp_path / "s.sql", "SELECT 1")
    path = _contract(
        tmp_path,
        "builds:\n"
        "  - id: a\n    properties: {sql: {$ref: ./s.sql}}\n"
        "  - id: b\n    properties: {sql: {$ref: ./s.sql}}\n",
    )

    builds = load_contract(path)["builds"]

    assert [b["properties"]["sql"] for b in builds] == ["SELECT 1", "SELECT 1"]


def test_e07_the_same_expose_fragment_twice_gives_two_equal_copies(tmp_path: Path) -> None:
    _write(tmp_path / "e.yaml", "exposeId: e\nschema: [{name: id}]\n")
    path = _contract(tmp_path, "exposes: [{$ref: ./e.yaml}, {$ref: ./e.yaml}]\n")

    exposes = load_contract(path)["exposes"]

    assert exposes[0] == exposes[1] == {"exposeId": "e", "schema": [{"name": "id"}]}
    assert exposes[0] is not exposes[1] and exposes[0]["schema"] is not exposes[1]["schema"]


# ── E-08..E-10: depth ───────────────────────────────────────────────────


def test_e08_the_limit_is_twenty_file_refs() -> None:
    assert contract_refs.MAX_REF_DEPTH == 20
    assert loader._MAX_REF_DEPTH == 20, "the loader's alias still names the same limit"


def test_e08_chain_of_twenty_file_refs_composes(tmp_path: Path) -> None:
    contract = load_contract(_ref_chain(tmp_path, 20))

    assert _depth_of(contract["r"], "next") == 19
    leaf = contract["r"]
    while "next" in leaf:
        leaf = leaf["next"]
    assert leaf == {"end": True}


def test_e08_chain_of_twenty_one_file_refs_is_refused_at_the_last_ref(tmp_path: Path) -> None:
    """The boundary, not the wording: it is a ref error, and it points at the ref that went
    too deep (``./f20.yaml``, written in ``f19.yaml``)."""
    path = _ref_chain(tmp_path, 21)

    message = _ref_error(lambda: load_contract(path), file=tmp_path / "f19.yaml")

    assert "./f20.yaml" in message


def test_e08_the_depth_limit_is_the_module_constant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(contract_refs, "MAX_REF_DEPTH", 3)

    assert _depth_of(load_contract(_ref_chain(tmp_path / "ok", 3))["r"], "next") == 2
    path = _ref_chain(tmp_path / "over", 4)
    _ref_error(lambda: load_contract(path), file=tmp_path / "over" / "f2.yaml")


@pytest.mark.parametrize("style", ["flow", "block"])
def test_e09_one_fragment_nested_a_hundred_yaml_levels_is_not_ref_depth(
    tmp_path: Path, style: str
) -> None:
    if style == "flow":
        text = _nested_flow(100, "leaf")
    else:
        text = "\n".join("  " * i + "k:" for i in range(100)) + "\n" + "  " * 100 + "leaf"
    _write(tmp_path / "deep.yaml", text + "\n")
    path = _contract(tmp_path, "r: {$ref: ./deep.yaml}\n")

    contract = load_contract(path)

    assert _depth_of(contract["r"], "k") == 100


def test_e09_yaml_nesting_has_its_own_cap_of_128_levels(tmp_path: Path) -> None:
    _write(tmp_path / "ok.yaml", _nested_flow(128, "leaf") + "\n")
    _write(tmp_path / "over.yaml", _nested_flow(129, "leaf") + "\n")

    assert _depth_of(load_contract(_contract(tmp_path, "r: {$ref: ./ok.yaml}\n"))["r"], "k") == 128

    path = _contract(tmp_path, "r: {$ref: ./over.yaml}\n")
    message = _ref_error(lambda: load_contract(path), file=path, pointer="/r")
    assert "./over.yaml" in message and "128" in message


def _chained_deep_fragments(root: Path, files: int, levels: int) -> None:
    for i in range(files):
        inner = f"{{$ref: ./f{i + 1}.yaml}}" if i < files - 1 else "end"
        _write(root / f"f{i}.yaml", _nested_flow(levels, inner) + "\n")


def test_e10_ten_chained_fragments_of_120_levels_compose_or_fail_cleanly_sg34(
    tmp_path: Path,
) -> None:
    """SG-34: the walk is recursive. Composed depth ~1200 is past Python's recursion limit:
    the answer is a value or a ``RefResolutionError``, never a ``RecursionError``."""
    _chained_deep_fragments(tmp_path, files=10, levels=120)
    path = _contract(tmp_path, "r: {$ref: ./f0.yaml}\n")

    try:
        contract = load_contract(path)
    except RefResolutionError:
        return
    assert _depth_of(contract["r"], "k") >= 120


def test_e10_validate_accepts_a_deep_composed_contract_that_bundle_cannot_write_sg34(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """SG-34 characterisation. Between what the YAML writer can dump and what composition
    can walk there is a band of depths. A contract in it composes, ``validate`` accepts it
    and ``bundle`` dies in PyYAML's recursive representer, reported by the CLI's catch-all
    ("Unexpected error: maximum recursion depth exceeded", logged as an unhandled
    exception). The writer's limit is not new: the same depth written inline, with no ref
    at all, fails the same way. The band depends on the interpreter, so the depth is
    searched for, and the test skips where there is none."""
    monkeypatch.chdir(tmp_path)
    found: Optional[Dict[str, Any]] = None
    for levels in (40, 50, 60, 70, 80, 90, 100):
        product = tmp_path / f"deep{levels}"
        _chained_deep_fragments(product, files=10, levels=levels)
        path = _contract(product, _min_with_extensions({"deep": {"$ref": "./f0.yaml"}}))
        try:
            load_contract(path)
        except RefResolutionError:
            break  # composition itself now refuses: no band left
        capsys.readouterr()
        out = product / "bundle.yaml"
        code = _cli("bundle", str(path), "--out", str(out))
        seen = capsys.readouterr()
        if code != 0:
            found = {
                "path": path,
                "out": out,
                "levels": levels,
                "text": seen.out + seen.err,
            }
            break
    if found is None:
        pytest.skip("no depth between what bundle can write and what composition can walk")

    assert "maximum recursion depth exceeded" in found["text"]
    assert not found["out"].exists()
    assert _cli("validate", str(found["path"])) == 0, "validate accepts what bundle cannot write"

    # The same depth with no ref at all: the limit belongs to the writer, not to composition.
    inline = yaml.safe_load(_MIN)
    deep: Any = "end"
    for _ in range(10 * found["levels"]):
        deep = {"k": deep}
    inline["extensions"] = {"deep": deep}
    try:
        text = json.dumps(inline)
    except RecursionError:
        return  # this interpreter cannot even write the control document
    capsys.readouterr()
    inline_path = _write(tmp_path / "inline" / "contract.fluid.json", text)
    assert _cli("bundle", str(inline_path), "--out", str(tmp_path / "inline" / "b.yaml")) != 0


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG E10-msg: a RecursionError raised while a fragment is parsed (the stack is already "
        "deep from the walk) is reported as 'Failed to parse $ref target ...: maximum recursion "
        "depth exceeded', blaming a fragment that is fine, not with the 'nests too deep' message "
        "built for it"
    ),
)
def test_e10_recursion_while_parsing_a_fragment_is_reported_as_nesting_depth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With 10 chained fragments of 120 levels the error does arise inside the parse of a
    healthy fragment (f7.yaml); the parser is made to raise it here so the test holds on any
    interpreter."""
    _write(tmp_path / "f.yaml", "k: 1\n")
    path = _contract(tmp_path, "r: {$ref: ./f.yaml}\n")

    def too_deep(_path: Path) -> Any:
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(contract_refs, "_parse_target", too_deep)
    message = _ref_error(lambda: load_contract(path))

    assert "Failed to parse" not in message
    assert "too deep" in message


# ── E-11, E-12: file and pointer bombs ──────────────────────────────────


def test_e11_file_bomb_under_the_depth_limit_is_stopped_by_the_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``l0`` is a 1 KiB string and every ``lN`` holds two refs to ``l(N-1)``: 19 levels
    (2**19 leaves) stay inside the depth limit, so only the cap can end it."""
    monkeypatch.setattr(contract_refs, "MAX_REF_EXPANSION_CHARS", 64 * 1024)
    _fan_in_bomb(tmp_path, 19, json.dumps("x" * 1024))
    path = _contract(tmp_path, "r: {$ref: ./l19.yaml}\n")

    started = time.monotonic()
    message = _ref_error(lambda: load_contract(path), has=(f"{64 * 1024:,}",))
    elapsed = time.monotonic() - started

    assert "depth exceeded" not in message, "it was the cap, not the depth limit"
    assert "./l" in message and ".yaml" in message, "the ref where the cap was crossed"
    assert elapsed < 5, f"{elapsed:.1f}s to refuse a patched-cap bomb"


def test_e11_node_cap_stops_a_file_bomb_of_tiny_leaves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(contract_refs, "MAX_REF_EXPANSION_NODES", 500)
    _fan_in_bomb(tmp_path, 19, "1")
    path = _contract(tmp_path, "r: {$ref: ./l19.yaml}\n")

    _ref_error(lambda: load_contract(path), has=("500 values",))


def test_e11_catalog_bomb_of_thirty_levels_is_ended_by_the_depth_limit_in_seconds(
    tmp_path: Path,
) -> None:
    """The catalog's own numbers (N up to 30) never reach the cap: the first descent hits
    the depth limit. It still ends at once, with a ref error."""
    _fan_in_bomb(tmp_path, 30, json.dumps("x" * 1024))
    path = _contract(tmp_path, "r: {$ref: ./l30.yaml}\n")

    started = time.monotonic()
    message = _ref_error(lambda: load_contract(path), has=("depth exceeded 20",))
    elapsed = time.monotonic() - started

    assert elapsed < 5, f"{elapsed:.1f}s"
    assert "./l10.yaml" in message


def _checkout_root() -> Path:
    return Path(fluid_build.__file__).resolve().parent.parent


def _cli_subprocess(args: list, cwd: Path, timeout: float) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    # the checkout under test first, so the child imports it and not an installed copy
    env["PYTHONPATH"] = os.pathsep.join(
        [str(_checkout_root())] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])
    )
    env.pop("FLUID_REF_ROOT", None)
    return subprocess.run(
        [sys.executable, "-m", "fluid_build.cli", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def test_e11_control_a_small_file_bomb_composes_through_the_same_subprocess(
    tmp_path: Path,
) -> None:
    """The harness of the test below, on a bomb of 6 levels (64 refs): it composes at once."""
    _fan_in_bomb(tmp_path, 6, json.dumps("x" * 1024))
    path = _contract(tmp_path, _min_with_extensions({"bomb": {"$ref": "./l6.yaml"}}))

    done = _cli_subprocess(["validate", str(path)], tmp_path, timeout=60)

    assert done.returncode == 0, done.stdout + done.stderr


@pytest.mark.slow
@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG E11-time: the expansion cap bounds the size of what refs bring in, not the work. "
        "Every file ref re-reads and re-parses its target (~0.7 ms), so a 19-level doubling "
        "bomb of 1 KiB fragments (2**19 refs, inside the depth limit) takes ~70 s to be "
        "refused, not under 5 s"
    ),
)
def test_e11_real_caps_file_bomb_is_refused_in_seconds(tmp_path: Path) -> None:
    _fan_in_bomb(tmp_path, 19, json.dumps("x" * 1024))
    path = _contract(tmp_path, _min_with_extensions({"bomb": {"$ref": "./l19.yaml"}}))
    bound = 12.0

    try:
        done = _cli_subprocess(["validate", str(path)], tmp_path, timeout=bound)
    except subprocess.TimeoutExpired:
        pytest.fail(f"fluid validate was still composing a 19-level file bomb after {bound:.0f}s")

    assert done.returncode == 1
    assert "contract_load_failed" in _squash(done.stdout + done.stderr)


def test_e12_mixed_bomb_is_capped_across_both_passes_with_real_constants(tmp_path: Path) -> None:
    """Ten levels of file doubling, then fifteen of pointer doubling in the root."""
    _fan_in_bomb(tmp_path, 10, json.dumps("x" * 1024))
    lines = ["extensions:", "  q0: {$ref: ./l10.yaml}"]
    lines += [
        f'  q{n}: [{{$ref: "#/extensions/q{n - 1}"}}, {{$ref: "#/extensions/q{n - 1}"}}]'
        for n in range(1, 16)
    ]
    path = _contract(tmp_path, "\n".join(lines) + "\n")

    started = time.monotonic()
    message = _ref_error(lambda: load_contract(path), file=path, has=("#/extensions/q",))
    elapsed = time.monotonic() - started

    assert f"{contract_refs.MAX_REF_EXPANSION_CHARS:,}" in message
    assert elapsed < 30, f"{elapsed:.1f}s"


def test_e12_file_pass_and_pointer_pass_draw_on_one_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each pass fits under the cap alone; together they do not, and the loader runs both
    on one budget."""
    _fan_in_bomb(tmp_path, 3, json.dumps("x" * 1000))
    text = (
        "extensions:\n"
        "  q0: {$ref: ./l3.yaml}\n"
        '  q1: [{$ref: "#/extensions/q0"}, {$ref: "#/extensions/q0"}]\n'
    )
    path = _contract(tmp_path, text)
    root_doc = yaml.safe_load(text)

    file_budget = RefBudget()
    composed = resolve_file_refs(root_doc, tmp_path, ref_root=tmp_path, budget=file_budget)
    pointer_budget = RefBudget()
    resolve_pointer_refs(composed, budget=pointer_budget)
    files, pointers = file_budget.chars, pointer_budget.chars
    assert files > 0 and pointers > 0

    monkeypatch.setattr(contract_refs, "MAX_REF_EXPANSION_CHARS", max(files, pointers))
    resolve_file_refs(root_doc, tmp_path, ref_root=tmp_path, budget=RefBudget())
    resolve_pointer_refs(composed, budget=RefBudget())
    _ref_error(lambda: load_contract(path), has=(f"{max(files, pointers):,}",))

    monkeypatch.setattr(contract_refs, "MAX_REF_EXPANSION_CHARS", files + pointers)
    assert len(load_contract(path)["extensions"]["q1"]) == 2


# ── E-13..E-16: what the budget counts, and aliases ─────────────────────


def test_e13_the_cap_counts_the_characters_of_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 5,000-character key doubled five times is 160,000 characters of keys."""
    monkeypatch.setattr(contract_refs, "MAX_REF_EXPANSION_CHARS", 20_000)
    _write(tmp_path / "k0.yaml", _dump({"k" * 5000: 1}))
    for n in range(1, 6):
        _write(
            tmp_path / f"k{n}.yaml", f"- {{$ref: ./k{n - 1}.yaml}}\n- {{$ref: ./k{n - 1}.yaml}}\n"
        )

    _ref_error(
        lambda: resolve_file_refs({"x": {"$ref": "./k5.yaml"}}, tmp_path, ref_root=tmp_path),
        has=("20,000",),
    )


def test_e14_yaml_aliases_in_a_fragment_count_at_their_expanded_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One file is ~83 nodes if shared containers count once and ~1,283 if every alias
    counts as the copy the composition makes; the cap sits between."""
    base = ", ".join(str(i) for i in range(60))
    _write(
        tmp_path / "al.yaml",
        f"base: &b [{base}]\nr: [{', '.join(['*b'] * 20)}]\n",
    )
    refs = {f"x{i}": {"$ref": "./al.yaml"} for i in range(5)}

    monkeypatch.setattr(contract_refs, "MAX_REF_EXPANSION_NODES", 2_000)
    _ref_error(lambda: resolve_file_refs(refs, tmp_path, ref_root=tmp_path), has=("2,000 values",))

    monkeypatch.setattr(contract_refs, "MAX_REF_EXPANSION_NODES", 1_000_000)
    composed = resolve_file_refs(refs, tmp_path, ref_root=tmp_path)
    expanded = RefBudget()
    expanded.charge(composed, "x", "here")
    assert expanded.nodes > 5 * 1_283 - 50, "the composed document really is that big"


def test_e15_recursive_alias_in_a_fragment_is_refused_naming_the_fragment(tmp_path: Path) -> None:
    loop = _write(tmp_path / "loop.yaml", "a: &x [1, *x]\n")
    path = _contract(tmp_path, "r: {$ref: ./loop.yaml}\n")

    with pytest.raises((ValueError, RefResolutionError)) as info:
        load_contract(path)

    assert str(loop.resolve()) in str(info.value)
    assert "contains itself" in str(info.value)


def test_e15_validate_and_bundle_refuse_a_recursive_alias_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.chdir(tmp_path)
    _write(tmp_path / "loop.yaml", "a: &x [1, *x]\n")
    path = _contract(tmp_path, _min_with_extensions({"loop": {"$ref": "./loop.yaml"}}))
    out = tmp_path / "bundle.yaml"

    assert _cli("validate", str(path)) == 1
    validated = capsys.readouterr()
    assert _cli("bundle", str(path), "--out", str(out)) != 0
    bundled = capsys.readouterr()

    assert not out.exists()
    for seen in (validated, bundled):
        text = _squash(seen.out + seen.err)
        assert "loop.yaml" in text and "containsitself" in text
        assert "cli_unhandled_exception" not in text and "Traceback" not in text


def test_e16_classic_alias_bomb_in_a_fragment_is_refused_by_the_yaml_cap(tmp_path: Path) -> None:
    lines = ["l0: &l0 [x, x]"]
    lines += [f"l{n}: &l{n} [*l{n - 1}, *l{n - 1}, *l{n - 1}, *l{n - 1}]" for n in range(1, 10)]
    _write(tmp_path / "bomb.yaml", "\n".join(lines) + "\n")
    path = _contract(tmp_path, "r: {$ref: ./bomb.yaml}\n")

    started = time.monotonic()
    message = _ref_error(lambda: load_contract(path), file=path, pointer="/r", has=("./bomb.yaml",))

    assert "billion-laughs" in message
    assert time.monotonic() - started < 5


# ── E-17..E-21: the boundaries of the cap ───────────────────────────────


@pytest.mark.slow
@pytest.mark.parametrize("refs, ok", [(10, True), (11, False)])
def test_e17_ten_five_mib_text_refs_are_exactly_the_character_cap(
    tmp_path: Path, refs: int, ok: bool
) -> None:
    assert contract_refs.MAX_REF_EXPANSION_CHARS == 10 * 5 * 1024 * 1024 == 52_428_800
    (tmp_path / "big.txt").write_text("a" * (5 * 1024 * 1024), encoding="utf-8")
    text = "extensions:\n" + "".join(f"  t{i}: {{$ref: ./big.txt}}\n" for i in range(refs))
    path = _contract(tmp_path, text)

    if ok:
        assert len(load_contract(path)["extensions"]) == refs
    else:
        _ref_error(lambda: load_contract(path), file=path, has=("52,428,800",))


@pytest.mark.parametrize("refs, ok", [(10, True), (11, False)])
def test_e17_character_cap_boundary_with_a_patched_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, refs: int, ok: bool
) -> None:
    monkeypatch.setattr(contract_refs, "MAX_REF_EXPANSION_CHARS", 1_000)
    (tmp_path / "t.txt").write_text("a" * 100, encoding="utf-8")
    text = "extensions:\n" + "".join(f"  t{i}: {{$ref: ./t.txt}}\n" for i in range(refs))
    path = _contract(tmp_path, text)

    if ok:
        assert len(load_contract(path)["extensions"]) == refs
    else:
        _ref_error(lambda: load_contract(path), has=("1,000 characters",))


@pytest.mark.parametrize("items, ok", [(999, True), (1000, False)])
def test_e18_value_cap_boundary_with_a_patched_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, items: int, ok: bool
) -> None:
    """A list of 999 values plus the list itself is exactly 1,000 values; one more is not."""
    monkeypatch.setattr(contract_refs, "MAX_REF_EXPANSION_NODES", 1_000)
    _write(tmp_path / "list.json", json.dumps([0] * items))

    if ok:
        composed = resolve_file_refs({"x": {"$ref": "./list.json"}}, tmp_path, ref_root=tmp_path)
        assert composed == {"x": [0] * items}
    else:
        _ref_error(
            lambda: resolve_file_refs({"x": {"$ref": "./list.json"}}, tmp_path, ref_root=tmp_path),
            has=("1,000 values",),
        )


def test_e18_the_real_caps_are_one_million_values_and_fifty_mebibytes() -> None:
    assert contract_refs.MAX_REF_EXPANSION_NODES == 1_000_000
    assert contract_refs.MAX_REF_EXPANSION_CHARS == 52_428_800

    exactly = RefBudget()
    exactly.charge([0] * 999_999, "./f", "here")
    assert exactly.nodes == 1_000_000
    with pytest.raises(RefResolutionError, match="1,000,000 values"):
        RefBudget().charge([0] * 1_000_000, "./f", "here")

    text = RefBudget()
    text.charge("a" * 52_428_800, "./f", "here")
    assert text.chars == 52_428_800
    with pytest.raises(RefResolutionError, match="52,428,800 characters"):
        RefBudget().charge("a" * 52_428_801, "./f", "here")


def test_e19_the_budget_is_per_composition(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A contract that brings in 0.9 of the cap composes again and again in one process."""
    monkeypatch.setattr(contract_refs, "MAX_REF_EXPANSION_CHARS", 1_000)
    (tmp_path / "t.txt").write_text("a" * 900, encoding="utf-8")
    path = _contract(tmp_path, "x: {$ref: ./t.txt}\n")

    for _ in range(3):
        assert load_contract(path) == {"x": "a" * 900}

    twice = _contract(tmp_path, "x: {$ref: ./t.txt}\ny: {$ref: ./t.txt}\n", "twice.fluid.yaml")
    _ref_error(lambda: load_contract(twice), has=("1,000 characters",))
    assert load_contract(path) == {"x": "a" * 900}, "a refused composition leaves no debt"


def _overlay_budget_contract(tmp_path: Path, base: int, overlay: int, pointer: bool) -> Path:
    (tmp_path / "frag").mkdir(parents=True, exist_ok=True)
    (tmp_path / "frag" / "b.txt").write_text("b" * base, encoding="utf-8")
    (tmp_path / "frag" / "o.txt").write_text("o" * overlay, encoding="utf-8")
    ext: Dict[str, Any] = {"fromBase": {"$ref": "./frag/b.txt"}} if base else {}
    path = _contract(tmp_path, _dump({"id": "x", "extensions": ext}))
    over: Dict[str, Any] = {"fromOverlay": {"$ref": "../frag/o.txt"}} if overlay else {}
    if pointer:
        over["copy"] = {"$ref": "#/extensions/fromBase"}
    _write(tmp_path / "overlays" / "prod.yaml", _dump({"extensions": over}))
    return path


def test_e20_base_overlay_and_pointers_draw_on_one_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(contract_refs, "MAX_REF_EXPANSION_CHARS", 1_000)

    fits = _overlay_budget_contract(tmp_path / "fits", base=450, overlay=450, pointer=False)
    assert load_with_overlay(fits, None)["extensions"] == {"fromBase": "b" * 450}
    assert len(load_with_overlay(fits, "prod")["extensions"]) == 2, "450 + 450 fits in 1,000"

    both = _overlay_budget_contract(tmp_path / "both", base=600, overlay=600, pointer=False)
    assert load_with_overlay(both, None)["extensions"] == {"fromBase": "b" * 600}
    _ref_error(lambda: load_with_overlay(both, "prod"), has=("1,000 characters",))

    pointer = _overlay_budget_contract(tmp_path / "ptr", base=450, overlay=450, pointer=True)
    _ref_error(lambda: load_with_overlay(pointer, "prod"), has=("1,000 characters",))


@pytest.mark.slow
def test_e21_a_large_inline_json_root_is_not_charged_to_the_budget(tmp_path: Path) -> None:
    """1.5M values, no ref: the cap is about what refs bring in."""
    path = _write(tmp_path / "contract.fluid.json", json.dumps({"id": "x", "big": [0] * 1_500_000}))

    assert len(load_contract(path)["big"]) == 1_500_000


def test_e21_inline_content_beyond_a_patched_cap_loads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(contract_refs, "MAX_REF_EXPANSION_NODES", 100)
    monkeypatch.setattr(contract_refs, "MAX_REF_EXPANSION_CHARS", 100)
    _write(tmp_path / "small.yaml", "k: v\n")
    doc = {"big": [{"name": "n" * 20, "v": i} for i in range(500)], "x": {"$ref": "./small.yaml"}}
    path = _contract(tmp_path, _dump(doc))

    contract = load_contract(path)

    assert len(contract["big"]) == 500 and contract["x"] == {"k": "v"}


# ── E-22..E-25: odd cycles ──────────────────────────────────────────────


def test_e22_a_symlink_back_to_the_file_is_the_same_file_and_so_a_cycle(tmp_path: Path) -> None:
    a = _write(tmp_path / "a.yaml", "inner: {$ref: ./link.yaml}\n")
    try:
        os.symlink(a, tmp_path / "link.yaml")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available here")

    for entry in ("./a.yaml", "./link.yaml"):
        path = _contract(tmp_path, _dump({"r": {"$ref": entry}}))
        _ref_error(lambda: load_contract(path), file=a, has=("Circular",))


@pytest.mark.parametrize(
    "inner, first",
    [
        ("./a.yaml#", "./a.yaml#"),
        ("./a.yaml#/", "./a.yaml#/"),
        ("./a.yaml", "./a.yaml"),
        ("./a.yaml#/", "./a.yaml"),
        ("./a.yaml#", "./a.yaml#/"),
    ],
)
def test_e23_spellings_of_one_whole_file_ref_are_still_a_cycle(
    tmp_path: Path, inner: str, first: str
) -> None:
    a = _write(tmp_path / "a.yaml", _dump({"x": {"$ref": inner}}))
    path = _contract(tmp_path, _dump({"r": {"$ref": first}}))

    started = time.monotonic()
    _ref_error(lambda: load_contract(path), file=a, pointer="/x", has=("Circular",))

    assert time.monotonic() - started < 2


@pytest.mark.slow
def test_e24_five_thousand_distinct_fragments_compose_within_seconds(tmp_path: Path) -> None:
    count = 5_000
    for i in range(count):
        _write(tmp_path / f"f{i}.yaml", f"k: {i}\n")
    path = _contract(
        tmp_path,
        "extensions:\n" + "".join(f"  e{i}: {{$ref: ./f{i}.yaml}}\n" for i in range(count)),
    )

    started = time.monotonic()
    contract = load_contract(path)
    elapsed = time.monotonic() - started

    assert contract["extensions"]["e4999"] == {"k": 4999} and len(contract["extensions"]) == count
    # ~4 s on an idle machine; the bound only guards against a quadratic blow-up
    assert elapsed < 60, f"{elapsed:.1f}s for {count} small fragments"


def test_e25_an_overlay_fragment_that_refs_itself_is_a_cycle(tmp_path: Path) -> None:
    fragment = _write(tmp_path / "frag" / "ov.yaml", "k: {$ref: ./ov.yaml}\n")
    _write(tmp_path / "overlays" / "prod.yaml", "extensions: {x: {$ref: ../frag/ov.yaml}}\n")
    path = _contract(tmp_path, "id: x\nextensions: {}\n")

    _ref_error(
        lambda: load_with_overlay(path, "prod"), file=fragment, pointer="/k", has=("Circular",)
    )
    assert load_with_overlay(path, None) == {"id": "x", "extensions": {}}


# ── E-26: the commands ──────────────────────────────────────────────────

#: A case writes its fragments under ``root`` and returns the ``extensions`` of the contract
#: and the caps (module constants of ``contract_refs``) it needs patched.
_Built = Tuple[Dict[str, Any], Dict[str, int]]
_Case = Callable[[Path], _Built]


def _case_file_cycle(root: Path) -> _Built:
    _write(root / "a.yaml", "x: {$ref: ./a.yaml}\n")
    return {"r": {"$ref": "./a.yaml"}}, {}


def _case_two_file_cycle(root: Path) -> _Built:
    _write(root / "a.yaml", "x: {$ref: ./b.yaml}\n")
    _write(root / "b.yaml", "y: {$ref: ./a.yaml}\n")
    return {"r": {"$ref": "./a.yaml"}}, {}


def _case_pointer_cycle(root: Path) -> _Built:
    return {"a": {"$ref": "#/extensions/b"}, "b": {"$ref": "#/extensions/a"}}, {}


def _case_whole_document_pointer(root: Path) -> _Built:
    return {"x": {"$ref": "#"}}, {}


def _case_depth(root: Path) -> _Built:
    for i in range(21):
        _write(root / f"f{i}.yaml", f"next: {{$ref: ./f{i + 1}.yaml}}\n")
    _write(root / "f21.yaml", "end: true\n")
    return {"r": {"$ref": "./f0.yaml"}}, {}


def _case_file_cap(root: Path) -> _Built:
    _fan_in_bomb(root, 7, "[1, 2, 3, 4]")
    return {"r": {"$ref": "./l7.yaml"}}, {"MAX_REF_EXPANSION_NODES": 200}


def _case_pointer_cap(root: Path) -> _Built:
    ext: Dict[str, Any] = {"l0": [1, 2, 3, 4]}
    for n in range(1, 12):
        ext[f"l{n}"] = [{"$ref": f"#/extensions/l{n - 1}"}, {"$ref": f"#/extensions/l{n - 1}"}]
    return ext, {"MAX_REF_EXPANSION_NODES": 500}


_E26_CASES: Dict[str, _Case] = {
    "file-cycle": _case_file_cycle,
    "two-file-cycle": _case_two_file_cycle,
    "pointer-cycle": _case_pointer_cycle,
    "whole-document-pointer": _case_whole_document_pointer,
    "depth": _case_depth,
    "file-bomb-cap": _case_file_cap,
    "pointer-bomb-cap": _case_pointer_cap,
}


@pytest.mark.parametrize("case", sorted(_E26_CASES))
def test_e26_validate_and_bundle_end_with_a_ref_error_and_never_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
    case: str,
) -> None:
    """validate exits 1 (``contract_load_failed``), bundle exits 2 and writes nothing, for
    every cycle, depth and cap error; none ends in the CLI's unhandled-exception path.
    (Slice 5 owns the wording; this pins the codes and that nothing crashes.)"""
    monkeypatch.chdir(tmp_path)
    extensions, caps = _E26_CASES[case](tmp_path)
    for name, value in caps.items():
        monkeypatch.setattr(contract_refs, name, value)
    path = _contract(tmp_path, _min_with_extensions(extensions))
    out = tmp_path / "bundle.yaml"

    started = time.monotonic()
    validated = _cli("validate", str(path))
    seen_validate = capsys.readouterr()
    bundled = _cli("bundle", str(path), "--out", str(out))
    seen_bundle = capsys.readouterr()

    assert (validated, bundled) == (1, 2)
    assert "contract_load_failed" in _squash(seen_validate.out + seen_validate.err)
    assert not out.exists()
    for seen in (seen_validate, seen_bundle):
        text = seen.out + seen.err
        assert "cli_unhandled_exception" not in text
        assert "Unexpected error" not in text and "Traceback" not in text
    assert time.monotonic() - started < 30
