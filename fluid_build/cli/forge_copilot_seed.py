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

"""Pre-processor for ``fluid forge --seed-from <path>``.

Turns a user-supplied ODCS contract, Bitol ODPS product, or directory
bundle into a structural FLUID skeleton the copilot can use as ground truth.
The skeleton's schema/quality/qos are the truth — the LLM is expected to
fill in builds, execution, and governance only.

Returns a :class:`SeedResult` with:
  - ``fluid``: the imported FLUID dict (multi-expose where applicable).
  - ``shape``: ``"odcs-file" | "odps-file" | "directory" | "odcs-only-directory"``.
  - ``provenance``: an audit trail of every resolved contract source
    (paths/URLs) so downstream consumers (system prompt, ground-truth
    guard) can trace what came from where.
  - ``ground_truth_paths``: dotted paths into the FLUID dict whose values
    the LLM must not mutate (filled in by Phase 7's runtime guard).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Union

from fluid_build.providers.base import ProviderError
from fluid_build.providers.odcs import OdcsProvider
from fluid_build.providers.odcs.io import read_input
from fluid_build.providers.odps_standard import BitolOdpsProvider
from fluid_build.providers.odps_standard.resolver import ContractResolver


@dataclass
class SeedResult:
    """The outcome of a ``--seed-from`` invocation."""

    fluid: Dict[str, Any]
    shape: str
    provenance: List[Dict[str, Any]] = field(default_factory=list)
    ground_truth_paths: List[str] = field(default_factory=list)


SHAPE_ODCS_FILE = "odcs-file"
SHAPE_ODPS_FILE = "odps-file"
SHAPE_DIRECTORY = "directory"
SHAPE_ODCS_ONLY_DIRECTORY = "odcs-only-directory"


def load_seed(seed_from: Union[str, Path], *, allow_remote: bool = False) -> SeedResult:
    """Detect the input shape and run the matching importer.

    Detection order:
      1. Directory             → BitolOdpsProvider.import_directory (handles
         both bundle and ODCS-only layouts).
      2. ``*.odcs.{yaml,yml,json}`` filename → OdcsProvider.import_contract.
      3. ``*.odps.{yaml,yml,json}`` filename → BitolOdpsProvider.import_contract.
      4. Sniff the file's ``kind`` field: ``DataContract`` → ODCS,
         ``DataProduct`` → ODPS.
    """
    path = Path(seed_from)
    if not path.exists():
        raise ProviderError(f"--seed-from path not found: {path}")

    if path.is_dir():
        return _load_directory(path, allow_remote=allow_remote)

    name_lower = path.name.lower()

    if name_lower.endswith((".odcs.yaml", ".odcs.yml", ".odcs.json")):
        return _load_odcs_file(path)

    if name_lower.endswith((".odps.yaml", ".odps.yml", ".odps.json")):
        return _load_odps_file(path, allow_remote=allow_remote)

    # Last-resort: read the file and sniff `kind`
    sniffed = read_input(path)
    kind = sniffed.get("kind") if isinstance(sniffed, Mapping) else None
    if kind == "DataContract":
        return _load_odcs_file(path)
    if kind == "DataProduct":
        return _load_odps_file(path, allow_remote=allow_remote)

    raise ProviderError(
        f"--seed-from: cannot determine input type of {path}. "
        "Expected a directory, *.odcs.yaml, *.odps.yaml, or a file with "
        "kind: DataContract or kind: DataProduct."
    )


# ---- shape-specific loaders -----------------------------------------------


def _load_odcs_file(path: Path) -> SeedResult:
    provider = OdcsProvider()
    fluid = provider.import_contract(path)
    provenance = [{"shape": SHAPE_ODCS_FILE, "source": "local", "origin": str(path)}]
    ground_truth = _ground_truth_paths_for(fluid)
    return SeedResult(
        fluid=fluid,
        shape=SHAPE_ODCS_FILE,
        provenance=provenance,
        ground_truth_paths=ground_truth,
    )


def _load_odps_file(path: Path, *, allow_remote: bool) -> SeedResult:
    provider = BitolOdpsProvider()
    provider.strict_validation = False  # seed mode tolerates non-strict contracts
    resolver = ContractResolver(
        base_path=path.parent,
        allow_remote=allow_remote,
        odcs_provider=provider._odcs,
    )
    fluid = provider.import_contract(
        path,
        base_path=path.parent,
        allow_remote=allow_remote,
        resolver=resolver,
        lenient=True,
    )
    provenance = [{"shape": SHAPE_ODPS_FILE, "source": "local", "origin": str(path)}]
    provenance.extend(_resolver_provenance(resolver))
    ground_truth = _ground_truth_paths_for(fluid)
    return SeedResult(
        fluid=fluid,
        shape=SHAPE_ODPS_FILE,
        provenance=provenance,
        ground_truth_paths=ground_truth,
    )


def _load_directory(path: Path, *, allow_remote: bool) -> SeedResult:
    """Either a full bundle (ODPS + ODCS) or an ODCS-only directory."""
    provider = BitolOdpsProvider()
    provider.strict_validation = False
    fluid = provider.import_directory(path, allow_remote=allow_remote, lenient=True)
    pt = (fluid.get("metadata") or {}).get("odps_passthrough") or {}
    shape = SHAPE_ODCS_ONLY_DIRECTORY if pt.get("odcs_only_directory") else SHAPE_DIRECTORY
    provenance: List[Dict[str, Any]] = [
        {"shape": shape, "source": "local", "origin": str(path)},
    ]
    # Enumerate the ODCS files we picked up
    for child in sorted(path.glob("*.odcs.yaml")):
        provenance.append({"source": "local", "origin": str(child)})
    ground_truth = _ground_truth_paths_for(fluid)
    return SeedResult(
        fluid=fluid,
        shape=shape,
        provenance=provenance,
        ground_truth_paths=ground_truth,
    )


def _resolver_provenance(resolver: ContractResolver) -> List[Dict[str, Any]]:
    return [
        {
            "contract_id": resolved.contract_id,
            "source": resolved.source,
            "origin": resolved.origin,
        }
        for resolved in resolver._cache.values()
    ]


# ---- ground-truth path enumeration ----------------------------------------


def _ground_truth_paths_for(fluid: Mapping[str, Any]) -> List[str]:
    """Enumerate dotted paths inside ``fluid`` the LLM must not mutate.

    Currently: every ``exposes[i].contract.schema`` and ``exposes[i].qos``,
    plus ``exposes[i].contract.relationships`` and
    ``exposes[i].contract.odcs_quality``. The runtime guard (Phase 7
    finishing touches) reads this list and diffs the LLM's output against
    the seed values at each path.
    """
    paths: List[str] = []
    for i, expose in enumerate(fluid.get("exposes") or []):
        if not isinstance(expose, Mapping):
            continue
        prefix = f"exposes[{i}]"
        if isinstance(expose.get("contract"), Mapping):
            paths.append(f"{prefix}.contract.schema")
            if "relationships" in expose["contract"]:
                paths.append(f"{prefix}.contract.relationships")
            if "odcs_quality" in expose["contract"]:
                paths.append(f"{prefix}.contract.odcs_quality")
        if "qos" in expose:
            paths.append(f"{prefix}.qos")
    return paths


def resolve_at_path(fluid: Mapping[str, Any], path: str) -> Any:
    """Look up the value at a dotted/indexed path like ``exposes[0].contract.schema``."""
    import re

    cursor: Any = fluid
    for token in re.findall(r"[^.\[\]]+|\[\d+\]", path):
        if token.startswith("[") and token.endswith("]"):
            cursor = cursor[int(token[1:-1])]
        else:
            if isinstance(cursor, Mapping):
                cursor = cursor.get(token)
            else:
                cursor = getattr(cursor, token, None)
        if cursor is None:
            return None
    return cursor


def diff_against_seed(seed: SeedResult, candidate: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Compare ground-truth values in ``candidate`` against the seed.

    Returns a list of ``{"path", "seed", "candidate"}`` entries for paths
    where the candidate's value differs from the seed. The runtime guard
    raises (or triggers a repair loop) when this list is non-empty.
    """
    mismatches: List[Dict[str, Any]] = []
    for path in seed.ground_truth_paths:
        seed_value = resolve_at_path(seed.fluid, path)
        cand_value = resolve_at_path(candidate, path)
        if seed_value != cand_value:
            mismatches.append({"path": path, "seed": seed_value, "candidate": cand_value})
    return mismatches
