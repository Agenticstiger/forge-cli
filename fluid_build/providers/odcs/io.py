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

"""Filesystem helpers for the ODCS provider — read/write ODCS YAML/JSON.

When the file *looks* like an ODCS contract (``apiVersion`` and ``kind``
present) we route the load through ``OpenDataContractStandard.from_file()``
— the Bitol-vendor-listed Pydantic library — so structural errors surface
at parse time, not deep in the mappers. Falls back to plain YAML/JSON
parsing for files that don't conform to the ODCS shape (round-trip
fixtures that intentionally exercise pass-through buckets).

The mappers continue to operate on plain dicts to keep our
``odcs_passthrough`` round-trip fidelity contract intact; the lib is used
as the file-I/O boundary, not the canonical internal type.

Borrowed-not-built per /borrow-before-build (commit msg has receipts):
  - https://pypi.org/project/open-data-contract-standard/
  - https://github.com/datacontract/open-data-contract-standard-python
  - Listed on https://github.com/bitol-io/open-data-contract-standard/blob/main/vendors.md
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, Union

LOG = logging.getLogger(__name__)


def read_input(path: Union[str, Path]) -> Dict[str, Any]:
    """Load an ODCS YAML/JSON file.

    Routes ODCS-shaped files through ``OpenDataContractStandard.from_file()``
    so Pydantic catches structural issues at the boundary. Falls back to
    plain ``yaml.safe_load`` / ``json.load`` when the file doesn't have
    the ``apiVersion`` + ``kind`` markers (e.g. partial fixtures).
    """
    input_path = Path(path)
    raw = _raw_load(input_path)
    if not isinstance(raw, dict):
        return raw  # malformed; let the caller surface the type error

    if _looks_like_odcs(raw):
        try:
            from open_data_contract_standard.model import (
                OpenDataContractStandard,
            )

            dc = OpenDataContractStandard.from_file(str(input_path))
            # ``model_dump`` with by_alias=True preserves the wire-format key
            # names; ``exclude_none=True`` keeps the dict shape lean so the
            # mappers don't see a flood of explicit ``None`` keys that the
            # author never set.
            return dc.model_dump(by_alias=True, exclude_none=True)
        except Exception as exc:  # noqa: BLE001 — soft-fail to raw parse
            LOG.debug(
                "OpenDataContractStandard.from_file failed on %s; falling back to raw YAML: %s",
                input_path,
                exc,
            )

    return raw


def write_output(data: Dict[str, Any], path: Union[Path, str], fmt: str) -> None:
    """Serialise an ODCS dict to YAML or JSON.

    For YAML output on ODCS-shaped payloads we hand the dict to
    ``OpenDataContractStandard`` and call ``to_yaml()`` so the wire-format
    matches what the Bitol-tooling-ecosystem produces. Falls back to
    ``yaml.dump`` for non-ODCS shapes and for explicit JSON output.
    """
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if fmt == "yaml" and isinstance(data, dict) and _looks_like_odcs(data):
        try:
            from open_data_contract_standard.model import (
                OpenDataContractStandard,
            )

            dc = OpenDataContractStandard.model_validate(data)
            output_path.write_text(dc.to_yaml(), encoding="utf-8")
            return
        except Exception as exc:  # noqa: BLE001 — soft-fail to raw dump
            LOG.debug(
                "OpenDataContractStandard.to_yaml failed for %s; falling back to raw YAML dump: %s",
                output_path,
                exc,
            )

    with open(output_path, "w", encoding="utf-8") as f:
        if fmt == "yaml":
            import yaml

            yaml.dump(data, f, default_flow_style=False, sort_keys=False)
        else:
            json.dump(data, f, indent=2)


def _raw_load(path: Path) -> Any:
    with open(path, encoding="utf-8") as f:
        if path.suffix in (".yaml", ".yml"):
            import yaml

            return yaml.safe_load(f)
        return json.load(f)


def _looks_like_odcs(data: Dict[str, Any]) -> bool:
    """Quick sniff: ODCS docs carry ``apiVersion`` + ``kind: DataContract``."""
    return (
        isinstance(data, dict)
        and "apiVersion" in data
        and str(data.get("kind", "")).lower() in {"datacontract", "data_contract"}
    )
