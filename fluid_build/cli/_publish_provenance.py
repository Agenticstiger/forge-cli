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

"""Where a published contract came from, for ``fluid publish``.

The Command Center records each contract version with its git provenance
(``POST /api/v1/contracts/sync``: ``git_commit_sha``, ``git_repo_url``,
``git_branch``, ``git_file_path``, ``deployed_by``). This module collects those
facts, in this order for each one:

1. the variables Jenkins' git plugin exports (``GIT_COMMIT``, ``GIT_URL``,
   ``GIT_BRANCH``) and ``BUILD_TAG``;
2. the contract's own repository, read with one ``git rev-parse`` and, when no
   ``GIT_URL`` is set, one ``git config --get remote.origin.url``;
3. nothing: a fact that cannot be read is left out, never guessed.

``git status``, ``git log`` and anything else that can run a repository's
configured helpers (``core.fsmonitor``) or touch the network is not used, so a
checkout from an untrusted branch cannot run code through this step.
"""

from __future__ import annotations

import getpass
import os
import re
import subprocess
from pathlib import Path
from typing import Dict, Mapping, Optional
from urllib.parse import urlsplit, urlunsplit

# A commit id as the Command Center stores it: ``contract_versions.git_commit_sha``
# is ``String(40)``, which holds a SHA-1 id, full or abbreviated.
_COMMIT_RE = re.compile(r"[0-9a-fA-F]{7,40}")

# The Command Center's column widths. A longer value is a server error, so it
# is left out rather than cut: a truncated path or URL names something else.
_MAX_LENGTH = {
    "git_repo_url": 500,
    "git_file_path": 500,
    "git_branch": 255,
    "deployed_by": 255,
}

_GIT_TIMEOUT_SECONDS = 5

_BUNDLE_SUFFIXES = (".tgz", ".tar.gz")


def _clean(value: Optional[str], limit: int) -> Optional[str]:
    """``value`` stripped, or None when it is empty, too long or not printable."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > limit or not text.isprintable():
        return None
    return text


def _commit(value: Optional[str]) -> Optional[str]:
    text = value.strip() if isinstance(value, str) else ""
    return text.lower() if _COMMIT_RE.fullmatch(text) else None


def repo_url_without_credentials(url: Optional[str]) -> Optional[str]:
    """``url`` with any credential removed, or None when nothing safe remains.

    A clone URL can carry a token: ``https://x-access-token:<token>@host/...``,
    ``https://<token>@host/...``, or a query parameter. For a URL with a scheme
    the whole userinfo, the query and the fragment are dropped (pip's
    ``remove_auth_from_url`` drops the userinfo the same way). The scp form
    ``user@host:path`` cannot carry a password, so it is kept unless the part
    before ``@`` holds a ``:``.
    """
    text = _clean(url, _MAX_LENGTH["git_repo_url"])
    if text is None:
        return None
    if "://" in text:
        try:
            parts = urlsplit(text)
        except ValueError:
            return None
        netloc = parts.netloc.rpartition("@")[2]
        return urlunsplit((parts.scheme, netloc, parts.path, "", "")) or None
    user, at, rest = text.partition("@")
    if at and ":" in user:
        return rest or None
    return text


def _git(contract_dir: Path, *args: str) -> Optional[str]:
    """stdout of ``git -C <contract_dir> <args>``, or None when it fails."""
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        completed = subprocess.run(  # noqa: S603 — fixed argv, shell=False
            ["git", "-C", str(contract_dir), *args],
            shell=False,
            check=False,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout


def _repo_facts(contract_dir: Path) -> Dict[str, str]:
    """``toplevel``, ``commit`` and ``branch`` of the repository holding ``contract_dir``."""
    out = _git(contract_dir, "rev-parse", "--show-toplevel", "HEAD", "--abbrev-ref", "HEAD")
    lines = out.splitlines() if out else []
    if len(lines) != 3:
        return {}
    facts = {"toplevel": lines[0].strip()}
    commit = _commit(lines[1])
    if commit:
        facts["commit"] = commit
    # ``HEAD`` is a detached checkout (what Jenkins' git step makes): no branch.
    if lines[2].strip() and lines[2].strip() != "HEAD":
        facts["branch"] = lines[2].strip()
    return facts


def _file_path(contract_path: Path, toplevel: Optional[str]) -> Optional[str]:
    """The contract's path inside its repository, with ``/`` separators."""
    if not toplevel or contract_path.name.endswith(_BUNDLE_SUFFIXES):
        # A bundle is a build artifact, not the file under version control.
        return None
    try:
        relative = contract_path.resolve().relative_to(Path(toplevel).resolve())
    except (OSError, ValueError):
        return None
    return _clean(relative.as_posix(), _MAX_LENGTH["git_file_path"])


def _deployed_by(environ: Mapping[str, str]) -> Optional[str]:
    """Jenkins' ``BUILD_TAG`` (``jenkins-<job>-<build>``), else the OS user."""
    tag = _clean(environ.get("BUILD_TAG"), _MAX_LENGTH["deployed_by"])
    if tag:
        return tag
    try:
        return _clean(getpass.getuser(), _MAX_LENGTH["deployed_by"])
    except (OSError, KeyError, ImportError):
        # No USER/LOGNAME and no passwd entry, as in some CI containers.
        return None


def contract_provenance(
    contract_path: Path, environ: Optional[Mapping[str, str]] = None
) -> Dict[str, str]:
    """The ``ContractSync`` provenance fields for ``contract_path``; unknown ones absent."""
    env = os.environ if environ is None else environ
    repo = _repo_facts(contract_path.parent)

    facts: Dict[str, Optional[str]] = {
        "git_commit_sha": _commit(env.get("GIT_COMMIT")) or repo.get("commit"),
        "git_branch": _clean(env.get("GIT_BRANCH"), _MAX_LENGTH["git_branch"])
        or _clean(repo.get("branch"), _MAX_LENGTH["git_branch"]),
        "git_file_path": _file_path(contract_path, repo.get("toplevel")),
        "deployed_by": _deployed_by(env),
    }
    repo_url = repo_url_without_credentials(env.get("GIT_URL"))
    if repo_url is None and repo:
        repo_url = repo_url_without_credentials(
            _git(contract_path.parent, "config", "--get", "remote.origin.url")
        )
    facts["git_repo_url"] = repo_url
    return {key: value for key, value in facts.items() if value}
