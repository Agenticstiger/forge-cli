# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid profile``: aggregate statistics of an expose's records, read where they live.

FLUX keeps its generator away from the data (flux-engine ADR 0003, amended
2026-11-16): the engine reads only digest-pinned documents, and a profiler is
the one part that reads records. It runs inside the data owner's environment,
and what it lets out is a profile document (flux-engine
``docs/spec/PROFILE-SPEC.md``, format 1-0-0) of aggregates that passed the
disclosure rules. Nothing else leaves: no record, no sample of records, and no
per-record value. This package is that profiler, shipped as forge-cli's
``[profile]`` extra because forge-cli already holds the connectors.

The package has six parts, each its own module:

* :mod:`~fluid_build.profiler.sources` reads the columns of one table, through
  the DuckDB scanners ``fluid init --discover`` uses, or a newline-delimited
  JSON slice read the way the engine reads one.
* :mod:`~fluid_build.profiler._stats` is the one seam to FLUX's statistic
  registry (``flux-stats``). Every statistic is formed by the registry's
  definition, never here, so a profile and a fidelity check of the same records
  hold the same numbers (one definition per statistic).
* :mod:`~fluid_build.profiler.disclosure` decides what may be published, with
  ACRO's output-checking defaults.
* :mod:`~fluid_build.profiler.document` writes the canonical document and its
  digest.
* :mod:`~fluid_build.profiler.bundle` finds the fidelity block a FLUX bundle's
  Simulation declares for the expose, read the way the engine reads it, for
  ``--bundle``.
* :mod:`~fluid_build.profiler.runner` puts them in order for one expose.

Nothing here is imported on the ``fluid --help`` path; ``cli/profile_cmd.py``
imports the runner only when ``fluid profile`` runs.
"""
