# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Iceberg table maintenance, declared per expose (``exposes[].iceberg.maintenance``).

Where it fits in the pipeline:

* the contract declares, per Iceberg expose, what keeps its table healthy (compaction, the
  manifests, the snapshots' expiry, the orphans, a plugin's actions such as a GDPR erasure),
  with a schedule and the retention (:mod:`fluid_build.maintenance.spec`);
* ``fluid validate`` checks it, and in its plugin phase loads each action's kind and checks the
  statements it returns (``contract_iceberg_maintenance``, ``contract_maintenance_actions``);
* ``backend: spark``: ``fluid plan`` lists a ``spark.maintain`` action per table, which
  ``planDigest`` covers; ``fluid apply`` uploads its job config beside the writer build's
  (OpenTofu, as the build's own); ``fluid generate schedule`` writes a maintenance DAG per
  table, whose task runs ``fluid apply --mode maintain --expose-id <expose>``. That mode deploys
  nothing: it starts forge's Spark runner on the writer build's runtime, as its role, with the
  config the last apply deployed, refused unless it is the one the contract compiles to now.
  The runner stamps its commits as it stamps the build's, and the build's stream keeps running;
* ``backend: glue``: ``fluid apply`` provisions the Glue Data Catalog's table optimizers
  (``aws_glue_catalog_table_optimizer``), which AWS runs on its own schedule.

A package adds an action kind through the ``fluid_build.maintenance_actions`` entry-point group;
its base class is :class:`MaintenanceAction`.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from fluid_build.maintenance.statements import sql_literal


class MaintenanceAction:
    """Base class of a maintenance action kind: the SDK surface of
    ``fluid_build.maintenance_actions``.

    Register it in the package's ``pyproject.toml``::

        [project.entry-points."fluid_build.maintenance_actions"]
        gdpr-erasure = "acme_pack.maintenance:GdprErasure"

    and a contract action with ``kind: gdpr-erasure`` runs its statements. The entry point may
    name a class (built with no arguments), an instance or a function that returns one, and it
    need not subclass this: forge calls the same methods on any object that has them.
    """

    def check(self, options: Mapping[str, Any]) -> Sequence[str]:
        """Problems with the action's ``options``, each a sentence; none when it can run.
        Never quote a value that may be a secret."""
        return ()

    def statements(self, table: str, options: Mapping[str, Any]) -> Sequence[str]:
        """The SQL to run on ``table``, named as the runner's catalog holds it
        (``fluid.<database>.<table>``): each one ``DELETE``, ``UPDATE`` or ``MERGE`` with that
        table as its target, which may read others. Called when the job config is compiled
        (``fluid plan`` and ``fluid apply``): the statements are part of the config, which the
        plan binds, and forge's Spark runner executes them in order, before compaction, each
        commit stamped as the runner stamps its writes. A statement holds no comment and no
        backslash; write an option's value into it with :func:`sql_literal`."""
        raise NotImplementedError(f"{type(self).__name__}.statements")


__all__ = ["MaintenanceAction", "sql_literal"]
