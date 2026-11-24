# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A validity window's conventions reach dbt as ``config.meta.snapshot_config``
(``fluid_build/engines/dbt/semantic_models.py``), in dbt's own snapshot key names."""

from __future__ import annotations

import copy
import json
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional

import jinja2
import pytest
import yaml

from fluid_build.engines.dbt import DbtEngine
from fluid_build.engines.dbt.semantic_models import generate_semantic_models
from tests.engines.test_dbt_semantic_models import (
    _dimension,
    _emit,
    _find_dbt,
    _model,
    _scd_contract,
)

pytestmark = pytest.mark.unit


def _window_contract(
    *,
    open_end: Optional[str] = None,
    chain: Optional[str] = None,
    marker: Optional[Dict[str, Any]] = None,
    end_type: str = "TIMESTAMP",
) -> Dict[str, Any]:
    """The SCD customer history with the new window members declared."""
    contract = _scd_contract()
    history = contract["exposes"][1]
    semantics = history["semantics"]
    params = semantics["dimensions"][2]["typeParams"]["validityParams"]
    if open_end is not None:
        params["openEnd"] = open_end
    if chain is not None:
        params["chain"] = chain
    if marker is not None:
        natural = next(e for e in semantics["entities"] if e["type"] == "natural")
        natural["lifecycle"] = {"endMarker": marker}
    schema = history["contract"]["schema"]
    schema.append({"name": "is_deleted", "type": "BOOLEAN"})
    schema.append({"name": "closed_reason", "type": "STRING"})
    next(c for c in schema if c["name"] == "valid_to")["type"] = end_type
    return contract


