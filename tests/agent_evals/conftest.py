# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Keep pytest out of live-eval output: an agent's workspace may hold its own tests."""

collect_ignore_glob = ["runs/*"]
