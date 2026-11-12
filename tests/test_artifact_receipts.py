# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Tests for fluid_build.cli.artifact_receipts."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from fluid_build.cli import artifact_receipts
from fluid_build.cli.artifact_receipts import (
    ReceiptBuilder,
    ReceiptDocument,
    ReceiptEntry,
    generate_run_id,
)
from fluid_build.cli.artifact_writer import ArtifactWriter


class TestGenerateRunId:
    def test_format_has_timestamp_and_random(self):
        rid = generate_run_id()
        assert "-" in rid
        ts, rand = rid.split("-", 1)
        assert len(ts) == 11  # hex timestamp width
        assert len(rand) == 8  # 4 bytes hex = 8 chars

    def test_run_ids_are_unique(self):
        """Tight loop generation must not collide on random bits."""
        ids = {generate_run_id() for _ in range(1000)}
        assert len(ids) == 1000

    def test_ids_in_one_millisecond_never_repeat(self, monkeypatch):
        """The collision behind the flake above (``999 == 1000``), made certain.

        With the clock stopped and the random source fixed, a random suffix
        repeats at once. Uniqueness in one process must not rest on chance.
        """
        frozen = time.time()
        monkeypatch.setattr(artifact_receipts, "time", SimpleNamespace(time=lambda: frozen))
        monkeypatch.setattr(
            artifact_receipts,
            "secrets",
            SimpleNamespace(token_hex=lambda n=32: "ab" * n, randbits=lambda k: 0x1234),
        )
        ids = [generate_run_id() for _ in range(1000)]
        assert len(set(ids)) == 1000
        assert ids == sorted(ids)

    def test_ids_keep_increasing_when_the_clock_steps_back(self, monkeypatch):
        start = time.time()
        clock = iter([start, start - 1.0, start - 2.0])
        monkeypatch.setattr(artifact_receipts, "time", SimpleNamespace(time=lambda: next(clock)))
        ids = [generate_run_id() for _ in range(3)]
        assert len(set(ids)) == 3
        assert ids == sorted(ids)

    def test_a_used_up_suffix_moves_to_the_next_millisecond(self, monkeypatch):
        ms = int(time.time() * 1000)
        monkeypatch.setattr(artifact_receipts, "_last_ms", -1)
        monkeypatch.setattr(artifact_receipts, "_last_suffix", 0)
        # +0.5 ms so the float round trip cannot land on the millisecond before.
        monkeypatch.setattr(
            artifact_receipts, "time", SimpleNamespace(time=lambda: (ms + 0.5) / 1000)
        )
        monkeypatch.setattr(
            artifact_receipts, "secrets", SimpleNamespace(randbits=lambda k: 0xFFFFFFFF)
        )
        assert generate_run_id() == f"{ms:011x}-ffffffff"
        assert generate_run_id() == f"{ms + 1:011x}-ffffffff"

    @pytest.mark.skipif(not hasattr(os, "fork"), reason="os.fork is POSIX only")
    def test_a_forked_child_starts_its_own_sequence(self):
        """A real ``os.fork()``, in a fresh interpreter with no other threads.

        Without the at-fork reset, the child continues the parent's sequence
        and makes the same id as the parent's next one (``...-00000006``).
        """
        script = textwrap.dedent(
            """
            import os, types
            from fluid_build.cli import artifact_receipts as ar
            ar.time = types.SimpleNamespace(time=lambda: 1_800_000_000.0005)
            seeds = iter([5, 9])
            ar.secrets = types.SimpleNamespace(randbits=lambda k: next(seeds))
            print(ar.generate_run_id(), flush=True)
            read_end, write_end = os.pipe()
            pid = os.fork()
            if pid == 0:
                os.write(write_end, ar.generate_run_id().encode())
                os._exit(0)
            os.close(write_end)
            os.waitpid(pid, 0)
            print(os.read(read_end, 64).decode())
            print(ar.generate_run_id())
            """
        )
        root = Path(artifact_receipts.__file__).resolve().parents[2]
        paths = [str(root), os.environ.get("PYTHONPATH", "")]
        env = {**os.environ, "PYTHONPATH": os.pathsep.join(p for p in paths if p)}
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=root,
            env=env,
            check=True,
        )
        ms = 1_800_000_000_000
        parent_first, child, parent_next = result.stdout.split()
        assert parent_first == f"{ms:011x}-00000005"
        assert child == f"{ms:011x}-00000009"
        assert parent_next == f"{ms:011x}-00000006"


class TestReceiptEntryToDict:
    def test_minimal_entry_drops_empty_fields(self):
        entry = ReceiptEntry(path="a.yaml", action="create")
        d = entry.to_dict()
        assert d == {"path": "a.yaml", "action": "create"}

    def test_full_entry_keeps_all_fields(self):
        entry = ReceiptEntry(
            path="a.yaml",
            action="create",
            sha256="abc",
            size=10,
            reason="new",
        )
        d = entry.to_dict()
        assert d == {
            "path": "a.yaml",
            "action": "create",
            "sha256": "abc",
            "size": 10,
            "reason": "new",
        }


