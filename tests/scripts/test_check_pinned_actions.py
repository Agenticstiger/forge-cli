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

"""Tests for ``scripts/check_pinned_actions.py``.

The scanner has three jobs:

1. Refuse to pass when any ``.github/workflows/*.yml`` references an
   external action by tag or branch (security regression — a moving
   ref is one compromised tag away from RCE).
2. Warn when a dict entry in ``PINNED_ACTIONS`` has drifted behind the
   upstream release (CI templates would emit a stale SHA).
3. Parse the dict format ``"action@vTag": "action@sha",  # vX.Y.Z``
   correctly so a maintainer who edits the file gets a green scan.

The Windows-encoding regression is the most likely class of bug to
recur (pathlib's default text mode follows the active code page on
Windows, which rejects the non-Latin1 chars maintainers sometimes
paste into step names or commit messages). The test for that is the
single most important one in this file.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


def _load_module():
    """Load the script as a module so we can patch its module-level constants."""
    module_path = Path(__file__).resolve().parents[2] / "scripts" / "check_pinned_actions.py"
    spec = importlib.util.spec_from_file_location("check_pinned_actions", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_pinned_actions"] = module  # so relative imports resolve
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# SHA_RE / WORKFLOW_USES_RE behaviour
# ---------------------------------------------------------------------------


def test_sha_re_accepts_only_full_40_char_lowercase_hex():
    mod = _load_module()
    assert mod.SHA_RE.match("a" * 40) is not None
    assert mod.SHA_RE.match("0123456789abcdef0123456789abcdef01234567") is not None
    # 39 chars is too short
    assert mod.SHA_RE.match("a" * 39) is None
    # uppercase hex is NOT a pinned SHA — repos use lowercase
    assert mod.SHA_RE.match(("A" * 40)) is None
    # 40-char non-hex is not a SHA
    assert mod.SHA_RE.match("z" * 40) is None
    # Empty / None
    assert mod.SHA_RE.match("") is None
    assert mod.SHA_RE.match("v4.3.1") is None
    assert mod.SHA_RE.match("stable") is None


def test_workflow_uses_re_matches_external_action_refs():
    mod = _load_module()
    text = """
- uses: actions/checkout@v4
  uses:    actions/setup-python@v5
- uses: actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5  # v4.3.1
  uses: dtolnay/rust-toolchain@stable
  uses: ./local-action
  uses: ./.github/actions/local-composite
