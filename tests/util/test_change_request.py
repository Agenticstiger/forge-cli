# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Whether the process runs in a CI build of a change request (a pull or merge request)."""

from __future__ import annotations

import pytest

from fluid_build.util.change_request import (
    CHANGE_REQUEST_ENV,
    CHANGE_REQUEST_VARIABLES,
    change_request_marker,
)

pytestmark = [pytest.mark.unit]


@pytest.mark.parametrize(
    "env,names",
    [
        ({"CHANGE_ID": "42"}, "CHANGE_ID"),
        ({"ghprbPullId": "7"}, "ghprbPullId"),
        ({"gitlabMergeRequestId": "8"}, "gitlabMergeRequestId"),
        ({"GITHUB_EVENT_NAME": "pull_request"}, "GITHUB_EVENT_NAME=pull_request"),
        ({"GITHUB_EVENT_NAME": "pull_request_target"}, "GITHUB_EVENT_NAME=pull_request_target"),
        ({"GITHUB_EVENT_NAME": "pull_request_review"}, "GITHUB_EVENT_NAME=pull_request_review"),
        (
            {"GITHUB_EVENT_NAME": "pull_request_review_comment"},
            "GITHUB_EVENT_NAME=pull_request_review_comment",
        ),
        ({"CI_MERGE_REQUEST_IID": "3"}, "CI_MERGE_REQUEST_IID"),
        ({"CI_MERGE_REQUEST_ID": "301"}, "CI_MERGE_REQUEST_ID"),
        ({"CI_PIPELINE_SOURCE": "merge_request_event"}, "CI_PIPELINE_SOURCE=merge_request_event"),
        (
            {"CI_PIPELINE_SOURCE": "external_pull_request_event"},
            "CI_PIPELINE_SOURCE=external_pull_request_event",
        ),
        ({"CI_EXTERNAL_PULL_REQUEST_IID": "5"}, "CI_EXTERNAL_PULL_REQUEST_IID"),
        ({"BUILD_REASON": "PullRequest"}, "BUILD_REASON=PullRequest"),
        ({"SYSTEM_PULLREQUEST_PULLREQUESTID": "9"}, "SYSTEM_PULLREQUEST_PULLREQUESTID"),
        ({"SYSTEM_PULLREQUEST_PULLREQUESTNUMBER": "9"}, "SYSTEM_PULLREQUEST_PULLREQUESTNUMBER"),
        ({"BITBUCKET_PR_ID": "11"}, "BITBUCKET_PR_ID"),
        ({"CIRCLE_PULL_REQUEST": "https://example.invalid/o/r/pull/1"}, "CIRCLE_PULL_REQUEST"),
        ({"CIRCLE_PR_NUMBER": "1"}, "CIRCLE_PR_NUMBER"),
        ({"CI_PULL_REQUEST": "https://example.invalid/o/r/pull/1"}, "CI_PULL_REQUEST"),
        ({CHANGE_REQUEST_ENV: "true"}, CHANGE_REQUEST_ENV),
        ({CHANGE_REQUEST_ENV: "1"}, CHANGE_REQUEST_ENV),
    ],
)
def test_each_systems_change_request_build_is_recognised(env: dict, names: str) -> None:
    marker = change_request_marker(env)
    assert marker is not None
    assert names in marker


@pytest.mark.parametrize(
    "env",
    [
        {},
        {"CHANGE_ID": ""},
        {"CHANGE_ID": "   "},
        {"GITHUB_EVENT_NAME": "push"},
        {"GITHUB_EVENT_NAME": "workflow_dispatch"},
        {"CI_PIPELINE_SOURCE": "push"},
        {"BUILD_REASON": "IndividualCI"},
        {CHANGE_REQUEST_ENV: "false"},
        {CHANGE_REQUEST_ENV: "FALSE"},
        {CHANGE_REQUEST_ENV: " 0 "},
        {CHANGE_REQUEST_ENV: "no"},
        {CHANGE_REQUEST_ENV: "off"},
        {CHANGE_REQUEST_ENV: ""},
        {"BRANCH_NAME": "main", "GITLAB_CI": "true", "GITHUB_ACTIONS": "true"},
    ],
)
def test_a_default_branch_or_local_run_is_not_one(env: dict) -> None:
    assert change_request_marker(env) is None


def test_values_are_compared_without_regard_to_case() -> None:
    assert change_request_marker({"BUILD_REASON": "pullrequest"}) is not None
    assert change_request_marker({"GITHUB_EVENT_NAME": "PULL_REQUEST"}) is not None
    assert change_request_marker({CHANGE_REQUEST_ENV: "TRUE"}) is not None


@pytest.mark.parametrize("value", ["y", "t", "enabled", "pull_request", "ture"])
def test_any_other_value_of_the_pipelines_own_variable_marks_the_build(value: str) -> None:
    """A misspelt or unexpected value fails closed: only blank or a false spelling leaves
    the build unmarked."""
    assert change_request_marker({CHANGE_REQUEST_ENV: value}) is not None


def test_the_pipelines_own_value_is_never_quoted() -> None:
    """The marker is printed in ship's log and in the refusal; the pipeline's variable can
    hold anything, so only its name is."""
    marker = str(change_request_marker({CHANGE_REQUEST_ENV: "yes\n::notice::spoofed"}))
    assert CHANGE_REQUEST_ENV in marker
    assert "spoofed" not in marker and "\n" not in marker


def test_nothing_turns_a_change_request_back_into_a_default_branch_build() -> None:
    """The marker variable only ever adds a change request: false never clears the CI's own."""
    assert change_request_marker({"CHANGE_ID": "4", CHANGE_REQUEST_ENV: "false"}) is not None


def test_the_marker_names_the_system() -> None:
    assert "Jenkins" in str(change_request_marker({"CHANGE_ID": "4"}))
    assert "GitLab" in str(change_request_marker({"CI_MERGE_REQUEST_IID": "4"}))


def test_every_variable_read_is_listed() -> None:
    """``tests/conftest.py`` clears these before every test: CI runs the suite on pull
    requests too."""
    assert CHANGE_REQUEST_ENV in CHANGE_REQUEST_VARIABLES
    assert {"CHANGE_ID", "GITHUB_EVENT_NAME", "CI_PULL_REQUEST"} <= set(CHANGE_REQUEST_VARIABLES)
