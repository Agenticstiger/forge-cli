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

"""Tests for the SodaCL exporter (``exporters/sodacl.py``)."""

from __future__ import annotations

import yaml

from fluid_build.exporters.sodacl import render_sodacl


def _contract_with_quality():
    return {
        "exposes": [
            {
                "id": "orders",
                "binding": {
                    "location": {"properties": {"table": "ORDERS"}},
                },
                "quality": {
                    "sla": {"freshness": "1h"},
                    "tests": [
                        {"name": "t_pk", "type": "not_null", "column": "order_id"},
                        {"name": "t_unique", "type": "unique", "column": "order_id"},
                        {
                            "name": "t_status",
                            "type": "accepted_values",
                            "column": "status",
                            "values": ["new", "shipped"],
                        },
                        {
                            "name": "t_amt",
                            "type": "range",
                            "column": "amount",
                            "min": 0,
                            "max": 1000,
                        },
                        {
                            "name": "t_email",
                            "type": "regex",
                            "column": "email",
                            "pattern": ".+@.+",
                        },
                    ],
                },
            }
        ]
    }


def test_render_emits_checks_for_block():
    out = render_sodacl(_contract_with_quality())
    doc = yaml.safe_load(out)
    assert "checks for ORDERS" in doc


def test_render_translates_not_null_and_unique():
    out = render_sodacl(_contract_with_quality())
    doc = yaml.safe_load(out)
    checks = doc["checks for ORDERS"]
    assert "missing_count(order_id) = 0" in checks
    assert "duplicate_count(order_id) = 0" in checks


def test_render_translates_range_to_flat_min_and_max_checks():
    """A range test with both min and max emits TWO flat string checks."""
    out = render_sodacl(_contract_with_quality())
    doc = yaml.safe_load(out)
    checks = doc["checks for ORDERS"]
    # The two range entries are flat strings, not a nested list.
    assert "min(amount) >= 0" in checks
    assert "max(amount) <= 1000" in checks


def test_render_translates_accepted_values_as_check_dict():
    """accepted_values must be a dict with check-name key and 'valid values' sub-config."""
    out = render_sodacl(_contract_with_quality())
    doc = yaml.safe_load(out)
    checks = doc["checks for ORDERS"]
    av_check = next(c for c in checks if isinstance(c, dict) and "invalid_count(status) = 0" in c)
    assert av_check["invalid_count(status) = 0"]["valid values"] == ["new", "shipped"]


def test_render_translates_regex_as_check_dict():
    """regex must be a dict with check-name key and 'valid regex' sub-config."""
    out = render_sodacl(_contract_with_quality())
    doc = yaml.safe_load(out)
    checks = doc["checks for ORDERS"]
    rx_check = next(c for c in checks if isinstance(c, dict) and "invalid_count(email) = 0" in c)
    assert rx_check["invalid_count(email) = 0"]["valid regex"] == ".+@.+"


def test_render_emits_freshness_from_sla_as_plain_string():
    """SLA-derived freshness is a flat string check, not a dict."""
    out = render_sodacl(_contract_with_quality())
    doc = yaml.safe_load(out)
    checks = doc["checks for ORDERS"]
    assert "freshness using <last_updated> < 1h" in checks


def test_render_empty_quality_emits_no_checks_marker():
    contract = {"exposes": [{"id": "x", "binding": {"location": {"properties": {"table": "X"}}}}]}
    out = render_sodacl(contract)
    assert "No quality tests" in out


def test_render_skips_exposes_with_no_quality():
    contract = {
        "exposes": [
            {
                "id": "no_quality",
                "binding": {"location": {"properties": {"table": "NO_QUALITY"}}},
            },
            {
                "id": "yes_quality",
                "binding": {"location": {"properties": {"table": "YES_QUALITY"}}},
                "quality": {"tests": [{"type": "not_null", "column": "id"}]},
            },
        ]
    }
    out = render_sodacl(contract)
    doc = yaml.safe_load(out)
    assert "checks for YES_QUALITY" in doc
    assert "checks for NO_QUALITY" not in doc
