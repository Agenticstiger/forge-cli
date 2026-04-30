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

"""Tests for the structured corrective-feedback layer."""

from __future__ import annotations

from fluid_build.cli.forge_copilot_corrective_feedback import (
    TOOL_ERROR_GUIDANCE,
    build_corrective_messages,
    diagnose_tool_failure,
)
from fluid_build.cli.forge_tool import ToolDispatchResult


class TestDiagnoseToolFailure:
    def test_legacy_error_dict_diagnosed_as_failure(self) -> None:
        result = {"error": "ToolValidationError", "message": "see logs"}
        is_failure, klass = diagnose_tool_failure(result)
        assert is_failure
        assert klass == "ToolValidationError"

    def test_dispatch_result_failure(self) -> None:
        result = ToolDispatchResult.failure("PathTraversalError", "denied")
        is_failure, klass = diagnose_tool_failure(result)
        assert is_failure
        assert klass == "PathTraversalError"

    def test_dispatch_result_success(self) -> None:
        result = ToolDispatchResult.success({"data": [1, 2, 3]})
        is_failure, klass = diagnose_tool_failure(result)
        assert not is_failure
        assert klass == ""

    def test_plain_success_dict(self) -> None:
        is_failure, klass = diagnose_tool_failure({"data": "ok"})
        assert not is_failure

    def test_dict_with_unrelated_error_key_is_not_failure(self) -> None:
        # Empty/non-string ``error`` shouldn't be treated as failure.
        # (Edge case: a tool legitimately returns a success dict with
        # a field named "error" that's empty — don't false-positive
        # corrective feedback on it.)
        is_failure, _ = diagnose_tool_failure({"error": ""})
        assert not is_failure
        is_failure, _ = diagnose_tool_failure({"error": None})
        assert not is_failure


class TestBuildCorrectiveMessages:
    def test_no_failures_returns_empty_list(self) -> None:
        tool_calls = [{"name": "discover_workspace", "arguments": {}}]
        results = [{"files": ["a.csv"]}]
        messages = build_corrective_messages(tool_calls, results)
        assert messages == []

    def test_validation_error_emits_specific_guidance(self) -> None:
        tool_calls = [{"name": "read_sample_schema", "arguments": {}}]
        results = [{"error": "ToolValidationError", "message": "see logs"}]
        messages = build_corrective_messages(tool_calls, results)
        assert len(messages) == 1
        msg = messages[0]
        assert msg["role"] == "user"
        assert "read_sample_schema" in msg["content"]
        assert "ToolValidationError" in msg["content"]
        # The guidance text from TOOL_ERROR_GUIDANCE must appear.
        assert TOOL_ERROR_GUIDANCE["ToolValidationError"] in msg["content"]

    def test_unknown_error_class_falls_back_to_default_guidance(self) -> None:
        tool_calls = [{"name": "x", "arguments": {}}]
        results = [{"error": "TotallyNovelError", "message": "..."}]
        messages = build_corrective_messages(tool_calls, results)
        assert len(messages) == 1
        assert TOOL_ERROR_GUIDANCE["_default"] in messages[0]["content"]

    def test_mixed_success_and_failure_only_emits_for_failures(self) -> None:
        tool_calls = [
            {"name": "good_tool", "arguments": {}},
            {"name": "bad_tool", "arguments": {}},
            {"name": "another_good", "arguments": {}},
        ]
        results = [
            {"data": 1},
            {"error": "PathTraversalError", "message": "denied"},
            {"data": 2},
        ]
        messages = build_corrective_messages(tool_calls, results)
        assert len(messages) == 1
        assert "bad_tool" in messages[0]["content"]
        assert "PathTraversalError" in messages[0]["content"]

    def test_dispatch_result_failure_produces_message(self) -> None:
        tool_calls = [{"name": "validator", "arguments": {}}]
        results = [ToolDispatchResult.failure("ValidationError", "field x missing")]
        messages = build_corrective_messages(tool_calls, results)
        assert len(messages) == 1
        assert "ValidationError" in messages[0]["content"]

    def test_no_message_quotes_server_side_error_text(self) -> None:
        """Security: corrective feedback must NOT quote the original
        exception ``message`` field — that field can contain server-side
        state. The guidance text is a deterministic per-class string."""
        tool_calls = [{"name": "x", "arguments": {}}]
        results = [
            {
                "error": "ToolValidationError",
                "message": "internal server detail SHOULD NOT LEAK",
            }
        ]
        messages = build_corrective_messages(tool_calls, results)
        content = messages[0]["content"]
        assert "SHOULD NOT LEAK" not in content
        assert "internal server detail" not in content
