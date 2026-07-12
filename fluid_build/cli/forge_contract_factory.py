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

"""Shared contract scaffolding for blank and guided Forge modes.

All contract generation goes through ``build_minimal_contract()`` which
returns a Python dict.  The dict is serialised to YAML via ``yaml.dump``
so that user-supplied values (descriptions containing colons, quotes,
etc.) are escaped correctly.
"""

from __future__ import annotations

__all__ = [
    "build_minimal_contract",
    "create_and_validate_contract",
    "stamp_prompt_profile",
    "write_contract",
    "validate_contract_file",
]

import logging
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

from fluid_build.cli.artifact_paths import CONTRACT_FILENAME
from fluid_build.schema_manager import FluidSchemaManager

LOG = logging.getLogger("fluid.cli.forge.contract_factory")

DOCS_URL = "https://fluid-build.dev/docs/contracts"


def build_minimal_contract(
    *,
    product_id: str = "my-data-product",
    name: Optional[str] = None,
    domain: str = "analytics",
    owner: str = "data-team",
    description: str = "TODO: Describe your data product",
    engine: str = "sql",
    tags: Optional[list] = None,
) -> Dict[str, Any]:
    """Build a minimal but valid FLUID contract as a Python dict.

    This is the single source of truth for scaffold contracts used by
    both ``--blank`` and guided mode.
    """
    owner_metadata: Dict[str, Any]
    if isinstance(owner, dict):
        owner_metadata = dict(owner)
    else:
        owner_metadata = {"team": str(owner)}

    build_doc: Dict[str, Any] = {
        "id": "main",
        "engine": engine,
    }
    if engine == "sql":
        build_doc["pattern"] = "embedded-logic"
        build_doc["properties"] = {"sql": "SELECT 1 AS placeholder"}
    else:
        build_doc["pattern"] = "hybrid-reference"
        build_doc["repository"] = "./models"
        build_doc["properties"] = {"model": "main"}

    return {
        "fluidVersion": FluidSchemaManager.latest_bundled_version(),
        "kind": "DataProduct",
        "id": product_id,
        "name": name or product_id.replace("-", " ").title(),
        "description": description,
        "domain": domain,
        "tags": tags or [],
        "metadata": {
            "layer": "Bronze",
            "productType": "SDP",
            "owner": owner_metadata,
        },
        "builds": [build_doc],
        "exposes": [
            {
                "exposeId": "output",
                "kind": "table",
                "version": "1.0.0",
                "binding": {
                    "platform": "local",
                    "format": "parquet",
                    "location": {
                        "path": "runtime/output.parquet",
                    },
                },
                "contract": {
                    "schema": [
                        {
                            "name": "placeholder",
                            "type": "integer",
                            "required": True,
                        }
                    ]
                },
            },
        ],
    }


def stamp_prompt_profile(contract: Dict[str, Any]) -> Dict[str, Any]:
    """Stamp the active prompt profile into ``metadata.provenance``.

    When a ``fluid forge --prompt-profile <name>`` (or
    ``FLUID_PROMPT_PROFILE``) profile is active, records it at
    ``contract.metadata.provenance.prompt_profile`` so published contracts
    carry the audit trail of *which* prompt overlay authored them. This is
    the single chokepoint used by both the blank/guided write path
    (:func:`write_contract`) and the AI copilot write path
    (``forge_copilot_agent._create_forge_config``).

    No-op — and no key added — when no profile is active, keeping the
    default contract output byte-identical. Mutates *contract* in place and
    returns it. Never raises: provenance stamping must not crash a write.
    """
    try:
        from fluid_build.cli.forge_copilot_prompts import get_active_prompt_profile

        profile = get_active_prompt_profile()
    except Exception:  # noqa: BLE001 — defensive; never block a write on this
        profile = None
    if not profile:
        return contract
    metadata = contract.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
        contract["metadata"] = metadata
    provenance = metadata.get("provenance")
    if not isinstance(provenance, dict):
        provenance = {}
        metadata["provenance"] = provenance
    provenance["prompt_profile"] = profile
    return contract


