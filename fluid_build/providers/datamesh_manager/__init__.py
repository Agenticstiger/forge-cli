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

# Data Mesh Manager is a catalog, not a deployment provider: it is reached by
# ``fluid publish --target datamesh-manager`` and ``fluid datamesh-manager``,
# which import the class directly. Opt out of every passive registration
# (including the single-BaseProvider-subclass fallback) so it stays out of
# ``fluid providers``.
__fluid_no_autoregister__ = True

__all__ = ["DataMeshManagerProvider"]
