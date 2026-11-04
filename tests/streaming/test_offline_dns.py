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

"""The streaming unit tests never resolve real DNS."""

import socket

import pytest

from fluid_build._net import hostname_scope


def test_a_name_fails_at_once_and_is_recorded(offline_dns):
    assert hostname_scope("minio.lake.svc.cluster.local") == "unresolved"
    assert offline_dns == ["minio.lake.svc.cluster.local"]


def test_an_ip_literal_resolves_to_itself_without_a_lookup(offline_dns):
    assert hostname_scope("10.0.0.5") == "non_public"
    assert hostname_scope("8.8.8.8") == "public"
    assert offline_dns == []
    with pytest.raises(socket.gaierror):
        socket.getaddrinfo("example.invalid", 80)
