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

"""PR3 — opt-in fluid-schema-0.7.5 (streamingSink + iceberg_table alias).

The crux: adding the 0.7.5 schema must NOT bump the default version for untagged
contracts (which would churn plan/bundle digests install-wide — RFC §8). 0.7.5
is bundled + validatable on explicit opt-in, but excluded from the default.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft7Validator

import fluid_build
from fluid_build.cli._common import _normalize_contract_aliases
from fluid_build.cli.plan import _default_fluid_version
from fluid_build.schema_manager import SchemaManager

pytestmark = [pytest.mark.unit]

_SCHEMA_DIR = Path(fluid_build.__file__).parent / "schemas"


def _schema(version):
    return json.load(open(_SCHEMA_DIR / f"fluid-schema-{version}.json"))


def _v075_contract(**kc):
    return {
        "fluidVersion": "0.7.5",
        "kind": "DataProduct",
        "id": "bronze.x",
        "name": "X",
        "metadata": {"layer": "Bronze", "owner": {"team": "dp", "email": "x@y.z"}},
        "builds": [
            {
                "id": "i",
                "pattern": "acquisition",
                "engine": "kafka-connect",
                "properties": {
                    "source": {"kind": "postgres", "mode": "incremental_append"},
                    "sink": {"format": "iceberg"},
                    "kafka-connect": kc,
                },
            }
        ],
        "exposes": [
            {
                "exposeId": "d",
                "kind": "table",
                "binding": {
                    "platform": "aws",
                    "format": "iceberg",
                    "location": {"database": "s", "table": "o"},
                },
                "contract": {"schema": []},
            }
        ],
    }


# ── opt-in, no default bump (the headline guarantee) ────────────────────────


def test_075_is_bundled_and_opt_in_available():
    assert "0.7.5" in SchemaManager.BUNDLED_VERSIONS


def test_current_preview_does_not_become_the_default():
    # The GA lifecycle invariant, version-agnostic: whatever versions are in
    # PREVIEW_VERSIONS must never be the default for untagged contracts — the
    # default stays the latest STABLE, so plan.json format_version (and
    # digests) only move on a deliberate GA promotion.
    # (0.7.5 was promoted to stable on 2026-07-18; 0.7.6 is the open preview.)
    assert "0.7.5" not in SchemaManager.PREVIEW_VERSIONS  # promoted to stable (GA)
    for preview in SchemaManager.PREVIEW_VERSIONS:
        assert SchemaManager.latest_bundled_version() != preview
    assert _default_fluid_version() == SchemaManager.latest_bundled_version()


# ── enum retention (legacy contracts must still validate) ───────────────────


def test_075_enum_retains_older_versions():
    enum = _schema("0.7.5")["properties"]["fluidVersion"]["enum"]
    assert "0.7.3" in enum and "0.7.4" in enum and "0.7.5" in enum


def test_075_validates_a_074_shaped_contract_unchanged():
    schema = _schema("0.7.5")
    c = _v075_contract()
    c["fluidVersion"] = "0.7.4"  # a 0.7.4-vintage contract
    errs = list(Draft7Validator(schema).iter_errors(c))
    assert errs == [], [e.message for e in errs]


# ── streamingSink (typed, closed sub-block) ─────────────────────────────────


def test_075_accepts_streaming_sink_block():
    schema = _schema("0.7.5")
    c = _v075_contract(
        streamingSink={"autoCreate": True, "commitIntervalMs": 1000, "upsertMode": False},
        iceberg_sink_enabled=True,
        iceberg_catalog_overrides={"iceberg.catalog.warehouse": "s3://x/"},
    )
    errs = list(Draft7Validator(schema).iter_errors(c))
    assert errs == [], [e.message for e in errs]


def test_075_rejects_streaming_sink_typo():
    schema = _schema("0.7.5")
    c = _v075_contract(streamingSink={"autoCreat": True})  # typo -> closed block rejects
    assert list(Draft7Validator(schema).iter_errors(c))


def test_074_has_no_streaming_sink_block():
    # the addition is scoped to 0.7.5; 0.7.4 is untouched
    assert "streamingSink" not in json.dumps(_schema("0.7.4"))


# ── iceberg_table alias normalizes to the canonical enum ────────────────────


@pytest.mark.parametrize("alias", ["iceberg_table", "iceberg-table"])
def test_iceberg_table_alias_normalizes_to_iceberg(alias):
    contract = {"exposes": [{"exposeId": "d", "binding": {"platform": "aws", "format": alias}}]}
    out = _normalize_contract_aliases(contract)
    assert out["exposes"][0]["binding"]["format"] == "iceberg"


def test_existing_iceberg_format_unchanged_by_alias():
    contract = {"exposes": [{"exposeId": "d", "binding": {"platform": "aws", "format": "iceberg"}}]}
    out = _normalize_contract_aliases(contract)
    assert out["exposes"][0]["binding"]["format"] == "iceberg"
