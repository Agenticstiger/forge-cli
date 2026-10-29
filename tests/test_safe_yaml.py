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

"""Tests for ``fluid_build.util.safe_yaml`` — billion-laughs / anchor-bomb
denial-of-service defense for untrusted YAML (G1)."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
import yaml

from fluid_build.util.safe_yaml import (
    MAX_YAML_ALIASES,
    DuplicateKeyError,
    UnsafeYamlError,
    load_json_unique,
    load_yaml_safe,
)


def test_loads_plain_yaml() -> None:
    assert load_yaml_safe("a: 1\nb: [x, y]") == {"a": 1, "b": ["x", "y"]}


def test_none_returns_none() -> None:
    assert load_yaml_safe(None) is None


def test_accepts_bytes_and_streams() -> None:
    assert load_yaml_safe(b"k: v") == {"k": "v"}
    assert load_yaml_safe(io.StringIO("k: v")) == {"k": "v"}


def test_a_few_aliases_are_allowed() -> None:
    """Legitimate, modest anchor reuse must still parse."""
    doc = "anchor: &a value\nref1: *a\nref2: *a"
    assert load_yaml_safe(doc) == {"anchor": "value", "ref1": "value", "ref2": "value"}


def test_rejects_oversized_input() -> None:
    huge = "x: " + ("a" * (6 * 1024 * 1024))
    with pytest.raises(UnsafeYamlError):
        load_yaml_safe(huge)


def test_rejects_alias_bomb() -> None:
    """A classic billion-laughs payload — nested anchors each referenced
    many times — is rejected before the expensive compose step."""
    lines = ["l0: &l0 [a, a, a, a, a, a, a, a, a]"]
    prev = "l0"
    for i in range(1, 9):
        cur = f"l{i}"
        refs = ", ".join([f"*{prev}"] * 9)
        lines.append(f"{cur}: &{cur} [{refs}]")
        prev = cur
    with pytest.raises(UnsafeYamlError):
        load_yaml_safe("\n".join(lines))


def test_alias_cap_boundary() -> None:
    """One alias reference over the cap is enough to reject the document."""
    refs = "\n".join(f"r{i}: *a" for i in range(MAX_YAML_ALIASES + 1))
    with pytest.raises(UnsafeYamlError):
        load_yaml_safe("a: &a 1\n" + refs)


def _doubling_chain(levels: int) -> str:
    """Each level is a list of two aliases to the level below: 2 aliases per
    level, 2**levels leaves. 24 levels stay under the 50-alias cap."""
    lines = ["l0: &l0 [lol]"]
    for i in range(1, levels + 1):
        lines.append(f"l{i}: &l{i} [*l{i - 1}, *l{i - 1}]")
    return "\n".join(lines)


def test_rejects_doubling_chain_under_the_alias_cap() -> None:
    """48 aliases pass a count cap of 50 yet expand to 2**24 leaves (100,663,294
    nodes); a 44-alias chain like this one held `fluid validate` for over 90 s."""
    doc = _doubling_chain(24)
    assert doc.count("*") <= MAX_YAML_ALIASES
    with pytest.raises(UnsafeYamlError, match="expand"):
        load_yaml_safe(doc)


def test_recursive_alias_is_left_to_its_consumers() -> None:
    """The expansion walk counts an alias inside its own anchor once and ends.
    The document still loads as the cycle it is: the contract API refuses it
    with its documented ``contract_load_failed``."""
    data = load_yaml_safe("a: &a [x, *a]")
    assert data["a"][1] is data["a"]


def test_recursive_alias_does_not_hide_an_expansion() -> None:
    doc = _doubling_chain(24) + "\nloop: &loop [*l24, *loop]"
    with pytest.raises(UnsafeYamlError, match="expand"):
        load_yaml_safe(doc)


def test_budget_counts_what_aliases_add_not_document_size() -> None:
    """One alias sends this 800-node document through the expansion walk; it
    adds one node, so it loads under a budget of 50."""
    doc = "anchor: &a x\nref: *a\n" + "\n".join(f"k{i}: [a, b]" for i in range(200))
    assert load_yaml_safe(doc, max_alias_expansion=50)["ref"] == "x"


def test_ratio_cap_refuses_a_chain_under_the_node_budget() -> None:
    """13 levels add 49,120 nodes, inside the 100,000 default, but grow a 30-node
    document more than 1,600-fold, so the ratio rule refuses it on its own."""
    try:
        load_yaml_safe(_doubling_chain(13))
    except UnsafeYamlError as refused:
        assert "from 30 to 49150 nodes" in str(refused)
    else:
        pytest.fail("a 1,638-fold expansion loaded")


@pytest.mark.parametrize("refs, refused", [(39, False), (45, True)])
def test_default_expansion_cap(refs: int, refused: bool) -> None:
    """A 2,501-node block aliased 39 times adds 97,539 nodes; 45 times adds
    112,545, over the 100,000 default. Neither trips the ratio rule."""
    block = "{" + ", ".join(f"f{i}: v" for i in range(1250)) + "}"
    doc = f"base: &b {block}\n" + "\n".join(f"r{i}: *b" for i in range(refs))
    if refused:
        with pytest.raises(UnsafeYamlError, match="expand"):
            load_yaml_safe(doc)
    else:
        assert len(load_yaml_safe(doc)) == refs + 1


@pytest.mark.parametrize(
    "tag", ["!!python/object/apply:os.getcwd []", "!!python/name:os.getcwd ''"]
)
def test_alias_path_constructs_safe_tags_only(tag: str) -> None:
    """With an alias the document is composed and constructed by hand; that
    path must refuse Python object tags exactly as ``yaml.safe_load`` does."""
    with pytest.raises(yaml.YAMLError):
        load_yaml_safe(f"a: &x 1\nb: *x\nc: {tag}")


def test_stream_is_read_only_up_to_the_cap() -> None:
    """A stream is refused after one unit past the cap, not read whole."""

    class _Stream:
        def __init__(self) -> None:
            self.sizes: list = []

        def read(self, size: int = -1) -> str:
            self.sizes.append(size)
            return "x" * (size if size > 0 else 10_000_000)

    stream = _Stream()
    with pytest.raises(UnsafeYamlError, match="safety cap"):
        load_yaml_safe(stream, max_bytes=1_000)
    assert stream.sizes == [1_001]


def test_short_reads_are_read_to_the_end() -> None:
    """A raw stream may return fewer units than asked before its end; the
    bounded read keeps reading instead of parsing the first chunk."""

    class _Trickle(io.RawIOBase):
        def __init__(self, data: bytes) -> None:
            self.data = data

        def readable(self) -> bool:
            return True

        def read(self, size: int = -1) -> bytes:
            chunk, self.data = self.data[:7], self.data[7:]
            return chunk

    assert load_yaml_safe(_Trickle(b"name: a-long-enough-value\nid: x\n")) == {
        "name": "a-long-enough-value",
        "id": "x",
    }
    with pytest.raises(UnsafeYamlError, match="safety cap"):
        load_yaml_safe(_Trickle(b"k: " + b"v" * 100), max_bytes=50)


@pytest.mark.parametrize("as_bytes", [False, True], ids=["str", "bytes"])
def test_size_cap_counts_utf8_bytes(as_bytes: bool) -> None:
    """400 three-byte characters are 1,203 bytes: over a 1,000-byte cap
    whether they arrive as text or as bytes."""
    doc = "k: " + "\u20ac" * 400
    with pytest.raises(UnsafeYamlError, match="1000-byte"):
        load_yaml_safe(doc.encode("utf-8") if as_bytes else doc, max_bytes=1_000)
    assert load_yaml_safe("k: " + "\u20ac" * 300, max_bytes=1_000) == {"k": "\u20ac" * 300}


def test_depth_cap_boundary() -> None:
    """A document nested to the cap loads; one level more is refused, not
    left to PyYAML's ``RecursionError``, and so is one 20,000 levels deep,
    which PyYAML's scanner took tens of seconds to read to the end.
    ``a: [[1]]`` is three levels."""
    assert load_yaml_safe("[" * 128 + "]" * 128) is not None
    for depth in (129, 20_000):
        with pytest.raises(UnsafeYamlError, match="more than 128 levels"):
            load_yaml_safe("[" * depth + "]" * depth)
    assert load_yaml_safe("a: [[1]]", max_depth=3) == {"a": [[1]]}
    with pytest.raises(UnsafeYamlError, match="more than 2 levels"):
        load_yaml_safe("a: [[1]]", max_depth=2)


def test_aliases_repeating_a_long_string_are_refused() -> None:
    """Twenty aliases add few nodes but repeat a 1,000,000-character string
    twenty times; the node budget alone let it through."""
    big = "x" * 1_000_000
    doc = f"s: &s {big}\nl1: &l1 [*s, *s, *s, *s]\nl2: &l2 [*l1, *l1, *l1, *l1]\n"
    with pytest.raises(UnsafeYamlError, match="repeat 20000000 characters"):
        load_yaml_safe(doc)


def test_character_budget_boundary() -> None:
    """Two aliases to a ten-character string add 20 characters: allowed at
    20, not at 19."""
    doc = "a: &a abcdefghij\nb: *a\nc: *a\n"
    assert load_yaml_safe(doc, max_alias_expansion_chars=20)["c"] == "abcdefghij"
    with pytest.raises(UnsafeYamlError, match="repeat 20 characters"):
        load_yaml_safe(doc, max_alias_expansion_chars=19)


def test_reusing_a_block_is_allowed() -> None:
    """Ordinary DRY reuse of one anchored block stays well inside the budget."""
    block = "{" + ", ".join(f"f{i}: v{i}" for i in range(100)) + "}"
    refs = "\n".join(f"r{i}: *b" for i in range(40))
    data = load_yaml_safe(f"base: &b {block}\n{refs}")
    assert data["r39"] == data["base"]


def test_expansion_budget_boundary() -> None:
    """Five aliases to a ten-item list add 55 nodes: allowed at 55, not at 54."""
    doc = "base: &b [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]\n" + "\n".join(f"r{i}: *b" for i in range(5))
    assert load_yaml_safe(doc, max_alias_expansion=55)["r4"] == list(range(1, 11))
    with pytest.raises(UnsafeYamlError):
        load_yaml_safe(doc, max_alias_expansion=54)


def test_odcs_reader_applies_the_caps(tmp_path: Path) -> None:
    """ODCS/ODPS files come from other teams and catalogs; the reader the
    Bitol import, the contractId resolver and ``--seed-from`` share used a
    plain ``yaml.safe_load``."""
    from fluid_build.providers.odcs.io import read_input

    path = tmp_path / "bomb.odcs.yaml"
    path.write_text(
        "apiVersion: v3.1.0\nkind: DataContract\n" + _doubling_chain(18), encoding="utf-8"
    )
    with pytest.raises(UnsafeYamlError):
        read_input(path)


def test_callers_report_a_refused_file_as_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Callers that caught ``yaml.YAMLError`` alone crashed with
    ``UnsafeYamlError``; the expansion budget refuses more inputs than the
    alias count did, so each must report the file as it reports bad YAML."""
    import logging
    import sys
    import types

    from fluid_build.forge.core import artifact_fanout, validators
    from fluid_build.forge.federation import load_federation_manifest

    bomb = _doubling_chain(13)
    contract = tmp_path / "contract.fluid.yaml"
    contract.write_text(bomb, encoding="utf-8")
    assert artifact_fanout._contract_is_reference_only(contract) is False
    assert artifact_fanout._load_schedule_contract(contract, None, logging.getLogger()) is None
    # Every emitter loads through _load_contract; `fluid generate artifacts`
    # reports a FanoutError and nothing else.
    with pytest.raises(artifact_fanout.FanoutError, match="did not parse"):
        artifact_fanout._load_contract(contract)

    manifest = tmp_path / "federation" / "upstreams.yaml"
    manifest.parent.mkdir()
    manifest.write_text(bomb, encoding="utf-8")
    assert load_federation_manifest(tmp_path).workspaces == []

    # The parse runs before the validator library is used.
    monkeypatch.setitem(sys.modules, "openapi_spec_validator", types.ModuleType("stub"))
    monkeypatch.setattr(validators, "_openapi_validator_available", lambda: True)
    issues = validators.validate_openapi("api.yaml", bomb.encode(), strict=True)
    assert [issue.code for issue in issues] == ["OAS-PARSE"]


