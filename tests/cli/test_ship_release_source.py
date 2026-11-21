# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The code a release was built from, as ``release.json`` records it.

``provenance.source`` names the revision, repository and ref the CI says it built, from environment
variables only. A clone URL can carry a token and a repository can be a path on the runner's
disk, and ``release.json`` is published for others to read: what is not a plain revision, a
credential-free URL or a ref name is left out.
"""

from __future__ import annotations

import pytest

from fluid_build.cli import _ship_release as release

SHA = "4458aaa47b3ad2190a101dd457e244c7ed5a14a8"


def github(**changes):
    env = {
        "GITHUB_RUN_ID": "42",
        "GITHUB_SHA": SHA,
        "GITHUB_REPOSITORY": "org/orders",
        "GITHUB_REF": "refs/heads/main",
    }
    env.update(changes)
    return env


def test_github_names_the_revision_the_repository_and_the_ref():
    assert release.source_revision(github()) == {
        "revision": SHA,
        "repository": "https://github.com/org/orders",
        "ref": "refs/heads/main",
    }


def test_a_github_enterprise_server_is_the_repositorys_host():
    env = github(GITHUB_SERVER_URL="https://ghe.example.com/")
    assert release.source_revision(env)["repository"] == "https://ghe.example.com/org/orders"


@pytest.mark.parametrize(
    "env, expected",
    [
        (
            {
                "CI_JOB_ID": "7",
                "CI_COMMIT_SHA": SHA,
                "CI_PROJECT_URL": "https://gitlab.example.com/org/orders",
                "CI_COMMIT_REF_NAME": "v1.4.0",
            },
            {
                "revision": SHA,
                "repository": "https://gitlab.example.com/org/orders",
                "ref": "v1.4.0",
            },
        ),
        (
            {
                "CIRCLE_BUILD_NUM": "9",
                "CIRCLE_SHA1": SHA,
                "CIRCLE_REPOSITORY_URL": "git@github.com:org/orders.git",
                "CIRCLE_BRANCH": "main",
            },
            {"revision": SHA, "repository": "github.com:org/orders.git", "ref": "main"},
        ),
        (
            {
                "BUILD_BUILDID": "12",
                "BUILD_SOURCEVERSION": SHA,
                "BUILD_REPOSITORY_URI": "https://dev.example.com/org/_git/orders",
                "BUILD_SOURCEBRANCH": "refs/heads/release/1.4",
            },
            {
                "revision": SHA,
                "repository": "https://dev.example.com/org/_git/orders",
                "ref": "refs/heads/release/1.4",
            },
        ),
        (
            {
                "BITBUCKET_BUILD_NUMBER": "3",
                "BITBUCKET_COMMIT": SHA,
                "BITBUCKET_GIT_HTTP_ORIGIN": "https://bitbucket.example.com/org/orders",
                "BITBUCKET_BRANCH": "main",
            },
            {
                "revision": SHA,
                "repository": "https://bitbucket.example.com/org/orders",
                "ref": "main",
            },
        ),
        (
            {
                "BUILD_ID": "88",
                "GIT_COMMIT": SHA.upper(),
                "GIT_URL": "ssh://git@git.example.com:7999/org/orders.git",
                "GIT_BRANCH": "origin/main",
            },
            {
                "revision": SHA,
                "repository": "ssh://git.example.com:7999/org/orders.git",
                "ref": "origin/main",
            },
        ),
    ],
    ids=["gitlab", "circleci", "azure", "bitbucket", "jenkins"],
)
def test_every_ci_the_run_metadata_knows_names_its_source_the_same_way(env, expected):
    assert release.source_revision(env) == expected


def test_a_run_outside_ci_records_no_source():
    assert release.source_revision({}) is None
    assert release.source_revision({"GITHUB_SHA": SHA}) is None  # not running on GitHub Actions


def test_the_first_ci_that_is_running_wins():
    env = {**github(), "CI_JOB_ID": "7", "CI_COMMIT_SHA": "0" * 40}
    assert release.source_revision(env)["revision"] == SHA


@pytest.mark.parametrize(
    "clone, shown",
    [
        (
            "https://user:s3cr3t@git.example.com/org/orders.git",
            "https://git.example.com/org/orders.git",
        ),
        (
            "https://x-access-token:abc@git.example.com:8443/o/r?token=abc#frag",
            "https://git.example.com:8443/o/r",
        ),
        ("ssh://deploy@git.example.com/org/orders.git", "ssh://git.example.com/org/orders.git"),
        ("deploy@git.example.com:org/orders.git", "git.example.com:org/orders.git"),
    ],
)
def test_a_clone_url_loses_the_user_the_password_the_query_and_the_fragment(clone, shown):
    env = {"BUILD_ID": "1", "GIT_COMMIT": SHA, "GIT_URL": clone}
    got = release.source_revision(env)["repository"]
    assert got == shown
    for secret in ("s3cr3t", "abc", "deploy", "x-access-token"):
        assert secret not in got


@pytest.mark.parametrize(
    "clone",
    [
        "/srv/git/orders",
        "file:///srv/git/orders",
        "ftp://git.example.com/orders",
        "https://",
        "https://host:notaport/x",
        "two words",
        "https://host/" + "x" * 300,
        "https://host/\nhttps://evil",
        "",
    ],
)
def test_a_repository_that_is_not_a_plain_url_is_left_out(clone):
    env = {"BUILD_ID": "1", "GIT_COMMIT": SHA, "GIT_URL": clone}
    assert release.source_revision(env) == {"revision": SHA}


@pytest.mark.parametrize("value", ["not-a-sha", "abc", "g" * 40, "a" * 65, "4458aaa 4458aaa"])
def test_a_revision_that_is_not_hex_is_left_out(value):
    got = release.source_revision(github(GITHUB_SHA=value))
    assert "revision" not in got and "repository" in got


@pytest.mark.parametrize("value", ["two words", "refs/heads/a;b", "x" * 201, "é", ""])
def test_a_ref_that_is_not_a_name_is_left_out(value):
    assert "ref" not in release.source_revision(github(GITHUB_REF=value))


def test_a_ci_that_sets_nothing_usable_records_no_source():
    env = github(GITHUB_SHA="nope", GITHUB_REPOSITORY="no slash", GITHUB_REF="a b")
    assert release.source_revision(env) is None


def test_the_run_details_carry_the_source_only_when_there_is_one(monkeypatch):
    for name in ("GITHUB_RUN_ID", "CI_JOB_ID", "BUILD_ID", "BUILD_BUILDID", "CIRCLE_BUILD_NUM"):
        monkeypatch.delenv(name, raising=False)
    assert "source" not in release.provenance([], run_id="r-1", report_written_at=None)
    for name, value in github().items():
        monkeypatch.setenv(name, value)
    details = release.provenance([], run_id="r-1", report_written_at=None)
    assert details["source"]["revision"] == SHA and details["run_id"] == "r-1"
