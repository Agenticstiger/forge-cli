# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``env://NAME``: a variable of the process environment."""

from __future__ import annotations

import os

from fluid_build._errors import doc_url

from .errors import SecretResolveError
from .ref import SecretRef
from .registry import Existence
from .run import RunState


class EnvResolver:
    """Reads ``os.environ``. Which variables may go where is the policy's call."""

    scheme = "env"

    def resolve(self, ref: SecretRef, run: RunState) -> str:
        value = os.environ.get(ref.locator)
        if value is None:
            raise SecretResolveError(
                what=f"{ref.canonical} did not resolve",
                why=f"environment variable {ref.locator} is not set.",
                fix=f"Export {ref.locator} in the shell or CI job that runs this stage.",
                doc=doc_url("secrets"),
                extras={"reason": "not_found"},
            )
        return value

    def exists(self, ref: SecretRef, run: RunState) -> Existence:
        if ref.locator in os.environ:
            return Existence(True, f"{ref.locator} is set")
        return Existence(False, f"{ref.locator} is not set")