def _snapshot(contract: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    model = _model(_emit(contract), "customer_history")
    return ((model.get("config") or {}).get("meta") or {}).get("snapshot_config")


BASE = {
    "unique_key": "customer_id",
    "snapshot_meta_column_names": {"dbt_valid_from": "valid_from", "dbt_valid_to": "valid_to"},
}


def test_a_window_with_no_new_member_gets_the_key_and_the_column_names_only():
    assert _snapshot(_scd_contract()) == BASE


def test_a_contract_without_a_window_has_no_snapshot_config():
    doc = _emit(_scd_contract())
    assert "config" not in _model(doc, "orders")


def test_an_open_end_date_literal_casts_to_date():
    contract = _window_contract(open_end="9999-12-31", end_type="DATE")
    assert _snapshot(contract) == {**BASE, "dbt_valid_to_current": "cast('9999-12-31' as date)"}


def test_an_open_end_timestamp_literal_casts_to_timestamp():
    contract = _window_contract(open_end="9999-12-31 23:59:59.123456")
    assert _snapshot(contract)["dbt_valid_to_current"] == (
        "cast('9999-12-31 23:59:59.123456' as timestamp)"
    )
    contract = _window_contract(open_end="9999-12-31T00:00")
    assert _snapshot(contract)["dbt_valid_to_current"] == "cast('9999-12-31T00:00' as timestamp)"


@pytest.mark.parametrize("end_type", ["timestamptz", "timestamp_tz", "timestamp with time zone"])
@pytest.mark.parametrize(
    "literal",
    [
        "9999-12-31T00:00:00Z",
        "9999-12-31 23:59:59.123456+01:00",
        "9999-12-31T00:00-05:30",
        "9999-12-31T00:00",  # a zoned column still takes a naive literal
    ],
)
def test_a_zoned_end_column_takes_an_offset_literal(literal, end_type):
    contract = _window_contract(open_end=literal, end_type=end_type)
    assert _snapshot(contract)["dbt_valid_to_current"] == f"cast('{literal}' as timestamp)"


@pytest.mark.parametrize(
    "literal",
    [
        "9999-12-31T00:00:00Z",
        "9999-12-31T00:00+01:00",
        "9999-12-31 00:00:00-05:00",
    ],
)
def test_a_naive_end_column_refuses_an_offset_literal(literal, caplog):
    contract = _window_contract(open_end=literal, end_type="TIMESTAMP")
    with caplog.at_level("WARNING"):
        assert _snapshot(contract) == BASE
    assert "dbt_valid_to_current is left out" in caplog.text


@pytest.mark.parametrize(
    "literal",
    [
        "9999-12-31T00:00:00z",  # lower-case zone designator
        "9999-12-31T00:00:00+0100",  # no colon in the offset
        "9999-12-31T00:00:00+01",  # hours only
        "9999-12-31T00:00:00Z ",  # trailing text
        "9999-12-31T00:00:00Z\n",
        "9999-12-31T00:00:00+01:00', 1) --",
        "9999-12-31T00:00:00 UTC",
        "9999-12-31",  # a date literal on a timestamp column
    ],
)
def test_a_zoned_end_column_refuses_a_malformed_offset(literal, caplog):
    contract = _window_contract(open_end=literal, end_type="timestamptz")
    with caplog.at_level("WARNING"):
        assert _snapshot(contract) == BASE
    assert "dbt_valid_to_current is left out" in caplog.text


@pytest.mark.parametrize(
    "literal,end_type",
    [
        ("\u0662\u0660\u0662\u0664-\u0660\u0661-\u0660\u0661", "DATE"),  # Arabic-Indic digits
        ("2024-99-99", "DATE"),
        ("2024-02-30", "DATE"),
        ("2024-13-01T00:00", "TIMESTAMP"),
        ("2024-02-30T00:00", "TIMESTAMP"),
        ("2024-01-01T25:00", "TIMESTAMP"),
        ("2024-01-01T00:61", "TIMESTAMP"),
        ("\u0662\u0660\u0662\u0664-01-01T00:00", "TIMESTAMP"),
        ("2024-01-01T00:00:00+25:00", "timestamptz"),
        ("2024-99-99T00:00:00Z", "timestamptz"),
        ("2024-01-01T00:\u0660\u0660:00Z", "timestamptz"),
    ],
)
def test_an_open_end_that_is_no_real_ascii_date_or_time_is_left_out(literal, end_type, caplog):
    contract = _window_contract(open_end=literal, end_type=end_type)
    with caplog.at_level("WARNING"):
        assert _snapshot(contract) == BASE
    assert "dbt_valid_to_current is left out" in caplog.text


def test_no_open_end_leaves_dbt_valid_to_current_out():
    assert "dbt_valid_to_current" not in _snapshot(_window_contract())


@pytest.mark.parametrize(
    ("literal", "end_type"),
    [
        ("9999-12-31 ", "DATE"),  # trailing text
        ("9999-12-31\n", "DATE"),  # `$` would accept the newline
        ("9999-12-31T00:00", "DATE"),  # a timestamp literal on a date column
        ("9999-12-31", "TIMESTAMP"),  # a date literal on a timestamp column
        ("infinity", "TIMESTAMP"),
        ("9999-12-31', 1) --", "DATE"),
        ("9999-12-31", "STRING"),
    ],
)
def test_an_invalid_open_end_is_left_out_with_a_warning(literal, end_type, caplog):
    contract = _window_contract(open_end=literal, end_type=end_type)
    with caplog.at_level("WARNING"):
        snapshot = _snapshot(contract)
    assert snapshot == BASE
    assert "dbt_valid_to_current is left out" in caplog.text


def test_a_boolean_end_marker_names_the_deleted_column_and_hard_deletes():
    contract = _window_contract(marker={"column": "is_deleted", "value": True})
    assert _snapshot(contract) == {
        "unique_key": "customer_id",
        "snapshot_meta_column_names": {
            "dbt_valid_from": "valid_from",
            "dbt_valid_to": "valid_to",
            "dbt_is_deleted": "is_deleted",
        },
        "hard_deletes": "new_record",
    }


def test_a_non_boolean_end_marker_adds_nothing():
    contract = _window_contract(marker={"column": "closed_reason", "value": "closed"})
    assert _snapshot(contract) == BASE
    # a boolean column, but a value that is not the bool True
    contract = _window_contract(marker={"column": "is_deleted", "value": False})
    assert _snapshot(contract) == BASE


@pytest.mark.parametrize("chain", ["dayBefore", "none"])
def test_a_closed_chain_warns_that_metricflow_joins_half_open(chain, caplog):
    with caplog.at_level("WARNING"):
        snapshot = _snapshot(_window_contract(chain=chain))
    assert snapshot == BASE  # a warning, no YAML change
    assert "[start, end)" in caplog.text and chain in caplog.text


def test_the_default_chain_does_not_warn(caplog):
    with caplog.at_level("WARNING"):
        _snapshot(_window_contract(chain="nextStart"))
    assert "[start, end)" not in caplog.text


def test_only_the_snapshot_block_differs_from_the_output_without_new_members():
    contract = _window_contract(
        open_end="9999-12-31", chain="dayBefore", marker={"column": "is_deleted", "value": True}
    )
    plain = copy.deepcopy(contract)
    history = plain["exposes"][1]["semantics"]
    history["dimensions"][2]["typeParams"]["validityParams"] = {"isEnd": True}
    for entity in history["entities"]:
        entity.pop("lifecycle", None)
    new = generate_semantic_models(contract)
    old = generate_semantic_models(plain)
    assert new["models/metricflow_time_spine.sql"] == old["models/metricflow_time_spine.sql"]
    new_doc = yaml.safe_load(new["models/semantic_models.yml"])
    old_doc = yaml.safe_load(old["models/semantic_models.yml"])
    for doc in (new_doc, old_doc):
        model = _model(doc, "customer_history")
        assert set(model["config"]["meta"]) == {"snapshot_config"}
        del model["config"]
    assert new_doc == old_doc
    # and the dimension the window ends on is unchanged
    assert _dimension(_model(new_doc, "customer_history"), "valid_to")["type_params"] == {
        "time_granularity": "day",
        "validity_params": {"is_end": True},
    }


def test_existing_meta_keys_keep_their_place_before_the_snapshot_block():
    contract = _window_contract(open_end="9999-12-31", end_type="DATE")
    contract["exposes"][1]["semantics"]["tags"] = ["scd"]
    meta = _model(_emit(contract), "customer_history")["config"]["meta"]
    assert list(meta) == ["fluid_tags", "snapshot_config"]


def test_a_hostile_open_end_never_reaches_the_yaml_as_jinja():
    hostile = "{{ 9999 }}-12-31"
    contract = _window_contract(open_end=hostile, end_type="DATE")
    text = generate_semantic_models(contract)["models/semantic_models.yml"]
    assert "{{ 9999" not in text and "{%" not in text
    # the literal is dropped, and nothing in the file renders to anything but itself
    assert _snapshot(contract) == BASE
    env = jinja2.Environment(keep_trailing_newline=True)
    assert env.from_string(text).render() == text


def test_a_valid_literal_survives_a_jinja_render_unchanged():
    contract = _window_contract(open_end="9999-12-31", end_type="DATE")
    text = generate_semantic_models(contract)["models/semantic_models.yml"]
    assert jinja2.Environment(keep_trailing_newline=True).from_string(text).render() == text


@pytest.mark.integration
@pytest.mark.slow
def test_a_window_with_the_snapshot_block_passes_dbt_parse(tmp_path: Path):
    """ACCEPTANCE: dbt-core 1.11 accepts ``config.meta.snapshot_config`` on a semantic model."""
    dbt = _find_dbt()
    if dbt is None:
        pytest.skip("dbt CLI not available")
    contract = _window_contract(
        open_end="9999-12-31",
        chain="dayBefore",
        marker={"column": "is_deleted", "value": True},
        end_type="DATE",
    )
    out_dir = tmp_path / "dbt_project"
    files = DbtEngine().generate(contract, contract["builds"][0], output_dir=out_dir)
    out_dir.mkdir(parents=True)
    for rel_path, content in files.items():
        target = out_dir / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    result = subprocess.run(
        [dbt, "parse", "--project-dir", str(out_dir), "--profiles-dir", str(out_dir)],
        capture_output=True,
        text=True,
        check=False,
        cwd=out_dir,
        timeout=300,
    )
    assert result.returncode == 0, f"dbt parse failed:\n{result.stdout}\n{result.stderr}"
    manifest = json.loads((out_dir / "target" / "manifest.json").read_text())
    node = manifest["semantic_models"][
        "semantic_model.%s.customer_history" % manifest["metadata"]["project_name"]
    ]
    assert node["config"]["meta"]["snapshot_config"]["hard_deletes"] == "new_record"