# ── unique_keys: the strict mode the contract composition reads with (SG-18) ──


def test_a_duplicate_key_keeps_the_last_value_by_default() -> None:
    """Every reader but the composition keeps PyYAML's last-wins, unchanged."""
    assert load_yaml_safe("a: 1\na: 2\n") == {"a": 2}
    assert load_yaml_safe("x: &x 1\na: *x\na: 2\n") == {"x": 1, "a": 2}


@pytest.mark.parametrize(
    ("text", "line", "key"),
    [
        ("a: 1\nb: 2\na: 3\n", 3, "a"),
        ("x:\n  y: 1\n  z:\n    k: 1\n    k: 2\n", 5, "k"),
        ("- {a: 1, a: 2}\n", 1, "a"),
        ("x:\n  $ref: ./a.yaml\n  $ref: ./b.yaml\n", 3, "$ref"),
        ("a: &x {k: 1}\nb: *x\nb: 2\n", 3, "b"),
        ("1: one\n1: uno\n", 2, "1"),
    ],
    ids=["top-level", "nested", "flow", "two-refs", "with-aliases", "int-key"],
)
def test_unique_keys_refuses_a_key_written_twice_naming_the_line(
    text: str, line: int, key: str
) -> None:
    with pytest.raises(DuplicateKeyError) as info:
        load_yaml_safe(text, unique_keys=True, name="frag.yaml")

    assert str(info.value) == (
        f"frag.yaml:{line}: duplicate key {key!r} (YAML 1.2 requires unique keys); "
        "remove one of them"
    )
    assert (info.value.key, info.value.line, info.value.name) == (key, line, "frag.yaml")
    assert isinstance(info.value, UnsafeYamlError), "a refused input, as the caps are"


