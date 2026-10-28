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

"""``fluid split``: scenario area I of the fragment catalog, plus C-24.

The invariant behind every case: a contract and its split tree give the same
``fluid bundle --out`` file byte for byte (and compose to equal documents) in
every environment, and splitting the result again changes nothing. ``Pair``
builds the flat contract and its copy to split; ``split_and_check`` runs the
split on the copy and asserts that invariant.

Findings are kept as ``xfail(strict=True)`` tests that assert the behaviour
the catalog asks for; characterisation tests pin today's behaviour where the
catalog has no oracle (SPEC GAP SG-32, SG-18).
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import random
import re
import shutil
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, NamedTuple, Optional, Sequence, Tuple, Union

import pytest
import yaml

from fluid_build import loader
from fluid_build.loader import compose_contract

REPO = Path(__file__).resolve().parents[2]
ALL_PARTS = "sovereignty,accessPolicy,builds,exposes,sql,quality,schema,extensions,description"
ENVS: Tuple[Optional[str], ...] = (None, "dev", "prod")
CLI_ENVS: Tuple[Optional[str], ...] = (None, "prod")
NAME = "contract.fluid.yaml"

# ── fixtures: the contract every area-I case is built from ──────────────

#: SPLIT-ALL: a hand-written 0.7.6 contract with a comment in every place a
#: split can touch (header, kept keys, inside kept blocks, above and inside
#: moved sections, on the moved key, between items, end of file). No YAML
#: alias: a root with one cannot be spliced (see ``SPLIT_ALL_ALIAS``).
SPLIT_ALL = """\
# ===== Orders data product (header comment) =====
# second header line

fluidVersion: "0.7.6"   # quoted scalar, kept verbatim
kind: DataProduct
id: commerce.orders
name: Orders            # trailing comment on a kept key
description:
  purpose: One row per order.   # trailing on purpose
  usage: |
    Join on `order_id`.
    Amounts are in EUR.
  limitations: "no"
domain: commerce
tags: [orders, "no", core]      # a flow list
metadata:
  # comment inside a kept block
  layer: Silver
  owner: {team: commerce-data, email: commerce@example.com}   # a flow mapping

# comment directly above a moved section
sovereignty:  # ours (inline on the moved key)
  # comment above the first key of the moved section
  jurisdiction: EU          # trailing comment inside the moved section
  # comment between keys of the moved section
  allowedRegions: [europe-west1, europe-west4]
  dataResidency: true       # trailing comment on its last line

# between sovereignty and accessPolicy
accessPolicy:
  grants:
    - principal: "group:analysts@example.com"
      permissions: [read, select]
    # comment between grants
    - principal: "group:stewards@example.com"
      permissions: [read]

builds:  # ours
  # above the first build
  - id: ingest.orders
    pattern: embedded-logic
    engine: sql
    properties:
      sql: |
        -- keeps its trailing newline
        SELECT * FROM raw.orders
      parameters:
        mode: full

  # between builds
  - id: rollup
    pattern: embedded-logic
    engine: sql
    properties:
      sql: |-
        SELECT order_id, SUM(amount) AS total
        FROM ingest_orders
        GROUP BY 1
  # after the last build
exposes:
  - exposeId: orders
    kind: table
    version: "1.0.0"
    binding: {platform: local, format: csv, location: {path: runtime/out/orders.csv}}
    contract:
      schema:
        - {name: order_id, type: string, required: true}
        - name: amount   # block-style item
          type: number
      quality:
        - {rule: id_present, expression: order_id IS NOT NULL, severity: error}
      dq:
        rules:
          - id: amount_complete
            type: completeness
            selector: amount
            threshold: 1.0
            operator: ">="
            severity: error
  - exposeId: order_totals
    kind: table
    version: "1.0.0"
    binding: {platform: local, format: csv, location: {path: runtime/out/totals.csv}}
    contract:
      schema:
        - {name: order_id, type: string}
        - {name: total, type: number}
extensions:
  acme:
    region: eu
    # comment inside the extension
    tiers: [gold, silver]
# end-of-file comment
"""

#: The prod overlay lists its entries in the reverse order of the base, so the
#: keyed merge (by build id and exposeId) is what makes it come out right.
PROD_OVERLAY = """\
# prod overlay: entries listed in reverse order on purpose
exposes:
  - exposeId: order_totals
    binding:
      location:
        path: runtime/out/totals_prod.csv
  - exposeId: orders
    binding:
      location:
        path: runtime/out/orders_prod.csv
builds:
  - id: rollup
    properties:
      parameters:
        mode: prod
  - id: ingest.orders
    properties:
      parameters:
        mode: incremental
"""

SPLIT_ALL_FILES: Dict[str, str] = {NAME: SPLIT_ALL, "overlays/prod.yaml": PROD_OVERLAY}

#: What a default split of SPLIT-ALL creates: the 0.24.0 layout.
DEFAULT_LAYOUT = [
    "fragments/access-policy.yaml",
    "fragments/builds/ingest-orders.yaml",
    "fragments/builds/rollup.yaml",
    "fragments/exposes/order_totals.yaml",
    "fragments/exposes/orders.yaml",
    "fragments/sovereignty.yaml",
]

#: The files each part alone creates for SPLIT-ALL.
PART_FILES: Dict[str, List[str]] = {
    "sovereignty": ["fragments/sovereignty.yaml"],
    "accessPolicy": ["fragments/access-policy.yaml"],
    "builds": ["fragments/builds/ingest-orders.yaml", "fragments/builds/rollup.yaml"],
    "exposes": ["fragments/exposes/order_totals.yaml", "fragments/exposes/orders.yaml"],
    "sql": ["sql/ingest-orders.sql", "sql/rollup.sql"],
    "quality": ["fragments/quality/orders.yaml"],
    "schema": ["fragments/schemas/order_totals.yaml", "fragments/schemas/orders.yaml"],
    "extensions": ["fragments/extensions/acme.yaml"],
    "extensions.acme": ["fragments/extensions/acme.yaml"],
    "description": ["docs/limitations.md", "docs/purpose.md", "docs/usage.md"],
}

#: Comment lines ``fluid split`` writes at the top of a fragment itself.
_GENERATED_HEADERS = (
    "Data sovereignty rules.",
    "Access policy",
    "Build: ",
    "Expose: ",
    "Columns: ",
    "Quality rules: ",
    "Extension: ",
)


# ── helpers ─────────────────────────────────────────────────────────────


def write(path: Path, content: Union[str, bytes]) -> Path:
    """Write ``content`` exactly (no newline translation)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode("utf-8") if isinstance(content, str) else content)
    return path


def files_of(root: Path) -> Dict[str, bytes]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def entries_of(root: Path) -> List[str]:
    """Every file and directory under ``root`` (directories end in ``/``)."""
    return sorted(
        p.relative_to(root).as_posix() + ("/" if p.is_dir() else "") for p in root.rglob("*")
    )


def digest(root: Path) -> str:
    """sha256 over the whole tree: every entry's name and every file's bytes."""
    h = hashlib.sha256()
    for entry in entries_of(root):
        h.update(entry.encode("utf-8") + b"\0")
        path = root / entry
        if path.is_file():
            h.update(hashlib.sha256(path.read_bytes()).digest())
    return h.hexdigest()


def run_cli(*argv: Any) -> int:
    from fluid_build.cli import main

    try:
        return main([str(arg) for arg in argv])
    except SystemExit as exc:  # argparse: --help, usage errors
        return exc.code if isinstance(exc.code, int) else 0


def split(contract: Path, *args: Any) -> int:
    return run_cli("split", contract, *args)


def bundle(contract: Path, out: Path, env: Optional[str] = None) -> int:
    args: List[Any] = ["bundle", contract, "--out", out]
    if env:
        args += ["--env", env]
    return run_cli(*args)


def has_ref_key(node: Any) -> bool:
    """True if any mapping under ``node`` has a ``$ref`` KEY (text may mention it)."""
    stack = [node]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            if "$ref" in item:
                return True
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return False


class Pair(NamedTuple):
    """The same files twice: ``flat`` stays as written, ``split`` is split in place."""

    flat: Path
    split: Path
    name: str

    @property
    def flat_contract(self) -> Path:
        return self.flat / self.name

    @property
    def split_contract(self) -> Path:
        return self.split / self.name


def make_pair(tmp_path: Path, files: Mapping[str, Union[str, bytes]], name: str = NAME) -> Pair:
    pair = Pair(tmp_path / "flat", tmp_path / "split", name)
    for side in (pair.flat, pair.split):
        for rel, content in files.items():
            write(side / rel, content)
    return pair


def solo(tmp_path: Path, text: Union[str, bytes], name: str = NAME) -> Pair:
    """A pair whose only file is the contract ``text``."""
    return make_pair(tmp_path, {name: text}, name)


def assert_same_bundle(
    pair: Pair,
    envs: Sequence[Optional[str]] = (None,),
    cli_envs: Optional[Sequence[Optional[str]]] = None,
) -> None:
    """The flat and the split tree compose to the same contract in every env.

    For the envs in ``cli_envs`` (default: all of ``envs``) the proof is the
    strong one: ``fluid bundle --out`` writes the same file, byte for byte.
    The others are compared as composed documents (each CLI call costs ~0.2 s).
    """
    out = pair.flat.parent
    cli_envs = envs if cli_envs is None else cli_envs
    for env in envs:
        tag = env or "base"
        flat_doc = compose_contract(pair.flat_contract, env).document
        assert compose_contract(pair.split_contract, env).document == flat_doc
        assert not has_ref_key(flat_doc)
        if env not in cli_envs:
            continue
        assert bundle(pair.flat_contract, out / f"flat-{tag}.out", env) == 0
        assert bundle(pair.split_contract, out / f"split-{tag}.out", env) == 0
        flat = (out / f"flat-{tag}.out").read_bytes()
        got = (out / f"split-{tag}.out").read_bytes()
        assert got == flat, f"bundle --env {env} differs between the flat and the split tree"
        assert yaml.safe_load(got) == yaml.safe_load(flat)
        assert not has_ref_key(yaml.safe_load(got))


def split_and_check(
    pair: Pair,
    parts: Optional[str] = None,
    envs: Sequence[Optional[str]] = (None,),
    *,
    cli_envs: Optional[Sequence[Optional[str]]] = None,
    again: bool = True,
) -> Dict[str, bytes]:
    """Split ``pair.split`` in place, assert the invariant, return its files.

    Also asserts that splitting the result again, with the same arguments,
    exits 0 and changes no byte of the tree.
    """
    args = ["--parts", parts] if parts is not None else []
    assert split(pair.split_contract, *args) == 0
    assert_same_bundle(pair, envs, cli_envs)
    after = files_of(pair.split)
    if again:
        before = digest(pair.split)
        assert split(pair.split_contract, *args) == 0
        assert digest(pair.split) == before
    return after


def new_files(pair: Pair, after: Mapping[str, bytes]) -> List[str]:
    """Files the split created (the ones the flat tree does not have)."""
    return sorted(set(after) - set(files_of(pair.flat)))


def comments(text: str) -> List[str]:
    """The text of every ``#`` comment in ``text``, generated headers left out."""
    found = []
    for line in text.splitlines():
        match = re.search(r"(?:^|\s)#\s?(.*)$", line)
        if match and not match.group(1).startswith(_GENERATED_HEADERS):
            found.append(match.group(1).strip())
    return found


def assert_comments_conserved(original: str, after: Mapping[str, bytes]) -> None:
    """Every comment of ``original`` is in the root or a fragment, exactly once."""
    kept = []
    for rel, content in after.items():
        if rel.endswith((".yaml", ".yml", ".json")) and not rel.startswith("overlays/"):
            kept += comments(content.decode("utf-8"))
    assert sorted(kept) == sorted(comments(original))


