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

"""Tests for ``fluid publish --target`` (11-stage pipeline Phase 5).

Adversarial bias: every test pins a behavior the pipeline design
depends on. ``--target`` is the canonical shape going forward;
``--catalog`` is a deprecation-warned alias for one release.
"""

from __future__ import annotations

import argparse
import logging
from unittest.mock import patch

import pytest

from fluid_build.cli import publish

# ---------------------------------------------------------------------------
# Flag parsing
# ---------------------------------------------------------------------------


class TestTargetFlagParsing:
    def _parser(self):
        p = argparse.ArgumentParser()
        sub = p.add_subparsers()
        publish.register(sub)
        return p

    def test_target_is_repeatable(self):
        args = self._parser().parse_args(
            ["publish", "x.yaml", "--target", "command-center", "--target", "datahub"]
        )
        assert args.target == ["command-center", "datahub"]

    def test_target_accepts_endpoint_override_suffix(self):
        """--target name:endpoint is the canonical override shape from
        perfect-pipeline.html. Parser accepts raw, parses in run_async."""
        args = self._parser().parse_args(
            [
                "publish",
                "x.yaml",
                "--target",
                "command-center:https://cc.company.com",
            ]
        )
        assert args.target == ["command-center:https://cc.company.com"]

    def test_target_default_is_none(self):
        """Default is None (not empty list) so run_async can distinguish
        'user passed nothing' from 'user passed an empty list'. Falls back
        to fluid-command-center when neither --target nor --catalog is set."""
        args = self._parser().parse_args(["publish", "x.yaml"])
        assert args.target is None
        assert args.catalog is None

    def test_short_form_t(self):
        args = self._parser().parse_args(["publish", "x.yaml", "-t", "datahub"])
        assert args.target == ["datahub"]

    def test_catalog_short_form_c_still_works(self):
        """``-c`` alias for --catalog preserved for back-compat during the
        deprecation period."""
        args = self._parser().parse_args(["publish", "x.yaml", "-c", "legacy"])
        assert args.catalog == "legacy"

    def test_both_target_and_catalog_can_coexist_on_cli(self):
        """Parser allows both; the deprecation warning + merge logic lives
        in run_async, not in argparse."""
        args = self._parser().parse_args(
            ["publish", "x.yaml", "--target", "new", "--catalog", "old"]
        )
        assert args.target == ["new"]
        assert args.catalog == "old"


# ---------------------------------------------------------------------------
# Target resolution in run_async (--target + --catalog → normalized list)
# ---------------------------------------------------------------------------


