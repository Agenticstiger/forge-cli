# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Native fluid exporters that consume a parsed contract and emit a target format.

Scope is intentionally narrow: only formats that hook into forge-native
workflows live here. Broad-format export (avro, protobuf, dbml, mermaid,
etc.) is deliberately out of scope — users who need those convert via the
ODCS export and a downstream tool.

- :mod:`dbt_tests` — quality block -> dbt schema.yml test entries
- :mod:`sodacl` — quality block -> Soda SodaCL YAML
"""
