# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Tests for the unified host-side plugin manager + the Validator-role wiring."""

from __future__ import annotations

import pytest

from fluid_build import plugin_manager as PM


class _FakeEP:
    def __init__(self, name, obj, *, boom=False):
        self.name = name
        self._obj = obj
        self._boom = boom

    def load(self):
        if self._boom:
            raise RuntimeError("load exploded")
        return self._obj


class _ToyValidator:
    """Duck-typed fluid_sdk.Validator: plan() emits findings as action dicts."""

    def __init__(self):
        pass

    def plan(self, contract):
        return [
            {
                "op": "emit_finding",
                "resource_id": "OWNER",
                "params": {
                    "severity": "error",
                    "code": "OWNER_MISSING",
                    "message": "no owner",
                    "path": "metadata.owner",
                },
            },
            {
                "op": "emit_finding",
                "resource_id": "DESC",
                "params": {"severity": "warning", "code": "NO_DESC", "message": "no description"},
            },
            {"op": "noise", "resource_id": "ignored"},  # non-finding action ignored
        ]


class _BoomValidator:
    def plan(self, contract):
        # A secret-shaped message that MUST NOT reach the logs/findings.
        raise ValueError("leaked-secret-sk-live-abc123")


# ── allow/block policy ────────────────────────────────────────────────


def test_is_allowed_default(monkeypatch):
    monkeypatch.delenv("FLUID_PLUGINS_ALLOWLIST", raising=False)
    monkeypatch.delenv("FLUID_PLUGINS_BLOCKLIST", raising=False)
    assert PM.is_allowed("anything") is True


def test_blocklist_wins(monkeypatch):
    monkeypatch.setenv("FLUID_PLUGINS_BLOCKLIST", "evil, other")
    assert PM.is_allowed("evil") is False
    assert PM.is_allowed("good") is True


def test_allowlist_excludes_unlisted(monkeypatch):
    monkeypatch.setenv("FLUID_PLUGINS_ALLOWLIST", "only-this")
    assert PM.is_allowed("only-this") is True
    assert PM.is_allowed("someone-else") is False


# ── severity normalization ────────────────────────────────────────────


def test_normalize_severity():
    assert PM._normalize_severity("error") == "error"
    assert PM._normalize_severity("WARNING") == "warn"
    assert PM._normalize_severity("fatal") == "critical"
    assert PM._normalize_severity("") == "info"
    assert PM._normalize_severity(None) == "info"
    # Unknown fails safe to error (mirrors Severity.coerce).
    assert PM._normalize_severity("errror") == "error"


# ── iter_plugins isolation ────────────────────────────────────────────


def test_iter_plugins_skips_blocked_and_broken(monkeypatch):
    eps = [
        _FakeEP("ok", _ToyValidator),
        _FakeEP("blocked", _ToyValidator),
        _FakeEP("broken", None, boom=True),
    ]
    monkeypatch.setattr(PM, "_entry_points", lambda group: eps)
    monkeypatch.setenv("FLUID_PLUGINS_BLOCKLIST", "blocked")
    monkeypatch.delenv("FLUID_PLUGINS_ALLOWLIST", raising=False)
    loaded = dict(PM.iter_plugins("fluid_build.validators"))
    assert set(loaded) == {"ok"}  # blocked excluded, broken load isolated


# ── collect_validator_findings ────────────────────────────────────────


def test_collect_validator_findings_parses_and_normalizes(monkeypatch):
    monkeypatch.setattr(PM, "_entry_points", lambda group: [_FakeEP("toy", _ToyValidator)])
    monkeypatch.delenv("FLUID_PLUGINS_ALLOWLIST", raising=False)
    monkeypatch.delenv("FLUID_PLUGINS_BLOCKLIST", raising=False)
    findings = PM.collect_validator_findings({"metadata": {}})
    assert len(findings) == 2  # the "noise" action is ignored
    by_code = {f["code"]: f for f in findings}
    assert by_code["OWNER_MISSING"]["severity"] == "error"
    assert by_code["OWNER_MISSING"]["path"] == "metadata.owner"
    assert by_code["OWNER_MISSING"]["plugin"] == "toy"
    assert by_code["NO_DESC"]["severity"] == "warn"  # "warning" normalized


