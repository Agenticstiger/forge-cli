"""A local ``location.path`` with ``{{ env.NAME }}`` names the same file for every stage.

The build runner resolves ``{{ env.NAME }}`` before it writes a landed file.
``fluid verify``, ``fluid diff`` and the local provider read the path through
``fluid_build.util.binding_paths.resolve_binding_path``, which did not, so a
contract whose data lives under a shared directory named by a variable
(``path: "{{ env.FLUID_DATA_DIR }}/product/product.parquet"``) had its file
written by stage 7 and reported missing by stage 9 ("Output file not found:
<contract dir>/{{ env.FLUID_DATA_DIR }}/...").
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fluid_build.build_runners.base import _resolve_env_placeholders
from fluid_build.util.binding_paths import anchor_binding_paths, resolve_binding_path

TEMPLATED = "{{ env.FLUID_TEST_DATA_DIR }}/product/product.parquet"


def test_an_env_path_resolves_to_the_absolute_directory_and_is_not_anchored(tmp_path, monkeypatch):
    monkeypatch.setenv("FLUID_TEST_DATA_DIR", str(tmp_path / "shared"))

    resolved = resolve_binding_path(TEMPLATED, tmp_path / "contracts" / "product")

    assert resolved == str(tmp_path / "shared" / "product" / "product.parquet")


def test_a_relative_value_is_anchored_after_it_is_resolved(tmp_path, monkeypatch):
    monkeypatch.setenv("FLUID_TEST_DATA_DIR", "out")

    resolved = resolve_binding_path(TEMPLATED, tmp_path)

    assert resolved == str(tmp_path / "out" / "product" / "product.parquet")


@pytest.mark.parametrize("value", ["/srv/lake", "rel/dir", None, "s3://bucket/prefix"])
def test_readers_resolve_exactly_as_the_build_runner_writes(tmp_path, monkeypatch, value):
    if value is None:
        monkeypatch.delenv("FLUID_TEST_DATA_DIR", raising=False)
    else:
        monkeypatch.setenv("FLUID_TEST_DATA_DIR", value)

    written = resolve_binding_path(_resolve_env_placeholders(TEMPLATED), tmp_path)
    read = resolve_binding_path(TEMPLATED, tmp_path)

    assert read == written


def test_anchor_binding_paths_resolves_the_expose_path(tmp_path, monkeypatch):
    monkeypatch.setenv("FLUID_TEST_DATA_DIR", str(tmp_path / "shared"))
    contract = {"exposes": [{"binding": {"location": {"path": TEMPLATED}}}]}

    anchored = anchor_binding_paths(contract, tmp_path / "contracts" / "product")

    assert anchored["exposes"][0]["binding"]["location"]["path"] == str(
        tmp_path / "shared" / "product" / "product.parquet"
    )
    # The input is never mutated.
    assert contract["exposes"][0]["binding"]["location"]["path"] == TEMPLATED


def test_verify_finds_the_file_the_build_wrote_under_the_shared_directory(tmp_path, monkeypatch):
    duckdb = pytest.importorskip("duckdb")
    from fluid_build.cli.verify import _verify_local_file

    shared = tmp_path / "shared"
    monkeypatch.setenv("FLUID_TEST_DATA_DIR", str(shared))
    landed = Path(_resolve_env_placeholders(TEMPLATED))
    landed.parent.mkdir(parents=True)
    con = duckdb.connect(":memory:")
    con.execute(f"COPY (SELECT 1 AS id, 'a' AS name) TO '{landed.as_posix()}' (FORMAT PARQUET)")
    con.close()
    expose = {"binding": {"format": "parquet", "location": {"path": TEMPLATED}}}

    result = _verify_local_file(
        "product", expose, "parquet", anchor_dir=tmp_path / "contracts" / "product"
    )

    assert result["status"] == "match", result
    assert result["row_count"] == 1
