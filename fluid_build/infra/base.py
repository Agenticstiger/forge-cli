# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""ArtifactGenerator Protocol + registry for the managed-mode artifact emitters."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol


@dataclass
class ArtifactFile:
    """One file in a generated artifact bundle."""

    relative_path: str
    content: str
    digest: str  # sha256 hex of content
    mode: int = 0o644


@dataclass
class ArtifactBundle:
    """Outcome of an artifact generation pass."""

    target: str  # "docker" | "kubernetes" | "terraform"
    files: List[ArtifactFile] = field(default_factory=list)
    bundle_digest: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def write_to(self, root: Path) -> None:
        """Write all files under ``root`` (atomic per file)."""
        root.mkdir(parents=True, exist_ok=True)
        for f in self.files:
            target = root / f.relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f.content, encoding="utf-8")
            target.chmod(f.mode)

    @classmethod
    def of(
        cls, target: str, files: List[ArtifactFile], metadata: Optional[Dict[str, Any]] = None
    ) -> "ArtifactBundle":
        h = hashlib.sha256()
        for f in sorted(files, key=lambda x: x.relative_path):
            h.update(f.relative_path.encode("utf-8"))
            h.update(f.digest.encode("utf-8"))
        return cls(
            target=target,
            files=files,
            bundle_digest=f"sha256:{h.hexdigest()}",
            metadata=metadata or {},
        )


def make_file(relative_path: str, content: str, *, mode: int = 0o644) -> ArtifactFile:
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return ArtifactFile(
        relative_path=relative_path, content=content, digest=f"sha256:{digest}", mode=mode
    )


@dataclass
class InfraValidationResult:
    ok: bool
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


@dataclass
class InfraStatus:
    deployed: bool
    drift_detected: bool = False
    chart_version_declared: Optional[str] = None
    chart_version_live: Optional[str] = None
    notes: List[str] = field(default_factory=list)


class ArtifactGenerator(Protocol):
    """One generator per managed-mode target. Pure function (contract, env) → bundle."""

    target: str

    def generate(
        self,
        contract: Dict[str, Any],
        *,
        env: Optional[Dict[str, str]] = None,
    ) -> ArtifactBundle: ...

    def validate(self, bundle: ArtifactBundle) -> InfraValidationResult: ...

    def status(self, contract: Dict[str, Any]) -> InfraStatus: ...


# ── Registry ────────────────────────────────────────────────────────────


GENERATORS: Dict[str, ArtifactGenerator] = {}


def register_generator(target: str, generator: ArtifactGenerator) -> None:
    GENERATORS[target] = generator


def get_generator(target: str) -> Optional[ArtifactGenerator]:
    return GENERATORS.get(target)
