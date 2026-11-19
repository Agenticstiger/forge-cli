# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The Tekton Task image: pinned by digest, as the GitHub actions are by SHA,
and set with ``fluid generate ci --image``."""

from __future__ import annotations

import argparse
import logging
import re
from pathlib import Path
from typing import List

import pytest
import yaml

from fluid_build.cli import build_parser
from fluid_build.cli._common import CLIError
from fluid_build.cli.generate_ci import run as generate_ci_run
from fluid_build.forge.core.pipeline_systems import PINNED_IMAGES
from fluid_build.forge.core.pipeline_systems._base import check_image_ref
from fluid_build.forge.core.pipeline_systems.tekton import IMAGE

pytestmark = [pytest.mark.unit]

_LOG = logging.getLogger("test_tekton_image")
DIGEST = re.compile(r"[^@\s]+:[^@\s]+@sha256:[0-9a-f]{64}")


def _images(root: Path) -> List[str]:
    found = []
    for doc in yaml.safe_load_all((root / "tekton/tasks.yaml").read_text()):
        if doc and doc.get("kind") == "Task":
            found += [step["image"] for step in doc["spec"]["steps"]]
    return found


def _generate(tmp_path: Path, monkeypatch, system: str = "tekton", add: tuple = (), **extra) -> int:
    (tmp_path / "contract.fluid.yaml").write_text(
        yaml.safe_dump({"fluidVersion": "0.7.5", "kind": "DataProduct", "id": "gold.x"})
    )
    monkeypatch.chdir(tmp_path)
    args = argparse.Namespace(
        system=system,
        out=None,
        contract="contract.fluid.yaml",
        add=list(add),
        provider=None,
        **extra,
    )
    return generate_ci_run(args, _LOG)


def test_every_pinned_image_is_pinned_by_digest():
    assert PINNED_IMAGES, "PINNED_IMAGES is empty"
    for tagged, pinned in PINNED_IMAGES.items():
        assert pinned.startswith(tagged + "@sha256:"), tagged
        assert DIGEST.fullmatch(pinned), pinned


def test_the_default_task_image_is_pinned(tmp_path, monkeypatch):
    assert IMAGE == PINNED_IMAGES["python:3.12-slim"]
    assert _generate(tmp_path, monkeypatch, add=("security",)) == 0
    images = _images(tmp_path)
    assert len(images) >= 2  # fluid-ship and the add-on Task
    assert set(images) == {IMAGE}


def test_the_flag_sets_every_task_image(tmp_path, monkeypatch):
    mine = "registry.example.com:5000/team/fluid-runner:1.2@sha256:" + "a" * 64
    assert _generate(tmp_path, monkeypatch, image=mine) == 0
    assert set(_images(tmp_path)) == {mine}


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "python:3.12 ; rm -rf /",
        "python:3.12\nimage: evil",
        "-python",
        "Python:3.12",
        "python@sha256:abc",
    ],
)
def test_an_image_that_is_not_a_reference_is_refused(tmp_path, monkeypatch, bad):
    with pytest.raises(CLIError) as err:
        _generate(tmp_path, monkeypatch, image=bad)
    assert err.value.event == "generate_ci_invalid_option"
    assert not (tmp_path / "tekton").exists()


@pytest.mark.parametrize(
    "good",
    ["python:3.12-slim", "python", "ghcr.io/org/img:v1", "localhost:5000/a/b@sha256:" + "0" * 64],
)
def test_image_references_are_accepted(good):
    assert check_image_ref(good) == good


@pytest.mark.parametrize(
    "system", ["github", "gitlab", "jenkins", "azure", "bitbucket", "circleci"]
)
def test_the_flag_is_refused_where_it_would_do_nothing(tmp_path, monkeypatch, system):
    with pytest.raises(CLIError) as err:
        _generate(tmp_path, monkeypatch, system=system, image="python:3.12-slim")
    assert err.value.event == "generate_ci_image_unsupported"


def test_the_parser_takes_the_flag():
    args = build_parser().parse_args(
        ["generate", "ci", "--system", "tekton", "--image", "python:3.12-slim"]
    )
    assert args.image == "python:3.12-slim"
    assert build_parser().parse_args(["generate", "ci", "--system", "tekton"]).image is None
