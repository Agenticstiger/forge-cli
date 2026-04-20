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


GITLAB = """# FLUID CI/CD Pipeline — GitLab CI
#
# Credential model (provider-agnostic):
#   fluid's credential resolver reads provider auth from the runner
#   environment. Each provider expects its own env vars (Snowflake:
#   SNOWFLAKE_*; GCP: GOOGLE_APPLICATION_CREDENTIALS; AWS: AWS_*;
#   Azure: AZURE_*; see `fluid_build.credentials.resolver`).
#
#   Surface them as GitLab CI/CD variables (Settings → CI/CD →
#   Variables). GitLab auto-injects them as env for every job — no
#   explicit binding needed in this file. Mark sensitive values as
#   "Masked" and "Protected" so they only reach protected branches.
#
# Optional job-level knobs you may want as CI/CD variables too:
#   PROVIDER             — default provider (e.g. snowflake, gcp, aws)
#   BUILD_ID             — builds[].id for dbt hybrid-reference projects
#   AIRFLOW_DAGS_DEST    — rsync target for airflow DAG deployment (optional)
#   CATALOG              — catalog name for `fluid publish` (default: datamesh-manager)
image: python:3.12-slim
before_script:
  - pip install --quiet data-product-forge
variables:
  CONTRACT: contract.fluid.yaml
  PROVIDER: default
  BUILD_ID: ""                 # Set to builds[].id for dbt hybrid-reference projects.
  CATALOG: datamesh-manager

stages:
  - validate
  - generate
  - plan
  - test
  - apply
  - deploy
  - publish

validate:
  stage: validate
  script:
    - fluid validate $CONTRACT
generate:
  stage: generate
  script:
    - fluid generate speed-transformation
    - fluid generate schedule
  artifacts:
    paths: [dbt_project/, dags/]
plan:
  stage: plan
  script:
    - fluid --provider $PROVIDER plan $CONTRACT --out runtime/plan.json
  artifacts:
    paths: [runtime/plan.json]
tests:
  stage: test
  script:
    - fluid contract-tests $CONTRACT
apply:
  stage: apply
  when: manual
  script:
    # --build is required for dbt hybrid-reference builds; harmless otherwise.
    - if [ -n "$BUILD_ID" ]; then fluid apply $CONTRACT --build $BUILD_ID --yes; else fluid apply runtime/plan.json --yes; fi
airflow_sync:
  stage: deploy
  rules:
    - if: '$CI_COMMIT_REF_NAME == "main" && $AIRFLOW_DAGS_DEST'
  script:
    - if [ -d dags/ ]; then rsync -av --delete dags/ "$AIRFLOW_DAGS_DEST"/ ; fi
publish:
  stage: publish
  rules:
    - if: '$CI_COMMIT_REF_NAME == "main" && $DMM_API_URL'
  script:
    - fluid publish $CONTRACT --catalog $CATALOG
"""

