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

from ..policy.compiler import compile_policy
from ..providers.gcp.bq import plan_bigquery
from ..providers.gcp.gcs import plan_gcs
from ..providers.gcp.pubsub import plan_pubsub


def build_plan(contract: dict, provider: str):
    actions = []
    if provider == "gcp":
        actions += plan_bigquery(contract)
        actions += plan_gcs(contract)
        actions += plan_pubsub(contract)
        actions += compile_policy(contract)
    else:
        # local -> interpret exposes with file targets or SQL stubs
        from ..providers.local.ducksql import plan_sql

        actions += plan_sql(contract)
    return [a.__dict__ for a in actions]
