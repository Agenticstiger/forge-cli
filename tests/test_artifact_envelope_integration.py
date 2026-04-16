# Copyright 2024-2026 Agentics Transformation Ltd
# Licensed under the Apache License, Version 2.0

"""Integration tests: every real write site must emit envelope fields.

Slice 4 makes envelope emission universal across the CLI's write sites:

* ``save_workspace_config`` → top-level envelope in ``fluid.workspace.yaml``.
* ``write_contract`` → envelope under ``metadata.provenance`` inside
  ``contract.fluid.yaml``.
* ``CopilotMemoryStore.save`` → ``kind`` + ``generated_by`` alongside the
  existing ``schema_version``/``saved_at`` fields in ``copilot-memory.json``.

Read tolerance is load-bearing too: every existing loader must ignore
unknown envelope keys rather than crashing.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from fluid_build.cli.artifact_paths import ENVELOPE_SCHEMA_VERSION
from fluid_build.cli.forge_contract_factory import (
    build_minimal_contract,
    validate_contract_file,
    write_contract,
)
from fluid_build.cli.forge_copilot_memory import (
    MEMORY_SCHEMA_VERSION,
    CopilotMemoryStore,
    CopilotProjectMemory,
)
from fluid_build.cli.workspace_config import (
    load_workspace_config,
    save_workspace_config,
)


class TestWorkspaceConfigEnvelope:
    def test_save_emits_all_four_envelope_fields_at_top_level(self, tmp_path: Path):
        save_workspace_config(tmp_path, name="acme", domain="retail")
        doc = yaml.safe_load((tmp_path / "fluid.workspace.yaml").read_text())
        assert doc["schema_version"] == ENVELOPE_SCHEMA_VERSION
        assert doc["kind"] == "WorkspaceConfig"
        assert doc["generated_at"].endswith("Z")
        assert doc["generated_by"]["tool"] == "fluid-cli"
        assert "command" in doc["generated_by"]

    def test_save_preserves_workspace_payload(self, tmp_path: Path):
        save_workspace_config(
            tmp_path,
            name="acme",
            domain="retail",
            owner_team="data",
            owner_email="data@acme.io",
            provider="gcp",
        )
        doc = yaml.safe_load((tmp_path / "fluid.workspace.yaml").read_text())
        ws = doc["workspace"]
        assert ws["name"] == "acme"
        assert ws["domain"] == "retail"
        assert ws["owner"] == {"team": "data", "email": "data@acme.io"}
        assert ws["provider"] == "gcp"

    def test_load_tolerates_new_envelope(self, tmp_path: Path):
        save_workspace_config(tmp_path, name="acme", domain="retail")
        ws = load_workspace_config(tmp_path)
        assert ws.name == "acme"
        assert ws.domain == "retail"

    def test_load_still_handles_legacy_no_envelope_files(self, tmp_path: Path):
        """A pre-envelope file must still load."""
        legacy = {"workspace": {"name": "legacy", "domain": "other"}}
        (tmp_path / "fluid.workspace.yaml").write_text(yaml.dump(legacy), encoding="utf-8")
        ws = load_workspace_config(tmp_path)
        assert ws.name == "legacy"
        assert ws.domain == "other"

    def test_envelope_command_reflects_caller(self, tmp_path: Path):
        save_workspace_config(
            tmp_path,
            name="acme",
            command="fluid init my-project --blank --yes",
        )
        doc = yaml.safe_load((tmp_path / "fluid.workspace.yaml").read_text())
        assert doc["generated_by"]["command"] == "fluid init my-project --blank --yes"


class TestContractProvenanceEnvelope:
    def test_write_contract_injects_provenance(self, tmp_path: Path):
        path = tmp_path / "contract.fluid.yaml"
        write_contract(build_minimal_contract(), path)

        doc = yaml.safe_load(path.read_text())
        assert "provenance" in doc["metadata"]
        prov = doc["metadata"]["provenance"]
        assert prov["schema_version"] == ENVELOPE_SCHEMA_VERSION
        assert prov["kind"] == "ContractMetadata"
        assert prov["generated_by"]["tool"] == "fluid-cli"

    def test_contract_top_level_kind_is_DataProduct(self, tmp_path: Path):
        """Envelope lives under metadata.provenance — top-level kind unchanged."""
        path = tmp_path / "contract.fluid.yaml"
        write_contract(build_minimal_contract(), path)

        doc = yaml.safe_load(path.read_text())
        assert doc["kind"] == "DataProduct"
        assert doc["fluidVersion"]  # unchanged

    def test_contract_still_validates_via_quick_check(self, tmp_path: Path):
        path = tmp_path / "contract.fluid.yaml"
        write_contract(build_minimal_contract(), path)
        assert validate_contract_file(path) is None

    def test_caller_contract_dict_not_mutated(self, tmp_path: Path):
        """write_contract must not mutate the caller's dict."""
        contract = build_minimal_contract()
        assert "provenance" not in (contract.get("metadata") or {})

        write_contract(contract, tmp_path / "contract.fluid.yaml")

        # Caller's dict remains clean
        assert "provenance" not in (contract.get("metadata") or {})

    def test_custom_command_recorded(self, tmp_path: Path):
        write_contract(
            build_minimal_contract(),
            tmp_path / "contract.fluid.yaml",
            command="fluid forge --blank --target-dir my-prod",
        )
        doc = yaml.safe_load((tmp_path / "contract.fluid.yaml").read_text())
        assert (
            doc["metadata"]["provenance"]["generated_by"]["command"]
            == "fluid forge --blank --target-dir my-prod"
        )


