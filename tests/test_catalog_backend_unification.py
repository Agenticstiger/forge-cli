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

"""Catalog backend unification — both surfaces reach every backend.

The CLI exposes two paths for pushing a contract into a metadata
catalog:

* ``fluid publish --target <name>`` — async path through
  ``providers/catalogs/CATALOG_PROVIDERS``
* ``properties.catalog.register: [<name>]`` in the contract — sync
  path through ``build_runners/_catalog.py::register_all``

Before this slice the two surfaces had drifted: ``--target datahub``
was documented but unwired, and ``_REGISTRY`` for the sync path was
empty so contracts couldn't reach any backend either. The plug-in
backend registry collapses both into a single declaration point —
this file pins the round-trip behaviour and the modularity guarantee
("adding a backend = one new file").
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from fluid_build.api.catalog import CatalogRegistrar, RegistrationResult
from fluid_build.api.catalog_backend import (
    CatalogBackendSpec,
    all_catalog_backend_names,
    apply_env_overrides,
    get_catalog_backend,
    register_catalog_backend,
)
from fluid_build.build_runners._catalog import CatalogPlan, build_registrar, register_all
from fluid_build.providers.catalogs import CATALOG_PROVIDERS, get_catalog_provider
from fluid_build.providers.catalogs._registrar_adapter import RegistrarBackedCatalogProvider

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _contract(platform: str = "snowflake") -> Dict[str, Any]:
    return {
        "id": "bronze.x",
        "name": "x",
        "description": "Unification test contract",
        "metadata": {"layer": "Bronze", "owner": {"team": "data-platform", "email": "x@y.z"}},
        "exposes": [
            {
                "name": "orders",
                "binding": {"platform": platform, "location": {"path": "/data/orders/"}},
                "contract": {
                    "schema": [
                        {"name": "id", "type": "STRING"},
                        {"name": "email", "type": "STRING"},
                    ]
                },
            }
        ],
    }


# ---------------------------------------------------------------------------
# CATALOG_PROVIDERS auto-population from plug-in registry
# ---------------------------------------------------------------------------


class TestPluginBackendsExposedViaTarget:
    """Every backend registered via ``register_catalog_backend`` shows
    up in ``CATALOG_PROVIDERS`` with all its declared aliases. Without
    this guarantee ``fluid publish --target datahub`` resolves to
    nothing and falls through to ``ValueError: Unsupported catalog
    type`` — the bug this slice exists to close."""

    @pytest.mark.parametrize(
        "target",
        ["datahub", "openmetadata", "glue", "aws-glue", "snowflake_horizon", "snowflake-horizon"],
    )
    def test_target_is_in_catalog_providers(self, target):
        assert target in CATALOG_PROVIDERS, (
            f"--target {target!r} won't resolve — CATALOG_PROVIDERS keys: "
            f"{sorted(CATALOG_PROVIDERS.keys())}"
        )

    def test_aliases_resolve_to_same_provider_class(self):
        """``glue`` / ``aws-glue`` and ``snowflake_horizon`` /
        ``snowflake-horizon`` are alias pairs."""
        assert CATALOG_PROVIDERS["glue"] is CATALOG_PROVIDERS["aws-glue"]
        assert CATALOG_PROVIDERS["snowflake_horizon"] is CATALOG_PROVIDERS["snowflake-horizon"]

    def test_instantiating_plugin_provider_builds_registrar(self):
        """``get_catalog_provider`` for a plug-in backend returns a
        :class:`RegistrarBackedCatalogProvider` instance whose internal
        registrar is the one declared by the spec's factory."""
        provider = get_catalog_provider("datahub", {"endpoint": "https://dh.x", "api_token": "tok"})
        assert isinstance(provider, RegistrarBackedCatalogProvider)
        assert provider.name == "datahub"
        # The registrar carries the config through unchanged
        assert provider._registrar.base_url == "https://dh.x"
        assert provider._registrar.api_token == "tok"

    def test_native_provider_priority_over_plugin(self):
        """If a plug-in were to declare ``fluid-command-center`` as a
        name, the native provider must keep priority — the registry
        uses ``setdefault`` so native always wins."""
        from fluid_build.providers.catalogs.fluid_cc import FluidCommandCenterProvider

        assert CATALOG_PROVIDERS["fluid-command-center"] is FluidCommandCenterProvider


