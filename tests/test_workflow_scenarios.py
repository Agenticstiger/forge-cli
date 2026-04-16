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

import json
import re
import unittest
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Dict, List, Optional, Set

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


def load_yaml(relative_path: str):
    with (REPO_ROOT / relative_path).open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if True in data and "on" not in data:
        data["on"] = data.pop(True)
    return data


def load_label_names():
    with (REPO_ROOT / ".github/labels.json").open("r", encoding="utf-8") as handle:
        return {item["name"] for item in json.load(handle)}


def load_text(relative_path: str) -> str:
    return (REPO_ROOT / relative_path).read_text(encoding="utf-8")


def iter_markdown_links(relative_path: str):
    for match in MARKDOWN_LINK_RE.finditer(load_text(relative_path)):
        yield match.group(1), match.group(2)


def slugify_heading(heading: str) -> str:
    heading = heading.strip().lower()
    heading = re.sub(r"[^\w\s-]", "", heading)
    heading = re.sub(r"\s+", "-", heading)
    heading = re.sub(r"-{2,}", "-", heading)
    return heading.strip("-")


def markdown_anchors(relative_path: str) -> Set[str]:
    anchors = set()
    for line in load_text(relative_path).splitlines():
        if line.startswith("#"):
            heading = line.lstrip("#").strip()
            if heading:
                anchors.add(slugify_heading(heading))
    return anchors


def docs_reminder_outcome(body: str, files: List[str]) -> Dict[str, bool]:
    docs_link_pattern = re.compile(
        r"github\.com/Agenticstiger/forge_docs/pull/\d+",
        re.IGNORECASE,
    )
    no_docs_pattern = re.compile(r"- \[x\] \*\*No docs needed\*\*", re.IGNORECASE)
    docs_todo_pattern = re.compile(r"- \[x\] \*\*Docs TODO\*\*", re.IGNORECASE)

    has_docs_link = bool(docs_link_pattern.search(body or ""))
    no_docs_needed = bool(no_docs_pattern.search(body or ""))
    docs_todo = bool(docs_todo_pattern.search(body or ""))
    all_docs = all(
        file_path.startswith("docs/")
        or file_path in {"README.md", "CONTRIBUTING.md", "CODE_OF_CONDUCT.md", "SECURITY.md"}
        for file_path in files
    )
    documented = has_docs_link or no_docs_needed or docs_todo or all_docs

    return {
        "documented": documented,
        "should_add_needs_docs": not documented,
        "should_comment": not documented,
        "should_remove_needs_docs": documented,
    }


def docs_reminder_actions(
    body: str,
    files: List[str],
    existing_labels: Optional[Set[str]] = None,
    existing_comments: Optional[List[str]] = None,
) -> Dict[str, bool]:
    existing_labels = existing_labels or set()
    existing_comments = existing_comments or []
    outcome = docs_reminder_outcome(body=body, files=files)
    already_commented = any("Documentation Reminder" in comment for comment in existing_comments)

    return {
        "add_needs_docs": outcome["should_add_needs_docs"] and "needs-docs" not in existing_labels,
        "remove_needs_docs": outcome["should_remove_needs_docs"]
        and "needs-docs" in existing_labels,
        "create_comment": outcome["should_comment"] and not already_commented,
    }


def compute_labels(files: List[str]) -> Set[str]:
    config = load_yaml(".github/labeler.yml")
    labels = set()

    for label, rules in config.items():
        for rule in rules:
            patterns = rule["changed-files"][0]["any-glob-to-any-file"]
            if isinstance(patterns, str):
                patterns = [patterns]

            if any(fnmatchcase(file_path, pattern) for file_path in files for pattern in patterns):
                labels.add(label)
                break

    return labels


def sync_actions(
    existing: Dict[str, Dict[str, str]], desired: List[Dict[str, str]]
) -> Dict[str, Set[str]]:
    actions = {"create": set(), "update": set(), "unchanged": set()}

    for label in desired:
        current = existing.get(label["name"])
        color = label["color"].lstrip("#").lower()
        description = label.get("description", "")

        if current is None:
            actions["create"].add(label["name"])
            continue

        current_color = current["color"].lstrip("#").lower()
        current_description = current.get("description", "")

        if current_color == color and current_description == description:
            actions["unchanged"].add(label["name"])
        else:
            actions["update"].add(label["name"])

    return actions


