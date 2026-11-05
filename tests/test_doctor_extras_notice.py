# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid doctor`` tells users once, only when litellm or mcp is missing, that they are extras."""

import argparse
import logging
from unittest.mock import patch

from fluid_build.cli import doctor
from fluid_build.llm.providers import LlmReadinessCheck

_NOTICE = "pip install 'data-product-forge[standard]'"


def _present(*missing: str):
    """Patch availability so exactly the names in *missing* look uninstalled."""
    return patch("fluid_build._optional.is_available", side_effect=lambda m: m not in missing)


def _run(*flags: str) -> int:
    parser = argparse.ArgumentParser()
    doctor.register(parser.add_subparsers())
    args = parser.parse_args(["doctor", *flags])
    with (
        patch.object(doctor, "_check_fluid_features", return_value=(True, [])),
        patch.object(
            doctor, "_check_copilot_readiness", return_value=LlmReadinessCheck(ready=True)
        ),
        patch.object(doctor, "_inspect_store_backend", return_value={"backend": "file"}),
        patch.object(doctor, "_resolve_extended_diagnostic_script", return_value=None),
    ):
        return doctor.run(args, logging.getLogger("test"))


def test_notice_names_the_release_and_the_install_command():
    assert "0.28.0" in doctor.EXTRAS_MOVE_NOTICE
    assert _NOTICE in doctor.EXTRAS_MOVE_NOTICE
    assert "\n" not in doctor.EXTRAS_MOVE_NOTICE


def test_missing_litellm_prints_the_notice_exactly_once(capsys):
    with _present("litellm"):
        assert _run() == 0
    # The bracketed extra survives, so the notice is printed literally.
    assert capsys.readouterr().out.count(_NOTICE) == 1


def test_missing_mcp_prints_the_notice(capsys):
    with _present("mcp"):
        assert _run() == 0
    assert capsys.readouterr().out.count(_NOTICE) == 1


def test_nothing_missing_prints_no_notice(capsys):
    with _present():
        assert _run() == 0
    assert _NOTICE not in capsys.readouterr().out


def test_json_output_stays_machine_readable(capsys):
    import json

    with _present("litellm", "mcp"):
        assert _run("--json") == 0
    json.loads(capsys.readouterr().out)