GITHUB = """# FLUID CI/CD Pipeline — GitHub Actions
#
# Credential model (provider-agnostic):
#   fluid's credential resolver reads provider auth from the runner
#   environment. Each provider expects its own env vars (Snowflake:
#   SNOWFLAKE_*; GCP: GOOGLE_APPLICATION_CREDENTIALS; AWS: AWS_*;
#   Azure: AZURE_*; see `fluid_build.credentials.resolver`).
#
#   Surface them as GitHub Actions secrets: Settings → Secrets and
#   variables → Actions. Then map them into the job env at the job
#   level (repeat per job that runs a `fluid` command):
#
#       env:
#         SNOWFLAKE_ACCOUNT: ${{ secrets.SNOWFLAKE_ACCOUNT }}
#         SNOWFLAKE_USER: ${{ secrets.SNOWFLAKE_USER }}
#         # …etc for your provider
#         DMM_API_KEY: ${{ secrets.DMM_API_KEY }}    # if using `fluid publish`
#         DMM_API_URL: ${{ secrets.DMM_API_URL }}
#         GEMINI_API_KEY: ${{ secrets.GEMINI_API_KEY }}  # if running `fluid forge` in CI
#
# Required repository variables (Settings → Secrets and variables → Actions → Variables):
#   PROVIDER             — default provider (e.g. snowflake, gcp, aws)
#   BUILD_ID             — builds[].id for dbt hybrid-reference projects
#   AIRFLOW_DAGS_DEST    — rsync target for airflow DAG deployment (optional)
#   CATALOG              — catalog name for `fluid publish` (default: datamesh-manager)
name: FLUID
on: [push, workflow_dispatch]
env:
  CONTRACT: contract.fluid.yaml
permissions: {}  # Least privilege — grant per-job only
jobs:
  validate:
    runs-on: ubuntu-latest
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5  # v4.3.1
      - uses: actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065  # v5.6.0
        with: { python-version: '3.12' }
      - run: pip install --quiet data-product-forge
      - run: fluid validate ${{ env.CONTRACT }}
  generate:
    needs: [validate]
    runs-on: ubuntu-latest
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5  # v4.3.1
      - uses: actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065  # v5.6.0
        with: { python-version: '3.12' }
      - run: pip install --quiet data-product-forge
      - run: fluid generate speed-transformation
      - run: fluid generate schedule
      - uses: actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02  # v4.6.2
        with: { name: fluid-artifacts, path: |
            dbt_project/
            dags/
        }
  plan:
    needs: [generate]
    runs-on: ubuntu-latest
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5  # v4.3.1
      - uses: actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065  # v5.6.0
        with: { python-version: '3.12' }
      - run: pip install --quiet data-product-forge
      - run: fluid --provider ${{ vars.PROVIDER || 'default' }} plan ${{ env.CONTRACT }} --out runtime/plan.json
      - uses: actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02  # v4.6.2
        with: { name: fluid-plan, path: runtime/plan.json }
  apply:
    needs: [plan]
    runs-on: ubuntu-latest
    permissions:
      contents: read
    if: github.event_name == 'workflow_dispatch'
    steps:
      - uses: actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5  # v4.3.1
      - uses: actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065  # v5.6.0
        with: { python-version: '3.12' }
      - run: pip install --quiet data-product-forge
      - name: Apply contract
        run: |
          if [ -n "${{ vars.BUILD_ID }}" ]; then
            fluid apply ${{ env.CONTRACT }} --build ${{ vars.BUILD_ID }} --yes
          else
            fluid apply runtime/plan.json --yes
          fi
  airflow_sync:
    needs: [apply]
    runs-on: ubuntu-latest
    permissions:
      contents: read
    if: github.ref == 'refs/heads/main' && vars.AIRFLOW_DAGS_DEST != ''
    steps:
      - uses: actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5  # v4.3.1
      - run: |
          if [ -d dags/ ]; then rsync -av --delete dags/ "${{ vars.AIRFLOW_DAGS_DEST }}"/ ; fi
  publish:
    needs: [apply]
    runs-on: ubuntu-latest
    permissions:
      contents: read
    if: github.ref == 'refs/heads/main' && secrets.DMM_API_URL != ''
    steps:
      - uses: actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5  # v4.3.1
      - uses: actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065  # v5.6.0
        with: { python-version: '3.12' }
      - run: pip install --quiet data-product-forge
      - run: fluid publish ${{ env.CONTRACT }} --catalog ${{ vars.CATALOG || 'datamesh-manager' }}
"""