class TestTargetResolution:
    """The new flag merges with the deprecated --catalog and with the
    default fallback. Test the normalization logic by stubbing the async
    work and inspecting how publish_contract was called."""

    def _make_args(self, **overrides):
        defaults = {
            "contract_files": ["tests/fixtures/contracts/minimal.yaml"],
            "target": None,
            "catalog": None,
            "list_catalogs": False,
            "dry_run": True,
            "verify_only": False,
            "force": False,
            "format": "json",
            "verbose": False,
            "quiet": True,
            "skip_health_check": True,
            "show_metrics": False,
            "env": None,
        }
        defaults.update(overrides)
        return argparse.Namespace(**defaults)

    @pytest.fixture(autouse=True)
    def _stub_dependencies(self, monkeypatch):
        """Stub everything that requires real network / config / file IO so
        we can assert on publish_contract's call args without side effects."""

        class _FakePath:
            def __init__(self, path):
                self._p = path

            def exists(self):
                return True

        monkeypatch.setattr(publish, "hydrate_dotenv", lambda *a, **k: None)

        class _FakeConfig:
            def get_catalog_config(self, name=None):
                if name is None:
                    return {}
                return {"endpoint": f"https://default/{name}", "enabled": True}

        monkeypatch.setattr(publish, "FluidConfig", lambda *a, **k: _FakeConfig())
        monkeypatch.setattr(publish, "Console", lambda *a, **k: None)
        monkeypatch.setattr(publish, "RICH_AVAILABLE", False)
        # Make Path existence checks trivially true
        monkeypatch.setattr("pathlib.Path.exists", lambda self: True)

    def _run_and_capture(self, args, recorded):
        """Patch publish.publish_contract to record its kwargs, then run."""
        import asyncio

        from fluid_build.providers.catalogs import PublishResult

        async def _fake_publish(**kwargs):
            recorded.append(kwargs)
            return PublishResult(
                success=True,
                catalog_id=kwargs["catalog_name"],
                asset_id="test-asset",
            )

        with patch.object(publish, "publish_contract", _fake_publish):
            logger = logging.getLogger("test.publish_target")
            return asyncio.run(publish.run_async(args, logger))

    def test_single_target_flag(self):
        recorded = []
        args = self._make_args(target=["command-center"])
        rc = self._run_and_capture(args, recorded)
        assert rc == 0
        assert len(recorded) == 1
        assert recorded[0]["catalog_name"] == "command-center"
        assert recorded[0].get("endpoint_override") is None

    def test_multiple_target_flag_publishes_to_each(self):
        """The core new capability: ``--target a --target b`` publishes to
        BOTH, not just the last one."""
        recorded = []
        args = self._make_args(target=["command-center", "datahub"])
        rc = self._run_and_capture(args, recorded)
        assert rc == 0
        assert [r["catalog_name"] for r in recorded] == ["command-center", "datahub"]

    def test_target_with_endpoint_override_passed_through(self):
        """``--target name:endpoint`` parses + forwards the endpoint to
        publish_contract's new endpoint_override kwarg."""
        recorded = []
        args = self._make_args(target=["command-center:https://cc.company.com", "datahub"])
        rc = self._run_and_capture(args, recorded)
        assert rc == 0
        assert recorded[0]["catalog_name"] == "command-center"
        assert recorded[0]["endpoint_override"] == "https://cc.company.com"
        # Second target has no override
        assert recorded[1]["catalog_name"] == "datahub"
        assert recorded[1].get("endpoint_override") is None

    def test_catalog_deprecated_still_works(self):
        """Legacy ``--catalog X`` continues to publish (preserves existing
        CI scripts during the deprecation window) but logs a warning.

        Uses a logger-spy fixture rather than caplog — caplog's capture
        relies on root-logger propagation which other tests occasionally
        leave in a reconfigured state under random test ordering."""
        import asyncio

        from fluid_build.providers.catalogs import PublishResult

        recorded = []
        warnings = []

        async def _fake_publish(**kwargs):
            recorded.append(kwargs)
            return PublishResult(success=True, catalog_id=kwargs["catalog_name"], asset_id="x")

        # Spy on the logger passed INTO run_async — publish.py emits the
        # deprecation warning via that logger, not the module logger.
        spy_logger = logging.getLogger("test.publish_target_deprecated")

        original_warning = spy_logger.warning

        def _capture_warning(msg, *a, **kw):
            warnings.append(str(msg) % a if a else str(msg))
            return original_warning(msg, *a, **kw)

        spy_logger.warning = _capture_warning  # type: ignore[method-assign]

        args = self._make_args(catalog="legacy-catalog")
        try:
            with patch.object(publish, "publish_contract", _fake_publish):
                rc = asyncio.run(publish.run_async(args, spy_logger))
        finally:
            spy_logger.warning = original_warning  # type: ignore[method-assign]

        assert rc == 0
        assert len(recorded) == 1
        assert recorded[0]["catalog_name"] == "legacy-catalog"
        # Deprecation warning surfaced — captured via direct logger spy,
        # order-independent.
        assert any(
            "deprecated" in w.lower() for w in warnings
        ), f"--catalog should log a deprecation warning; saw: {warnings}"

    def test_target_and_catalog_both_publish(self):
        """When BOTH flags are set, both participate (both get published).
        Intentional: existing CI may set --catalog via env default; users
        adding --target should see it work additively during migration."""
        recorded = []
        args = self._make_args(target=["new-target"], catalog="old-catalog")
        rc = self._run_and_capture(args, recorded)
        assert rc == 0
        assert len(recorded) == 2
        names = {r["catalog_name"] for r in recorded}
        assert names == {"new-target", "old-catalog"}

    def test_no_flags_falls_back_to_default(self):
        recorded = []
        args = self._make_args()  # no target, no catalog
        rc = self._run_and_capture(args, recorded)
        assert rc == 0
        # Falls back to fluid-command-center — preserves pre-pipeline
        # default behavior so existing ``fluid publish X.yaml`` keeps working.
        assert recorded[0]["catalog_name"] == "fluid-command-center"


# ---------------------------------------------------------------------------
# Pipeline-templates wiring
# ---------------------------------------------------------------------------


class TestPipelineTemplateCommands:
    """``_get_fluid_commands`` now exposes stage-5 (diff), stage-9 (verify),
    and a ``--target``-aware stage-10 (publish). These are referenced by
    every CI template; tests pin the shape so Phase 7's 6-CI-system port
    can't silently drop them."""

    def _cmds(self):
        from fluid_build.forge.core.pipeline_templates import BasePipelineTemplate

        return BasePipelineTemplate()._get_fluid_commands()

    def test_diff_command_present_with_exit_on_drift(self):
        cmds = self._cmds()
        assert "diff" in cmds
        assert "--exit-on-drift" in cmds["diff"]
        # Must reference the ${CONTRACT} default so Build Now works.
        assert "${CONTRACT:-contract.fluid.yaml}" in cmds["diff"]
        # Must pass --env so dev/staging/prod have different drift baselines.
        assert "--env" in cmds["diff"]

    def test_verify_command_present_with_strict(self):
        cmds = self._cmds()
        assert "verify" in cmds
        assert "--strict" in cmds["verify"]
        assert "${CONTRACT:-contract.fluid.yaml}" in cmds["verify"]
        # Writes a JSON report — CI artifact uploads need a deterministic path.
        assert "--report" in cmds["verify"]

    def test_publish_catalog_uses_target_flag(self):
        cmds = self._cmds()
        # --target is the canonical flag post-Phase 5. Must appear.
        assert "--target" in cmds["publish_catalog"]

    def test_publish_catalog_supports_multiple_targets_env(self):
        """PUBLISH_TARGETS is the space-separated multi-target env; the
        template expands it into ``--target X --target Y``. Without this,
        teams publishing to 3 catalogs would need 3 separate CI jobs."""
        cmd = self._cmds()["publish_catalog"]
        assert "PUBLISH_TARGETS" in cmd

    def test_publish_catalog_falls_back_to_single_catalog_env(self):
        """When PUBLISH_TARGETS is unset, fall back to the legacy single
        CATALOG env so existing CI installations keep working."""
        cmd = self._cmds()["publish_catalog"]
        assert "${CATALOG:-datamesh-manager}" in cmd

    def test_publish_catalog_no_longer_uses_deprecated_flag(self):
        """After Phase 5, the template must not emit ``fluid publish
        ... --catalog`` — that flag is a deprecation-warned alias and
        generated pipelines shouldn't perpetuate the warning."""
        cmd = self._cmds()["publish_catalog"]
        # ``--catalog`` as a CLI flag must NOT appear; bare word "catalog"
        # in a comment is fine but we guard the flag form specifically.
        assert "--catalog " not in cmd and "--catalog $" not in cmd


