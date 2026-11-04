# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

from fluid_build.providers import register_provider

from .local import LocalProvider  # must have class LocalProvider(BaseProvider) with name="local"

register_provider("local", LocalProvider)
__all__ = ["LocalProvider"]
