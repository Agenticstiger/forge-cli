# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The parts of a release under differential privacy.

The randomness a release draws and the exact sampler that turns it into noise
live here, each as its own module:

* :mod:`~fluid_build.profiler.dp.randomness` is the seeded randomness source.
* :mod:`~fluid_build.profiler.dp.discrete_gaussian` is the exact discrete
  Gaussian sampler.
* :mod:`~fluid_build.profiler.dp.consistency` makes the noisy count tables agree
  exactly, and writes them as decimal text that still agrees.
* :mod:`~fluid_build.profiler.dp.bounding` reads the privacy unit from the contract and
  bounds what one unit contributes to a release.
* :mod:`~fluid_build.profiler.dp.selection` chooses the clique marginals a release
  measures, with OpenDP's AIM, from the optional ``[privacy]`` extra.
* :mod:`~fluid_build.profiler.dp.record` writes and reads the owner-only release
  record, so a release can be re-derived inside the data owner's boundary.
* :mod:`~fluid_build.profiler.dp.release` plans a release before any record is read, then
  reads the table once, bounds, chooses the cliques, counts, adds noise, makes the tables
  agree and builds the profile document (format 1-2-0).

Nothing here prints, logs or records anything that depends on the records, outside the
noise, and nothing here imports a heavy package at module scope.
"""
