# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Finding the fidelity block in a FLUX bundle directory."""

from __future__ import annotations

import pytest

from fluid_build.profiler import bundle
from fluid_build.profiler.bundle import BundleError, read_bundle_block

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


def test_the_yaml_suffix_is_read_too_and_the_name_is_matched_without_case(tmp_path):
    _write(tmp_path, "Simulation.FLUX.YAML", SIMULATION)
    assert read_bundle_block(tmp_path, "orders").simulation == "Simulation.FLUX.YAML"


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
    assert "simulation.flux.yml" in str(refused.value) and "line" in str(refused.value)
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
