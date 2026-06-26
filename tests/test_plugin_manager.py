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

"""Tests for the unified host-side plugin manager + the Validator-role wiring."""

from __future__ import annotations

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
