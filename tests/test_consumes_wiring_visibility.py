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

"""A declared upstream that the engine will not wire must be said out loud.

``consumes[]`` is the mesh edge -- the contract's statement of which
upstream data products this one is built from. Only the dbt engine acts
on it. Every other engine generates from the build's own SQL and never
reads ``consumes[]``, so a contract could declare three upstreams,
generate cleanly, exit 0, and emit artifacts that read from somewhere
else entirely.

The shipped ``customer-360`` template is exactly that shape, which is
what makes this worth a test rather than a comment.
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from fluid_build.cli.generate_speed_transformation import _warn_unwired_consumes

pytestmark = pytest.mark.unit


REPO_ROOT = Path(__file__).resolve().parents[1]
CUSTOMER_360 = REPO_ROOT / "fluid_build" / "templates" / "customer-360" / "contract.fluid.yaml"


class _Engine(SimpleNamespace):
    pass


def _contract(*pairs):
    return {"consumes": [{"productId": p, "exposeId": e} for p, e in pairs]}


class TestUnwiredConsumesAreNamed:
    def test_non_wiring_engine_names_every_declared_upstream(self, caplog):
        engine = _Engine(wires_consumes=False)
        contract = _contract(("bronze.orders_v1", "orders"), ("bronze.customers_v1", "customers"))

        with caplog.at_level(logging.WARNING, logger="fluid.cli"):
            unwired = _warn_unwired_consumes(contract, engine, "sql")

        assert unwired == ["bronze.orders_v1.orders", "bronze.customers_v1.customers"]
        blob = " ".join(r.getMessage() for r in caplog.records)
        assert "consumes_not_wired" in blob
        # Naming them is the whole point -- a count alone does not tell an
        # operator which edge silently went missing.
        assert "bronze.orders_v1.orders" in blob
        assert "bronze.customers_v1.customers" in blob

    def test_a_wiring_engine_stays_silent(self, caplog):
        """dbt resolves consumes[] into models/sources.yml, so warning
        there would be noise on the one engine that gets this right."""
        engine = _Engine(wires_consumes=True)

        with caplog.at_level(logging.WARNING, logger="fluid.cli"):
            unwired = _warn_unwired_consumes(
                _contract(("bronze.orders_v1", "orders")), engine, "dbt"
            )

        assert unwired == []
        assert not [r for r in caplog.records if "consumes_not_wired" in r.getMessage()]

    def test_no_consumes_is_silent(self, caplog):
        engine = _Engine(wires_consumes=False)
        with caplog.at_level(logging.WARNING, logger="fluid.cli"):
            assert _warn_unwired_consumes({}, engine, "sql") == []
            assert _warn_unwired_consumes({"consumes": []}, engine, "sql") == []
        assert not [r for r in caplog.records if "consumes_not_wired" in r.getMessage()]

    @pytest.mark.parametrize(
        "consumes",
        [
            "not-a-list",
            [None, 42, "str"],
            [{"exposeId": "orders"}],  # no productId -> nothing addressable to name
        ],
        ids=["not-a-list", "junk-entries", "no-productId"],
    )
    def test_malformed_consumes_does_not_crash_generation(self, consumes):
        """This runs before `engine.generate`. A warning helper that
        raises on a shape the schema happens to allow would turn a
        cosmetic gap into a failed generate."""
        engine = _Engine(wires_consumes=False)
        assert _warn_unwired_consumes({"consumes": consumes}, engine, "sql") == []

    def test_engine_missing_the_attribute_is_treated_as_not_wiring(self):
        """An out-of-tree engine predating the flag must fail safe: warn,
        rather than silently claim it wired the mesh edge."""
        engine = SimpleNamespace()  # no wires_consumes at all
        assert _warn_unwired_consumes(_contract(("bronze.x", "y")), engine, "custom") == [
            "bronze.x.y"
        ]


class TestTheShippedTemplateIsTheMotivatingCase:
    def test_customer_360_declares_upstreams_and_uses_a_non_wiring_engine(self):
        """Pins the real-world shape. If customer-360 is ever migrated to
        dbt, or its consumes[] removed, this test should be revisited --
        it exists to prove the gap is reachable from a shipped template,
        not a synthetic contract.
        """
        contract = yaml.safe_load(CUSTOMER_360.read_text(encoding="utf-8"))

        consumes = contract.get("consumes") or []
        assert len(consumes) >= 1, "customer-360 is expected to declare upstreams"

        engines = {b.get("engine") for b in (contract.get("builds") or [])}
        assert "sql" in engines, f"expected the sql engine; got {engines}"

        from fluid_build.engines import get_engine

        assert get_engine("sql").wires_consumes is False

    def test_dbt_engine_declares_that_it_wires_consumes(self):
        from fluid_build.engines import get_engine

        assert get_engine("dbt").wires_consumes is True

    def test_every_registered_engine_declares_the_capability(self):
        """A new engine must make a deliberate choice here rather than
        inheriting a silent default by accident."""
        from fluid_build.engines import get_engine, list_engines

        for name in list_engines():
            engine = get_engine(name)
            assert isinstance(
                getattr(engine, "wires_consumes", None), bool
            ), f"engine {name!r} does not declare wires_consumes"
