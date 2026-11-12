# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Pin ``fluid plan --check-sovereignty`` against fail-open regressions.

The bug these tests exist for: ``AwsProvider`` has no public
``validate_sovereignty`` hook, so ``run_validate_sovereignty`` returned ``[]``,
and ``plan.py`` rendered an empty list as the string ``PASS`` — on a contract
``fluid validate`` rejects with two residency errors. **Absence of a check was
rendered as a pass**, on a governance control, with exit code 0.

The invariant, asserted from several angles below: the word ``PASS`` appears
only when a check actually ran and found nothing.
"""

import argparse
import logging
from typing import Any, Dict

import pytest

from fluid_build.cli.plan import _report_sovereignty

LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _contract(
    *,
    region: str = "us-east-1",
    mode: str = "strict",
    denied: bool = True,
    sovereignty: bool = True,
) -> Dict[str, Any]:
    """A v0.7.x contract whose binding sits outside the allowed regions."""
    contract: Dict[str, Any] = {
        "fluidVersion": "0.7.6",
        "kind": "DataProduct",
        "id": "analytics.eu.customer_events_v1",
        "name": "EU Customer Events",
        "description": "Pseudonymised customer interaction events, EU-resident.",
        "domain": "Customer",
        "metadata": {
            "layer": "Silver",
            "productType": "ADP",
            "owner": {"team": "customer-platform", "email": "customer-platform@example.com"},
        },
        "exposes": [
            {
                "exposeId": "customer_events",
                "kind": "table",
                "binding": {
                    "platform": "aws",
                    "format": "parquet",
                    "location": {
                        "region": region,
                        "database": "customer_analytics",
                        "table": "customer_events",
                        "bucket": "acme-lake",
                        "path": "curated/customer_analytics/customer_events/",
                    },
                },
                "contract": {
                    "schema": [
                        {"name": "event_id", "type": "string", "required": True},
                        {"name": "event_time", "type": "timestamp", "required": True},
                    ]
                },
            }
        ],
    }
    if sovereignty:
        block: Dict[str, Any] = {
            "enforcementMode": mode,
            "jurisdiction": "EU",
            "allowedRegions": ["eu-central-1", "eu-west-1"],
        }
        if denied:
            block["deniedRegions"] = ["us-east-1"]
        contract["sovereignty"] = block
    return contract


class _NoHookProvider:
    """Stands in for ``AwsProvider`` — no ``validate_sovereignty`` hook."""

    name = "aws"


class _CleanHookProvider:
    name = "gcp"

    def validate_sovereignty(self, contract):  # pragma: no cover - trivial
        return []


class _ViolatingHookProvider:
    name = "gcp"

    def validate_sovereignty(self, contract):  # pragma: no cover - trivial
        return ["Data resides outside the EU"]


class _RaisingHookProvider:
    name = "gcp"

    def validate_sovereignty(self, contract):  # pragma: no cover - trivial
        raise RuntimeError("provider exploded")


# ---------------------------------------------------------------------------
# The fail-open itself
# ---------------------------------------------------------------------------


class TestNoProviderHookIsNotAPass:
    pytestmark = pytest.mark.unit

    def test_missing_hook_does_not_print_pass_on_violating_contract(self, capsys):
        """The exact regression: no hook + violating contract used to print PASS."""
        blocked = _report_sovereignty(_contract(), _NoHookProvider(), "aws", LOG)
        out = capsys.readouterr().out

        assert "PASS" not in out
        assert blocked is True

    def test_missing_hook_falls_back_to_the_builtin_policy_engine(self, capsys):
        """A provider without a hook still gets a real answer, not a shrug."""
        _report_sovereignty(_contract(), _NoHookProvider(), "aws", LOG)
        out = capsys.readouterr().out

        assert "us-east-1" in out  # the offending region is named
        assert "built-in policy engine" in out  # and the source is attributed
        assert "no sovereignty hook" in out

    def test_unbuildable_provider_still_runs_the_check(self, capsys):
        """``hook_provider=None`` must not silently skip a requested check.

        The call site used to read ``if check_sovereignty and hook_provider``,
        so a provider that failed to build swallowed the flag entirely and the
        command exited 0 having checked nothing.
        """
        blocked = _report_sovereignty(_contract(), None, "aws", LOG)
        out = capsys.readouterr().out

        assert blocked is True
        assert "PASS" not in out

    def test_raising_hook_is_not_a_pass(self, capsys):
        """A hook that blows up must not read as "checked, clean"."""
        blocked = _report_sovereignty(_contract(), _RaisingHookProvider(), "gcp", LOG)
        out = capsys.readouterr().out

        assert blocked is True
        assert "PASS" not in out


# ---------------------------------------------------------------------------
# The paths that legitimately pass
# ---------------------------------------------------------------------------


class TestLegitimateVerdicts:
    pytestmark = pytest.mark.unit

    def test_compliant_contract_passes_and_names_its_source(self, capsys):
        blocked = _report_sovereignty(
            _contract(region="eu-central-1"), _NoHookProvider(), "aws", LOG
        )
        out = capsys.readouterr().out

        assert blocked is False
        assert "PASS" in out
        assert "built-in policy engine" in out

    def test_provider_hook_clean_result_passes(self, capsys):
        blocked = _report_sovereignty(_contract(), _CleanHookProvider(), "gcp", LOG)
        out = capsys.readouterr().out

        assert blocked is False
        assert "PASS" in out
        assert "provider hook" in out

    def test_provider_hook_violations_block(self, capsys):
        blocked = _report_sovereignty(_contract(), _ViolatingHookProvider(), "gcp", LOG)
        out = capsys.readouterr().out

        assert blocked is True
        assert "Data resides outside the EU" in out
        assert "PASS" not in out

    def test_contract_without_a_sovereignty_block_is_not_checked_not_passed(self, capsys):
        """Nothing to check is reported as such — never as a pass."""
        blocked = _report_sovereignty(_contract(sovereignty=False), _NoHookProvider(), "aws", LOG)
        out = capsys.readouterr().out

        assert blocked is False
        assert "NOT CHECKED" in out
        assert "PASS" not in out


# ---------------------------------------------------------------------------
# enforcementMode semantics — and parity with ``fluid validate``
# ---------------------------------------------------------------------------


class TestEnforcementMode:
    pytestmark = pytest.mark.unit

    def test_advisory_downgrades_an_allowed_region_mismatch_to_a_warning(self, capsys):
        """Advisory mode does real work: a mere mismatch reports without blocking."""
        blocked = _report_sovereignty(
            _contract(mode="advisory", denied=False), _NoHookProvider(), "aws", LOG
        )
        out = capsys.readouterr().out

        assert blocked is False
        assert "finding(s)" in out  # reported...
        assert "us-east-1" in out  # ...and named

    def test_advisory_still_blocks_an_explicitly_denied_region(self, capsys):
        """An explicit deny is an error in any mode — and ``fluid validate``
        exits 1 on it, so ``plan`` must too or the stages disagree."""
        blocked = _report_sovereignty(
            _contract(mode="advisory", denied=True), _NoHookProvider(), "aws", LOG
        )
        assert blocked is True
        assert "PASS" not in capsys.readouterr().out

    @pytest.mark.parametrize(
        "kwargs",
        [
            dict(region="eu-central-1"),  # compliant
            dict(mode="advisory", denied=False),  # warning only
            dict(mode="advisory", denied=True),  # error under advisory
            dict(mode="strict", denied=True),  # error under strict
        ],
    )
    def test_blocking_verdict_matches_fluid_validate(self, kwargs):
        """``plan --check-sovereignty`` must never be weaker than ``validate``.

        Both consult ``policy.sovereignty``; this pins that they agree on every
        combination, so a contract cannot pass one stage and fail the next.
        """
        from fluid_build.policy.sovereignty import validate_sovereignty

        contract = _contract(**kwargs)
        _, messages = validate_sovereignty(contract)
        validate_would_block = any("❌" in m for m in messages)

        blocked = _report_sovereignty(contract, _NoHookProvider(), "aws", LOG)
        assert blocked == validate_would_block


# ---------------------------------------------------------------------------
# End-to-end through ``plan.run`` — the exit code is the CI gate
# ---------------------------------------------------------------------------


class TestPlanExitCode:
    pytestmark = pytest.mark.unit

    @staticmethod
    def _args(tmp_path, contract_path, **kw):
        defaults = dict(
            contract=str(contract_path),
            env=None,
            out=str(tmp_path / "plan.json"),
            verbose=False,
            validate_actions=False,
            estimate_cost=False,
            check_sovereignty=True,
            provider=None,
            project=None,
            region=None,
        )
        defaults.update(kw)
        return argparse.Namespace(**defaults)

    @staticmethod
    def _write(tmp_path, contract):
        import yaml

        path = tmp_path / "contract.fluid.yaml"
        path.write_text(yaml.safe_dump(contract), encoding="utf-8")
        return path

    def test_violating_contract_exits_non_zero_and_writes_no_plan(self, tmp_path, capsys):
        from fluid_build._contract_loader import CLIError
        from fluid_build.cli.plan import run

        path = self._write(tmp_path, _contract())
        with pytest.raises(CLIError) as excinfo:
            run(self._args(tmp_path, path), LOG)

        assert excinfo.value.exit_code == 1
        assert excinfo.value.event == "sovereignty_violation"
        assert not (tmp_path / "plan.json").exists()
        assert "Plan saved" not in capsys.readouterr().out

    def test_compliant_contract_exits_zero(self, tmp_path):
        from fluid_build.cli.plan import run

        path = self._write(tmp_path, _contract(region="eu-central-1"))
        assert run(self._args(tmp_path, path), LOG) == 0

    def test_no_check_sovereignty_skips_the_check_and_says_so(self, tmp_path, caplog, capsys):
        """``--no-check-sovereignty`` writes the plan anyway: on the console and at WARNING."""
        from fluid_build.cli.plan import run

        path = self._write(tmp_path, _contract())
        with caplog.at_level(logging.WARNING):
            assert run(self._args(tmp_path, path, check_sovereignty=False), LOG) == 0
        assert (tmp_path / "plan.json").exists()
        assert "sovereignty check was SKIPPED" in caplog.text
        assert "Sovereignty check skipped (--no-check-sovereignty)" in capsys.readouterr().out

    def test_a_clean_provider_hook_does_not_weaken_check_sovereignty(self, tmp_path, monkeypatch):
        """The explicit flag runs the default check first, so a hook that reports
        nothing cannot pass a contract ``fluid validate`` refuses."""
        import fluid_build.cli.plan as plan_mod
        from fluid_build._contract_loader import CLIError

        monkeypatch.setattr(
            plan_mod, "_hook_provider", lambda *_a, **_k: ("aws", _CleanHookProvider())
        )
        path = self._write(tmp_path, _contract())
        with pytest.raises(CLIError) as excinfo:
            plan_mod.run(self._args(tmp_path, path, check_sovereignty=True), LOG)
        assert excinfo.value.event == "sovereignty_violation"
        assert not (tmp_path / "plan.json").exists()


class TestPlanChecksSovereigntyByDefault:
    """``fluid plan`` with no flag refuses what ``fluid validate`` refuses."""

    pytestmark = pytest.mark.unit

    def _run(self, tmp_path, contract):
        from fluid_build.cli.plan import run

        path = TestPlanExitCode._write(tmp_path, contract)
        return run(TestPlanExitCode._args(tmp_path, path, check_sovereignty=None), LOG)

    @pytest.mark.parametrize(
        "kwargs",
        [
            {},  # strict, outside allowedRegions and in deniedRegions
            {"denied": False},  # strict, outside allowedRegions
            {"mode": "advisory"},  # an explicit deny blocks in every mode
        ],
        ids=["strict-denied", "strict-not-allowed", "advisory-denied"],
    )
    def test_a_blocking_contract_is_refused_and_no_plan_is_written(self, tmp_path, kwargs, capsys):
        from fluid_build._contract_loader import CLIError

        with pytest.raises(CLIError) as excinfo:
            self._run(tmp_path, _contract(**kwargs))
        assert (excinfo.value.exit_code, excinfo.value.event) == (1, "sovereignty_violation")
        assert not (tmp_path / "plan.json").exists()
        out = capsys.readouterr().out
        assert "us-east-1" in out and "Sovereignty check FAILED" in out
        # The expose is named, printed literally (Rich would take "[id]" for markup).
        assert "[customer_events]" in out
        # The findings ride on the error, as local_plan_validation_failed's do, so an
        # engine-API caller (Command Center's plan endpoint) can show the refusal.
        context = excinfo.value.context
        assert context["errors"] and all("[customer_events]" in e for e in context["errors"])
        assert "us-east-1" in " ".join(context["errors"]) and context["message"]

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"region": "eu-central-1"},  # compliant
            {"mode": "advisory", "denied": False},  # advisory: a warning, not a block
            {"sovereignty": False},  # no policy to check
        ],
        ids=["compliant", "advisory-not-allowed", "no-policy"],
    )
    def test_a_contract_that_does_not_block_plans_as_before(self, tmp_path, kwargs, capsys):
        assert self._run(tmp_path, _contract(**kwargs)) == 0
        assert (tmp_path / "plan.json").exists()
        assert "Sovereignty check" not in capsys.readouterr().out

    def test_the_default_check_builds_no_provider(self, tmp_path, monkeypatch):
        """A plain plan stays off the cloud SDKs (Command Center's plan relies on it)."""
        import fluid_build.cli.plan as plan_mod
        from fluid_build._contract_loader import CLIError

        def no_provider(*_args, **_kwargs):
            raise AssertionError("the default sovereignty check built a provider")

        monkeypatch.setattr(plan_mod, "build_provider", no_provider)
        with pytest.raises(CLIError):
            self._run(tmp_path, _contract())

    @pytest.mark.parametrize(
        "argv, expected",
        [([], None), (["--check-sovereignty"], True), (["--no-check-sovereignty"], False)],
    )
    def test_the_flag_has_three_states(self, argv, expected):
        from fluid_build.cli import build_parser

        args = build_parser().parse_args(["plan", "contract.fluid.yaml", *argv])
        assert args.check_sovereignty is expected
