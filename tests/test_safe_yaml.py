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

from fluid_build.util.safe_yaml import MAX_YAML_ALIASES, UnsafeYamlError, load_yaml_safe


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
