# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Build-runner test fixtures.

Build-runner tests use ``respx`` / ``httpx.MockTransport`` to mock
HTTP responses for synthetic hostnames (``databricks.test``,
``kafka-connect.test``, ``airbyte.test``). Two pieces of the SSRF
guard interfere with those tests:

1. ``safe_httpx_client`` validates ``base_url`` at construction via
   ``assert_safe_url``, which calls ``socket.getaddrinfo`` and fails
   for synthetic ``.test`` hostnames.
2. The per-request pin hook resolves + rewrites the URL host to the
   pinned IP, which makes respx miss its route (it matches by host).

Both behaviours are exhaustively tested in ``tests/util/test_safe_http.py``.
This fixture disables them for build-runner unit tests so the tests
stay focused on the runner / registrar logic.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest


def _noop_assert_safe_url(url, *, allow_private=False):  # noqa: ARG001
    """Test stub — accept any URL, return a sentinel pinned IP."""
    from urllib.parse import urlparse

    parsed = urlparse(url)
    return parsed.hostname or "test", "127.0.0.1"


def _noop_pin_hook_factory(*, allow_private: bool = False):  # noqa: ARG001
    def _hook(request) -> None:  # noqa: ARG001
        return None

    return _hook


@pytest.fixture(autouse=True)
def _disable_ssrf_guard_for_build_runner_tests():
    """Replace the SSRF guard primitives with no-ops for build-runner
    tests so respx-mocked hostnames work without real DNS."""
    with patch(
        "fluid_build.util.safe_http.assert_safe_url",
        side_effect=_noop_assert_safe_url,
    ), patch(
        "fluid_build.util.safe_http._make_request_pin_hook",
        side_effect=_noop_pin_hook_factory,
    ):
        yield
