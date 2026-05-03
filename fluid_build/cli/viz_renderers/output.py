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

"""Output writer — multi-format dispatcher (DOT / SVG / PNG / HTML / JSON). Extracted from ``viz_graph.py`` so the format-specific code stays self-contained.

The public entry point :func:`_write_output` is re-imported by
``viz_graph`` at top level so existing test patches that target
``fluid_build.cli.viz_graph._write_output`` still resolve.
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple, Union

from fluid_build.cli import viz_graph as _viz
from fluid_build.cli._common import CLIError
from fluid_build.cli._logging import error, info, warn
from fluid_build.cli.viz_graph import (
    GraphConfig,
    GraphMetrics,
    ProductionLogger,
)

# Reference module-level helpers via the ``_viz`` alias rather than
# importing them directly. Tests routinely
# ``patch("fluid_build.cli.viz_graph._prepare_output_directory", ...)``
# — patching binds the new value on the ``viz_graph`` module's
# namespace, which only takes effect for callers who do attribute
# access at call-time. Direct ``from … import name`` would snapshot
# the original at import time and miss every patch.


def _write_output(
    dot: str,
    config: GraphConfig,
    metrics: GraphMetrics,
    logger: logging.Logger,
) -> None:
    """Enhanced output writer with security hardening and better error handling."""
    secure_logger = ProductionLogger(logger)

    try:
        out_path = _viz._prepare_output_directory(config.output_path, config.force_overwrite)

        # Track input metrics
        metrics.dot_size = len(dot.encode("utf-8"))

        # If DOT requested or Graphviz not available, write DOT
        graphviz_available, graphviz_version = _viz._check_graphviz_installation()

        if config.format == "dot" or not graphviz_available:
            if config.format != "dot" and not graphviz_available:
                warn(
                    logger,
                    "graphviz_not_available_writing_dot",
                    out=str(out_path.with_suffix(".dot")),
                )
                out_path = out_path.with_suffix(".dot")

            # Secure file write
            from fluid_build.cli.security import write_file_secure

            write_file_secure(out_path, dot, "DOT graph file")
            metrics.output_size = _viz._get_file_size(out_path)

            if not config.quiet:
                info(
                    logger,
                    "viz_graph_output_written",
                    out=str(out_path),
                    fmt="dot",
                    size_bytes=metrics.output_size,
                )

            if config.open_when_done:
                _viz._shell_open(out_path, logger)
            return

        # Render using Graphviz with security safeguards
        fmt_map = {"svg": "svg", "png": "png", "html": "svg"}
        gv_fmt = fmt_map.get(config.format, "svg")

        # Validate format to prevent injection
        if not gv_fmt.isalnum():
            raise ValueError(f"Invalid Graphviz format: {gv_fmt}")

        # Use secure temporary file for DOT input
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".dot", delete=False, encoding="utf-8"
        ) as tmp_dot:
            tmp_dot.write(dot)
            tmp_dot_path = tmp_dot.name

        try:
            result_file = out_path if config.format != "html" else out_path.with_suffix(".svg")

            # Build Graphviz command with input validation
            cmd = ["dot", f"-T{gv_fmt}", tmp_dot_path, "-o", str(result_file)]

            # Validate and sanitize custom Graphviz args
            if config.graphviz_args:
                sanitized_args = []
                for arg in config.graphviz_args:
                    # Allow only safe Graphviz options
                    if (
                        arg.startswith("-")
                        and len(arg) > 1
                        and arg[1:].replace("=", "").replace(":", "").isalnum()
                    ):
                        sanitized_args.append(arg)
                    else:
                        secure_logger.log_safe(
                            "warning", f"Skipping potentially unsafe Graphviz argument: {arg}"
                        )

                if sanitized_args:
                    # Insert custom args before the -o option
                    cmd = cmd[:-2] + sanitized_args + cmd[-2:]

            subprocess.run(
                cmd,
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
                cwd=Path.cwd(),
                env={**os.environ, "PATH": os.environ.get("PATH", "")},
            )

            if config.format == "html":
                # Wrap SVG into HTML with secure file operations
                from fluid_build.cli.security import read_file_secure, write_file_secure

                svg_content = read_file_secure(result_file, "generated SVG")
                html_content = _viz._create_html_wrapper(svg_content, config, metrics)
                write_file_secure(out_path, html_content, "HTML wrapper")
                result_file.unlink(missing_ok=True)

            metrics.output_size = _viz._get_file_size(out_path)

            if not config.quiet:
                info(
                    logger,
                    "viz_graph_output_written",
                    out=str(out_path),
                    fmt=config.format,
                    size_bytes=metrics.output_size,
                    graphviz_version=graphviz_version,
                )

            if config.open_when_done:
                _viz._shell_open(out_path, logger)

        except subprocess.TimeoutExpired:
            error(logger, "graphviz_timeout", timeout_seconds=30)
            raise CLIError(1, "graphviz_timeout", {"timeout": 30})
        except subprocess.CalledProcessError as e:
            secure_logger.log_safe(
                "error", "Graphviz render failed", stderr=e.stderr, returncode=e.returncode
            )
            # Fallback: write DOT file
            from fluid_build.cli.security import write_file_secure

            dot_path = out_path.with_suffix(".dot")
            write_file_secure(dot_path, dot, "fallback DOT file")
            warn(logger, "falling_back_to_dot_output", out=str(dot_path))
            if config.open_when_done:
                _viz._shell_open(dot_path, logger)
        finally:
            # Clean up temporary file securely
            try:
                temp_path = Path(tmp_dot_path)
                if temp_path.exists():
                    temp_path.unlink()
            except Exception:
                pass

    except Exception as e:
        secure_logger.log_safe(
            "error", "Output write failed", error=str(e), output_path=config.output_path
        )
        raise CLIError(1, "output_write_failed", {"error": str(e), "path": config.output_path})
