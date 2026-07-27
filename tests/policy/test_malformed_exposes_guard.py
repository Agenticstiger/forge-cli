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

"""Regression tests: a malformed ``exposes`` must not crash governance passes.

``exposes: "probe_table"`` (a bare string instead of a list) made every
governance validator iterate the string's characters and call
``"p".get(...)``. The resulting ``AttributeError`` surfaced as
``cli_unhandled_exception: 'str' object has no attribute 'get'`` — with
no file, path, field or line — and discarded the correct JSON-schema
error (``exposes: 'probe_table' is not of type 'array'``) that had
already been computed one pass earlier.
"""

from __future__ import annotations

import pytest

from fluid_build.policy._common import iter_exposes

MALFORMED = [
    {"exposes": "probe_table"},
    {"exposes": ["probe_table"]},
    {"exposes": 42},
    {"exposes": {"exposeId": "t"}},
    {"exposes": None},
    {},
]


@pytest.mark.parametrize("contract", MALFORMED)
def test_iter_exposes_never_yields_a_non_dict(contract):
    assert all(isinstance(e, dict) for e in iter_exposes(contract))


def test_iter_exposes_keeps_well_formed_entries():
    contract = {"exposes": [{"exposeId": "a"}, "junk", {"exposeId": "b"}]}
    assert [e["exposeId"] for e in iter_exposes(contract)] == ["a", "b"]


@pytest.mark.parametrize("contract", MALFORMED)
def test_agent_policy_validator_survives(contract):
    from fluid_build.policy.agent_policy import AgentPolicyValidator

    is_valid, violations = AgentPolicyValidator().validate(contract)
    assert is_valid is True
    assert violations == []


@pytest.mark.parametrize("contract", MALFORMED)
def test_sovereignty_validator_survives(contract):
    from fluid_build.policy.sovereignty import SovereigntyValidator

    payload = dict(contract)
    payload["sovereignty"] = {
        "jurisdiction": "EU",
        "dataResidency": True,
        "crossBorderTransfer": False,
    }
    # Must not raise; the JSON-schema pass reports the malformed shape.
    SovereigntyValidator().validate(payload)
