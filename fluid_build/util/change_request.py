# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Whether this process runs in a CI build of a change request (a pull or merge request).

A change-request build runs the change's code before anyone has merged it, and a CI
system often gives it the same credentials a default-branch build has. ``fluid ship``
and ``fluid generate artifacts`` use :func:`change_request_marker` to keep code the
contract names out of such a build: the custom-scaffold engine renders a contract's bundle
templates in the ``fluid`` process.

Each CI system says so in its own variables. The table is the union of two maintained
detectors' rules, ci-info (``vendors.json``, the ``pr`` entries) and env-ci (each
service's ``isPr``), both MIT, adapted rather than depended on (they are npm packages):

* Jenkins: ``CHANGE_ID`` (multibranch), ``ghprbPullId`` (GitHub Pull Request Builder),
  ``gitlabMergeRequestId`` (GitLab plugin);
* GitHub Actions: ``GITHUB_EVENT_NAME`` ``pull_request`` or ``pull_request_target``;
* GitLab CI: ``CI_MERGE_REQUEST_IID`` / ``CI_MERGE_REQUEST_ID``, ``CI_PIPELINE_SOURCE``
  ``merge_request_event`` (merge request and merged-results pipelines) or
  ``external_pull_request_event``, and ``CI_EXTERNAL_PULL_REQUEST_IID``;
* Azure Pipelines: ``BUILD_REASON`` ``PullRequest``, ``SYSTEM_PULLREQUEST_PULLREQUESTID``
  and ``SYSTEM_PULLREQUEST_PULLREQUESTNUMBER``;
* Bitbucket Pipelines: ``BITBUCKET_PR_ID``;
* CircleCI: ``CIRCLE_PULL_REQUEST``, ``CIRCLE_PR_NUMBER`` (a fork's pull request) and the
  older ``CI_PULL_REQUEST``.

Tekton sets nothing of its own, so a pipeline marks the build itself with
``FLUID_CHANGE_REQUEST=true`` (the generated Tekton plan Pipeline takes it as a parameter
its Triggers binding sets on a pull request). A marker only ever makes a build a change
request: no variable turns one back into a default-branch build; only a command-line flag
of the command that refuses lets a change-request build run the code anyway.

Standard library only: ``fluid ship`` imports it on every run.
"""

from __future__ import annotations

from typing import FrozenSet, Mapping, Optional, Tuple

#: The variable a pipeline sets on a change-request build its CI system does not mark.
CHANGE_REQUEST_ENV = "FLUID_CHANGE_REQUEST"

_TRUE: FrozenSet[str] = frozenset({"1", "true", "yes", "on"})

#: ``(system, variable, values)``: the build is a change request when ``variable`` holds one
#: of ``values`` (compared without regard to case), or, with ``values`` ``None``, anything
#: but blank.
_MARKERS: Tuple[Tuple[str, str, Optional[FrozenSet[str]]], ...] = (
    ("Jenkins", "CHANGE_ID", None),
    ("Jenkins", "ghprbPullId", None),
    ("Jenkins", "gitlabMergeRequestId", None),
    ("GitHub Actions", "GITHUB_EVENT_NAME", frozenset({"pull_request", "pull_request_target"})),
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
    ("the pipeline", CHANGE_REQUEST_ENV, _TRUE),
)

#: Every variable :func:`change_request_marker` reads.
CHANGE_REQUEST_VARIABLES: Tuple[str, ...] = tuple(variable for _, variable, _ in _MARKERS)


def change_request_marker(env: Mapping[str, str]) -> Optional[str]:
    """What marks the build in ``env`` as a change request (``"CHANGE_ID is set
    (Jenkins)"``, ``"GITHUB_EVENT_NAME=pull_request (GitHub Actions)"``), or ``None``."""
    for system, variable, values in _MARKERS:
        raw = env.get(variable)
        value = raw.strip() if isinstance(raw, str) else ""
        if not value:
            continue
        if values is None:
            return f"{variable} is set ({system})"
        if value.casefold() in values:
            return f"{variable}={value} ({system})"
    return None


__all__ = ["CHANGE_REQUEST_ENV", "CHANGE_REQUEST_VARIABLES", "change_request_marker"]
