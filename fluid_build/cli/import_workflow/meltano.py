# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Meltano project → FLUID contract importer."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from .registry import Importer, ImportReport


@dataclass
class MeltanoImporter(Importer):
    name: str = "meltano"

    def can_import(self, source: str) -> bool:
        return (Path(source) / "meltano.yml").exists()

    def import_to_contract(
        self, source: str, *, options: Optional[Dict[str, Any]] = None
    ) -> tuple[Dict[str, Any], ImportReport]:
        project_dir = Path(source)
        meltano_yml = project_dir / "meltano.yml"
        if not meltano_yml.exists():
            raise FileNotFoundError(meltano_yml)
        with meltano_yml.open() as f:
            meltano = yaml.safe_load(f) or {}

        report = ImportReport()
        plugins = meltano.get("plugins") or {}
        extractors = plugins.get("extractors") or []
        if not extractors:
            report.unsupported.append("no extractors found in meltano.yml")
            return {}, report

        # Take the first extractor as the source.
        extractor = extractors[0]
        tap_name = extractor.get("name", "tap-unknown")
        tap_kind = tap_name.replace("tap-", "")
        config = extractor.get("config", {}) or {}
        report.mapped_one_to_one.append(f"extractor.{tap_name}")

        loaders = plugins.get("loaders") or []
        if loaders:
            report.mapped_one_to_one.append(f"loader.{loaders[0].get('name', 'unknown')}")

        product_id = f"bronze.{(meltano.get('default_environment') or 'imported')}_{tap_kind}"
        contract = {
            "fluidVersion": "0.7.3",
            "kind": "DataProduct",
            "id": product_id,
            "name": f"Imported from Meltano: {tap_name}",
            "domain": "imported",
            "description": f"Auto-converted from Meltano project at {project_dir}",
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
                            "connection": _redact_secrets(config),
                            "mode": "full_refresh",
                            "streams": _streams_from_select(extractor),
                        },
                        "sink": {"format": "parquet"},
                        "meltano": {"tap": tap_name, "project_dir": str(project_dir)},
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
        }
        if not config:
            report.required_defaults.append("source.connection (provide tap-specific config)")
        return contract, report


def _redact_secrets(config: Dict[str, Any]) -> Dict[str, Any]:
    """Replace literal secrets with placeholders so the converted contract
    doesn't leak credentials. Heuristic: any key containing 'token',
    'password', 'secret', 'key' becomes ``${ENV_VAR}``.
    """
    redacted = {}
    for k, v in (config or {}).items():
        lk = k.lower()
        if any(s in lk for s in ("token", "password", "secret", "key", "credential")):
            redacted[k] = f"{{{{ env.{k.upper()} }}}}"
        else:
            redacted[k] = v
    return redacted


def _streams_from_select(extractor: Dict[str, Any]) -> List[str]:
    select = extractor.get("select") or []
    streams: List[str] = []
    for entry in select:
        # Meltano select pattern: <tap>.<entity>.<column>; we keep the entity.
        parts = entry.split(".")
        if len(parts) >= 2:
            streams.append(parts[1])
    return list(dict.fromkeys(streams))  # dedupe, preserve order
