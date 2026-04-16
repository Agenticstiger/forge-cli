# Copyright 2024-2026 Agentics Transformation Ltd
# Licensed under the Apache License, Version 2.0

"""Slice 8 tests: drift-aware CI regeneration.

The classifier in ``fluid_build.cli.artifact_ci_state`` distinguishes:

* pristine          → file matches recorded sha; safe to silently overwrite
* drifted           → file exists but body differs; caller must preserve
* missing_from_disk → recorded in state but deleted; safe to regenerate
* missing_from_state → exists but never recorded; caller must skip

A fresh generation writes the header + body to disk and the sha of the
*pre-header* body to ci-state.  That lets two successive pristine
generations (which produce different on-disk bytes because the header
carries a fresh timestamp) still compare equal at the body level.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fluid_build.cli.artifact_ci_state import (
    CIStateDocument,
    CIStateDriftReport,
    build_ci_state_payload,
    classify_ci_drift,
    load_ci_state,
    write_ci_state,
)
from fluid_build.cli.artifact_paths import product_ci_state_path
from fluid_build.cli.pipeline_generator import build_pipeline_config, write_pipeline_files
from fluid_build.forge.core.pipeline_templates import PipelineTemplateGenerator


def _generate_and_record(
    product_root: Path,
    provider: str = "github_actions",
    complexity: str = "standard",
) -> tuple[dict, list[Path], CIStateDocument]:
    """Helper: generate, write, and persist ci-state — mimics slice 7's flow."""
    config = build_pipeline_config(provider=provider, complexity=complexity)
    files = PipelineTemplateGenerator().generate_pipeline(config)
    written = write_pipeline_files(
        files,
        product_root,
        command=f"fluid forge --ci {provider}",
        tool_version="0.7.9",
    )
    doc = build_ci_state_payload(
        provider=provider,
        complexity=complexity,
        environments=list(config.environments or []),
        options={},
        written_files=written,
        product_root=product_root,
        body_contents=files,
    )
    write_ci_state(
        doc,
        product_root,
        command=f"fluid forge --ci {provider}",
        tool_version="0.7.9",
    )
    return files, written, doc


class TestLoadCIState:
    def test_returns_none_when_file_missing(self, tmp_path: Path):
        assert load_ci_state(tmp_path) is None

    def test_loads_envelope_wrapped_file(self, tmp_path: Path):
        _generate_and_record(tmp_path)
        state = load_ci_state(tmp_path)
        assert state is not None
        assert state.provider == "github_actions"
        assert state.complexity == "standard"
        assert len(state.files) >= 1
        assert state.files[0].get("sha256")

    def test_rejects_wrong_kind(self, tmp_path: Path):
        product_ci_state_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
        product_ci_state_path(tmp_path).write_text(
            json.dumps(
                {"kind": "NotCIState", "provider": "github_actions", "complexity": "standard"}
            )
        )
        assert load_ci_state(tmp_path) is None

    def test_rejects_invalid_provider(self, tmp_path: Path):
        product_ci_state_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
        product_ci_state_path(tmp_path).write_text(
            json.dumps({"kind": "CIState", "provider": None, "complexity": "standard"})
        )
        assert load_ci_state(tmp_path) is None

    def test_round_trip_through_load(self, tmp_path: Path):
        _, _, original = _generate_and_record(tmp_path, "gitlab_ci", "basic")
        loaded = load_ci_state(tmp_path)
        assert loaded is not None
        assert loaded.provider == original.provider
        assert loaded.complexity == original.complexity
        assert len(loaded.files) == len(original.files)


class TestDriftClassifierNoState:
    """When no ci-state exists, everything on disk is missing_from_state."""

    def test_empty_dir_yields_empty_report(self, tmp_path: Path):
        report = classify_ci_drift(
            tmp_path,
            {".github/workflows/ci.yml": "content"},
            state=None,
        )
        assert isinstance(report, CIStateDriftReport)
        assert report.pristine == []
        assert report.drifted == []
        assert report.missing_from_state == []

    def test_existing_file_without_state_is_missing_from_state(self, tmp_path: Path):
        (tmp_path / ".github" / "workflows").mkdir(parents=True)
        (tmp_path / ".github" / "workflows" / "ci.yml").write_text("content")
        report = classify_ci_drift(
            tmp_path,
            {".github/workflows/ci.yml": "content"},
            state=None,
        )
        assert report.missing_from_state == [".github/workflows/ci.yml"]


