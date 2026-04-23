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

"""Tests for fluid_build.cli.artifact_receipts."""

from __future__ import annotations

from pathlib import Path

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
