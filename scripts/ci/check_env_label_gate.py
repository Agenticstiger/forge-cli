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

"""Assert every environment-bound job in a workflow still carries its label gate.

`integration-emulated-heavy.yml` reaches a GitHub environment that holds a secret.
The environment scopes WHICH secret; what gates un-reviewed pull-request code from
reaching it is the `ci:integration-emulated` label, re-earned per push by
`integration-label-guard.yml`. Since the environment no longer carries a
required-reviewer rule, that label is load-bearing and has to be enforced rather
than assumed.

This replaces a whole-file `grep` for the label expression. A grep proves the
string appears SOMEWHERE in the file, not that it is attached to an `if:`, and not
that it is attached to EVERY job. A third job added later with
`environment: integration-emulated` and no label condition would have passed that
grep while running on every pull request with the secret in scope.

    python scripts/ci/check_env_label_gate.py \
      --workflow .github/workflows/integration-emulated-heavy.yml \
      --environment integration-emulated \
      --label ci:integration-emulated \
      --guard .github/workflows/integration-label-guard.yml
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def triggers(workflow: dict) -> dict:
    """Return the workflow's `on:` block.

    YAML 1.1 resolves the bare key ``on`` to the boolean ``True``, so a plain
    ``workflow["on"]`` lookup silently finds nothing and every check downstream
    passes vacuously.
    """
    for key in (True, "on"):
        if key in workflow:
            value = workflow[key]
            return value if isinstance(value, dict) else {str(value): None}
    return {}


def ungated_jobs(workflow: dict, environment: str, label: str) -> list[str]:
    """Names of jobs bound to ``environment`` whose ``if:`` omits ``label``."""
    offenders = []
    for name, job in (workflow.get("jobs") or {}).items():
        if not isinstance(job, dict):
            continue
        # `environment:` accepts a bare string or a {name, url} mapping.
        env = job.get("environment")
        env_name = env.get("name") if isinstance(env, dict) else env
        if env_name != environment:
            continue
        if label not in str(job.get("if", "")):
            offenders.append(name)
    return offenders


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workflow", type=Path, required=True)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--guard", type=Path, required=True)
    args = parser.parse_args(argv)

    import yaml

    if not args.workflow.exists():
        print(f"No {args.workflow} — nothing to check.")
        return 0

    workflow = yaml.safe_load(args.workflow.read_text()) or {}
    on = triggers(workflow)

    if "pull_request" not in on:
        print(f"{args.workflow} has no pull_request trigger — label gate not required.")
        return 0

    offenders = ungated_jobs(workflow, args.environment, args.label)
    if offenders:
        print(f"::error::{args.workflow} runs on pull_request, and these jobs bind")
        print(f"::error::environment '{args.environment}' without requiring the")
        print(f"::error::'{args.label}' label: {', '.join(sorted(offenders))}.")
        print("::error::That would run un-vouched PR code with the environment's")
        print("::error::secrets in scope. Add the label condition to each job's if:.")
        return 1

    print(f"{args.workflow}: every '{args.environment}' job gates on '{args.label}' OK.")

    # The label only means anything because it is stripped on each new push.
    if not args.guard.exists():
        print(f"::error::{args.guard} is missing — the '{args.label}' label would")
        print("::error::survive new pushes, so one vouch would cover every later commit.")
        return 1

    print(f"{args.guard} present OK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
