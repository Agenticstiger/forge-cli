# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Packaging-modes PR1 release gate — the LEGACY byte-identity pin.

RFC-packaging-modes.md's compatibility invariant, enforced not promised:
a contract with no ``packaging`` block resolves to the distinct LEGACY
sentinel, and ``fluid generate iac`` — with ``resolve_packaging`` imported
and called at the entry point — emits ``main.tf.json`` **byte-for-byte
identical** to the pre-existing pure emit pipeline, for EVERY existing
example / template / fixture contract in the repo.

Layers:

* ``TestEveryExistingContractIsLegacy`` — every shipped contract resolves
  to the LEGACY sentinel (by identity — no contract predates the feature
  with a ``packaging`` block).
* ``TestGenerateIacByteIdentity`` — the wired CLI path produces the exact
  bytes of the unwired emit pipeline for every cloud-resolvable contract.
* ``TestResolverIsWiredAtTheEntryPoint`` — the pin is not vacuous: the
  resolver genuinely runs inside ``generate_iac.run``.
* ``TestResolvePackaging`` — unit pins for the chokepoint itself
  (sentinel identity, two-level precedence, typed ``PackagingError``s).
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

import pytest
import yaml

from fluid_build.cli import generate_iac
from fluid_build.cli._common import (
    CLIError,
    load_contract_with_overlay,
    resolve_env_templates_in_contract,
)
from fluid_build.iac import assemble_tofu_document, get_iac_plugin, render_tofu_json
from fluid_build.iac.packaging import (
    CONTAINER_KINDS,
    LEGACY,
    ContainerDecision,
    PackagingError,
    resolve_packaging,
)

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]

_LOG = logging.getLogger("test_iac_packaging_default_pin")


def _collect_contract_paths() -> list[Path]:
    """Every existing contract shipped in the repo — examples, templates,
    bootstrap templates, and test fixtures."""
    patterns = [
        ("examples", "**/*.fluid.yaml"),
        ("fluid_build/templates", "**/*.fluid.yaml"),
        ("tools/fluid_bootstrap/templates", "**/*.fluid.yaml"),
        ("tests/fixtures", "**/*.fluid.yaml"),
        ("tests/fixtures/contracts", "**/*.yaml"),
    ]
    found: set[Path] = set()
    for base, pattern in patterns:
        root = REPO_ROOT / base
        if root.is_dir():
            found.update(p for p in root.glob(pattern) if p.is_file())
    return sorted(found)


CONTRACT_PATHS = _collect_contract_paths()
CONTRACT_IDS = [str(p.relative_to(REPO_ROOT)) for p in CONTRACT_PATHS]

# The repo must actually ship contracts — an empty sweep would make the
# pin vacuous (e.g. after a directory rename).
assert len(CONTRACT_PATHS) >= 20, "contract sweep came back suspiciously empty"


def _load_yaml_or_skip(path: Path) -> dict:
    """Best-effort raw YAML load; skip files that are not contract mappings."""
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except yaml.YAMLError:
        pytest.skip(f"{path.name} is not parseable YAML (not a contract)")
    if not isinstance(data, dict):
        pytest.skip(f"{path.name} is not a mapping (not a contract)")
    return data


class TestEveryExistingContractIsLegacy:
    """No shipped contract declares `packaging` — all resolve to the sentinel."""

    @pytest.mark.parametrize("path", CONTRACT_PATHS, ids=CONTRACT_IDS)
    def test_resolves_to_the_legacy_sentinel_by_identity(self, path: Path):
        contract = _load_yaml_or_skip(path)
        assert resolve_packaging(contract) is LEGACY

    def test_legacy_sentinel_shape(self):
        assert LEGACY.is_legacy is True
        assert LEGACY.pool is None and LEGACY.pool_manifest is None
        assert set(LEGACY.decisions) == set(CONTAINER_KINDS)
        assert all(d is ContainerDecision.LEGACY for d in LEGACY.decisions.values())


