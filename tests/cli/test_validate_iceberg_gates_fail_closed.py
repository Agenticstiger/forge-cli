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

"""A crash inside an Iceberg / Confluent validate gate is an ERROR, always.

``fluid validate`` wrapped each of these gates in ``except Exception`` and
reported the exception only under ``--verbose``. A validator that raised on
some contract shape therefore passed that contract as VALID with the gate
silently switched off: the silent-no-op class, in the one place whose job is
to refuse silent no-ops. The gates now fail closed.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from fluid_build.cli.validate import _run_contract_rules
from fluid_build.schema_manager import ValidationResult

pytestmark = pytest.mark.unit

#: (module, function, the label the error names) for each gate.
GATES = [
    (
        "fluid_build.build_runners.kafka_connect.iceberg_sink_validation",
        "validate_iceberg_sink",
        "Iceberg sink check",
    ),
    (
        "fluid_build.iac.providers.confluent",
        "validate_confluent_binding",
        "Confluent binding check",
    ),
    ("fluid_build.iac.iceberg_validation", "validate_iceberg_bindings", "Iceberg binding check"),
]


def _args():
    # --verbose OFF: the crash used to be visible only with it on.
    return SimpleNamespace(verbose=False, quiet=True, strict=False, offline=True)


def _contract():
    return {
        "fluidVersion": "0.7.6",
        "kind": "DataProduct",
        "id": "gold.orders",
        "name": "orders",
        "metadata": {"layer": "Gold", "owner": {"team": "dp", "email": "x@y.z"}},
        "exposes": [
            {
                "exposeId": "orders",
                "kind": "table",
                "binding": {
                    "platform": "aws",
                    "format": "iceberg",
                    "location": {
                        "database": "sales",
                        "table": "orders",
                        "bucket": "lake",
                        "region": "us-east-1",
                    },
                },
            }
        ],
    }


def _run():
    result = ValidationResult(is_valid=True)
    _run_contract_rules(_contract(), None, result, _args(), logging.getLogger("test"))
    return result


@pytest.mark.parametrize("module, func, label", GATES, ids=[g[2] for g in GATES])
def test_a_crashing_gate_is_reported_as_an_error(monkeypatch, module, func, label):
    def _boom(contract):
        raise KeyError("location")

    monkeypatch.setattr(f"{module}.{func}", _boom)
    result = _run()
    crash = [e for e in result.errors if e.startswith(f"{label} could not run")]
    assert len(crash) == 1, result.errors
    assert "KeyError" in crash[0] and "location" in crash[0]
    assert "NOT checked" in crash[0]
    assert result.is_valid is False


def test_healthy_gates_report_no_crash():
    # Negative control: the errors above come from the crash, not the contract.
    result = _run()
    for _, _, label in GATES:
        assert not any(e.startswith(f"{label} could not run") for e in result.errors)
