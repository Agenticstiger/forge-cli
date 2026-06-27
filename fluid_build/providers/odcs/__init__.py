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

# fluid_build/providers/odcs/__init__.py
"""
ODCS (Open Data Contract Standard) Provider - Bitol.io

This provider handles bidirectional conversion between FLUID and ODCS formats.
ODCS contracts define the data structure, quality, and SLA requirements.

ODCS Specification: https://github.com/bitol-io/open-data-contract-standard
"""

from .odcs import OdcsProvider

# ODCS (Open Data Contract Standard, Bitol) is a data-contract SPEC / export
# format — NOT a cloud/infrastructure provider. ``OdcsProvider.apply()`` raises
# ("does not support apply()"); it only renders/imports the ODCS spec. Like ODPS,
# it is therefore intentionally NOT registered in the provider registry and never
# appears in ``fluid providers`` / ``fluid plugins`` / ``--provider``.
#
# The class stays importable for the spec commands, which construct it directly
# (``from fluid_build.providers.odcs import OdcsProvider`` — see cli/odcs.py,
# cli/generate_standard.py, api/catalog_publication.py, datamesh_manager). The
# opt-out below stops the provider auto-discovery single-subclass fallback from
# re-registering it on the strength of that import.
__fluid_no_autoregister__ = True

__all__ = ["OdcsProvider"]
