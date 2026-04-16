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

"""Legacy copilot scaffold and analysis helpers kept for compatibility."""

from __future__ import annotations

__all__ = [
    "CopilotLegacyScaffoldMixin",
]


import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

from fluid_build.cli.console import cprint
from fluid_build.cli.console import error as console_error
from fluid_build.cli.forge_copilot_runtime import normalize_provider_name, normalize_template_name
from fluid_build.cli.forge_copilot_taxonomy import (
    format_use_case_label,
    normalize_copilot_context,
    normalize_use_case,
)
from fluid_build.cli.forge_ui import (
    show_copilot_analysis,
    show_next_steps_panel,
)

LOG = logging.getLogger("fluid.cli.forge")


class CopilotLegacyScaffoldMixin:
    """Legacy copilot generation helpers still exercised by tests."""

    def _sanitize_project_name(self, goal: str) -> str:
        from .forge_validation import sanitize_project_name

        return sanitize_project_name(goal, strict=False)

    def _create_with_forge_engine(
        self, project_config: Dict[str, Any], dry_run: bool = False
    ) -> bool:
        try:
            from fluid_build.forge import ForgeEngine

            if self.console:
                with self.console.status(
                    "[bold blue]🔧 Generating project...",
                    spinner="dots",
                ) as status:
                    status.update(
                        f"[dim]Using template: {project_config.get('template', 'N/A')}[/dim]"
                    )
                    engine = ForgeEngine()
                    success = engine.run_with_config(project_config, dry_run=dry_run)
                    if success:
                        status.update("[green]✓ Project generated successfully[/green]")
                return success

            cprint("🔧 Generating project...")
            engine = ForgeEngine()
            success = engine.run_with_config(project_config, dry_run=dry_run)
            if success:
                cprint("✓ Project generated successfully")
            return success
        except Exception as exc:
            if self.console:
                self.console.print(f"[red]❌ ForgeEngine integration failed: {exc}[/red]")
            else:
                console_error(f"ForgeEngine integration failed: {exc}")
            return False

    def _analyze_requirements(self, context: Dict[str, Any]) -> Dict[str, Any]:
        context = normalize_copilot_context(context)
        goal = context.get("project_goal", "").lower()
        data_sources = context.get("data_sources", "").lower()
        complexity = context.get("complexity", "intermediate")

        suggestions = {
            "recommended_template": self.recommend_template_for_use_case(context),
            "recommended_provider": "local",
            "recommended_patterns": [],
            "architecture_suggestions": [],
            "best_practices": [],
        }

        normalized_use_case = normalize_use_case(context.get("use_case"))
        if normalized_use_case == "ml_pipeline":
            suggestions["recommended_patterns"].append("feature_store")
            suggestions["architecture_suggestions"].append("Consider MLflow for model versioning")
        elif normalized_use_case == "analytics":
            suggestions["recommended_patterns"].append("dimensional_modeling")
            suggestions["architecture_suggestions"].append(
                "Use layered architecture (bronze/silver/gold)"
            )
        elif normalized_use_case == "streaming":
            suggestions["recommended_patterns"].append("event_sourcing")
            suggestions["architecture_suggestions"].append("Consider Apache Kafka for streaming")
        elif any(word in goal for word in ["ml", "machine learning", "model", "prediction"]):
            suggestions["recommended_template"] = "ml_pipeline"
            suggestions["recommended_patterns"].append("feature_store")
            suggestions["architecture_suggestions"].append("Consider MLflow for model versioning")
        elif any(word in goal for word in ["dashboard", "reporting", "analytics", "visualization"]):
            suggestions["recommended_template"] = "analytics"
            suggestions["recommended_patterns"].append("dimensional_modeling")
            suggestions["architecture_suggestions"].append(
                "Use layered architecture (bronze/silver/gold)"
            )
        elif any(word in goal for word in ["real-time", "streaming", "live"]):
            suggestions["recommended_template"] = "streaming"
            suggestions["recommended_patterns"].append("event_sourcing")
            suggestions["architecture_suggestions"].append("Consider Apache Kafka for streaming")

        if "bigquery" in data_sources:
            suggestions["recommended_provider"] = "gcp"
            suggestions["best_practices"].append("Use BigQuery partitioning for large datasets")
        elif "snowflake" in data_sources:
            suggestions["recommended_provider"] = "snowflake"
            suggestions["best_practices"].append("Leverage Snowflake's auto-scaling features")
        elif any(word in data_sources for word in ["s3", "redshift", "athena"]):
            suggestions["recommended_provider"] = "aws"
            suggestions["best_practices"].append("Use S3 for cost-effective data lake storage")

        if complexity == "simple":
            suggestions["architecture_suggestions"].append("Start with single-layer architecture")
            suggestions["best_practices"].append("Focus on essential features first")
        elif complexity == "advanced":
            suggestions["recommended_patterns"].extend(["data_mesh", "event_driven"])
            suggestions["architecture_suggestions"].append("Consider microservices architecture")

        return suggestions

    def _show_ai_analysis(
        self,
        context: Dict[str, Any],
        suggestions: Dict[str, Any],
        generation_result: Optional[Any] = None,
    ) -> None:
        if not self.console:
            return
        show_copilot_analysis(
            self.console,
            context=context,
            suggestions=suggestions,
            use_case_label=format_use_case_label(
                context.get("use_case", "analytics"),
                context.get("use_case_other"),
            ),
            memory_lines=self._build_memory_guidance_lines(generation_result),
        )

    async def _generate_intelligent_structure(
        self, target_dir: Path, context: Dict[str, Any], suggestions: Dict[str, Any]
    ) -> None:
        if not self.console:
            return
        with self.console.status("[bold blue]🤖 AI is crafting your project..."):
            target_dir.mkdir(parents=True, exist_ok=True)
            contract = self._generate_intelligent_contract(context, suggestions)
            with open(target_dir / "contract.fluid.yaml", "w") as handle:
                handle.write(contract)
            self._generate_supporting_files(target_dir, context, suggestions)
            with open(target_dir / "README.md", "w") as handle:
                handle.write(self._generate_intelligent_readme(context, suggestions))
        self.console.print(f"[green]✅ AI-generated project created at {target_dir}[/green]")

    def _generate_intelligent_contract(
        self, context: Dict[str, Any], suggestions: Dict[str, Any]
    ) -> str:
        goal = context.get("project_goal", "Data Product")
        use_case = format_use_case_label(
            context.get("use_case", "analytics"),
            context.get("use_case_other"),
        )
        provider = suggestions["recommended_provider"]
        return f"""# FLUID Contract - AI Generated
# Goal: {goal}
# Generated by AI Copilot on {Path.cwd()}

meta:
  name: {goal.lower().replace(' ', '-')}
  version: "1.0.0"
  description: "{goal}"
  owner: data-team
  domain: {json.dumps(use_case)}

sources:
  - name: raw_data
    type: table
    description: "Primary data source for {goal}"
    location: "{{{{ provider.dataset }}}}.raw_data"

transforms:
  - name: clean_data
    type: sql
    description: "Data cleaning and validation"
    materialization: table
    sources:
      - ref("raw_data")
    sql: |
      SELECT
        *,
        CURRENT_TIMESTAMP() as processed_at
      FROM {{{{ ref("raw_data") }}}}
      WHERE data_quality_score > 0.8

  - name: aggregated_metrics
    type: sql
    description: "Business metrics aggregation"
    materialization: table
    sources:
      - ref("clean_data")
    sql: |
      SELECT
        date_trunc(created_date, DAY) as metric_date,
        count(*) as total_records,
        avg(value) as avg_value
      FROM {{{{ ref("clean_data") }}}}
      GROUP BY 1
      ORDER BY 1 DESC

exposures:
  - name: {goal.lower().replace(' ', '_')}_dataset
    type: table
    description: "Final dataset for {goal}"
    sources:
      - ref("aggregated_metrics")

provider:
  type: {provider}
  {"# GCP-specific configuration" if provider == "gcp" else ""}
  {"project: your-gcp-project" if provider == "gcp" else ""}
  {"dataset: your_dataset" if provider == "gcp" else ""}

quality:
  - name: data_freshness
    description: "Ensure data is updated daily"
    test: "max_age(1, 'day')"

  - name: completeness_check
    description: "Check for required fields"
    test: "not_null(['id', 'created_date'])"
"""

    def _generate_supporting_files(
        self, target_dir: Path, context: Dict[str, Any], suggestions: Dict[str, Any]
    ) -> None:
        gitignore = """
# FLUID Build artifacts
runtime/
.fluid/
*.log

# Provider-specific
.env
credentials.json

# IDE
.vscode/
.idea/
*.swp

# OS
.DS_Store
Thumbs.db
"""
        with open(target_dir / ".gitignore", "w") as handle:
            handle.write(gitignore.strip())

        makefile = """
# FLUID Project Makefile
# Generated by AI Copilot

.PHONY: validate plan apply clean help

help:
\t@echo "Available commands:"
\t@echo "  validate  - Validate the contract"
\t@echo "  plan      - Generate execution plan"
\t@echo "  apply     - Apply the plan"
\t@echo "  clean     - Clean up artifacts"

validate:
\tfluid validate contract.fluid.yaml

plan:
\tfluid plan contract.fluid.yaml --out runtime/plan.json

apply:
\tfluid apply runtime/plan.json

clean:
\trm -rf runtime/
\trm -f *.log
"""
        with open(target_dir / "Makefile", "w") as handle:
            handle.write(makefile)

    def _generate_intelligent_readme(
        self, context: Dict[str, Any], suggestions: Dict[str, Any]
    ) -> str:
        goal = context.get("project_goal", "Data Product")
        use_case = format_use_case_label(
            context.get("use_case", "analytics"),
            context.get("use_case_other"),
        )
        readme = f"""# {goal}

> AI-Generated FLUID Data Product

## Overview

This project was intelligently generated by FLUID AI Copilot based on your requirements:

- **Goal:** {goal}
- **Use Case:** {use_case}
- **Data Sources:** {context.get('data_sources', 'Various sources')}
- **Recommended Template:** {suggestions['recommended_template']}

## AI Insights

### Architecture Recommendations
"""
        for suggestion in suggestions["architecture_suggestions"]:
            readme += f"- {suggestion}\n"

        readme += "\n### Best Practices\n"
        for practice in suggestions["best_practices"]:
            readme += f"- {practice}\n"

        readme += f"""
## Quick Start

1. **Validate your contract:**
   ```bash
   fluid validate contract.fluid.yaml
   ```

2. **Generate execution plan:**
   ```bash
   fluid plan contract.fluid.yaml --out runtime/plan.json
   ```

3. **Apply the plan:**
   ```bash
   fluid apply runtime/plan.json
   ```

## Project Structure

```
{goal.lower().replace(' ', '-')}/
├── contract.fluid.yaml    # Main FLUID contract
├── README.md             # This file
├── Makefile             # Common tasks
└── .gitignore           # Git ignore rules
```

## Next Steps

1. Customize the contract based on your specific data sources
2. Add provider-specific configurations
3. Define data quality rules
4. Set up CI/CD pipeline with `fluid scaffold-ci`

## AI Copilot Generated

This project structure was intelligently created by FLUID AI Copilot.
For more advanced features, explore:

- `fluid forge --mode agent --agent domain-expert` for domain-specific assistance
- `fluid market` for discovering existing data products

---

*Generated by FLUID AI Copilot - The one command you need to know: `fluid forge`*
"""
        return readme

    def _show_next_steps(
        self, target_dir: Path, context: Dict[str, Any], suggestions: Dict[str, Any]
    ) -> None:
        if not self.console:
            return
        show_next_steps_panel(
            self.console,
            target_dir=target_dir,
            provider=suggestions["recommended_provider"],
        )
