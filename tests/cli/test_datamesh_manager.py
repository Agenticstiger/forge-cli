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

"""
Tests for fluid_build.cli.datamesh_manager — the ``fluid dmm`` subcommand.

Covers:
  - Regression test for the _cmd_* logger argument fix
  - Parser registration and subcommand wiring
  - Publish dry-run flow (mocked provider)
  - List / get / delete / teams flows (mocked provider)
  - load_contract_with_overlay receives correct logger
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import yaml

from fluid_build.cli import datamesh_manager as dmm_mod

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

MINIMAL_CONTRACT = {
    "fluidVersion": "0.7.1",
    "kind": "DataProduct",
    "id": "test.dmm.smoke",
    "name": "DMM Smoke Test",
    "description": "Minimal contract for DMM CLI tests",
    "domain": "testing",
    "metadata": {
        "layer": "Bronze",
        "owner": {"team": "test-team", "email": "test@example.com"},
    },
    "consumes": [],
    "exposes": [
        {
            "name": "test_output",
            "type": "table",
            "provider": "local",
            "schema": [
                {"name": "id", "type": "integer"},
                {"name": "name", "type": "string"},
            ],
        }
    ],
}


@pytest.fixture()
def contract_file(tmp_path):
    """Write a minimal FLUID contract YAML and return the path."""
    p = tmp_path / "contract.bundled.yaml"
    p.write_text(yaml.dump(MINIMAL_CONTRACT), encoding="utf-8")
    return str(p)


@pytest.fixture()
def _clean_dmm_env(monkeypatch):
    """Remove any DMM env vars that could interfere."""
    monkeypatch.delenv("DMM_API_KEY", raising=False)
    monkeypatch.delenv("DMM_API_URL", raising=False)


# ---------------------------------------------------------------------------
# 1. Regression: _cmd_* functions accept (args, logger)
# ---------------------------------------------------------------------------


class TestCmdSignatures:
    """Ensure all _cmd_* functions accept two positional args (args, logger).

    This is the regression test for the bug where _execute_command() calls
    ``args.func(args, logger)`` but the DMM handlers only accepted one arg.
    """

    @pytest.mark.parametrize(
        "fn_name",
        [
            "_cmd_publish",
            "_cmd_list",
            "_cmd_get",
            "_cmd_delete",
            "_cmd_teams",
        ],
    )
    def test_cmd_accepts_two_args(self, fn_name):
        """Each _cmd_* function must accept (args, logger)."""
        import inspect

        fn = getattr(dmm_mod, fn_name)
        sig = inspect.signature(fn)
        params = list(sig.parameters.keys())
        assert (
            len(params) >= 2
        ), f"{fn_name} must accept at least 2 params (args, logger), got {params}"
        assert params[0] == "args"
        assert params[1] == "logger"

    @pytest.mark.parametrize(
        "fn_name",
        [
            "_cmd_publish",
            "_cmd_list",
            "_cmd_get",
            "_cmd_delete",
            "_cmd_teams",
        ],
    )
    def test_cmd_logger_has_default(self, fn_name):
        """Logger param should have a default (backwards-compatible)."""
        import inspect

        fn = getattr(dmm_mod, fn_name)
        sig = inspect.signature(fn)
        logger_param = sig.parameters["logger"]
        assert (
            logger_param.default is not inspect.Parameter.empty
        ), f"{fn_name}: logger param must have a default value"


# ---------------------------------------------------------------------------
# 2. Parser registration
# ---------------------------------------------------------------------------


class TestParserRegistration:
    """Validate that add_parser() wires all subcommands correctly."""

    @pytest.fixture()
    def dmm_parser(self):
        root = argparse.ArgumentParser()
        sp = root.add_subparsers(dest="cmd")
        dmm_mod.add_parser(sp)
        return root

    def test_dmm_alias(self, dmm_parser):
        args = dmm_parser.parse_args(["dmm", "list"])
        assert hasattr(args, "func")

    def test_datamesh_manager_full_name(self, dmm_parser):
        args = dmm_parser.parse_args(["datamesh-manager", "list"])
        assert hasattr(args, "func")

    @pytest.mark.parametrize(
        "subcmd,extra_args",
        [
            ("publish", ["contract.yaml"]),
            ("list", []),
            ("get", ["some-product-id"]),
            ("delete", ["some-product-id"]),
            ("teams", []),
        ],
    )
    def test_subcommand_parses(self, dmm_parser, subcmd, extra_args):
        args = dmm_parser.parse_args(["dmm", subcmd] + extra_args)
        assert hasattr(args, "func")
        assert args.dmm_command == subcmd

    def test_publish_flags(self, dmm_parser):
        args = dmm_parser.parse_args(
            [
                "dmm",
                "publish",
                "c.yaml",
                "--dry-run",
                "--with-contract",
                "--no-create-team",
                "--api-key",
                "test-key",
                "--api-url",
                "https://example.com",
                "--team-id",
                "my-team",
                "-o",
                "overlay.yaml",
            ]
        )
        assert args.dry_run is True
        assert args.with_contract is True
        assert args.no_create_team is True
        assert args.api_key == "test-key"
        assert args.api_url == "https://example.com"
        assert args.team_id == "my-team"
        assert args.overlay == "overlay.yaml"

    def test_publish_parser_wiring_strict_invalid_contract_returns_nonzero(self, tmp_path):
        invalid_path = tmp_path / "invalid.yaml"
        invalid_path.write_text(yaml.dump(INVALID_072_CONTRACT), encoding="utf-8")

        root = argparse.ArgumentParser()
        sp = root.add_subparsers(dest="cmd")
        dmm_mod.add_parser(sp)
        args = root.parse_args(
            ["dmm", "publish", str(invalid_path), "--dry-run", "--validation-mode", "strict"]
        )
        args.api_key = "dummy-key"
        args.api_url = "https://api.entropy-data.com"

        with patch.object(dmm_mod, "load_contract_with_overlay", return_value=INVALID_072_CONTRACT):
            code = args.func(args, logging.getLogger("test_dmm_parser_wiring"))

        assert code == 1


# ---------------------------------------------------------------------------
# 3. Publish command
# ---------------------------------------------------------------------------


class TestCmdPublish:
    """Test _cmd_publish with mocked provider and loader."""

    @patch.object(dmm_mod, "load_contract_with_overlay")
    @patch.object(dmm_mod, "DataMeshManagerProvider")
    def test_publish_dry_run(self, MockProvider, mock_loader, contract_file):
        mock_loader.return_value = MINIMAL_CONTRACT
        provider_inst = MagicMock()
        provider_inst.apply.return_value = {
            "method": "PUT",
            "url": "https://api.entropy-data.com/api/dataproducts/test.dmm.smoke",
            "payload": {"info": {"id": "test.dmm.smoke"}},
        }
        MockProvider.return_value = provider_inst

        args = SimpleNamespace(
            contract=contract_file,
            overlay=None,
            dry_run=True,
            with_contract=False,
            no_create_team=False,
            team_id=None,
            api_key="fake-key",
            api_url=None,
        )
        logger = logging.getLogger("test")

        result = dmm_mod._cmd_publish(args, logger)

        assert result == 0
        mock_loader.assert_called_once_with(contract_file, None, logger)
        provider_inst.apply.assert_called_once()
        call_kwargs = provider_inst.apply.call_args[1]
        assert call_kwargs["dry_run"] is True

    @patch.object(dmm_mod, "load_contract_with_overlay")
    @patch.object(dmm_mod, "DataMeshManagerProvider")
    def test_publish_live(self, MockProvider, mock_loader, contract_file):
        mock_loader.return_value = MINIMAL_CONTRACT
        provider_inst = MagicMock()
        provider_inst.apply.return_value = {
            "product_id": "test.dmm.smoke",
            "status": "created",
        }
        MockProvider.return_value = provider_inst

        args = SimpleNamespace(
            contract=contract_file,
            overlay=None,
            dry_run=False,
            with_contract=True,
            no_create_team=True,
            team_id="custom-team",
            api_key="fake-key",
            api_url="https://custom.api.com",
        )
        logger = logging.getLogger("test")

        result = dmm_mod._cmd_publish(args, logger)

        assert result == 0
        call_kwargs = provider_inst.apply.call_args[1]
        assert call_kwargs["dry_run"] is False
        assert call_kwargs["publish_contract"] is True
        assert call_kwargs["create_team"] is False
        assert call_kwargs["team_id"] == "custom-team"
        MockProvider.assert_called_once_with(api_key="fake-key", api_url="https://custom.api.com")

    @patch.object(dmm_mod, "load_contract_with_overlay")
    @patch.object(dmm_mod, "DataMeshManagerProvider")
    def test_publish_provider_error(self, MockProvider, mock_loader, contract_file):
        from fluid_build.providers.base import ProviderError

        mock_loader.return_value = MINIMAL_CONTRACT
        provider_inst = MagicMock()
        provider_inst.apply.side_effect = ProviderError("API key invalid")
        MockProvider.return_value = provider_inst

        args = SimpleNamespace(
            contract=contract_file,
            overlay=None,
            dry_run=False,
            with_contract=False,
            no_create_team=False,
            team_id=None,
            api_key="bad-key",
            api_url=None,
        )

        result = dmm_mod._cmd_publish(args, logging.getLogger("test"))
        assert result == 1

    @patch.object(dmm_mod, "load_contract_with_overlay")
    @patch.object(dmm_mod, "DataMeshManagerProvider")
    def test_publish_logger_default(self, MockProvider, mock_loader, contract_file):
        """Calling _cmd_publish with only args (no logger) should not crash."""
        mock_loader.return_value = MINIMAL_CONTRACT
        provider_inst = MagicMock()
        provider_inst.apply.return_value = {"status": "ok"}
        MockProvider.return_value = provider_inst

        args = SimpleNamespace(
            contract=contract_file,
            overlay=None,
            dry_run=False,
            with_contract=False,
            no_create_team=False,
            team_id=None,
            api_key="key",
            api_url=None,
        )

        # Call with only 1 arg — logger defaults to None
        result = dmm_mod._cmd_publish(args)
        assert result == 0
        # load_contract_with_overlay should still get a valid logger
        call_args = mock_loader.call_args[0]
        assert call_args[2] is not None  # logger fallback works


# ---------------------------------------------------------------------------
# 3b. Publish contract validation against the bundled FLUID master schema
# ---------------------------------------------------------------------------


# A contract that conforms to fluid-schema-0.7.2.json end-to-end. Used to
# verify that publish succeeds cleanly when the input is valid.
VALID_072_CONTRACT = {
    "fluidVersion": "0.7.2",
    "kind": "DataProduct",
    "id": "test.dmm.valid_072",
    "name": "DMM 0.7.2 Valid",
    "description": "Valid 0.7.2 contract for publish-validation tests",
    "domain": "testing",
    "metadata": {
        "layer": "Gold",
        "owner": {"team": "test-team", "email": "test@example.com"},
    },
    "exposes": [
        {
            "exposeId": "test_output",
            "kind": "table",
            "binding": {
                "platform": "local",
                "format": "parquet",
                "location": {"path": "runtime/test_output.parquet"},
            },
            "contract": {
                "schema": [
                    {"name": "id", "type": "integer", "required": True},
                ]
            },
        }
    ],
}

# Deliberately non-conforming: ``exposes[0]`` uses pre-0.7.2 field names
# (``name``/``type``/``provider``/``schema``) that the 0.7.2 schema rejects
# because ``$defs/expose`` has ``additionalProperties: false`` and requires
# ``exposeId``/``kind``/``binding``/``contract``.
INVALID_072_CONTRACT = {
    "fluidVersion": "0.7.2",
    "kind": "DataProduct",
    "id": "test.dmm.invalid_072",
    "name": "DMM 0.7.2 Invalid",
    "description": "Invalid 0.7.2 contract for publish-validation tests",
    "domain": "testing",
    "metadata": {
        "layer": "Bronze",
        "owner": {"team": "test-team"},
    },
    "exposes": [
        {
            "name": "test_output",
            "type": "table",
            "provider": "local",
            "schema": [{"name": "id", "type": "integer"}],
        }
    ],
}


class TestCmdPublishSchemaValidation:
    """The DMM publish path is the designated master-coordinator for pushing
    FLUID data products to data-mesh-manager. These tests lock the behavior
    where `_cmd_publish` validates the loaded contract against the bundled
    master schema (fluid-schema-0.7.2.json) BEFORE calling the provider.

    Two modes are honored via the existing ``--validation-mode`` flag:
      * ``strict``: abort the publish (return 1) on any schema error, and
        never invoke ``provider.apply``
      * ``warn``   (default): log errors and continue — preserving backward
        compatibility for contracts that carry extension fields
    """

    def _make_args(self, contract_path: str, validation_mode: str) -> SimpleNamespace:
        return SimpleNamespace(
            contract=contract_path,
            overlay=None,
            dry_run=True,
            with_contract=False,
            no_create_team=False,
            team_id=None,
            api_key="fake-key",
            api_url=None,
            validation_mode=validation_mode,
        )

    @patch.object(dmm_mod, "load_contract_with_overlay")
    @patch.object(dmm_mod, "DataMeshManagerProvider")
    def test_strict_mode_aborts_on_invalid_contract(self, MockProvider, mock_loader, tmp_path):
        """Strict mode must return 1 and NEVER call ``provider.apply`` when
        the contract fails schema validation."""
        mock_loader.return_value = INVALID_072_CONTRACT
        provider_inst = MagicMock()
        MockProvider.return_value = provider_inst

        path = tmp_path / "invalid.yaml"
        path.write_text(yaml.dump(INVALID_072_CONTRACT), encoding="utf-8")

        result = dmm_mod._cmd_publish(
            self._make_args(str(path), "strict"),
            logging.getLogger("test"),
        )

        assert result == 1
        provider_inst.apply.assert_not_called()

    @patch.object(dmm_mod, "load_contract_with_overlay")
    @patch.object(dmm_mod, "DataMeshManagerProvider")
    def test_warn_mode_continues_on_invalid_contract(self, MockProvider, mock_loader, tmp_path):
        """Warn mode must print errors but still invoke ``provider.apply``
        so existing workflows that rely on extension fields keep working."""
        mock_loader.return_value = INVALID_072_CONTRACT
        provider_inst = MagicMock()
        provider_inst.apply.return_value = {
            "method": "PUT",
            "url": "https://api.entropy-data.com/api/dataproducts/test.dmm.invalid_072",
            "payload": {"info": {"id": "test.dmm.invalid_072"}},
        }
        MockProvider.return_value = provider_inst

        path = tmp_path / "invalid.yaml"
        path.write_text(yaml.dump(INVALID_072_CONTRACT), encoding="utf-8")

        result = dmm_mod._cmd_publish(
            self._make_args(str(path), "warn"),
            logging.getLogger("test"),
        )

        assert result == 0
        provider_inst.apply.assert_called_once()

    @patch.object(dmm_mod, "run_on_contract_dict", side_effect=RuntimeError("schema exploded"))
    def test_validation_fallback_logs_type_and_traceback_warn_mode(self, _mock_validate, caplog):
        logger = logging.getLogger("test_dmm_warn_fallback")

        with caplog.at_level(logging.WARNING, logger=logger.name):
            result = dmm_mod._validate_fluid_contract({}, "warn", logger)

        assert result == 0
        record = caplog.records[-1]
        assert "fluid_contract_validation_failed_to_run" in record.getMessage()
        assert "RuntimeError" in record.getMessage()
        assert record.exc_info is not None

    @patch.object(dmm_mod, "run_on_contract_dict", side_effect=RuntimeError("schema exploded"))
    def test_validation_fallback_logs_error_in_strict_mode(self, _mock_validate, caplog):
        logger = logging.getLogger("test_dmm_strict_fallback")

        with caplog.at_level(logging.ERROR, logger=logger.name):
            result = dmm_mod._validate_fluid_contract({}, "strict", logger)

        assert result == 1
        record = caplog.records[-1]
        assert record.levelno == logging.ERROR
        assert "RuntimeError" in record.getMessage()
        assert record.exc_info is not None

    @patch.object(dmm_mod, "load_contract_with_overlay")
    @patch.object(dmm_mod, "DataMeshManagerProvider")
    def test_strict_mode_allows_valid_contract(self, MockProvider, mock_loader, tmp_path):
        """Strict mode must NOT reject a contract that conforms to 0.7.2."""
        mock_loader.return_value = VALID_072_CONTRACT
        provider_inst = MagicMock()
        provider_inst.apply.return_value = {
            "method": "PUT",
            "url": "https://api.entropy-data.com/api/dataproducts/test.dmm.valid_072",
            "payload": {"info": {"id": "test.dmm.valid_072"}},
        }
        MockProvider.return_value = provider_inst

        path = tmp_path / "valid.yaml"
        path.write_text(yaml.dump(VALID_072_CONTRACT), encoding="utf-8")

        result = dmm_mod._cmd_publish(
            self._make_args(str(path), "strict"),
            logging.getLogger("test"),
        )

        assert result == 0
        provider_inst.apply.assert_called_once()

    @patch.object(dmm_mod, "load_contract_with_overlay")
    @patch.object(dmm_mod, "DataMeshManagerProvider")
    def test_default_mode_is_warn(self, MockProvider, mock_loader, tmp_path):
        """When ``validation_mode`` is absent from the Namespace, the default
        must be ``warn`` (i.e. publish proceeds). This preserves backward
        compatibility for older call sites."""
        mock_loader.return_value = INVALID_072_CONTRACT
        provider_inst = MagicMock()
        provider_inst.apply.return_value = {"payload": {"info": {"id": "x"}}}
        MockProvider.return_value = provider_inst

        path = tmp_path / "invalid.yaml"
        path.write_text(yaml.dump(INVALID_072_CONTRACT), encoding="utf-8")

        args = SimpleNamespace(
            contract=str(path),
            overlay=None,
            dry_run=True,
            with_contract=False,
            no_create_team=False,
            team_id=None,
            api_key="k",
            api_url=None,
            # validation_mode intentionally omitted
        )

        result = dmm_mod._cmd_publish(args, logging.getLogger("test"))

        assert result == 0
        provider_inst.apply.assert_called_once()


# ---------------------------------------------------------------------------
# 3b-bis. Backward-compat: publish validates against the contract's own
#          declared fluidVersion, NOT a hardcoded master version.
# ---------------------------------------------------------------------------


class TestCmdPublishBackwardCompatibleValidation:
    """``fluid dmm publish`` must honor the contract's declared
    ``fluidVersion`` when validating. Upgrading the CLI must never
    invalidate a contract that was valid against its own version — the
    CLI coordinates publishes across the whole FLUID version range, not
    just the latest.

    These tests exercise the real publish path (loader → validator →
    provider dry-run) on the three FLUID versions that ship a usable
    lineage/minimal contract fixture: 0.5.7, 0.7.1, 0.7.2.
    """

    _FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "contracts" / "compatibility"

    @pytest.mark.parametrize(
        "fixture_name,expected_version",
        [
            ("minimal_057.yaml", "0.5.7"),
            ("minimal_071.yaml", "0.7.1"),
            ("minimal_072.yaml", "0.7.2"),
            ("lineage_071.yaml", "0.7.1"),
            ("lineage_072.yaml", "0.7.2"),
        ],
    )
    def test_strict_publish_succeeds_for_every_bundled_fluid_version(
        self, fixture_name, expected_version, caplog
    ):
        """Strict-mode publish on a contract that conforms to its own
        declared version must succeed — regardless of whether that
        version matches the CLI's ``latest_bundled_version``."""
        import logging as stdlib_logging

        fixture_path = self._FIXTURES_DIR / fixture_name
        assert fixture_path.exists(), f"missing fixture: {fixture_path}"

        args = SimpleNamespace(
            contract=str(fixture_path),
            overlay=None,
            dry_run=True,
            with_contract=False,
            no_create_team=True,
            team_id=None,
            contract_format="odcs",
            data_product_spec=None,
            validate_generated_contracts=False,
            validation_mode="strict",
            fail_on_contract_error=False,
            provider="odps",
            api_key="dummy-key",
            api_url="https://api.entropy-data.com",
        )

        with caplog.at_level(stdlib_logging.INFO):
            code = dmm_mod._cmd_publish(
                args,
                stdlib_logging.getLogger(f"test_bc_{expected_version}"),
            )

        # Strict mode must accept the fixture against ITS OWN version.
        assert code == 0, (
            f"{fixture_name} was rejected in strict mode even though it "
            f"conforms to its declared fluidVersion={expected_version}"
        )

    def test_validation_uses_contracts_own_version_not_latest(self):
        """Direct assertion via the public ``run_on_contract_dict`` API:
        the ``ValidationResult.schema_version`` must equal whatever the
        contract declared, not the CLI's latest bundled version. This is
        the guardrail that prevents a future contributor from hardcoding
        ``schema_version=latest`` on the publish path."""
        import logging as stdlib_logging

        from fluid_build.cli.validate import run_on_contract_dict
        from fluid_build.schema_manager import FluidSchemaManager

        latest = FluidSchemaManager.latest_bundled_version()

        for fixture_name, expected in [
            ("minimal_057.yaml", "0.5.7"),
            ("minimal_071.yaml", "0.7.1"),
            ("minimal_072.yaml", "0.7.2"),
        ]:
            with (self._FIXTURES_DIR / fixture_name).open() as f:
                contract = yaml.safe_load(f)

            result, rc = run_on_contract_dict(
                contract,
                strict=False,
                logger=stdlib_logging.getLogger("test_bc_direct"),
                offline_only=True,
            )
            assert rc == 0
            # The critical assertion: validated against the DECLARED version
            # even when that version is not the latest bundled.
            assert str(result.schema_version) == expected
            if expected != latest:
                # Sanity: we really did exercise a non-latest path.
                assert str(result.schema_version) != latest