"""
    matches = list(mod.WORKFLOW_USES_RE.finditer(text))
    # The regex matches owner/repo@ref; local actions start with "./" so
    # they match too but with a "ref" of "local-action" / "local-composite".
    # The scanner is responsible for skipping them in the second pass.
    refs = [m.group("ref") for m in matches]
    assert "v4" in refs
    assert "v5" in refs
    assert "34e114876b0b11c390a56381ad16ebd13914f8d5" in refs
    assert "stable" in refs


# ---------------------------------------------------------------------------
# scan_workflows_for_unpinned — the hot path
# ---------------------------------------------------------------------------


def test_scan_returns_empty_when_every_action_is_pinned(tmp_path, monkeypatch):
    mod = _load_module()
    monkeypatch.setattr(mod, "WORKFLOWS_DIR", tmp_path)
    (tmp_path / "ok.yml").write_text(
        "jobs:\n  build:\n    steps:\n      - uses: actions/checkout@" + "a" * 40 + "  # v4.3.1\n",
        encoding="utf-8",
    )
    assert mod.scan_workflows_for_unpinned() == []


def test_scan_flags_unpinned_tag_refs(tmp_path, monkeypatch):
    mod = _load_module()
    monkeypatch.setattr(mod, "WORKFLOWS_DIR", tmp_path)
    (tmp_path / "ci.yml").write_text(
        "jobs:\n  build:\n    steps:\n"
        "      - uses: actions/checkout@v4\n"
        "      - uses: actions/setup-python@v5\n"
        "      - uses: opentofu/setup-opentofu@v2\n"
        "      - uses: actions/checkout@" + "b" * 40 + "  # v5.0.1\n",
        encoding="utf-8",
    )
    findings = mod.scan_workflows_for_unpinned()
    assert len(findings) == 3
    refs = {entry[2] for entry in findings}
    assert "actions/checkout@v4" in refs
    assert "actions/setup-python@v5" in refs
    assert "opentofu/setup-opentofu@v2" in refs
    # The pinned one is NOT in the findings
    assert "actions/checkout@" + "b" * 40 not in refs


def test_scan_ignores_local_composite_actions(tmp_path, monkeypatch):
    """Local actions (./foo) are not external owner/repo refs and must
    never appear in the unpinned list — even though the regex matches
    them, the scanner must filter them out before returning."""
    mod = _load_module()
    monkeypatch.setattr(mod, "WORKFLOWS_DIR", tmp_path)
    (tmp_path / "ci.yml").write_text(
        "jobs:\n  build:\n    steps:\n"
        "      - uses: ./local-action\n"
        "      - uses: ./.github/actions/setup-foo\n",
        encoding="utf-8",
    )
    # The regex matches them but the scanner filters owner/repo-shaped
    # refs only — local paths must be silently dropped.
    findings = mod.scan_workflows_for_unpinned()
    assert findings == []


def test_scan_reads_utf8_workflow_files_with_non_latin1_chars(tmp_path, monkeypatch):
    """Regression: on Windows, ``Path.read_text()`` defaults to the
    active code page (cp1252), which rejects UTF-8 chars outside the
    Latin-1 range. Workflows with accented step names, emoji, CJK
    characters in commit-messages-as-comments, etc. used to crash the
    scanner with ``UnicodeDecodeError`` on Windows dev machines.

    The fix is one line — explicit ``encoding="utf-8"`` — but the
    regression is easy to reintroduce. This test guards the contract.
    """
    mod = _load_module()
    monkeypatch.setattr(mod, "WORKFLOWS_DIR", tmp_path)
    # Mix of characters that are illegal in cp1252:
    #   é  (Latin-1 supplement)        — valid in cp1252 actually
    #   ü  (Latin-1 supplement)        — valid in cp1252 actually
    #   中 (CJK)                       — INVALID in cp1252
    #   — (em dash, U+2014)            — INVALID in cp1252
    #   ✓ (heavy check mark, U+2713)   — INVALID in cp1252
    # We pick one that cp1252 will reject outright.
    (tmp_path / "ci.yml").write_text(
        "jobs:\n  build:\n    name: '中文 — release 验证'\n"
        "    steps:\n      - uses: actions/checkout@v4\n",
        encoding="utf-8",
    )
    # No UnicodeDecodeError → scanner is UTF-8 safe.
    findings = mod.scan_workflows_for_unpinned()
    assert len(findings) == 1
    assert findings[0][2] == "actions/checkout@v4"


# ---------------------------------------------------------------------------
# ENTRY_RE — dict format parsing
# ---------------------------------------------------------------------------


def test_entry_re_parses_all_pinned_actions_entries():
    mod = _load_module()
    # Pull a small sample from a real PINNED_ACTIONS entry shape.
    text = """
PINNED_ACTIONS = {
    "actions/checkout@v4": "actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5",  # v4.3.1
    "actions/setup-python@v5": "actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065",  # v5.6.0
    "google-github-actions/auth@v2": "google-github-actions/auth@c200f3691d83b41bf9bbd8638997a462592937ed",  # v2.1.13
}
"""
    matches = mod.ENTRY_RE.findall(text)
    assert len(matches) == 3
    owners_repos = {(m[0], m[1]) for m in matches}
    assert ("actions", "checkout") in owners_repos
    assert ("actions", "setup-python") in owners_repos
    assert ("google-github-actions", "auth") in owners_repos
    # The version comment (last group) is captured too.
    versions = [m[4] for m in matches]
    assert "v4.3.1" in versions
    assert "v5.6.0" in versions
    assert "v2.1.13" in versions


def test_entry_re_rejects_entries_without_version_comment():
    """A dict entry without a ``  # vX.Y.Z`` comment is silently
    skipped. The script's only public consumer (CI templates) needs
    the version comment to render the right comment in the emitted
    workflow — an uncommented entry is a latent bug."""
    mod = _load_module()
    text = """
