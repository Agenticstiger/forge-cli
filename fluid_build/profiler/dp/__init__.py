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

Nothing here reads a record, and nothing here imports a heavy package at module
scope.
"""