# ---------------------------------------------------------------------------
# 3c. End-to-end publish integration (no provider mock)
# ---------------------------------------------------------------------------


class TestCmdPublishEndToEnd:
    """Exercises the full publish pipeline without mocking the provider:

        on-disk fixture → load_contract_with_overlay
                       → _validate_fluid_contract (strict)
                       → DataMeshManagerProvider.apply(dry_run=True)
                       → payload assertions

    Because ``dry_run=True`` short-circuits the HTTP layer inside
    ``DataMeshManagerProvider`` before any request is built, no network
    stubs are required. This test is the one that would catch a wiring
    regression anywhere along the publish chain — schema manager, loader,
    validator, provider, ODPS renderer — that unit tests with mocks would
    silently paper over.
    """

    _FIXTURE = (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "contracts"
        / "compatibility"
        / "lineage_072.yaml"
    )

    def test_publish_dry_run_strict_mode_renders_odps_payload(self, capsys):
        assert self._FIXTURE.exists(), f"missing fixture: {self._FIXTURE}"

        args = SimpleNamespace(
            contract=str(self._FIXTURE),
            overlay=None,
            dry_run=True,
            with_contract=False,
            no_create_team=True,
            team_id=None,
            contract_format="odcs",
            data_product_spec=None,
            validate_generated_contracts=False,
            validation_mode="strict",  # aborts publish if the fixture ever stops conforming
            fail_on_contract_error=False,
            provider="odps",  # route through the ODPS-Bitol path
            api_key="dummy-key-for-dry-run",
            api_url="https://api.entropy-data.com",
        )

        code = dmm_mod._cmd_publish(args, logging.getLogger("test_e2e_publish"))

        # The entire pipeline must succeed:
        #   1. Loader returns a dict
        #   2. Strict master-schema validation passes on the real fixture
        #   3. DataMeshManagerProvider.apply(dry_run=True) returns a payload
        #   4. Printing the dry-run result does not raise
        assert code == 0

    def test_publish_dry_run_payload_preserves_inputports_from_consumes(self):
        """The ODPS-Bitol renderer path must surface FLUID ``consumes[]``
        as ``inputPorts`` on the DMM payload. This is the integration-level
        cousin of the unit test in ``test_odps_standard`` — the difference
        is that this test walks through the real DMM provider and the real
        loader, so any wiring drift between them surfaces here first."""
        import yaml

        with self._FIXTURE.open("r", encoding="utf-8") as fh:
            fixture_contract = yaml.safe_load(fh)

        # Bypass _cmd_publish so we can inspect the payload directly.
        provider = dmm_mod.DataMeshManagerProvider(
            api_key="dummy", api_url="https://api.entropy-data.com"
        )
        result = provider.apply(
            fixture_contract,
            dry_run=True,
            provider_hint="odps",
        )

        payload = result["payload"]
        assert payload["kind"] == "DataProduct"
        assert payload["id"] == fixture_contract["id"]

        expected_refs = [c["productId"] for c in fixture_contract["consumes"]]
        expected_ids = [c["exposeId"] for c in fixture_contract["consumes"]]
        actual_refs = [p["reference"] for p in payload["inputPorts"]]
        actual_ids = [p["id"] for p in payload["inputPorts"]]
        assert actual_refs == expected_refs
        assert actual_ids == expected_ids

        # 0.7.2 ``consumeRef`` doesn't carry ``contractId`` / ``required``,
        # so the renderer must not fabricate them.
        for port in payload["inputPorts"]:
            assert "contractId" not in port
            assert "required" not in port


