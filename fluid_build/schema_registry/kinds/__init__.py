# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""One plugin per schema registry kind.

A *kind* is a way a contract names a registry (``registry.kind``). Everything that
differs between kinds lives in one :class:`RegistryKind` subclass, in one module here:

* its spelling rules: the extra fields it takes, its subject name pattern, the
  compatibility modes it accepts and the one it defaults to;
* where it lives (:meth:`RegistryKind.location`) and how a registry block for it is
  written (:meth:`RegistryKind.registry_block`);
* its egress rules and, for a produced subject, its operator-side checks;
* how it is read at plan and apply: which client, which call, and whether the
  registry answers a dry-run compatibility check;
* which cloud's OpenTofu plugin registers its produced subjects
  (``registering_cloud``).

The core (``reference``, ``verify``, ``policy``, ``produced``, ``importer``) asks the
kind and holds no ``kind == "..."`` branch.

Adding a kind takes four steps:

1. A module here with a :class:`RegistryKind` subclass (import ``clients``, ``policy``,
   ``produced`` and the ``iac`` package inside methods: the package stays light, and
   nothing here is on the ``fluid --help`` cold path).
2. One :func:`register_kind` line at the bottom of this file. Registration order is
   the order messages list the kinds in.
3. The kind's name in the schema's ``$defs/schemaRegistry`` ``kind`` enum.
4. Its emitter, in the OpenTofu plugin of the cloud named by ``registering_cloud``.

Third-party discovery through entry points is deliberately absent: the schema's
``kind`` enum decides which kinds a contract can name, so an out-of-tree kind could
never be written in a valid contract.
"""

from __future__ import annotations

from ._base import RegistryKind, all_kinds, get_kind, names, register_kind
from .confluent import ConfluentKind
from .glue import GlueKind

register_kind(GlueKind())
register_kind(ConfluentKind())

__all__ = [
    "ConfluentKind",
    "GlueKind",
    "RegistryKind",
    "all_kinds",
    "get_kind",
    "names",
    "register_kind",
]
