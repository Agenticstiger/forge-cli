# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Finding the fidelity block in a FLUX bundle directory."""

from __future__ import annotations

import pytest

from fluid_build.profiler import _stats, bundle
from fluid_build.profiler.bundle import BundleError

flux_stats = pytest.importorskip("flux_stats")

pytestmark = pytest.mark.unit

SIMULATION = """\
fluxVersion: "0.5.0"
kind: Simulation
id: sim-orders
extensions:
  com.example.flux/1: {audit: true}
  com.agentics.flux/1:
    emits:
      orders:
        fidelity:
          encoding: {fractionalBits: 8, rounding: floor}
          columns: {amount: {range: {min: "0", max: "100"}}}
      refunds: {rows: 10}
"""


def read_bundle_block(directory, expose_id):
    """The bundle's block, read through flux-stats' exact-YAML reader as the CLI reads it."""
    return bundle.read_bundle_block(directory, expose_id, _stats.load_backend())


def _write(directory, name, text):
    (directory / name).write_text(text, encoding="utf-8")


def test_the_block_of_the_one_simulation_is_returned_unparsed(tmp_path):
    _write(tmp_path, "simulation.flux.yml", SIMULATION)
    _write(tmp_path, "orders.fluid.yml", "kind: DataProduct\n")
    _write(tmp_path, "world.flux.yml", "kind: World\nid: w\n")
    found = read_bundle_block(tmp_path, "orders")
    assert found.simulation == "simulation.flux.yml"
    assert found.expose_id == "orders"
    assert found.raw["encoding"] == {"fractionalBits": 8, "rounding": "floor"}


def test_the_yaml_suffix_is_read_too_and_matched_as_the_engine_globs_it(tmp_path):
    _write(tmp_path, "Simulation.flux.yaml", SIMULATION)
    assert read_bundle_block(tmp_path, "orders").simulation == "Simulation.flux.yaml"
    _write(tmp_path, "Other.FLUX.YML", SIMULATION)  # not a *.flux.yml name: not read
    assert read_bundle_block(tmp_path, "orders").simulation == "Simulation.flux.yaml"


def test_a_simulation_after_another_document_in_one_file_is_read(tmp_path):
    _write(tmp_path, "s.flux.yml", "kind: Segment\nid: seg\n---\n" + SIMULATION)
    assert read_bundle_block(tmp_path, "orders").simulation == "s.flux.yml"


def test_an_integer_expose_key_names_the_expose_as_the_engine_reads_it(tmp_path):
    _write(tmp_path, "s.flux.yml", SIMULATION.replace("      orders:", "      2024:"))
    block = read_bundle_block(tmp_path, "2024").raw
    assert block["encoding"] == {"fractionalBits": 8, "rounding": "floor"}


def test_a_directory_with_no_simulation_is_refused(tmp_path):
    _write(tmp_path, "world.flux.yml", "kind: World\n")
    _write(tmp_path, "notes.yml", SIMULATION)  # not a FLUX document by its name
    with pytest.raises(BundleError) as refused:
        read_bundle_block(tmp_path, "orders")
    assert refused.value.event == "profile_bundle_no_simulation"
    assert "next" in refused.value.context


def test_two_simulations_are_refused_and_named(tmp_path):
    _write(tmp_path, "a.flux.yml", SIMULATION)
    _write(tmp_path, "b.flux.yml", SIMULATION)
    with pytest.raises(BundleError) as refused:
        read_bundle_block(tmp_path, "orders")
    assert refused.value.event == "profile_bundle_many_simulations"
    assert refused.value.context["simulations"] == "a.flux.yml, b.flux.yml"


@pytest.mark.parametrize("expose", ["refunds", "absent"])
def test_an_expose_without_a_block_is_refused(tmp_path, expose):
    _write(tmp_path, "simulation.flux.yml", SIMULATION)
    with pytest.raises(BundleError) as refused:
        read_bundle_block(tmp_path, expose)
    assert refused.value.event == "profile_bundle_no_block"
    assert "simulation.flux.yml" in str(refused.value) and expose in str(refused.value)


def test_a_simulation_with_no_extension_namespace_is_refused(tmp_path):
    _write(tmp_path, "simulation.flux.yml", "kind: Simulation\nid: s\n")
    with pytest.raises(BundleError) as refused:
        read_bundle_block(tmp_path, "orders")
    assert refused.value.event == "profile_bundle_no_block"


def test_a_block_that_is_not_a_mapping_is_refused(tmp_path):
    text = SIMULATION.replace("fidelity:\n", "fidelity: oops\n#", 1)
    _write(tmp_path, "simulation.flux.yml", text.split("#")[0] + "      refunds: {rows: 1}\n")
    with pytest.raises(BundleError) as refused:
        read_bundle_block(tmp_path, "orders")
    assert refused.value.event == "profile_bundle_no_block"
    assert "not a mapping" in str(refused.value)