# ---------------------------------------------------------------------------
# 4. List command
# ---------------------------------------------------------------------------


class TestCmdList:
    @patch.object(dmm_mod, "DataMeshManagerProvider")
    def test_list_json(self, MockProvider):
        provider_inst = MagicMock()
        provider_inst.list_products.return_value = [
            {"info": {"id": "prod-1", "name": "Product 1", "status": "active"}, "teamId": "t1"},
        ]
        MockProvider.return_value = provider_inst

        args = SimpleNamespace(api_key="key", api_url=None, format="json")
        result = dmm_mod._cmd_list(args, logging.getLogger("test"))
        assert result == 0

    @patch.object(dmm_mod, "DataMeshManagerProvider")
    def test_list_table(self, MockProvider):
        provider_inst = MagicMock()
        provider_inst.list_products.return_value = []
        MockProvider.return_value = provider_inst

        args = SimpleNamespace(api_key="key", api_url=None, format="table")
        result = dmm_mod._cmd_list(args, logging.getLogger("test"))
        assert result == 0

    @patch.object(dmm_mod, "DataMeshManagerProvider")
    def test_list_error(self, MockProvider):
        from fluid_build.providers.base import ProviderError

        provider_inst = MagicMock()
        provider_inst.list_products.side_effect = ProviderError("fail")
        MockProvider.return_value = provider_inst

        args = SimpleNamespace(api_key="key", api_url=None, format="table")
        result = dmm_mod._cmd_list(args, logging.getLogger("test"))
        assert result == 1


