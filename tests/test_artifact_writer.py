# Copyright 2024-2026 Agentics Transformation Ltd
# Licensed under the Apache License, Version 2.0

"""Tests for fluid_build.cli.artifact_writer — the single I/O choke point."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from fluid_build.cli.artifact_writer import ArtifactWriter, ArtifactWriteRecord


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class TestDryRun:
    """Dry-run mode must never touch disk and must record ``would-create``."""

    def test_write_text_dry_run_writes_nothing(self, tmp_path: Path):
        target = tmp_path / "nested" / "dir" / "file.yaml"
        writer = ArtifactWriter(command="fluid init --dry-run", dry_run=True)

        result_path = writer.write_text(target, "hello world")

        assert result_path == target
        assert not target.exists()
        assert not (tmp_path / "nested").exists()  # parents not created in dry run
        assert len(writer.records) == 1
        assert writer.records[0].action == "would-create"
        assert writer.records[0].sha256 == _sha(b"hello world")
        assert writer.records[0].size == len(b"hello world")

    def test_write_bytes_dry_run_writes_nothing(self, tmp_path: Path):
        target = tmp_path / "bin.dat"
        writer = ArtifactWriter(command="fluid init --dry-run", dry_run=True)
        writer.write_bytes(target, b"\x00\x01\x02")

        assert not target.exists()
        assert writer.records[0].action == "would-create"
        assert writer.records[0].size == 3

    def test_dry_run_sha256_matches_real_run_bytes(self, tmp_path: Path):
        """A dry-run receipt must be diffable against a real run."""
        content = "the same content both runs"
        writer_dry = ArtifactWriter(command="fluid init", dry_run=True)
        writer_real = ArtifactWriter(command="fluid init", dry_run=False)

        writer_dry.write_text(tmp_path / "a.yaml", content)
        writer_real.write_text(tmp_path / "a.yaml", content)

        assert (
            writer_dry.records[0].sha256 == writer_real.records[0].sha256
        )
        assert writer_dry.records[0].size == writer_real.records[0].size


class TestRealWrites:
    def test_create_action_on_fresh_write(self, tmp_path: Path):
        writer = ArtifactWriter(command="fluid forge", dry_run=False)
        target = tmp_path / "fresh.yaml"
        writer.write_text(target, "abc")

        assert target.read_text() == "abc"
        assert writer.records[0].action == "create"
        assert writer.records[0].sha256 == _sha(b"abc")

    def test_unchanged_action_when_bytes_are_identical(self, tmp_path: Path):
        target = tmp_path / "same.yaml"
        target.write_text("same content")

        writer = ArtifactWriter(command="fluid forge", dry_run=False)
        writer.write_text(target, "same content")

        assert writer.records[0].action == "unchanged"

    def test_update_action_when_bytes_differ(self, tmp_path: Path):
        target = tmp_path / "changed.yaml"
        target.write_text("old")

        writer = ArtifactWriter(command="fluid forge", dry_run=False)
        writer.write_text(target, "new")

        assert target.read_text() == "new"
        assert writer.records[0].action == "update"

    def test_parent_directories_are_created(self, tmp_path: Path):
        target = tmp_path / "a" / "b" / "c" / "deep.yaml"
        writer = ArtifactWriter(command="fluid init", dry_run=False)
        writer.write_text(target, "hello")

        assert target.exists()
        assert target.read_text() == "hello"

    def test_sequence_of_actions(self, tmp_path: Path):
        target = tmp_path / "seq.yaml"
        writer = ArtifactWriter(command="fluid forge", dry_run=False)

        writer.write_text(target, "v1")
        writer.write_text(target, "v1")  # same → unchanged
        writer.write_text(target, "v2")  # different → update
        writer.skip(target, reason="user declined")

        actions = [r.action for r in writer.records]
        assert actions == ["create", "unchanged", "update", "skip"]
        assert writer.records[-1].reason == "user declined"


class TestRecordAPI:
    def test_records_are_ordered(self, tmp_path: Path):
        writer = ArtifactWriter(command="fluid init", dry_run=False)
        writer.write_text(tmp_path / "a.yaml", "a")
        writer.write_text(tmp_path / "b.yaml", "b")
        writer.write_text(tmp_path / "c.yaml", "c")

        paths = [r.path.name for r in writer.records]
        assert paths == ["a.yaml", "b.yaml", "c.yaml"]

    def test_clear_resets_records(self, tmp_path: Path):
        writer = ArtifactWriter(command="fluid init", dry_run=False)
        writer.write_text(tmp_path / "a.yaml", "a")
        assert len(writer.records) == 1
        writer.clear()
        assert writer.records == []

    def test_skip_action_captures_reason(self, tmp_path: Path):
        writer = ArtifactWriter(command="fluid forge", dry_run=False)
        writer.skip(tmp_path / "never.yaml", reason="collision with existing")
        rec = writer.records[0]
        assert rec.action == "skip"
        assert rec.reason == "collision with existing"

    def test_record_type_is_frozen_fields(self, tmp_path: Path):
        """ArtifactWriteRecord carries exactly the fields we rely on."""
        writer = ArtifactWriter(command="fluid forge", dry_run=False)
        writer.write_text(tmp_path / "f.yaml", "hello")
        rec = writer.records[0]
        assert isinstance(rec, ArtifactWriteRecord)
        assert rec.path is not None
        assert rec.action in {"create", "update", "would-create", "unchanged", "skip"}
        assert rec.size >= 0


class TestCommandMetadata:
    def test_command_string_preserved(self):
        writer = ArtifactWriter(command="fluid forge --ci jenkins", tool_version="0.42.1")
        assert writer.command == "fluid forge --ci jenkins"
        assert writer.tool_version == "0.42.1"