# ---------------------------------------------------------------------------
# Backend registry public API
# ---------------------------------------------------------------------------


class TestBackendRegistry:
    def test_get_catalog_backend_returns_spec_for_known_name(self):
        spec = get_catalog_backend("datahub")
        assert spec is not None
        assert spec.name == "datahub"
        assert "endpoint" in spec.env_vars
        assert "api_token" in spec.env_vars

    def test_get_catalog_backend_returns_none_for_unknown(self):
        assert get_catalog_backend("nonexistent-future-backend") is None

    def test_aliases_in_all_names(self):
        """``glue`` exposes ``aws-glue`` as an alias."""
        spec = get_catalog_backend("glue")
        assert spec is not None
        assert "aws-glue" in spec.all_names
        assert get_catalog_backend("aws-glue") is spec

    def test_all_catalog_backend_names_covers_known_backends(self):
        names = set(all_catalog_backend_names())
        # Plug-in backends + their aliases
        for required in (
            "datahub",
            "openmetadata",
            "glue",
            "aws-glue",
            "snowflake_horizon",
            "snowflake-horizon",
        ):
            assert required in names, f"backend {required!r} missing from registry: {names}"


# ---------------------------------------------------------------------------
# Modularity — adding a backend is ONE file with one register call
# ---------------------------------------------------------------------------


class TestBackendIsSingleFileAddition:
    """The architectural promise of this slice. A new backend module
    that declares its own spec + factory becomes visible on both
    surfaces (``--target`` and contract-driven) without any edit to
    ``providers/catalogs/__init__.py``, ``config_manager.py``, or
    ``build_runners/_catalog.py``."""

    @pytest.fixture
    def fake_backend(self):
        """Register a fake backend just for the duration of the test."""

        class FakeRegistrar:
            target = "fakecat"

            def __init__(self, endpoint=""):
                self.endpoint = endpoint
                self.last_call = None

            def register(self, product_id, expose_id, contract, classifications):
                self.last_call = (product_id, expose_id, dict(contract))
                return RegistrationResult(
                    target="fakecat",
                    urn=f"fake://{product_id}/{expose_id}",
                    succeeded=True,
                )

            def unregister(self, product_id, expose_id):
                return RegistrationResult(
                    target="fakecat",
                    urn=f"fake://{product_id}/{expose_id}",
                    succeeded=True,
                )

        registrar_instances: list = []

        def factory(config):
            r = FakeRegistrar(endpoint=config.get("endpoint", ""))
            registrar_instances.append(r)
            return r

        # Register, then on test cleanup, unregister by overwriting with a
        # sentinel that fails — the registry doesn't expose a delete, but
        # we use an isolated name so leakage is harmless across tests.
        from fluid_build.api import catalog_backend

        spec = CatalogBackendSpec(
            name="fakecat",
            registrar_factory=factory,
            env_vars={"endpoint": ("FAKECAT_URL",)},
            description="Fake backend for test",
        )
        register_catalog_backend(spec)

        # Re-trigger CATALOG_PROVIDERS auto-population for this new spec.
        from fluid_build.providers.catalogs import CATALOG_PROVIDERS as _CP
        from fluid_build.providers.catalogs._registrar_adapter import (
            build_registrar_backed_provider,
        )

        _CP.setdefault("fakecat", build_registrar_backed_provider(spec))

        yield registrar_instances

        # Cleanup so other tests don't see fakecat hanging around. We
        # mutate the internal registry directly because the public API
        # intentionally doesn't expose remove.
        catalog_backend._BACKENDS.pop("fakecat", None)
        _CP.pop("fakecat", None)

    def test_appears_in_catalog_providers_after_registration(self, fake_backend):
        assert "fakecat" in CATALOG_PROVIDERS

    def test_appears_in_acquisition_registrar_via_build_registrar(self, fake_backend):
        registrar = build_registrar("fakecat", {"endpoint": "https://fake.test"})
        assert registrar is not None
        assert registrar.endpoint == "https://fake.test"

    def test_get_catalog_provider_instantiates(self, fake_backend):
        provider = get_catalog_provider("fakecat", {"endpoint": "https://fake.test"})
        assert isinstance(provider, RegistrarBackedCatalogProvider)
        assert provider.name == "fakecat"

    def test_env_var_override_from_spec_applied(self, fake_backend, monkeypatch):
        monkeypatch.setenv("FAKECAT_URL", "https://via-env.test")
        cfg: Dict[str, Any] = {}
        apply_env_overrides("fakecat", cfg)
        assert cfg == {"endpoint": "https://via-env.test"}