class TestReceiptBuilder:
    def test_defaults(self):
        b = ReceiptBuilder(flow="blank")
        assert b.flow == "blank"
        assert b.dry_run is False
        assert b.run_id  # auto-generated
        assert b.entries == []
        assert b.skipped == []
        assert b.inputs == {}

    def test_explicit_run_id_honored(self):
        b = ReceiptBuilder(flow="blank", run_id="fixed-id")
        assert b.run_id == "fixed-id"

    def test_dry_run_flag_propagates_to_document(self):
        b = ReceiptBuilder(flow="blank", dry_run=True)
        doc = b.build_document()
        assert doc.dry_run is True

    def test_record_entry_classifies_by_action(self):
        b = ReceiptBuilder(flow="blank")
        b.record_entry(Path("a.yaml"), action="create", sha256="sha1", size=5)
        b.record_entry(Path("b.yaml"), action="skip", reason="exists")
        assert len(b.entries) == 1
        assert b.entries[0].action == "create"
        assert len(b.skipped) == 1
        assert b.skipped[0].reason == "exists"

    def test_set_inputs_drops_none_values(self):
        b = ReceiptBuilder(flow="blank")
        b.set_inputs(template="customer-360", provider=None, use_case="analytics")
        assert b.inputs == {"template": "customer-360", "use_case": "analytics"}

    def test_merge_inputs(self):
        b = ReceiptBuilder(flow="blank")
        b.set_inputs(template="x")
        b.merge_inputs({"provider": "gcp", "template": "y"})  # overwrite
        assert b.inputs == {"template": "y", "provider": "gcp"}

    def test_record_writes_copies_from_writer(self, tmp_path: Path):
        writer = ArtifactWriter(command="fluid init", dry_run=False)
        writer.write_text(tmp_path / "a.yaml", "hello")
        writer.write_text(tmp_path / "a.yaml", "hello")  # unchanged
        writer.write_text(tmp_path / "a.yaml", "world")  # update
        writer.skip(tmp_path / "b.yaml", reason="collision")

        builder = ReceiptBuilder(flow="blank")
        builder.record_writes(writer, scope_root=tmp_path)

        assert [e.action for e in builder.entries] == [
            "create",
            "unchanged",
            "update",
        ]
        assert len(builder.skipped) == 1
        assert builder.skipped[0].reason == "collision"

    def test_record_writes_makes_paths_relative_to_scope_root(self, tmp_path: Path):
        writer = ArtifactWriter(command="fluid init", dry_run=False)
        writer.write_text(tmp_path / "nested" / "a.yaml", "hello")

        builder = ReceiptBuilder(flow="blank")
        builder.record_writes(writer, scope_root=tmp_path)

        assert builder.entries[0].path == "nested/a.yaml"

    def test_record_writes_keeps_absolute_path_outside_scope(self, tmp_path: Path):
        outside_dir = tmp_path / "outside"
        inside_dir = tmp_path / "inside"
        outside_dir.mkdir()
        inside_dir.mkdir()

        writer = ArtifactWriter(command="fluid init", dry_run=False)
        writer.write_text(outside_dir / "user.json", "{}")

        builder = ReceiptBuilder(flow="blank")
        builder.record_writes(writer, scope_root=inside_dir)

        # Path lives outside the scope, so it stays absolute.
        assert Path(builder.entries[0].path).is_absolute()


class TestBuildDocument:
    def test_document_shape(self):
        b = ReceiptBuilder(flow="blank", run_id="fixed-id")
        b.record_entry(Path("a.yaml"), action="create", sha256="s1", size=5)
        b.set_inputs(template="customer-360")

        doc = b.build_document()
        assert isinstance(doc, ReceiptDocument)
        assert doc.run_id == "fixed-id"
        assert doc.flow == "blank"
        assert doc.dry_run is False
        assert len(doc.artifacts) == 1
        assert doc.inputs == {"template": "customer-360"}

    def test_to_payload_shape(self):
        b = ReceiptBuilder(flow="template", run_id="r1")
        b.record_entry(
            Path("contract.fluid.yaml"),
            action="create",
            sha256="abc",
            size=100,
        )
        b.set_inputs(template="customer-360", provider="local")

        payload = b.build_document().to_payload()

        assert payload == {
            "run_id": "r1",
            "flow": "template",
            "dry_run": False,
            "artifacts": [
                {
                    "path": "contract.fluid.yaml",
                    "action": "create",
                    "sha256": "abc",
                    "size": 100,
                }
            ],
            "skipped": [],
            "inputs": {"template": "customer-360", "provider": "local"},
        }
