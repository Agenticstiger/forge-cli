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

from __future__ import annotations

import argparse
import logging
import os

from ._common import CLIError
from ._io import atomic_write
from ._logging import info

COMMAND = "scaffold-ci"


def register(subparsers: argparse._SubParsersAction):
    p = subparsers.add_parser(COMMAND, help="Generate CI pipeline (GitLab/GitHub)")
    p.add_argument("contract", help="contract.fluid.yaml")
    p.add_argument(
        "--system", choices=["gitlab", "github", "jenkins"], default="gitlab", help="CI system"
    )
    p.add_argument("--out", default=".gitlab-ci.yml", help="Output path")
    p.set_defaults(cmd=COMMAND, func=run)


GITLAB = """stages:
  - validate
  - generate
  - plan
  - test
  - apply
validate:
  stage: validate
  script:
    - python -m fluid_build.cli validate $CONTRACT
generate:
  stage: generate
  script:
    - python -m fluid_build.cli generate transformation
    - python -m fluid_build.cli generate schedule
plan:
  stage: plan
  script:
    - python -m fluid_build.cli --provider $PROVIDER plan $CONTRACT --out runtime/plan.json
  artifacts:
    paths: [runtime/plan.json]
tests:
  stage: test
  script:
    - python -m fluid_build.cli contract-tests $CONTRACT
apply:
  stage: apply
  when: manual
  script:
    - python -m fluid_build.cli --provider $PROVIDER apply runtime/plan.json --yes
"""

GITHUB = """name: FLUID
on: [push]
permissions: {}  # Least privilege — grant per-job only
jobs:
  validate:
    runs-on: ubuntu-latest
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5  # v4.3.1
      - run: python -m fluid_build.cli validate ${{ env.CONTRACT }}
  generate:
    needs: [validate]
    runs-on: ubuntu-latest
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5  # v4.3.1
      - run: python -m fluid_build.cli generate transformation
      - run: python -m fluid_build.cli generate schedule
  plan:
    needs: [generate]
    runs-on: ubuntu-latest
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5  # v4.3.1
      - run: python -m fluid_build.cli --provider ${{ env.PROVIDER }} plan ${{ env.CONTRACT }} --out runtime/plan.json
  apply:
    needs: [plan]
    runs-on: ubuntu-latest
    permissions:
      contents: read
    if: github.event_name == 'workflow_dispatch'
    steps:
      - uses: actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5  # v4.3.1
      - run: python -m fluid_build.cli --provider ${{ env.PROVIDER }} apply runtime/plan.json --yes
"""

JENKINS = """\
// FLUID CI/CD Pipeline — Jenkinsfile
pipeline {
    agent { label 'fluid' }

    environment {
        CONTRACT = 'contract.fluid.yaml'
        PROVIDER = 'default'
        // Bind credentials from Jenkins credential store.
        // Configure 'fluid-provider-credentials' in Jenkins > Manage Credentials.
        PROVIDER_CREDS = credentials('fluid-provider-credentials')
    }

    stages {
        stage('Validate') {
            steps {
                sh 'python -m fluid_build.cli validate $CONTRACT'
            }
        }
        stage('Generate') {
            parallel {
                stage('Transformations') {
                    steps {
                        sh 'python -m fluid_build.cli generate transformation'
                    }
                }
                stage('Schedules') {
                    steps {
                        sh 'python -m fluid_build.cli generate schedule'
                    }
                }
            }
        }
        stage('Plan') {
            steps {
                sh 'python -m fluid_build.cli --provider $PROVIDER plan $CONTRACT --out runtime/plan.json'
            }
            post {
                success {
                    archiveArtifacts artifacts: 'runtime/plan.json', fingerprint: true
                }
            }
        }
        stage('Test') {
            steps {
                sh 'python -m fluid_build.cli contract-tests $CONTRACT'
            }
        }
        stage('Apply') {
            when { branch 'main' }
            input {
                message 'Deploy?'
                ok 'Apply'
            }
            steps {
                sh 'python -m fluid_build.cli --provider $PROVIDER apply runtime/plan.json --yes'
            }
        }
    }

    post {
        always {
            cleanWs()
        }
    }
}
"""

_TEMPLATES = {"gitlab": GITLAB, "github": GITHUB, "jenkins": JENKINS}
_DEFAULT_PATHS = {
    "gitlab": ".gitlab-ci.yml",
    "github": ".github/workflows/fluid.yml",
    "jenkins": "Jenkinsfile",
}


def run(args, logger: logging.Logger) -> int:
    try:
        content = _TEMPLATES[args.system]
        out = args.out
        os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
        atomic_write(out, content)
        info(logger, "scaffold_ci_written", out=out, system=args.system)
        return 0
    except Exception as e:
        raise CLIError(1, "scaffold_ci_failed", {"error": str(e)})
