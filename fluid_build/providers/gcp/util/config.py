# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

# fluid_build/providers/gcp/util/config.py
"""
GCP configuration resolution utilities.

Handles project/region resolution from various sources:
- CLI flags/parameters
- Environment variables
- Application Default Credentials
- Sensible defaults
"""

import os
from typing import Optional, Tuple


def resolve_project_and_region(
    project: Optional[str] = None, region: Optional[str] = None
) -> Tuple[str, str]:
    """
    Resolve GCP project ID and region from multiple sources.

    Priority order:
    1. Explicit parameters
    2. Environment variables (GOOGLE_CLOUD_PROJECT, FLUID_PROJECT, FLUID_REGION)
    3. ADC default project
    4. Default region (us-central1)

    Returns:
        Tuple of (project_id, region)

    Raises:
        ValueError: If project cannot be determined
    """
    # Resolve project
    resolved_project = project
    if not resolved_project:
        resolved_project = (
            os.environ.get("GOOGLE_CLOUD_PROJECT")
            or os.environ.get("FLUID_PROJECT")
            or os.environ.get("GCLOUD_PROJECT")
        )

    # Try to get from ADC if still not found
    if not resolved_project:
        try:
            resolved_project = _get_adc_project()
        except Exception:
            pass  # Continue without ADC project

    if not resolved_project:
        raise ValueError(
            "GCP project not specified. Set via --project, GOOGLE_CLOUD_PROJECT, "
            "FLUID_PROJECT environment variable, or configure Application Default Credentials"
        )

    # Resolve region
    resolved_region = region
    if not resolved_region:
        resolved_region = (
            os.environ.get("FLUID_REGION")
            or os.environ.get("GOOGLE_CLOUD_REGION")
            or "us-central1"  # Default region
        )

    return resolved_project, resolved_region


def _get_adc_project() -> Optional[str]:
    """
    Attempt to get default project from Application Default Credentials.

    Returns:
        Project ID if available, None otherwise
    """
    try:
        from google.auth import default

        _, project = default()
        return project
    except ImportError:
        # google-auth not available
        return None
    except Exception:
        # ADC not configured or other error
        return None
