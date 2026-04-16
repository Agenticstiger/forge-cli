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

from fluid_build.providers import register_provider

try:
    # Import the production-grade provider implementation
    from .provider import GcpProvider

    register_provider("gcp", GcpProvider)
except Exception as e:
    _gcp_import_error = str(e)
    # Fallback: try legacy implementation
    try:
        from .gcp import GcpProvider

        register_provider("gcp", GcpProvider)
    except Exception:
        # Don't crash discovery; register a helpful stub
        from fluid_build.providers.base import BaseProvider, ProviderError

        _err = _gcp_import_error

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
