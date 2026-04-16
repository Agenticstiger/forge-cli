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

"""
Data Mesh Manager (Entropy Data) provider.

Publishes FLUID contracts to Data Mesh Manager / Entropy Data platform.
"""

from fluid_build.providers.datamesh_manager.datamesh_manager import DataMeshManagerProvider

# Self-register with the provider registry so ``fluid providers`` lists us
# and ``discover_providers()`` picks us up automatically.
try:
    from fluid_build.providers import register_provider

    register_provider("datamesh-manager", DataMeshManagerProvider)
except Exception:
    pass  # Registry not yet available (e.g. during isolated import)

# Expose NAME + Provider for auto-discovery strategy 2 as a fallback
NAME = "datamesh-manager"
Provider = DataMeshManagerProvider

__all__ = ["DataMeshManagerProvider", "NAME", "Provider"]
