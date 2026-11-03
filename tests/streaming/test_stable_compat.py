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

"""What the managed lane must leave alone: stable contracts and existing exports.

``parameters.flink`` was free-form before 0.7.7, so a stable Flink build may
already carry settings there for the platform that runs it. ODCS export of
scalar timestamps outside the streaming type model must also keep working.
"""

import copy

import pytest

from fluid_build.build_runners import base
from fluid_build.providers.odcs.mappers.schema import _field_to_property
from fluid_build.schema_manager import FluidSchemaManager
from fluid_build.streaming.compiler import (
    compile_build,
    compile_contract,
    managed_builds,
    validation_errors,
    validation_warnings,
)
from tests._flink_support import flink_contract


def _stable(parameters):
    contract = flink_contract("0.7.6")
    contract["builds"][0]["properties"]["parameters"] = parameters
    return contract


def test_stable_flink_settings_under_parameters_flink_stay_external(tmp_path):
    contract = _stable({"flink": {"parallelism": 2}})
    result = FluidSchemaManager(cache_dir=tmp_path).validate_contract(contract, offline_only=True)
    assert result.is_valid, result.errors
    assert managed_builds(contract) == []
    assert compile_contract(contract) == ()
    assert validation_errors(contract) == []
    assert validation_warnings(contract) == []


def test_stable_contract_asking_for_the_managed_lane_is_warned_not_refused(tmp_path):
    contract = _stable({"flink": {"management": "operator"}})
    result = FluidSchemaManager(cache_dir=tmp_path).validate_contract(contract, offline_only=True)
    assert result.is_valid, result.errors
    assert any("needs fluidVersion 0.7.7 or later" in w for w in result.warnings), result.warnings


def test_runner_reports_a_stable_flink_build_as_run_elsewhere(monkeypatch):
    contract = _stable({"flink": {"parallelism": 2}})
    said = []
    monkeypatch.setattr(base, "cprint", lambda message, **_: said.append(message))
    base._report_externally_run_build(contract["builds"][0], "reconcile", None, contract)
    assert said and "outside forge-cli" in said[0], said


def test_preview_contract_is_managed_and_an_older_one_is_not(contract):
    assert len(managed_builds(contract)) == 1
    assert validation_warnings(contract) == []
    older = copy.deepcopy(contract)
    older["fluidVersion"] = "0.7.6"
    assert managed_builds(older) == []
    assert compile_contract(older) == ()
    (warning,) = validation_warnings(older)
    assert "needs fluidVersion 0.7.7 or later" in warning
    # compile_build has its own check, for a caller that skips managed_builds.
    with pytest.raises(ValueError, match="0.7.7 or later"):
        compile_build(older, older["builds"][0])


@pytest.mark.parametrize("version", ["0.7.8", "0.8", "1.0.0", "0.7.7-rc1", "0.7.7 ", 0.8, None])
def test_only_a_bundled_schema_with_the_lane_enables_it(contract, version):
    # None of these was validated against a schema that has the lane, and
    # `fluid verify` has no schema gate of its own: it would reach the cluster.
    contract["fluidVersion"] = version
    assert managed_builds(contract) == []
    assert compile_contract(contract) == ()


def test_scalar_timestamps_outside_the_streaming_model_export_as_before():
    for raw in ("TIMESTAMP_LTZ(9)", "timestamptz(3)", "TIMESTAMP(9)"):
        prop = _field_to_property({"name": "ts", "type": raw}, None, include_quality=False)
        assert prop["name"] == "ts"
        assert not any(
            item.get("property") == "fluid.recursiveType"
            for item in prop.get("customProperties") or []
        ), raw
    kept = _field_to_property(
        {"name": "ts", "type": "timestamp_ltz(3)"}, None, include_quality=False
    )
    assert kept["customProperties"][-1]["property"] == "fluid.recursiveType"
