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

"""Airbyte workspace → FLUID contract importer.

Pulls source/destination/connection from a live Airbyte API endpoint and
emits a Bronze contract per connection.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from .registry import Importer, ImportReport


@dataclass
class AirbyteImporter(Importer):
    name: str = "airbyte"
    server_url: str = "https://airbyte.test"
    api_token: Optional[str] = None
    timeout_seconds: int = 30

    def can_import(self, source: str) -> bool:
        # ``source`` is the workspace id; we always claim we can if the
        # caller routed to us via the registry.
        return True

    def import_to_contract(
        self, source: str, *, options: Optional[Dict[str, Any]] = None
    ) -> tuple[Dict[str, Any], ImportReport]:
        from fluid_build.build_runners.airbyte.runner import AirbyteRestClient

        opts = options or {}
        url = opts.get("server_url", self.server_url)
        token = opts.get("api_token", self.api_token)
        client = AirbyteRestClient(url, api_token=token, timeout_seconds=self.timeout_seconds)
        report = ImportReport()
        try:
            sources = client.list_sources(workspace_id=source)
            if not sources:
                report.unsupported.append(f"workspace {source} has no sources")
                return {}, report
            primary = sources[0]
            report.mapped_one_to_one.append("source")
            kind = primary.get("sourceName", "airbyte_source")
            connection_config = primary.get("connectionConfiguration") or {}
            product_id = f"bronze.airbyte_{source.replace('-', '_')}"
            contract = {
                "fluidVersion": "0.7.3",
                "kind": "DataProduct",
                "id": product_id,
                "name": f"Imported from Airbyte: {kind}",
                "domain": "imported",
                "description": f"Auto-converted from Airbyte workspace {source}",
                "metadata": {
                    "layer": "Bronze",
                    "productType": "SDP",
                    "owner": {"team": "imported", "email": "import@forge.local"},
                },
                "builds": [
                    {
                        "id": "ingest",
                        "pattern": "acquisition",
                        "engine": "airbyte",
                        "capabilities": ["full_refresh"],
                        "properties": {
                            "source": {
                                "kind": kind.lower(),
                                "connection": _redact_secrets(connection_config),
                                "mode": "full_refresh",
                                "streams": [],
                            },
                            "sink": {"format": "parquet"},
                            "airbyte": {
                                "deployment": {
                                    "mode": "bring-your-own",
                                    "server_url": url,
                                },
                                "workspace_id": source,
                            },
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
            if not connection_config:
                report.required_defaults.append("source.connection (provide credentials)")
            return contract, report
        finally:
            client.close()


def _redact_secrets(config: Dict[str, Any]) -> Dict[str, Any]:
    redacted = {}
    for k, v in (config or {}).items():
        lk = k.lower()
        if any(s in lk for s in ("token", "password", "secret", "key", "credential")):
            redacted[k] = f"{{{{ env.{k.upper()} }}}}"
        else:
            redacted[k] = v
    return redacted