# ---------------------------------------------------------------------------
# Surface A (--target) reaches the registrar end-to-end
# ---------------------------------------------------------------------------


class TestTargetDataHubReachesGmsEndpoint:
    """End-to-end: ``fluid publish --target datahub`` (via
    ``publish_contract``) posts to the DataHub GMS REST endpoint that
    the standalone registrar would. respx asserts the actual HTTP
    call shape so silent regressions in either layer surface here."""

    def test_publish_contract_for_datahub_calls_gms(self, datahub_mock):
        import asyncio

        from fluid_build.providers.catalogs import (
            CatalogAsset,
            get_catalog_provider,
        )

        provider = get_catalog_provider(
            "datahub", {"endpoint": "https://datahub.test", "api_token": "tok"}
        )
        import yaml

        asset = CatalogAsset(
            id="bronze.x",
            name="x",
            description="t",
            type="dataproduct",
            domain="d",
            owner="data-platform",
            owner_email="x@y.z",
            layer="Bronze",
            tags=["pii"],
            version="1.0.0",
            platform="snowflake",
            location={"path": "/data/"},
            schema=[{"name": "id", "type": "STRING"}],
            contract_yaml=yaml.safe_dump(_contract()),
        )
        result = asyncio.run(provider.publish(asset))
        assert result.success, result.error
        assert datahub_mock.entities, (
            "DataHub mock recorded no Dataset ingestion — adapter did not "
            "reach the registrar layer"
        )
        # The DataProduct is the primary entity an operator sees in the
        # UI; the adapter surfaces its URN as ``catalog_url``. The
        # dataset URN comes back in ``details['urn']`` (legacy alias
        # passed through from ``RegistrationResult.urn``→
        # ``PublishResult.details['urn']`` — see the adapter for
        # context). Pin both shapes so future refactors of either layer
        # don't silently drop the linkage.
        assert result.catalog_url and result.catalog_url.startswith("urn:li:dataProduct:")
        assert datahub_mock.proposals_for("dataProduct"), (
            "DataHub mock recorded no DataProduct MCP — register() must "
            "publish the FLUID contract as a DataProduct, not only its "
            "physical dataset assets"
        )


# ---------------------------------------------------------------------------
# Surface B (contract.register: [...]) reaches every plug-in backend AND
# the native async providers via the symmetric adapter
# ---------------------------------------------------------------------------