def _expected_module_bytes(contract_path: Path) -> tuple[str, bytes]:
    """Today's pure emit pipeline (pre-packaging, resolver NOT in the loop).

    Replicates ``generate_iac.run``'s emit body verbatim — load, resolve
    provider, ``plugin.emit`` + ``plugin.emit_data`` with no native actions,
    assemble, render. Raises ``CLIError`` for non-cloud contracts.
    """
    contract = load_contract_with_overlay(str(contract_path), None, _LOG)
    contract = resolve_env_templates_in_contract(contract)
    provider = generate_iac._resolve_provider(contract, "auto")
    plugin = get_iac_plugin(provider)
    resources = plugin.emit(contract, [])
    provider_cfg = plugin.provider_block()
    document = assemble_tofu_document(
        required_providers=plugin.required_providers,
        resources=resources,
        data=plugin.emit_data(contract, []),
        provider={plugin.name: provider_cfg} if provider_cfg else None,
    )
    return provider, render_tofu_json(document).encode("utf-8")


def _run_generate_iac(contract_path: Path, out_dir: Path) -> bytes:
    """The real wired CLI path (`fluid generate iac`) → emitted bytes."""
    args = argparse.Namespace(
        contract=str(contract_path),
        provider="auto",
        out=str(out_dir),
        env=None,
        validate=False,
        shadow=False,
    )
    rc = generate_iac.run(args, _LOG)
    assert rc == 0
    return (out_dir / "main.tf.json").read_bytes()