# ---------------------------------------------------------------------------
# 5. Get command
# ---------------------------------------------------------------------------


class TestCmdGet:
    @patch.object(dmm_mod, "DataMeshManagerProvider")
    def test_get_success(self, MockProvider):
        provider_inst = MagicMock()
        provider_inst.verify.return_value = {"info": {"id": "p1"}}
        MockProvider.return_value = provider_inst

        args = SimpleNamespace(product_id="p1", api_key="key", api_url=None)
        result = dmm_mod._cmd_get(args, logging.getLogger("test"))
        assert result == 0
        provider_inst.verify.assert_called_once_with("p1")

    @patch.object(dmm_mod, "DataMeshManagerProvider")
    def test_get_not_found(self, MockProvider):
        from fluid_build.providers.base import ProviderError

        provider_inst = MagicMock()
        provider_inst.verify.side_effect = ProviderError("Not found")
        MockProvider.return_value = provider_inst

        args = SimpleNamespace(product_id="missing", api_key="key", api_url=None)
        result = dmm_mod._cmd_get(args, logging.getLogger("test"))
        assert result == 1


# ---------------------------------------------------------------------------
# 6. Delete command
# ---------------------------------------------------------------------------


class TestCmdDelete:
    @patch.object(dmm_mod, "DataMeshManagerProvider")
    def test_delete_with_yes(self, MockProvider):
        provider_inst = MagicMock()
        provider_inst.delete.return_value = True
        MockProvider.return_value = provider_inst

        args = SimpleNamespace(product_id="p1", api_key="key", api_url=None, yes=True)
        result = dmm_mod._cmd_delete(args, logging.getLogger("test"))
        assert result == 0
        provider_inst.delete.assert_called_once_with("p1")

    @patch.object(dmm_mod, "DataMeshManagerProvider")
    def test_delete_cancelled(self, MockProvider):
        args = SimpleNamespace(product_id="p1", api_key="key", api_url=None, yes=False)
        with patch("builtins.input", return_value="n"):
            result = dmm_mod._cmd_delete(args, logging.getLogger("test"))
        assert result == 0
        MockProvider.return_value.delete.assert_not_called()

    @patch.object(dmm_mod, "DataMeshManagerProvider")
    def test_delete_fails(self, MockProvider):
        provider_inst = MagicMock()
        provider_inst.delete.return_value = False
        MockProvider.return_value = provider_inst

        args = SimpleNamespace(product_id="p1", api_key="key", api_url=None, yes=True)
        result = dmm_mod._cmd_delete(args, logging.getLogger("test"))
        assert result == 1