JENKINS = """\
// FLUID CI/CD Pipeline — Jenkinsfile
//
// Credential model (provider-agnostic):
//   fluid's credential resolver reads provider auth from the runner
//   environment. Each provider expects its own env vars (Snowflake:
//   SNOWFLAKE_*; GCP: GOOGLE_APPLICATION_CREDENTIALS; AWS: AWS_*;
//   Azure: AZURE_*; see `fluid_build.credentials.resolver`). Two ways
//   to surface them inside this pipeline — see the `environment {}`
//   block below for the commented examples.
//
// Operator variables (set via Jenkins env, credentials, or inline):
//   BUILD_ID            — builds[].id for dbt hybrid-reference projects
//   AIRFLOW_DAGS_DEST   — rsync target for airflow DAG deployment (optional)
//   CATALOG             — catalog name for `fluid publish` (default: datamesh-manager)
pipeline {
    // Default to any available agent. Change to `label 'your-label'` if you
    // have a dedicated FLUID build agent.
    agent any

    environment {
        CONTRACT = 'contract.fluid.yaml'
        PROVIDER = 'default'
        BUILD_ID = ''
        AIRFLOW_DAGS_DEST = ''
        CATALOG = 'datamesh-manager'

        // ── Provider credential bindings (pick one pattern) ──────
        // fluid's credential resolver chain (see
        // `fluid_build.credentials.resolver`) already knows how to
        // read provider auth from the runner environment — Snowflake
        // reads SNOWFLAKE_*, GCP reads GOOGLE_APPLICATION_CREDENTIALS,
        // AWS reads AWS_*, Azure reads AZURE_*, and so on. This
        // Jenkinsfile is provider-agnostic; you only need to make
        // sure those env vars are present when `fluid` runs.
        //
        // Path 1 — agent env passthrough. Set the env vars on the
        // Jenkins agent/container once (docker-compose `environment:`,
        // Kubernetes agent template, or Jenkins Global Node
        // Properties). `sh` steps inherit them automatically; no
        // changes needed here.
        //
        // Path 2 — Jenkins credential store. After creating
        // `string` credentials for your provider, uncomment the
        // matching lines below. Replace `<credential-id>` with your
        // actual Jenkins credential ID.
        //
        //   <PROVIDER_ENV_VAR> = credentials('<credential-id>')
        //
        // e.g. Snowflake:  SNOWFLAKE_ACCOUNT = credentials('snowflake-account')
        //      GCP:        GOOGLE_APPLICATION_CREDENTIALS = credentials('gcp-sa-key')
        //      AWS:        AWS_ACCESS_KEY_ID = credentials('aws-access-key')
        //                  AWS_SECRET_ACCESS_KEY = credentials('aws-secret-key')
        //
        // Catalog publish (only if using `fluid publish`):
        //   DMM_API_URL = credentials('dmm-api-url')
        //   DMM_API_KEY = credentials('dmm-api-key')
    }

    stages {
        stage('Setup') {
            steps {
                // Ensure Python + fluid-build are available on the agent.
                // If your agent image pre-installs fluid-build, this step
                // is a no-op.
                sh '''
                    python3 -m pip install --user --quiet data-product-forge
                    export PATH="$HOME/.local/bin:$PATH"
                    fluid --version
                '''
            }
        }
        stage('Validate') {
            steps {
                sh 'fluid validate $CONTRACT'
            }
        }
        stage('Generate') {
            parallel {
                stage('Transformations') {
                    steps {
                        sh 'fluid generate speed-transformation'
                    }
                }
                stage('Schedules') {
                    steps {
                        sh 'fluid generate schedule'
                    }
                }
            }
            post {
                success {
                    archiveArtifacts artifacts: 'dbt_project/**, dags/**', allowEmptyArchive: true
                }
            }
        }
        stage('Plan') {
            steps {
                sh 'fluid --provider $PROVIDER plan $CONTRACT --out runtime/plan.json'
            }
            post {
                success {
                    archiveArtifacts artifacts: 'runtime/plan.json', fingerprint: true
                }
            }
        }
        stage('Test') {
            steps {
                sh 'fluid contract-tests $CONTRACT'
            }
        }
        stage('Apply') {
            when { branch 'main' }
            input {
                message 'Deploy?'
                ok 'Apply'
            }
            steps {
                // --build is required for dbt hybrid-reference builds; harmless otherwise.
                sh '''
                    if [ -n "$BUILD_ID" ]; then
                        fluid apply $CONTRACT --build $BUILD_ID --yes
                    else
                        fluid apply runtime/plan.json --yes
                    fi
                '''
            }
        }
        stage('Airflow DAG Sync') {
            when {
                allOf {
                    branch 'main'
                    expression { return env.AIRFLOW_DAGS_DEST?.trim() }
                }
            }
            steps {
                sh 'if [ -d dags/ ]; then rsync -av --delete dags/ "$AIRFLOW_DAGS_DEST"/ ; fi'
            }
        }
        stage('Publish') {
            when {
                allOf {
                    branch 'main'
                    expression { return env.DMM_API_URL?.trim() }
                }
            }
            steps {
                sh 'fluid publish $CONTRACT --catalog $CATALOG'
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
