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

"""Tests for fluid_build.observability.git — git repository introspection."""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from fluid_build.observability.git import get_git_info


class TestGetGitInfo:
    def test_not_a_git_repo(self):
        """When git rev-parse fails, return empty info."""
        with patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, "git")):
            info = get_git_info(Path("/tmp"))
        assert info["repo"] is None
        assert info["commit"] is None
        assert info["dirty"] is False

    def test_git_not_installed(self):
        """When git binary missing, return empty info."""
        with patch("subprocess.run", side_effect=FileNotFoundError()):
            info = get_git_info()
        assert info["repo"] is None

    def test_timeout(self):
        """When git times out, return empty info."""
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("git", 2)):
            info = get_git_info()
        assert info["repo"] is None

    def test_full_git_info(self):
        """Mock all subprocess calls to simulate full git info."""
        call_count = {"n": 0}
        responses = [
            # rev-parse --git-dir (success)
            MagicMock(returncode=0, stdout=""),
            # config --get remote.origin.url
            MagicMock(returncode=0, stdout="https://github.com/user/repo.git\n"),
            # rev-parse HEAD
            MagicMock(returncode=0, stdout="abc123def456\n"),
            # rev-parse --abbrev-ref HEAD
            MagicMock(returncode=0, stdout="main\n"),
            # describe --exact-match --tags
            MagicMock(returncode=0, stdout="v1.0.0\n"),
            # log -1 --format=...
            MagicMock(returncode=0, stdout="Jane <jane@co.com>\n"),
            # status --porcelain
            MagicMock(returncode=0, stdout=""),
        ]

        def mock_run(*args, **kwargs):
            idx = call_count["n"]
            call_count["n"] += 1
            if idx == 0:
                # First call is rev-parse --git-dir; must not raise
                return responses[idx]
            return responses[min(idx, len(responses) - 1)]

        with patch("subprocess.run", side_effect=mock_run):
            info = get_git_info(Path("/fake"))

        assert info["repo"] == "https://github.com/user/repo.git"
        assert info["commit"] == "abc123def456"
        assert info["branch"] == "main"
        assert info["tag"] == "v1.0.0"
        assert info["author"] == "Jane <jane@co.com>"
        assert info["dirty"] is False

    def test_dirty_working_tree(self):
        """Detect dirty working directory."""
        call_count = {"n": 0}
        responses = [
            MagicMock(returncode=0),  # rev-parse --git-dir
            MagicMock(returncode=1, stdout=""),  # remote.origin.url fails
            MagicMock(returncode=0, stdout="abc\n"),  # HEAD
            MagicMock(returncode=0, stdout="main\n"),  # branch
            MagicMock(returncode=1, stdout=""),  # no tag
            MagicMock(returncode=0, stdout="Dev <d@c.com>\n"),  # author
            MagicMock(returncode=0, stdout=" M file.py\n"),  # dirty
        ]

        def mock_run(*args, **kwargs):
            idx = call_count["n"]
            call_count["n"] += 1
            return responses[min(idx, len(responses) - 1)]

        with patch("subprocess.run", side_effect=mock_run):
            info = get_git_info()
        assert info["dirty"] is True

    def test_detached_head(self):
        """Detached HEAD should not set branch."""
        call_count = {"n": 0}
        responses = [
            MagicMock(returncode=0),
            MagicMock(returncode=1, stdout=""),
            MagicMock(returncode=0, stdout="abc\n"),
            MagicMock(returncode=0, stdout="HEAD\n"),  # detached
            MagicMock(returncode=1, stdout=""),
            MagicMock(returncode=0, stdout="Dev <d@c.com>\n"),
            MagicMock(returncode=0, stdout=""),
        ]

        def mock_run(*args, **kwargs):
            idx = call_count["n"]
            call_count["n"] += 1
            return responses[min(idx, len(responses) - 1)]

        with patch("subprocess.run", side_effect=mock_run):
            info = get_git_info()
        assert info["branch"] is None

    def test_default_directory_is_cwd(self):
        """When no directory given, uses cwd."""
        with patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, "git")) as mock:
            get_git_info()
            # Check cwd was passed
            call_kwargs = mock.call_args
            assert call_kwargs.kwargs.get("cwd") == Path.cwd()
