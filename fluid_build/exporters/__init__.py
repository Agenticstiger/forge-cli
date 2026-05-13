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

"""Native fluid exporters that consume a parsed contract and emit a target format.

Scope is intentionally narrow: only formats that hook into forge-native
workflows live here. Broad-format export (avro, protobuf, dbml, mermaid,
etc.) is deliberately out of scope — users who need those convert via the
ODCS export and a downstream tool.

- :mod:`dbt_tests` — quality block -> dbt schema.yml test entries
- :mod:`sodacl` — quality block -> Soda SodaCL YAML
"""
