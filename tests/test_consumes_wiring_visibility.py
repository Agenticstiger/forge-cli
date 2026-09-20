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
from unittest.mock import patch

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

    def test_the_helper_is_actually_wired_into_generation(self, tmp_path, monkeypatch, caplog):
        """Run the real CLI end-to-end and assert the warning reaches a user.

        Every other test here calls `_warn_unwired_consumes` directly, which
        proves the helper works and nothing about whether anything CALLS it.
        Deleting the single line at the call site left all of them green --
        the feature could be removed silently. This test fails if that line
        goes, because it drives `fluid generate speed-transformation` over
        the shipped customer-360 contract and looks for the warning.
        """
        import shutil

        from fluid_build.cli import generate_speed_transformation as gst

        workdir = tmp_path / "ws"
        workdir.mkdir()
        shutil.copy(CUSTOMER_360, workdir / "contract.fluid.yaml")
        monkeypatch.chdir(workdir)

        args = SimpleNamespace(
            contract=str(workdir / "contract.fluid.yaml"),
            output=str(workdir / "out"),
            build_index=0,
            model=None,
            all_builds=False,
            concurrency=1,
            overwrite=True,
            env=None,
            list=False,
            verbose=False,
            quiet=True,
            mesh_hub=None,
            model_contracts=False,
            dbt_tests_key="auto",
            dbt_validate=False,
        )

        with caplog.at_level(logging.WARNING, logger="fluid.cli"):
            gst.run(args, logging.getLogger("fluid.cli.test"))

        blob = " ".join(r.getMessage() for r in caplog.records)
        assert "consumes_not_wired" in blob, (
            "generation did not emit the unwired-consumes warning -- is the "
            f"helper still called from run()? got: {blob[:400]}"
        )
        # and it names a real declared upstream, not just a count
        assert "raw_customers_v1" in blob

    def test_the_copilot_generate_path_is_wired_too(self, tmp_path, caplog):
        """There are exactly TWO engine.generate() call sites; both must warn.

        The first version of this feature wired only
        ``generate_speed_transformation``. The copilot/template path in
        ``_template_mode._generate_engine_artifacts`` calls the same engines
        with the same contracts and warned about nothing, so a contract
        authored through ``fluid forge`` dropped its declared upstreams in
        exactly the silence this feature exists to break.

        Driven through the real function with a stub engine rather than the
        full copilot, which needs an LLM.
        """
        # ``forge_modes`` first: it and ``_template_mode`` import each
        # other, and importing the latter cold trips the half-built module.
        # Normal CLI startup goes through forge_modes, so this mirrors the
        # real import order rather than working around it.
        import fluid_build.cli.forge_modes  # noqa: F401
        from fluid_build.cli import _template_mode as tm

        contract = {
            "id": "mesh.consumer",
            "consumes": [
                {"productId": "bronze.orders_v1", "exposeId": "orders"},
            ],
            "builds": [
                {"id": "b1", "engine": "sql", "pattern": "embedded-logic", "properties": {}}
            ],
        }

        class _StubEngine:
            name = "sql"
            wires_consumes = False

            def validate(self, *a, **kw):
                return []  # _generate_engine_artifacts gates generation on this

            def generate(self, *a, **kw):
                return {"out.sql": "SELECT 1"}

        with (
            patch("fluid_build.engines.get_engine", return_value=_StubEngine()),
            patch("fluid_build.engines.has_engine", return_value=True),
            caplog.at_level(logging.WARNING, logger="fluid.cli"),
        ):
            tm._generate_engine_artifacts(
                contract,
                target_dir=tmp_path,
                context={"build_engine": "sql"},
                discovery_report=None,
                logger=logging.getLogger("fluid.cli.test"),
                console=None,
            )

        blob = " ".join(r.getMessage() for r in caplog.records)
        assert "consumes_not_wired" in blob, (
            "the copilot generate path did not warn -- is _warn_unwired_consumes "
            f"still called from _generate_engine_artifacts? got: {blob[:400]}"
        )
        assert "bronze.orders_v1" in blob

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
