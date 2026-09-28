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

"""The one anchoring rule for a relative ``binding.location.path``.

A relative ``location.path`` in a contract is relative to the directory of
the SOURCE contract file, never to the process working directory. That is
the rule Compose applies to the paths in a compose file ("relative to the
location of the Compose file"), and it is the only rule under which the
stage that WRITES a local file and the stage that READS it back agree
regardless of where each was launched from.

Before this module there were three answers in the tree: the acquisition
runner anchored at the contract directory, the local provider (and so every
embedded-SQL build) wrote relative to the working directory, and ``fluid
verify`` read relative to the working directory. A pipeline that built from
``contracts/x/contract.fluid.yaml`` and verified from the repo root therefore
looked for ``out/x.parquet`` in a directory the build never wrote to.

Remote URIs (``s3://``, ``gs://``, ``azure://``, ``file://`` ...) and
absolute paths are returned unchanged. Stdlib only: imported by the local
provider, the build runners and ``fluid verify``.
"""

from __future__ import annotations

import copy
import os
import re
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple, Union

PathLike = Union[str, Path]

#: ``{{ env.NAME }}`` in a contract string. The build runners resolve these
#: before they write (``build_runners/base.py`` imports this pattern), so a
#: path is resolved the same way here before it is anchored: the stage that
#: writes a local file and the stages that read it back (``fluid verify``,
#: ``fluid diff``, the local provider) must name the same file.
ENV_PLACEHOLDER_RE = re.compile(r"\{\{\s*env\.([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


def resolve_env_placeholders_in_path(path: str) -> str:
    """``path`` with each ``{{ env.NAME }}`` replaced by the variable's value.

    A variable that is not set becomes the empty string, exactly as the build
    runner resolves it when it writes, so reader and writer still agree.
    """
    return ENV_PLACEHOLDER_RE.sub(lambda m: os.getenv(m.group(1), ""), path)


def is_remote_uri(path: str) -> bool:
    """True when ``path`` carries a URI scheme (``s3://``, ``gs://`` ...)."""
    return "://" in str(path)


def resolve_binding_path(path: Any, anchor_dir: Optional[PathLike]) -> Any:
    """Anchor a relative ``location.path`` at ``anchor_dir``.

    ``{{ env.NAME }}`` placeholders are resolved first, as the build runner
    resolves them before it writes. Then the path is returned as it is when it
    is empty or not a string, when it is a remote URI or already absolute, or
    when no anchor is known (``None`` keeps the historical working-directory
    semantics for callers that have no source contract to anchor to).
    Otherwise returns ``str(anchor_dir / path)``.
    """
    if not path or not isinstance(path, str):
        return path
    path = resolve_env_placeholders_in_path(path)
    if anchor_dir is None or is_remote_uri(path) or Path(path).is_absolute():
        return path
    return str(Path(anchor_dir) / path)


def anchor_binding_paths(
    contract: Mapping[str, Any], anchor_dir: Optional[PathLike]
) -> Dict[str, Any]:
    """Return a copy of ``contract`` whose relative expose paths are anchored.

    Rewrites ``exposes[].binding.location.path`` (and the legacy
    ``exposes[].location.path``) through :func:`resolve_binding_path`. The
    input is never mutated; with ``anchor_dir=None`` the copy is identical to
    the input.
    """
    anchored: Dict[str, Any] = copy.deepcopy(dict(contract))
    if anchor_dir is None:
        return anchored
    for expose in anchored.get("exposes") or []:
        if not isinstance(expose, dict):
            continue
        binding = expose.get("binding")
        locations = []
        if isinstance(binding, dict) and isinstance(binding.get("location"), dict):
            locations.append(binding["location"])
        if isinstance(expose.get("location"), dict):
            locations.append(expose["location"])
        for loc in locations:
            if "path" in loc:
                loc["path"] = resolve_binding_path(loc["path"], anchor_dir)
    return anchored


_PARQUET_ALIASES = frozenset({"parquet", "pq"})
_PARQUET_SUFFIXES = frozenset({".parquet", ".pq"})


def local_provider_landing(path: str, declared_format: Any) -> Tuple[str, str]:
    """``(path, format)`` of the file the local provider writes for a local binding.

    The one statement of the local provider's rule, used by the writer
    (``LocalProvider._derive_actions_from_contract``, which every embedded-SQL
    build on DuckDB lands through) and by the reader that reads that file back
    as an upstream (``build_runners._embedded_sql_io``). They used to carry
    their own copies and disagreed: ``format: parquet`` at ``out/orders`` was
    written to ``out/orders.parquet`` and read at ``out/orders``, and
    ``format: json`` was written as CSV and read as JSON.

    * ``parquet`` (or ``pq``) is written as parquet, and a path without a
      ``.parquet`` / ``.pq`` suffix gets ``.parquet`` in place of its suffix.
    * Anything else, an unset format included, is written as CSV at the path
      as declared, whatever its suffix.

    ``path`` is already anchored (``resolve_binding_path``); this decides only
    the file name and the format.
    """
    fmt = str(declared_format or "").strip().lower()
    if fmt not in _PARQUET_ALIASES:
        return path, "csv"
    if Path(path).suffix.lower() in _PARQUET_SUFFIXES:
        return path, "parquet"
    return str(Path(path).with_suffix(".parquet")), "parquet"


__all__ = [
    "ENV_PLACEHOLDER_RE",
    "anchor_binding_paths",
    "is_remote_uri",
    "local_provider_landing",
    "resolve_binding_path",
    "resolve_env_placeholders_in_path",
]
