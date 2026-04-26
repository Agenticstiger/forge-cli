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

"""History/version snapshots for forged contracts and logical sidecars."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional


def history_root(root: Optional[Path] = None) -> Path:
    return (root or (Path.home() / ".fluid" / "store" / "history")).expanduser()


def archive_snapshot(
    *,
    contract: Dict[str, Any],
    logical_model: Optional[Dict[str, Any]] = None,
    root: Optional[Path] = None,
) -> Path:
    base = history_root(root)
    contract_blob = json.dumps(contract, sort_keys=True, default=str).encode("utf-8")
    contract_hash = hashlib.sha256(contract_blob).hexdigest()[:16]
    bucket = base / contract_hash
    bucket.mkdir(parents=True, exist_ok=True)
    next_index = 1
    for existing in bucket.glob("*.json"):
        try:
            next_index = max(next_index, int(existing.stem) + 1)
        except ValueError:
            continue
    payload = {
        "meta": {
            "version": next_index,
            "contract_hash": contract_hash,
            "archived_at": datetime.now(timezone.utc).isoformat(),
        },
        "contract": contract,
        "logical_model": logical_model,
    }
    path = bucket / f"{next_index}.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
    return path