# ---------------------------------------------------------------------------
# DMM integration — outputPort.contractId must link to the ODCS contract URL
# ---------------------------------------------------------------------------


class TestOdcsOutputPortLinkage:
    """When publishing to Data Mesh Manager with publish_contract=True, every
    outputPort.contractId MUST match the URL of the ODCS contract that DMM
    will PUT alongside. Without this, DMM's data product page shows the
    port but the ODCS link 404s — the catalog UI becomes inconsistent.

    Phase 4 stripped ``outputPorts[].id`` (schema conformance). DMM's
    overlay at datamesh_manager.py:408 falls back to ``name or id`` — this
    test pins that the linkage survives even with the schema-conformant
    name-only shape.
    """

    def _load_lineage_contract(self):
        """Resolve the fixture path from __file__ rather than cwd. Test
        isolation: random-order runs can land this test when a prior test
        left cwd pointing at a tmp_path; absolute resolution avoids that."""
        from pathlib import Path

        import yaml

        repo_root = Path(__file__).parent.parent.parent
        fixture = repo_root / "tests/fixtures/contracts/compatibility/lineage_072.yaml"
        with open(fixture, "r") as fh:
            return yaml.safe_load(fh)

    def test_every_output_port_has_matching_odcs_put_url(self):
        """The critical invariant. Without this, the DMM UI shows a data
        product with output ports pointing at contract IDs that don't
        resolve."""
        from fluid_build.providers.datamesh_manager.datamesh_manager import (
            DataMeshManagerProvider,
        )

        contract = self._load_lineage_contract()
        result = DataMeshManagerProvider(api_key="dummy", api_url="https://x").apply(
            contract, dry_run=True, provider_hint="odps", publish_contract=True
        )
        payload = result["payload"]

        # Collect port contract IDs
        port_contract_ids = {
            p["contractId"] for p in payload.get("outputPorts", []) if "contractId" in p
        }
        assert port_contract_ids, (
            "outputPorts must carry contractId when publish_contract=True; "
            "without this, DMM's data product page has no link to the ODCS contract"
        )

        # Collect ODCS PUT IDs (extract from the URL tail)
        odcs_put_ids = set()
        for preview in result.get("odcs_contracts", []):
            url = preview.get("url", "")
            if url:
                odcs_put_ids.add(url.rsplit("/", 1)[-1])

        # Every port's contractId must appear in the ODCS PUT set.
        # This is the core linkage — broken when either half drifts.
        unlinked = port_contract_ids - odcs_put_ids
        assert not unlinked, (
            f"outputPort contractIds without matching ODCS PUT URLs: {unlinked}. "
            f"This means DMM's data product page would link to contract IDs "
            f"that 404. ODCS PUTs: {odcs_put_ids}"
        )

    def test_contract_id_uses_expose_name_not_id(self):
        """Post-Phase-4, outputPorts have ``name`` but no ``id``. DMM's
        overlay must read from ``name`` (or it'd silently emit an empty
        contractId and every ODCS link in the UI would 404). The fallback
        ``port.get("name") or port.get("id")`` in DMM overlay covers the
        schema-conformant shape."""
        from fluid_build.providers.datamesh_manager.datamesh_manager import (
            DataMeshManagerProvider,
        )

        contract = self._load_lineage_contract()
        result = DataMeshManagerProvider(api_key="dummy", api_url="https://x").apply(
            contract, dry_run=True, provider_hint="odps", publish_contract=True
        )

        for port in result["payload"].get("outputPorts", []):
            assert port.get("contractId"), (
                f"outputPort has no contractId — DMM overlay failed to resolve "
                f"from port.name. Port: {port}"
            )
            assert port["name"] in port["contractId"], (
                f"contractId should contain the port name; "
                f"got name={port['name']!r}, contractId={port['contractId']!r}"
            )
