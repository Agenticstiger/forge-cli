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

"""Unit tests for fluid_build.extension_schemas (native extension support).

Covers discovery isolation/redaction, the factored validator core, the LLM
grounding fragment, and schema-gated assembly of proposed extension blocks.
"""

from __future__ import annotations

import importlib.metadata as md

import fluid_build.extension_schemas as es


class _FakeEP:
    """Minimal stand-in for importlib.metadata.EntryPoint."""

    def __init__(self, name, fn):
        self.name = name
        self._fn = fn

    def load(self):
        return self._fn


# A tiny but real draft-07 schema (libraries/patterns must be non-empty).
_CUSTOM_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "type": "object",
    "required": ["libraries", "patterns"],
    "additionalProperties": False,
    "properties": {
        "libraries": {"type": "array", "minItems": 1},
        "patterns": {"type": "array", "minItems": 1},
    },
}


def _patch_eps(monkeypatch, eps):
    # The discovery helpers ignore the group when our lambda ignores it, so the
    # same patch serves both the schemas and validators groups per-test.
    monkeypatch.setattr(md, "entry_points", lambda *a, **k: eps)


# ── iter_extension_schemas ───────────────────────────────────────────


def test_iter_empty(monkeypatch) -> None:
    _patch_eps(monkeypatch, [])
    assert es.iter_extension_schemas() == {}


def test_iter_collects_and_isolates(monkeypatch) -> None:
    def boom(fv=None):
        raise RuntimeError("kaboom")

    eps = [
        _FakeEP("customScaffold", lambda fv=None: dict(_CUSTOM_SCHEMA)),
        _FakeEP("brokenExt", boom),
        _FakeEP("weirdExt", lambda fv=None: "not-a-dict"),
        _FakeEP("zeroArg", lambda: dict(_CUSTOM_SCHEMA)),  # zero-arg provider supported
    ]
    _patch_eps(monkeypatch, eps)
    out = es.iter_extension_schemas("0.7.4")
    assert set(out) == {"customScaffold", "zeroArg"}


def test_iter_redacts_secret_in_log(monkeypatch, caplog) -> None:
    secret = "ghp_" + "a" * 36  # GitHub-PAT shape — reliably redacted

    def boom(fv=None):
        raise RuntimeError(f"auth failed with {secret}")

    _patch_eps(monkeypatch, [_FakeEP("brokenExt", boom)])
    with caplog.at_level("WARNING"):
        assert es.iter_extension_schemas() == {}
    assert secret not in caplog.text


# ── run_extension_validators ─────────────────────────────────────────


def test_run_validators_no_extensions() -> None:
    assert es.run_extension_validators({}) == []
    assert es.run_extension_validators({"extensions": "nope"}) == []


def test_run_validators_collects_and_isolates(monkeypatch) -> None:
    def good(extensions, errors):
        if "customScaffold" in extensions:
            errors.append("bad thing")

    def boom(extensions, errors):
        raise RuntimeError("validator exploded")

    _patch_eps(monkeypatch, [_FakeEP("customScaffold", good), _FakeEP("brokenExt", boom)])
    errs = es.run_extension_validators({"extensions": {"customScaffold": {"x": 1}}})
    assert "extensions.customScaffold: bad thing" in errs
    assert any("validator 'brokenExt' raised" in e for e in errs)


def test_run_validators_does_not_mutate_contract(monkeypatch) -> None:
    def mutator(extensions, errors):
        extensions["customScaffold"]["injected"] = True  # must not leak out

    _patch_eps(monkeypatch, [_FakeEP("customScaffold", mutator)])
    contract = {"extensions": {"customScaffold": {"x": 1}}}
    es.run_extension_validators(contract)
    assert contract["extensions"]["customScaffold"] == {"x": 1}


# ── build_extension_prompt_fragment ──────────────────────────────────


def test_fragment_empty_when_no_schemas() -> None:
    assert es.build_extension_prompt_fragment({}) == ""


def test_fragment_mentions_key_and_target() -> None:
    frag = es.build_extension_prompt_fragment({"customScaffold": _CUSTOM_SCHEMA})
    assert "extensions.customScaffold" in frag
    assert "proposed_extensions" in frag


# ── assemble_proposed_extensions ─────────────────────────────────────


def test_assemble_keeps_valid_drops_unknown_key(monkeypatch) -> None:
    _patch_eps(monkeypatch, [_FakeEP("customScaffold", lambda fv=None: dict(_CUSTOM_SCHEMA))])
    proposed = {
        "customScaffold": {"libraries": [{"id": "ci"}], "patterns": [{"use": "ci:basic"}]},
        "unknownExt": {"anything": 1},  # no installed schema → dropped
    }
    assert set(es.assemble_proposed_extensions(proposed)) == {"customScaffold"}


def test_assemble_drops_schema_invalid(monkeypatch) -> None:
    _patch_eps(monkeypatch, [_FakeEP("customScaffold", lambda fv=None: dict(_CUSTOM_SCHEMA))])
    proposed = {"customScaffold": {"libraries": [], "patterns": []}}  # minItems:1 violated
    assert es.assemble_proposed_extensions(proposed) == {}


def test_assemble_empty_paths(monkeypatch) -> None:
    assert es.assemble_proposed_extensions(None) == {}
    assert es.assemble_proposed_extensions({}) == {}
    _patch_eps(monkeypatch, [])  # no schemas installed
    assert es.assemble_proposed_extensions({"customScaffold": {"x": 1}}) == {}
