# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Shared Forge context loading and CLI-argument helpers."""

from __future__ import annotations

__all__ = [
    "get_cli_arg",
    "get_target_directory",
    "load_context",
]


import json
from pathlib import Path
from typing import Any, Dict, Optional, Type

import yaml


def get_target_directory(args: Any, default_name: str = "my-fluid-project") -> Path:
    """
    Determine target directory for project creation.

    If no target is specified and we're inside the package, create outside the
    repository root by default.
    """
    if args.target_dir:
        return Path(args.target_dir)

    cwd = Path.cwd()
    try:
        package_root = Path(__file__).parent.parent.parent
        if cwd.is_relative_to(package_root):
            suggested_parent = package_root.parent
            if suggested_parent.exists() and suggested_parent.is_dir():
                return suggested_parent / default_name
            return Path.home() / "fluid-projects" / default_name
    except (ValueError, Exception):
        pass

    return cwd / default_name


def get_cli_arg(args: Any, name: str, default: Any = None) -> Any:
    """Read argparse-style attributes without letting MagicMock invent values."""
    if hasattr(args, "__dict__") and name in vars(args):
        return vars(args)[name]
    return default


def load_context(
    context_input: str,
    console: Optional[Any] = None,
    *,
    context_error_cls: Type[Exception] = ValueError,
) -> Dict[str, Any]:
    """Load and validate additional context from JSON or YAML text/files."""
    try:
        if context_input.strip().startswith("{"):
            try:
                context = json.loads(context_input)
            except json.JSONDecodeError as exc:
                raise context_error_cls(f"Invalid JSON: {exc}")
            if not isinstance(context, dict):
                raise context_error_cls("Context must be a JSON object")
            return context

        context_path = Path(context_input)
        if not context_path.exists():
            raise context_error_cls(f"Context file not found: {context_path}")
        if not context_path.is_file():
            raise context_error_cls(f"Context path is not a file: {context_path}")
        if context_path.stat().st_size > 1024 * 1024:
            raise context_error_cls("Context file too large (max 1MB)")

        with open(context_path, encoding="utf-8") as handle:
            if context_path.suffix in {".md", ".markdown", ".txt"}:
                content = handle.read().strip()
                if not content:
                    raise context_error_cls("Context file is empty")
                context = {
                    "project_goal": content,
                    "description": content,
                }
            elif context_path.suffix in {".yaml", ".yml"}:
                context = yaml.safe_load(handle)
            elif context_path.suffix == ".json":
                context = json.load(handle)
            else:
                content = handle.read()
                try:
                    context = json.loads(content)
                except json.JSONDecodeError:
                    try:
                        context = yaml.safe_load(content)
                    except yaml.YAMLError as exc:
                        raise context_error_cls(f"Could not parse as JSON or YAML: {exc}")

        if not isinstance(context, dict):
            raise context_error_cls("Context must be a dictionary/object")

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
            "provider",
            "owner",
            "description",
            "technologies",
        }
        invalid_keys = set(context.keys()) - valid_keys
        if invalid_keys and console:
            console.print(
                f"[yellow]Warning:[/yellow] Unknown context keys: {', '.join(invalid_keys)}"
            )

        return context
    except context_error_cls:
        raise
    except Exception as exc:
        raise context_error_cls(f"Failed to load context: {exc}")
