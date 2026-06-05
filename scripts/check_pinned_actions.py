#!/usr/bin/env python3
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

import json
import os
import re
import sys
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parent.parent

# ``PINNED_ACTIONS`` (the action@tag -> action@sha map for generated CI
# templates) lives in ``pipeline_systems/_base.py``. It used to live in
# ``pipeline_templates.py``; that module now only re-exports it, so the
# scanner reads the file that actually holds the dict literal.
TEMPLATES_PATH = REPO_ROOT / "fluid_build" / "forge" / "core" / "pipeline_systems" / "_base.py"

WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"

GITHUB_API_HOST = "api.github.com"

# A `uses:` reference inside a workflow file. Captures the action ref
# (``owner/repo`` plus optional ``/subpath``) and whatever follows the ``@``.
# Local actions (``./path``) and reusable-workflow refs to the same repo are
# intentionally not matched — only external ``owner/repo@ref`` forms.
WORKFLOW_USES_RE = re.compile(
    r"^\s*-?\s*uses:\s*"
    r"(?P<action>[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9._/-]+)"
    r"@(?P<ref>[^\s#]+)",
    re.MULTILINE,
)

# A fully pinned ref is a 40-character lowercase hex commit SHA.
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _github_api_json(url: str) -> object:
    """Fetch JSON from the GitHub API after enforcing the expected origin."""
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.netloc != GITHUB_API_HOST:
        raise ValueError(f"Refusing non-GitHub API URL: {url}")
    req = urllib.request.Request(url, headers=_gh_headers())
    with urllib.request.urlopen(req, timeout=15) as resp:  # nosec B310
        return json.loads(resp.read())


# Regex to parse PINNED_ACTIONS entries (single line, inside the dict):
#   "owner/repo@vTag": "owner/repo@sha",  # vX.Y.Z
ENTRY_RE = re.compile(
    r'^\s*"(?P<owner>[A-Za-z0-9_-]+)/(?P<repo>[A-Za-z0-9_/-]+)@(?P<tag>v[\w.]+)"'
    r"\s*:\s*"
    r'"[A-Za-z0-9_/-]+@(?P<sha>[0-9a-f]{40})"'
    r"\s*,?\s*#\s*(?P<pinned_version>v[\S]+)",
    re.MULTILINE,
)


def _gh_headers() -> dict:
    """Build GitHub API headers, including auth token if available."""
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def get_latest_release_sha(owner: str, repo: str, tag_prefix: str) -> dict | None:
    """Query GitHub API for the latest release matching a major version prefix."""
    # Strip sub-path from repo (e.g., "codeql-action/upload-sarif" -> "codeql-action")
    base_repo = repo.split("/")[0]
    url = f"https://api.github.com/repos/{owner}/{base_repo}/releases?per_page=30"
    try:
        releases = _github_api_json(url)
    except Exception as e:
        print(f"  WARNING: Could not fetch releases for {owner}/{base_repo}: {e}")
        return None

    # Find the latest release matching the major version prefix (e.g., "v4" matches "v4.3.1")
    major = tag_prefix.split(".")[0]  # "v4.3.1" -> "v4", "v0.35.0" -> "v0"
    if not major:
        major = tag_prefix

    for release in releases:
        tag = release.get("tag_name", "")
        if tag.startswith(major + ".") or tag == major:
            # Get the commit SHA for this tag
            tag_url = f"https://api.github.com/repos/{owner}/{base_repo}/git/ref/tags/{tag}"
            try:
                tag_data = _github_api_json(tag_url)
                sha = tag_data["object"]["sha"]
                # If it's an annotated tag, dereference to commit
                if tag_data["object"]["type"] == "tag":
                    deref_url = tag_data["object"]["url"]
                    deref_data = _github_api_json(deref_url)
                    sha = deref_data["object"]["sha"]
                return {"tag": tag, "sha": sha}
            except Exception as e:
                print(f"  WARNING: Could not resolve tag {tag} for {owner}/{base_repo}: {e}")
                return None

    return None