def test_an_unreadable_document_names_the_file_and_the_line_and_quotes_nothing(tmp_path):
    _write(tmp_path, "simulation.flux.yml", "kind: Simulation\nsecret: [unclosed value-77\n")
    with pytest.raises(BundleError) as refused:
        read_bundle_block(tmp_path, "orders")
    assert refused.value.event == "profile_bundle_unreadable"
    assert "simulation.flux.yml" in str(refused.value) and "unreadable YAML" in str(refused.value)
    assert "value-77" not in str(refused.value)


def test_a_document_that_is_not_text_is_refused(tmp_path):
    (tmp_path / "simulation.flux.yml").write_bytes(b"\xff\xfe\x00bad")
    with pytest.raises(BundleError) as refused:
        read_bundle_block(tmp_path, "orders")
    assert refused.value.event == "profile_bundle_unreadable"


def test_a_document_that_is_too_large_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(bundle, "_MAX_BYTES", 10)
    _write(tmp_path, "simulation.flux.yml", SIMULATION)
    with pytest.raises(BundleError) as refused:
        read_bundle_block(tmp_path, "orders")
    assert refused.value.event == "profile_bundle_unreadable"


def test_a_linked_document_is_not_read(tmp_path):
    target = tmp_path / "elsewhere.txt"
    target.write_text(SIMULATION, encoding="utf-8")
    (tmp_path / "simulation.flux.yml").symlink_to(target)
    with pytest.raises(BundleError) as refused:
        read_bundle_block(tmp_path, "orders")
    assert refused.value.event == "profile_bundle_no_simulation"


def _simulation_with(block_text):
    indented = "".join(f"          {line}\n" for line in block_text.strip("\n").split("\n"))
    return (
        "kind: Simulation\nid: s\nextensions:\n  com.agentics.flux/1:\n    emits:\n"
        f"      orders:\n        fidelity:\n{indented}"
    )


def test_a_bare_null_key_is_the_string_null(tmp_path):
    _write(tmp_path, "s.flux.yml", _simulation_with("columns:\n  aborted_at: {null: fail}"))
    block = read_bundle_block(tmp_path, "orders").raw
    assert block == {"columns": {"aborted_at": {"null": "fail"}}}


def test_yes_and_true_keys_read_as_the_json_spellings(tmp_path):
    _write(tmp_path, "s.flux.yml", _simulation_with("columns: {yes: 1, off: 2, 7: 3}"))
    assert read_bundle_block(tmp_path, "orders").raw == {"columns": {"7": 3, "false": 2, "true": 1}}


def test_a_bare_float_is_its_source_text(tmp_path):
    _write(
        tmp_path,
        "s.flux.yml",
        _simulation_with("properties: [{level: 0.5, tolerance: 1.10, big: 1e3}]"),
    )
    (entry,) = read_bundle_block(tmp_path, "orders").raw["properties"]
    assert entry["level"] == "0.5"
    assert entry["tolerance"] == "1.10"
    assert isinstance(entry["big"], str)


def test_an_unquoted_timestamp_stays_the_string_written(tmp_path):
    _write(
        tmp_path,
        "s.flux.yml",
        _simulation_with("columns: {at: {range: {min: 2024-01-01T00:00:00Z, max: 2024-02-01}}}"),
    )
    got = read_bundle_block(tmp_path, "orders").raw["columns"]["at"]["range"]
    assert got == {"max": "2024-02-01", "min": "2024-01-01T00:00:00Z"}


def test_strings_and_keys_are_nfc(tmp_path):
    nfd = "cafe\u0301"
    _write(tmp_path, "s.flux.yml", _simulation_with(f'columns: {{"{nfd}": {{note: "{nfd}"}}}}'))
    assert read_bundle_block(tmp_path, "orders").raw == {
        "columns": {"caf\u00e9": {"note": "caf\u00e9"}}
    }


def test_a_bare_float_key_is_its_decimal_spelling_as_the_engine_reads_it(tmp_path):
    _write(tmp_path, "s.flux.yml", _simulation_with("columns: {0.50: x}"))
    assert read_bundle_block(tmp_path, "orders").raw == {"columns": {"0.50": "x"}}


def test_a_binary_float_key_is_refused_and_names_the_file():
    # The loader never builds a float, so this is the canonical form's own guard.
    with pytest.raises(_stats.BundleTextRefused) as refused:
        _stats.load_backend().canonicalise({0.5: "x"}, name="s.flux.yml: fidelity")
    assert "s.flux.yml" in str(refused.value) and "float" in str(refused.value)
    assert refused.value.code == "FLX-1001"


def test_an_integer_outside_the_safe_range_is_refused(tmp_path):
    _write(tmp_path, "s.flux.yml", _simulation_with("rows: 9007199254740993"))
    with pytest.raises(BundleError) as refused:
        read_bundle_block(tmp_path, "orders")
    assert refused.value.event == "profile_bundle_unreadable"
    assert "s.flux.yml" in str(refused.value) and "integer" in str(refused.value)


