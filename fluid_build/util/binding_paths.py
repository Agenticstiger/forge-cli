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
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Union

PathLike = Union[str, Path]


def is_remote_uri(path: str) -> bool:
    """True when ``path`` carries a URI scheme (``s3://``, ``gs://`` ...)."""
    return "://" in str(path)


def resolve_binding_path(path: Any, anchor_dir: Optional[PathLike]) -> Any:
    """Anchor a relative ``location.path`` at ``anchor_dir``.

    Returns ``path`` unchanged when it is empty or not a string, when it is a
    remote URI or already absolute, or when no anchor is known (``None`` keeps
    the historical working-directory semantics for callers that have no
    source contract to anchor to). Otherwise returns ``str(anchor_dir / path)``.
    """
    if not path or not isinstance(path, str) or anchor_dir is None:
        return path
    if is_remote_uri(path) or Path(path).is_absolute():
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


__all__ = ["anchor_binding_paths", "is_remote_uri", "resolve_binding_path"]