# ---------------------------------------------------------------------------
# 7. Teams command
# ---------------------------------------------------------------------------


class TestCmdTeams:
    @patch.object(dmm_mod, "DataMeshManagerProvider")
    def test_teams_json(self, MockProvider):
        provider_inst = MagicMock()
        provider_inst.list_teams.return_value = [
            {"id": "t1", "name": "Team One"},
        ]
        MockProvider.return_value = provider_inst

        args = SimpleNamespace(api_key="key", api_url=None, format="json")
        result = dmm_mod._cmd_teams(args, logging.getLogger("test"))
        assert result == 0

    @patch.object(dmm_mod, "DataMeshManagerProvider")
    def test_teams_table(self, MockProvider):
        provider_inst = MagicMock()
        provider_inst.list_teams.return_value = []
        MockProvider.return_value = provider_inst

        args = SimpleNamespace(api_key="key", api_url=None, format="table")
        result = dmm_mod._cmd_teams(args, logging.getLogger("test"))
        assert result == 0


# ---------------------------------------------------------------------------
# 8. _make_provider wiring
# ---------------------------------------------------------------------------


class TestMakeProvider:
    @patch.object(dmm_mod, "DataMeshManagerProvider")
    def test_api_key_and_url_passed(self, MockProvider):
        args = SimpleNamespace(api_key="my-key", api_url="https://custom.com")
        dmm_mod._make_provider(args)
        MockProvider.assert_called_once_with(api_key="my-key", api_url="https://custom.com")

    @patch.object(dmm_mod, "DataMeshManagerProvider")
    def test_no_key_no_url(self, MockProvider):
        args = SimpleNamespace(api_key=None, api_url=None)
        dmm_mod._make_provider(args)
        MockProvider.assert_called_once_with()

    @patch.object(dmm_mod, "DataMeshManagerProvider")
    def test_only_key(self, MockProvider):
        args = SimpleNamespace(api_key="k", api_url=None)
        dmm_mod._make_provider(args)
        MockProvider.assert_called_once_with(api_key="k")


# ---------------------------------------------------------------------------
# 9. Integration: _execute_command dispatch simulation
# ---------------------------------------------------------------------------


class TestExecuteCommandDispatch:
    """Simulate the exact dispatch pattern from cli/__init__.py:432:
    args.func(args, self.logger.logger if self.logger else LOG)
    """

    @patch.object(dmm_mod, "load_contract_with_overlay")
    @patch.object(dmm_mod, "DataMeshManagerProvider")
    def test_dispatch_publish(self, MockProvider, mock_loader, contract_file):
        mock_loader.return_value = MINIMAL_CONTRACT
        provider_inst = MagicMock()
        provider_inst.apply.return_value = {"status": "ok"}
        MockProvider.return_value = provider_inst

        root = argparse.ArgumentParser()
        sp = root.add_subparsers(dest="cmd")
        dmm_mod.add_parser(sp)

        args = root.parse_args(["dmm", "publish", contract_file, "--dry-run"])

        # This is how _execute_command calls it:
        logger = logging.getLogger("fluid_build.cli")
        result = args.func(args, logger)

        assert result == 0

    @patch.object(dmm_mod, "DataMeshManagerProvider")
    def test_dispatch_list(self, MockProvider):
        provider_inst = MagicMock()
        provider_inst.list_products.return_value = []
        MockProvider.return_value = provider_inst

        root = argparse.ArgumentParser()
        sp = root.add_subparsers(dest="cmd")
        dmm_mod.add_parser(sp)

        args = root.parse_args(["dmm", "list", "--api-key", "k"])
        logger = logging.getLogger("fluid_build.cli")
        result = args.func(args, logger)
        assert result == 0

    @patch.object(dmm_mod, "DataMeshManagerProvider")
    def test_dispatch_get(self, MockProvider):
        provider_inst = MagicMock()
        provider_inst.verify.return_value = {}
        MockProvider.return_value = provider_inst

        root = argparse.ArgumentParser()
        sp = root.add_subparsers(dest="cmd")
        dmm_mod.add_parser(sp)

        args = root.parse_args(["dmm", "get", "some-id", "--api-key", "k"])
        logger = logging.getLogger("fluid_build.cli")
        result = args.func(args, logger)
        assert result == 0

    @patch.object(dmm_mod, "DataMeshManagerProvider")
    def test_dispatch_delete(self, MockProvider):
        provider_inst = MagicMock()
        provider_inst.delete.return_value = True
        MockProvider.return_value = provider_inst

        root = argparse.ArgumentParser()
        sp = root.add_subparsers(dest="cmd")
        dmm_mod.add_parser(sp)

        args = root.parse_args(["dmm", "delete", "p1", "--api-key", "k", "-y"])
        logger = logging.getLogger("fluid_build.cli")
        result = args.func(args, logger)
        assert result == 0

    @patch.object(dmm_mod, "DataMeshManagerProvider")
    def test_dispatch_teams(self, MockProvider):
        provider_inst = MagicMock()
        provider_inst.list_teams.return_value = []
        MockProvider.return_value = provider_inst

        root = argparse.ArgumentParser()
        sp = root.add_subparsers(dest="cmd")
        dmm_mod.add_parser(sp)

        args = root.parse_args(["dmm", "teams", "--api-key", "k"])
        logger = logging.getLogger("fluid_build.cli")
        result = args.func(args, logger)
        assert result == 0