def test_an_integer_too_long_to_form_is_refused_with_its_position(tmp_path):
    _write(tmp_path, "s.flux.yml", _simulation_with("rows: " + "9" * 5000))
    with pytest.raises(BundleError) as refused:
        read_bundle_block(tmp_path, "orders")
    assert "too long" in str(refused.value) and "line" in str(refused.value)


def test_a_block_whose_aliases_write_out_too_many_values_is_refused(tmp_path):
    lines = ['a: &a ["x", "x", "x", "x", "x", "x", "x", "x", "x", "x"]']
    for name, previous in zip("bcdef", "abcde", strict=True):
        lines.append(f"{name}: &{name} [{', '.join([f'*{previous}'] * 10)}]")
    _write(tmp_path, "s.flux.yml", _simulation_with("\n".join(lines)))
    with pytest.raises(BundleError) as refused:
        read_bundle_block(tmp_path, "orders")
    assert refused.value.event == "profile_bundle_unreadable"
    assert "more than 1000000 values" in str(refused.value)


def test_a_block_that_holds_itself_through_an_alias_is_refused(tmp_path):
    _write(tmp_path, "s.flux.yml", _simulation_with("r: &r [*r]"))
    with pytest.raises(BundleError) as refused:
        read_bundle_block(tmp_path, "orders")
    assert "holds itself through an alias" in str(refused.value)


def test_a_number_too_long_to_write_out_is_refused(tmp_path):
    _write(tmp_path, "s.flux.yml", _simulation_with("columns: {amount: {max: 1.0e+300000000}}"))
    with pytest.raises(BundleError) as refused:
        read_bundle_block(tmp_path, "orders")
    assert "more than 1000 digits" in str(refused.value) and "line" in str(refused.value)


def test_a_document_nested_too_deeply_to_read_is_refused(tmp_path):
    _write(tmp_path, "deep.flux.yml", "kind: Simulation\nx: " + "[" * 2000 + "]" * 2000 + "\n")
    with pytest.raises(BundleError) as refused:
        read_bundle_block(tmp_path, "orders")
    assert refused.value.event == "profile_bundle_unreadable"
    assert "nests too deeply" in str(refused.value)


def test_a_merge_key_chain_that_writes_out_too_many_pairs_is_refused(tmp_path):
    # Any *.flux.yml is read, so the chain need not sit in the Simulation.
    _write(tmp_path, "bomb.flux.yml", "\n".join(_merge_chain(29)) + "\n")
    _write(tmp_path, "s.flux.yml", SIMULATION)
    with pytest.raises(BundleError) as refused:
        read_bundle_block(tmp_path, "orders")
    assert refused.value.event == "profile_bundle_unreadable"
    assert "bomb.flux.yml" in str(refused.value) and "merge key" in str(refused.value)


def _merge_chain(levels):
    """A document whose mapping a{n} merges a{n-1} twice: 2^(n+1) pairs at level n."""
    lines = ["kind: Segment", "a0: &a0 {k0: 1, k1: 2}"]
    lines += [f"a{n}: &a{n} {{<<: [*a{n - 1}, *a{n - 1}]}}" for n in range(1, levels + 1)]
    return lines


def test_merges_that_each_stay_under_the_bound_are_counted_together(tmp_path):
    # Each mapping below merges 2,048 pairs, far under the bound, and 50 of them pass it.
    lines = _merge_chain(10) + ["b: [" + ", ".join(["{<<: [*a10]}"] * 50) + "]"]
    _write(tmp_path, "wide.flux.yml", "\n".join(lines) + "\n")
    _write(tmp_path, "s.flux.yml", SIMULATION)
    with pytest.raises(BundleError) as refused:
        read_bundle_block(tmp_path, "orders")
    assert "wide.flux.yml" in str(refused.value) and "merge keys" in str(refused.value)


def test_merges_in_different_documents_of_a_bundle_are_counted_together(tmp_path):
    # 65,532 pairs a document: under the bound alone, past it in the second.
    for name in ("one.flux.yml", "two.flux.yml"):
        _write(tmp_path, name, "\n".join(_merge_chain(14)) + "\n")
    _write(tmp_path, "s.flux.yml", SIMULATION)
    with pytest.raises(BundleError) as refused:
        read_bundle_block(tmp_path, "orders")
    assert "two.flux.yml" in str(refused.value) and "merge keys" in str(refused.value)


def test_a_merge_key_inside_the_bound_reads_as_the_engine_reads_it(tmp_path):
    _write(
        tmp_path,
        "s.flux.yml",
        _simulation_with("base: &base {fractionalBits: 8}\nencoding: {<<: *base, rounding: floor}"),
    )
    block = read_bundle_block(tmp_path, "orders").raw
    assert block["encoding"] == {"fractionalBits": 8, "rounding": "floor"}


def test_the_module_imports_no_registry():
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(bundle))
    names = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert not names & {"flux_stats", "flux_engine"}
