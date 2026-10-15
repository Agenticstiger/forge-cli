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

"""Discoverer Protocol + registry."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol


@dataclass(frozen=True)
class DiscoveredColumn:
    name: str
    type: str
    nullable: bool = True
    classifications: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class DiscoveredStream:
    """One source stream (table / file / topic) the discoverer found."""

    name: str  # e.g., "public.orders" for postgres, "orders.csv" for filesystem
    columns: List[DiscoveredColumn]
    metadata: Dict[str, Any] = field(default_factory=dict)


class Discoverer(Protocol):
    scheme: str

    def discover(self, uri: str) -> List[DiscoveredStream]: ...


# ── Registry ────────────────────────────────────────────────────────────


DISCOVERERS: Dict[str, Discoverer] = {}


def register_discoverer(scheme: str, discoverer: Discoverer) -> None:
    """Route ``<scheme>://`` URIs to ``discoverer``.

    Register under an RFC 3986 scheme: a letter, then letters, digits, ``+``,
    ``-`` or ``.`` (RFC 3986, section 3.1). A key that is not one (``my_db``,
    ``a_b``, ``jdbc:postgresql``) still routes its URIs here, but
    ``fluid init --discover`` does not show such a URI as a URI: it masks it
    from its first ``:`` to its last ``@``, user name included. It reads the
    text after the ``://`` as it would be read after an RFC 3986 scheme, and
    refuses the URI before any connection wherever it refuses that text after
    one: by the rules for a database URI when ``discoverer`` is a
    ``JdbcDiscoverer``, by the rules for a URL otherwise. The contracts are
    written from the same reading, so they keep no user:password part, except
    where a URL is used as given: a user name that holds a raw ``/`` before its
    ``:`` leaves the rest in the path, as it does after ``https://``.
    """
    DISCOVERERS[scheme] = discoverer


def get_discoverer(uri: str) -> Optional[Discoverer]:
    """Pick the discoverer that matches the URI's scheme."""
    if "://" not in uri:
        return DISCOVERERS.get("file")
    scheme = uri.split("://", 1)[0]
    # mongodb+srv://, postgresql:// etc. — strip drivers from the scheme.
    base = scheme.split("+")[0]
    if base in ("postgresql", "postgres"):
        return DISCOVERERS.get("postgres")
    if base in ("mysql", "mariadb"):
        return DISCOVERERS.get("mysql")
    if base in ("sqlite",):
        return DISCOVERERS.get("sqlite")
    if base in ("s3", "gs", "gcs", "azure", "https", "http", "file"):
        return DISCOVERERS.get("file")
    return DISCOVERERS.get(base)


# Bootstrap is performed in `fluid_build/cli/discover/__init__.py` after all
# discoverer modules are imported, to avoid a circular import here.
