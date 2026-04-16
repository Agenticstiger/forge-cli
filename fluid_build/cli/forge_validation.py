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

"""
Input validation utilities for FLUID Forge command
"""

import re
from pathlib import Path
from typing import Optional, Tuple

from .forge_copilot_taxonomy import normalize_use_case


def validate_project_name(name: str) -> Tuple[bool, Optional[str]]:
    """Validate project name format

    Rules:
    - Must be 3-63 characters
    - Lowercase letters, numbers, hyphens only
    - Must start with letter
    - Must end with letter or number
    - No consecutive hyphens
    - No reserved names

    Returns:
        (is_valid, error_message)
    """
    if not name:
        return False, "Project name cannot be empty"

    if len(name) < 3:
        return False, "Project name must be at least 3 characters"

    if len(name) > 63:
        return False, "Project name must be 63 characters or less"

    if not re.match(r"^[a-z][a-z0-9-]*[a-z0-9]$", name):
        return (
            False,
            "Project name must start with a letter, end with letter/number, and contain only lowercase letters, numbers, and hyphens",
        )

    if "--" in name:
        return False, "Project name cannot contain consecutive hyphens"

    reserved_names = ["test", "tmp", "temp", "new", "project", "fluid", "admin", "api", "app"]
    if name in reserved_names:
        return False, f"'{name}' is a reserved name. Please choose something more specific"

    return True, None


def sanitize_project_name(name: str, strict: bool = False) -> str:
    """Sanitize a project name to make it valid

    Args:
        name: The name to sanitize
        strict: If True, raise exception on invalid names

    Returns:
        Sanitized project name

    Raises:
        ValueError: If strict=True and name cannot be sanitized
    """
    if not name:
        if strict:
            raise ValueError("Name is empty")
        return "my-data-product"

    # Convert to lowercase
    name = name.lower()

    # Remove special characters except hyphens and underscores
    name = re.sub(r"[^a-z0-9-_\s]", "", name)

    # Replace spaces and underscores with hyphens
    name = re.sub(r"[\s_]+", "-", name)

    # Collapse multiple hyphens
    name = re.sub(r"-+", "-", name)

    # Remove leading/trailing hyphens
    name = name.strip("-")

    # Ensure starts with letter
    if name and not name[0].isalpha():
        name = f"project-{name}"

    # Ensure ends with letter or number
    if name and not name[-1].isalnum():
        name = name.rstrip("-")

    # Truncate if too long
    if len(name) > 63:
        name = name[:63].rstrip("-")

    # Validate final result
    is_valid, error = validate_project_name(name)
    if not is_valid:
        if strict:
            raise ValueError(f"Cannot sanitize '{name}': {error}")
        # Fallback to default
        return "my-data-product"

    return name


def validate_provider(provider: str) -> Tuple[bool, Optional[str]]:
    """Validate provider name

    Returns:
        (is_valid, error_message)
    """
    valid_providers = ["local", "gcp", "aws", "azure", "snowflake", "databricks"]

    if not provider:
        return False, "Provider cannot be empty"

    if provider.lower() not in valid_providers:
        return False, f"Invalid provider. Must be one of: {', '.join(valid_providers)}"

    return True, None


def validate_template_name(template: str) -> Tuple[bool, Optional[str]]:
    """Validate template name format

    Returns:
        (is_valid, error_message)
    """
    if not template:
        return False, "Template name cannot be empty"

    if len(template) < 2:
        return False, "Template name must be at least 2 characters"

    if not re.match(r"^[a-z][a-z0-9-]*$", template):
        return (
            False,
            "Template name must start with a letter and contain only lowercase letters, numbers, and hyphens",
        )

    return True, None


def validate_directory_path(path: str) -> Tuple[bool, Optional[str]]:
    """Validate directory path

    Returns:
        (is_valid, error_message)
    """
    if not path:
        return False, "Directory path cannot be empty"

    try:
        dir_path = Path(path)

        # Check if path is absolute or relative
        if not dir_path.is_absolute():
            # Relative path is ok
            pass

        # Check if parent exists (if path has parent)
        if dir_path.parent.exists() and not dir_path.parent.is_dir():
            return False, f"Parent path exists but is not a directory: {dir_path.parent}"

        return True, None

    except Exception as e:
        return False, f"Invalid path: {e}"


def validate_context_dict(context: dict) -> Tuple[bool, Optional[str]]:
    """Validate context dictionary structure

    Returns:
        (is_valid, error_message)
    """
    if not isinstance(context, dict):
        return False, "Context must be a dictionary"

    # Valid context keys
    valid_keys = {
        "project_goal",
        "data_sources",
        "use_case",
        "use_case_other",
        "complexity",
        "team_size",
        "domain",
        "canonical_model",
        "supporting_standards",
        "owner_team",
        "build_engine",
        "output_kind",
        "primary_entity",
        "primary_measures",
        "primary_dimensions",
        "time_dimension",
        "time_granularity",
        "refresh_cadence",
        "consumes",
        "interview_summary",
        "assumptions_used",
        "technologies",
        "budget",
        "timeline",
    }

    # Check for required keys
    required_keys = {"project_goal"}
    missing_keys = required_keys - set(context.keys())
    if missing_keys:
        return False, f"Missing required keys: {', '.join(missing_keys)}"

    # Check for unknown keys
    unknown_keys = set(context.keys()) - valid_keys
    if unknown_keys:
        # Warning, not error
        pass

    # Validate specific fields
    if "project_goal" in context:
        goal = context["project_goal"]
        if not isinstance(goal, str) or len(goal) < 5:
            return False, "project_goal must be a string with at least 5 characters"

    if "use_case" in context:
        valid_use_cases = [
            "analytics",
            "etl_pipeline",
            "streaming",
            "ml_pipeline",
            "data_platform",
            "other",
        ]
        if normalize_use_case(context["use_case"]) is None:
            return False, f"use_case must resolve to one of: {', '.join(valid_use_cases)}"

    if "use_case_other" in context and not isinstance(context["use_case_other"], str):
        return False, "use_case_other must be a string when provided"

    if "complexity" in context:
        valid_complexity = ["simple", "intermediate", "advanced"]
        if context["complexity"] not in valid_complexity:
            return False, f"complexity must be one of: {', '.join(valid_complexity)}"

    return True, None


def suggest_fixes(name: str, validation_error: str) -> str:
    """Suggest fixes for common validation errors

    Args:
        name: The invalid name
        validation_error: The validation error message

    Returns:
        Suggestion text
    """
    suggestions = []

    if "must start with a letter" in validation_error:
        # Remove non-letter prefix
        sanitized = re.sub(r"^[^a-z]+", "", name.lower())
        if sanitized:
            suggestions.append(f"Try: {sanitized}")
        suggestions.append("Start with a letter (a-z)")

    if "consecutive hyphens" in validation_error:
        fixed = re.sub(r"-+", "-", name)
        suggestions.append(f"Try: {fixed}")

    if "must be at least" in validation_error:
        suggestions.append("Choose a longer, more descriptive name")

    if "must be 63 characters or less" in validation_error:
        truncated = name[:60] + "..."
        suggestions.append(f"Try: {truncated}")

    if "reserved name" in validation_error:
        suggestions.append(f"Try: my-{name}")
        suggestions.append("Choose a more specific name that describes your project")

    if not suggestions:
        suggestions.append("Use lowercase letters, numbers, and hyphens only")
        suggestions.append("Start with a letter and end with letter/number")

    return "\n  💡 " + "\n  💡 ".join(suggestions)