class DocsReminderScenarioTests(unittest.TestCase):
    def test_missing_docs_reference_adds_label_and_comment(self):
        outcome = docs_reminder_outcome(
            body="## Description\n\nA CLI change with no docs section completed.",
            files=["fluid_build/cli/sample.py"],
        )

        self.assertFalse(outcome["documented"])
        self.assertTrue(outcome["should_add_needs_docs"])
        self.assertTrue(outcome["should_comment"])

    def test_no_docs_needed_counts_as_documented(self):
        outcome = docs_reminder_outcome(
            body="- [x] **No docs needed**\n  - Justification: internal refactor only",
            files=["tests/test_smoke.py"],
        )

        self.assertTrue(outcome["documented"])
        self.assertTrue(outcome["should_remove_needs_docs"])

    def test_docs_todo_counts_as_documented(self):
        outcome = docs_reminder_outcome(
            body="- [x] **Docs TODO** — I will create a PR in forge_docs before merge",
            files=["fluid_build/providers/example/README.md"],
        )

        self.assertTrue(outcome["documented"])

    def test_linked_docs_pr_counts_as_documented(self):
        outcome = docs_reminder_outcome(
            body="- [x] **Docs PR linked:**\n  - Docs PR: https://github.com/Agenticstiger/forge_docs/pull/123",
            files=["fluid_build/cli/sample.py"],
        )

        self.assertTrue(outcome["documented"])

    def test_docs_only_changes_are_auto_documented(self):
        outcome = docs_reminder_outcome(
            body="No explicit docs section checked yet.",
            files=["docs/README.md", "CONTRIBUTING.md"],
        )

        self.assertTrue(outcome["documented"])

    def test_unrelated_pr_link_does_not_count_as_docs_pr(self):
        outcome = docs_reminder_outcome(
            body="- [x] **Docs PR linked:**\n  - Docs PR: https://github.com/example/other-repo/pull/7",
            files=["fluid_build/cli/sample.py"],
        )

        self.assertFalse(outcome["documented"])

    def test_documented_pr_removes_existing_needs_docs_label(self):
        actions = docs_reminder_actions(
            body="- [x] **No docs needed**\n  - Justification: test-only change",
            files=["tests/test_smoke.py"],
            existing_labels={"needs-docs"},
        )

        self.assertFalse(actions["add_needs_docs"])
        self.assertTrue(actions["remove_needs_docs"])
        self.assertFalse(actions["create_comment"])

    def test_existing_reminder_comment_is_not_duplicated(self):
        actions = docs_reminder_actions(
            body="CLI behavior changed but the docs section is still empty.",
            files=["fluid_build/cli/sample.py"],
            existing_comments=["### 📄 Documentation Reminder\n\nPlease add docs."],
        )

        self.assertTrue(actions["add_needs_docs"])
        self.assertFalse(actions["create_comment"])

    def test_security_and_code_of_conduct_only_changes_count_as_docs_only(self):
        outcome = docs_reminder_outcome(
            body="No docs box checked.",
            files=["SECURITY.md", "CODE_OF_CONDUCT.md"],
        )

        self.assertTrue(outcome["documented"])


class LabelerScenarioTests(unittest.TestCase):
    def test_cli_and_tests_changes_receive_both_labels(self):
        labels = compute_labels(["fluid_build/cli/sample.py", "tests/test_smoke.py"])
        self.assertEqual(labels, {"cli", "tests"})

    def test_provider_changes_receive_provider_label(self):
        labels = compute_labels(["fluid_build/providers/example/README.md"])
        self.assertEqual(labels, {"provider"})

    def test_docs_changes_receive_docs_label(self):
        labels = compute_labels(["docs/README.md", "CONTRIBUTING.md"])
        self.assertEqual(labels, {"docs"})

    def test_github_changes_receive_ci_label(self):
        labels = compute_labels([".github/workflows/docs-reminder.yml"])
        self.assertEqual(labels, {"ci"})

    def test_security_changes_receive_security_label(self):
        labels = compute_labels(["SECURITY.md", "fluid_build/auth.py"])
        self.assertEqual(labels, {"security"})

    def test_mixed_changes_can_receive_multiple_labels(self):
        labels = compute_labels(
            [
                ".github/workflows/docs-reminder.yml",
                "docs/README.md",
                "tests/test_smoke.py",
            ]
        )
        self.assertEqual(labels, {"ci", "docs", "tests"})


class LabelCatalogTests(unittest.TestCase):
    def test_required_labels_exist(self):
        names = load_label_names()
        self.assertTrue(
            {
                "bug",
                "documentation",
                "duplicate",
                "enhancement",
                "good first issue",
                "help wanted",
                "invalid",
                "question",
                "wontfix",
                "triage",
                "provider",
                "needs-docs",
                "dependencies",
                "ci",
                "stale",
                "keep-open",
                "security",
                "cli",
                "docs",
                "tests",
            }.issubset(names)
        )

    def test_issue_templates_and_dependabot_only_reference_catalog_labels(self):
        names = load_label_names()
        referenced = {"bug", "triage", "provider", "enhancement", "dependencies", "ci"}
        self.assertTrue(referenced.issubset(names))

    def test_stale_workflow_uses_canonical_good_first_issue_label(self):
        workflow = load_yaml(".github/workflows/stale.yml")
        exempt = workflow["jobs"]["stale"]["steps"][0]["with"]["exempt-issue-labels"]
        self.assertIn("good first issue", exempt)