def test_unique_keys_compares_keys_as_constructed() -> None:
    """YAML 1.1 reads ``on`` and ``yes`` as the one key ``True``: a dict keeps one."""
    with pytest.raises(DuplicateKeyError) as info:
        load_yaml_safe("on: 1\nyes: 2\n", unique_keys=True)

    assert str(info.value) == (
        "line 2: duplicate key 'yes' (the same key as 'on' at line 1; YAML 1.2 requires "
        "unique keys); remove one of them"
    )


@pytest.mark.parametrize(
    "text",
    [
        "{=: 1}\n",
        "=: 1\na: 2\n",
        "ops:\n  =: eq\n  '!=': ne\n",
        "base: &b {=: 1}\nuse: {<<: *b, =: 2}\n",
        "? !!value =\n: 1\n",
    ],
    ids=["flow", "block", "nested", "merged-then-overridden", "explicit-tag"],
)
def test_unique_keys_reads_a_plain_equals_key_as_stock_pyyaml_does(text: str) -> None:
    """YAML 1.1 tags a plain ``=`` key ``tag:yaml.org,2002:value``, and PyYAML's
    ``flatten_mapping`` makes it the string ``"="``. The duplicate check runs before that,
    so it must read the key as it becomes: SafeLoader has no constructor for the tag, and
    constructing it refused every file holding ``{=: 1}``, which ``yaml.safe_load`` reads."""
    assert load_yaml_safe(text, unique_keys=True) == yaml.safe_load(text)