# ---------------------------------------------------------------------------
# 10. Data Product Specification conformance
# ---------------------------------------------------------------------------

from fluid_build.providers.datamesh_manager import DataMeshManagerProvider

RICH_CONTRACT = {
    "fluidVersion": "0.7.1",
    "kind": "DataProduct",
    "id": "bronze.bss.accounts",
    "name": "BSS Customer Accounts",
    "description": "CRM party master",
    "domain": "BSS",
    "metadata": {
        "name": "BSS Customer Accounts",
        "layer": "Bronze",
        "status": "active",
        "owner": {"team": "analytics-coe", "email": "a@b.com"},
        "archetype": "source-aligned",
        "maturity": "managed",
    },
    "tags": ["bronze-layer", "bss"],
    "labels": {"layer": "bronze", "domain": "bss"},
    "owner": {"team": "analytics-coe"},
    "exposes": [
        {
            "id": "customer_accounts",
            "name": "Customer Accounts",
            "description": "One row per customer",
            "kind": "table",
            "binding": {
                "platform": "gcp",
                "location": {
                    "project": "ducci-sandbox",
                    "dataset": "bronze_bss",
                    "table": "customer_accounts_raw",
                },
            },
            "schema": {
                "fields": [
                    {
                        "name": "customer_id",
                        "type": "string",
                        "required": True,
                        "description": "Unique ID",
                        "primaryKey": True,
                    },
                    {
                        "name": "full_name",
                        "type": "string",
                        "description": "Customer name",
                        "classification": "pii",
                    },
                    {"name": "created_at", "type": "timestamp"},
                ]
            },
            "policy": {
                "dq": {
                    "rules": [
                        {
                            "type": "not_null",
                            "description": "ID required",
                            "selector": "customer_id",
                            "severity": "error",
                        },
                    ]
                }
            },
        }
    ],
    "expects": [
        {
            "id": "crm-source",
            "name": "CRM System",
            "description": "CRM CDC feed",
            "provider": "gcp",
            "sourceSystem": "bss-crm",
            "gcp": {"project": "ducci-sandbox", "dataset": "raw_bss", "table": "crm_export"},
        }
    ],
    "sla": {"freshness": "24h", "availability": "99.9%"},
    "builds": [{"id": "staging", "engine": "dbt"}],
    "links": {"documentation": "https://docs.example.com"},
}


class TestDataProductSpecConformance:
    """Verify _to_data_product() output matches DPS 0.0.1 schema."""

    def _make_provider(self):
        return DataMeshManagerProvider(api_key="fake", api_url="https://test.com")

    def test_root_id_present(self):
        dp = self._make_provider()._to_data_product(RICH_CONTRACT)
        assert "id" in dp, "Root-level 'id' is required by DPS schema"
        assert dp["id"] == "bronze.bss.accounts"

    def test_data_product_specification_version(self):
        dp = self._make_provider()._to_data_product(RICH_CONTRACT)
        assert dp["dataProductSpecification"] == "0.0.1"

    def test_info_title_not_name(self):
        dp = self._make_provider()._to_data_product(RICH_CONTRACT)
        info = dp["info"]
        assert "title" in info, "DPS requires info.title, not info.name"
        assert info["title"] == "BSS Customer Accounts"
        assert "name" not in info, "info.name is not in the DPS schema"

    def test_info_owner_is_team_id(self):
        dp = self._make_provider()._to_data_product(RICH_CONTRACT)
        assert dp["info"]["owner"] == "analytics-coe"

    def test_info_status(self):
        dp = self._make_provider()._to_data_product(RICH_CONTRACT)
        assert dp["info"]["status"] == "active"

    def test_info_archetype(self):
        dp = self._make_provider()._to_data_product(RICH_CONTRACT)
        assert dp["info"]["archetype"] == "source-aligned"

    def test_info_maturity(self):
        dp = self._make_provider()._to_data_product(RICH_CONTRACT)
        assert dp["info"]["maturity"] == "managed"

    def test_tags_merged(self):
        dp = self._make_provider()._to_data_product(RICH_CONTRACT)
        assert "bronze-layer" in dp["tags"]
        assert "bss" in dp["tags"]

    def test_output_port_server_object(self):
        dp = self._make_provider()._to_data_product(RICH_CONTRACT)
        port = dp["outputPorts"][0]
        assert "server" in port, "DPS expects server object, not location string"
        assert "location" not in port, "Flat location string should not be present"
        server = port["server"]
        assert server["account"] == "ducci-sandbox"
        assert server["database"] == "bronze_bss"
        assert server["table"] == "customer_accounts_raw"

    def test_output_port_contains_pii(self):
        dp = self._make_provider()._to_data_product(RICH_CONTRACT)
        port = dp["outputPorts"][0]
        assert port["containsPii"] is True  # full_name has classification: pii

    def test_output_port_type(self):
        dp = self._make_provider()._to_data_product(RICH_CONTRACT)
        port = dp["outputPorts"][0]
        assert port["type"] == "BigQuery"

    def test_input_port_location_string(self):
        dp = self._make_provider()._to_data_product(RICH_CONTRACT)
        port = dp["inputPorts"][0]
        # Input ports still use location string per DPS schema
        assert port["sourceSystemId"] == "bss-crm"

    def test_archetype_inferred_from_layer(self):
        """When metadata.archetype is absent, infer from layer."""
        contract = {
            "kind": "DataProduct",
            "id": "test-infer",
            "name": "Test",
            "metadata": {"layer": "Gold", "owner": {"team": "t"}},
            "exposes": [],
        }
        dp = self._make_provider()._to_data_product(contract)
        assert dp["info"].get("archetype") == "aggregate"


