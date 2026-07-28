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

"""``fluid contract apply-suggestion`` CLI integration tests.

Pin the wiring between the AI-guardrails module and the CLI: a
contract.suggested.json with a benign field merges; one with an
``ai``-provenance value on a blocked path returns exit 1 with the
right error code.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict

import pytest
import yaml

from fluid_build.cli._common import CLIError
from fluid_build.cli._forge_ai_guardrails import (
    FieldSuggestion,
    Suggestion,
    write_suggestion_file,
)
from fluid_build.cli.contract import _run_apply_suggestion


def _draft_contract() -> Dict[str, Any]:
    return {
        "fluidVersion": "0.7.3",
        "kind": "DataProduct",
        "id": "bronze.x",
        "metadata": {"layer": "Bronze", "owner": {"team": "dp", "email": "dp@x.co"}},
        "builds": [
            {
                "id": "ingest",
                "pattern": "acquisition",
                "engine": "duckdb",
                "properties": {
                    "source": {
                        "kind": "filesystem",
                        "connection": {"uri": "/tmp/x.csv"},
                        "mode": "full_refresh",
                    }
                },
                "outputs": ["raw"],
            }
        ],
        "exposes": [{"exposeId": "raw", "kind": "table"}],
    }


@pytest.fixture
def logger() -> logging.Logger:
    return logging.getLogger("test.contract.apply_suggestion")


# ── Happy path ───────────────────────────────────────────────────────────


class TestApplySuggestionHappyPath:
    def test_merges_introspection_and_ai_into_yaml(self, tmp_path: Path, logger):
        contract_path = tmp_path / "x.fluid.yaml"
        contract_path.write_text(yaml.safe_dump(_draft_contract()), encoding="utf-8")

        suggestion = Suggestion(
            contract_id="bronze.x",
            fields=[
                FieldSuggestion(
                    path="metadata.classification",
                    value="confidential",
                    provenance="ai",
                    rationale="email PII detected",
                ),
                FieldSuggestion(
                    path="exposes[0].contract.schema",
                    value=[{"name": "id", "type": "long"}],
                    provenance="introspection",
                ),
            ],
        )
        suggestion_path = write_suggestion_file(suggestion, tmp_path / "x.suggested.json")

        rc = _run_apply_suggestion(
            SimpleNamespace(
                suggestion=str(suggestion_path),
                target=str(contract_path),
                accept_provenance=None,
                out=None,
            ),
            logger,
        )
        assert rc == 0

        # Backup file written.
        assert (contract_path.parent / "x.fluid.yaml.bak").exists()
        # Merged contract has both suggested values.
        merged = yaml.safe_load(contract_path.read_text())
        assert merged["metadata"]["classification"] == "confidential"
        assert merged["exposes"][0]["contract"]["schema"][0]["name"] == "id"

    def test_accept_only_introspection_filters_ai(self, tmp_path: Path, logger):
        contract_path = tmp_path / "x.fluid.json"
        contract_path.write_text(json.dumps(_draft_contract()), encoding="utf-8")

        suggestion = Suggestion(
            contract_id="bronze.x",
            fields=[
                FieldSuggestion(
                    path="metadata.classification",
                    value="confidential",
                    provenance="ai",
                ),
                FieldSuggestion(
                    path="builds[0].properties.source.streams",
                    value=["a", "b"],
                    provenance="introspection",
                ),
            ],
        )
        sp = write_suggestion_file(suggestion, tmp_path / "x.suggested.json")

        rc = _run_apply_suggestion(
            SimpleNamespace(
                suggestion=str(sp),
                target=str(contract_path),
                accept_provenance=["introspection"],
                out=None,
            ),
            logger,
        )
        assert rc == 0
        merged = json.loads(contract_path.read_text())
        # ai field excluded.
        assert "classification" not in merged["metadata"]
        # introspection field merged.
        assert merged["builds"][0]["properties"]["source"]["streams"] == ["a", "b"]


# ── Guardrail rejections ─────────────────────────────────────────────────


class TestApplySuggestionGuardrails:
    def test_ai_on_secret_ref_blocked(self, tmp_path: Path, logger):
        contract_path = tmp_path / "x.fluid.yaml"
        contract_path.write_text(yaml.safe_dump(_draft_contract()), encoding="utf-8")

        suggestion = Suggestion(
            contract_id="bronze.x",
            fields=[
                FieldSuggestion(
                    path="builds[0].properties.source.connection.secretRef",
                    value="vault://hallucinated",
                    provenance="ai",
                )
            ],
        )
        sp = write_suggestion_file(suggestion, tmp_path / "x.suggested.json")

        with pytest.raises(CLIError) as exc_info:
            _run_apply_suggestion(
                SimpleNamespace(
                    suggestion=str(sp),
                    target=str(contract_path),
                    accept_provenance=None,
                    out=None,
                ),
                logger,
            )
        assert exc_info.value.event == "ai_guardrail_violation"
        assert exc_info.value.exit_code == 1
        # Target contract NOT mutated when guardrail rejects.
        assert "secretRef" not in yaml.safe_load(contract_path.read_text())["builds"][0][
            "properties"
        ]["source"]["connection"].get("secretRef", "")

    def test_missing_suggestion_file_returns_exit_1(self, tmp_path: Path, logger):
        contract_path = tmp_path / "x.fluid.yaml"
        contract_path.write_text(yaml.safe_dump(_draft_contract()), encoding="utf-8")
        with pytest.raises(CLIError):
            _run_apply_suggestion(
                SimpleNamespace(
                    suggestion=str(tmp_path / "nope.json"),
                    target=str(contract_path),
                    accept_provenance=None,
                    out=None,
                ),
                logger,
            )

    def test_missing_target_returns_exit_1(self, tmp_path: Path, logger):
        sp = write_suggestion_file(
            Suggestion(contract_id="x", fields=[]),
            tmp_path / "s.json",
        )
        with pytest.raises(CLIError):
            _run_apply_suggestion(
                SimpleNamespace(
                    suggestion=str(sp),
                    target=str(tmp_path / "absent.fluid.yaml"),
                    accept_provenance=None,
                    out=None,
                ),
                logger,
            )


# ── End-to-end via fluid CLI subprocess ──────────────────────────────────


class TestApplySuggestionViaCli:
    def test_subcommand_registered(self):
        # Confirms the subcommand is wired into the CLI surface.
        r = subprocess.run(
            [sys.executable, "-m", "fluid_build.cli", "contract", "--help"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert r.returncode == 0
        assert "apply-suggestion" in r.stdout
