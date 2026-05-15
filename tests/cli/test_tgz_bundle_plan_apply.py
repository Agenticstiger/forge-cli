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

"""Integration tests: ``.tgz`` bundle input to ``fluid plan`` / ``fluid apply``.

Regression coverage for a bug where ``fluid plan <bundle>.tgz`` and
``fluid apply <bundle>.tgz`` crashed with::

    'utf-8' codec can't decode byte 0x8b in position 1: invalid start byte

``load_contract_with_overlay`` handed the gzip-binary ``.tgz`` straight to
the text loader, which tried to UTF-8-decode it. ``fluid validate`` had
bundle-aware unpacking; ``plan`` / ``apply`` did not. The consequence was
that ``plan.py``'s ``.tgz`` support — ``is_bundle_path`` +
``inject_digests(bundle_path=...)`` — was unreachable: a plan could never
get ``bindingMode="bound"`` with a populated ``bundleDigest`` via the real
CLI.

What the tests pin:

  1. ``load_contract_with_overlay`` accepts a ``.tgz`` and returns the
     resolved contract dict (the bug's exact center).
  2. ``plan.run`` on a ``.tgz`` succeeds and emits a plan with
     ``bindingMode="bound"`` + a non-empty ``bundleDigest`` that matches
     the bundle's MANIFEST merkle root.
  3. ``apply.run`` on that plan ``.json`` + ``--bundle <tgz>`` passes the
     stage-7 plan-binding gate (no ``apply_plan_digest_*`` failure).
  4. ``apply.run`` directly on a ``.tgz`` loads the contract and dispatches.
  5. ``$source`` sentinels (inline SQL extracted into ``sources/``) are
     unwrapped — the planner never sees a bare ``{"$source": ...}`` dict.
  6. A tampered bundle is rejected before any contract bytes are parsed.
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Any, Dict

import pytest

from fluid_build.cli._common import CLIError, load_contract_with_overlay
from fluid_build.forge.core.bundle import build_bundle_tgz

LOGGER = logging.getLogger("fluid_build.cli.test.tgz")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _resolved_contract() -> Dict[str, Any]:
    """A resolved v0.7.x contract — local provider, no inline SQL fragments.

    Mirrors ``examples/02-csv-to-data-product/contract.fluid.yaml`` (which
    keeps its SQL under ``builds[].properties.sql``, so nothing gets
    extracted into ``sources/``).
    """
    return {
        "fluidVersion": "0.7.2",
        "kind": "DataProduct",
        "id": "example.customer_clean_v1",
        "name": "Clean Customer Data",
        "domain": "customer",
        "metadata": {
            "layer": "Bronze",
            "owner": {"team": "data-team", "email": "data@example.com"},
        },
        "builds": [
            {
                "id": "clean_customers",
                "pattern": "embedded-logic",
                "engine": "sql",
                "properties": {"sql": "SELECT 1\n"},
            }
        ],
        "exposes": [
            {
                "exposeId": "clean_customer_data",
                "kind": "table",
                "binding": {
                    "platform": "local",
                    "format": "csv",
                    "location": {"path": "runtime/out/customer-clean-v1.csv"},
                },
                "contract": {"schema": [{"name": "customer_id", "type": "integer"}]},
            }
        ],
    }


def _contract_with_inline_sql() -> Dict[str, Any]:
    """Resolved contract carrying inline SQL under ``embeddedLogicPattern.sql``.

    ``fluid bundle`` extracts that SQL into ``sources/sql/…`` and replaces it
    with a ``{"$source": "sources/…"}`` sentinel. The loader must unwrap the
    sentinel back into the SQL string.
    """
    contract = _resolved_contract()
    contract["builds"] = [
        {
            "id": "orders_clean",
            "pattern": "embedded-logic",
            "engine": "sql",
            "embeddedLogicPattern": {"sql": "SELECT id, name FROM orders_raw\n"},
        }
    ]
    return contract


@pytest.fixture
def bundle_tgz(tmp_path: Path) -> Path:
    """A real, valid ``.tgz`` bundle built via the stage-1 bundle builder."""
    out = tmp_path / "b.tgz"
    build_bundle_tgz(_resolved_contract(), out, contract_id="example.customer_clean_v1")
    return out


@pytest.fixture
def bundle_tgz_with_sql(tmp_path: Path) -> Path:
    """A valid ``.tgz`` bundle whose contract had inline SQL extracted."""
    out = tmp_path / "with_sql.tgz"
    build_bundle_tgz(_contract_with_inline_sql(), out, contract_id="orders")
    return out


def _plan_args(contract: str, out: str, **overrides: Any) -> argparse.Namespace:
    """argparse Namespace mirroring ``plan.register``'s surface."""
    ns = argparse.Namespace(
        contract=contract,
        env=None,
        mode=None,
        out=out,
        verbose=False,
        validate_actions=False,
        estimate_cost=False,
        check_sovereignty=False,
        provider="local",
        project=None,
        region=None,
        html_output=None,
    )
    for k, v in overrides.items():
        setattr(ns, k, v)
    return ns


