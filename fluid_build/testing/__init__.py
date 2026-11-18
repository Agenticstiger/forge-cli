# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A test kit for packages that plug into ``fluid``.

An outside package (an IaC emitter, a provider) cannot import forge-cli's own
``tests/`` tree: it is not in the wheel. This package is the part of that tree a
plugin author needs, shipped with the CLI:

* :mod:`fluid_build.testing.http_fake`: ``FakeHttpServer``, a loopback HTTP
  server with a route table, a request log and failure injection, over TLS when
  you hand it an ``ssl.SSLContext``.
* :mod:`fluid_build.testing.cli`: ``run_fluid``, which runs the real CLI in
  process and returns its exit code and output.
* :mod:`fluid_build.testing.env`: ``isolated_env``, a context manager that gives
  a test its own HOME and working directory and a clean plugin-policy
  environment, and restores everything afterwards.
* :mod:`fluid_build.testing.entry_points`: ``installed_plugin``, a context
  manager that installs a real ``.dist-info`` with entry points and resets the
  plugin registries, so ``fluid`` discovers a plugin the way it discovers an
  installed package.

Rules the kit keeps:

* Standard library only, and no ``pytest`` import. The pieces are context
  managers and plain functions; wrap them in a fixture if you want one.
* Nothing in the CLI imports it, so ``fluid --help`` and ``fluid providers`` never
  load it (``tests/testing/test_cold_path.py`` pins this).
* Import each module on its own; this ``__init__`` imports nothing.
"""
