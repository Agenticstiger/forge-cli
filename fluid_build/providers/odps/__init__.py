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

# ODPS (Open Data Product Standard, Bitol / LF-ODPI) is a data-product SPEC /
# serialization format — NOT a cloud/infrastructure provider like aws/gcp/
# snowflake/local. ``OdpsProvider`` exports a contract to the ODPS spec via
# ``render()``; its ``apply()`` is a no-op. It is therefore intentionally NOT
# registered in the provider registry and NOT advertised as a
# ``fluid_build.providers`` entry-point, so it never appears in ``fluid
# providers`` / ``fluid plugins`` / ``--provider`` as a deployment target.
#
# The class stays importable for the spec-export commands, which construct it
# DIRECTLY:  from fluid_build.providers.odps.odps import OdpsProvider
# (see cli/odps.py, cli/export_odps.py, cli/generate_standard.py). This package
# __init__ deliberately exposes no BaseProvider subclass, so the provider
# auto-discovery scan cannot re-register it.
