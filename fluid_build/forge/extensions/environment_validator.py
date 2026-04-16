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

"""
Environment Validator Extension for FLUID Forge

Validates development environment setup and provides recommendations
for missing tools and configuration.
"""

import shutil
import subprocess
from typing import Any, Dict, List

from ..core.interfaces import Extension, GenerationContext


class EnvironmentValidatorExtension(Extension):
    """Extension for validating development environment"""

    def get_metadata(self) -> Dict[str, Any]:
        return {
            "name": "Environment Validator",
            "description": "Validate development environment setup",
            "version": "1.0.0",
            "author": "FLUID Build Team",
        }

    def on_forge_start(self, context: GenerationContext) -> None:
        """Validate environment when forge starts"""
        from rich import print as rprint

        # Check basic tools
        missing_tools = self._check_basic_tools()
        if missing_tools:
            rprint(f"[yellow]⚠️ Missing tools: {', '.join(missing_tools)}[/yellow]")

        # Check Python environment
        python_issues = self._check_python_environment()
        if python_issues:
            rprint("[yellow]⚠️ Python environment issues detected[/yellow]")
            for issue in python_issues:
                rprint(f"  • {issue}")

    def on_template_selected(self, template, context: GenerationContext) -> None:
        """Validate environment for specific template"""
        template_name = context.project_config.get("template")

        # Template-specific validation
        if template_name == "analytics":
            self._validate_analytics_environment()
        elif template_name == "ml_pipeline":
            self._validate_ml_environment()
        elif template_name == "streaming":
            self._validate_streaming_environment()

    def _check_basic_tools(self) -> List[str]:
        """Check for basic development tools"""
        required_tools = ["git", "python3", "pip"]
        missing = []

        for tool in required_tools:
            if not shutil.which(tool):
                missing.append(tool)

        return missing

    def _check_python_environment(self) -> List[str]:
        """Check Python environment setup"""
        issues = []

        try:
            # Check Python version
            result = subprocess.run(["python3", "--version"], capture_output=True, text=True)
            if result.returncode == 0:
                version_str = result.stdout.strip()
                # Extract version number
                if "Python 3." in version_str:
                    version_parts = version_str.split("Python 3.")[1].split(".")
                    if len(version_parts) >= 1:
                        minor_version = int(version_parts[0])
                        if minor_version < 8:
                            issues.append(f"Python version {version_str} is below minimum 3.8")
                else:
                    issues.append("Could not determine Python version")
            else:
                issues.append("Python3 not accessible")
        except Exception:
            issues.append("Could not check Python version")

        return issues

    def _validate_analytics_environment(self) -> None:
        """Validate environment for analytics template"""
        from rich import print as rprint

        # Check for analytics-specific tools
        analytics_tools = ["dbt", "sql"]
        missing_analytics = []

        for tool in analytics_tools:
            if not shutil.which(tool):
                missing_analytics.append(tool)

        if missing_analytics:
            rprint(
                f"[yellow]💡 Consider installing analytics tools: {', '.join(missing_analytics)}[/yellow]"
            )

    def _validate_ml_environment(self) -> None:
        """Validate environment for ML template"""
        from rich import print as rprint

        # Check for ML-specific tools
        try:
            import numpy
            import pandas
            import sklearn

            rprint("[green]✅ ML libraries available[/green]")
        except ImportError:
            rprint(
                "[yellow]💡 Consider installing ML libraries: scikit-learn, numpy, pandas[/yellow]"
            )

    def _validate_streaming_environment(self) -> None:
        """Validate environment for streaming template"""
        from rich import print as rprint

        # Check for streaming-specific tools
        streaming_tools = ["docker", "docker-compose"]
        missing_streaming = []

        for tool in streaming_tools:
            if not shutil.which(tool):
                missing_streaming.append(tool)

        if missing_streaming:
            rprint(f"[yellow]💡 Streaming requires: {', '.join(missing_streaming)}[/yellow]")
