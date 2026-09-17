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

"""``fluid_to_odps_document`` must not stamp a hardcoded schema version.

The emitter's fallback for a contract that declares no ``fluidVersion``
was the literal ``"0.7.3"``. ``fluid validate`` defaults the same document
to ``FluidSchemaManager.latest_bundled_version()``, so from 0.7.4 onwards
the emitted ODPS ``info.version`` disagreed with the version the validator
had just checked the contract against — and it drifted one release further
every time a schema shipped.

Both assertions below resolve the expected value through the same lookup
the code now uses. Pinning a literal here would only move the staleness
from the module into its test.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from fluid_build.providers.opds.serializer import fluid_to_odps_document
from fluid_build.schema_manager import FluidSchemaManager

pytestmark = pytest.mark.unit


def _contract(**extra: Any) -> Dict[str, Any]:
    base: Dict[str, Any] = {
        "id": "bronze.sales.orders",
        "name": "Orders",
        "domain": "sales",
        "description": "Raw orders",
    }
    base.update(extra)
    return base


def test_version_less_contract_gets_the_newest_bundled_stable_schema() -> None:
    doc = fluid_to_odps_document(_contract())

    assert doc["info"]["version"] == FluidSchemaManager.latest_bundled_version()


def test_a_declared_version_is_still_preserved() -> None:
    doc = fluid_to_odps_document(_contract(fluidVersion="0.7.1"))

    assert doc["info"]["version"] == "0.7.1"
