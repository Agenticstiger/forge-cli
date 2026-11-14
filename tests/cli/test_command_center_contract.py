# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""What Command Center calls in forge-cli, pinned so a shrink cannot break it silently.

Read from Command Center's ``fluid_cc_backend`` (origin/main 88067918, 2026-11-13). It
installs ``data-product-forge`` with no extras and uses forge two ways:

* the CLI: ``fluid forge data-model from-intent`` (``fluid_cli_service.py:1655``) and
  ``fluid stats --json`` (``:2586``);
* in-process imports: ``copilot.pii``, ``copilot.enrichment``,
  ``forge_datamodel.emit.validator`` and ``cli.forge_copilot_runtime.build_capability_matrix``.

Command Center wraps ``build_capability_matrix`` in a ``try``, so a broken import there
would only empty its template list, without an error. These tests fail instead.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fluid_build.cli import build_parser

CONTRACT = {
    "fluidVersion": "0.7.6",
    "kind": "DataProduct",
    "id": "sales.customers",
    "name": "Customers",
    "metadata": {"owner": {"team": "sales", "email": "sales@example.com"}},
    "exposes": [
        {
            "exposeId": "customers",
            "kind": "table",
            "binding": {"platform": "local", "format": "parquet", "location": {"path": "x"}},
            "contract": {
                "schema": [
                    {"name": "customer_id", "type": "string", "required": True},
                    {"name": "email", "type": "string"},
                ]
            },
        }
    ],
}


@pytest.mark.parametrize(
    "extra",
    [["--deterministic"], ["--llm-provider", "gemini"], ["--llm-provider", "anthropic"]],
)
def test_from_intent_argv_parses(extra: list) -> None:
    args = build_parser().parse_args(
        [
            "forge",
            "data-model",
            "from-intent",
            "intent.fluid.yaml",
            "--output",
            "contract.fluid.yaml",
            "--technique",
            "dimensional",
            "--quiet",
            *extra,
        ]
    )
    assert args.cmd == "forge" or getattr(args, "func", None) is not None


def test_stats_json_runs_on_an_empty_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    import logging

    from fluid_build.cli import stats

    monkeypatch.chdir(tmp_path)
    args = build_parser().parse_args(["stats", "--since", "30d", "--by", "provider", "--json"])
    assert stats.run(args, logging.getLogger("test")) == 0
    json.loads(capsys.readouterr().out)


def test_pii_classifier_tags_in_place() -> None:
    import copy

    from fluid_build.copilot.pii import classify_contract_schemas

    contract = copy.deepcopy(CONTRACT)
    summary = classify_contract_schemas(contract)
    assert isinstance(summary, dict)
    email = contract["exposes"][0]["contract"]["schema"][1]
    assert email.get("sensitivity") == "pii"


def test_enrichment_and_validator_import_and_run() -> None:
    import copy

    from fluid_build.copilot.enrichment import enrich_contract
    from fluid_build.forge_datamodel.emit.validator import FluidContractValidator

    enrich_contract(copy.deepcopy(CONTRACT))
    report = FluidContractValidator().validate(contract=copy.deepcopy(CONTRACT))
    assert report.model_dump(mode="json", by_alias=True)


def test_capability_matrix_keeps_its_import_path_and_templates() -> None:
    from fluid_build.cli.forge_copilot_runtime import build_capability_matrix

    matrix = build_capability_matrix()
    assert isinstance(matrix.get("templates"), dict) and matrix["templates"]


def test_library_entry_points_import() -> None:
    import fluid_build
    import fluid_build.engine  # noqa: F401 — Command Center probes it
    from fluid_build.describe import self_describe
    from fluid_build.schema_manager import FluidSchemaManager

    assert fluid_build.__version__
    assert callable(self_describe) and FluidSchemaManager.latest_bundled_version()