def _apply_args(contract: str, **overrides: Any) -> argparse.Namespace:
    """argparse Namespace mirroring ``apply.register``'s surface."""
    ns = argparse.Namespace(
        contract=contract,
        env="dev",
        mode=None,
        allow_data_loss=False,
        bundle=None,
        no_verify_plan_binding=False,
        no_verify_federation=False,
        build_id=None,
        dry_run=True,
        yes=True,
        verbose=False,
        debug=False,
        workspace_dir=Path("."),
        state_file=None,
        config_override=None,
        report=None,
        report_format="html",
        metrics_export="none",
        notify=None,
        rollback_strategy="none",
        require_approval=False,
        backup_state=False,
        validate_dependencies=False,
        timeout=120,
        parallel_phases=False,
        max_workers=4,
        keep_temp_files=False,
        profile=False,
        delay=2,
        fail_fast=False,
        no_output=False,
        provider="local",
        project=None,
        region=None,
        provider_config=None,
        force_pattern_drift=False,
    )
    for k, v in overrides.items():
        setattr(ns, k, v)
    return ns


# ---------------------------------------------------------------------------
# load_contract_with_overlay — the bug's exact center
# ---------------------------------------------------------------------------


class TestLoadContractFromBundle:
    """``load_contract_with_overlay`` must accept a ``.tgz`` without crashing
    on the gzip magic byte (0x8b)."""

    def test_tgz_does_not_crash_on_utf8_decode(self, bundle_tgz: Path) -> None:
        """The regression: a ``.tgz`` is gzip binary; the old code tried to
        ``read_text(encoding="utf-8")`` it and raised the 0x8b decode error.
        """
        contract = load_contract_with_overlay(str(bundle_tgz), None, LOGGER)
        assert isinstance(contract, dict)
        assert contract["id"] == "example.customer_clean_v1"
        assert contract["fluidVersion"] == "0.7.2"

    def test_tgz_returns_fully_resolved_contract(self, bundle_tgz: Path) -> None:
        """The contract extracted from the bundle is structurally complete —
        builds + exposes survive the round trip."""
        contract = load_contract_with_overlay(str(bundle_tgz), None, LOGGER)
        assert contract["builds"][0]["id"] == "clean_customers"
        assert contract["exposes"][0]["binding"]["platform"] == "local"

    def test_tar_gz_extension_also_accepted(self, tmp_path: Path) -> None:
        """The ``.tar.gz`` spelling routes through the same bundle path."""
        out = tmp_path / "b.tar.gz"
        build_bundle_tgz(_resolved_contract(), out, contract_id="x")
        contract = load_contract_with_overlay(str(out), None, LOGGER)
        assert contract["id"] == "example.customer_clean_v1"

    def test_source_sentinels_are_unwrapped(self, bundle_tgz_with_sql: Path) -> None:
        """Inline SQL extracted into ``sources/`` becomes a ``$source``
        sentinel in ``contract.resolved.yaml``; the loader must resolve it
        back to the SQL string so downstream planners never see the dict."""
        contract = load_contract_with_overlay(str(bundle_tgz_with_sql), None, LOGGER)
        sql = contract["builds"][0]["embeddedLogicPattern"]["sql"]
        assert isinstance(sql, str), f"expected unwrapped SQL string, got {type(sql)}"
        assert "SELECT id, name FROM orders_raw" in sql

    def test_raw_yaml_contract_still_loads(self, tmp_path: Path) -> None:
        """Non-bundle inputs must be unaffected — the bundle branch is gated
        on the file extension and a raw ``.fluid.yaml`` skips it entirely."""
        import yaml

        raw = tmp_path / "contract.fluid.yaml"
        raw.write_text(yaml.safe_dump(_resolved_contract()))
        contract = load_contract_with_overlay(str(raw), None, LOGGER)
        assert contract["id"] == "example.customer_clean_v1"