class TestRegisterAllReachesPluginBackends:
    def test_build_registrar_returns_real_registrar_for_plugin_backend(self):
        r = build_registrar("datahub", {"endpoint": "https://datahub.test"})
        assert r is not None
        # The factory produces a real DataHubRegistrar — not a wrapper
        from fluid_build.build_runners.catalog_registrars.datahub import DataHubRegistrar

        assert isinstance(r, DataHubRegistrar)
        assert r.base_url == "https://datahub.test"

    def test_register_all_dispatches_to_plugin_backend(self, datahub_mock):
        """``register_all`` for a contract listing ``datahub`` reaches
        the same GMS endpoint a manual registrar would — proves the
        backend declaration is the sole config site."""
        plan = CatalogPlan(targets=["datahub"])
        outcome = register_all(
            plan,
            product_id="bronze.x",
            expose_id="orders",
            contract=_contract(),
            target_configs={"datahub": {"endpoint": "https://datahub.test"}},
        )
        assert outcome.results, "register_all returned no results"
        assert outcome.results[0].succeeded, outcome.results[0].error
        assert datahub_mock.entities, "DataHub mock saw no ingestion"

    def test_build_registrar_returns_provider_adapter_for_native_async(self):
        """For native async providers (fluid-command-center), the
        symmetric adapter wraps the async provider behind the sync
        Protocol so contracts can still register them."""
        from fluid_build.build_runners.catalog_registrars._provider_adapter import (
            ProviderBackedRegistrar,
        )

        r = build_registrar("fluid-command-center", {"endpoint": "https://cc.test"})
        assert isinstance(r, ProviderBackedRegistrar)
        assert r.target == "fluid-command-center"

    def test_unknown_target_still_records_failure(self):
        """Preserves the pre-unification behaviour for genuinely
        unknown targets so existing CI scripts that assert on the
        error message keep working."""
        plan = CatalogPlan(targets=["nonexistent-catalog"])
        outcome = register_all(plan, product_id="x", expose_id="y", contract={"exposes": []})
        assert outcome.failed
        assert "No registrar configured" in outcome.failed[0].error


# ---------------------------------------------------------------------------
# Env var resolution — spec-declared mapping is honored by config_manager
# ---------------------------------------------------------------------------


class TestSpecDrivenEnvVarOverrides:
    @pytest.mark.parametrize(
        "env_var,target,expected_key,expected_value",
        [
            ("DATAHUB_GMS_URL", "datahub", "endpoint", "https://dh.from-env"),
            ("DATAHUB_GMS_TOKEN", "datahub", "api_token", "tok-from-env"),
            (
                "OPENMETADATA_SERVER_URL",
                "openmetadata",
                "endpoint",
                "https://om.from-env",
            ),
            ("AWS_REGION", "glue", "region", "eu-west-1"),
            ("AWS_ACCOUNT_ID", "glue", "catalog_id", "123456789012"),
            (
                "SNOWFLAKE_ACCOUNT_URL",
                "snowflake_horizon",
                "endpoint",
                "https://sf.from-env",
            ),
            (
                "SNOWFLAKE_AUTH_TOKEN",
                "snowflake_horizon",
                "api_token",
                "tok-from-env",
            ),
        ],
    )
    def test_upstream_conventional_env_var_resolves(
        self, env_var, target, expected_key, expected_value, monkeypatch
    ):
        # Use the actual value the test expects so the trip from env →
        # config dict is checked for value preservation, not just key.
        monkeypatch.setenv(env_var, expected_value)
        from fluid_build.config_manager import FluidConfig

        config = FluidConfig().get_catalog_config(target)
        assert config.get(expected_key) == expected_value, (
            f"env var {env_var}={expected_value!r} did not land at "
            f"config[{expected_key!r}] for target {target!r}; got {config!r}"
        )

    def test_fluid_namespaced_env_var_takes_priority_over_upstream(self, monkeypatch):
        """``FLUID_CATALOG_*`` is the project-scoped namespace; when
        both are set, fluid wins (matches secrets-pipeline ergonomics
        — you can override per-project without unsetting the shell
        environment)."""
        monkeypatch.setenv("DATAHUB_GMS_URL", "https://shell.test")
        monkeypatch.setenv("FLUID_CATALOG_DATAHUB_URL", "https://project.test")
        from fluid_build.config_manager import FluidConfig

        config = FluidConfig().get_catalog_config("datahub")
        assert config["endpoint"] == "https://project.test"

    def test_no_env_var_leaves_config_alone(self):
        """When neither YAML nor env vars are set, the returned config
        is empty — publish then errors with ``Catalog X not
        configured``, which is the documented honest behaviour."""
        from fluid_build.config_manager import FluidConfig

        config = FluidConfig().get_catalog_config("datahub")
        # Empty config or only the schema-defaults — neither has a
        # real endpoint key that would mask a missing configuration.
        assert "endpoint" not in config or not config["endpoint"]
