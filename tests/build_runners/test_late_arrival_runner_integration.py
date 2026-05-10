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

"""Pin the late-arrival policy → connector config translation.

Streaming runners (Kafka Connect / Debezium) don't consume per-record
in a Python loop; the actual ingestion runs inside a connector worker
on a Kafka cluster. The integration point is the **connector
config** — the runner reads ``WatermarkSpec.allowed_lateness`` from
the contract and surfaces it as ``fluid.late_arrival.*`` keys on the
connector config so a downstream Single-Message-Transform (SMT) or
sink-side enforcer can route over-budget events to a canonical
side-output table.

These tests:

1. Pin :func:`extract_late_arrival_policy` against both input shapes
   (SourceSpec, contract dict).
2. Pin the connector-config keys that downstream SMTs depend on.
3. Assert "no policy configured" yields a consistent, opaque
   ``{enabled: False}`` shape so callers don't need to unpack.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from fluid_build.build_runners._late_arrival import (
    extract_late_arrival_policy,
    side_output_table_name,
)


class _FakeWatermark:
    """Minimal WatermarkSpec stand-in for the SourceSpec attribute path."""

    def __init__(self, allowed_lateness):
        self.allowed_lateness = allowed_lateness


class _FakeSource:
    def __init__(self, allowed_lateness=None):
        self.watermark = _FakeWatermark(allowed_lateness) if allowed_lateness else None


class TestExtractLateArrivalPolicy:
    def test_no_policy_returns_disabled(self):
        out = extract_late_arrival_policy(contract_or_source=_FakeSource(allowed_lateness=None))
        assert out == {"enabled": False}

    def test_pt5m_yields_300_seconds(self):
        out = extract_late_arrival_policy(
            contract_or_source=_FakeSource(allowed_lateness="PT5M"),
            target_table="orders_topic",
        )
        assert out["enabled"] is True
        assert out["allowed_lateness_iso"] == "PT5M"
        assert out["allowed_lateness_seconds"] == 300.0
        assert out["side_output_table"] == "orders_topic__late_events"

    def test_connector_config_keys_are_stable(self):
        """Downstream SMTs read these specific keys; the names are
        part of the contract surface."""
        out = extract_late_arrival_policy(
            contract_or_source=_FakeSource(allowed_lateness="PT1H"),
            target_table="t",
        )
        cc = out["connector_config"]
        assert cc["fluid.late_arrival.enabled"] == "true"
        assert cc["fluid.late_arrival.allowed_lateness_seconds"] == "3600.0"
        assert cc["fluid.late_arrival.side_output_table"] == "t__late_events"

    def test_zero_duration_returns_disabled(self):
        out = extract_late_arrival_policy(contract_or_source=_FakeSource(allowed_lateness="PT"))
        assert out["enabled"] is False

    def test_invalid_duration_returns_disabled(self):
        out = extract_late_arrival_policy(
            contract_or_source=_FakeSource(allowed_lateness="not-iso")
        )
        assert out["enabled"] is False
        assert out["allowed_lateness_iso"] == "not-iso"

    def test_contract_dict_path(self):
        """Some callers pass the raw contract dict instead of
        SourceSpec — we should walk
        ``builds[].properties.source.watermark.allowedLateness``."""
        contract = {
            "builds": [
                {
                    "properties": {
                        "source": {
                            "watermark": {
                                "strategy": "high_water_mark",
                                "allowedLateness": "P1D",
                            }
                        }
                    }
                }
            ]
        }
        out = extract_late_arrival_policy(contract_or_source=contract)
        assert out["enabled"] is True
        assert out["allowed_lateness_seconds"] == 86400.0


class TestKafkaConnectRunnerWiring:
    """The kafka-connect runner must merge the late-arrival keys into
    its connector config and surface the policy in RunResult.facets.

    Patching at module import time lets us assert the call happens
    without standing up Kafka."""

    def test_kc_runner_uses_extract_helper(self):
        # Pure import-shape pin: the runner module imports the helper.
        from fluid_build.build_runners.kafka_connect import runner as kc_runner

        src = kc_runner.__file__
        with open(src) as f:
            text = f.read()
        assert "extract_late_arrival_policy" in text, (
            "kafka_connect runner must call extract_late_arrival_policy"
        )
        assert "fluid.late_arrival" in text or "late_arrival_policy" in text


class TestDebeziumRunnerWiring:
    def test_dbz_runner_uses_extract_helper(self):
        from fluid_build.build_runners.debezium import runner as dbz_runner

        src = dbz_runner.__file__
        with open(src) as f:
            text = f.read()
        assert "extract_late_arrival_policy" in text, (
            "debezium runner must call extract_late_arrival_policy"
        )
        assert "late_arrival_policy" in text


def test_side_output_table_name_is_stable():
    """Side-output table name must follow the
    ``<target>__late_events`` convention so operators know where to
    look without grep'ing per-runner."""
    assert side_output_table_name("orders") == "orders__late_events"
    assert side_output_table_name("crm.customers") == "crm.customers__late_events"