class TestDriftClassifierWithState:
    def test_pristine_after_fresh_generation(self, tmp_path: Path):
        files, _, _ = _generate_and_record(tmp_path)
        state = load_ci_state(tmp_path)

        report = classify_ci_drift(tmp_path, files, state=state)
        assert report.pristine and not report.drifted
        assert len(report.pristine) == len(files)

    def test_drifted_after_hand_edit(self, tmp_path: Path):
        files, written, _ = _generate_and_record(tmp_path)
        state = load_ci_state(tmp_path)

        # Hand-edit the first written file — appending a comment line
        # is enough to change the body sha256 after header-stripping.
        written[0].write_text(written[0].read_text() + "\n# hand-edited\n")

        report = classify_ci_drift(tmp_path, files, state=state)
        # The edited file is drifted, nothing else is
        drifted_names = set(report.drifted)
        expected = set(list(files.keys())[:1])
        assert drifted_names == expected

    def test_missing_from_disk_when_deleted(self, tmp_path: Path):
        files, written, _ = _generate_and_record(tmp_path)
        state = load_ci_state(tmp_path)

        written[0].unlink()

        report = classify_ci_drift(tmp_path, files, state=state)
        assert list(files.keys())[0] in report.missing_from_disk

    def test_pristine_ignores_header_timestamp(self, tmp_path: Path):
        """Two pristine writes produce different headers but same body."""
        files, written, _ = _generate_and_record(tmp_path)
        state = load_ci_state(tmp_path)

        # Rewrite the same file with a fresh header (simulating a
        # second `fluid forge` run) — body unchanged, header refreshed.
        new_written = write_pipeline_files(
            files,
            tmp_path,
            command="fluid forge --ci github_actions",
            tool_version="0.7.9",
        )

        # Both writes should still classify as pristine — the recorded
        # sha is against the pre-header body, so a fresh header doesn't
        # count as drift.
        report = classify_ci_drift(tmp_path, files, state=state)
        assert not report.drifted
        assert len(report.pristine) == len(files)


@pytest.mark.parametrize("provider", ["github_actions", "gitlab_ci", "jenkins"])
class TestDriftAcrossProviders:
    def test_pristine_then_drift_then_pristine_again(self, provider: str, tmp_path: Path):
        files, written, _ = _generate_and_record(tmp_path, provider=provider)
        state = load_ci_state(tmp_path)

        # Pristine baseline
        report = classify_ci_drift(tmp_path, files, state=state)
        assert len(report.pristine) == len(files) and not report.drifted

        # Hand-edit every file
        for path in written:
            path.write_text(path.read_text() + "\n# drifted\n")
        report = classify_ci_drift(tmp_path, files, state=state)
        assert len(report.drifted) == len(files)

        # Restore originals — re-run write_pipeline_files with the
        # original bodies to get fresh headers but the right bodies
        write_pipeline_files(
            files,
            tmp_path,
            command="fluid forge",
            tool_version="0.7.9",
        )
        report = classify_ci_drift(tmp_path, files, state=state)
        assert not report.drifted
        assert len(report.pristine) == len(files)


class TestCIStateDocumentRecordedSha:
    def test_returns_sha_for_known_path(self):
        doc = CIStateDocument(
            provider="github_actions",
            complexity="standard",
            files=[{"path": "a.yml", "sha256": "abc"}],
        )
        assert doc.recorded_sha("a.yml") == "abc"

    def test_returns_none_for_unknown_path(self):
        doc = CIStateDocument(
            provider="github_actions",
            complexity="standard",
            files=[{"path": "a.yml", "sha256": "abc"}],
        )
        assert doc.recorded_sha("b.yml") is None

    def test_handles_missing_sha_field(self):
        doc = CIStateDocument(
            provider="github_actions",
            complexity="standard",
            files=[{"path": "a.yml"}],
        )
        assert doc.recorded_sha("a.yml") is None
