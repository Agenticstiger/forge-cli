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
Git repository information detection.
"""

import subprocess
from pathlib import Path
from typing import Dict, Optional


def get_git_info(directory: Optional[Path] = None) -> Dict[str, Optional[str]]:
    """
    Extract git information from current directory.

    Args:
        directory: Directory to check (default: current directory)

    Returns:
        Dictionary with git information:
        - repo: Git repository URL
        - commit: Current commit hash (SHA)
        - branch: Current branch name
        - tag: Current tag (if on a tag)
        - author: Commit author name and email
        - dirty: True if working directory has uncommitted changes

    Example:
        >>> info = get_git_info()
        >>> print(info)
        {
            'repo': 'https://github.com/company/data-pipelines.git',
            'commit': 'a3f7c2e9b1d4c5f6e7a8b9c0d1e2f3a4b5c6d7e8',
            'branch': 'main',
            'tag': 'v1.2.3',
            'author': 'John Doe <john@company.com>',
            'dirty': False
        }
    """
    if directory is None:
        directory = Path.cwd()

    info = {
        "repo": None,
        "commit": None,
        "branch": None,
        "tag": None,
        "author": None,
        "dirty": False,
    }

    try:
        # Check if in a git repository
        subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=directory,
            capture_output=True,
            check=True,
            timeout=2,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        # Not a git repository or git not installed
        return info

    # Get remote URL
    try:
        result = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            cwd=directory,
            capture_output=True,
            text=True,
            timeout=2,
        )
        if result.returncode == 0:
            info["repo"] = result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    # Get current commit hash
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=directory, capture_output=True, text=True, timeout=2
        )
        if result.returncode == 0:
            info["commit"] = result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    # Get current branch
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=directory,
            capture_output=True,
            text=True,
            timeout=2,
        )
        if result.returncode == 0:
            branch = result.stdout.strip()
            # Don't set branch if in detached HEAD state
            if branch != "HEAD":
                info["branch"] = branch
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    # Get current tag (if on a tag)
    try:
        result = subprocess.run(
            ["git", "describe", "--exact-match", "--tags"],
            cwd=directory,
            capture_output=True,
            text=True,
            timeout=2,
        )
        if result.returncode == 0:
            info["tag"] = result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    # Get commit author
    try:
        result = subprocess.run(
            ["git", "log", "-1", "--format=%an <%ae>"],
            cwd=directory,
            capture_output=True,
            text=True,
            timeout=2,
        )
        if result.returncode == 0:
            info["author"] = result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    # Check if working directory is dirty
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=directory,
            capture_output=True,
            text=True,
            timeout=2,
        )
        if result.returncode == 0:
            info["dirty"] = bool(result.stdout.strip())
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    return info