class TestLoadContractFromBundleRejectsTampered:
    """A bundle that fails the MANIFEST tamper gate must be rejected before
    any contract bytes are parsed."""

    def test_tampered_member_rejected(self, bundle_tgz: Path, tmp_path: Path) -> None:
        import gzip
        import io
        import tarfile

        forged = tmp_path / "tampered.tgz"
        with tarfile.open(bundle_tgz, "r:gz") as src:
            tar_buf = io.BytesIO()
            with tarfile.open(fileobj=tar_buf, mode="w") as out:
                for member in src.getmembers():
                    data = src.extractfile(member).read() if member.isfile() else b""
                    if member.name == "contract.resolved.json":
                        data = data + b" "  # 1-byte tamper, MANIFEST untouched
                        member.size = len(data)
                    out.addfile(member, io.BytesIO(data) if member.isfile() else None)
        gz_buf = io.BytesIO()
        with gzip.GzipFile(fileobj=gz_buf, mode="wb") as gz:
            gz.write(tar_buf.getvalue())
        forged.write_bytes(gz_buf.getvalue())

        with pytest.raises(CLIError) as exc_info:
            load_contract_with_overlay(str(forged), None, LOGGER)
        assert exc_info.value.event == "bundle_manifest_invalid"

    def test_missing_bundle_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(CLIError) as exc_info:
            load_contract_with_overlay(str(tmp_path / "nope.tgz"), None, LOGGER)
        assert exc_info.value.event == "bundle_not_found"

    def test_non_bundle_tgz_rejected(self, tmp_path: Path) -> None:
        """A ``.tgz`` that is not a fluid bundle (no MANIFEST) is rejected
        as a manifest-invalid input, not a UTF-8 crash."""
        import gzip
        import io
        import tarfile

        junk = tmp_path / "junk.tgz"
        tar_buf = io.BytesIO()
        with tarfile.open(fileobj=tar_buf, mode="w") as tar:
            payload = b"not a bundle"
            info = tarfile.TarInfo(name="random.txt")
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
        gz_buf = io.BytesIO()
        with gzip.GzipFile(fileobj=gz_buf, mode="wb") as gz:
            gz.write(tar_buf.getvalue())
        junk.write_bytes(gz_buf.getvalue())

        with pytest.raises(CLIError) as exc_info:
            load_contract_with_overlay(str(junk), None, LOGGER)
        assert exc_info.value.event == "bundle_manifest_invalid"


# ---------------------------------------------------------------------------
# fluid plan <bundle>.tgz — end-to-end
# ---------------------------------------------------------------------------


