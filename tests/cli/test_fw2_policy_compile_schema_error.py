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

"""A ``policy compile`` crash names the block at fault and the command to run.

``policy compile`` does not schema-validate the contract, so a grant written as
a bare string reaches ``compile_policy`` and fails there with
``AttributeError: 'str' object has no attribute 'get'``. Once that crash
became the command's failure (RT-707-3), it must name the value at fault and
point at ``fluid validate``, not at the agent-policy block or the sovereignty
page that ``policy_compile_failed`` routes to.
"""

from __future__ import annotations

import argparse
import copy
import logging
from pathlib import Path
from typing import Any, Dict

import pytest
import yaml

from fluid_build._errors import _DOC_BASE
from fluid_build.cli import policy_compile
from fluid_build.cli._common import CLIError

pytestmark = pytest.mark.unit

_CONTRACT: Dict[str, Any] = {
    "fluidVersion": "0.7.6",
    "kind": "DataProduct",
    "id": "analytics.pol",
    "name": "Pol",
    "metadata": {"owner": {"team": "data"}, "layer": "Bronze"},
    "accessPolicy": {
        "grants": [{"principal": "group:analysts@example.com", "permissions": ["read"]}]
    },
    "exposes": [
        {
            "exposeId": "orders",
            "kind": "table",
            "binding": {
                "platform": "aws",
                "format": "parquet",
                "location": {"bucket": "acme-lake", "path": "orders/", "region": "eu-west-1"},
            },
            "contract": {"schema": [{"name": "order_id", "type": "integer", "required": True}]},
        }
    ],
}


def _run(tmp_path: Path, doc: Dict[str, Any]) -> CLIError:
    path = tmp_path / "contract.fluid.yaml"
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    args = argparse.Namespace(contract=str(path), out=str(tmp_path / "o" / "b.json"))
    with pytest.raises(CLIError) as excinfo:
        policy_compile.run(args, logging.getLogger("test.fw2.policy"))
    return excinfo.value


def test_a_malformed_grant_is_reported_as_the_schema_error(tmp_path):
    doc = copy.deepcopy(_CONTRACT)
    doc["accessPolicy"]["grants"] = ["group:analysts@example.com"]

    err = _run(tmp_path, doc)

    assert err.event == "policy_compiler_crashed"
    assert err.context["error"] == (
        "policy compile failed on contract values of the wrong type: "
        "accessPolicy.grants[0]: 'group:analysts@example.com' is not of type 'object'"
    )
    assert "AttributeError" not in err.context["error"]
    assert not (tmp_path / "o" / "b.json").exists()


def test_the_suggestion_points_at_fluid_validate_not_the_agent_policy_block(tmp_path, monkeypatch):
    """A crash the schema does not explain keeps the compiler's own error."""

    def _boom(contract):
        raise KeyError("location")

    monkeypatch.setattr("fluid_build.policy.compiler.compile_policy", _boom)
    err = _run(tmp_path, _CONTRACT)

    assert err.context["error"] == "policy compiler failed: KeyError: 'location'"
    assert "fluid validate <contract>" in err.suggestions[0]
    assert "accessPolicy" in err.suggestions[0]
    assert "agent-policy" not in " ".join(err.suggestions)
    assert err.docs_url == f"{_DOC_BASE}/cli/policy-compile.html#errors"
