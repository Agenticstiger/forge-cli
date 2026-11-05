# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

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
