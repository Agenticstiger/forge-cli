# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Singer tap+target config → FLUID contract importer.

Singer config files are plain JSON. The importer accepts either a single
tap config path (e.g., ``configs/tap-postgres.json``) or a colon-delimited
``<tap-cfg>:<target-cfg>`` pair. Output: a contract using the Meltano
engine (since Meltano runs Singer taps directly).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from .registry import Importer, ImportReport


@dataclass
class SingerImporter(Importer):
    name: str = "singer"

    def can_import(self, source: str) -> bool:
        # ``source`` is "<tap-cfg>" or "<tap-cfg>:<target-cfg>"
        first = source.split(":", 1)[0]
        return Path(first).exists()

    def import_to_contract(
        self, source: str, *, options: Optional[Dict[str, Any]] = None
    ) -> tuple[Dict[str, Any], ImportReport]:
        parts = source.split(":", 1)
        tap_cfg_path = Path(parts[0])
        target_cfg_path = Path(parts[1]) if len(parts) == 2 else None
        if not tap_cfg_path.exists():
            raise FileNotFoundError(tap_cfg_path)

        with tap_cfg_path.open(encoding="utf-8") as f:
            tap_cfg = json.load(f)
        report = ImportReport()
        report.mapped_one_to_one.append("tap.config")
        target_cfg: Dict[str, Any] = {}
        if target_cfg_path and target_cfg_path.exists():
            with target_cfg_path.open(encoding="utf-8") as f:
                target_cfg = json.load(f)
            report.mapped_one_to_one.append("target.config")
        elif target_cfg_path:
            report.required_defaults.append("target config: file missing")

        # Heuristic: derive the tap kind from the file name (`tap-postgres.json` → postgres).
        tap_kind = tap_cfg_path.stem.replace("tap-", "")
        product_id = f"bronze.singer_{tap_kind}"
        return {
            "fluidVersion": "0.7.3",
            "kind": "DataProduct",
            "id": product_id,
            "name": f"Imported from Singer: tap-{tap_kind}",
            "domain": "imported",
            "description": f"Auto-converted from Singer tap config {tap_cfg_path}",
            "metadata": {
                "layer": "Bronze",
                "productType": "SDP",
                "owner": {"team": "imported", "email": "import@forge.local"},
            },
            "builds": [
                {
                    "id": f"ingest_{tap_kind}",
                    "pattern": "acquisition",
                    "engine": "meltano",
                    "capabilities": ["full_refresh"],
                    "properties": {
                        "source": {
                            "kind": tap_kind,
                            "connection": _redact_secrets(tap_cfg),
                            "mode": "full_refresh",
                        },
                        "sink": {"format": "parquet"},
                        "meltano": {"tap": f"tap-{tap_kind}"},
                    },
                    "outputs": ["data"],
                }
            ],
            "exposes": [
                {
                    "exposeId": "data",
                    "kind": "table",
                    "binding": {
                        "platform": "local",
                        "format": "parquet",
                        "location": {"path": "./out/data.parquet"},
                    },
                    "contract": {"schema": [], "schemaPolicy": "discover_and_freeze"},
                }
            ],
        }, report


def _redact_secrets(config: Dict[str, Any]) -> Dict[str, Any]:
    redacted = {}
    for k, v in (config or {}).items():
        lk = k.lower()
        if any(s in lk for s in ("token", "password", "secret", "key", "credential")):
            redacted[k] = f"{{{{ env.{k.upper()} }}}}"
        else:
            redacted[k] = v
    return redacted
