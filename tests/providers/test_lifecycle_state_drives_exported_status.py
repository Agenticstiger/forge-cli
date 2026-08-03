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

"""``lifecycle.state`` must drive ``status`` in every exported standard.

Both spec exporters used to read ``metadata.status``. The FLUID schema forbids
that key (``metadata`` is ``additionalProperties: false``), so it was never
populated on a valid contract and every export shipped its hard-coded default:
Bitol ODPS said ``draft`` for everything, ODCS said ``active`` for everything.
A retired data product was therefore published to a catalog as an active
contract, and an active one as a draft product.

The field that *is* reachable is ``lifecycle.state`` — enum
preview/active/deprecated/retired, present at the contract root and per expose.
"""

from __future__ import annotations

import copy
from typing import Any, Dict

import pytest

from fluid_build.providers._mapper_common import resolve_status
from fluid_build.providers.odcs import OdcsProvider
from fluid_build.providers.odps_standard import BitolOdpsProvider

pytestmark = pytest.mark.unit


BASE: Dict[str, Any] = {
    "fluidVersion": "0.7.5",
    "kind": "DataProduct",
    "id": "gold.retail.customer_360_v1",
    "name": "Customer 360",
    "domain": "retail",
    "tags": ["snowflake", "customer"],
    "metadata": {"layer": "Gold", "owner": {"team": "data-platform"}},
    "lifecycle": {"state": "active"},
    "exposes": [
        {
            "exposeId": "customer_360",
            "kind": "table",
            "binding": {
                "platform": "snowflake",
                "format": "snowflake_table",
                "location": {
                    "account": "ACME-TEST",
                    "database": "FLUID_TEST",
                    "schema": "GOLD",
                    "table": "CUSTOMER_360",
                },
            },
            "contract": {"schema": [{"name": "CUSTOMER_ID", "type": "decimal(38,0)"}]},
        }
    ],
}


def _with_state(state: str, *, on_expose: bool = False) -> Dict[str, Any]:
    contract = copy.deepcopy(BASE)
    if on_expose:
        contract.pop("lifecycle")
        contract["exposes"][0]["lifecycle"] = {"state": state}
    else:
        contract["lifecycle"] = {"state": state}
    return contract


# The FLUID lifecycleState enum → what each target should publish.
_EXPECTED = {
    "preview": {"odcs": "draft", "odps": "draft"},
    "active": {"odcs": "active", "odps": "active"},
    "deprecated": {"odcs": "deprecated", "odps": "deprecated"},
    "retired": {"odcs": "retired", "odps": "retired"},
}


@pytest.mark.parametrize("state", sorted(_EXPECTED))
def test_root_lifecycle_state_drives_odcs_status(state: str) -> None:
    assert OdcsProvider().render(_with_state(state))["status"] == _EXPECTED[state]["odcs"]


@pytest.mark.parametrize("state", sorted(_EXPECTED))
def test_root_lifecycle_state_drives_bitol_odps_status(state: str) -> None:
    product = BitolOdpsProvider().render(_with_state(state))["product"]
    assert product["status"] == _EXPECTED[state]["odps"]


@pytest.mark.parametrize("state", sorted(_EXPECTED))
def test_expose_lifecycle_state_drives_status(state: str) -> None:
    contract = _with_state(state, on_expose=True)
    assert OdcsProvider().render(contract)["status"] == _EXPECTED[state]["odcs"]
    assert BitolOdpsProvider().render(contract)["product"]["status"] == _EXPECTED[state]["odps"]


def test_expose_state_wins_over_root_state() -> None:
    contract = copy.deepcopy(BASE)
    contract["lifecycle"] = {"state": "active"}
    contract["exposes"][0]["lifecycle"] = {"state": "retired"}
    assert OdcsProvider().render(contract)["status"] == "retired"


def test_per_port_export_does_not_mask_the_root_state() -> None:
    """``_scope_to_expose`` defaulted the scoped status to "active", which
    overrode the contract-root ``lifecycle.state`` for every port whose expose
    had no lifecycle block of its own."""
    contract = _with_state("retired")
    ports = OdcsProvider().render_all_ports(contract)
    assert [odcs["status"] for _, odcs in ports] == ["retired"]


def test_resolve_status_defaults_to_active_without_a_lifecycle() -> None:
    contract = copy.deepcopy(BASE)
    contract.pop("lifecycle")
    assert resolve_status(contract) == "active"


def test_root_domain_and_tags_reach_both_exporters() -> None:
    """``domain`` and ``tags`` are root fields in FLUID and in both targets,
    but only the ``metadata.*`` copies were read — a key an ODCS import writes
    and a hand-written contract never has."""
    contract = copy.deepcopy(BASE)
    odcs = OdcsProvider().render(contract)
    odps = BitolOdpsProvider().render(contract)["product"]
    assert odcs["domain"] == "retail"
    assert odps["domain"] == "retail"
    assert odcs["tags"] == ["snowflake", "customer"]
    assert odps["tags"] == ["snowflake", "customer"]
    assert odcs["name"] == "Customer 360"
