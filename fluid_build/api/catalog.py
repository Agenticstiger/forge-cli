# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Catalog auto-registration types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol


@dataclass(frozen=True)
class RegistrationResult:
    """Outcome of one catalog registration."""

    target: str  # "datahub" | "openmetadata" | "unity" | "glue" | "snowflake_horizon"
    urn: str
    succeeded: bool
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


class CatalogRegistrar(Protocol):
    """Catalog registrar Protocol. One implementation per target."""

    target: str

    def register(
        self,
        product_id: str,
        expose_id: str,
        contract: Dict[str, Any],
        classifications: Dict[str, List[str]],
    ) -> RegistrationResult: ...

    def unregister(self, product_id: str, expose_id: str) -> RegistrationResult: ...
