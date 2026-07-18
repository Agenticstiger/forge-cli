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

"""Engine/version-aware ``tests:`` vs ``data_tests:`` emission.

dbt-core 1.8 renamed the data-test attachment key ``tests:`` →
``data_tests:`` (both accepted on core 1.8+ for backward compatibility);
the Fusion engine (dbt v2) strict-parses and does not support the
deprecated legacy key, while dbt-core <1.8 only understands ``tests:``.
These tests pin:

* the engine emitters (``schema_yml`` / ``sources``) and the exporter
  (``exporters/dbt_tests``) honour a threaded ``tests_key`` option;
* the DEFAULT path stays byte-identical to the pre-option output
  (legacy ``tests:`` everywhere);
* the CLI resolver picks the key from the detected dbt binary
  (fusion / core>=1.8 → ``data_tests``, core<1.8 / none → ``tests``)
  with flag + env overrides.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest
import yaml

from fluid_build.cli import generate_speed_transformation as gst
from fluid_build.engines import get_engine
from fluid_build.engines.dbt.schema_yml import (
    TESTS_KEY_LEGACY,
    TESTS_KEY_MODERN,
    VALID_TESTS_KEYS,
    generate_schema_yml,
    normalize_tests_key,
)
from fluid_build.engines.dbt.sources import generate_sources
from fluid_build.exporters.dbt_tests import render_dbt_tests

LOGGER = logging.getLogger("test_dbt_tests_key_dialect")


@pytest.fixture
def contract():
    """Contract exercising every tests-key emit site.

    - column-scoped dq rules + inline constraints → ``schema.yml`` columns
    - a table-wide (``selector: "*"``) rule → exporter model-level tests
    - a dq rule on a column absent from ``schema[]`` → orphan-column path
    - ``consumes[]`` + ``schema_context`` id columns → ``sources.yml`` tests
    """
    return {
        "fluidVersion": "0.7.2",
        "kind": "DataProduct",
        "id": "gold.analytics.tests_key_v1",
        "name": "Tests Key",
        "consumes": [
            {"exposeId": "orders", "productId": "silver.sales.orders_v1", "purpose": "Orders"},
        ],
        "builds": [
            {
                "id": "main_transform",
                "engine": "dbt",
                "pattern": "hybrid-reference",
                "properties": {
                    "model": "main",
                    "materializations": {"staging": "view", "marts": "table"},
                },
                "execution": {"runtime": {"platform": "local"}},
            }
        ],
        "exposes": [
            {
                "exposeId": "customer_orders",
                "kind": "table",
                "contract": {
                    "schema": [
                        {"name": "customer_id", "type": "STRING", "required": True},
                        {"name": "total_amount", "type": "NUMBER", "minimum": 0},
                    ],
                    "dq": {
                        "rules": [
                            {"id": "cid_unique", "type": "uniqueness", "selector": "customer_id"},
                            {"id": "orphan_nn", "type": "completeness", "selector": "ghost_col"},
                            {"id": "table_wide", "type": "uniqueness", "selector": "*"},
                        ]
                    },
                },
            }
        ],
    }


@pytest.fixture
def schema_context():
    return {"schemas": {"orders": {"columns": {"order_id": "INTEGER", "note": "STRING"}}}}


def _walk_keys(node):
    """Yield every dict key anywhere in a parsed YAML tree."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield key
            yield from _walk_keys(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_keys(item)


# ---------------------------------------------------------------------------
# normalize_tests_key
# ---------------------------------------------------------------------------


class TestNormalizeTestsKey:
    def test_none_defaults_to_legacy(self):
        assert normalize_tests_key(None) == TESTS_KEY_LEGACY == "tests"

    @pytest.mark.parametrize("key", sorted(VALID_TESTS_KEYS))
    def test_valid_keys_pass_through(self, key):
        assert normalize_tests_key(key) == key

    @pytest.mark.parametrize("bad", ["Tests", "data-tests", "unit_tests", "", "auto"])
    def test_invalid_raises_value_error(self, bad):
        with pytest.raises(ValueError, match="invalid dbt tests key"):
            normalize_tests_key(bad)


# ---------------------------------------------------------------------------
# schema_yml
# ---------------------------------------------------------------------------


class TestSchemaYmlDialect:
    def test_default_emits_legacy_key_only(self, contract):
        content = generate_schema_yml(contract)["models/marts/schema.yml"]
        keys = set(_walk_keys(yaml.safe_load(content.split("\n\n", 1)[1])))
        assert TESTS_KEY_LEGACY in keys
        assert TESTS_KEY_MODERN not in keys

    def test_default_is_byte_identical_to_explicit_legacy(self, contract):
        default = generate_schema_yml(contract)["models/marts/schema.yml"]
        explicit = generate_schema_yml(contract, tests_key="tests")["models/marts/schema.yml"]
        assert default == explicit

    def test_data_tests_is_a_pure_key_rename(self, contract):
        default = generate_schema_yml(contract)["models/marts/schema.yml"]
        modern = generate_schema_yml(contract, tests_key="data_tests")["models/marts/schema.yml"]
        assert default.replace("tests:", "data_tests:") == modern
        keys = set(_walk_keys(yaml.safe_load(modern.split("\n\n", 1)[1])))
        assert TESTS_KEY_MODERN in keys
        assert TESTS_KEY_LEGACY not in keys

    def test_orphan_dq_rule_column_uses_selected_key(self, contract):
        modern = generate_schema_yml(contract, tests_key="data_tests")["models/marts/schema.yml"]
        doc = yaml.safe_load(modern.split("\n\n", 1)[1])
        ghost = next(col for col in doc["models"][0]["columns"] if col["name"] == "ghost_col")
        assert ghost["data_tests"] == ["not_null"]

    def test_model_contracts_split_respects_selected_key(self, contract):
        # Keep only schema-declared column selectors — model contracts skip
        # enforcement when a dq rule targets a column absent from schema[]
        # (both the orphan ghost_col and the "*" table-wide pseudo-column).
        rules = contract["exposes"][0]["contract"]["dq"]["rules"]
        contract["exposes"][0]["contract"]["dq"]["rules"] = [
            r for r in rules if r["selector"] == "customer_id"
        ]
        out = generate_schema_yml(contract, model_contracts=True, tests_key="data_tests")
        doc = yaml.safe_load(out["models/marts/schema.yml"].split("\n\n", 1)[1])
        model = doc["models"][0]
        assert model["config"] == {"contract": {"enforced": True}}
        cid = next(col for col in model["columns"] if col["name"] == "customer_id")
        # not_null moved to a build-time constraint; unique stays a data test
        # under the modern key.
        assert {"type": "not_null"} in cid["constraints"]
        assert cid["data_tests"] == ["unique"]
        assert "tests" not in cid

    def test_invalid_key_raises(self, contract):
        with pytest.raises(ValueError, match="invalid dbt tests key"):
            generate_schema_yml(contract, tests_key="unit_tests")


# ---------------------------------------------------------------------------
# sources
# ---------------------------------------------------------------------------


class TestSourcesDialect:
    def test_default_emits_legacy_key_only(self, contract, schema_context):
        content = generate_sources(contract, schema_context=schema_context)
        keys = set(_walk_keys(yaml.safe_load(content.split("\n\n", 1)[1])))
        assert TESTS_KEY_LEGACY in keys
        assert TESTS_KEY_MODERN not in keys

    def test_data_tests_is_a_pure_key_rename(self, contract, schema_context):
        default = generate_sources(contract, schema_context=schema_context)
        modern = generate_sources(contract, schema_context=schema_context, tests_key="data_tests")
        assert default.replace("tests:", "data_tests:") == modern
        doc = yaml.safe_load(modern.split("\n\n", 1)[1])
        order_id = doc["sources"][0]["tables"][0]["columns"][0]
        assert order_id == {"name": "order_id", "data_tests": ["unique", "not_null"]}

    def test_invalid_key_raises(self, contract, schema_context):
        with pytest.raises(ValueError, match="invalid dbt tests key"):
            generate_sources(contract, schema_context=schema_context, tests_key="bogus")


# ---------------------------------------------------------------------------
# DbtEngine.generate threading
# ---------------------------------------------------------------------------


class TestEngineThreading:
    def _generate(self, contract, schema_context, **kwargs):
        engine = get_engine("dbt")
        return engine.generate(
            contract, contract["builds"][0], schema_context=schema_context, **kwargs
        )

    def test_data_tests_reaches_schema_and_sources(self, contract, schema_context):
        files = self._generate(contract, schema_context, tests_key="data_tests")
        for path in ("models/marts/schema.yml", "models/sources.yml"):
            keys = set(_walk_keys(yaml.safe_load(files[path].split("\n\n", 1)[1])))
            assert TESTS_KEY_MODERN in keys, path
            assert TESTS_KEY_LEGACY not in keys, path

    def test_default_stays_legacy_everywhere(self, contract, schema_context):
        files = self._generate(contract, schema_context)
        for path in ("models/marts/schema.yml", "models/sources.yml"):
            keys = set(_walk_keys(yaml.safe_load(files[path].split("\n\n", 1)[1])))
            assert TESTS_KEY_LEGACY in keys, path
            assert TESTS_KEY_MODERN not in keys, path


# ---------------------------------------------------------------------------
# exporters/dbt_tests
# ---------------------------------------------------------------------------


class TestExporterDialect:
    def test_default_emits_legacy_key_only(self, contract):
        rendered = render_dbt_tests(contract)
        keys = set(_walk_keys(yaml.safe_load(rendered)))
        assert TESTS_KEY_LEGACY in keys
        assert TESTS_KEY_MODERN not in keys

    def test_data_tests_is_a_pure_key_rename(self, contract):
        default = render_dbt_tests(contract)
        modern = render_dbt_tests(contract, tests_key="data_tests")
        assert default.replace("tests:", "data_tests:") == modern
        doc = yaml.safe_load(modern)
        model = doc["models"][0]
        # Table-wide ("*") rule lands at model level under the modern key
        # (uniqueness at table scope maps to the fluid sentinel test name).
        assert model["data_tests"] == ["fluid_uniqueness_table_level"]
        cid = next(col for col in model["columns"] if col["name"] == "customer_id")
        assert "unique" in cid["data_tests"]
        # Orphan dq-rule column also honours the key.
        ghost = next(col for col in model["columns"] if col["name"] == "ghost_col")
        assert ghost["data_tests"] == ["not_null"]
        assert TESTS_KEY_LEGACY not in set(_walk_keys(doc))

    def test_invalid_key_raises(self, contract):
        with pytest.raises(ValueError, match="invalid dbt tests key"):
            render_dbt_tests(contract, tests_key="Tests")


# ---------------------------------------------------------------------------
# CLI resolution (auto-detection matrix + overrides)
# ---------------------------------------------------------------------------

_RUNNER = "fluid_build.build_runners.dbt.runner"


def _args(dbt_tests_key=None):
    return SimpleNamespace(dbt_tests_key=dbt_tests_key)


def _patch_detection(monkeypatch, *, dbt_bin, flavor="", version=""):
    monkeypatch.setattr(f"{_RUNNER}._resolve_dbt_executable", lambda: dbt_bin)
    monkeypatch.setattr(
        f"{_RUNNER}._detect_dbt_engine",
        lambda executable, timeout=10.0: (flavor, version),
    )


class TestCliResolution:
    @pytest.fixture(autouse=True)
    def _clean_env(self, monkeypatch):
        monkeypatch.delenv("FLUID_DBT_TESTS_KEY", raising=False)

    @pytest.mark.parametrize("explicit", ["tests", "data_tests"])
    def test_explicit_flag_wins_without_detection(self, explicit, monkeypatch):
        # Detection must not even run — make it explode if reached.
        monkeypatch.setattr(
            f"{_RUNNER}._resolve_dbt_executable",
            lambda: pytest.fail("detection ran despite explicit flag"),
        )
        assert gst._resolve_dbt_tests_key(_args(explicit), LOGGER) == explicit

    def test_env_var_override(self, monkeypatch):
        monkeypatch.setenv("FLUID_DBT_TESTS_KEY", "data_tests")
        monkeypatch.setattr(
            f"{_RUNNER}._resolve_dbt_executable",
            lambda: pytest.fail("detection ran despite env override"),
        )
        assert gst._resolve_dbt_tests_key(_args(), LOGGER) == "data_tests"

    def test_flag_beats_env(self, monkeypatch):
        monkeypatch.setenv("FLUID_DBT_TESTS_KEY", "data_tests")
        assert gst._resolve_dbt_tests_key(_args("tests"), LOGGER) == "tests"

    def test_env_garbage_falls_back_to_auto(self, monkeypatch, capsys):
        monkeypatch.setenv("FLUID_DBT_TESTS_KEY", "unit_tests")
        _patch_detection(monkeypatch, dbt_bin=None)
        assert gst._resolve_dbt_tests_key(_args(), LOGGER) == "tests"
        assert "FLUID_DBT_TESTS_KEY" in capsys.readouterr().out

    def test_no_binary_defaults_legacy(self, monkeypatch):
        _patch_detection(monkeypatch, dbt_bin=None)
        assert gst._resolve_dbt_tests_key(_args(), LOGGER) == "tests"

    def test_fusion_selects_data_tests(self, monkeypatch):
        _patch_detection(
            monkeypatch, dbt_bin="/opt/dbt", flavor="fusion", version="2.0.0-preview.126"
        )
        assert gst._resolve_dbt_tests_key(_args(), LOGGER) == "data_tests"

    @pytest.mark.parametrize("version", ["1.8.0", "1.8.0b1", "1.11.11", "1.12.0"])
    def test_core_1_8_plus_selects_data_tests(self, version, monkeypatch):
        _patch_detection(monkeypatch, dbt_bin="/opt/dbt", flavor="core", version=version)
        assert gst._resolve_dbt_tests_key(_args(), LOGGER) == "data_tests"

    @pytest.mark.parametrize("version", ["1.7.14", "1.0.0", "0.21.1", "", "garbage"])
    def test_core_pre_1_8_or_unparseable_selects_legacy(self, version, monkeypatch):
        _patch_detection(monkeypatch, dbt_bin="/opt/dbt", flavor="core", version=version)
        assert gst._resolve_dbt_tests_key(_args(), LOGGER) == "tests"

    def test_unknown_flavor_selects_legacy(self, monkeypatch):
        _patch_detection(monkeypatch, dbt_bin="/opt/dbt", flavor="unknown", version="")
        assert gst._resolve_dbt_tests_key(_args(), LOGGER) == "tests"


class TestVersionFloor:
    @pytest.mark.parametrize(
        ("version", "expected"),
        [
            ("1.8.0", True),
            ("1.8.0b1", True),
            ("1.11.11", True),
            ("2.0.0", True),
            ("10.0", True),
            ("1.7.14", False),
            ("1.7", False),
            ("1", False),
            ("", False),
            ("garbage", False),
            ("1.x.0", False),
        ],
    )
    def test_matrix(self, version, expected):
        assert gst._dbt_core_version_at_least(version, (1, 8)) is expected