class TestProjectMemoryEnvelope:
    def _build_memory(self) -> CopilotProjectMemory:
        return CopilotProjectMemory(
            schema_version=MEMORY_SCHEMA_VERSION,
            saved_at="2026-01-01T00:00:00Z",
            project_profile={
                "template": "customer-360",
                "provider": "gcp",
                "domain": "retail",
                "owner": "data",
            },
            conventions={
                "build_engines": ["dbt"],
                "binding_platforms": ["bigquery"],
                "binding_formats": ["parquet"],
                "expose_kinds": ["table"],
                "provider_hints": [],
                "source_formats": {},
                "schema_summaries": [],
            },
            recent_outcomes=[],
        )

    def test_save_adds_kind_and_generated_by(self, tmp_path: Path):
        store = CopilotMemoryStore(tmp_path)
        store.save(self._build_memory())

        raw = json.loads(store.path.read_text())
        assert raw["kind"] == "ProjectMemory"
        assert raw["generated_by"]["tool"] == "fluid-cli"

    def test_existing_schema_version_and_saved_at_preserved(self, tmp_path: Path):
        store = CopilotMemoryStore(tmp_path)
        store.save(self._build_memory())

        raw = json.loads(store.path.read_text())
        assert raw["schema_version"] == MEMORY_SCHEMA_VERSION
        assert raw["saved_at"] == "2026-01-01T00:00:00Z"

    def test_load_round_trip_preserves_payload(self, tmp_path: Path):
        store = CopilotMemoryStore(tmp_path)
        store.save(self._build_memory())

        loaded = store.load()
        assert loaded is not None
        assert loaded.project_profile["domain"] == "retail"
        assert loaded.project_profile["provider"] == "gcp"
        assert loaded.conventions["build_engines"] == ["dbt"]

    def test_load_ignores_extra_envelope_fields(self, tmp_path: Path):
        """The loader must not crash on unknown envelope keys at the top level."""
        # Hand-write a v1 file with the envelope already present.
        store = CopilotMemoryStore(tmp_path)
        store.save(self._build_memory())

        # Tamper: add an additional "unknown_envelope_field" — loader should ignore
        raw = json.loads(store.path.read_text())
        raw["unknown_envelope_field"] = "ignored"
        store.path.write_text(json.dumps(raw), encoding="utf-8")

        loaded = store.load()
        assert loaded is not None
        assert loaded.project_profile["domain"] == "retail"
