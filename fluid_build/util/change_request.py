# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Whether this process runs in a CI build of a change request (a pull or merge request).

A change-request build runs the change's code before anyone has merged it, and a CI
system often gives it the same credentials a default-branch build has. ``fluid ship``
and ``fluid generate artifacts`` use :func:`change_request_marker` to keep code the
contract names out of such a build: the custom-scaffold engine renders a contract's bundle
templates in the ``fluid`` process.

Each CI system says so in its own variables. For the systems ``fluid generate ci`` writes
pipelines for, the table is the union of the rules of two maintained detectors, ci-info
(``vendors.json``, the ``pr`` entries) and env-ci (each service's ``isPr``), both MIT,
adapted rather than depended on (they are npm packages), plus what each vendor documents:

* Jenkins: ``CHANGE_ID`` (multibranch), ``ghprbPullId`` (GitHub Pull Request Builder),
  ``gitlabMergeRequestId`` (GitLab plugin);
* GitHub Actions: ``GITHUB_EVENT_NAME`` ``pull_request`` or ``pull_request_target``, and
  ``pull_request_review`` / ``pull_request_review_comment``, which also run the pull
  request's merge commit;
* GitLab CI: ``CI_MERGE_REQUEST_IID`` / ``CI_MERGE_REQUEST_ID``, ``CI_PIPELINE_SOURCE``
  ``merge_request_event`` (merge request and merged-results pipelines) or
  ``external_pull_request_event``, and ``CI_EXTERNAL_PULL_REQUEST_IID``;
* Azure Pipelines: ``BUILD_REASON`` ``PullRequest``, ``SYSTEM_PULLREQUEST_PULLREQUESTID``
  and ``SYSTEM_PULLREQUEST_PULLREQUESTNUMBER``;
* Bitbucket Pipelines: ``BITBUCKET_PR_ID``;
* CircleCI: ``CIRCLE_PULL_REQUEST``, ``CIRCLE_PR_NUMBER`` (a fork's pull request) and the
  older ``CI_PULL_REQUEST``.

Tekton sets nothing of its own, and neither does a CI system missing from the table, so a
pipeline marks the build itself with ``FLUID_CHANGE_REQUEST``. Any value but blank, ``0``,
``false``, ``no`` or ``off`` marks it, so a misspelt value fails closed. The generated
Tekton Pipelines take it as a parameter that is true unless the Triggers binding of a
default-branch run passes false. A marker only ever makes a build a change request: no
variable turns one back into a default-branch build; only a command-line flag of the
command that refuses lets a change-request build run the code anyway.

Standard library only: ``fluid ship`` imports it on every run.
"""

from __future__ import annotations

from typing import FrozenSet, Mapping, Optional, Tuple

#: The variable a pipeline sets on a change-request build its CI system does not mark.
CHANGE_REQUEST_ENV = "FLUID_CHANGE_REQUEST"

#: The values of :data:`CHANGE_REQUEST_ENV` that leave a build unmarked (besides blank).
_NOT_MARKED: FrozenSet[str] = frozenset({"0", "false", "no", "off"})

#: ``(system, variable, values)``: the build is a change request when ``variable`` holds one
#: of ``values`` (compared without regard to case), or, with ``values`` ``None``, anything
#: but blank.
_MARKERS: Tuple[Tuple[str, str, Optional[FrozenSet[str]]], ...] = (
    ("Jenkins", "CHANGE_ID", None),
    ("Jenkins", "ghprbPullId", None),
    ("Jenkins", "gitlabMergeRequestId", None),
    (
        "GitHub Actions",
        "GITHUB_EVENT_NAME",
        frozenset(
            {
                "pull_request",
                "pull_request_target",
                "pull_request_review",
                "pull_request_review_comment",
            }
        ),
    ),
    ("GitLab CI", "CI_MERGE_REQUEST_IID", None),
    ("GitLab CI", "CI_MERGE_REQUEST_ID", None),
    (
        "GitLab CI",
        "CI_PIPELINE_SOURCE",
        frozenset({"merge_request_event", "external_pull_request_event"}),
    ),
    ("GitLab CI", "CI_EXTERNAL_PULL_REQUEST_IID", None),
    ("Azure Pipelines", "BUILD_REASON", frozenset({"pullrequest"})),
    ("Azure Pipelines", "SYSTEM_PULLREQUEST_PULLREQUESTID", None),
    ("Azure Pipelines", "SYSTEM_PULLREQUEST_PULLREQUESTNUMBER", None),
    ("Bitbucket Pipelines", "BITBUCKET_PR_ID", None),
    ("CircleCI", "CIRCLE_PULL_REQUEST", None),
    ("CircleCI", "CIRCLE_PR_NUMBER", None),
    ("CircleCI", "CI_PULL_REQUEST", None),
)

#: Every variable :func:`change_request_marker` reads.
CHANGE_REQUEST_VARIABLES: Tuple[str, ...] = (
    *(variable for _, variable, _ in _MARKERS),
    CHANGE_REQUEST_ENV,
)


def _value(env: Mapping[str, str], variable: str) -> str:
    raw = env.get(variable)
    return raw.strip() if isinstance(raw, str) else ""


def change_request_marker(env: Mapping[str, str]) -> Optional[str]:
    """What marks the build in ``env`` as a change request (``"Jenkins: CHANGE_ID is
    set"``, ``"GitHub Actions: GITHUB_EVENT_NAME=pull_request"``), or ``None``.

    A value is quoted only when it is one of a marker's fixed values, so the text never
    carries what a variable held otherwise.
    """
    for system, variable, values in _MARKERS:
        value = _value(env, variable)
        if not value:
            continue
        if values is None:
            return f"{system}: {variable} is set"
        if value.casefold() in values:
            return f"{system}: {variable}={value}"
    own = _value(env, CHANGE_REQUEST_ENV)
    if own and own.casefold() not in _NOT_MARKED:
        return f"the pipeline: {CHANGE_REQUEST_ENV} is set"
    return None


__all__ = ["CHANGE_REQUEST_ENV", "CHANGE_REQUEST_VARIABLES", "change_request_marker"]
