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

# fluid_build/providers/odps_standard/__init__.py
"""
ODPS (Open Data Product Standard) Provider - Bitol.io

This provider exports FLUID contracts to ODPS format for data marketplace
integration, particularly with Entropy Data.

ODPS Specification: https://github.com/bitol-io/open-data-product-standard
"""

from .odps import OdpsStandardProvider

__all__ = ["OdpsStandardProvider"]