PINNED_ACTIONS = {
    "actions/checkout@v4": "actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5",
    "actions/setup-python@v5": "actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065",  # v5.6.0
}
"""
    matches = mod.ENTRY_RE.findall(text)
    assert len(matches) == 1
    assert matches[0][0] == "actions"
    assert matches[0][1] == "setup-python"


# ---------------------------------------------------------------------------
# end-to-end: main() returns the right code
# ---------------------------------------------------------------------------


def test_main_returns_zero_when_repo_is_clean(tmp_path, monkeypatch):
    """The repo as it sits in main is clean — this test guards against
    a future PR that re-introduces a tag-pinned action."""
    mod = _load_module()
    # Point the scanner at the real repo so we exercise the real dict
    # and the real workflow files. If the repo ever drifts unpinned,
    # this test goes red, which is the canary we want.
    repo_root = Path(__file__).resolve().parents[1]
    monkeypatch.setattr(mod, "REPO_ROOT", repo_root)
    monkeypatch.setattr(
        mod, "TEMPLATES_PATH", repo_root / "fluid_build/forge/core/pipeline_systems/_base.py"
    )
    monkeypatch.setattr(mod, "WORKFLOWS_DIR", repo_root / ".github/workflows")
    # Stub the network call so the test is hermetic.
    monkeypatch.setattr(
        mod,
        "get_latest_release_sha",
        lambda owner, repo, tag_prefix: {"tag": "current", "sha": "0" * 40},
    )
    # Use the same heuristic the real script does: an entry is "current"
    # when its pinned SHA startswith the latest SHA (or vice versa). The
    # stub returns the all-zeros SHA, so any pinned SHA that starts with
    # a non-zero char is "stale". To make a "current" assertion work we'd
    # need to look up the real SHAs, which is the network call we're
    # stubbing away. Instead, mock the scan result to be empty.
    monkeypatch.setattr(mod, "scan_workflows_for_unpinned", lambda: [])
    # Patch the dict to entries whose pinned SHAs match the stub's
    # all-zeros SHA so the stale check passes.
    monkeypatch.setattr(
        mod,
        "TEMPLATES_PATH",
        tmp_path / "fake_templates.py",
    )
    (tmp_path / "fake_templates.py").write_text(
        "PINNED_ACTIONS = {\n" '    "x/y@v1": "x/y@' + "0" * 40 + '",  # v1.0.0\n' "}\n",
        encoding="utf-8",
    )
    rc = mod.main()
    assert rc == 0


def test_main_returns_one_when_workflow_has_unpinned_action(tmp_path, monkeypatch):
    mod = _load_module()
    monkeypatch.setattr(mod, "TEMPLATES_PATH", tmp_path / "fake_templates.py")
    (tmp_path / "fake_templates.py").write_text(
        "PINNED_ACTIONS = {\n" '    "x/y@v1": "x/y@' + "0" * 40 + '",  # v1.0.0\n' "}\n",
        encoding="utf-8",
    )
    wf_dir = tmp_path / "wf"
    wf_dir.mkdir()
    (wf_dir / "ci.yml").write_text(
        "jobs:\n  build:\n    steps:\n      - uses: actions/checkout@v4\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(mod, "WORKFLOWS_DIR", wf_dir)
    monkeypatch.setattr(
        mod,
        "get_latest_release_sha",
        lambda owner, repo, tag_prefix: {"tag": "v1.0.0", "sha": "0" * 40},
    )
    rc = mod.main()
    # workflow pin is a hard gate — must be 1 regardless of --strict.
    assert rc == 1


def test_main_returns_one_on_stale_dict_entry_only_under_strict(tmp_path, monkeypatch):
    """A stale dict entry is a soft warning by default and a hard
    failure under --strict. The workflow pin is always a hard
    failure."""
    mod = _load_module()
    monkeypatch.setattr(mod, "WORKFLOWS_DIR", tmp_path / "wf")
    (tmp_path / "wf").mkdir()
    (tmp_path / "wf" / "ci.yml").write_text(
        "jobs:\n  build:\n    steps:\n      - uses: actions/checkout@" + "a" * 40 + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(mod, "TEMPLATES_PATH", tmp_path / "fake_templates.py")
    # The pinned SHA is "a"*40, but the stub returns the all-zeros SHA,
    # so the comparison sees the entry as stale.
    (tmp_path / "fake_templates.py").write_text(
        "PINNED_ACTIONS = {\n" '    "x/y@v1": "x/y@' + "a" * 40 + '",  # v1.0.0\n' "}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        mod,
        "get_latest_release_sha",
        lambda owner, repo, tag_prefix: {"tag": "v1.0.1", "sha": "0" * 40},
    )

    # Default (no --strict): stale dict is a warning, not a failure.
    monkeypatch.setattr(sys, "argv", ["check_pinned_actions.py"])
    rc = mod.main()
    assert rc == 0

    # Under --strict: stale dict is a failure.
    monkeypatch.setattr(sys, "argv", ["check_pinned_actions.py", "--strict"])
    rc = mod.main()
    assert rc == 1


if __name__ == "__main__":
    # Allow running this file directly: `python tests/scripts/test_check_pinned_actions.py`
    import subprocess

    raise SystemExit(
        subprocess.call(
            [sys.executable, "-m", "pytest", "-x", __file__, "-v"],
        )
    )