@pytest.mark.parametrize("text", ['=: 1\n"=": 2\n', "'=': 1\n=: 2\n", "{=: 1, =: 2}\n"])
def test_unique_keys_refuses_a_plain_equals_key_beside_the_same_key(text: str) -> None:
    """A plain ``=`` is the key ``"="``: written beside a quoted one, a dict keeps one."""
    assert len(yaml.safe_load(text)) == 1, "stock PyYAML drops one of the two"

    with pytest.raises(DuplicateKeyError) as info:
        load_yaml_safe(text, unique_keys=True, name="f")

    assert (info.value.key, info.value.line) == ("=", text.count("\n"))


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("d: &d {a: 1, b: 2}\nitem:\n  <<: *d\n  b: 3\n", {"a": 1, "b": 3}),
        ("d: &d {a: 1}\nitem:\n  b: 3\n  <<: *d\n", {"a": 1, "b": 3}),
        ("d: &d {a: 1}\ne: &e {a: 2}\nitem:\n  <<: [*d, *e]\n", {"a": 1}),
    ],
    ids=["override", "merge-last", "merge-list"],
)
def test_unique_keys_lets_a_mapping_override_what_a_merge_brings_in(
    text: str, expected: object
) -> None:
    assert load_yaml_safe(text, unique_keys=True)["item"] == expected