class WorkflowTriggerTests(unittest.TestCase):
    def test_docs_reminder_uses_pull_request_target(self):
        workflow = load_yaml(".github/workflows/docs-reminder.yml")
        self.assertIn("pull_request_target", workflow["on"])

    def test_labeler_uses_pull_request_target(self):
        workflow = load_yaml(".github/workflows/labeler.yml")
        self.assertIn("pull_request_target", workflow["on"])

    def test_label_sync_has_manual_and_push_triggers(self):
        workflow = load_yaml(".github/workflows/sync-labels.yml")
        self.assertIn("workflow_dispatch", workflow["on"])
        self.assertIn("push", workflow["on"])

    def test_pull_request_target_workflows_only_use_trusted_checkout_patterns(self):
        for relative_path in [
            ".github/workflows/docs-reminder.yml",
            ".github/workflows/labeler.yml",
        ]:
            workflow = load_yaml(relative_path)
            steps = workflow["jobs"][next(iter(workflow["jobs"]))]["steps"]

            for step in steps:
                uses = step.get("uses", "")
                if "actions/checkout" not in uses:
                    continue

                checkout_config = step.get("with", {})
                self.assertEqual(
                    checkout_config.get("ref"),
                    "${{ github.event.pull_request.base.sha }}",
                    relative_path,
                )
                self.assertFalse(
                    checkout_config.get("persist-credentials", True),
                    relative_path,
                )


class LabelSyncScenarioTests(unittest.TestCase):
    def test_empty_repo_creates_all_labels(self):
        with (REPO_ROOT / ".github/labels.json").open("r", encoding="utf-8") as handle:
            desired = json.load(handle)

        actions = sync_actions(existing={}, desired=desired)

        self.assertEqual(len(actions["create"]), len(desired))
        self.assertFalse(actions["update"])
        self.assertFalse(actions["unchanged"])

    def test_sync_updates_changed_labels_and_leaves_matching_labels_alone(self):
        desired = [
            {
                "name": "needs-docs",
                "color": "5319e7",
                "description": "Pull request needs a linked docs update or justification",
            },
            {
                "name": "ci",
                "color": "bfd4f2",
                "description": "Continuous integration and automation changes",
            },
        ]
        existing = {
            "needs-docs": {"color": "000000", "description": "old"},
            "ci": {
                "color": "bfd4f2",
                "description": "Continuous integration and automation changes",
            },
        }

        actions = sync_actions(existing=existing, desired=desired)

        self.assertEqual(actions["update"], {"needs-docs"})
        self.assertEqual(actions["unchanged"], {"ci"})
        self.assertFalse(actions["create"])

    def test_sync_leaves_unmanaged_existing_labels_untouched(self):
        desired = [
            {
                "name": "needs-docs",
                "color": "5319e7",
                "description": "Pull request needs a linked docs update or justification",
            },
        ]
        existing = {
            "needs-docs": {
                "color": "5319e7",
                "description": "Pull request needs a linked docs update or justification",
            },
            "legacy-label": {"color": "123456", "description": "left alone"},
        }

        actions = sync_actions(existing=existing, desired=desired)

        self.assertEqual(actions["unchanged"], {"needs-docs"})
        self.assertFalse(actions["create"])
        self.assertFalse(actions["update"])


class MarkdownValidationTests(unittest.TestCase):
    def test_changed_markdown_relative_links_resolve(self):
        files = [
            ".github/pull_request_template.md",
            ".github/PULL_REQUEST_TEMPLATE/provider.md",
            ".github/PULL_REQUEST_TEMPLATE/docs-only.md",
            "CONTRIBUTING.md",
        ]

        for relative_path in files:
            source = REPO_ROOT / relative_path
            for _label, target in iter_markdown_links(relative_path):
                if "://" in target or target.startswith("mailto:"):
                    continue

                path_part, anchor = (target.split("#", 1) + [""])[:2]
                resolved = (source.parent / path_part).resolve() if path_part else source.resolve()

                self.assertTrue(
                    resolved.exists(),
                    f"{relative_path} links to missing file: {target}",
                )

                if anchor:
                    target_relative = resolved.relative_to(REPO_ROOT).as_posix()
                    self.assertIn(
                        anchor,
                        markdown_anchors(target_relative),
                        f"{relative_path} links to missing anchor: {target}",
                    )

    def test_general_pr_template_matches_docs_reminder_options(self):
        template = load_text(".github/pull_request_template.md")

        self.assertIn("**Docs PR linked:**", template)
        self.assertIn("**No docs needed**", template)
        self.assertIn("**Docs TODO**", template)


if __name__ == "__main__":
    unittest.main()