class TestPlanOnBundle:
    """``fluid plan <bundle>.tgz`` must succeed and emit a *bound* plan."""

    def test_plan_succeeds_on_tgz(self, bundle_tgz: Path, tmp_path: Path) -> None:
        from fluid_build.cli import plan as plan_mod

        out = tmp_path / "p.json"
        rc = plan_mod.run(_plan_args(str(bundle_tgz), str(out)), LOGGER)
        assert rc == 0
        assert out.exists()

    def test_plan_emits_bound_binding_mode(self, bundle_tgz: Path, tmp_path: Path) -> None:
        """The headline guarantee: planning against a ``.tgz`` makes
        ``inject_digests(bundle_path=...)`` reachable, so the plan is
        ``bindingMode="bound"`` — not the raw fallback."""
        from fluid_build.cli import plan as plan_mod

        out = tmp_path / "p.json"
        plan_mod.run(_plan_args(str(bundle_tgz), str(out)), LOGGER)
        plan = json.loads(out.read_text())
        assert plan["bindingMode"] == "bound"

    def test_plan_emits_non_empty_bundle_digest(self, bundle_tgz: Path, tmp_path: Path) -> None:
        """``bundleDigest`` must be populated and equal the bundle's own
        MANIFEST merkle root (``build_bundle_tgz`` returns that digest)."""
        from fluid_build.cli import plan as plan_mod
        from fluid_build.forge.core.plan_digest import read_bundle_digest

        out = tmp_path / "p.json"
        plan_mod.run(_plan_args(str(bundle_tgz), str(out)), LOGGER)
        plan = json.loads(out.read_text())

        assert plan["bundleDigest"], "bundleDigest must not be empty for a .tgz plan"
        assert plan["bundleDigest"].startswith("sha256:")
        assert plan["bundleDigest"] == read_bundle_digest(bundle_tgz)

    def test_plan_carries_plan_digest(self, bundle_tgz: Path, tmp_path: Path) -> None:
        from fluid_build.cli import plan as plan_mod

        out = tmp_path / "p.json"
        plan_mod.run(_plan_args(str(bundle_tgz), str(out)), LOGGER)
        plan = json.loads(out.read_text())
        assert plan["planDigest"].startswith("sha256:")


# ---------------------------------------------------------------------------
# fluid apply — plan.json + --bundle, and direct .tgz apply
# ---------------------------------------------------------------------------


class TestApplyWithBundle:
    """``fluid apply <plan>.json --bundle <tgz>`` must pass the stage-7
    plan-binding gate, and ``fluid apply <tgz>`` must load + dispatch."""

    def test_apply_plan_with_bundle_passes_binding(self, bundle_tgz: Path, tmp_path: Path) -> None:
        """plan against a ``.tgz`` → apply that plan with ``--bundle``: the
        bundleDigest in plan.json is re-verified against the bundle and the
        apply proceeds (dry-run). Any digest mismatch would raise a
        ``apply_plan_digest_*`` CLIError."""
        from fluid_build.cli import apply as apply_mod
        from fluid_build.cli import plan as plan_mod

        plan_out = tmp_path / "p.json"
        plan_mod.run(_plan_args(str(bundle_tgz), str(plan_out)), LOGGER)

        rc = apply_mod.run(
            _apply_args(str(plan_out), bundle=str(bundle_tgz)),
            LOGGER,
        )
        assert rc == 0

    def test_apply_plan_with_swapped_bundle_fails_binding(
        self, bundle_tgz: Path, tmp_path: Path
    ) -> None:
        """Negative control: applying a plan bound to bundle A while passing
        a *different* bundle B must trip the plan-binding gate. Proves the
        digest in plan.json is genuinely load-bearing, not cosmetic."""
        from fluid_build.cli import apply as apply_mod
        from fluid_build.cli import plan as plan_mod

        plan_out = tmp_path / "p.json"
        plan_mod.run(_plan_args(str(bundle_tgz), str(plan_out)), LOGGER)

        # Build a *different* bundle (different contract id → different digest).
        other = _resolved_contract()
        other["id"] = "example.other_v1"
        other_bundle = tmp_path / "other.tgz"
        build_bundle_tgz(other, other_bundle, contract_id="example.other_v1")

        with pytest.raises(CLIError) as exc_info:
            apply_mod.run(
                _apply_args(str(plan_out), bundle=str(other_bundle)),
                LOGGER,
            )
        assert exc_info.value.event == "apply_plan_digest_bundle_mismatch"

    def test_apply_directly_on_tgz(self, bundle_tgz: Path) -> None:
        """``fluid apply <bundle>.tgz`` (no plan.json) must load the
        contract from the bundle and dispatch via the provider — the
        ``apply.run`` ``else`` branch also routes through
        ``load_contract_with_overlay``."""
        from fluid_build.cli import apply as apply_mod

        rc = apply_mod.run(_apply_args(str(bundle_tgz)), LOGGER)
        assert rc == 0
