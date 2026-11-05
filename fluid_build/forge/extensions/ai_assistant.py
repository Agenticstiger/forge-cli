# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""
AI Assistant Extension for FLUID Forge

Provides AI-powered recommendations and assistance during project creation.
"""

from typing import Any, Dict, List

from ..core.interfaces import Extension, GenerationContext


class AIAssistantExtension(Extension):
    """Extension providing AI-powered assistance"""

    def get_metadata(self) -> Dict[str, Any]:
        return {
            "name": "AI Assistant",
            "description": "AI-powered recommendations and assistance",
            "version": "1.0.0",
            "author": "FLUID Build Team",
        }

    def on_template_selected(self, template, context: GenerationContext) -> None:
        """Provide AI recommendations based on template selection"""
        from rich import print as rprint

        template_name = context.project_config.get("template")
        domain = context.project_config.get("domain", "")

        # Generate contextual recommendations
        recommendations = self._get_template_recommendations(template_name, domain)

        if recommendations:
            rprint("\n[cyan]🤖 AI Assistant Recommendations:[/cyan]")
            for rec in recommendations:
                rprint(f"  💡 {rec}")

    def modify_prompts(
        self, prompts: List[Dict[str, Any]], context: GenerationContext
    ) -> List[Dict[str, Any]]:
        """Add AI-powered prompts"""
        # Add smart defaults based on context
        enhanced_prompts = prompts.copy()

        # Add AI suggestion prompt
        ai_prompt = {
            "name": "use_ai_suggestions",
            "type": "confirm",
            "message": "Use AI-powered configuration suggestions?",
            "default": True,
        }

        enhanced_prompts.insert(0, ai_prompt)
        return enhanced_prompts

    def _get_template_recommendations(self, template_name: str, domain: str) -> List[str]:
        """Generate template-specific recommendations"""
        recommendations = []

        if template_name == "analytics":
            recommendations.extend(
                [
                    "Consider using dimensional modeling for scalable analytics",
                    "Set up data quality monitoring from day one",
                    "Plan your BI tool integration early",
                ]
            )

            if "finance" in domain.lower():
                recommendations.append(
                    "Consider regulatory compliance requirements for financial data"
                )

        elif template_name == "ml_pipeline":
            recommendations.extend(
                [
                    "Start with experiment tracking (MLflow) for reproducibility",
                    "Plan your feature store architecture early",
                    "Consider model monitoring and drift detection",
                ]
            )

        elif template_name == "streaming":
            recommendations.extend(
                [
                    "Design for exactly-once processing guarantees",
                    "Plan your schema evolution strategy",
                    "Consider backpressure handling mechanisms",
                ]
            )

        elif template_name == "etl_pipeline":
            recommendations.extend(
                [
                    "Implement comprehensive error handling and retry logic",
                    "Plan for data lineage tracking",
                    "Consider incremental processing patterns",
                ]
            )

        return recommendations