class TestGenerateIacByteIdentity:
    """The release gate: resolver wired in, output byte-identical."""

    @pytest.mark.parametrize("path", CONTRACT_PATHS, ids=CONTRACT_IDS)
    def test_wired_cli_emit_is_byte_identical(
        self, path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # Pin determinism: the native planner is best-effort (credentials-
        # dependent) — force the no-actions path on BOTH sides so the pin
        # never depends on the developer's cloud environment.
        monkeypatch.setattr(generate_iac, "native_actions", lambda contract, logger: [])
        try:
            _, expected = _expected_module_bytes(path)
        except CLIError as exc:
            pytest.skip(f"not a single-cloud contract ({exc.event})")
        actual = _run_generate_iac(path, tmp_path)
        assert actual == expected, (
            f"{path.relative_to(REPO_ROOT)}: `fluid generate iac` output changed "
            "with the packaging resolver wired in — the LEGACY no-op invariant is broken"
        )


class TestResolverIsWiredAtTheEntryPoint:
    """The pin is not vacuous — resolve_packaging really runs inside run()."""

    def test_resolver_called_once_and_returns_legacy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        example = REPO_ROOT / "examples" / "aws-s3-glue-athena" / "contract.fluid.yaml"
        assert example.is_file()
        monkeypatch.setattr(generate_iac, "native_actions", lambda contract, logger: [])
        seen = []

        def spy(contract):
            resolution = resolve_packaging(contract)
            seen.append(resolution)
            return resolution

        monkeypatch.setattr(generate_iac, "resolve_packaging", spy)
        _run_generate_iac(example, tmp_path)
        assert len(seen) == 1
        assert seen[0] is LEGACY

    def test_invalid_packaging_fails_generate_with_typed_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # A packaging-bearing contract with an invalid combination must fail
        # fast at the entry point (typed PackagingError → CLIError), never
        # silently emit.
        contract = {
            "fluidVersion": "0.7.6",
            "id": "pin.invalid",
            "name": "Pin Invalid",
            "packaging": {"mode": "shared"},  # pool-required
            "exposes": [
                {
                    "exposeId": "t",
                    "binding": {
                        "platform": "aws",
                        "format": "parquet",
                        "location": {"bucket": "b"},
                    },
                }
            ],
        }
        path = tmp_path / "contract.fluid.yaml"
        path.write_text(yaml.safe_dump(contract), encoding="utf-8")
        monkeypatch.setattr(generate_iac, "native_actions", lambda contract, logger: [])
        with pytest.raises(CLIError) as excinfo:
            _run_generate_iac(path, tmp_path / "out")
        assert "pool" in str((excinfo.value.context or {}).get("error", ""))


class TestResolvePackaging:
    """Unit pins for the chokepoint: sentinel, precedence, typed errors."""

    def test_absent_block_returns_sentinel_identity(self):
        contract = {"id": "x", "exposes": [{"exposeId": "a", "binding": {"platform": "aws"}}]}
        assert resolve_packaging(contract) is LEGACY

    def test_rfc_example_1_snowflake_hybrid_tier(self):
        contract = {
            "packaging": {
                "mode": "shared",
                "pool": "sales-domain",
                "containers": {"schema": "isolated", "warehouse": "isolated"},
            },
            "exposes": [{"exposeId": "orders", "binding": {"platform": "snowflake"}}],
        }
        res = resolve_packaging(contract)
        assert res.is_legacy is False and res.pool == "sales-domain"
        assert res.decision_for("database", "orders") is ContainerDecision.REFERENCED
        assert res.decision_for("schema", "orders") is ContainerDecision.OWNED
        assert res.decision_for("warehouse", "orders") is ContainerDecision.OWNED
        assert res.decision_for("bucket") is ContainerDecision.REFERENCED

    def test_binding_override_beats_top_level(self):
        contract = {
            "packaging": {"mode": "isolated"},
            "exposes": [
                {"exposeId": "t", "binding": {"packaging": {"mode": "shared", "pool": "p1"}}},
                {"exposeId": "u", "binding": {}},
            ],
        }
        res = resolve_packaging(contract)
        assert res.decision_for("bucket") is ContainerDecision.OWNED
        assert res.decision_for("bucket", "t") is ContainerDecision.REFERENCED
        assert res.decision_for("bucket", "u") is ContainerDecision.OWNED
        assert res.exposure_for("t").pool == "p1"
        assert res.exposure_for("u").pool is None

    def test_binding_block_inherits_pool_from_top_level(self):
        contract = {
            "packaging": {"pool": "iot-lake"},
            "exposes": [{"exposeId": "t", "binding": {"packaging": {"mode": "shared"}}}],
        }
        res = resolve_packaging(contract)
        assert res.exposure_for("t").pool == "iot-lake"
        assert res.decision_for("bucket", "t") is ContainerDecision.REFERENCED

    def test_mode_defaults_to_isolated_when_block_present(self):
        res = resolve_packaging({"packaging": {"pool": "p"}, "exposes": []})
        assert res.is_legacy is False
        assert all(d is ContainerDecision.OWNED for d in res.decisions.values())

    @pytest.mark.parametrize(
        ("block", "kind"),
        [
            ({"mode": "hybrid"}, "invalid-mode"),
            ({"mode": "shared"}, "pool-required"),
            ({"mode": "isolated", "tier": "gold"}, "invalid-block"),
            ("shared", "invalid-block"),
            ({"containers": "shared"}, "invalid-containers"),
            ({"containers": {"volume": "shared"}}, "invalid-container-kind"),
            ({"containers": {"bucket": "owned"}}, "invalid-container-mode"),
            ({"pool": ""}, "invalid-pool"),
            ({"poolManifest": 7}, "invalid-pool"),
            (
                {"mode": "isolated", "containers": {"cluster": "isolated"}},
                "cluster-isolated-unsupported",
            ),
        ],
    )
    def test_typed_errors(self, block, kind):
        with pytest.raises(PackagingError) as excinfo:
            resolve_packaging({"packaging": block, "exposes": []})
        assert excinfo.value.kind == kind

    def test_pool_required_fires_for_shared_containers_override_too(self):
        with pytest.raises(PackagingError) as excinfo:
            resolve_packaging({"packaging": {"containers": {"bucket": "shared"}}})
        assert excinfo.value.kind == "pool-required"

    def test_purity_contract_not_mutated(self):
        import copy

        contract = {
            "packaging": {"mode": "shared", "pool": "p"},
            "exposes": [{"exposeId": "t", "binding": {"packaging": {"mode": "isolated"}}}],
        }
        snapshot = copy.deepcopy(contract)
        resolve_packaging(contract)
        assert contract == snapshot
