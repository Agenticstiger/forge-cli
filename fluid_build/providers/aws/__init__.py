# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""AWS provider package — comprehensive AWS data-platform support.

Surfaces:
* :class:`AwsProvider` — plan / restore_ddl / cleanup_backups for
  S3 / Glue / Athena / Lambda / EventBridge / Redshift.
* :class:`RedshiftProvider` — owns Redshift's rollback DDL (DROP +
  CTAS) which is incompatible with the rest of AWS's S3 prefix-copy
  semantics. Not a registered provider (retired as one, see
  ``providers.RETIRED_PROVIDERS``); the rollback dispatch imports it directly.

The legacy ``aws.py::AWSProvider`` (1601 LOC, registered as
``LegacyAWSProvider``) was deleted — its rollback methods migrated
into :class:`AwsProvider` and its ``plan`` method was redundant with
the planner module's 6-phase scaffold.
"""

from fluid_build.providers import register_provider

from .provider import AwsProvider
from .redshift_provider import RedshiftProvider

register_provider("aws", AwsProvider)

__all__ = ["AwsProvider", "RedshiftProvider"]