# ---------------------------------------------------------------------------
# 11. Server object builder
# ---------------------------------------------------------------------------


class TestBuildServerObject:
    def _build(self, section, provider=""):
        return DataMeshManagerProvider._build_server_object(section, provider)

    def test_bigquery_binding(self):
        section = {
            "binding": {
                "platform": "gcp",
                "location": {"project": "p1", "dataset": "ds1", "table": "t1"},
            }
        }
        server = self._build(section, "gcp")
        assert server == {"account": "p1", "database": "ds1", "table": "t1"}

    def test_snowflake_binding(self):
        section = {
            "binding": {
                "platform": "snowflake",
                "location": {"account": "acc", "database": "db", "schema": "sch", "table": "tbl"},
            }
        }
        server = self._build(section, "snowflake")
        assert server == {"account": "acc", "database": "db", "schema": "sch", "table": "tbl"}

    def test_s3_binding(self):
        section = {
            "binding": {
                "platform": "s3",
                "location": {"bucket": "my-bucket", "path": "data/"},
                "format": "parquet",
            }
        }
        server = self._build(section, "s3")
        assert server["location"] == "s3://my-bucket/data"
        assert server["format"] == "parquet"

    def test_kafka_binding(self):
        section = {
            "binding": {
                "platform": "kafka",
                "location": {"topic": "events.user.click"},
            }
        }
        server = self._build(section, "kafka")
        assert server == {"topic": "events.user.click"}

    def test_legacy_gcp_flat(self):
        section = {"gcp": {"project": "p", "dataset": "d", "table": "t"}}
        server = self._build(section, "gcp")
        assert server == {"account": "p", "database": "d", "table": "t"}

    def test_empty_binding(self):
        server = self._build({}, "")
        assert server == {}

    def test_fallback_location_string(self):
        section = {"location": "some://custom/uri"}
        server = self._build(section, "custom")
        assert server == {"location": "some://custom/uri"}


# ---------------------------------------------------------------------------
# 12. ODCS v3.1.0 data contract builder
# ---------------------------------------------------------------------------


class TestBuildDataContractODCS:
    def _make_provider(self):
        return DataMeshManagerProvider(api_key="fake", api_url="https://test.com")

    def test_odcs_envelope(self):
        dc = self._make_provider()._build_data_contract_odcs(RICH_CONTRACT, "bronze.bss.accounts")
        assert dc["apiVersion"] == "v3.1.0"
        assert dc["kind"] == "DataContract"
        assert dc["id"] == "bronze.bss.accounts-contract"

    def test_odcs_name(self):
        dc = self._make_provider()._build_data_contract_odcs(RICH_CONTRACT, "bronze.bss.accounts")
        assert dc["name"] == "BSS Customer Accounts"

    def test_odcs_team(self):
        dc = self._make_provider()._build_data_contract_odcs(RICH_CONTRACT, "bronze.bss.accounts")
        assert dc["team"] == {"name": "analytics-coe"}

    def test_odcs_domain(self):
        dc = self._make_provider()._build_data_contract_odcs(RICH_CONTRACT, "bronze.bss.accounts")
        assert dc["domain"] == "bss"

    def test_odcs_description_is_object(self):
        dc = self._make_provider()._build_data_contract_odcs(RICH_CONTRACT, "bronze.bss.accounts")
        assert isinstance(dc["description"], dict)
        assert "purpose" in dc["description"]

    def test_odcs_schema_array(self):
        dc = self._make_provider()._build_data_contract_odcs(RICH_CONTRACT, "bronze.bss.accounts")
        assert isinstance(dc["schema"], list)
        assert len(dc["schema"]) == 1
        model = dc["schema"][0]
        assert model["name"] == "customer_accounts"
        assert model["physicalType"] == "table"

    def test_odcs_schema_properties(self):
        dc = self._make_provider()._build_data_contract_odcs(RICH_CONTRACT, "bronze.bss.accounts")
        props = dc["schema"][0]["properties"]
        assert len(props) == 3
        id_prop = next(p for p in props if p["name"] == "customer_id")
        assert id_prop["logicalType"] == "string"
        assert id_prop["required"] is True
        assert id_prop["primaryKey"] is True

    def test_odcs_logical_type_mapping(self):
        mapping = DataMeshManagerProvider._odcs_logical_type
        assert mapping("VARCHAR") == "string"
        assert mapping("int64") == "integer"
        assert mapping("FLOAT") == "number"
        assert mapping("boolean") == "boolean"
        assert mapping("timestamp_ntz") == "timestamp"
        assert mapping("DATE") == "date"
        assert mapping("unknown_type") == "string"

    def test_odcs_custom_properties(self):
        dc = self._make_provider()._build_data_contract_odcs(RICH_CONTRACT, "bronze.bss.accounts")
        assert "customProperties" in dc
        prop_names = [p["property"] for p in dc["customProperties"]]
        assert "layer" in prop_names
        assert "domain" in prop_names

    def test_odcs_data_product_link(self):
        dc = self._make_provider()._build_data_contract_odcs(RICH_CONTRACT, "bronze.bss.accounts")
        assert dc["dataProduct"] == "bronze.bss.accounts"

    def test_odcs_sla(self):
        dc = self._make_provider()._build_data_contract_odcs(RICH_CONTRACT, "bronze.bss.accounts")
        assert "serviceLevelObjectives" in dc
        assert dc["serviceLevelObjectives"]["freshness"] == "24h"

    def test_odcs_tags(self):
        dc = self._make_provider()._build_data_contract_odcs(RICH_CONTRACT, "bronze.bss.accounts")
        assert "bronze-layer" in dc["tags"]

    def test_odcs_tags_fallback_to_metadata_tags(self):
        contract = {
            "id": "sales.product",
            "metadata": {
                "name": "Sales Product",
                "owner": {"team": "analytics"},
                "tags": ["sales", "curated"],
            },
            "owner": {"team": "analytics"},
            "exposes": [],
        }

        dc = self._make_provider()._build_data_contract_odcs(contract, "sales.product")
        assert dc["tags"] == ["sales", "curated"]

    def test_odcs_schema_uses_legacy_expose_type_when_kind_missing(self):
        contract = {
            "id": "legacy.product",
            "metadata": {"name": "Legacy Product", "owner": {"team": "analytics"}},
            "owner": {"team": "analytics"},
            "exposes": [
                {
                    "id": "legacy_port",
                    "type": "view",
                    "schema": [{"name": "id", "type": "string"}],
                }
            ],
        }

        dc = self._make_provider()._build_data_contract_odcs(contract, "legacy.product")
        assert dc["schema"][0]["physicalType"] == "view"


