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

"""Handler for ``fluid init --discover <uri>``.

Connects to the supplied URI, enumerates streams via
:mod:`fluid_build.cli.discover`, and emits one acquisition contract per
stream into the current directory.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import urlparse

import yaml

from fluid_build.cli._errors import (
    ConnectivityProbeError,
    SchemaValidationError,
)
from fluid_build.cli.console import cprint
from fluid_build.cli.discover import get_discoverer
from fluid_build.cli.discover.emitter import emit_contract

# URI scheme → (engine, source.kind) tuple. The runner contract requires
# ``engine`` (which runner module to dispatch to) plus ``source.kind``
# (used for connector-class selection downstream).
_SCHEME_DEFAULTS = {
    "postgres": ("duckdb", "postgres"),
    "postgresql": ("duckdb", "postgres"),
    "mysql": ("duckdb", "mysql"),
    "mariadb": ("duckdb", "mysql"),
    "sqlite": ("duckdb", "sqlite"),
    "file": ("duckdb", "filesystem"),
    "https": ("duckdb", "filesystem"),
    "http": ("duckdb", "filesystem"),
    "s3": ("duckdb", "filesystem"),
    "gs": ("duckdb", "filesystem"),
    "gcs": ("duckdb", "filesystem"),
}


def run_discover(args, logger: logging.Logger, *, uri: str) -> int:
    discoverer = get_discoverer(uri)
    if discoverer is None:
        raise SchemaValidationError(
            what=f"unsupported source scheme in --discover URI: {uri}",
            why=(
                "The URI's scheme is not registered. Supported: "
                + ", ".join(sorted(_SCHEME_DEFAULTS.keys()))
                + "."
            ),
            fix="Use postgres://, mysql://, sqlite://, file://, s3://, https://, or http://.",
            doc="https://forge.fluid.dev/ref/discover#schemes",
            extras={"uri": uri, "supported": sorted(_SCHEME_DEFAULTS.keys())},
        )

    cprint(f"🔍 Discovering streams at {uri}…")
    try:
        streams = discoverer.discover(uri)
    except Exception as exc:  # noqa: BLE001
        raise ConnectivityProbeError.for_target(
            target=uri,
            reason=str(exc),
        ) from exc

    if not streams:
        cprint("(no streams found at the URI)")
        return 0

    parsed = urlparse(uri)
    scheme_base = (parsed.scheme.split("+", 1)[0]) or "file"
    engine, source_kind = _SCHEME_DEFAULTS.get(scheme_base, ("duckdb", scheme_base))
    connection = _connection_from_uri(parsed)

    target_dir = Path(getattr(args, "target_dir", None) or ".").resolve()
    target_dir.mkdir(parents=True, exist_ok=True)

    project = getattr(args, "name", None) or _slug(parsed.path.strip("/")) or scheme_base
    written: List[Path] = []

    for stream in streams:
        contract_id = f"bronze.{project}.{_slug(stream.name)}"
        contract = emit_contract(
            product_id=contract_id,
            name=f"{project} / {stream.name}",
            domain=getattr(args, "domain", None) or "data",
            owner_team=getattr(args, "owner_team", None) or "data-platform",
            owner_email=getattr(args, "owner_email", None) or "data-platform@example.com",
            engine=engine,
            source_kind=source_kind,
            connection=connection,
            streams=[stream],
        )
        out_path = target_dir / f"contract.{_slug(stream.name)}.fluid.yaml"
        out_path.write_text(yaml.safe_dump(contract, sort_keys=False), encoding="utf-8")
        written.append(out_path)
        cprint(f"  ✓ {out_path.relative_to(target_dir)}  ({len(stream.columns)} cols)")

    logger.info("discover.emitted count=%d uri=%s", len(written), uri)
    cprint(f"\n✓ Emitted {len(written)} contract(s). Next: `fluid validate <file>`.")
    return 0


def _connection_from_uri(parsed) -> Dict[str, Any]:
    if parsed.scheme.split("+", 1)[0] in ("postgres", "postgresql", "mysql", "mariadb"):
        return {
            "host": parsed.hostname or "localhost",
            "port": parsed.port or (5432 if "postgres" in parsed.scheme else 3306),
            "database": (parsed.path or "/").lstrip("/") or None,
            "user": parsed.username,
            "password": parsed.password,
        }
    if parsed.scheme == "sqlite":
        return {"path": parsed.path}
    return {"uri": parsed.geturl()}


def _slug(s: str) -> str:
    out = []
    for ch in (s or "").lower():
        if ch.isalnum():
            out.append(ch)
        elif ch in (".", "_", "-"):
            out.append("_")
        else:
            out.append("_")
    slug = "".join(out).strip("_")
    return slug or "default"