def scan_workflows_for_unpinned() -> list[tuple[str, int, str]]:
    """Scan ``.github/workflows/*.yml`` for actions not pinned to a commit SHA.

    Returns a list of ``(filename, line_number, "action@ref")`` tuples, one per
    unpinned ``uses:`` reference. An empty list means every external action in
    every workflow file is SHA-pinned.
    """
    findings: list[tuple[str, int, str]] = []
    if not WORKFLOWS_DIR.is_dir():
        return findings

    for path in sorted(WORKFLOWS_DIR.glob("*.yml")) + sorted(WORKFLOWS_DIR.glob("*.yaml")):
        # Force UTF-8: Windows defaults to the active code page (cp1252)
        # which rejects the non-Latin1 chars maintainers sometimes add to
        # commit messages / step names. The files are committed as UTF-8
        # on every platform.
        text = path.read_text(encoding="utf-8")
        for match in WORKFLOW_USES_RE.finditer(text):
            action = match.group("action")
            ref = match.group("ref")
            # Local composite actions resolve to a path, never owner/repo@ref,
            # so the regex already excludes them. Anything reaching here is an
            # external action and must be pinned to a 40-hex commit SHA.
            if SHA_RE.match(ref):
                continue
            line_no = text.count("\n", 0, match.start()) + 1
            findings.append((path.name, line_no, f"{action}@{ref}"))
    return findings


def check_workflow_pins() -> int:
    """Fail (return 1) if any workflow file references an unpinned action."""
    print("Scanning .github/workflows/ for unpinned actions...\n")
    findings = scan_workflows_for_unpinned()
    if not findings:
        print("  All workflow actions are pinned to a commit SHA.\n")
        return 0

    print(f"  {len(findings)} unpinned action reference(s) found:")
    for filename, line_no, ref in findings:
        print(f"  UNPINNED  {filename}:{line_no}  {ref}")
    print(
        "\nEvery external action must be pinned to a full 40-char commit SHA "
        "(e.g. `uses: actions/checkout@<sha>  # v5.0.1`).\n"
    )
    return 1


def main() -> int:
    strict = "--strict" in sys.argv

    # Correctness gate (always blocking): no workflow file may reference an
    # action by floating tag/branch. This runs regardless of --strict because
    # an unpinned action is a security regression, not a staleness warning.
    workflow_rc = check_workflow_pins()

    source = TEMPLATES_PATH.read_text()
    entries = ENTRY_RE.findall(source)

    if not entries:
        print("ERROR: No PINNED_ACTIONS entries found in pipeline_templates.py")
        return 1

    stale = []
    print(f"Checking {len(entries)} pinned actions...\n")

    for owner, repo, tag, current_sha, pinned_version in entries:
        display = f"{owner}/{repo}@{tag}"
        latest = get_latest_release_sha(owner, repo, pinned_version)

        if latest is None:
            print(f"  ? {display:<55} (could not check)")
            continue

        if latest["sha"].startswith(current_sha) or current_sha.startswith(latest["sha"]):
            print(f"  OK {display:<54} {pinned_version} (current)")
        else:
            stale.append((display, pinned_version, latest["tag"], latest["sha"]))
            print(f"  STALE {display:<51} pinned={pinned_version} latest={latest['tag']}")

    print()
    if stale:
        print(f"{len(stale)} action(s) are outdated:")
        for display, old_ver, new_ver, new_sha in stale:
            print(f"  {display}: {old_ver} -> {new_ver} ({new_sha[:12]})")
        print("\nUpdate PINNED_ACTIONS in fluid_build/forge/core/pipeline_systems/_base.py")
        stale_rc = 1 if strict else 0
    else:
        print("All pinned actions are up to date.")
        stale_rc = 0

    # An unpinned workflow action is always a hard failure; template staleness
    # is only fatal under --strict. The worse of the two wins.
    return max(workflow_rc, stale_rc)


if __name__ == "__main__":
    sys.exit(main())
