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

"""Tests for fluid_build.forge.core.artifact_fanout — stage-3 orchestrator.

Adversarial bias: every test pins a specific contract the downstream
stages depend on. If one of these starts passing under a behavioral
regression, stage 4 (validate artifacts) would catch a divergence we
should have prevented here.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
import yaml

from fluid_build.forge.core.artifact_fanout import (
    DEFAULT_EMIT,
    EMIT_KEYS,
    REFERENCE_ONLY_SKIP,
    FanoutError,
    _contract_has_orchestration_engine,
    _contract_is_reference_only,
    parse_emit_set,
    run_fanout,
)

# ---------------------------------------------------------------------------
# Fixture: the committed hello-world contract is the simplest valid product
# ---------------------------------------------------------------------------


_HELLO_WORLD_CONTRACT = Path(__file__).parent.parent.parent / (
    "examples/01-hello-world/contract.fluid.yaml"
)


@pytest.fixture
def logger():
    return logging.getLogger("test.artifact_fanout")


@pytest.fixture
def bundle_tgz(tmp_path):
    """Build a real Phase-2 bundle from the hello-world contract. Gives us
    the canonical tgz layout stage 3 expects as input."""
    import argparse

    from fluid_build.cli.bundle import run as bundle_run

    contract_copy = tmp_path / "contract.fluid.yaml"
    contract_copy.write_text(_HELLO_WORLD_CONTRACT.read_text())
    tgz = tmp_path / "hello.fluid.bundle.tgz"
    args = argparse.Namespace(contract=str(contract_copy), out=str(tgz), env=None, format="tgz")
    rc = bundle_run(args, logging.getLogger("test.bundle"))
    assert rc == 0, "failed to produce the bundle fixture"
    return tgz


# ---------------------------------------------------------------------------
# parse_emit_set
# ---------------------------------------------------------------------------


class TestParseEmitSet:
    def test_none_means_default(self, logger):
        out = parse_emit_set(None, reference_only=False, logger=logger)
        assert out == list(DEFAULT_EMIT)

    def test_empty_string_means_default(self, logger):
        out = parse_emit_set("", reference_only=False, logger=logger)
        assert out == list(DEFAULT_EMIT)

    def test_default_excludes_odps_and_opds(self, logger):
        """Both 'odps' (alias of broken OPDS emitter) and 'opds' itself are
        opt-in only because the current ``providers/odps/to_odps()`` produces
        a homebrew ``{specVersion: "1.0", ...}`` shape that does NOT match
        the real OPDS v4.1 schema (``{schema, version, product}``). Default
        set is restricted to the verified-conformant emitters:
        odcs (v3.1.0) + odps-bitol (v1.0.0). See
        trello-verify-odps-linux-foundation.md for the fix plan."""
        out = parse_emit_set(None, reference_only=False, logger=logger)
        assert "odps" not in out
        assert "opds" not in out
        assert "odps-bitol" in out  # conformant ✅
        assert "odcs" in out  # conformant ✅

    def test_dbt_is_rejected_loudly(self, logger):
        """dbt is NOT a catalog artifact. --emit dbt must fail with a clear
        message pointing at fluid generate speed-transformation."""
        with pytest.raises(FanoutError) as exc_info:
            parse_emit_set("odps-bitol,dbt", reference_only=False, logger=logger)
        assert exc_info.value.key == "dbt"
        assert "speed-transformation" in str(exc_info.value)

    def test_unknown_key_rejected(self, logger):
        with pytest.raises(FanoutError) as exc_info:
            parse_emit_set("odps-bitol,nonsense", reference_only=False, logger=logger)
        assert "nonsense" in str(exc_info.value)
        assert exc_info.value.key == "nonsense"

    def test_explicit_odps_opt_in_allowed(self, logger):
        """The emitter itself isn't removed — users who know what they're
        doing can opt in via --emit odps."""
        out = parse_emit_set("odps", reference_only=False, logger=logger)
        assert out == ["odps"]

    def test_reference_only_drops_schedule_and_policies(self, logger):
        out = parse_emit_set(
            "odps-bitol,odcs,schedule,policies",
            reference_only=True,
            logger=logger,
        )
        assert "schedule" not in out
        assert "policies" not in out
        assert "odps-bitol" in out
        assert "odcs" in out

    def test_reference_only_keeps_catalog_emits(self, logger):
        """Reference-only products still need catalog artifacts (ODCS,
        ODPS-Bitol) — only execution-owned emitters (schedule, policies) are
        skipped because those live in the product's own repo. OPDS is not
        in the default set while its emitter is being fixed."""
        out = parse_emit_set(None, reference_only=True, logger=logger)
        # Order is canonical (EMIT_KEYS order); OPDS excluded pending fix.
        assert out == ["odps-bitol", "odcs"]

    def test_output_order_is_canonical(self, logger):
        """Regardless of input order, output follows EMIT_KEYS canonical
        order so downstream MANIFEST hashes stay stable. Explicitly opt in
        to opds so we test the full ordering; default set excludes it."""
        out = parse_emit_set(
            "policies,opds,odcs,odps-bitol,schedule",
            reference_only=False,
            logger=logger,
        )
        assert out == ["odps-bitol", "odcs", "opds", "schedule", "policies"]

    def test_deduplication(self, logger):
        out = parse_emit_set("odcs,odcs,odcs,odps-bitol", reference_only=False, logger=logger)
        assert out.count("odcs") == 1

    def test_reference_only_skip_set_matches_docs(self):
        """The plan document + generate_ci.py both reference this set; if
        we ever diverge, both pipelines disagree on what 'reference-only'
        means. Pin the set."""
        assert set(REFERENCE_ONLY_SKIP) == {"schedule", "policies"}


# ---------------------------------------------------------------------------
# Contract inspection helpers
# ---------------------------------------------------------------------------


class TestContractInspectionHelpers:
    def test_reference_only_hybrid_reference(self, tmp_path):
        c = tmp_path / "c.yaml"
        c.write_text("builds:\n  - id: b\n    pattern: hybrid-reference\n")
        assert _contract_is_reference_only(c) is True

    def test_reference_only_plain_reference(self, tmp_path):
        c = tmp_path / "c.yaml"
        c.write_text("builds:\n  - id: b\n    pattern: reference\n")
        assert _contract_is_reference_only(c) is True

    def test_not_reference_when_pattern_is_embedded_logic(self, tmp_path):
        c = tmp_path / "c.yaml"
        c.write_text("builds:\n  - id: b\n    pattern: embedded-logic\n")
        assert _contract_is_reference_only(c) is False

    def test_not_reference_when_no_builds(self, tmp_path):
        c = tmp_path / "c.yaml"
        c.write_text("kind: DataProduct\n")
        assert _contract_is_reference_only(c) is False

    def test_malformed_yaml_defaults_false(self, tmp_path):
        c = tmp_path / "c.yaml"
        c.write_text("not: valid: yaml: [\n")
        assert _contract_is_reference_only(c) is False

    def test_orchestration_engine_detected(self, tmp_path):
        c = tmp_path / "c.yaml"
        c.write_text("orchestration:\n  engine: airflow\n")
        assert _contract_has_orchestration_engine(c) is True

    def test_orchestration_engine_missing(self, tmp_path):
        c = tmp_path / "c.yaml"
        c.write_text("kind: DataProduct\n")
        assert _contract_has_orchestration_engine(c) is False

    def test_orchestration_engine_empty_string_treated_as_absent(self, tmp_path):
        c = tmp_path / "c.yaml"
        c.write_text("orchestration:\n  engine: ''\n")
        assert _contract_has_orchestration_engine(c) is False


# ---------------------------------------------------------------------------
# run_fanout — end-to-end orchestration
# ---------------------------------------------------------------------------


class TestRunFanout:
    def test_default_emit_produces_manifest(self, bundle_tgz, tmp_path, logger):
        out_dir = tmp_path / "art"
        manifest = run_fanout(
            bundle_tgz,
            out_dir,
            emit_raw=None,
            manifest_path=None,
            logger=logger,
        )
        assert (out_dir / "MANIFEST.json").exists()
        assert manifest["digest"].startswith("sha256:")
        assert len(manifest["files"]) >= 1

    def test_output_layout_matches_plan(self, bundle_tgz, tmp_path, logger):
        """Stage 4 validators dispatch by path prefix (odcs/, odps-bitol/,
        opds/, schedule/, policy/). Path prefixes must not drift between
        stage 3 output and stage 4 expectations."""
        out_dir = tmp_path / "art"
        run_fanout(bundle_tgz, out_dir, emit_raw=None, manifest_path=None, logger=logger)

        # Hello-world has no orchestration.engine → schedule auto-skipped.
        # Default emit set excludes opds/odps (broken OPDS emitter; see
        # trello-verify-odps-linux-foundation).
        expected_prefixes = {"odps-bitol/", "odcs/", "policy/"}
        on_disk = {p.relative_to(out_dir).as_posix() for p in out_dir.rglob("*") if p.is_file()}
        present_prefixes = {f.split("/")[0] + "/" for f in on_disk if "/" in f}
        assert (
            expected_prefixes <= present_prefixes
        ), f"missing expected subdirs; got {sorted(present_prefixes)}"

    def test_determinism_two_runs_byte_identical_per_file(self, bundle_tgz, tmp_path, logger):
        """Same bundle input + same emit set → byte-identical artifact files
        + MANIFEST. Non-determinism here corrupts stage-4 SHA-256 checks."""
        out_a = tmp_path / "art-a"
        out_b = tmp_path / "art-b"
        manifest_a = run_fanout(bundle_tgz, out_a, emit_raw=None, manifest_path=None, logger=logger)
        manifest_b = run_fanout(bundle_tgz, out_b, emit_raw=None, manifest_path=None, logger=logger)

        # Each emitted file must be byte-identical across runs.
        files_a = sorted(p.relative_to(out_a) for p in out_a.rglob("*") if p.is_file())
        files_b = sorted(p.relative_to(out_b) for p in out_b.rglob("*") if p.is_file())
        assert files_a == files_b, "file set differs across runs"
        for rel in files_a:
            if rel.as_posix() == "MANIFEST.json":
                # MANIFEST itself may differ if any of its content differs.
                # Assert below once per-file parity is confirmed.
                continue
            a_bytes = (out_a / rel).read_bytes()
            b_bytes = (out_b / rel).read_bytes()
            assert a_bytes == b_bytes, f"{rel}: non-deterministic emitter; bytes differ across runs"

        # With all per-file bytes identical, the merkle root must also match.
        assert manifest_a["digest"] == manifest_b["digest"]

    def test_explicit_emit_filters_down(self, bundle_tgz, tmp_path, logger):
        out_dir = tmp_path / "art"
        run_fanout(bundle_tgz, out_dir, emit_raw="odcs", manifest_path=None, logger=logger)
        on_disk_dirs = {p.name for p in out_dir.iterdir() if p.is_dir()}
        # Only odcs/ + (no other emit subdirs); MANIFEST at the root.
        assert on_disk_dirs == {"odcs"}

    def test_dbt_emit_errors(self, bundle_tgz, tmp_path, logger):
        with pytest.raises(FanoutError) as exc_info:
            run_fanout(
                bundle_tgz, tmp_path / "art", emit_raw="dbt", manifest_path=None, logger=logger
            )
        assert exc_info.value.key == "dbt"

    def test_unknown_emit_key_errors(self, bundle_tgz, tmp_path, logger):
        with pytest.raises(FanoutError):
            run_fanout(
                bundle_tgz,
                tmp_path / "art",
                emit_raw="bogus",
                manifest_path=None,
                logger=logger,
            )

    def test_missing_bundle_errors(self, tmp_path, logger):
        with pytest.raises(FanoutError, match="input not found"):
            run_fanout(
                tmp_path / "does-not-exist.tgz",
                tmp_path / "art",
                emit_raw=None,
                manifest_path=None,
                logger=logger,
            )

    def test_clean_slate_removes_stale_subdirs(self, bundle_tgz, tmp_path, logger):
        """If a previous run left odcs/oldfile.yaml behind, it must not
        pollute the MANIFEST of the new run. Stage-4 SHA-256 check would
        catch it but better to never ship the stale file."""
        out_dir = tmp_path / "art"
        out_dir.mkdir()
        stale = out_dir / "odcs" / "STALE.yaml"
        stale.parent.mkdir()
        stale.write_text("this is leftover from a previous run")

        run_fanout(
            bundle_tgz,
            out_dir,
            emit_raw="odcs",
            manifest_path=None,
            logger=logger,
        )
        assert not stale.exists(), "stale file survived re-run; MANIFEST could be wrong"

    def test_custom_manifest_path(self, bundle_tgz, tmp_path, logger):
        out_dir = tmp_path / "art"
        custom = tmp_path / "custom-manifest.json"
        run_fanout(
            bundle_tgz,
            out_dir,
            emit_raw="odcs",
            manifest_path=custom,
            logger=logger,
        )
        assert custom.exists()
        # Default location should NOT have been used when explicit path given.
        assert not (out_dir / "MANIFEST.json").exists()

    def test_raw_contract_input_works(self, tmp_path, logger):
        """Passing a raw .fluid.yaml (not a tgz) is a supported local-dev
        shortcut — iteration without re-bundling every time."""
        contract = tmp_path / "contract.fluid.yaml"
        contract.write_text(_HELLO_WORLD_CONTRACT.read_text())

        out_dir = tmp_path / "art"
        manifest = run_fanout(
            contract,
            out_dir,
            emit_raw="odcs",
            manifest_path=None,
            logger=logger,
        )
        assert (out_dir / "MANIFEST.json").exists()
        assert len(manifest["files"]) >= 1

    def test_schedule_auto_skipped_when_no_engine(self, bundle_tgz, tmp_path, logger, caplog):
        """Hello-world has no orchestration.engine. Default --emit includes
        schedule; fanout must auto-skip it and log the reason, NOT hard-fail."""
        out_dir = tmp_path / "art"
        with caplog.at_level(logging.INFO, logger="test.artifact_fanout"):
            run_fanout(bundle_tgz, out_dir, emit_raw=None, manifest_path=None, logger=logger)
        # schedule/ must not exist
        assert not (out_dir / "schedule").exists()


# ---------------------------------------------------------------------------
# CLI-level integration — ensure the subcommand is wired
# ---------------------------------------------------------------------------


class TestCliIntegration:
    def test_generate_artifacts_subcommand_registered(self):
        """Regression guard: fluid generate --help must list 'artifacts'."""
        import argparse

        from fluid_build.cli import generate

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="cmd")
        generate.register(sub)

        # Parse a minimal invocation and check the subcommand survives.
        args = parser.parse_args(["generate", "artifacts", "bundle.tgz"])
        assert args.generate_sub == "artifacts"
        assert args.bundle == "bundle.tgz"
