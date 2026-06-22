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
FLUID Forge Engine - Main orchestration system

The ForgeEngine is the central coordinator that manages the entire project creation
workflow. It integrates templates, providers, generators, and extensions to provide
a seamless, extensible experience for creating FLUID data products.

Key Responsibilities:
- Workflow orchestration and user interaction
- Component coordination and lifecycle management
- Configuration validation and project generation
- Extension integration and hook management
- Error handling and recovery
- Progress tracking and reporting

Architecture:
- Interactive workflow with rich console UI
- Plugin-based extension system
- Configurable validation pipeline
- Async generation with progress tracking
- Comprehensive error handling and rollback
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from rich import print as rprint
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.prompt import Confirm, IntPrompt, Prompt
from rich.table import Table
from rich.tree import Tree

from fluid_build.schema_manager import FluidSchemaManager

from .interfaces import ComplexityLevel, GenerationContext
from .registry import (
    extension_registry,
    generator_registry,
    get_registry_status,
    initialize_all_registries,
    provider_registry,
    template_registry,
    validation_registry,
)

logger = logging.getLogger(__name__)
COPILOT_WARNING_ONLY_PROVIDERS = {"local", "gcp", "aws", "snowflake"}


class ForgeEngine:
    """
    Main FLUID Forge engine for creating data products

    The ForgeEngine orchestrates the entire project creation process,
    from initial user input through final project generation.

    Example usage:
        engine = ForgeEngine()
        result = engine.run(target_dir="/path/to/project")

        # Or with custom configuration
        config = {
            "name": "my-project",
            "template": "analytics",
            "provider": "gcp"
        }
        result = engine.run_with_config(config)
    """

    def __init__(self, console: Optional[Console] = None, auto_init_registries: bool = True):
        """
        Initialize the ForgeEngine

        Args:
            console: Rich console for output (creates default if None)
            auto_init_registries: Whether to auto-initialize component registries
        """
        self.console = console or Console(force_terminal=True, force_interactive=True)
        self.project_config: Dict[str, Any] = {}
        self.generation_context: Optional[GenerationContext] = None
        self.session_stats = {
            "start_time": datetime.now(),
            "steps_completed": [],
            "errors_encountered": [],
            "components_used": {},
        }

        # Initialize registries if requested
        if auto_init_registries:
            initialize_all_registries()

        # Validate registries have components
        self._validate_registry_setup()

    def _validate_registry_setup(self) -> None:
        """Validate that registries have the minimum required components"""
        status = get_registry_status()

        if status["templates"]["count"] == 0:
            logger.warning("No templates registered - forge may not work properly")

        if status["providers"]["count"] == 0:
            logger.warning("No providers registered - forge may not work properly")

    def run(
        self,
        target_dir: Optional[str] = None,
        template: Optional[str] = None,
        provider: Optional[str] = None,
        non_interactive: bool = False,
        dry_run: bool = False,
    ) -> bool:
        """
        Run the forge process interactively

        Args:
            target_dir: Target directory for project creation
            template: Pre-selected template name
            provider: Pre-selected provider name
            non_interactive: Skip interactive prompts and use defaults
            dry_run: Preview what would be created without generating files

        Returns:
            True if successful, False otherwise
        """
        try:
            self._show_welcome()

            # Set up initial configuration
            if target_dir:
                self.project_config["target_dir"] = Path(target_dir)
            if template:
                self.project_config["template"] = template
            if provider:
                self.project_config["provider"] = provider

            # Run workflow based on mode
            if dry_run:
                return self._run_dry_run()
            elif non_interactive:
                return self._run_non_interactive()
            else:
                return self._run_interactive()

        except KeyboardInterrupt:
            rprint("\n[red]❌ Process interrupted by user[/red]")
            return False
        except Exception as e:
            logger.error(f"Forge engine error: {e}", exc_info=True)
            rprint(f"[red]❌ Error: {e}[/red]")
            return False

    def run_with_config(self, config: Dict[str, Any], dry_run: bool = False) -> bool:
        """
        Run forge with pre-defined configuration

        Args:
            config: Project configuration dictionary
            dry_run: Preview mode without file generation

        Returns:
            True if successful, False otherwise
        """
        self.project_config.update(config)

        try:
            # Validate configuration
            if not self._validate_configuration():
                return False

            # Create generation context
            self._create_generation_context()

            # Run generation
            if dry_run:
                return self._preview_generation()
            else:
                return self._execute_generation()

        except Exception as e:
            logger.error(f"Forge execution error: {e}", exc_info=True)
            rprint(f"[red]❌ Error: {e}[/red]")
            return False

    def _run_interactive(self) -> bool:
        """Run interactive workflow with user prompts"""
        try:
            # Trigger extension hooks
            extension_registry.trigger_lifecycle_hook("on_forge_start", self.generation_context)

            # Interactive workflow steps
            steps = [
                ("📋 Project Information", self._gather_project_info),
                ("🎯 Template Selection", self._select_template),
                ("☁️ Provider Configuration", self._configure_provider),
                ("⚙️ Advanced Options", self._configure_advanced_options),
                ("🔍 Validation", self._validate_configuration),
                ("🚀 Generation", self._execute_generation),
            ]

            for step_name, step_func in steps:
                self._show_step_progress(step_name, len(steps))

                if not step_func():
                    rprint(f"[red]❌ Step failed: {step_name}[/red]")
                    return False

                self.session_stats["steps_completed"].append(step_name)

            self._show_completion_summary()
            return True

        except Exception as e:
            logger.error(f"Interactive workflow error: {e}", exc_info=True)
            return False

    def _run_non_interactive(self) -> bool:
        """Run with intelligent defaults, no user interaction"""
        try:
            rprint("[cyan]🤖 Running in non-interactive mode with intelligent defaults[/cyan]")

            # Apply defaults
            self._apply_intelligent_defaults()

            # Validate and generate
            if not self._validate_configuration():
                return False

            self._create_generation_context()
            return self._execute_generation()

        except Exception as e:
            logger.error(f"Non-interactive workflow error: {e}", exc_info=True)
            return False

    def _run_dry_run(self) -> bool:
        """Preview what would be created without generating files"""
        try:
            rprint("[cyan]🔍 Running in dry-run mode - no files will be created[/cyan]")

            # Use interactive or default configuration
            if not self.project_config.get("template"):
                if not self._run_interactive():
                    return False
            else:
                self._apply_intelligent_defaults()

            return self._preview_generation()

        except Exception as e:
            logger.error(f"Dry-run workflow error: {e}", exc_info=True)
            return False

    def _show_welcome(self) -> None:
        """Display welcome message with system status"""
        status = get_registry_status()

        welcome_text = f"""
🔨 [bold blue]FLUID Forge v2.0[/bold blue] - Extensible Project Bootstrap

[dim]Creating production-ready FLUID data products with best practices[/dim]

[bold cyan]📊 System Status:[/bold cyan]
• Templates: {status["templates"]["count"]} available
• Providers: {status["providers"]["count"]} available  
• Extensions: {status["extensions"]["count"]} loaded
• Generators: {status["generators"]["count"]} available

[bold green]System ready ⚡[/bold green]
        """

        self.console.print(Panel(welcome_text.strip(), title="🔨 FLUID Forge", style="blue"))

    def _show_step_progress(self, step_name: str, total_steps: int) -> None:
        """Show progress for current step"""
        current_step = len(self.session_stats["steps_completed"]) + 1
        progress_bar = "█" * (current_step * 20 // total_steps) + "░" * (
            20 - (current_step * 20 // total_steps)
        )
        percentage = (current_step / total_steps) * 100

        rprint(
            f"\\n[bold blue]📋 Step {current_step}/{total_steps}: [{progress_bar}] {percentage:.0f}% - {step_name}[/bold blue]"
        )

    def _gather_project_info(self) -> bool:
        """Gather basic project information"""
        try:
            rprint("\\n[bold cyan]Tell us about your data product[/bold cyan]")

            # Project name
            if "name" not in self.project_config:
                name = Prompt.ask("Project name", default="my-data-product")
                if not self._validate_project_name(name):
                    return False
                self.project_config["name"] = name

            # Description
            if "description" not in self.project_config:
                description = Prompt.ask("Description", default="A production-ready data product")
                self.project_config["description"] = description

            # Domain/Owner
            if "domain" not in self.project_config:
                domain = Prompt.ask("Domain/Team", default="analytics")
                self.project_config["domain"] = domain

            if "owner" not in self.project_config:
                owner = Prompt.ask("Owner", default="data-team")
                self.project_config["owner"] = owner

            # Target directory
            if "target_dir" not in self.project_config:
                default_dir = Path.cwd() / self.project_config["name"]
                target_dir = Prompt.ask("Target directory", default=str(default_dir))
                self.project_config["target_dir"] = Path(target_dir)

            return True

        except Exception as e:
            logger.error(f"Error gathering project info: {e}")
            return False

    def _select_template(self) -> bool:
        """Interactive template selection with recommendations"""
        try:
            # Get AI recommendations
            recommendations = self._get_template_recommendations()

            # Show available templates
            available_templates = template_registry.list_available()
            if not available_templates:
                rprint("[red]❌ No templates available[/red]")
                return False

            # Display template options
            table = Table(show_header=True, header_style="bold magenta")
            table.add_column("Option", width=8)
            table.add_column("Template", width=25)
            table.add_column("Complexity", width=12)
            table.add_column("Description", width=50)

            for i, template_name in enumerate(available_templates, 1):
                template = template_registry.get(template_name)
                if template:
                    metadata = template.get_metadata()
                    complexity_icon = self._get_complexity_icon(metadata.complexity)

                    # Highlight recommendations
                    style = "bold green" if template_name in recommendations else ""

                    table.add_row(
                        str(i),
                        template_name,
                        f"{complexity_icon} {metadata.complexity.value}",
                        metadata.description,
                        style=style,
                    )

            self.console.print("\\n[bold cyan]🎯 Available Templates[/bold cyan]")
            if recommendations:
                rprint(f"[green]💡 Recommended: {', '.join(recommendations)}[/green]")
            self.console.print(table)

            # Get user selection
            if "template" not in self.project_config:
                choice = IntPrompt.ask(
                    "Select template",
                    default=1,
                    choices=[str(i) for i in range(1, len(available_templates) + 1)],
                )
                selected_template = available_templates[choice - 1]
                self.project_config["template"] = selected_template

            # Trigger extension hook
            template = template_registry.get(self.project_config["template"])
            if template:
                extension_registry.trigger_lifecycle_hook(
                    "on_template_selected", template, self.generation_context
                )

            return True

        except Exception as e:
            logger.error(f"Error selecting template: {e}")
            return False

    def _configure_provider(self) -> bool:
        """Interactive provider configuration"""
        try:
            # Get template to determine supported providers
            template_name = self.project_config.get("template")
            template = template_registry.get(template_name)

            if template:
                metadata = template.get_metadata()
                supported_providers = metadata.provider_support
            else:
                supported_providers = provider_registry.list_available()

            # Filter available providers
            available_providers = [
                p for p in provider_registry.list_available() if p in supported_providers
            ]

            if not available_providers:
                rprint("[red]❌ No providers available for selected template[/red]")
                return False

            # Display provider options
            table = Table(show_header=True, header_style="bold magenta")
            table.add_column("Option", width=8)
            table.add_column("Provider", width=20)
            table.add_column("Status", width=15)
            table.add_column("Description", width=40)

            # Check provider prerequisites
            provider_status = provider_registry.check_prerequisites()

            for i, provider_name in enumerate(available_providers, 1):
                provider = provider_registry.get(provider_name)
                if provider:
                    metadata = provider.get_metadata()
                    status = provider_status.get(provider_name, {})
                    status_icon = "✅" if status.get("available", False) else "⚠️"

                    table.add_row(
                        str(i),
                        provider_name,
                        f"{status_icon} {'Ready' if status.get('available', False) else 'Setup Needed'}",
                        metadata.get("description", "Infrastructure provider"),
                    )

            self.console.print("\\n[bold cyan]☁️ Infrastructure Providers[/bold cyan]")
            self.console.print(table)

            # Get user selection
            if "provider" not in self.project_config:
                choice = IntPrompt.ask(
                    "Select provider",
                    default=1,
                    choices=[str(i) for i in range(1, len(available_providers) + 1)],
                )
                selected_provider = available_providers[choice - 1]
                self.project_config["provider"] = selected_provider

            # Configure selected provider
            provider = provider_registry.get(self.project_config["provider"])
            if provider:
                try:
                    self._create_generation_context()  # Create context for provider config
                    provider_config = provider.configure_interactive(self.generation_context)
                    self.project_config["provider_config"] = provider_config

                    # Trigger extension hook
                    extension_registry.trigger_lifecycle_hook(
                        "on_provider_configured", provider, self.generation_context
                    )

                except Exception as e:
                    rprint(f"[yellow]⚠️ Provider configuration error: {e}[/yellow]")
                    self.project_config["provider_config"] = {}

            return True

        except Exception as e:
            logger.error(f"Error configuring provider: {e}")
            return False

    def _configure_advanced_options(self) -> bool:
        """Configure advanced options"""
        try:
            rprint("\\n[bold cyan]⚙️ Advanced Configuration[/bold cyan]")

            # FLUID version selection
            if "fluid_version" not in self.project_config:
                try:
                    from fluid_build.schema_manager import FluidSchemaManager

                    available = FluidSchemaManager.BUNDLED_VERSIONS
                except Exception:  # noqa: BLE001
                    available = ["0.4.0", "0.5.7", "0.7.1", "0.7.2"]
                version_choice = Prompt.ask(
                    "FLUID specification version", choices=available, default=available[-1]
                )
                self.project_config["fluid_version"] = version_choice

            # Optional features
            features = {
                "enable_monitoring": "Enable monitoring and observability",
                "enable_testing": "Include comprehensive testing framework",
                "enable_docs": "Generate documentation",
                "enable_ci_cd": "Set up CI/CD pipeline",
            }

            for feature_key, feature_desc in features.items():
                if feature_key not in self.project_config:
                    enable = Confirm.ask(feature_desc, default=True)
                    self.project_config[feature_key] = enable

            # Pipeline configuration if CI/CD is enabled
            if self.project_config.get("enable_ci_cd", True):
                if not self._configure_pipeline_options():
                    return False

            return True

        except Exception as e:
            logger.error(f"Error configuring advanced options: {e}")
            return False

    def _configure_pipeline_options(self) -> bool:
        """Configure CI/CD pipeline options"""
        try:
            rprint("\\n[bold cyan]🚀 CI/CD Pipeline Configuration[/bold cyan]")

            # Import pipeline components
            from .pipeline_templates import (
                PipelineComplexity,
                PipelineProvider,
            )

            # Provider selection
            providers = [p.value for p in PipelineProvider]
            provider_table = Table(show_header=True, header_style="bold magenta")
            provider_table.add_column("Option", width=8)
            provider_table.add_column("Provider", width=20)
            provider_table.add_column("Description", width=50)

            provider_descriptions = {
                "github_actions": "GitHub Actions - Integrated with GitHub repositories",
                "gitlab_ci": "GitLab CI - GitLab integrated CI/CD",
                "azure_devops": "Azure DevOps - Microsoft cloud CI/CD platform",
                "jenkins": "Jenkins - Self-hosted automation server",
                "bitbucket": "Bitbucket Pipelines - Atlassian CI/CD solution",
                "circleci": "CircleCI - Cloud-native continuous integration",
                "tekton": "Tekton - Kubernetes-native CI/CD framework",
            }

            for i, provider in enumerate(providers, 1):
                provider_table.add_row(
                    str(i),
                    provider.replace("_", " ").title(),
                    provider_descriptions.get(provider, "CI/CD provider"),
                )

            self.console.print(provider_table)

            choice = IntPrompt.ask(
                "Select CI/CD provider",
                default=1,
                choices=[str(i) for i in range(1, len(providers) + 1)],
            )
            selected_provider = providers[choice - 1]

            # Complexity selection
            complexities = [c.value for c in PipelineComplexity]
            complexity_table = Table(show_header=True, header_style="bold magenta")
            complexity_table.add_column("Option", width=8)
            complexity_table.add_column("Level", width=15)
            complexity_table.add_column("Features", width=50)

            complexity_features = {
                "basic": "Simple validate → apply workflow",
                "standard": "Full workflow with testing and multi-environment",
                "advanced": "Multi-environment with approvals and security scanning",
                "enterprise": "Full governance, compliance, and audit capabilities",
            }

            for i, complexity in enumerate(complexities, 1):
                complexity_table.add_row(
                    str(i),
                    complexity.title(),
                    complexity_features.get(complexity, "Pipeline complexity level"),
                )

            rprint("\\n[bold cyan]Pipeline Complexity Levels:[/bold cyan]")
            self.console.print(complexity_table)

            complexity_choice = IntPrompt.ask(
                "Select pipeline complexity",
                default=2,  # Standard as default
                choices=[str(i) for i in range(1, len(complexities) + 1)],
            )
            selected_complexity = complexities[complexity_choice - 1]

            # Environment configuration
            environments = []
            if selected_complexity in ["standard", "advanced", "enterprise"]:
                env_config = Confirm.ask("Configure multiple environments?", default=True)
                if env_config:
                    rprint("\\nEnvironments (press Enter when done):")
                    while True:
                        env = Prompt.ask("Environment name", default="")
                        if not env:
                            break
                        environments.append(env)

                    if not environments:
                        environments = ["dev", "staging", "prod"]
                        rprint(f"[yellow]Using default environments: {environments}[/yellow]")
                else:
                    environments = ["dev"]
            else:
                environments = ["dev"]

            # Additional options for advanced/enterprise
            enable_approvals = False
            enable_security_scan = True
            enable_marketplace = False

            if selected_complexity in ["advanced", "enterprise"]:
                enable_approvals = Confirm.ask(
                    "Enable manual approval gates for production?", default=True
                )
                enable_security_scan = Confirm.ask("Enable security scanning?", default=True)
                enable_marketplace = Confirm.ask("Enable marketplace publishing?", default=False)

            # Store pipeline configuration
            self.project_config["pipeline_config"] = {
                "provider": selected_provider,
                "complexity": selected_complexity,
                "environments": environments,
                "enable_approvals": enable_approvals,
                "enable_security_scan": enable_security_scan,
                "enable_marketplace_publishing": enable_marketplace,
            }

            rprint(
                f"\\n[green]✅ Pipeline configured: {selected_provider} ({selected_complexity})[/green]"
            )
            return True

        except Exception as e:
            logger.error(f"Error configuring pipeline: {e}")
            rprint(f"[red]❌ Pipeline configuration failed: {e}[/red]")
            return False

    def _validate_configuration(self) -> bool:
        """Validate complete project configuration"""
        try:
            rprint("\\n[bold cyan]🔍 Validating Configuration[/bold cyan]")

            # Create generation context
            self._create_generation_context()

            # Run validation plugins
            validation_registry.validate_all(self.generation_context, "config")

            # Check core validation
            errors = []
            warnings = []

            # Validate required fields
            required_fields = ["name", "description", "template", "provider", "target_dir"]
            for field in required_fields:
                if field not in self.project_config:
                    errors.append(f"Missing required field: {field}")

            # Validate template
            template = template_registry.get(self.project_config.get("template"))
            if template:
                is_valid, template_errors = template.validate_configuration(self.project_config)
                if not is_valid:
                    errors.extend(template_errors)
            else:
                errors.append(f"Invalid template: {self.project_config.get('template')}")

            # Validate provider
            provider = provider_registry.get(self.project_config.get("provider"))
            if provider:
                provider_config = self.project_config.get("provider_config", {})
                is_valid, provider_errors = provider.validate_configuration(provider_config)
                if not is_valid:
                    provider_name = self.project_config.get("provider")
                    if (
                        self.project_config.get("copilot_generated_contract")
                        and provider_name in COPILOT_WARNING_ONLY_PROVIDERS
                    ):
                        warnings.extend(
                            [
                                f"Provider setup for '{provider_name}' still needs review: {provider_error}"
                                for provider_error in provider_errors
                            ]
                        )
                    else:
                        errors.extend(provider_errors)
            else:
                errors.append(f"Invalid provider: {self.project_config.get('provider')}")

            # Show validation results
            if errors:
                rprint("[red]❌ Configuration errors found:[/red]")
                for error in errors:
                    rprint(f"  • {error}")
                return False

            if warnings:
                rprint("[yellow]⚠️ Configuration warnings:[/yellow]")
                for warning in warnings:
                    rprint(f"  • {warning}")

            rprint("[green]✅ Configuration validated successfully[/green]")
            return True

        except Exception as e:
            logger.error(f"Validation error: {e}")
            rprint(f"[red]❌ Validation failed: {e}[/red]")
            return False

    def _execute_generation(self) -> bool:
        """Execute project generation"""
        try:
            rprint("\\n[bold cyan]🚀 Generating Project[/bold cyan]")

            if not self.generation_context:
                self._create_generation_context()

            target_dir = self.generation_context.target_dir

            # Create target directory
            target_dir.mkdir(parents=True, exist_ok=True)

            # Get template and provider
            template = template_registry.get(self.project_config["template"])
            provider = provider_registry.get(self.project_config["provider"])

            if not template:
                rprint("[red]❌ Template not found[/red]")
                return False

            # Generate project structure
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                transient=True,
            ) as progress:
                # Generate folder structure
                progress.add_task("Creating folder structure...", total=None)
                structure = template.generate_structure(self.generation_context)
                self._create_folder_structure(target_dir, structure)

                # Generate contract
                progress.add_task("Generating FLUID contract...", total=None)
                contract = self._build_contract(template)
                self._write_contract_file(target_dir, contract)

                # Generate provider configuration
                if provider:
                    progress.add_task("Configuring provider...", total=None)
                    provider_config = provider.generate_config(self.generation_context)
                    self._write_provider_config(target_dir, provider_config)

                # Run generators
                progress.add_task("Generating additional files...", total=None)
                self._run_generators()

                # Generate CI/CD pipeline if enabled
                #
                # Slice UX-H: this branch is now a no-op in practice.
                # `_generate_pipeline_files` has been replaced by a
                # deprecation shim; CI file generation routes
                # exclusively through
                # `forge_modes._scaffold_ci_pipeline`, which injects
                # the slice-7 header and emits
                # `.fluid/ci-state.json`.  The branch and method call
                # are preserved so that any external caller or mock
                # that depends on the `_generate_pipeline_files`
                # attribute still finds a reachable symbol.
                if self.project_config.get("enable_ci_cd", True):
                    progress.add_task("Generating CI/CD pipeline...", total=None)
                    self._generate_pipeline_files(target_dir)

                # Post-generation hooks
                progress.add_task("Running post-generation hooks...", total=None)
                template.post_generation_hooks(self.generation_context)
                extension_registry.trigger_lifecycle_hook(
                    "on_generation_complete", self.generation_context
                )

            logger.info("Project created successfully at: %s", target_dir)
            return True

        except Exception as e:
            logger.error(f"Generation error: {e}", exc_info=True)
            rprint(f"[red]❌ Generation failed: {e}[/red]")
            return False

    def _preview_generation(self) -> bool:
        """Preview what would be generated"""
        try:
            rprint("\\n[bold cyan]🔍 Generation Preview[/bold cyan]")

            if not self.generation_context:
                self._create_generation_context()

            # Get template
            template = template_registry.get(self.project_config["template"])
            if not template:
                return False

            # Preview structure
            structure = template.generate_structure(self.generation_context)
            self._preview_structure(structure)

            # Preview contract
            contract = self._build_contract(template)
            self._preview_contract(contract)

            return True

        except Exception as e:
            logger.error(f"Preview error: {e}")
            return False

    def _create_generation_context(self) -> None:
        """Create generation context from project configuration"""
        template_name = self.project_config.get("template")
        template = template_registry.get(template_name)
        template_metadata = template.get_metadata() if template else None

        # Ensure target_dir is a Path object
        target_dir = self.project_config.get("target_dir", Path.cwd())
        if isinstance(target_dir, str):
            target_dir = Path(target_dir)

        self.generation_context = GenerationContext(
            project_config=self.project_config,
            target_dir=target_dir,
            template_metadata=template_metadata,
            provider_config=self.project_config.get("provider_config", {}),
            user_selections=self.project_config,
            forge_version="2.0.0",
            creation_time=datetime.now().isoformat(),
        )

    # Helper methods...
    def _get_complexity_icon(self, complexity: ComplexityLevel) -> str:
        """Get icon for complexity level"""
        icons = {
            ComplexityLevel.BEGINNER: "🟢",
            ComplexityLevel.INTERMEDIATE: "🟡",
            ComplexityLevel.ADVANCED: "🔴",
        }
        return icons.get(complexity, "🟡")

    def _get_template_recommendations(self) -> List[str]:
        """Get AI-powered template recommendations"""
        domain = self.project_config.get("domain", "")
        return template_registry.get_recommended_for_domain(domain)

    def _validate_project_name(self, name: str) -> bool:
        """Validate project name"""
        if not name or len(name) < 2:
            rprint("[red]❌ Project name must be at least 2 characters[/red]")
            return False
        return True

    def _apply_intelligent_defaults(self) -> None:
        """Apply intelligent defaults for non-interactive mode"""
        defaults = {
            "name": "my-data-product",
            "description": "A production-ready data product",
            "domain": "analytics",
            "owner": "data-team",
            "template": "starter",
            "provider": "local",
            "fluid_version": FluidSchemaManager.latest_bundled_version(),
            "target_dir": Path.cwd() / "my-data-product",
        }

        for key, value in defaults.items():
            if key not in self.project_config:
                self.project_config[key] = value

    def _create_folder_structure(self, base_dir: Path, structure: Dict[str, Any]) -> None:
        """Create folder structure from template"""

        def create_dirs(current_dir: Path, struct: Dict[str, Any]):
            for name, content in struct.items():
                if name.endswith("/"):
                    # It's a directory
                    dir_path = current_dir / name.rstrip("/")
                    dir_path.mkdir(exist_ok=True)
                    if isinstance(content, dict):
                        create_dirs(dir_path, content)

        create_dirs(base_dir, structure)

    def _write_contract_file(self, target_dir: Path, contract: Dict[str, Any]) -> None:
        """Write FLUID contract to file"""
        contract_file = target_dir / "contract.fluid.yaml"

        import yaml

        with contract_file.open("w", encoding="utf-8") as f:
            yaml.dump(contract, f, default_flow_style=False, sort_keys=False)

    def _build_contract(self, template) -> Dict[str, Any]:
        """Resolve the contract source of truth for the current generation."""
        if self.project_config.get("copilot_generated_contract"):
            return self.project_config["copilot_generated_contract"]
        return template.generate_contract(self.generation_context)

    def _write_provider_config(self, target_dir: Path, config: Dict[str, Any]) -> None:
        """Write provider configuration"""
        config_file = target_dir / "config" / "provider.json"
        config_file.parent.mkdir(exist_ok=True)

        with config_file.open("w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)

    def _run_generators(self) -> None:
        """Run registered generators and write their files to disk."""
        if not self.generation_context:
            self._create_generation_context()
        if not self.generation_context:
            return

        target_dir = self.generation_context.target_dir
        generator_names = [
            name
            for name in generator_registry.get_dependency_order(generator_registry.list_available())
            if name != "contract"
        ]

        for generator_name in generator_names:
            generator = generator_registry.get(generator_name)
            if not generator:
                continue

            is_valid, errors = generator.validate_context(self.generation_context)
            if not is_valid:
                logger.warning("Skipping generator %s: %s", generator_name, errors)
                continue

            generated_files = generator.generate(self.generation_context)
            for relative_path, content in generated_files.items():
                self._write_generated_file(target_dir / relative_path, content)

    def _write_generated_file(self, file_path: Path, content: str) -> None:
        """Write a generator-produced file."""
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")

    def _generate_pipeline_files(self, target_dir: Path) -> None:
        """Deprecated in slice UX-H.

        CI file generation now routes exclusively through
        ``forge_modes._scaffold_ci_pipeline``, which injects the
        slice-7 ``# Generated by fluid-cli`` header and emits
        ``.fluid/ci-state.json`` for drift detection.

        This method remains as a no-op shim for backward compat and
        to satisfy any test/mock that patches it.  The original
        implementation has been preserved under
        :meth:`_generate_pipeline_files_legacy` so callers that
        explicitly opt into the legacy path still have a reachable
        implementation to invoke.
        """
        logger.debug(
            "engine._generate_pipeline_files called but is a no-op shim "
            "since slice UX-H; CI now flows through _scaffold_ci_pipeline."
        )
        return

    def _generate_pipeline_files_legacy(self, target_dir: Path) -> None:
        """Original slice-pre-UX-H pipeline file generator.

        Preserved so any test fixture or out-of-tree plugin that
        wants the old behavior can still invoke it explicitly.  The
        live code path no longer calls this.
        """
        try:
            pipeline_config = self.project_config.get("pipeline_config", {})
            if not pipeline_config:
                return

            # Import pipeline components
            from .pipeline_templates import (
                PipelineComplexity,
                PipelineConfig,
                PipelineProvider,
                PipelineTemplateGenerator,
            )

            # Create pipeline configuration
            config = PipelineConfig(
                provider=PipelineProvider(pipeline_config["provider"]),
                complexity=PipelineComplexity(pipeline_config["complexity"]),
                environments=pipeline_config.get("environments", ["dev"]),
                enable_approvals=pipeline_config.get("enable_approvals", False),
                enable_security_scan=pipeline_config.get("enable_security_scan", True),
                enable_marketplace_publishing=pipeline_config.get(
                    "enable_marketplace_publishing", False
                ),
            )

            # Generate pipeline files
            generator = PipelineTemplateGenerator()
            pipeline_files = generator.generate_pipeline(config)

            # Write pipeline files
            for filename, content in pipeline_files.items():
                file_path = target_dir / filename
                file_path.parent.mkdir(parents=True, exist_ok=True)

                with file_path.open("w", encoding="utf-8") as f:
                    f.write(content)

            rprint(f"[green]✅ Generated {len(pipeline_files)} pipeline files[/green]")

        except Exception as e:
            logger.error(f"Pipeline generation error: {e}")
            rprint(f"[yellow]⚠️ Pipeline generation failed: {e}[/yellow]")

    def _preview_structure(self, structure: Dict[str, Any]) -> None:
        """Preview folder structure"""
        tree = Tree("📁 Project Structure")

        def add_to_tree(node: Tree, struct: Dict[str, Any]):
            for name, content in struct.items():
                if name.endswith("/"):
                    dir_node = node.add(f"📁 {name.rstrip('/')}")
                    if isinstance(content, dict):
                        add_to_tree(dir_node, content)
                else:
                    node.add(f"📄 {name}")

        add_to_tree(tree, structure)
        self.console.print(tree)

    def _preview_contract(self, contract: Dict[str, Any]) -> None:
        """Preview FLUID contract"""
        rprint("\\n[bold cyan]📋 FLUID Contract Preview[/bold cyan]")

        # Show key contract details
        info_table = Table(show_header=False, box=None)
        info_table.add_column("Field", style="cyan")
        info_table.add_column("Value", style="white")

        key_fields = ["fluidVersion", "kind", "id", "name", "description", "domain"]
        for field in key_fields:
            if field in contract:
                info_table.add_row(field, str(contract[field]))

        self.console.print(info_table)

    def _show_completion_summary(self) -> None:
        """Show completion summary"""
        duration = datetime.now() - self.session_stats["start_time"]

        summary_text = f"""
🎉 [bold green]Project Creation Complete![/bold green]

[bold cyan]📊 Session Summary:[/bold cyan]
• Duration: {duration.total_seconds():.1f} seconds
• Steps completed: {len(self.session_stats["steps_completed"])}
• Project: {self.project_config.get("name", "Unknown")}
• Template: {self.project_config.get("template", "Unknown")}
• Provider: {self.project_config.get("provider", "Unknown")}
• Location: {self.project_config.get("target_dir", "Unknown")}

[bold green]🚀 Your data product is ready for development![/bold green]
        """

        self.console.print(Panel(summary_text.strip(), title="✅ Success", style="green"))