def assert_lines_conserved(original: str, root: str, fragments: Iterable[str]) -> None:
    """The root differs from ``original`` only where a part moved out.

    Every changed line of the root is a ``$ref`` line, and every line that
    left the root is in some fragment (compared stripped, bullet dash dropped).
    """
    old, new = original.splitlines(), root.splitlines()
    left: List[str] = []
    matcher = difflib.SequenceMatcher(a=old, b=new, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        left += old[i1:i2]
        assert all("$ref" in line for line in new[j1:j2]), new[j1:j2]
    pool = Counter(
        re.sub(r"^(?:-\s+)+", "", line.strip()) for text in fragments for line in text.splitlines()
    )
    for line in left:
        key = re.sub(r"^(?:-\s+)+", "", line.strip())
        if key:
            assert pool[key] > 0, f"line {line!r} left the root and is in no fragment"
            pool[key] -= 1


def text_of(after: Mapping[str, bytes], rel: str) -> str:
    return after[rel].decode("utf-8")


def _clear_warn_once() -> None:
    for name, value in list(vars(loader).items()):
        if name.startswith("_NOTED_") and isinstance(value, set):
            value.clear()


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    """Process-wide warn-once sets cleared; no ambient ref root; cwd is the test dir."""
    _clear_warn_once()
    monkeypatch.delenv("FLUID_REF_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    yield
    _clear_warn_once()


# ── the 0.24.0 splitter, kept as the oracle for the dict-level function ─


def split_0_24_0(contract: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, str]]:
    """``split_contract_to_fragments`` as v0.24.0 wrote it (before ``--parts``)."""
    import copy

    def slugify(value: Any) -> str:
        slug = re.sub(r"[^a-zA-Z0-9_-]", "-", str(value).strip())
        slug = re.sub(r"-{2,}", "-", slug).strip("-")
        return slug.lower() or "unnamed"

    sections = {
        "sovereignty": ("fragments/sovereignty.yaml", "# Data sovereignty rules.\n"),
        "accessPolicy": (
            "fragments/access-policy.yaml",
            "# Access policy — defines who can read and write the data product.\n",
        ),
    }
    lists = {
        "builds": ("fragments/builds", "id", "# Build: "),
        "exposes": ("fragments/exposes", "exposeId", "# Expose: "),
    }
    if not isinstance(contract, dict):
        return contract, {}
    root = copy.deepcopy(contract)
    fragments: Dict[str, str] = {}
    dump = dict(default_flow_style=False, sort_keys=False, allow_unicode=True)
    for key, (rel_path, header) in sections.items():
        value = root.get(key)
        if not isinstance(value, dict) or not value:
            continue
        fragments[rel_path] = header + "\n" + yaml.dump(value, **dump)
        root[key] = {"$ref": f"./{rel_path}"}
    for key, (directory, id_field, prefix) in lists.items():
        items = root.get(key)
        if not items or not isinstance(items, list):
            continue
        refs: List[Any] = []
        for item in items:
            if not isinstance(item, dict):
                refs.append(item)
                continue
            item_id = item.get(id_field, "")
            slug = slugify(item_id) if item_id else f"unnamed-{len(refs)}"
            rel_path = f"{directory}/{slug}.yaml"
            fragments[rel_path] = f"{prefix}{item_id or slug}\n" + "\n" + yaml.dump(item, **dump)
            refs.append({"$ref": f"./{rel_path}"})
        root[key] = refs
    return root, fragments


# ── the catalog's invariant, first ──────────────────────────────────────


def test_i01_fluid_build_is_the_checkout_under_test() -> None:
    assert Path(loader.__file__).resolve().is_relative_to(REPO)


def test_i01_default_split_writes_the_0_24_0_paths_and_data(tmp_path: Path) -> None:
    pair = make_pair(tmp_path, SPLIT_ALL_FILES)
    before = files_of(pair.split)

    after = split_and_check(pair, envs=ENVS)

    assert sorted(set(after) - set(before)) == DEFAULT_LAYOUT
    assert {k for k in before if after[k] != before[k]} == {NAME}
    # Overlays stay where they were, byte for byte.
    assert after["overlays/prod.yaml"] == before["overlays/prod.yaml"]
    # The same data as the 0.24.0 splitter wrote for these fragments.
    oracle_root, oracle = split_0_24_0(yaml.safe_load(SPLIT_ALL))
    assert sorted(oracle) == DEFAULT_LAYOUT
    for rel, content in oracle.items():
        assert yaml.safe_load(text_of(after, rel)) == yaml.safe_load(content), rel
    assert yaml.safe_load(text_of(after, NAME)) == oracle_root


def test_i01_the_split_root_refers_to_the_fragments_the_way_0_24_0_did(tmp_path: Path) -> None:
    pair = make_pair(tmp_path, SPLIT_ALL_FILES)

    after = split_and_check(pair)

    root = yaml.safe_load(text_of(after, NAME))
    assert root["sovereignty"] == {"$ref": "./fragments/sovereignty.yaml"}
    assert root["accessPolicy"] == {"$ref": "./fragments/access-policy.yaml"}
    assert root["builds"] == [
        {"$ref": "./fragments/builds/ingest-orders.yaml"},
        {"$ref": "./fragments/builds/rollup.yaml"},
    ]
    assert root["exposes"] == [
        {"$ref": "./fragments/exposes/orders.yaml"},
        {"$ref": "./fragments/exposes/order_totals.yaml"},
    ]
    # Fragments keep the header 0.24.0 wrote above them.
    assert text_of(after, "fragments/builds/rollup.yaml").startswith(
        "# Build: rollup\n\nid: rollup\n"
    )
    assert text_of(after, "fragments/sovereignty.yaml").startswith("# Data sovereignty rules.\n\n")


@pytest.mark.parametrize("part", sorted(PART_FILES))
def test_i01_every_part_alone_moves_its_files_and_keeps_the_bundle(
    tmp_path: Path, part: str
) -> None:
    pair = make_pair(tmp_path, SPLIT_ALL_FILES)

    after = split_and_check(pair, part, envs=ENVS, cli_envs=CLI_ENVS)

    assert new_files(pair, after) == PART_FILES[part]
    assert after[NAME] != SPLIT_ALL.encode("utf-8")


def test_i01_all_parts_together_keep_the_bundle_in_every_env(tmp_path: Path) -> None:
    pair = make_pair(tmp_path, SPLIT_ALL_FILES)

    after = split_and_check(pair, ALL_PARTS, envs=ENVS)

    everything = sorted({f for files in PART_FILES.values() for f in files})
    assert new_files(pair, after) == everything
    assert_comments_conserved(SPLIT_ALL, after)


def test_i01_the_split_tree_validates_as_the_flat_contract_does(tmp_path: Path) -> None:
    pair = make_pair(tmp_path, SPLIT_ALL_FILES)
    assert run_cli("validate", pair.flat_contract) == 0

    split_and_check(pair, ALL_PARTS)

    assert run_cli("validate", pair.split_contract) == 0
    assert run_cli("validate", pair.split_contract, "--env", "prod") == 0


# ── I-02 / I-03: what happens to the comments ───────────────────────────


def test_i02_every_comment_and_untouched_line_of_the_root_is_kept(tmp_path: Path) -> None:
    pair = make_pair(tmp_path, SPLIT_ALL_FILES)

    after = split_and_check(pair)

    root = text_of(after, NAME)
    fragments = [text_of(after, rel) for rel in DEFAULT_LAYOUT]
    assert_lines_conserved(SPLIT_ALL, root, fragments)
    assert_comments_conserved(SPLIT_ALL, after)
    # Untouched lines stay as written: quotes, flow style, block scalars, order.
    for kept in (
        "# ===== Orders data product (header comment) =====\n# second header line\n\n",
        'fluidVersion: "0.7.6"   # quoted scalar, kept verbatim\n',
        "name: Orders            # trailing comment on a kept key\n",
        "  usage: |\n    Join on `order_id`.\n    Amounts are in EUR.\n",
        '  limitations: "no"\n',
        'tags: [orders, "no", core]      # a flow list\n',
        "  # comment inside a kept block\n",
        "  owner: {team: commerce-data, email: commerce@example.com}   # a flow mapping\n",
        "# comment directly above a moved section\nsovereignty:  # ours (inline on the moved key)\n",
        "# between sovereignty and accessPolicy\naccessPolicy:\n",
        "builds:  # ours\n  # above the first build\n",
        "\n  # between builds\n",
        "  # after the last build\nexposes:\n",
        "    # comment inside the extension\n    tiers: [gold, silver]\n# end-of-file comment\n",
    ):
        assert kept in root, kept
    # Key order is the original's.
    assert [m.group(1) for m in re.finditer(r"^([A-Za-z]+):", root, re.M)] == [
        m.group(1) for m in re.finditer(r"^([A-Za-z]+):", SPLIT_ALL, re.M)
    ]


@pytest.mark.parametrize("part", ["builds", "exposes", "sovereignty", "accessPolicy", "extensions"])
def test_i02_each_part_leaves_every_other_line_of_the_root_alone(tmp_path: Path, part: str) -> None:
    pair = make_pair(tmp_path, SPLIT_ALL_FILES)

    after = split_and_check(pair, part)

    fragments = [text_of(after, rel) for rel in PART_FILES[part]]
    assert_lines_conserved(SPLIT_ALL, text_of(after, NAME), fragments)
    assert_comments_conserved(SPLIT_ALL, after)


def test_i03_a_comment_inside_a_moved_part_goes_to_the_fragment_not_nowhere(
    tmp_path: Path,
) -> None:
    """SG-32: where the comments of a moved part go. Today: with the part."""
    pair = make_pair(tmp_path, SPLIT_ALL_FILES)

    after = split_and_check(pair)

    sovereignty = text_of(after, "fragments/sovereignty.yaml")
    assert "# trailing comment inside the moved section" in sovereignty
    assert "# comment between keys of the moved section" in sovereignty
    assert "# trailing comment on its last line" in sovereignty
    assert "# comment between grants" in text_of(after, "fragments/access-policy.yaml")
    assert "# block-style item" in text_of(after, "fragments/exposes/orders.yaml")
    # Each comment exists once: moved, never copied.
    assert_comments_conserved(SPLIT_ALL, after)


def test_i03_a_comment_above_the_first_key_of_a_moved_section_stays_in_the_root(
    tmp_path: Path,
) -> None:
    """SG-32 characterisation: the comment sits above the part's first key, so
    it is outside the part's text and stays in the root, before the ``$ref``."""
    pair = make_pair(tmp_path, SPLIT_ALL_FILES)

    after = split_and_check(pair, "sovereignty")

    root = text_of(after, NAME)
    assert (
        "sovereignty:  # ours (inline on the moved key)\n"
        "  # comment above the first key of the moved section\n"
        "  $ref: ./fragments/sovereignty.yaml\n"
    ) in root
    assert "# comment above the first key" not in text_of(after, "fragments/sovereignty.yaml")


@pytest.mark.parametrize("parts", [None, ALL_PARTS])
def test_i02_a_comment_on_the_key_of_every_moved_section_stays_on_that_key(
    tmp_path: Path, parts: Optional[str]
) -> None:
    text = """\
id: x
sovereignty:  # c1
  jurisdiction: EU
accessPolicy:  # c2
  grants: []
builds:  # c3
  - id: a
    engine: sql
exposes:  # c4
  - exposeId: o
    kind: table
extensions:  # c5
  acme:  # c6
    a: 1
"""
    pair = solo(tmp_path, text)

    after = split_and_check(pair, parts)

    root = text_of(after, NAME)
    for line in (
        "sovereignty:  # c1\n",
        "accessPolicy:  # c2\n",
        "builds:  # c3\n",
        "exposes:  # c4\n",
        "extensions:  # c5\n",
    ):
        assert line in root
    if parts:
        assert "  acme:  # c6\n    $ref: ./fragments/extensions/acme.yaml\n" in root
    assert_comments_conserved(text, after)


# ── I-04: idempotence ───────────────────────────────────────────────────


def test_i04_splitting_the_output_again_changes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pair = make_pair(tmp_path, SPLIT_ALL_FILES)
    assert split(pair.split_contract) == 0
    first = digest(pair.split)
    sovereignty = (pair.split / "fragments/sovereignty.yaml").read_bytes()
    capsys.readouterr()

    assert split(pair.split_contract) == 0

    assert digest(pair.split) == first
    assert "Nothing to split" in capsys.readouterr().err
    assert (pair.split / "fragments/sovereignty.yaml").read_bytes() == sovereignty
    assert b"$ref" not in sovereignty  # not the self-referencing file 0.24.0 wrote
    assert not [p for p in files_of(pair.split) if "unnamed" in p]
    assert_same_bundle(pair, ENVS)


@pytest.mark.parametrize("parts", [ALL_PARTS, "builds,exposes,sql", "sql,quality,schema"])
def test_i04_a_second_run_with_other_parts_never_rewrites_the_first_run(
    tmp_path: Path, parts: str
) -> None:
    pair = make_pair(tmp_path, SPLIT_ALL_FILES)
    assert split(pair.split_contract) == 0
    first = files_of(pair.split)

    assert split(pair.split_contract, "--parts", parts) == 0

    # What the first run moved sits in fragments the root only refers to: no
    # file it wrote is touched, no self-referencing or unnamed fragment appears.
    after = files_of(pair.split)
    for rel, content in first.items():
        if rel != NAME:
            assert after[rel] == content, rel
    assert not [p for p in after if "unnamed" in p]
    assert_same_bundle(pair, ENVS)
    if parts != ALL_PARTS:
        assert after == first  # nothing left in the root for these parts


def test_i04_the_dict_level_splitter_skips_what_is_already_a_ref() -> None:
    from fluid_build.cli.forge_contract_fragments import split_contract_to_fragments

    root, files = split_contract_to_fragments(yaml.safe_load(SPLIT_ALL))
    again_root, again_files = split_contract_to_fragments(root)

    assert again_files == {}
    assert again_root == root
    assert sorted(files) == DEFAULT_LAYOUT


# ── I-05..I-09: what is already there, and what the names come out as ───


def test_i05_only_the_inline_build_moves_and_the_existing_ref_stays(tmp_path: Path) -> None:
    text = """\
id: x
builds:
  - $ref: ./fragments/builds/done.yaml
  - id: fresh
    pattern: embedded-logic
    engine: sql
    properties: {sql: "SELECT 1"}
"""
    done = "# mine\nid: done\nengine: sql\n"
    pair = make_pair(tmp_path, {NAME: text, "fragments/builds/done.yaml": done})

    after = split_and_check(pair)

    assert new_files(pair, after) == ["fragments/builds/fresh.yaml"]
    assert text_of(after, NAME).startswith(
        "id: x\nbuilds:\n  - $ref: ./fragments/builds/done.yaml\n"
    )
    assert after["fragments/builds/done.yaml"] == done.encode("utf-8")
    assert yaml.safe_load(text_of(after, NAME))["builds"][1] == {
        "$ref": "./fragments/builds/fresh.yaml"
    }


def test_i06_a_fragment_that_exists_keeps_its_bytes_and_the_new_one_gets_a_free_name(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    mine = b"# mine, with different bytes\nkeep: me\n"
    text = "id: x\nbuilds:\n  - id: transform\n    engine: sql\n"
    pair = make_pair(tmp_path, {NAME: text, "fragments/builds/transform.yaml": mine})
    # The existing file is not what the flat contract composes from, so the
    # flat side holds the same build inline.
    capsys.readouterr()

    assert split(pair.split_contract) == 0

    err = capsys.readouterr().err
    after = files_of(pair.split)
    assert after["fragments/builds/transform.yaml"] == mine
    assert text_of(after, "fragments/builds/transform-2.yaml").endswith(
        "id: transform\nengine: sql\n"
    )
    assert "fragments/builds/transform-2.yaml" in err  # the message says which file it wrote
    assert "$ref: ./fragments/builds/transform-2.yaml" in text_of(after, NAME)
    assert_same_bundle(pair)


def test_i06_every_target_that_exists_is_skipped_not_just_the_first(tmp_path: Path) -> None:
    files: Dict[str, Union[str, bytes]] = {
        NAME: "id: x\nbuilds:\n  - id: t\n    properties: {sql: SELECT 1}\n",
        "fragments/builds/t.yaml": "a: 1\n",
        "fragments/builds/t-2.yaml": "b: 2\n",
        "sql/t.sql": "-- mine\n",
    }
    pair = make_pair(tmp_path, files)

    after = split_and_check(pair, "builds,sql")

    assert new_files(pair, after) == ["fragments/builds/t-3.yaml", "sql/t-2.sql"]
    for rel in ("fragments/builds/t.yaml", "fragments/builds/t-2.yaml", "sql/t.sql"):
        assert after[rel] == str(files[rel]).encode("utf-8")


CLASHING_IDS = """\
id: x
builds:
  - id: ingest.orders
    engine: sql
    properties: {sql: "SELECT 1"}
  - id: ingest-orders
    engine: sql
    properties: {sql: "SELECT 2"}
  - id: Ingest_Orders
    engine: sql
exposes:
  - exposeId: Customer_Data
    kind: table
  - exposeId: customer_data
    kind: table
  - exposeId: CUSTOMER_DATA
    kind: table
"""


def test_i07_ids_that_make_one_slug_get_distinct_files_in_document_order(tmp_path: Path) -> None:
    pair = solo(tmp_path, CLASHING_IDS)

    after = split_and_check(pair, "builds,exposes,sql")

    assert new_files(pair, after) == [
        "fragments/builds/ingest-orders-2.yaml",
        "fragments/builds/ingest-orders.yaml",
        "fragments/builds/ingest_orders.yaml",
        "fragments/exposes/customer_data-2.yaml",
        "fragments/exposes/customer_data-3.yaml",
        "fragments/exposes/customer_data.yaml",
        "sql/ingest-orders-2.sql",
        "sql/ingest-orders.sql",
    ]
    # The first id of a slug gets the plain name; the next ones -2, -3.
    ids = {}
    for rel in new_files(pair, after):
        if rel.startswith("fragments/"):
            doc = yaml.safe_load(text_of(after, rel))
            ids[rel] = doc.get("id") or doc.get("exposeId")
    assert ids["fragments/builds/ingest-orders.yaml"] == "ingest.orders"
    assert ids["fragments/builds/ingest-orders-2.yaml"] == "ingest-orders"
    assert ids["fragments/exposes/customer_data.yaml"] == "Customer_Data"
    assert ids["fragments/exposes/customer_data-2.yaml"] == "customer_data"
    assert ids["fragments/exposes/customer_data-3.yaml"] == "CUSTOMER_DATA"
    # Both items of every clash are still in the contract, in order.
    composed = compose_contract(pair.split_contract).document
    assert [b["id"] for b in composed["builds"]] == [
        "ingest.orders",
        "ingest-orders",
        "Ingest_Orders",
    ]
    assert [e["exposeId"] for e in composed["exposes"]] == [
        "Customer_Data",
        "customer_data",
        "CUSTOMER_DATA",
    ]


def test_i07_the_names_are_deterministic_across_runs(tmp_path: Path) -> None:
    first = make_pair(tmp_path / "one", {NAME: CLASHING_IDS})
    second = make_pair(tmp_path / "two", {NAME: CLASHING_IDS})

    assert split(first.split_contract, "--parts", ALL_PARTS) == 0
    assert split(second.split_contract, "--parts", ALL_PARTS) == 0

    assert files_of(first.split) == files_of(second.split)


def test_i08_two_non_ascii_ids_get_two_files(tmp_path: Path) -> None:
    text = (
        "id: x\nbuilds:\n  - id: 订单\n    engine: sql\n  - id: заказ\n    engine: sql\n"
        "exposes:\n  - exposeId: 订单\n    kind: table\n  - exposeId: заказ\n    kind: table\n"
    )
    pair = solo(tmp_path, text)

    after = split_and_check(pair)

    builds = [rel for rel in new_files(pair, after) if rel.startswith("fragments/builds/")]
    exposes = [rel for rel in new_files(pair, after) if rel.startswith("fragments/exposes/")]
    assert len(builds) == 2 and len(exposes) == 2
    assert {yaml.safe_load(text_of(after, rel))["id"] for rel in builds} == {"订单", "заказ"}
    assert {yaml.safe_load(text_of(after, rel))["exposeId"] for rel in exposes} == {"订单", "заказ"}
    # SG-32 characterisation: neither id has an ASCII letter, both slugs are "unnamed".
    assert builds == ["fragments/builds/unnamed-2.yaml", "fragments/builds/unnamed.yaml"]


ODD_IDS = [
    "a/b",
    "../x",
    "a\\b",
    "-lead",
    "con",
    "aux",
    "nul",
    "com1",
    "trail.",
    "trail ",
    "a:b",
    ".hidden",
    "..",
]


def _odd_id_contract(ids: Sequence[str]) -> str:
    return "id: x\nbuilds:\n" + "".join(f"  - id: {json.dumps(i)}\n    engine: sql\n" for i in ids)


def test_i09_ids_with_path_characters_land_under_fragments_builds(tmp_path: Path) -> None:
    pair = solo(tmp_path, _odd_id_contract(ODD_IDS))

    after = split_and_check(pair)

    created = new_files(pair, after)
    assert len(created) == len(ODD_IDS)  # one file per build, none overwritten
    base = (pair.split / "fragments" / "builds").resolve()
    for rel in created:
        assert rel.startswith("fragments/builds/") and rel.endswith(".yaml"), rel
        name = rel.rsplit("/", 1)[1]
        stem = name[: -len(".yaml")]
        assert "/" not in name and "\\" not in name
        assert not name.startswith(("-", ".")) and not stem.endswith((".", " "))
        assert len(name.encode("utf-8")) <= 255
        assert (pair.split / rel).resolve().parent == base
    composed = compose_contract(pair.split_contract).document
    assert [b["id"] for b in composed["builds"]] == ODD_IDS


def test_i09_windows_device_names_are_not_avoided_today(tmp_path: Path) -> None:
    """SG-32 characterisation: ``con.yaml``, ``aux.yaml`` and ``nul.yaml`` are
    reserved on Windows whatever the extension, and split writes them as they are."""
    pair = solo(tmp_path, _odd_id_contract(["con", "aux", "nul"]))

    after = split_and_check(pair)

    assert new_files(pair, after) == [
        "fragments/builds/aux.yaml",
        "fragments/builds/con.yaml",
        "fragments/builds/nul.yaml",
    ]


def test_i09_a_300_character_id_cannot_be_written_but_the_dry_run_says_it_can(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """SG-32 characterisation (finding SP-07): the slug is not shortened, so the
    file name is over 255 bytes; ``--dry-run`` lists it and exits 0, the real
    run fails with ENAMETOOLONG."""
    big = "a" * 300
    pair = solo(tmp_path, f"id: x\nbuilds:\n  - id: {big}\n    engine: sql\n")
    before = files_of(pair.split)

    assert split(pair.split_contract, "--dry-run") == 0
    assert big in capsys.readouterr().err
    assert split(pair.split_contract) == 1

    assert "File name too long" in capsys.readouterr().err
    assert files_of(pair.split) == before


# ── I-10..I-15: the sql part ────────────────────────────────────────────


def test_i10_sql_gets_a_file_per_build_relative_to_the_fragment_and_stays_exact(
    tmp_path: Path,
) -> None:
    pair = make_pair(tmp_path, SPLIT_ALL_FILES)

    after = split_and_check(pair, "builds,exposes,sql", envs=ENVS, cli_envs=CLI_ENVS)

    original = yaml.safe_load(SPLIT_ALL)["builds"]
    assert after["sql/ingest-orders.sql"] == original[0]["properties"]["sql"].encode("utf-8")
    assert after["sql/rollup.sql"] == original[1]["properties"]["sql"].encode("utf-8")
    assert after["sql/ingest-orders.sql"].endswith(b"raw.orders\n")  # `|` keeps one newline
    assert not after["sql/rollup.sql"].endswith(b"\n")  # `|-` keeps none
    for build_id in ("ingest-orders", "rollup"):
        fragment = text_of(after, f"fragments/builds/{build_id}.yaml")
        assert f"sql: {{$ref: ../../sql/{build_id}.sql}}" in fragment  # relative to the fragment
    composed = compose_contract(pair.split_contract).document["builds"]
    assert [b["properties"]["sql"] for b in composed] == [b["properties"]["sql"] for b in original]
    assert_comments_conserved(SPLIT_ALL, after)


def test_i11_sql_alone_leaves_the_builds_in_the_root_with_a_root_relative_ref(
    tmp_path: Path,
) -> None:
    pair = make_pair(tmp_path, SPLIT_ALL_FILES)

    after = split_and_check(pair, "sql", envs=ENVS, cli_envs=CLI_ENVS)

    root = text_of(after, NAME)
    builds = yaml.safe_load(root)["builds"]
    assert [b["id"] for b in builds] == ["ingest.orders", "rollup"]  # still inline
    assert builds[0]["properties"]["sql"] == {"$ref": "./sql/ingest-orders.sql"}
    assert builds[1]["properties"]["sql"] == {"$ref": "./sql/rollup.sql"}
    assert new_files(pair, after) == ["sql/ingest-orders.sql", "sql/rollup.sql"]
    # Only the sql scalars left the root, and a ref line took each one's place.
    gone = Counter(SPLIT_ALL.splitlines()) - Counter(root.splitlines())
    assert set(gone) == {
        "      sql: |",
        "        -- keeps its trailing newline",
        "        SELECT * FROM raw.orders",
        "      sql: |-",
        "        SELECT order_id, SUM(amount) AS total",
        "        FROM ingest_orders",
        "        GROUP BY 1",
    }
    assert all(
        "$ref" in line
        for line in set((Counter(root.splitlines()) - Counter(SPLIT_ALL.splitlines())))
    )
    assert_comments_conserved(SPLIT_ALL, after)


@pytest.mark.parametrize(
    "orderings",
    [
        ["sql,builds", "builds,sql", "builds, sql", "sql,builds,sql"],
        [
            "builds,exposes,sql,schema",
            "schema,sql,exposes,builds",
            "sql,schema,builds,exposes",
            "exposes,builds,schema,sql",
            "builds,builds,exposes,sql,schema,schema",
        ],
    ],
)
def test_i12_the_order_and_repeats_of_the_parts_never_change_the_output(
    tmp_path: Path, orderings: List[str]
) -> None:
    trees = []
    for n, parts in enumerate(orderings):
        pair = make_pair(tmp_path / f"run{n}", SPLIT_ALL_FILES)
        assert split(pair.split_contract, "--parts", parts) == 0
        trees.append(files_of(pair.split))

    assert all(tree == trees[0] for tree in trees[1:])


SQL_SHAPES = """\
id: x
builds:
  - id: crlf
    engine: sql
    properties: {sql: "SELECT 1\\r\\nFROM t"}
  - id: cr
    engine: sql
    properties: {sql: "SELECT 1\\rFROM t"}
  - id: bom
    engine: sql
    properties: {sql: "\\ufeffSELECT 1"}
  - id: empty
    engine: sql
    properties: {sql: ""}
  - id: ok
    engine: sql
    properties: {sql: "SELECT 1\\nFROM t\\n"}
"""


def test_i13_sql_a_text_file_cannot_give_back_stays_inline(tmp_path: Path) -> None:
    pair = solo(tmp_path, SQL_SHAPES)

    after = split_and_check(pair, "sql")

    # A ref reads a file's line ends as \n and drops a leading BOM, so those
    # three strings stay in the contract; an empty string has no file either.
    assert new_files(pair, after) == ["sql/ok.sql"]
    builds = yaml.safe_load(text_of(after, NAME))["builds"]
    assert [b["properties"]["sql"] for b in builds][:4] == [
        "SELECT 1\r\nFROM t",
        "SELECT 1\rFROM t",
        "﻿SELECT 1",
        "",
    ]
    assert builds[4]["properties"]["sql"] == {"$ref": "./sql/ok.sql"}


def test_i13_a_lone_surrogate_in_sql_refuses_the_split_and_keeps_the_root(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """SG-32 characterisation: the string has no UTF-8 file, split refuses."""
    text = 'id: x\nbuilds:\n  - id: t\n    engine: sql\n    properties: {sql: "SELECT \\ud800 1"}\n'
    pair = solo(tmp_path, text)
    root = pair.split_contract.read_bytes()

    assert split(pair.split_contract, "--parts", "sql") == 1

    assert "Split not written" in capsys.readouterr().err
    assert pair.split_contract.read_bytes() == root
    assert not [p for p, content in files_of(pair.split).items() if p != NAME and content]


def test_i13_sql_over_the_5_mib_text_cap_in_a_json_root_refuses_the_split(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """SG-32/SG-09: a root over the 5 MiB cap is refused when it is read, as every file
    the composition reads is, so split refuses it and changes nothing."""
    big = "x" * (5 * 1024 * 1024 + 10)
    contract = {"id": "x", "builds": [{"id": "t", "engine": "sql", "properties": {"sql": big}}]}
    pair = solo(tmp_path, json.dumps(contract), name="contract.fluid.json")
    root = pair.split_contract.read_bytes()

    assert split(pair.split_contract, "--parts", "sql") == 1

    err = capsys.readouterr().err
    assert "5242880-byte" in err and "cap" in err
    assert pair.split_contract.read_bytes() == root
    assert not [p for p in files_of(pair.split) if p != pair.name]


def test_i14_acquisition_hybrid_and_already_referenced_sql_are_skipped(tmp_path: Path) -> None:
    text = """\
id: x
builds:
  - id: ingest
    pattern: acquisition
    engine: dlt
    properties: {source: {kind: rest, url: "https://api.example.com"}}
  - id: dbt_model
    pattern: hybrid-reference
    engine: dbt
    properties: {model: orders}
  - id: pre
    pattern: embedded-logic
    engine: sql
    properties: {sql: {$ref: ./sql/pre.sql}}
  - id: real
    pattern: embedded-logic
    engine: sql
    properties: {sql: "SELECT 1\\n"}
"""
    pair = make_pair(tmp_path, {NAME: text, "sql/pre.sql": "-- pre-existing\nSELECT 0\n"})

    after = split_and_check(pair, "sql")

    assert new_files(pair, after) == ["sql/real.sql"]
    assert after["sql/pre.sql"] == b"-- pre-existing\nSELECT 0\n"
    builds = yaml.safe_load(text_of(after, NAME))["builds"]
    assert builds[2]["properties"]["sql"] == {"$ref": "./sql/pre.sql"}
    assert builds[0]["properties"] == {"source": {"kind": "rest", "url": "https://api.example.com"}}


def test_i15_code_builds_get_the_suffix_of_their_language(tmp_path: Path) -> None:
    """SG-32 characterisation of the file names; the invariant is what counts."""
    text = "id: x\nbuilds:\n" + "".join(
        f"  - id: {name}\n    engine: spark\n    properties:\n"
        f"      language: {language}\n      sql: |\n        print('{name}')\n"
        for name, language in [
            ("py", "python"),
            ("pys", "pyspark"),
            ("sc", "scala"),
            ("rr", "r"),
            ("rupper", "R"),
            ("fl", "flink_sql"),
            ("plain", "sql"),
        ]
    )
    pair = solo(tmp_path, text)

    after = split_and_check(pair, "sql")

    assert new_files(pair, after) == [
        "sql/fl.sql",
        "sql/plain.sql",
        "sql/py.py",
        "sql/pys.py",
        "sql/rr.r",
        "sql/rupper.r",
        "sql/sc.scala",
    ]
    for rel in new_files(pair, after):
        assert after[rel] == b"print('" + rel.split("/")[1].split(".")[0].encode() + b"')\n"


@pytest.mark.parametrize("indicator", ["|", "|-", "|+", ">", ">-", ">+"])
@pytest.mark.parametrize("parts", ["builds", "sql", "builds,sql", "builds,exposes,sql"])
def test_i10_every_block_scalar_chomping_survives_a_split(
    tmp_path: Path, indicator: str, parts: str
) -> None:
    """The scalar is followed by another key, so even a keep-chomped (`+`) one
    ends inside the moved text; the composed string is the original's."""
    text = (
        "id: x\nbuilds:\n"
        "  - id: a\n    engine: sql\n    properties:\n"
        f"      sql: {indicator}\n        SELECT 1\n          FROM t\n\n        WHERE x\n\n\n"
        "      parameters: {mode: full}\n"
        "  - id: b\n    engine: sql\n    properties:\n"
        f"      sql: {indicator}\n        SELECT 2\n\n"
        "      parameters: {mode: full}\n"
        "exposes:\n  - exposeId: o\n    kind: table\n"
        f"    description: {indicator}\n      text of o\n"
        "    title: after\n"
    )
    pair = solo(tmp_path, text)

    after = split_and_check(pair, parts)

    expected = yaml.safe_load(text)
    composed = compose_contract(pair.split_contract).document
    assert composed == expected
    assert [b["properties"]["sql"] for b in composed["builds"]] == [
        b["properties"]["sql"] for b in expected["builds"]
    ]
    assert after[NAME] != text.encode("utf-8")


@pytest.mark.parametrize("indicator", ["|+", ">+"])
@pytest.mark.parametrize(
    "tail",
    [
        # a keep-chomped scalar that ends a build, then the next build
        "builds:\n  - id: a\n    engine: sql\n    properties:\n      sql: {i}\n        SELECT 1\n\n\n  - id: b\n    engine: sql\n",
        # ... that ends the last item, before the next top-level key
        "builds:\n  - id: a\n    properties:\n      sql: {i}\n        SELECT 1\n\n\nexposes: []\n",
        # ... that ends a moved mapping section
        "sovereignty:\n  note: {i}\n    keep\n\n\nbuilds: []\n",
    ],
    ids=["build-then-build", "last-item-then-key", "section-then-key"],
)
@pytest.mark.xfail(
    strict=True,
    reason="BUG SP-04: a keep-chomped block scalar (|+, >+) that ends a moved part loses its "
    "trailing blank lines; split refuses ('does not compose to the original') instead of "
    "moving them with it or splitting the data",
)
def test_i10_a_keep_chomped_scalar_ending_a_moved_part_splits(
    tmp_path: Path, indicator: str, tail: str
) -> None:
    pair = solo(tmp_path, "id: x\n" + tail.replace("{i}", indicator))

    split_and_check(pair, "builds,sovereignty")


# ── I-16..I-20: quality, schema, extensions, description, part names ────


def test_i16_quality_moves_the_rules_of_the_contract_as_a_list_root_fragment(
    tmp_path: Path,
) -> None:
    """SG-32: only ``contract.quality`` moves; ``dq.rules`` stays where it is."""
    pair = make_pair(tmp_path, SPLIT_ALL_FILES)

    after = split_and_check(pair, "quality", envs=ENVS, cli_envs=CLI_ENVS)

    assert new_files(pair, after) == ["fragments/quality/orders.yaml"]
    rules = yaml.safe_load(text_of(after, "fragments/quality/orders.yaml"))
    assert rules == [
        {"rule": "id_present", "expression": "order_id IS NOT NULL", "severity": "error"}
    ]
    contract = yaml.safe_load(text_of(after, NAME))["exposes"][0]["contract"]
    assert contract["quality"] == {"$ref": "./fragments/quality/orders.yaml"}
    assert contract["dq"]["rules"][0]["id"] == "amount_complete"  # not moved
    assert text_of(after, "fragments/quality/orders.yaml").startswith("# Quality rules: orders\n\n")


def test_i17_schema_moves_the_columns_as_a_list_root_fragment(tmp_path: Path) -> None:
    pair = make_pair(tmp_path, SPLIT_ALL_FILES)

    after = split_and_check(pair, "schema", envs=ENVS, cli_envs=CLI_ENVS)

    assert new_files(pair, after) == PART_FILES["schema"]
    columns = yaml.safe_load(text_of(after, "fragments/schemas/orders.yaml"))
    assert columns == [
        {"name": "order_id", "type": "string", "required": True},
        {"name": "amount", "type": "number"},
    ]
    assert "# block-style item" in text_of(after, "fragments/schemas/orders.yaml")
    exposes = yaml.safe_load(text_of(after, NAME))["exposes"]
    assert exposes[0]["contract"]["schema"] == {"$ref": "./fragments/schemas/orders.yaml"}


@pytest.mark.parametrize("part", ["quality", "schema"])
def test_i16_a_part_inside_a_moved_expose_is_referenced_from_the_expose_fragment(
    tmp_path: Path, part: str
) -> None:
    pair = make_pair(tmp_path, SPLIT_ALL_FILES)
    folder = {"quality": "quality", "schema": "schemas"}[part]

    after = split_and_check(pair, f"exposes,{part}", envs=ENVS, cli_envs=CLI_ENVS)

    expose = yaml.safe_load(text_of(after, "fragments/exposes/orders.yaml"))
    assert expose["contract"][part] == {"$ref": f"../{folder}/orders.yaml"}


def test_i18_one_extension_block_goes_to_its_own_file(tmp_path: Path) -> None:
    text = SPLIT_ALL.replace(
        "# end-of-file comment\n", "  other:\n    k: [1, 2]\n# end-of-file comment\n"
    )
    pair = make_pair(tmp_path, {NAME: text, "overlays/prod.yaml": PROD_OVERLAY})

    after = split_and_check(pair, "extensions.acme", envs=ENVS, cli_envs=CLI_ENVS)

    assert new_files(pair, after) == ["fragments/extensions/acme.yaml"]
    block = text_of(after, "fragments/extensions/acme.yaml")
    assert "# comment inside the extension" in block
    assert yaml.safe_load(block) == {"region": "eu", "tiers": ["gold", "silver"]}
    extensions = yaml.safe_load(text_of(after, NAME))["extensions"]
    assert extensions == {
        "acme": {"$ref": "./fragments/extensions/acme.yaml"},
        "other": {"k": [1, 2]},
    }


def test_i18_a_missing_extension_is_nothing_to_split(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """SG-32 characterisation: not an error, and not a single byte changes."""
    pair = make_pair(tmp_path, SPLIT_ALL_FILES)
    before = digest(pair.split)

    assert split(pair.split_contract, "--parts", "extensions.missing") == 0

    assert "Nothing to split" in capsys.readouterr().err
    assert digest(pair.split) == before


def test_i19_a_description_string_is_a_markdown_file(tmp_path: Path) -> None:
    text = "id: x\ndescription: |\n  One row per order.\n  Second line.\nbuilds: []\n"
    pair = solo(tmp_path, text)

    after = split_and_check(pair, "description")

    assert new_files(pair, after) == ["docs/description.md"]
    assert after["docs/description.md"] == b"One row per order.\nSecond line.\n"
    assert yaml.safe_load(text_of(after, NAME))["description"] == {"$ref": "./docs/description.md"}


def test_i19_a_description_object_gets_a_markdown_file_per_text(tmp_path: Path) -> None:
    pair = make_pair(tmp_path, SPLIT_ALL_FILES)

    after = split_and_check(pair, "description", envs=ENVS, cli_envs=CLI_ENVS)

    assert after["docs/purpose.md"] == b"One row per order."
    assert after["docs/usage.md"] == b"Join on `order_id`.\nAmounts are in EUR.\n"
    assert after["docs/limitations.md"] == b"no"
    description = yaml.safe_load(text_of(after, NAME))["description"]
    assert description == {
        "purpose": {"$ref": "./docs/purpose.md"},
        "usage": {"$ref": "./docs/usage.md"},
        "limitations": {"$ref": "./docs/limitations.md"},
    }
    # The comment on the purpose line is outside the text: it stays in the root.
    assert "# trailing on purpose" in text_of(after, NAME)


def test_i19_a_description_a_file_cannot_give_back_stays_inline(tmp_path: Path) -> None:
    text = 'id: x\ndescription: {purpose: "one\\r\\ntwo", usage: "fine"}\n'
    pair = solo(tmp_path, text)

    after = split_and_check(pair, "description")

    assert new_files(pair, after) == ["docs/usage.md"]


def test_i20_an_unknown_part_is_refused_with_the_list_and_nothing_is_written(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pair = make_pair(tmp_path, SPLIT_ALL_FILES)
    before = digest(pair.split)

    assert split(pair.split_contract, "--parts", "foo") != 0

    err = capsys.readouterr().err
    assert "unknown part 'foo'" in err
    for part in (
        "sovereignty",
        "accessPolicy",
        "builds",
        "exposes",
        "sql",
        "quality",
        "schema",
        "extensions",
        "description",
    ):
        assert part in err
    assert "extensions.<name>" in err
    assert digest(pair.split) == before  # not even an empty directory


@pytest.mark.parametrize(
    "parts", ["builds,foo", "foo,builds", "Builds", "extensions.", "sql;builds"]
)
def test_i20_one_bad_name_refuses_the_whole_list(
    tmp_path: Path, parts: str, capsys: pytest.CaptureFixture[str]
) -> None:
    pair = make_pair(tmp_path, SPLIT_ALL_FILES)
    before = digest(pair.split)

    assert split(pair.split_contract, "--parts", parts) != 0

    assert "unknown part" in capsys.readouterr().err
    assert digest(pair.split) == before


@pytest.mark.parametrize("parts", ["", " ", ","])
def test_i20_an_empty_part_list_is_refused(
    tmp_path: Path, parts: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """SG-32 characterisation: no part named is an error, not 'split everything'."""
    pair = make_pair(tmp_path, SPLIT_ALL_FILES)
    before = digest(pair.split)

    assert split(pair.split_contract, "--parts", parts) == 1

    assert "no part named" in capsys.readouterr().err
    assert digest(pair.split) == before


def test_i20_a_part_named_twice_acts_like_one(tmp_path: Path) -> None:
    once = make_pair(tmp_path / "once", SPLIT_ALL_FILES)
    twice = make_pair(tmp_path / "twice", SPLIT_ALL_FILES)

    assert split(once.split_contract, "--parts", "builds") == 0
    assert split(twice.split_contract, "--parts", "builds,builds, builds") == 0

    assert files_of(once.split) == files_of(twice.split)


# ── I-21: refs that are already in a moved part ─────────────────────────

REFS_IN_PARTS = """\
id: x
builds:
  - id: transform
    pattern: embedded-logic
    engine: sql
    properties:
      sql: {$ref: ./sql/transform.sql}
exposes:
  - exposeId: orders
    kind: table
    contract:
      schema: {$ref: ./schemas/orders.yaml}
  - exposeId: totals
    kind: table
    contract:
      schema:
        - {$ref: "./cols.yaml#/a", description: x}
"""
REFS_IN_PARTS_FILES: Dict[str, str] = {
    NAME: REFS_IN_PARTS,
    "sql/transform.sql": "SELECT 1 AS id\n",
    "schemas/orders.yaml": "- {name: order_id, type: string}\n- {name: amount, type: number}\n",
    "cols.yaml": "a: {name: z, type: string}\n",
}


@pytest.mark.xfail(
    strict=True,
    reason="BUG SP-02: a relative $ref inside a moved part is copied unchanged into the fragment, "
    "so it points one or two directories off; split refuses instead of rebasing it",
)
def test_i21_refs_in_moved_parts_are_rebased_to_the_fragment(tmp_path: Path) -> None:
    pair = make_pair(tmp_path, REFS_IN_PARTS_FILES)

    after = split_and_check(pair, envs=ENVS)

    assert "$ref: ../../sql/transform.sql" in text_of(after, "fragments/builds/transform.yaml")
    assert "../../schemas/orders.yaml" in text_of(after, "fragments/exposes/orders.yaml")
    assert "../../cols.yaml#/a" in text_of(after, "fragments/exposes/totals.yaml")


@pytest.mark.xfail(
    strict=True,
    reason="BUG SP-02: split in two passes (--parts sql, then --parts builds) fails: the sql ref "
    "written by the first pass is not rebased when the second moves the build",
)
def test_i21_sql_first_then_builds_is_a_two_pass_split_that_works(tmp_path: Path) -> None:
    pair = make_pair(
        tmp_path, {NAME: "id: x\nbuilds:\n  - id: t\n    properties: {sql: SELECT 1}\n"}
    )
    assert split(pair.split_contract, "--parts", "sql") == 0

    assert split(pair.split_contract, "--parts", "builds") == 0

    assert_same_bundle(pair)


def test_i21_a_refused_split_for_refs_it_cannot_rebase_changes_no_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Today's safe outcome: the split is refused before anything is changed."""
    pair = make_pair(tmp_path, REFS_IN_PARTS_FILES)
    files = files_of(pair.split)

    assert split(pair.split_contract) == 1

    assert "Split not written" in capsys.readouterr().err
    assert files_of(pair.split) == files


def test_i21_refs_that_stay_in_the_root_are_not_touched(tmp_path: Path) -> None:
    text = REFS_IN_PARTS.replace(
        "id: x\n", "id: x\nmetadata:\n  owner: {$ref: ./team.yaml}\n"
    ).replace("      sql: {$ref: ./sql/transform.sql}\n", "      sql: SELECT 1\n")
    files = dict(REFS_IN_PARTS_FILES)
    files[NAME] = text
    files["team.yaml"] = "team: t\nemail: t@example.com\n"
    files["cols.yaml"] = "a: {name: z, type: string}\n"
    # Only the sections whose parts hold no relative ref move.
    pair = make_pair(tmp_path, files)

    after = split_and_check(pair, "builds,sovereignty,accessPolicy")

    assert "  owner: {$ref: ./team.yaml}\n" in text_of(after, NAME)


# ── I-22: YAML aliases ──────────────────────────────────────────────────

ALIAS_CASES: Dict[str, str] = {
    "anchor-kept-alias-moved": """\
# header
id: x
labels: &team {team: orders, tier: gold}
builds:
  - id: a
    engine: sql
    labels: *team
""",
    "anchor-moved-alias-kept": """\
# header
id: x
builds:
  - id: a
    engine: sql
    labels: &team {team: orders, tier: gold}
extensions:
  copy: *team
""",
    "merge-key-in-moved-builds": """\
# header
id: x
defaults: &defaults
  pattern: embedded-logic
  engine: sql
builds:
  - id: a
    <<: *defaults
    properties: {sql: SELECT 1}
  - id: b
    <<: *defaults
exposes:
  - exposeId: o
    kind: table
""",
}


@pytest.mark.parametrize("case", sorted(ALIAS_CASES))
@pytest.mark.parametrize("parts", [None, ALL_PARTS])
def test_i22_aliases_across_the_split_boundary_are_split_as_data_with_a_notice(
    tmp_path: Path, case: str, parts: Optional[str], capsys: pytest.CaptureFixture[str]
) -> None:
    pair = solo(tmp_path, ALIAS_CASES[case])

    after = split_and_check(pair, parts)

    err = capsys.readouterr().err
    assert "Comments in the root were not kept: the contract uses YAML aliases" in err
    # Root and every fragment parse on their own: no alias without its anchor.
    for rel, content in after.items():
        if rel.endswith(".yaml"):
            yaml.safe_load(content.decode("utf-8"))
    assert "# header" not in text_of(after, NAME)  # the documented cost of the fallback
    assert new_files(pair, after)


def test_i22_an_alias_that_never_touches_a_moved_part_still_costs_every_root_comment(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Spec 11 says split keeps every root comment; the documented fallback (finding SP-12)
    gives that up for ANY alias in the file, also one whose anchor and alias both sit in
    parts that stay: here ``labels`` and ``tags``, while only ``builds`` moves."""
    text = """\
# header comment
id: x
labels: &team {team: orders}   # the anchor
tags: *team                    # its alias, both in kept parts
builds:  # ours
  - id: a   # first
    engine: sql
# end of file
"""
    pair = solo(tmp_path, text)

    after = split_and_check(pair)

    assert "Comments in the root were not kept: the contract uses YAML aliases" in (
        capsys.readouterr().err
    )
    assert comments(text) and not comments(text_of(after, NAME))


def test_i22_the_notice_is_only_for_the_alias_fallback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pair = make_pair(tmp_path, SPLIT_ALL_FILES)

    split_and_check(pair, ALL_PARTS)

    assert "were not kept" not in capsys.readouterr().err


# ── I-23: JSON roots ────────────────────────────────────────────────────

JSON_ROOT = json.dumps(yaml.safe_load(SPLIT_ALL), indent=2) + "\n"
JSON_NAME = "contract.fluid.json"


@pytest.mark.parametrize("part", sorted(PART_FILES) + [ALL_PARTS])
def test_i23_a_json_root_stays_json_for_every_part(tmp_path: Path, part: str) -> None:
    pair = make_pair(
        tmp_path, {JSON_NAME: JSON_ROOT, "overlays/prod.yaml": PROD_OVERLAY}, JSON_NAME
    )

    after = split_and_check(pair, part, envs=ENVS, cli_envs=(None, "prod"))

    root = json.loads(text_of(after, JSON_NAME))  # still valid JSON
    assert has_ref_key(root)
    assert list(root) == list(json.loads(JSON_ROOT))  # key order is the original's
    assert not text_of(after, JSON_NAME).startswith(("#", "---"))
    wanted = sorted({f for p in part.split(",") for f in PART_FILES.get(p, [])})
    assert new_files(pair, after) == wanted


def test_i23_a_json_root_holds_ref_objects_and_its_fragments_are_yaml(tmp_path: Path) -> None:
    """SG-32 characterisation: the fragments of a JSON root are YAML, with the usual header."""
    pair = make_pair(tmp_path, {JSON_NAME: JSON_ROOT}, JSON_NAME)

    after = split_and_check(pair, "builds,exposes,sovereignty,accessPolicy,sql")

    root = json.loads(text_of(after, JSON_NAME))
    assert root["sovereignty"] == {"$ref": "./fragments/sovereignty.yaml"}
    assert root["builds"] == [
        {"$ref": "./fragments/builds/ingest-orders.yaml"},
        {"$ref": "./fragments/builds/rollup.yaml"},
    ]
    assert text_of(after, "fragments/builds/rollup.yaml").startswith("# Build: rollup\n\n")
    assert yaml.safe_load(text_of(after, "fragments/builds/rollup.yaml"))["properties"]["sql"] == {
        "$ref": "../../sql/rollup.sql"
    }
    assert text_of(after, JSON_NAME).endswith("}\n")


def test_i23_a_json_root_holding_text_refs_is_valid_json_and_composes(tmp_path: Path) -> None:
    pair = make_pair(tmp_path, {JSON_NAME: JSON_ROOT}, JSON_NAME)

    after = split_and_check(pair, "description,sql")

    root = json.loads(text_of(after, JSON_NAME))
    assert root["description"]["purpose"] == {"$ref": "./docs/purpose.md"}
    assert root["builds"][0]["properties"]["sql"] == {"$ref": "./sql/ingest-orders.sql"}


# ── I-24: every YAML shape a section can be written in ──────────────────

ZOO: Dict[str, Tuple[str, bool]] = {
    # name: (text, comments the check may follow with `# `)
    "compact-sequences": (
        """\
id: x
builds:
- id: a
  engine: sql
  properties:
    parameters:
    - 1
    - 2
- id: b
  engine: sql
exposes:
- exposeId: o
  kind: table
  contract:
    schema:
    - name: a
      type: string
    quality:
    - rule: r
      expression: a IS NOT NULL
      severity: error
""",
        False,
    ),
    "indent-4": (
        """\
id: x
builds:
    -   id: a
        engine: sql
        properties:
            sql: |
                SELECT 1
                FROM t
    -   id: b
        engine: sql
exposes:
    -   exposeId: o
        kind: table
        contract:
            schema:
                -   name: a
                    type: string
""",
        False,
    ),
    "dash-then-spaces": (
        "id: x\nbuilds:\n  -   id: a\n      engine: sql\n  -   id: b\n      engine: sql\n",
        False,
    ),
    "dash-on-its-own-line": (
        "id: x\nbuilds:\n  -\n    id: a\n    engine: sql\n  -\n    id: b\n    engine: sql\n",
        False,
    ),
    "comment-after-the-dash": (
        "id: x\nbuilds:\n  - # after the dash\n    id: a\n    engine: sql\n"
        "  # between\n  - id: b  # on the first key\n    engine: sql\n",
        True,
    ),
    "quoted-keys": (
        '"id": x\n"builds":\n  - id: a\n    engine: sql\n\'exposes\':\n  - exposeId: o\n'
        '    kind: table\n"sovereignty": {jurisdiction: EU}\n',
        False,
    ),
    "explicit-keys": (
        "id: x\n? builds\n: - id: a\n    engine: sql\n? sovereignty\n:   jurisdiction: EU\n",
        False,
    ),
    "space-before-colon": (
        "id: x\nbuilds :\n  - id : a\n    engine : sql\nexposes  :\n  - exposeId: o\n",
        False,
    ),
    "flow-items-in-a-flow-list": (
        "id: x\nexposes: [{exposeId: a, kind: table}, {exposeId: b, kind: topic}]\n"
        "sovereignty: {jurisdiction: EU, allowedRegions: [a, b]}\n",
        False,
    ),
    "flow-over-several-lines": (
        "id: x\nbuilds:\n  - {id: a,\n     engine: sql,\n     properties: {sql: 'SELECT 1'}}\n"
        "  - id: b\n    engine: sql\nexposes: [\n  {exposeId: o1,\n   kind: table},\n"
        "  {exposeId: o2, kind: table}\n]\nsovereignty: {jurisdiction: EU,\n  allowedRegions: [a, b]}\n",
        False,
    ),
    "everything-in-flow-style": (
        "{id: x, builds: [{id: a, engine: sql, properties: {sql: 'SELECT 1'}}], exposes: "
        "[{exposeId: o, kind: table, contract: {schema: [{name: a, type: string}], quality: "
        "[{rule: r, expression: 'a is not null', severity: error}]}}], sovereignty: "
        "{jurisdiction: EU}, extensions: {acme: {a: 1}}, description: {purpose: p, usage: u}}\n",
        False,
    ),
    "json-written-as-yaml": (
        '{"id": "x", "builds": [{"id": "a", "engine": "sql"}],\n "exposes": '
        '[{"exposeId": "o", "kind": "table"}]}\n',
        False,
    ),
    "blank-lines-everywhere": (
        "id: x\nbuilds:\n\n  - id: a\n    engine: sql\n\n\n  - id: b\n    engine: sql\n\n"
        "exposes:\n\n  - exposeId: o\n",
        False,
    ),
    "comment-at-column-zero-inside-a-list": (
        "id: x\nbuilds:\n  - id: a\n    engine: sql\n# col0 comment inside builds\n  - id: b\n"
        "    engine: sql\n# another\nexposes:\n  - exposeId: o\n",
        True,
    ),
    "document-markers": (
        "---\nid: x\nbuilds:\n  - id: a\n    engine: sql\nexposes:\n  - exposeId: o\n...\n# after\n",
        True,
    ),
    "yaml-directive": ("%YAML 1.1\n---\nid: x\nbuilds:\n  - id: a\n    engine: sql\n", False),
    "deep-indentation": (
        "id: x\nbuilds:\n        - id: a\n          engine: sql\n          properties:\n"
        "                  sql: |\n                        SELECT 1\n",
        False,
    ),
    "hash-and-colon-in-values": (
        "id: x\nsovereignty:\n  jurisdiction: 'EU: strict # not a comment'\n  note: a#b\n"
        "builds:\n  - id: 'a: b'\n    engine: sql\nexposes:\n  - exposeId: \"o # x\"\n",
        False,
    ),
    "multi-line-plain-and-quoted-scalars": (
        "id: x\ndescription:\n  purpose: this is a long\n    plain scalar spread over\n"
        "    several lines\n  usage: 'quoted\n    over lines'\nbuilds:\n  - id: a\n"
        "    description: another long\n      plain scalar\n    engine: sql\n",
        False,
    ),
    "block-scalars-in-sequence-items": (
        "id: x\nexposes:\n- exposeId: o\n  kind: table\n  contract:\n    schema:\n"
        "    - name: a\n      description: |\n        multi\n          indented\n        lines\n"
        "      type: string\n    - name: b\n      description: >-\n        folded text\n"
        "        goes here\n      type: string\n",
        False,
    ),
    "indentation-indicator": (
        "id: x\nbuilds:\n  - id: a\n    engine: sql\n    properties:\n      sql: |2\n"
        "          leading spaces\n        SELECT 1\n      parameters: {a: 1}\n",
        False,
    ),
    "yaml-lookalikes-in-a-block-scalar": (
        "id: x\nbuilds:\n  - id: a\n    engine: sql\n    properties:\n      sql: |\n"
        "        # looks like a comment\n        --- looks like a document marker\n"
        "        ... and an end marker\n        SELECT 1\n      parameters: {a: 1}\n",
        False,
    ),
    "unicode": (
        "id: x\ndescription: {purpose: 'Café ☕ 订单', usage: 'заказ 😀'}\nbuilds:\n  - id: a\n"
        "    engine: sql\n    properties:\n      sql: |\n        SELECT 'ü' AS \"é\", '😀'\n"
        "exposes:\n  - exposeId: o\n    kind: table\n    description: 'ünïcode'\n",
        False,
    ),
    "keys-that-are-not-strings": (
        "id: x\nextensions:\n  yes: {a: 1}\n  no: {b: 2}\n  200: {c: 3}\n  acme: {d: 4}\n",
        False,
    ),
    "extension-names-with-dots-and-spaces": (
        "id: x\nextensions:\n  acme.v2: {a: 1}\n  'my ext': {b: 2}\n  listy: [1, 2]\n",
        False,
    ),
    "items-that-are-not-mappings": (
        "id: x\nbuilds:\n  - ~\n  - id: a\n    engine: sql\n  - plain\nexposes:\n  - 5\n  - exposeId: o\n",
        False,
    ),
    "sections-of-the-wrong-type": (
        "id: x\nbuilds: nothing\nexposes: {exposeId: o}\nsovereignty: EU\naccessPolicy: [a, b]\n"
        "extensions: text\ndescription: 5\n",
        False,
    ),
    "empty-sections": (
        "id: x\nbuilds: []\nsovereignty: {}\nexposes:\naccessPolicy: ~\nextensions: {}\ndescription: ''\n",
        False,
    ),
    "a-very-long-line": (
        "id: x\nbuilds:\n  - id: a\n    engine: sql\n    description: " + "word " * 4000 + "\n",
        False,
    ),
}


@pytest.mark.parametrize("case", sorted(ZOO))
def test_i24_every_yaml_shape_splits_with_default_parts_and_with_all(
    tmp_path: Path, case: str
) -> None:
    text, has_comments = ZOO[case]
    for parts, run in ((None, "default"), (ALL_PARTS, "all")):
        pair = solo(tmp_path / run, text)
        after = split_and_check(pair, parts, cli_envs=() if run == "default" else None)
        if has_comments:
            assert_comments_conserved(text, after)
        assert all(rel.endswith((".yaml", ".sql", ".md")) for rel in after)
        for rel, content in after.items():
            if rel.endswith(".yaml"):
                yaml.safe_load(content.decode("utf-8"))  # every file is valid on its own


@pytest.mark.parametrize("name", ["root-ref", "root-ref-and-keys"])
def test_i24_a_root_that_is_itself_a_ref_has_nothing_that_moves_or_only_its_own_keys(
    tmp_path: Path, name: str
) -> None:
    text = {
        "root-ref": "$ref: ./all.yaml\n",
        "root-ref-and-keys": "$ref: ./all.yaml\nbuilds:\n  - id: a\n",
    }[name]
    pair = make_pair(tmp_path, {NAME: text, "all.yaml": "id: z\nbuilds:\n  - id: q\n"})

    after = split_and_check(pair)

    assert after["all.yaml"] == b"id: z\nbuilds:\n  - id: q\n"
    assert after[NAME].startswith(b"$ref: ./all.yaml\n")


@pytest.mark.parametrize(
    "text,parts",
    [
        ("id: x\nsovereignty: !!map\n  jurisdiction: EU\n", None),
        ("id: x\nsovereignty: &unused\n  jurisdiction: EU\n", None),
        ("id: x\nextensions:\n  acme: &unused\n    region: eu\n", "extensions"),
    ],
    ids=["tag-on-a-section", "unused-anchor-on-a-section", "unused-anchor-on-an-extension"],
)
@pytest.mark.xfail(
    strict=True,
    reason="BUG SP-06: a tag or an unused anchor between a key and its block mapping puts the mapping "
    "on the key's line, so the splice writes 'key: $ref: ...', which does not parse; split "
    "refuses instead of falling back to a data split as it does when an alias is used",
)
def test_i24_a_tag_or_an_unused_anchor_on_a_moved_section_splits(
    tmp_path: Path, text: str, parts: Optional[str]
) -> None:
    pair = solo(tmp_path, text)

    split_and_check(pair, parts)


# ── I-25 / I-26: line ends, byte-order mark, the end of the file ────────

#: SPLIT-ALL with CRLF line ends, minus the one comment that ends a moved block
#: (see ``test_i25_a_comment_ending_a_moved_block...``, finding SP-01).
SPLIT_ALL_CRLF = SPLIT_ALL.replace(
    "  dataResidency: true       # trailing comment on its last line\n", "  dataResidency: true\n"
).replace("\n", "\r\n")


def assert_one_line_end(files: Mapping[str, bytes], crlf: bool = True) -> None:
    """Every YAML file the split wrote (the root and fragments) uses one line end."""
    for rel, content in files.items():
        if rel.endswith(".yaml") and not rel.startswith("overlays/"):
            if crlf:
                assert content.count(b"\n") == content.count(b"\r\n"), f"mixed line ends in {rel}"
            else:
                assert b"\r" not in content, rel


@pytest.mark.parametrize("part", sorted(PART_FILES) + [ALL_PARTS])
def test_i25_every_part_on_a_crlf_root_keeps_crlf_on_every_line(tmp_path: Path, part: str) -> None:
    pair = make_pair(tmp_path, {NAME: SPLIT_ALL_CRLF, "overlays/prod.yaml": PROD_OVERLAY})

    after = split_and_check(pair, part, envs=(None, "prod"), cli_envs=(None,))

    assert_one_line_end(after)
    assert b"\r\n" in after[NAME]
    assert_comments_conserved(SPLIT_ALL_CRLF, after)


COMMENT_ENDS_MOVED_BLOCK = (
    "id: x\r\n"
    "exposes:\r\n"
    "  - exposeId: a\r\n"
    "    kind: table  # a comment ends this block\r\n"
    "  - exposeId: b\r\n"
    "    kind: table\r\n"
    "# eof\r\n"
)


@pytest.mark.parametrize(
    "text,parts,where",
    [
        (COMMENT_ENDS_MOVED_BLOCK, None, NAME),
        (
            "id: x\r\nexposes:\r\n  - exposeId: a\r\n    kind: table\r\n    contract:\r\n"
            "      schema:\r\n        - name: a\r\n          type: string  # ends the columns\r\n"
            "      quality: []\r\n",
            "exposes,schema",
            "fragments/exposes/a.yaml",
        ),
    ],
    ids=["root", "nested-fragment"],
)
@pytest.mark.xfail(
    strict=True,
    reason="BUG SP-01: a trailing comment on the last line of a moved block takes that line's CR "
    "with it, so the $ref line that replaces it ends in a bare LF in a CRLF file",
)
def test_i25_a_comment_ending_a_moved_block_in_a_crlf_root_keeps_crlf(
    tmp_path: Path, text: str, parts: Optional[str], where: str
) -> None:
    pair = solo(tmp_path, text)

    after = split_and_check(pair, parts)

    assert after[where].count(b"\n") == after[where].count(b"\r\n"), f"mixed line ends in {where}"
    assert_one_line_end(after)


def test_i25_a_comment_ending_a_moved_block_still_moves_with_it_in_a_crlf_root(
    tmp_path: Path,
) -> None:
    """The data and the comment are right; only the line end of the ref line is not."""
    pair = solo(tmp_path, COMMENT_ENDS_MOVED_BLOCK)

    after = split_and_check(pair)

    assert "# a comment ends this block" in text_of(after, "fragments/exposes/a.yaml")
    assert text_of(after, NAME).endswith("\r\n# eof\r\n")


@pytest.mark.parametrize("line_end", ["\r\n", "\n"], ids=["crlf", "lf"])
def test_i25_a_byte_order_mark_is_kept(tmp_path: Path, line_end: str) -> None:
    text = "﻿" + SPLIT_ALL.replace("\n", line_end)
    text = text.replace("       # trailing comment on its last line", "")
    pair = make_pair(tmp_path, {NAME: text.encode("utf-8"), "overlays/prod.yaml": PROD_OVERLAY})

    after = split_and_check(pair, ALL_PARTS, envs=(None, "prod"), cli_envs=(None,))

    assert after[NAME].startswith(b"\xef\xbb\xbf# ===== Orders")
    assert after[NAME].count(b"\xef\xbb\xbf") == 1
    assert_one_line_end(after, crlf=line_end == "\r\n")
    assert b"$ref" in after[NAME]


EOF_CASES: Dict[str, Tuple[str, str]] = {
    # name: (contract text, the tail the split root must still end with)
    "no-newline-after-the-moved-section": (
        "id: x\nexposes:\n  - exposeId: a\n    kind: table",
        "a.yaml",
    ),
    "eof-comment-without-newline": (
        "id: x\nexposes:\n  - exposeId: a\n    kind: table\n# EOF comment",
        "a.yaml\n# EOF comment",
    ),
    "eof-comment-after-blank-lines": (
        "id: x\nexposes:\n  - exposeId: a\n    kind: table\n\n\n# EOF comment\n\n",
        "a.yaml\n\n\n# EOF comment\n\n",
    ),
    "comment-ending-the-moved-block-at-eof": (
        "id: x\nexposes:\n  - exposeId: a\n    kind: table  # last",
        "a.yaml",
    ),
    "moved-mapping-section-last-with-eof-comment": (
        "id: x\nsovereignty:\n  jurisdiction: EU\n# EOF comment\n",
        "sovereignty.yaml\n# EOF comment\n",
    ),
    "moved-flow-section-last": ("id: x\nsovereignty: {jurisdiction: EU}", "sovereignty.yaml}"),
}


@pytest.mark.parametrize("case", sorted(EOF_CASES))
def test_i26_the_end_of_the_file_is_kept_as_it_was(tmp_path: Path, case: str) -> None:
    text, tail = EOF_CASES[case]
    pair = solo(tmp_path, text)

    after = split_and_check(pair)

    assert text_of(after, NAME).endswith(tail)
    if "# EOF comment" in text:
        assert_comments_conserved(text, after)
    if not text.endswith("\n"):
        assert not text_of(after, NAME).endswith("\n")  # no newline invented


# ── I-27: duplicate top-level keys ──────────────────────────────────────


def test_i27_a_duplicate_top_level_key_splits_the_node_the_loader_uses(tmp_path: Path) -> None:
    """SG-18 characterisation: PyYAML keeps the LAST ``exposes:``; so does the splice."""
    text = (
        "id: x\nexposes:\n  - exposeId: first\n    kind: table\n"
        "exposes:\n  - exposeId: last\n    kind: topic\n"
    )
    pair = solo(tmp_path, text)
    assert [e["exposeId"] for e in yaml.safe_load(text)["exposes"]] == ["last"]

    after = split_and_check(pair)

    assert new_files(pair, after) == ["fragments/exposes/last.yaml"]
    root = text_of(after, NAME)
    assert "  - exposeId: first\n    kind: table\n" in root  # the dead duplicate is left alone
    assert root.endswith("exposes:\n  - $ref: ./fragments/exposes/last.yaml\n")
    assert [e["exposeId"] for e in compose_contract(pair.split_contract).document["exposes"]] == [
        "last"
    ]


# ── I-28: the dry run ───────────────────────────────────────────────────

DRY_RUN_CONTRACT = """\
id: x
sovereignty: {jurisdiction: EU}
builds:
  - id: t
    properties: {sql: "SELECT 1"}
  - id: T
    properties: {sql: "SELECT 2"}
exposes:
  - exposeId: o
    contract:
      schema: [{name: a, type: string}]
      quality: [{rule: r, expression: "a IS NOT NULL", severity: error}]
extensions:
  acme: {a: 1}
description: {purpose: "p", usage: "u", limitations: "l"}
"""


def _listed(err: str) -> List[str]:
    return sorted(line.strip() for line in err.splitlines()[1:] if line.startswith("   "))


@pytest.mark.parametrize("parts", [None, "builds,exposes,sql", ALL_PARTS])
def test_i28_the_dry_run_lists_exactly_what_the_real_run_writes_and_writes_nothing(
    tmp_path: Path, parts: Optional[str], capsys: pytest.CaptureFixture[str]
) -> None:
    files = {
        NAME: DRY_RUN_CONTRACT,
        "fragments/builds/t.yaml": "# mine\n",
        "sql/t.sql": "-- mine\n",
        "docs/usage.md": "mine",
    }
    pair = make_pair(tmp_path, files)
    args = ["--parts", parts] if parts else []
    before = digest(pair.split)
    capsys.readouterr()

    assert split(pair.split_contract, "--dry-run", *args) == 0

    assert digest(pair.split) == before  # not a file, not a directory
    listed = _listed(capsys.readouterr().err)
    assert listed

    assert split(pair.split_contract, *args) == 0

    written = sorted(set(files_of(pair.split)) - set(files))
    assert listed == written
    for rel, content in files.items():
        if rel != NAME:
            assert (pair.split / rel).read_bytes() == content.encode("utf-8")


# ── I-29: --out ─────────────────────────────────────────────────────────


def test_i29_split_into_another_directory_leaves_the_original_untouched(tmp_path: Path) -> None:
    pair = make_pair(tmp_path, SPLIT_ALL_FILES)
    out = tmp_path / "elsewhere"
    before = digest(pair.split)

    assert split(pair.split_contract, "--out", out) == 0

    assert digest(pair.split) == before
    assert (pair.split / NAME).read_bytes() == SPLIT_ALL.encode("utf-8")
    assert sorted(files_of(out)) == sorted([NAME, *DEFAULT_LAYOUT])
    # The copy is the same split as in place (overlays are not copied: base only).
    in_place = make_pair(tmp_path / "again", SPLIT_ALL_FILES)
    assert split(in_place.split_contract) == 0
    for rel in [NAME, *DEFAULT_LAYOUT]:
        assert (out / rel).read_bytes() == (in_place.split / rel).read_bytes()
    assert compose_contract(out / NAME).document == compose_contract(pair.flat_contract).document


def test_i29_a_root_that_keeps_a_ref_is_not_split_into_a_directory_without_its_target(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """SG-32 characterisation: the remaining root refs are not rebased, so the
    copy would not compose; split refuses, and the original is untouched."""
    text = "id: x\nmetadata:\n  owner: {$ref: ./team.yaml}\nbuilds:\n  - id: a\n    engine: sql\n"
    pair = make_pair(tmp_path, {NAME: text, "team.yaml": "team: t\nemail: t@example.com\n"})
    before = digest(pair.split)

    assert split(pair.split_contract, "--out", tmp_path / "elsewhere") == 1

    err = capsys.readouterr().err
    assert "Split not written" in err and "./team.yaml" in err
    assert digest(pair.split) == before
    assert not (tmp_path / "elsewhere" / NAME).exists()


def test_i29_the_same_directory_given_as_out_is_an_in_place_split(tmp_path: Path) -> None:
    pair = make_pair(tmp_path, SPLIT_ALL_FILES)

    assert split(pair.split_contract, "--out", pair.split) == 0

    assert_same_bundle(pair, ENVS, cli_envs=(None,))
    assert sorted(set(files_of(pair.split)) - set(files_of(pair.flat))) == DEFAULT_LAYOUT


def test_i29_an_existing_root_in_out_is_replaced_without_a_word_today(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """SG-32 characterisation (finding SP-11): fragments never overwrite a file,
    but the root written to ``--out`` replaces a different file of that name."""
    pair = make_pair(tmp_path, SPLIT_ALL_FILES)
    out = tmp_path / "elsewhere"
    other = b"# somebody else's contract\nid: precious\n"
    write(out / NAME, other)
    capsys.readouterr()

    assert split(pair.split_contract, "--dry-run", "--out", out) == 0
    assert NAME not in capsys.readouterr().err  # the preview does not mention it either
    assert split(pair.split_contract, "--out", out) == 0

    assert (out / NAME).read_bytes() != other
    assert b"precious" not in (out / NAME).read_bytes()


def test_i29_splitting_twice_into_the_same_out_adds_numbered_copies(tmp_path: Path) -> None:
    """SG-32 characterisation: the source stays flat, so a second run into the
    same ``--out`` meets its own first-run files and writes ``-2`` copies; the
    root now refers to those and the first run's fragments are left over."""
    pair = make_pair(tmp_path, SPLIT_ALL_FILES)
    out = tmp_path / "elsewhere"

    assert split(pair.split_contract, "--out", out) == 0
    first = files_of(out)
    assert split(pair.split_contract, "--out", out) == 0

    second = files_of(out)
    assert all(second[rel] == first[rel] for rel in first if rel != NAME)  # nothing rewritten
    assert sorted(set(second) - set(first)) == [
        "fragments/access-policy-2.yaml",
        "fragments/builds/ingest-orders-2.yaml",
        "fragments/builds/rollup-2.yaml",
        "fragments/exposes/order_totals-2.yaml",
        "fragments/exposes/orders-2.yaml",
        "fragments/sovereignty-2.yaml",
    ]
    assert "ingest-orders-2.yaml" in text_of(second, NAME)
    assert "ingest-orders.yaml" not in text_of(second, NAME)
    assert compose_contract(out / NAME).document == compose_contract(pair.flat_contract).document


# ── I-30: an alias bomb ─────────────────────────────────────────────────


def test_i30_an_alias_bomb_root_is_refused_by_the_bounded_loader(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    lines = ["a0: &a0 [x, x, x, x, x, x, x, x, x, x]"]
    for i in range(1, 12):
        lines.append(f"a{i}: &a{i} [" + ", ".join([f"*a{i - 1}"] * 10) + "]")
    text = "\n".join(lines) + "\nexposes:\n  - exposeId: o\n    kind: table\n"
    pair = solo(tmp_path, text)
    before = digest(pair.split)
    started = time.monotonic()

    assert split(pair.split_contract) == 1

    assert time.monotonic() - started < 10
    assert "alias" in capsys.readouterr().err.lower()
    assert digest(pair.split) == before


# ── I-32: the dict-level splitter (what `fluid forge` template mode calls) ──

ORACLE_CONTRACTS: Dict[str, Dict[str, Any]] = {
    "split-all": yaml.safe_load(SPLIT_ALL),
    "unnamed-items": yaml.safe_load(
        "builds:\n  - engine: sql\n  - id: n\nexposes:\n  - kind: table\n  - {exposeId: ''}\n"
    ),
    "items-that-are-not-mappings": {"builds": [1, {"id": "a"}, "s"], "exposes": ["x"]},
    "empty-and-null-sections": {
        "id": "x",
        "builds": [],
        "exposes": None,
        "sovereignty": {},
        "accessPolicy": None,
    },
    "only-an-id": {"id": "x"},
    "ids-with-odd-characters": {"builds": [{"id": "a/b"}, {"id": "../c"}, {"id": 12}, {"id": "ü"}]},
    "nested-data": {
        "sovereignty": {
            "jurisdiction": "EU",
            "allowedRegions": ["a", "b"],
            "n": {"k": [1, {"z": None}]},
        },
        "exposes": [
            {"exposeId": "o", "contract": {"schema": [{"name": "a"}], "note": "x: y # z\nw"}}
        ],
    },
}


@pytest.mark.parametrize("name", sorted(ORACLE_CONTRACTS))
def test_i32_the_dict_level_splitter_writes_what_0_24_0_wrote(name: str) -> None:
    import copy

    from fluid_build.cli.forge_contract_fragments import (
        describe_fragment_layout,
        split_contract_to_fragments,
    )

    contract = ORACLE_CONTRACTS[name]
    snapshot = copy.deepcopy(contract)

    root, files = split_contract_to_fragments(contract)

    assert (root, files) == split_0_24_0(contract)
    assert contract == snapshot  # the input is not mutated
    assert sorted(describe_fragment_layout(contract)) == sorted(files)


def test_i32_the_dict_level_splitter_returns_a_non_mapping_as_it_is() -> None:
    from fluid_build.cli.forge_contract_fragments import split_contract_to_fragments

    assert split_contract_to_fragments([1, 2]) == ([1, 2], {})


def test_i32_the_dict_level_splitter_no_longer_overwrites_a_fragment_with_a_clashing_one() -> None:
    """0.24.0 wrote both builds to one file and referenced it twice."""
    from fluid_build.cli.forge_contract_fragments import split_contract_to_fragments

    contract = {"builds": [{"id": "ingest.orders", "n": 1}, {"id": "ingest-orders", "n": 2}]}

    old_root, old_files = split_0_24_0(contract)
    root, files = split_contract_to_fragments(contract)

    assert len(old_files) == 1 and old_root["builds"][0] == old_root["builds"][1]
    assert sorted(files) == [
        "fragments/builds/ingest-orders-2.yaml",
        "fragments/builds/ingest-orders.yaml",
    ]
    assert root["builds"] == [
        {"$ref": "./fragments/builds/ingest-orders.yaml"},
        {"$ref": "./fragments/builds/ingest-orders-2.yaml"},
    ]


def test_i32_the_dict_level_splitter_takes_parts_and_keeps_a_part_inside_its_file() -> None:
    from fluid_build.cli.forge_contract_fragments import split_contract_to_fragments

    contract = {"builds": [{"id": "a", "properties": {"sql": "SELECT 1\n", "language": "python"}}]}

    root, files = split_contract_to_fragments(contract, ("builds", "sql"))

    assert files["sql/a.py"] == "SELECT 1\n"  # text parts are the text itself
    assert yaml.safe_load(files["fragments/builds/a.yaml"])["properties"]["sql"] == {
        "$ref": "../../sql/a.py"
    }
    assert root["builds"] == [{"$ref": "./fragments/builds/a.yaml"}]


# ── I-33 / I-34: writing where it must not, and failing halfway ─────────


@pytest.mark.skipif(sys.platform == "win32", reason="needs symlinks")
def test_i33_a_symlinked_fragments_directory_never_leaves_a_fragment_outside_out(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """SP-08: a ``fragments/`` directory that links outside is refused before anything is
    written, there or through it: no file and no directory lands outside."""
    pair = make_pair(tmp_path, SPLIT_ALL_FILES)
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, pair.split / "fragments")
    root = pair.split_contract.read_bytes()

    assert split(pair.split_contract) == 1

    assert "is a symlink; split writes no file through one" in capsys.readouterr().err
    assert pair.split_contract.read_bytes() == root
    assert entries_of(outside) == []


@pytest.mark.skipif(sys.platform == "win32", reason="needs symlinks")
def test_i33_a_symlinked_sql_directory_is_refused_and_no_text_is_left_outside(
    tmp_path: Path,
) -> None:
    pair = make_pair(tmp_path, SPLIT_ALL_FILES)
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, pair.split / "sql")
    root = pair.split_contract.read_bytes()

    assert split(pair.split_contract, "--parts", "sql") == 1

    assert pair.split_contract.read_bytes() == root
    assert not files_of(outside)


@pytest.mark.skipif(sys.platform == "win32", reason="needs symlinks")
def test_i33_a_symlink_to_an_existing_file_counts_as_taken_and_is_never_written_through(
    tmp_path: Path,
) -> None:
    victim = tmp_path / "outside" / "victim.yaml"
    write(victim, "precious: data\n")
    pair = make_pair(tmp_path, {NAME: "id: x\nbuilds:\n  - id: a\n    engine: sql\n"})
    (pair.split / "fragments" / "builds").mkdir(parents=True)
    os.symlink(victim, pair.split / "fragments" / "builds" / "a.yaml")

    assert split(pair.split_contract) == 0

    assert victim.read_text("utf-8") == "precious: data\n"
    assert (pair.split / "fragments" / "builds" / "a-2.yaml").is_file()  # the next free name
    assert_same_bundle(pair)


@pytest.mark.skipif(sys.platform == "win32", reason="needs symlinks")
def test_i33_a_dangling_symlink_at_a_target_name_refuses_the_split_and_creates_nothing_outside(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """SG-32 characterisation: the name does not 'exist' to ``Path.exists`` (the
    link is dangling), so it is not skipped; creating it exclusively fails."""
    pair = make_pair(tmp_path, {NAME: "id: x\nbuilds:\n  - id: a\n    engine: sql\n"})
    (pair.split / "fragments" / "builds").mkdir(parents=True)
    os.symlink(
        tmp_path / "outside" / "nothing.yaml", pair.split / "fragments" / "builds" / "a.yaml"
    )

    assert split(pair.split_contract) == 1

    assert "Split not written" in capsys.readouterr().err
    assert not (tmp_path / "outside").exists()
    assert pair.split_contract.read_bytes() == b"id: x\nbuilds:\n  - id: a\n    engine: sql\n"


@pytest.mark.skipif(sys.platform == "win32", reason="needs symlinks")
def test_i33_a_symlinked_root_is_refused_because_its_fragments_would_sit_beside_the_link(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """SG-14/SG-32 characterisation: the fragments are written beside the link,
    refs resolve beside the target, so the check fails and the split is undone."""
    text = "# c\nid: x\nbuilds:\n  - id: a\n    engine: sql\n"
    real = write(tmp_path / "shared" / NAME, text)
    link = tmp_path / "proj" / NAME
    link.parent.mkdir()
    os.symlink(real, link)

    assert split(link) == 1

    assert "Split not written" in capsys.readouterr().err
    assert real.read_text("utf-8") == text
    assert not files_of(tmp_path / "proj" / "fragments")


needs_posix_permissions = pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="permission bits do not stop this user",
)


@needs_posix_permissions
def test_i34_an_unwritable_fragment_directory_stops_the_split_and_leaves_the_root_alone(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """SG-32: the root is written last, so a failure before it changes nothing."""
    pair = make_pair(tmp_path, SPLIT_ALL_FILES)
    locked = pair.split / "fragments" / "exposes"
    locked.mkdir(parents=True)
    locked.chmod(0o555)
    root = pair.split_contract.read_bytes()
    try:
        assert split(pair.split_contract) == 1
    finally:
        locked.chmod(0o755)

    assert "Split not written" in capsys.readouterr().err
    assert pair.split_contract.read_bytes() == root
    assert not files_of(pair.split / "fragments")  # what was written is taken back


@needs_posix_permissions
def test_i34_an_unwritable_output_directory_is_a_clean_refusal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pair = make_pair(tmp_path, SPLIT_ALL_FILES)
    pair.split.chmod(0o555)
    try:
        assert split(pair.split_contract) == 1
    finally:
        pair.split.chmod(0o755)

    assert "Split not written" in capsys.readouterr().err
    assert pair.split_contract.read_bytes() == SPLIT_ALL.encode("utf-8")


@needs_posix_permissions
def test_i34_an_unwritable_root_file_is_an_unexpected_error_today(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """SG-32 characterisation (finding SP-09): every other failed write is exit 1,
    'Split not written'. A read-only root fails at the last write, and the undo
    that tries to write the root back fails the same way inside the handler."""
    pair = make_pair(tmp_path, SPLIT_ALL_FILES)
    pair.split_contract.chmod(0o444)
    try:
        rc = split(pair.split_contract)
    finally:
        pair.split_contract.chmod(0o644)

    assert rc == 2
    shown = capsys.readouterr()
    assert "Unexpected error" in shown.out + shown.err
    assert pair.split_contract.read_bytes() == SPLIT_ALL.encode("utf-8")
    assert not files_of(pair.split / "fragments")


@pytest.mark.parametrize("failure", ["refs-it-cannot-rebase", "out-elsewhere", "lone-surrogate"])
@pytest.mark.xfail(
    strict=True,
    reason="BUG SP-05: a refused split says 'nothing was changed' but leaves the directories it "
    "made (an empty fragments/ changes what `fluid bundle` does without --out), and for a "
    "failed write also the empty file it was writing",
)
def test_i34_a_refused_split_leaves_the_tree_exactly_as_it_was(
    tmp_path: Path, failure: str
) -> None:
    if failure == "refs-it-cannot-rebase":
        pair = make_pair(tmp_path, REFS_IN_PARTS_FILES)
        args: List[Any] = []
    elif failure == "out-elsewhere":
        text = (
            "id: x\nmetadata:\n  owner: {$ref: ./team.yaml}\nbuilds:\n  - id: a\n    engine: sql\n"
        )
        pair = make_pair(tmp_path, {NAME: text, "team.yaml": "team: t\n"})
        args = ["--out", tmp_path / "elsewhere"]
    else:
        text = 'id: x\nbuilds:\n  - id: t\n    properties: {sql: "SELECT \\ud800 1"}\n'
        pair = solo(tmp_path, text)
        args = ["--parts", "sql"]
    before = digest(tmp_path)

    assert split(pair.split_contract, *args) == 1

    assert digest(tmp_path) == before


def test_i03_a_comment_on_the_header_of_a_moved_text_scalar_is_dropped_without_a_word(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """SG-32 characterisation (finding SP-10): ``sql: |  # the main query``. The
    comment is outside the string's value, inside the span that becomes a ref,
    and a text file cannot hold it. Nothing says it was left out."""
    text = (
        "id: x\nbuilds:\n  - id: a\n    engine: sql\n    properties:\n"
        "      sql: |  # the main query\n        SELECT 1\n"
        "description:\n  purpose: >  # about\n    A folded\n    purpose.\n  usage: u  # kept\n"
    )
    pair = solo(tmp_path, text)
    capsys.readouterr()

    after = split_and_check(pair, "sql,description")

    everything = b"\n".join(after.values()).decode("utf-8")
    assert "the main query" not in everything and "# about" not in everything
    assert "# kept" in text_of(after, NAME)  # a comment after a scalar's end is the root's
    assert "were not kept" not in capsys.readouterr().err


@pytest.mark.parametrize(
    "name,content,rc",
    [
        ("contract.fluid.yaml", "", 0),
        ("contract.fluid.yaml", "# only a comment\n", 0),
        ("contract.fluid.yaml", "- a\n- b\n", 1),
        ("contract.fluid.yaml", "just text\n", 1),
        ("contract.fluid.yaml", "id: [unclosed\n", 1),
        ("contract.fluid.yaml", b"\xff\xfe\x00\x01", 1),
        ("contract.fluid.json", "[1, 2]", 1),
        ("contract.fluid.json", "{nope", 1),
        ("contract.fluid.json", "", 1),
    ],
    ids=[
        "empty",
        "comment-only",
        "list-root",
        "scalar-root",
        "bad-yaml",
        "not-utf8",
        "json-list",
        "bad-json",
        "empty-json",
    ],
)
def test_i01_a_file_that_is_not_a_contract_is_refused_cleanly_and_left_alone(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], name: str, content: Any, rc: int
) -> None:
    path = write(tmp_path / name, content)
    before = digest(tmp_path)

    assert split(path) == rc

    err = capsys.readouterr().err
    assert ("Nothing to split" in err) if rc == 0 else ("Failed to load contract" in err)
    assert "Traceback" not in err
    assert digest(tmp_path) == before


def test_i01_a_missing_contract_is_exit_2_and_a_directory_exit_1_and_nothing_is_written(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    before = digest(tmp_path)

    assert split(tmp_path / "nope.fluid.yaml") == 2
    assert "not found" in capsys.readouterr().err
    assert split(tmp_path) == 1
    assert "Failed to load contract" in capsys.readouterr().err

    assert digest(tmp_path) == before


# ── I-35: corpora (the baseline fixtures and every shipped example) ─────


def copy_tree_pair(tmp_path: Path, source: Path, name: str = NAME) -> Pair:
    """``source`` copied twice: once to keep flat, once to split in place."""
    pair = Pair(tmp_path / "flat", tmp_path / "split", name)
    for side in (pair.flat, pair.split):
        shutil.copytree(source, side)
    return pair


BASELINE_FIXTURES = (
    Path(os.environ.get("FLUID_BASELINE_DIR") or "/nonexistent-fluid-baseline") / "fixtures"
)


@pytest.mark.skipif(
    not (BASELINE_FIXTURES / "stream-flat").is_dir(),
    reason="the baseline corpus (FLUID_BASELINE_DIR) is not on this machine",
)
@pytest.mark.parametrize("fixture", ["stream-flat", "batch-flat"])
@pytest.mark.parametrize("parts", [None, ALL_PARTS], ids=["default", "all"])
def test_i35_baseline_flat_fixtures_split_in_place_bundle_equal_in_every_env(
    tmp_path: Path, fixture: str, parts: Optional[str]
) -> None:
    pair = copy_tree_pair(tmp_path, BASELINE_FIXTURES / fixture)
    overlays = {
        rel: data for rel, data in files_of(pair.split).items() if rel.startswith("overlays/")
    }
    assert sorted(overlays) == ["overlays/dev.yaml", "overlays/prod.yaml"]

    after = split_and_check(pair, parts, envs=ENVS, cli_envs=ENVS)

    assert new_files(pair, after)
    for rel, data in overlays.items():  # the overlays stay beside the root, untouched
        assert after[rel] == data


EXAMPLES = sorted(REPO.glob("examples/**/contract.fluid.yaml"))


def test_i35_the_example_corpus_is_there() -> None:
    assert len(EXAMPLES) >= 20


@pytest.mark.parametrize(
    "example", EXAMPLES, ids=lambda p: p.parent.relative_to(REPO / "examples").as_posix()
)
def test_i35_every_shipped_example_survives_a_split_with_default_and_with_all_parts(
    tmp_path: Path, example: Path
) -> None:
    flat_check = copy_tree_pair(tmp_path / "probe", example.parent, example.name)
    try:
        compose_contract(flat_check.flat_contract)
    except Exception:  # noqa: BLE001 - an example that does not compose on its own is not split's
        pytest.skip("the example does not compose on its own")
    envs = (None, *loader.available_overlay_envs(flat_check.flat_contract))

    default = copy_tree_pair(tmp_path / "default", example.parent, example.name)
    split_and_check(default, None, envs=envs, cli_envs=())
    everything = copy_tree_pair(tmp_path / "all", example.parent, example.name)
    split_and_check(everything, ALL_PARTS, envs=envs, cli_envs=(None,))


# ── I-36: refs that only compose with FLUID_REF_ROOT ────────────────────

REF_ROOT_FILES: Dict[str, str] = {
    "product/contract.fluid.yaml": """\
id: x
metadata:
  owner: {$ref: ../shared/team.yaml}
builds:
  - id: a
    engine: sql
    properties:
      parameters: {$ref: ../shared/params.yaml}
exposes:
  - exposeId: o
    kind: table
""",
    "shared/team.yaml": "team: t\nemail: t@example.com\n",
    "shared/params.yaml": "mode: full\n",
}
REF_ROOT_NAME = "product/contract.fluid.yaml"


def _compose_with_ref_root(contract: Path, root: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("FLUID_REF_ROOT", str(root))
    return compose_contract(contract).document


def test_i36_refs_in_the_root_that_need_the_ref_root_survive_a_split(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = dict(REF_ROOT_FILES)
    files[REF_ROOT_NAME] = files[REF_ROOT_NAME].replace(
        "    properties:\n      parameters: {$ref: ../shared/params.yaml}\n", ""
    )
    pair = make_pair(tmp_path, files, REF_ROOT_NAME)

    # Split checks its result against what the original composes to: without the root its
    # refs need, there is nothing to check against, so it refuses and changes nothing.
    monkeypatch.delenv("FLUID_REF_ROOT", raising=False)
    root = pair.split_contract.read_bytes()
    assert split(pair.split_contract) == 1
    assert pair.split_contract.read_bytes() == root

    monkeypatch.setenv("FLUID_REF_ROOT", str(pair.split))
    assert split(pair.split_contract) == 0

    flat = _compose_with_ref_root(pair.flat_contract, pair.flat, monkeypatch)
    assert _compose_with_ref_root(pair.split_contract, pair.split, monkeypatch) == flat
    assert "owner: {$ref: ../shared/team.yaml}" in pair.split_contract.read_text("utf-8")


@pytest.mark.xfail(
    strict=True,
    reason="BUG SP-02: a relative $ref inside a moved part is not rebased; with FLUID_REF_ROOT set "
    "(so the original composes and split can check its work) split refuses",
)
def test_i36_a_moved_part_with_a_ref_that_needs_the_ref_root_splits_with_the_env_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pair = make_pair(tmp_path, REF_ROOT_FILES, REF_ROOT_NAME)
    monkeypatch.setenv("FLUID_REF_ROOT", str(pair.split))

    assert split(pair.split_contract) == 0

    flat = _compose_with_ref_root(pair.flat_contract, pair.flat, monkeypatch)
    assert _compose_with_ref_root(pair.split_contract, pair.split, monkeypatch) == flat


def test_i36_split_without_the_ref_root_set_leaves_a_contract_that_still_composes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """SP-03: without FLUID_REF_ROOT the original does not compose, so its split cannot be
    checked: split refuses, says why, and changes nothing (rebasing the ref is SP-02)."""
    pair = make_pair(tmp_path, REF_ROOT_FILES, REF_ROOT_NAME)
    before = {p: p.read_bytes() for p in pair.split.rglob("*") if p.is_file()}

    assert split(pair.split_contract) == 1
    assert "does not compose here" in capsys.readouterr().err
    assert {p: p.read_bytes() for p in pair.split.rglob("*") if p.is_file()} == before

    flat = _compose_with_ref_root(pair.flat_contract, pair.flat, monkeypatch)
    assert _compose_with_ref_root(pair.split_contract, pair.split, monkeypatch) == flat


# ── I-37 / I-38 / I-39: file suffix, unnamed items, the help text ───────


def test_i37_a_yml_root_stays_yml_and_its_fragments_are_yaml(tmp_path: Path) -> None:
    pair = make_pair(
        tmp_path,
        {"contract.fluid.yml": SPLIT_ALL, "overlays/prod.yaml": PROD_OVERLAY},
        "contract.fluid.yml",
    )

    after = split_and_check(pair, ALL_PARTS, envs=(None, "prod"), cli_envs=(None, "prod"))

    assert sorted(set(after) - set(new_files(pair, after))) == [
        "contract.fluid.yml",
        "overlays/prod.yaml",
    ]
    assert not (pair.split / "contract.fluid.yaml").exists()
    assert all(rel.endswith((".yaml", ".sql", ".md")) for rel in new_files(pair, after))


@pytest.mark.parametrize("parts", [None, "builds,exposes,sql"])
def test_i38_items_without_an_id_are_unnamed_by_position_and_the_dry_run_agrees(
    tmp_path: Path, parts: Optional[str], capsys: pytest.CaptureFixture[str]
) -> None:
    text = """\
id: x
builds:
  - engine: sql
    properties: {sql: "SELECT 1"}
  - id: named
    engine: sql
  - pattern: embedded-logic
exposes:
  - kind: table
  - exposeId: ""
    kind: table
  - exposeId: 0
    kind: table
  - {exposeId: null, kind: table}
"""
    pair = solo(tmp_path, text)
    args = ["--parts", parts] if parts else []
    capsys.readouterr()

    assert split(pair.split_contract, "--dry-run", *args) == 0
    listed = _listed(capsys.readouterr().err)

    assert split(pair.split_contract, *args) == 0

    after = files_of(pair.split)
    assert listed == sorted(set(after) - {NAME})
    expected = {
        "fragments/builds/unnamed-0.yaml",
        "fragments/builds/named.yaml",
        "fragments/builds/unnamed-2.yaml",
        "fragments/exposes/unnamed-0.yaml",
        "fragments/exposes/unnamed-1.yaml",
        "fragments/exposes/unnamed-2.yaml",
        "fragments/exposes/unnamed-3.yaml",
    }
    assert expected <= set(after)
    assert text_of(after, "fragments/builds/unnamed-2.yaml").startswith("# Build: unnamed-2\n\n")
    assert_same_bundle(pair)
    # Running it again names nothing new.
    before = digest(pair.split)
    assert split(pair.split_contract, *args) == 0
    assert digest(pair.split) == before


def test_i39_split_help_lists_the_parts_option_and_every_part(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert run_cli("split", "-h") == 0

    out = capsys.readouterr().out
    assert "--parts" in out and "--dry-run" in out and "--out" in out
    for name in (
        "sovereignty",
        "accessPolicy",
        "builds",
        "exposes",
        "sql",
        "quality",
        "schema",
        "extensions",
        "extensions.<name>",
        "description",
    ):
        assert name in out, name
    assert "sovereignty,accessPolicy,builds,exposes" in out  # the default


# ── I-40: the authoring schema accepts what split writes ────────────────


def _schema_errors(schema: Mapping[str, Any], document: Any) -> List[str]:
    import jsonschema

    validator = jsonschema.validators.validator_for(schema)(schema)
    return sorted(e.message for e in validator.iter_errors(document))


@pytest.mark.parametrize(
    "parts", [None, "builds,exposes,sql", ALL_PARTS], ids=["i01", "i10", "all"]
)
def test_i40_the_authoring_schema_reports_nothing_on_the_root_and_the_fragments(
    tmp_path: Path, parts: Optional[str]
) -> None:
    from fluid_build import schema_authoring

    authoring = json.loads(schema_authoring.authoring_schema_path("0.7.6").read_text("utf-8"))
    pair = make_pair(tmp_path, SPLIT_ALL_FILES)

    after = split_and_check(pair, parts)

    assert _schema_errors(authoring, yaml.safe_load(text_of(after, NAME))) == []
    kinds = {
        "fragments/builds/": "build",
        "fragments/exposes/": "expose",
        "fragments/sovereignty.yaml": "sovereignty",
        "fragments/access-policy.yaml": "accessPolicy",
    }
    for rel, content in after.items():
        for prefix, kind in kinds.items():
            if rel.startswith(prefix) and rel.endswith(".yaml"):
                stands_for = {"$schema": authoring["$schema"], "$defs": authoring["$defs"]}
                errors = _schema_errors(
                    {**stands_for, "$ref": f"#/$defs/{kind}"},
                    yaml.safe_load(content.decode("utf-8")),
                )
                assert errors == [], (rel, errors)


# ── C-24: pointers keep their meaning when the part they sit in, or point into, moves ──

POINTER_CONTRACT = """\
fluidVersion: "0.7.6"
kind: DataProduct
id: ptr.x
name: Ptr
description:
  purpose: "Pointer product"
  usage: {$ref: "#/description/purpose"}
domain: d
metadata: {layer: Silver, owner: {team: t, email: t@example.com}}
extensions:
  paths:
    out: runtime/out/dev.csv
  copyOfParams: {$ref: "#/builds/0/properties/parameters"}
  copyOfBinding: {$ref: "#/exposes/0/binding", format: json}
  sqlCopy: {$ref: "#/builds/0/properties/sql"}
builds:
  - id: transform
    pattern: embedded-logic
    engine: sql
    properties:
      sql: |
        SELECT 1 AS id
      parameters:
        mode: dev
        outPath: {$ref: "#/extensions/paths/out"}
        exposeKind: {$ref: "#/exposes/0/kind"}
exposes:
  - exposeId: out
    kind: table
    version: "1.0.0"
    description: {$ref: "#/description/purpose"}
    binding:
      platform: local
      format: csv
      location:
        path: {$ref: "#/extensions/paths/out"}
    contract:
      schema: [{name: id, type: integer}]
      quality: [{rule: r, expression: "id > 0", severity: error}]
"""
POINTER_OVERLAY = """\
extensions:
  paths:
    out: runtime/out/prod.csv
builds:
  - id: transform
    properties:
      parameters:
        mode: prod
"""
POINTER_FILES = {NAME: POINTER_CONTRACT, "overlays/prod.yaml": POINTER_OVERLAY}


@pytest.mark.parametrize(
    "parts",
    [
        None,
        "builds,exposes",
        "builds,exposes,sql",
        "sql",
        "schema,quality",
        "extensions",
        "description",
        ALL_PARTS,
    ],
)
def test_c24_pointers_in_and_into_moved_parts_resolve_to_the_same_values_in_every_env(
    tmp_path: Path, parts: Optional[str]
) -> None:
    pair = make_pair(tmp_path, POINTER_FILES)

    after = split_and_check(pair, parts, envs=ENVS, cli_envs=CLI_ENVS)

    base = compose_contract(pair.split_contract).document
    prod = compose_contract(pair.split_contract, "prod").document
    # A pointer names a place in the composed contract, whichever file it is in.
    assert base["exposes"][0]["binding"]["location"]["path"] == "runtime/out/dev.csv"
    assert prod["exposes"][0]["binding"]["location"]["path"] == "runtime/out/prod.csv"
    assert prod["builds"][0]["properties"]["parameters"]["outPath"] == "runtime/out/prod.csv"
    assert prod["extensions"]["copyOfParams"]["mode"] == "prod"  # a pointer INTO a build
    assert base["extensions"]["copyOfParams"]["mode"] == "dev"
    assert base["extensions"]["copyOfBinding"] == {
        **base["exposes"][0]["binding"],
        "format": "json",
    }
    assert base["extensions"]["sqlCopy"] == "SELECT 1 AS id\n"
    assert base["builds"][0]["properties"]["parameters"]["exposeKind"] == "table"
    assert base["description"]["usage"] == "Pointer product"
    # And split never rewrote one: every pointer is the text it was.
    pointers = sorted(re.findall(r'\$ref: "(#[^"]*)"', POINTER_CONTRACT))
    written = "\n".join(
        content.decode("utf-8") for rel, content in after.items() if not rel.startswith("overlays/")
    )
    assert sorted(re.findall(r'\$ref: "(#[^"]*)"', written)) == pointers


def test_c24_a_pointer_to_a_part_that_became_a_file_ref_gets_the_file_content(
    tmp_path: Path,
) -> None:
    pair = make_pair(tmp_path, POINTER_FILES)

    after = split_and_check(pair, "builds,exposes,sql")

    assert "sql: {$ref: ../../sql/transform.sql}" in text_of(
        after, "fragments/builds/transform.yaml"
    )
    composed = compose_contract(pair.split_contract).document
    assert composed["extensions"]["sqlCopy"] == "SELECT 1 AS id\n"
    assert yaml.safe_load(text_of(after, NAME))["extensions"]["sqlCopy"] == {
        "$ref": "#/builds/0/properties/sql"
    }


# ── generated roots: many YAML styles, comments in many places ──────────

_GENERATED_PARTS = [
    "sovereignty",
    "accessPolicy",
    "builds",
    "exposes",
    "sql",
    "quality",
    "schema",
    "extensions",
    "description",
    "extensions.acme",
]
_SQL_TEXTS = [
    "SELECT 1",
    "SELECT 1\n",
    "SELECT a,\n       b\nFROM t\n",
    "-- c: x\nSELECT 'it''s' AS \"q\"\nFROM t  \n",
    "WITH x AS (SELECT 1)\n\nSELECT * FROM x\n\n",
    "SELECT '# not a comment'\n",
    "  indented first\nSELECT 2\n",
]


def _generated_contract(rng: random.Random) -> Dict[str, Any]:
    def build(i: int) -> Dict[str, Any]:
        return {
            "id": rng.choice([f"b{i}", f"build.{i}", f"B-{i}"]),
            "pattern": "embedded-logic",
            "engine": "sql",
            "properties": {
                "sql": rng.choice(_SQL_TEXTS),
                "parameters": {"mode": "full", "n": rng.randint(1, 5), "tags": ["a", "b"]},
            },
        }

    def expose(i: int) -> Dict[str, Any]:
        contract: Dict[str, Any] = {
            "schema": [
                {"name": "id", "type": "string", "required": True},
                {"name": "v", "type": "number", "description": "multi word: with colon"},
            ],
            "quality": [{"rule": "r1", "expression": "id IS NOT NULL", "severity": "error"}],
            "dq": {"rules": [{"id": "d1", "type": "completeness", "severity": "error"}]},
        }
        if rng.random() < 0.3:
            del contract["quality"]
        return {
            "exposeId": f"e{i}",
            "kind": "table",
            "version": "1.0.0",
            "binding": {
                "platform": "local",
                "format": "csv",
                "location": {"path": f"out/e{i}.csv"},
            },
            "contract": contract,
        }

    return {
        "fluidVersion": "0.7.6",
        "kind": "DataProduct",
        "id": "p.x",
        "name": "X",
        "description": {
            "purpose": rng.choice(["One row per order.", "p: with colon", "multi\nline\n"]),
            "usage": rng.choice(["Join on id.", "two\nlines", "u"]),
        },
        "domain": "d",
        "metadata": {"layer": "Silver", "owner": {"team": "t", "email": "t@example.com"}},
        "sovereignty": {"jurisdiction": "EU", "allowedRegions": ["a", "b"], "dataResidency": True},
        "accessPolicy": {"grants": [{"principal": "group:a@example.com", "permissions": ["read"]}]},
        "builds": [build(i) for i in range(rng.randint(1, 3))],
        "exposes": [expose(i) for i in range(rng.randint(1, 3))],
        "extensions": {
            "acme": {"region": "eu", "tiers": ["gold", "silver"]},
            "other": {"k": [1, {"z": "y"}]},
        },
    }


def _render(contract: Dict[str, Any], rng: random.Random) -> str:
    style = rng.choice(
        ["block", "flow", "dq", "sq", "indent4", "wide", "narrow", "lit", "fold", "lit4"]
    )
    options: Dict[str, Any] = {
        "sort_keys": False,
        "allow_unicode": True,
        "default_flow_style": False,
    }
    if style == "flow":
        options["default_flow_style"] = True
    elif style == "dq":
        options["default_style"] = '"'
    elif style == "sq":
        options["default_style"] = "'"
    elif style == "indent4":
        options["indent"] = 4
    elif style == "wide":
        options["width"] = 1000
    elif style == "narrow":
        options["width"] = 30
    elif style in ("lit", "fold", "lit4"):

        class _Dumper(yaml.SafeDumper):
            pass

        block = ">" if style == "fold" else "|"

        def _block_strings(dumper: yaml.SafeDumper, data: str) -> Any:
            return dumper.represent_scalar(
                "tag:yaml.org,2002:str", data, style=block if "\n" in data else None
            )

        _Dumper.add_representer(str, _block_strings)
        options["Dumper"] = _Dumper
        if style == "lit4":
            options["indent"] = 4
    return yaml.dump(contract, **options)


def _with_comments(text: str, rng: random.Random, trailing: bool) -> str:
    """Full-line comments at random lines, and trailing ones if ``trailing``.

    Lines inside a block scalar are left alone (a ``#`` there is text).
    """
    lines = text.split("\n")
    inside = [False] * len(lines)
    block_indent: Optional[int] = None
    for n, line in enumerate(lines):
        if block_indent is not None:
            if not line.strip() or len(line) - len(line.lstrip(" ")) > block_indent:
                inside[n] = True
                continue
            block_indent = None
        if re.search(r"(?::|^\s*-)\s+[|>][-+0-9]*\s*$", line):
            lead = re.match(r"^(\s*)((?:- )*)", line)
            assert lead is not None
            block_indent = (
                len(lead.group(1)) + len(lead.group(2)) if lead.group(2) else len(lead.group(1))
            )
    free = [n for n, line in enumerate(lines) if not inside[n] and line.strip()]
    full = set(rng.sample(free, min(len(free), rng.randint(1, 12))))
    tail = set(rng.sample(free, min(len(free), rng.randint(1, 12)))) if trailing else set()
    out = []
    for n, line in enumerate(lines):
        if n in full:
            out.append(" " * (len(line) - len(line.lstrip(" "))) + f"# full-{n}")
        if n in tail and not re.search(r"[|>][-+0-9]*$", line.rstrip()):
            line = f"{line}  # trail-{n}"
        out.append(line)
    return "\n".join(out)


def generated_root(seed: int) -> Tuple[str, List[str], bool]:
    """``(text, parts, crlf)``: a root in a random style, with random comments."""
    rng = random.Random(seed)
    crlf = rng.random() < 0.3
    for _ in range(50):
        text = _with_comments(_render(_generated_contract(rng), rng), rng, trailing=not crlf)
        try:
            yaml.safe_load(text)
        except yaml.YAMLError:
            continue  # a trailing comment landed where YAML does not take one: draw again
        break
    parts = rng.sample(_GENERATED_PARTS, rng.randint(1, len(_GENERATED_PARTS)))
    if rng.random() < 0.2:
        text = text.rstrip("\n")
    if crlf:
        text = text.replace("\n", "\r\n")
    return text, parts, crlf


@pytest.mark.parametrize("seed", range(24))
def test_i02_generated_roots_keep_every_comment_and_the_bundle(tmp_path: Path, seed: int) -> None:
    text, parts, crlf = generated_root(seed)
    pair = solo(tmp_path, text.encode("utf-8"))

    after = split_and_check(pair, ",".join(parts), cli_envs=())

    # A marker is conserved wherever it ends up: as a comment in the root or a
    # fragment, or, when the generator put it inside a multi-line quoted scalar
    # (where it is text, not a comment), in the text file that scalar became.
    marker = re.compile(r"#\s*((?:full|trail)-\d+)")
    kept = sorted(m for content in after.values() for m in marker.findall(content.decode("utf-8")))
    assert kept == sorted(marker.findall(text))
    if crlf:
        assert_one_line_end(after)


def test_i01_a_contract_with_hundreds_of_parts_splits_them_all(tmp_path: Path) -> None:
    n = 60
    lines = ["id: big", "builds:"]
    for i in range(n):
        lines += [
            f"  - id: b{i}",
            "    pattern: embedded-logic",
            "    engine: sql",
            "    properties:",
            "      sql: |",
            f"        SELECT {i}",
        ]
    lines.append("exposes:")
    for i in range(n):
        lines += [
            f"  - exposeId: e{i}",
            "    kind: table",
            "    contract:",
            "      schema:",
            "        - {name: a, type: string}",
            "      quality:",
            "        - {rule: r, expression: a IS NOT NULL, severity: error}",
        ]
    pair = solo(tmp_path, "\n".join(lines) + "\n")
    started = time.monotonic()

    after = split_and_check(pair, "builds,exposes,sql,quality,schema", cli_envs=(None,))

    assert len(new_files(pair, after)) == 5 * n
    assert time.monotonic() - started < 60


def test_the_root_is_never_written_through_a_symlink(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--out`` holding a link at the root's name (dangling or not) is refused before any
    file is written: the undo could remove only the link, not the file it pointed to."""
    source = tmp_path / "src"
    source.mkdir()
    contract = source / "contract.fluid.yaml"
    contract.write_text(
        "id: x\nbuilds:\n  - id: t\n    engine: sql\n    properties: {sql: SELECT 1}\n",
        encoding="utf-8",
    )
    out = tmp_path / "out"
    out.mkdir()
    elsewhere = tmp_path / "elsewhere" / "written.yaml"
    (out / "contract.fluid.yaml").symlink_to(elsewhere)

    assert split(contract, "--out", out) == 1
    assert "symlink" in capsys.readouterr().err
    assert not elsewhere.exists() and not elsewhere.parent.exists()
    assert (out / "contract.fluid.yaml").is_symlink()
    assert sorted(p.name for p in out.iterdir()) == ["contract.fluid.yaml"]


def test_no_fragment_is_written_through_a_symlinked_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """SP-08: ``fragments/`` linking outside the output directory is refused before any file
    or directory is written, there or through it."""
    contract = tmp_path / "contract.fluid.yaml"
    contract.write_text(
        "id: x\nbuilds:\n  - id: t\n    engine: sql\n    properties: {sql: SELECT 1}\n",
        encoding="utf-8",
    )
    original = contract.read_bytes()
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (tmp_path / "fragments").symlink_to(outside)

    assert split(contract) == 1
    assert "symlink" in capsys.readouterr().err
    assert list(outside.iterdir()) == []
    assert contract.read_bytes() == original


@pytest.mark.parametrize("char", ["\u0085", "\u2028", "\u2029"], ids=["NEL", "LS", "PS"])
def test_a_data_split_keeps_next_line_characters(tmp_path: Path, char: str) -> None:
    """The data fallback (a JSON root) writes text holding NEL, LS or PS escaped, as the
    bundle does, so the split composes to the original instead of being refused."""
    contract = tmp_path / "contract.fluid.json"
    sql = f"SELECT 'a{char}b'"
    contract.write_text(
        json.dumps({"id": "x", "builds": [{"id": "t", "properties": {"sql": sql}}]}),
        encoding="utf-8",
    )
    assert split(contract, "--parts", "builds") == 0
    from fluid_build.loader import load_contract

    assert load_contract(contract)["builds"][0]["properties"]["sql"] == sql


def test_a_bare_cr_file_is_split_as_data(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Old Mac line ends: the text splice cannot place them, so the data split runs, saying
    the root's comments were not kept, and the result composes to the original."""
    contract = tmp_path / "contract.fluid.yaml"
    contract.write_bytes(b"id: x\rbuilds:\r  - id: t\r    engine: sql\r")
    from fluid_build.loader import load_contract

    before = load_contract(contract)
    assert split(contract, "--parts", "builds") == 0
    assert "bare CR" in capsys.readouterr().err
    assert load_contract(contract) == before


def test_a_next_line_break_in_a_yaml_root_is_split_as_data(tmp_path: Path) -> None:
    """NEL used as a YAML line break: the text splice cannot place it, so the data split
    runs, and the result composes to the original."""
    contract = tmp_path / "contract.fluid.yaml"
    contract.write_bytes("id: x\u0085builds:\n  - id: t\n    engine: sql\n".encode("utf-8"))
    from fluid_build.loader import load_contract

    before = load_contract(contract)
    assert split(contract, "--parts", "builds") == 0
    assert load_contract(contract) == before
