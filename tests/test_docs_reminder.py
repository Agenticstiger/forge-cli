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

"""Tests for the docs reminder workflow helper."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
DOCS_REMINDER_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "docs_reminder.js"


def _run_node(source: str) -> dict:
    result = subprocess.run(
        [NODE, "-e", source],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def _call_export(function_name: str, argument):
    source = f"""
const mod = require({json.dumps(str(DOCS_REMINDER_SCRIPT))});
const result = mod[{json.dumps(function_name)}]({json.dumps(argument)});
console.log(JSON.stringify(result));
"""
    return _run_node(source)


def _run_docs_reminder(body: str, files: list[str], labels=None, comments=None):
    scenario = {
        "body": body,
        "files": files,
        "labels": labels or [],
        "comments": comments or [],
    }
    source = f"""
const mod = require({json.dumps(str(DOCS_REMINDER_SCRIPT))});
const scenario = {json.dumps(scenario)};
const calls = [];

const listFiles = async () => {{
  return {{ data: scenario.files.slice(0, 100).map((filename) => ({{ filename }})) }};
}};
const listLabelsOnIssue = async () => {{
  return {{ data: scenario.labels.map((name) => ({{ name }})) }};
}};
const listComments = async () => {{
  return {{
    data: scenario.comments.map((body) => ({{ user: {{ type: "Bot" }}, body }})),
  }};
}};

const github = {{
  paginate: async (method) => {{
    if (method === listFiles) {{
      return scenario.files.map((filename) => ({{ filename }}));
    }}
    if (method === listLabelsOnIssue) {{
      return scenario.labels.map((name) => ({{ name }}));
    }}
    if (method === listComments) {{
      return scenario.comments.map((body) => ({{ user: {{ type: "Bot" }}, body }}));
    }}
    throw new Error("Unexpected paginate call");
  }},
  rest: {{
    pulls: {{ listFiles }},
    issues: {{
      listLabelsOnIssue,
      listComments,
      addLabels: async (params) => calls.push({{ op: "addLabels", labels: params.labels }}),
      createComment: async (params) => calls.push({{ op: "createComment", body: params.body }}),
      removeLabel: async (params) => calls.push({{ op: "removeLabel", name: params.name }}),
    }},
  }},
}};

const context = {{
  repo: {{ owner: "Agenticstiger", repo: "forge-cli" }},
  payload: {{ pull_request: {{ number: 42, body: scenario.body }} }},
}};

(async () => {{
  const result = await mod.runDocsReminder({{
    github,
    context,
    core: {{ info: () => {{}}, warning: () => {{}} }},
  }});
  console.log(JSON.stringify({{ result, calls }}));
}})().catch((error) => {{
  console.error(error);
  process.exit(1);
}});
"""
    return _run_node(source)


@pytest.mark.skipif(NODE is None, reason="node is required to test the docs reminder helper")
@pytest.mark.parametrize(
    "files",
    [
        ["docs/getting-started/README.md"],
        ["GOVERNANCE.md"],
        ["SUPPORT.md"],
        [".github/PULL_REQUEST_TEMPLATE/docs-only.md"],
        [".github/pull_request_template.md"],
    ],
)
def test_is_docs_only_change_accepts_docs_and_community_paths(files):
    assert _call_export("isDocsOnlyChange", files) is True


@pytest.mark.skipif(NODE is None, reason="node is required to test the docs reminder helper")
def test_run_docs_reminder_adds_label_and_comment_for_undocumented_code_pr():
    result = _run_docs_reminder(body="", files=["fluid_build/cli/forge.py"])
    assert result["result"]["documented"] is False
    assert any(call["op"] == "addLabels" for call in result["calls"])
    assert any(call["op"] == "createComment" for call in result["calls"])


@pytest.mark.skipif(NODE is None, reason="node is required to test the docs reminder helper")
def test_run_docs_reminder_removes_label_when_docs_pr_is_linked():
    result = _run_docs_reminder(
        body="- [x] **Docs PR linked:** https://github.com/Agenticstiger/forge_docs/pull/123",
        files=["fluid_build/cli/forge.py"],
        labels=["needs-docs"],
    )
    assert result["result"]["documented"] is True
    assert any(call["op"] == "removeLabel" for call in result["calls"])
    assert not any(call["op"] == "addLabels" for call in result["calls"])


@pytest.mark.skipif(NODE is None, reason="node is required to test the docs reminder helper")
def test_run_docs_reminder_checks_all_pages_of_pr_files():
    files = [f"docs/page-{index}.md" for index in range(100)] + ["fluid_build/cli/forge.py"]
    result = _run_docs_reminder(body="", files=files)
    assert result["result"]["documented"] is False
    assert result["result"]["fileCount"] == 101
    assert any(call["op"] == "addLabels" for call in result["calls"])