def test_unique_keys_reads_a_merge_source_written_inline_and_used_again() -> None:
    """A mapping merged into another is flattened there first, in place: checked in
    ``construct_mapping`` (the usual PyYAML #165 hook), ``*mid`` would be refused for
    the merged ``a`` beside its own."""
    text = "base: &base {a: 1}\ntop:\n  <<: &mid\n    <<: *base\n    a: 2\n  b: 3\nother: *mid\n"

    assert load_yaml_safe(text, unique_keys=True) == {
        "base": {"a": 1},
        "top": {"a": 2, "b": 3},
        "other": {"a": 2},
    }


def test_unique_keys_applies_every_merge_key_of_a_mapping() -> None:
    """A ``<<`` is a merge directive, not a data key: a mapping may write several, each
    merge applies as PyYAML applies it (a later ``<<`` wins a key two of them bring),
    and nothing is dropped. yamllint allows them by default too."""
    defaults = "a: &a {x: 1, k: from-a}\nb: &b {y: 2, k: from-b}\n"

    merged = load_yaml_safe(defaults + "m:\n  <<: *a\n  <<: *b\n  z: 3\n", unique_keys=True)
    assert merged["m"] == {"x": 1, "y": 2, "k": "from-b", "z": 3}

    explicit = load_yaml_safe(defaults + "m:\n  <<: *a\n  <<: *b\n  k: own\n", unique_keys=True)
    assert explicit["m"] == {"x": 1, "y": 2, "k": "own"}, "a key the mapping writes wins"

    with pytest.raises(DuplicateKeyError) as info:
        load_yaml_safe(
            defaults + "m:\n  <<: *a\n  z: 3\n  <<: *b\n  z: 4\n", unique_keys=True, name="f"
        )
    assert str(info.value) == (
        "f:7: duplicate key 'z' (YAML 1.2 requires unique keys); remove one of them"
    ), "a data key written twice beside the merges is still refused"


def test_load_json_unique_refuses_a_name_written_twice() -> None:
    assert load_json_unique('{"a": [{"b": 1}], "c": null}') == {"a": [{"b": 1}], "c": None}
    with pytest.raises(DuplicateKeyError) as info:
        load_json_unique('{"a": {"k": 1, "k": 2}}', name="f.json")

    assert str(info.value) == (
        "f.json: duplicate key 'k' (the names of a JSON object must be unique: RFC 8259 "
        "leaves a repeated one undefined); remove one of them"
    )
    assert info.value.line is None
