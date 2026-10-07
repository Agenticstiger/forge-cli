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

"""A typo'd Iceberg catalog is reported once, as a typo.

``catalog: lakekeper`` is refused (the answer decides whether a Glue table
exists), and that refusal is the whole story. Before, the same contract also
drew a Lake Formation refusal advising the author to "govern access in the
lakekeper catalog", and the native AWS planner logged the refusal as a
``plan_failed`` JSON line before the IaC raised it again.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from fluid_build.iac.base import UnsupportedBindingError
from fluid_build.iac.catalog_moves import catalog_move_spec
from fluid_build.iac.governance_validation import validate_governance
from fluid_build.iac.iceberg_validation import validate_iceberg_bindings
from fluid_build.iac.providers.aws import AwsIacPlugin
from fluid_build.providers.base import ProviderError

pytestmark = pytest.mark.unit

ANALYST = "arn:aws:iam::123456789012:role/analyst"


def _contract(catalog: str, *, lake_formation: bool = True) -> Dict[str, Any]:
    binding: Dict[str, Any] = {
        "platform": "aws",
        "format": "iceberg",
        "location": {
            "database": "sales",
            "table": "orders",
            "bucket": "lake",
            "region": "us-east-1",
            "catalog": catalog,
        },
    }
    if lake_formation:
        binding["governance"] = {
            "lakeFormation": {
                "grants": [{"principal": ANALYST, "permissions": ["SELECT", "DESCRIBE"]}]
            }
        }
    return {
        "fluidVersion": "0.7.6",
        "kind": "DataProduct",
        "id": "gold.orders",
        "name": "orders",
        "metadata": {"layer": "Gold"},
        "exposes": [
            {
                "exposeId": "orders",
                "kind": "table",
                "binding": binding,
                "contract": {"schema": [{"name": "id", "type": "string"}]},
            }
        ],
    }


def test_validate_reports_the_typo_and_not_a_lake_formation_refusal():
    contract = _contract("lakekeper")
    gov_errors, _ = validate_governance(contract)
    ice_errors, _ = validate_iceberg_bindings(contract)
    assert not any("lakekeper' catalog" in e for e in gov_errors), gov_errors
    assert any("lakekeper" in e for e in ice_errors), ice_errors


def test_apply_refuses_the_typo_first():
    with pytest.raises(UnsupportedBindingError) as info:
        AwsIacPlugin().emit(_contract("lakekeper"))
    assert info.value.kind == "unknown-iceberg-catalog"


def test_a_real_external_catalog_still_refuses_lake_formation():
    with pytest.raises(UnsupportedBindingError) as info:
        AwsIacPlugin().emit(_contract("lakekeeper"))
    assert info.value.kind == "lake-formation-needs-glue-catalog"


def test_the_native_planner_refusal_is_not_logged_as_a_plan_failure(monkeypatch):
    from fluid_build.providers.aws.provider import AwsProvider

    provider = AwsProvider(account_id="123456789012", region="us-east-1")
    logged = []
    monkeypatch.setattr(provider, "err_kv", lambda **kw: logged.append(kw))
    with pytest.raises(ProviderError) as info:
        provider.plan(_contract("lakekeper", lake_formation=False))
    assert "lakekeper" in str(info.value)
    assert logged == [], logged


def test_only_a_real_catalog_move_spec_counts():
    class _Plugin:
        name = "aws"

        def catalog_move_spec(self):
            return {"not": "a spec"}

    assert catalog_move_spec(_Plugin()) is None
