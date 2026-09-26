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

"""Read the statements of an emitted Lake Formation bucket policy, whatever its mode.

``binding.governance.lakeFormation.bucketPolicy`` decides the shape:

* ``all-grantees`` puts a static JSON document on the ``aws_s3_bucket_policy``.
* ``cross-account`` (the default) points the resource at a
  ``data.aws_iam_policy_document`` whose ``dynamic "statement"`` blocks
  iterate over the grantees in other accounts, decided at plan time. The
  helper expands those blocks for EVERY grantee, i.e. the candidate
  statements before the plan-time filter runs, which is the set the
  prefix-scoping assertions must hold for.

Both come back in the static document's form and order, so a test can assert
one property across both modes.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Mapping

_DOC_REF = re.compile(r"^\$\{data\.aws_iam_policy_document\.([A-Za-z0-9_]+)\.json\}$")
_ARN_REF = re.compile(r"data\.aws_arn\.([A-Za-z0-9_]+)")


def policy_statements(
    policy: Mapping[str, Any], data: Mapping[str, Any] | None = None
) -> List[Dict[str, Any]]:
    """The statements of one ``aws_s3_bucket_policy`` body (see the module docstring)."""
    match = _DOC_REF.match(str(policy["policy"]))
    if match is None:
        return list(json.loads(policy["policy"])["Statement"])
    assert data is not None, "a cross-account policy needs emit_data() to be read"
    document = data["aws_iam_policy_document"][match.group(1)]
    blocks = document["dynamic"]["statement"]
    grantee_keys = _ARN_REF.findall(blocks[0]["for_each"])
    arns = [data["aws_arn"][key]["arn"] for key in grantee_keys]
    statements: List[Dict[str, Any]] = []
    for index, arn in enumerate(arns):
        for block in blocks:
            content = block["content"]
            resources = list(content["resources"])
            statement: Dict[str, Any] = {
                "Sid": content["sid"].replace("${statement.key}", str(index)),
                "Effect": content["effect"],
                "Principal": {content["principals"]["type"]: arn},
                "Action": list(content["actions"]),
                "Resource": resources[0] if len(resources) == 1 else resources,
            }
            condition = content.get("condition")
            if condition:
                statement["Condition"] = {
                    condition["test"]: {condition["variable"]: list(condition["values"])}
                }
            statements.append(statement)
    return statements


def only_policy(resources: Mapping[str, Any]) -> Dict[str, Any]:
    """The single ``aws_s3_bucket_policy`` body a one-bucket contract emits."""
    policies = resources.get("aws_s3_bucket_policy", {})
    assert len(policies) == 1, f"expected one bucket policy, got {sorted(policies)}"
    return dict(next(iter(policies.values())))
