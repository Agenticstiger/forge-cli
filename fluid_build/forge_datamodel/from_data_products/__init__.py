# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Forge composition mode — author ADP/CDP from existing upstream products."""

from .pipeline import (  # noqa: F401
    CompositionContext,
    UpstreamProduct,
    load_upstream_products,
    propagate_pii_classifications,
    resolve_upstream_paths,
    run_from_data_products,
)
