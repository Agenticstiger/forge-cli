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

"""The GCP IMDS probe must bypass any ambient HTTP proxy.

``_check_metadata_service`` probes ``metadata.google.internal`` to
detect whether the process runs on GCP. A bare
``urllib.request.urlopen`` silently honours ``http_proxy`` /
``HTTP_PROXY`` env vars — a poisoned proxy could intercept the probe
and forge a 200. The hardened path builds an explicit opener with an
empty ``ProxyHandler({})`` so no proxy is consulted for this call.
"""

from __future__ import annotations

import urllib.request
from unittest.mock import MagicMock, patch

from fluid_build.providers.gcp.util.auth import _check_metadata_service


def test_imds_probe_uses_no_proxy_opener(monkeypatch):
    """Even with ``http_proxy`` set in the environment, the probe must
    build an opener with an *empty* ProxyHandler — i.e. it must not
    route the IMDS request through the proxy."""
    monkeypatch.setenv("http_proxy", "http://attacker.example:3128")
    monkeypatch.setenv("https_proxy", "http://attacker.example:3128")

    captured_proxy_handlers = []

    real_build_opener = urllib.request.build_opener

    def spy_build_opener(*handlers):
        for h in handlers:
            if isinstance(h, urllib.request.ProxyHandler):
                captured_proxy_handlers.append(h)
        return real_build_opener(*handlers)

    fake_resp = MagicMock()
    fake_resp.status = 200
    fake_resp.__enter__.return_value = fake_resp
    fake_resp.__exit__.return_value = False

    with patch("urllib.request.build_opener", side_effect=spy_build_opener):
        with patch.object(urllib.request.OpenerDirector, "open", return_value=fake_resp):
            result = _check_metadata_service()

    assert result is True
    # An explicit ProxyHandler was passed to build_opener...
    assert captured_proxy_handlers, "probe must build an explicit ProxyHandler opener"
    # ...and it carries NO proxies (empty dict == bypass all proxies).
    for handler in captured_proxy_handlers:
        assert handler.proxies == {}, "IMDS probe ProxyHandler must be empty (no proxy)"


def test_imds_probe_returns_false_on_error():
    """Network failure → False (not on GCP / unreachable)."""
    with patch.object(
        urllib.request.OpenerDirector,
        "open",
        side_effect=OSError("unreachable"),
    ):
        assert _check_metadata_service() is False
