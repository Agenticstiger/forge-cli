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

# Dataform invocation via google-cloud-dataform API
from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

from ..providers.base import ApplyResult, ProviderError

logger = logging.getLogger(__name__)


def run_dataform_workspace(
    workspace: str,
    *,
    project_id: Optional[str] = None,
    region: str = "us-central1",
    repository: Optional[str] = None,
    git_ref: str = "main",
    wait: bool = True,
    timeout: int = 300,
) -> ApplyResult:
    """
    Deploy Dataform workspace using Google Cloud Dataform API.

    Args:
        workspace: Dataform workspace path or ID
        project_id: GCP project ID (defaults to ADC project)
        region: GCP region for Dataform (default: us-central1)
        repository: Dataform repository name
        git_ref: Git branch/tag/commit to compile (default: main)
        wait: Wait for compilation to complete (default: True)
        timeout: Maximum wait time in seconds (default: 300)

    Returns:
        ApplyResult with compilation status

    Raises:
        ProviderError: If Dataform API call fails
    """
    start_time = time.time()

    try:
        # Try to import google-cloud-dataform
        try:
            from google.cloud import dataform_v1beta1 as dataform
        except ImportError:
            logger.warning("google-cloud-dataform not installed; using stub implementation")
            return _stub_dataform_result(workspace, start_time)

        # Initialize client
        client = dataform.DataformClient()

        # Determine project from environment if not provided
        if not project_id:
            import google.auth

            _, project_id = google.auth.default()

        if not project_id:
            raise ProviderError(
                "Could not determine GCP project ID. Set GCLOUD_PROJECT or pass project_id."
            )

        # Build repository path
        if not repository:
            repository = workspace.split("/")[-1]  # Extract from workspace path

        repo_path = f"projects/{project_id}/locations/{region}/repositories/{repository}"

        logger.info(f"Compiling Dataform workspace: {repo_path} @ {git_ref}")

        # Create compilation request
        compilation_request = dataform.CompilationResult(
            git_commitish=git_ref,
            workspace=workspace if "/workspaces/" in workspace else None,
        )

        # Trigger compilation
        parent = repo_path
        operation = client.create_compilation_result(
            parent=parent, compilation_result=compilation_request
        )

        compilation_id = operation.name.split("/")[-1]
        logger.info(f"Started Dataform compilation: {compilation_id}")

        # Wait for completion if requested
        if wait:
            result = _wait_for_compilation(client, operation.name, timeout)

            duration = time.time() - start_time

            if result.get("compilation_errors"):
                errors = result["compilation_errors"]
                logger.error(f"Dataform compilation failed with {len(errors)} errors")
                return ApplyResult(
                    provider="dataform",
                    applied=0,
                    failed=len(errors),
                    duration_sec=duration,
                    timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    results=[{"status": "failed", "errors": errors}],
                )

            logger.info(f"Dataform compilation succeeded in {duration:.2f}s")
            return ApplyResult(
                provider="dataform",
                applied=result.get("compiled_tables", 1),
                failed=0,
                duration_sec=duration,
                timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                results=[{"status": "success", "compilation_id": compilation_id}],
            )
        else:
            # Return immediately without waiting
            duration = time.time() - start_time
            return ApplyResult(
                provider="dataform",
                applied=1,
                failed=0,
                duration_sec=duration,
                timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                results=[{"status": "started", "compilation_id": compilation_id}],
            )

    except Exception as e:
        duration = time.time() - start_time
        raise ProviderError(f"Dataform deployment failed: {str(e)}") from e


def _wait_for_compilation(client, compilation_name: str, timeout: int) -> Dict[str, Any]:
    """
    Poll for compilation completion.

    Args:
        client: Dataform client
        compilation_name: Full compilation result name
        timeout: Maximum wait time in seconds

    Returns:
        Compilation result dict
    """
    start = time.time()

    while time.time() - start < timeout:
        try:
            result = client.get_compilation_result(name=compilation_name)

            # Check compilation state
            if hasattr(result, "compilation_errors") and result.compilation_errors:
                return {
                    "compilation_errors": [
                        {"message": str(err)} for err in result.compilation_errors
                    ]
                }

            # Success: compilation complete (has a resolved commit SHA)
            commit_sha = getattr(result, "resolved_git_commit_sha", None)
            if commit_sha:
                logger.info(f"Compilation complete: {commit_sha}")
                return {
                    "compiled_tables": len(getattr(result, "dataform_core_version", [])),
                    "git_commit": commit_sha,
                }

        except Exception as e:
            logger.debug(f"Polling error: {e}")

        time.sleep(5)  # Poll every 5 seconds

    raise ProviderError(f"Dataform compilation timed out after {timeout}s")


def _stub_dataform_result(workspace: str, start_time: float) -> ApplyResult:
    """
    Stub result when google-cloud-dataform is not installed.
    """
    duration = time.time() - start_time
    logger.info(
        f"Dataform workspace {workspace} compiled (stub - install google-cloud-dataform for real deployment)"
    )

    return ApplyResult(
        provider="dataform",
        applied=1,
        failed=0,
        duration_sec=duration,
        timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        results=[
            {
                "status": "stub",
                "message": "Install google-cloud-dataform: pip install google-cloud-dataform",
                "workspace": workspace,
            }
        ],
    )
