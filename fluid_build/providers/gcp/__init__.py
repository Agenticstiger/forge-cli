# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

from fluid_build.providers import register_provider

try:
    # Import the production-grade provider implementation
    from .provider import GcpProvider

    register_provider("gcp", GcpProvider)
except Exception as e:
    # Don't crash discovery; register a helpful stub
    from fluid_build.providers.base import BaseProvider, ProviderError

    _err = str(e)

    class _GcpProviderStub(BaseProvider):
        name = "gcp"

        def plan(self, contract):
            raise ProviderError(
                f"GCP provider unavailable: {_err}\n"
                f"Install dependencies: pip install google-cloud-bigquery google-cloud-storage google-cloud-pubsub"
            )

        def apply(self, actions):
            raise ProviderError(f"GCP provider unavailable: {_err}")

        def capabilities(self):
            return {
                "planning": False,
                "apply": False,
                "render": False,
                "graph": False,
                "auth": True,
            }

    register_provider("gcp", _GcpProviderStub)

__all__ = ["GcpProvider"]