def test_collect_validator_findings_isolates_raising_plugin(monkeypatch):
    monkeypatch.setattr(PM, "_entry_points", lambda group: [_FakeEP("badval", _BoomValidator)])
    monkeypatch.delenv("FLUID_PLUGINS_ALLOWLIST", raising=False)
    monkeypatch.delenv("FLUID_PLUGINS_BLOCKLIST", raising=False)
    findings = PM.collect_validator_findings({})
    assert len(findings) == 1
    f = findings[0]
    assert f["severity"] == "error"
    assert "badval" in f["message"]
    assert "ValueError" in f["message"]  # typed; only the exception type is surfaced
    assert "leaked-secret" not in f["message"]  # the raw exception message is NOT surfaced


def test_no_plugins_is_noop(monkeypatch):
    monkeypatch.setattr(PM, "_entry_points", lambda group: [])
    assert PM.collect_validator_findings({"x": 1}) == []


# ── validate.py fold ──────────────────────────────────────────────────


def test_run_role_validators_folds_into_validation_result(monkeypatch):
    from fluid_build.cli import validate as V
    from fluid_build.schema_manager import ValidationResult

    fake_findings = [
        {"severity": "error", "code": "E1", "message": "bad", "path": "a.b", "plugin": "p"},
        {"severity": "critical", "code": "C1", "message": "very bad", "path": None, "plugin": "p"},
        {"severity": "warn", "code": "W1", "message": "meh", "path": None, "plugin": "p"},
        {"severity": "info", "code": "I1", "message": "fyi", "path": None, "plugin": "p"},
    ]
    monkeypatch.setattr(
        PM, "collect_validator_findings", lambda contract, logger=None: fake_findings
    )

    import logging

    result = ValidationResult(is_valid=True)
    V._run_role_validators({}, result, logging.getLogger("t"))
    # error + critical fail validation; warn is a warning; info is dropped.
    assert result.is_valid is False
    assert len(result.errors) == 2
    assert len(result.warnings) == 1
    assert any("E1" in e and "(at a.b)" in e for e in result.errors)
    assert any("W1" in w for w in result.warnings)
    assert not any("I1" in m for m in result.errors + result.warnings)


# ── allow/block spelling + distribution-qualified grants ──────────────────────


def test_is_allowed_matches_case_insensitively_with_dash_equal_underscore(monkeypatch):
    monkeypatch.delenv("FLUID_PLUGINS_ALLOWLIST", raising=False)
    monkeypatch.setenv("FLUID_PLUGINS_BLOCKLIST", "on_prem,Kubernetes")
    assert PM.is_allowed("on-prem") is False
    assert PM.is_allowed("ON_PREM") is False
    assert PM.is_allowed("kubernetes") is False
    assert PM.is_allowed("KUBERNETES") is False
    assert PM.is_allowed("other") is True
    monkeypatch.delenv("FLUID_PLUGINS_BLOCKLIST")
    monkeypatch.setenv("FLUID_PLUGINS_ALLOWLIST", "On-Prem")
    assert PM.is_allowed("on_prem") is True
    assert PM.is_allowed("other") is False


class _Dist:
    def __init__(self, name, version="1.0"):
        self.name = name
        self.version = version


class _EP:
    def __init__(self, name, dist):
        self.name = name
        self.dist = dist


@pytest.mark.parametrize(
    "name,expected",
    [
        ("acme-fluid-aws", "acme-fluid-aws"),
        ("Acme_Fluid.AWS", "Acme_Fluid.AWS"),
        ("acme-fluid-aws spoof", None),
        ("acme\x1b[31m", None),
        ("acme\n", None),
        ("", None),
        ("-lead", None),
        (None, None),
    ],
)
def test_granted_distribution_reads_the_real_name_and_validates_it(name, expected):
    assert PM.granted_distribution(_EP("aws", _Dist(name))) == expected
    assert PM.granted_distribution(_EP("aws", None)) is None


def test_override_granted_uses_the_distribution_name_not_the_display_string(monkeypatch):
    monkeypatch.setenv("FLUID_PLUGINS_ALLOWLIST", "acme-fluid-aws:aws")
    assert PM.override_granted(_EP("aws", _Dist("acme-fluid-aws"))) is True
    assert PM.override_granted(_EP("AWS", _Dist("Acme_Fluid.AWS"))) is True
    assert PM.override_granted(_EP("aws", _Dist("acme-fluid-aws spoof"))) is False
    assert PM.override_granted(_EP("aws", _Dist("other"))) is False
    assert PM.override_granted(_EP("aws", None)) is False
    monkeypatch.setenv("FLUID_PLUGINS_ALLOWLIST", "aws")
    assert PM.override_granted(_EP("aws", _Dist("acme-fluid-aws"))) is False
