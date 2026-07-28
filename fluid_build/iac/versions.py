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

"""OpenTofu engine + provider version pins — the single source of truth.

Pins use the pessimistic ``~>`` constraint so patch/minor updates flow
while majors are held. Re-verify against the OpenTofu registry before a
release; scattered version literals elsewhere are a regression.
"""

from __future__ import annotations

from typing import Dict

#: Minimum OpenTofu version. 1.6 is OpenTofu's first GA release and the
#: floor for config-driven ``import {}`` blocks.
REQUIRED_TOFU_VERSION = ">= 1.6"

#: Provider local-name -> ``{source, version}``. ``source`` addresses are
#: OpenTofu-registry namespaces; the Snowflake provider moved out of the
#: ``Snowflake-Labs`` org to the official ``snowflakedb`` org at v2.
PROVIDER_PINS: Dict[str, Dict[str, str]] = {
    "google": {"source": "hashicorp/google", "version": "~> 6.0"},
    "aws": {"source": "hashicorp/aws", "version": "~> 5.0"},
    "snowflake": {"source": "snowflakedb/snowflake", "version": "~> 2.0"},
    # Confluent Cloud — Tableflow (managed Kafka→Iceberg) + Glue catalog /
    # provider integration. The managed control plane owns compaction +
    # snapshot-expiry (RFC-streaming-extension §15).
    "confluent": {"source": "confluentinc/confluent", "version": "~> 2.0"},
    # `archive` zips Lambda source inline (`data.archive_file`) so the AWS
    # emitter ships function code without a separate packaging step.
    "archive": {"source": "hashicorp/archive", "version": "~> 2.0"},
    # `null` provides ``null_resource`` — the documented community bridge for
    # operations that have no first-party Terraform resource in
    # ``hashicorp/aws``. The AWS plugin uses it to run the
    # ``CREATE EXTERNAL SCHEMA ... FROM DATA CATALOG`` SQL via the
    # ``redshift-data`` API (no ``aws_redshiftserverless_external_schema``
    # exists today; filed upstream — see ``AUTOGEN_SPIKE.md``).
    "null": {"source": "hashicorp/null", "version": "~> 3.0"},
}


def required_providers(*names: str) -> Dict[str, Dict[str, str]]:
    """Return the OpenTofu ``required_providers`` map for the given
    provider local-names (e.g. ``required_providers("google")``)."""
    return {name: dict(PROVIDER_PINS[name]) for name in names}