# ---------------------------------------------------------------------------
# 13. DCS 0.9.3 data contract builder (deprecated path)
# ---------------------------------------------------------------------------


class TestBuildDataContractDCS:
    def _make_provider(self):
        return DataMeshManagerProvider(api_key="fake", api_url="https://test.com")

    def test_dcs_envelope(self):
        dc = self._make_provider()._build_data_contract_dcs(RICH_CONTRACT, "bronze.bss.accounts")
        assert dc["dataContractSpecification"] == "0.9.3"
        assert dc["id"] == "bronze.bss.accounts-contract"

    def test_dcs_info_title(self):
        dc = self._make_provider()._build_data_contract_dcs(RICH_CONTRACT, "bronze.bss.accounts")
        assert dc["info"]["title"] == "BSS Customer Accounts"
        assert dc["info"]["owner"] == "analytics-coe"

    def test_dcs_models(self):
        dc = self._make_provider()._build_data_contract_dcs(RICH_CONTRACT, "bronze.bss.accounts")
        assert "customer_accounts" in dc["models"]
        model = dc["models"]["customer_accounts"]
        assert "customer_id" in model["fields"]

    def test_dcs_model_type_uses_legacy_expose_type_when_kind_missing(self):
        contract = {
            "id": "legacy.product",
            "metadata": {"name": "Legacy Product", "owner": {"team": "analytics"}},
            "owner": {"team": "analytics"},
            "exposes": [
                {
                    "id": "legacy_port",
                    "type": "view",
                    "schema": [{"name": "id", "type": "string"}],
                }
            ],
        }

        dc = self._make_provider()._build_data_contract_dcs(contract, "legacy.product")
        assert dc["models"]["legacy_port"]["type"] == "view"

    def test_dcs_servers(self):
        dc = self._make_provider()._build_data_contract_dcs(RICH_CONTRACT, "bronze.bss.accounts")
        assert "customer_accounts" in dc.get("servers", {})


# ---------------------------------------------------------------------------
# 14. Contract format dispatch
# ---------------------------------------------------------------------------


class TestContractFormatDispatch:
    def _make_provider(self):
        return DataMeshManagerProvider(api_key="fake", api_url="https://test.com")

    def test_odcs_is_default(self):
        """ODCS should be the default format."""
        assert DataMeshManagerProvider.CONTRACT_FORMAT_ODCS == "odcs"

    def test_dcs_constant(self):
        assert DataMeshManagerProvider.CONTRACT_FORMAT_DCS == "dcs"

    @patch.object(DataMeshManagerProvider, "_request")
    def test_publish_internal_odcs(self, mock_request):
        mock_request.return_value = MagicMock(status_code=200)
        provider = self._make_provider()
        result = provider._publish_data_contract_internal(
            RICH_CONTRACT, "bronze.bss.accounts", fmt="odcs"
        )
        assert result["format"] == "odcs"
        # Verify the PUT body has ODCS structure
        call_body = mock_request.call_args[1]["json_body"]
        assert call_body["apiVersion"] == "v3.1.0"
        assert call_body["kind"] == "DataContract"

    @patch.object(DataMeshManagerProvider, "_request")
    def test_publish_internal_dcs(self, mock_request):
        mock_request.return_value = MagicMock(status_code=200)
        provider = self._make_provider()
        result = provider._publish_data_contract_internal(
            RICH_CONTRACT, "bronze.bss.accounts", fmt="dcs"
        )
        assert result["format"] == "dcs"
        call_body = mock_request.call_args[1]["json_body"]
        assert call_body["dataContractSpecification"] == "0.9.3"


# ---------------------------------------------------------------------------
# 15. CLI --contract-format flag
# ---------------------------------------------------------------------------


class TestContractFormatCLI:
    @pytest.fixture()
    def dmm_parser(self):
        root = argparse.ArgumentParser()
        sp = root.add_subparsers(dest="cmd")
        dmm_mod.add_parser(sp)
        return root

    def test_contract_format_default_odcs(self, dmm_parser):
        args = dmm_parser.parse_args(["dmm", "publish", "c.yaml"])
        assert args.contract_format == "odcs"

    def test_contract_format_dcs(self, dmm_parser):
        args = dmm_parser.parse_args(["dmm", "publish", "c.yaml", "--contract-format", "dcs"])
        assert args.contract_format == "dcs"

    def test_contract_format_odcs_explicit(self, dmm_parser):
        args = dmm_parser.parse_args(["dmm", "publish", "c.yaml", "--contract-format", "odcs"])
        assert args.contract_format == "odcs"

    @patch.object(dmm_mod, "load_contract_with_overlay")
    @patch.object(dmm_mod, "DataMeshManagerProvider")
    def test_contract_format_passed_to_apply(self, MockProvider, mock_loader, contract_file):
        mock_loader.return_value = MINIMAL_CONTRACT
        provider_inst = MagicMock()
        provider_inst.apply.return_value = {"status": "ok"}
        MockProvider.return_value = provider_inst

        args = SimpleNamespace(
            contract=contract_file,
            overlay=None,
            dry_run=False,
            with_contract=True,
            no_create_team=False,
            team_id=None,
            api_key="key",
            api_url=None,
            contract_format="dcs",
        )
        dmm_mod._cmd_publish(args, logging.getLogger("test"))

        call_kwargs = provider_inst.apply.call_args[1]
        assert call_kwargs["contract_format"] == "dcs"


# ---------------------------------------------------------------------------
# 16. dataContractId wiring on output ports
# ---------------------------------------------------------------------------


class TestDataContractIdWiring:
    def _make_provider(self):
        return DataMeshManagerProvider(api_key="fake", api_url="https://test.com")

    @patch.object(DataMeshManagerProvider, "_request")
    @patch.object(DataMeshManagerProvider, "_ensure_team")
    def test_output_port_gets_data_contract_id(self, _mock_team, mock_request):
        mock_request.return_value = MagicMock(status_code=200)
        provider = self._make_provider()
        result = provider._publish_one(
            RICH_CONTRACT,
            publish_contract=True,
            contract_format="odcs",
        )
        # The data product PUT should have dataContractId on output ports
        # dataContractId format is {product_id}.{expose_id}
        dp_call = mock_request.call_args_list[0]
        dp_body = dp_call[1]["json_body"]
        product_id = RICH_CONTRACT["id"]
        for port in dp_body.get("outputPorts", []):
            expose_id = port.get("id") or port.get("name")
            assert port["dataContractId"] == f"{product_id}.{expose_id}"

    def test_contract_id_always_present_on_output_ports(self):
        """After rebase, _to_data_product always wires dataContractId on
        output ports (per-expose ODCS linking)."""
        provider = self._make_provider()
        dp = provider._to_data_product(RICH_CONTRACT)
        for port in dp.get("outputPorts", []):
            assert "dataContractId" in port
