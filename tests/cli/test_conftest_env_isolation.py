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

"""``tests/cli/conftest.py::test_environment`` must not leak what it sets.

It wrote ``FLUID_ENV`` and ``FLUID_LOG_LEVEL`` straight into ``os.environ`` and
restored a snapshot afterwards. Autouse fixtures in the root conftest and in this
directory's conftest create ``monkeypatch`` before it, so ``monkeypatch.undo()`` ran
after that restore. A test that called ``monkeypatch.delenv`` or ``monkeypatch.setenv``
on either variable recorded the fixture's value as the original, and the undo put it
back for the rest of the session. ``test_cli_output_keeps_brackets.py`` delenvs every
``FLUID_*`` key. Later subprocess tests inherited ``FLUID_LOG_LEVEL=DEBUG``, and
``TestValidateStrict`` in ``tests/test_overlay_keyed_merge.py`` saw the loader log its
warning a second time.
"""

from __future__ import annotations

import os

import pytest

# Everything test_environment sets.
_FIXTURE_VARS = ("FLUID_ENV", "FLUID_LOG_LEVEL")


@pytest.fixture(scope="module", autouse=True)
def _nothing_outlives_the_module():
    # Module scope: torn down after every function-scoped fixture of the last test,
    # monkeypatch's undo included, so this sees what the next module would inherit.
    saved = {name: os.environ.pop(name, None) for name in _FIXTURE_VARS}
    yield
    leaked = {name: os.environ.pop(name) for name in _FIXTURE_VARS if name in os.environ}
    for name, value in saved.items():
        if value is not None:
            os.environ[name] = value
    assert not leaked, f"{leaked} outlived this module's tests"


def test_a_test_may_delenv_what_the_fixture_set(monkeypatch):
    assert (os.environ["FLUID_ENV"], os.environ["FLUID_LOG_LEVEL"]) == ("test", "DEBUG")
    for name in _FIXTURE_VARS:
        monkeypatch.delenv(name)
    assert not any(name in os.environ for name in _FIXTURE_VARS)