def write_contract(
    contract: Dict[str, Any],
    path: Path,
    *,
    command: str = "fluid forge",
) -> None:
    """Serialise *contract* to YAML at *path* using ``yaml.dump``.

    Adds a ``metadata.provenance`` block (envelope fields:
    ``schema_version``, ``kind``, ``generated_at``, ``generated_by``) so
    every contract carries its own "generated by" story.  The provenance
    block is additive — existing readers ignore unknown metadata keys,
    and the contract's own ``fluidVersion`` remains the schema version.

    The caller's ``contract`` dict is not mutated: a shallow copy is
    taken before provenance is injected.
    """
    from fluid_build.cli.artifact_envelope import build_envelope

    try:
        from fluid_build import __version__ as tool_version
    except Exception:  # pragma: no cover — defensive
        tool_version = ""

    doc: Dict[str, Any] = dict(contract)
    metadata = dict(doc.get("metadata") or {})
    envelope = build_envelope(
        kind="ContractMetadata",
        command=command,
        tool_version=str(tool_version),
    )
    # Drop the redundant kind — the contract already carries its own
    # top-level ``kind`` (e.g. "DataProduct").  provenance.kind stays as
    # "ContractMetadata" to signal what this sub-block describes.
    metadata["provenance"] = envelope
    doc["metadata"] = metadata
    # Audit trail: record the active prompt profile (if any) into the same
    # provenance block. No-op when no profile is active.
    stamp_prompt_profile(doc)

    header = f"# FLUID Data Product Contract\n# Docs: {DOCS_URL}\n"
    body = yaml.dump(doc, default_flow_style=False, sort_keys=False, allow_unicode=True)
    path.write_text(header + body, encoding="utf-8")
    LOG.debug("Wrote contract to %s", path)


def create_and_validate_contract(
    contract: Dict[str, Any],
    target_dir: Path,
    logger: logging.Logger,
    console: Any = None,
) -> Optional[Path]:
    """Write *contract* to ``target_dir/contract.fluid.yaml`` and validate.

    Returns the contract path on success or ``None`` on failure.

    On failure, prints an ``ActionableError`` panel via
    :func:`fluid_build.cli.errors.print_actionable_error` so the user
    sees a ``Fix:`` line in addition to the symptom. The function
    returns ``None`` (rather than raising) so callers can branch on
    write-result without a try/except — every consumer of this
    function is a contract-emit pipeline that needs to short-circuit
    cleanly when validation fails post-write.
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    contract_path = target_dir / CONTRACT_FILENAME
    write_contract(contract, contract_path)

    error = validate_contract_file(contract_path)
    if error:
        logger.error("Generated contract failed validation: %s", error)
        try:
            from fluid_build.cli.errors import (
                ActionableError,
                print_actionable_error,
            )

            actionable = ActionableError(
                f"The generated contract at {contract_path} is invalid: {error}",
                fix=(
                    f"Delete {contract_path} and re-run 'fluid forge --blank', "
                    f"or manually edit the contract to add the missing keys."
                ),
                docs_url="https://fluid-build.dev/docs/contracts",
            )
            print_actionable_error(actionable, console=console)
        except Exception:  # noqa: BLE001 — defensive; never let errors printer crash
            if console:
                try:
                    console.print(f"[red]Generated contract is invalid: {error}[/red]")
                except Exception:  # noqa: BLE001
                    pass
        return None
    return contract_path


def validate_contract_file(path: Path) -> Optional[str]:
    """Quick-validate the YAML contract at *path*.

    Returns ``None`` on success or an error message string on failure.
    Does **not** run full schema validation -- only checks that the file
    is parseable YAML with the required top-level keys.
    """
    required_keys = {"fluidVersion", "kind", "id"}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return "Contract file is not a YAML mapping"
        missing = required_keys - set(data.keys())
        if missing:
            return f"Contract is missing required keys: {', '.join(sorted(missing))}"
        return None
    except yaml.YAMLError as exc:
        return f"Invalid YAML: {exc}"
    except OSError as exc:
        return f"Cannot read contract file: {exc}"
