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
FLUID Plugin System - Extensible Architecture for Custom Commands

This module provides a comprehensive plugin system that allows third-party
developers and enterprise teams to extend FLUID with custom commands,
providers, and integrations.
"""

from __future__ import annotations

import argparse
import importlib
import inspect
import json
import logging
import shutil
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from fluid_build.cli.console import cprint

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table

    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False
    Console = None


COMMAND = "plugins"


class PluginType(Enum):
    """Types of plugins supported"""

    COMMAND = "command"
    PROVIDER = "provider"
    VALIDATOR = "validator"
    FORMATTER = "formatter"
    INTEGRATION = "integration"


class PluginStatus(Enum):
    """Plugin status"""

    ACTIVE = "active"
    INACTIVE = "inactive"
    ERROR = "error"
    LOADING = "loading"


@dataclass
class PluginMetadata:
    """Plugin metadata information"""

    name: str
    version: str
    description: str
    author: str
    homepage: Optional[str] = None
    documentation: Optional[str] = None
    plugin_type: PluginType = PluginType.COMMAND
    fluid_version_min: str = "2.0.0"
    fluid_version_max: Optional[str] = None
    dependencies: List[str] = field(default_factory=list)
    permissions: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary"""
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "author": self.author,
            "homepage": self.homepage,
            "documentation": self.documentation,
            "plugin_type": self.plugin_type.value,
            "fluid_version_min": self.fluid_version_min,
            "fluid_version_max": self.fluid_version_max,
            "dependencies": self.dependencies,
            "permissions": self.permissions,
        }


class PluginInterface(ABC):
    """Base interface for all FLUID plugins"""

    @abstractmethod
    def get_metadata(self) -> PluginMetadata:
        """Return plugin metadata"""
        pass

    @abstractmethod
    def initialize(self, config: Dict[str, Any]) -> bool:
        """Initialize the plugin with configuration"""
        pass

    @abstractmethod
    def cleanup(self) -> None:
        """Cleanup plugin resources"""
        pass


class CommandPlugin(PluginInterface):
    """Base class for command plugins"""

    @abstractmethod
    def register_commands(self, subparsers: argparse._SubParsersAction) -> None:
        """Register plugin commands with CLI parser"""
        pass

    @abstractmethod
    def execute(self, command: str, args: argparse.Namespace, logger: logging.Logger) -> int:
        """Execute plugin command"""
        pass


class ProviderPlugin(PluginInterface):
    """Base class for provider plugins"""

    @abstractmethod
    def get_provider_name(self) -> str:
        """Return provider name"""
        pass

    @abstractmethod
    def validate_config(self, config: Dict[str, Any]) -> bool:
        """Validate provider configuration"""
        pass

    @abstractmethod
    def execute_plan(self, plan: Dict[str, Any]) -> Dict[str, Any]:
        """Execute deployment plan"""
        pass


class ValidatorPlugin(PluginInterface):
    """Base class for validator plugins"""

    @abstractmethod
    def validate_contract(self, contract: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Validate contract and return issues"""
        pass


@dataclass
class InstalledPlugin:
    """Information about an installed plugin"""

    metadata: PluginMetadata
    path: Path
    module: Optional[Any] = None
    instance: Optional[PluginInterface] = None
    status: PluginStatus = PluginStatus.INACTIVE
    error_message: Optional[str] = None
    config: Dict[str, Any] = field(default_factory=dict)


class PluginManager:
    """Manages FLUID plugins"""

    def __init__(self, plugin_dir: Optional[Path] = None):
        self.plugin_dir = plugin_dir or Path.home() / ".fluid" / "plugins"
        self.plugin_dir.mkdir(parents=True, exist_ok=True)

        self.installed_plugins: Dict[str, InstalledPlugin] = {}
        self.active_plugins: Dict[str, InstalledPlugin] = {}

        self.console = Console() if RICH_AVAILABLE else None

        # Plugin registry for different types
        self.command_plugins: Dict[str, CommandPlugin] = {}
        self.provider_plugins: Dict[str, ProviderPlugin] = {}
        self.validator_plugins: Dict[str, ValidatorPlugin] = {}

        self._load_installed_plugins()

    def _load_installed_plugins(self):
        """Load all installed plugins"""
        for plugin_path in self.plugin_dir.iterdir():
            if plugin_path.is_dir() and (plugin_path / "plugin.json").exists():
                try:
                    self._load_plugin(plugin_path)
                except Exception as e:
                    if self.console:
                        self.console.print(
                            f"[red]Failed to load plugin {plugin_path.name}: {e}[/red]"
                        )

    def _load_plugin(self, plugin_path: Path):
        """Load a single plugin"""
        # Read plugin metadata
        metadata_file = plugin_path / "plugin.json"
        with open(metadata_file) as f:
            metadata_dict = json.load(f)

        metadata = PluginMetadata(
            name=metadata_dict["name"],
            version=metadata_dict["version"],
            description=metadata_dict["description"],
            author=metadata_dict["author"],
            homepage=metadata_dict.get("homepage"),
            documentation=metadata_dict.get("documentation"),
            plugin_type=PluginType(metadata_dict.get("plugin_type", "command")),
            fluid_version_min=metadata_dict.get("fluid_version_min", "2.0.0"),
            fluid_version_max=metadata_dict.get("fluid_version_max"),
            dependencies=metadata_dict.get("dependencies", []),
            permissions=metadata_dict.get("permissions", []),
        )

        # Create plugin instance
        plugin = InstalledPlugin(metadata=metadata, path=plugin_path, status=PluginStatus.LOADING)

        try:
            # Add plugin path to sys.path
            if str(plugin_path) not in sys.path:
                sys.path.insert(0, str(plugin_path))

            # Import plugin module
            main_module = metadata_dict.get("main_module", "main")
            module = importlib.import_module(main_module)

            # Find plugin class
            plugin_class = None
            for name, obj in inspect.getmembers(module, inspect.isclass):
                if issubclass(obj, PluginInterface) and obj != PluginInterface:
                    plugin_class = obj
                    break

            if not plugin_class:
                raise Exception("No plugin class found that implements PluginInterface")

            # Create plugin instance
            plugin_instance = plugin_class()

            # Initialize plugin
            config = self._load_plugin_config(plugin_path)
            if plugin_instance.initialize(config):
                plugin.module = module
                plugin.instance = plugin_instance
                plugin.status = PluginStatus.ACTIVE
                plugin.config = config

                # Register in appropriate registry
                self._register_plugin(plugin)

                self.active_plugins[metadata.name] = plugin
            else:
                plugin.status = PluginStatus.ERROR
                plugin.error_message = "Plugin initialization failed"

        except Exception as e:
            plugin.status = PluginStatus.ERROR
            plugin.error_message = str(e)

        self.installed_plugins[metadata.name] = plugin

    def _load_plugin_config(self, plugin_path: Path) -> Dict[str, Any]:
        """Load plugin configuration"""
        config_file = plugin_path / "config.json"
        if config_file.exists():
            with open(config_file) as f:
                return json.load(f)
        return {}

    def _register_plugin(self, plugin: InstalledPlugin):
        """Register plugin in appropriate registry"""
        if plugin.metadata.plugin_type == PluginType.COMMAND:
            if isinstance(plugin.instance, CommandPlugin):
                self.command_plugins[plugin.metadata.name] = plugin.instance
        elif plugin.metadata.plugin_type == PluginType.PROVIDER:
            if isinstance(plugin.instance, ProviderPlugin):
                self.provider_plugins[plugin.metadata.name] = plugin.instance
        elif plugin.metadata.plugin_type == PluginType.VALIDATOR:
            if isinstance(plugin.instance, ValidatorPlugin):
                self.validator_plugins[plugin.metadata.name] = plugin.instance

    def install_plugin(self, source: Union[str, Path], force: bool = False) -> bool:
        """Install a plugin from source"""
        try:
            source_path = Path(source) if isinstance(source, str) else source

            if not source_path.exists():
                if self.console:
                    self.console.print(f"[red]Plugin source not found: {source_path}[/red]")
                return False

            # Read plugin metadata
            metadata_file = source_path / "plugin.json"
            if not metadata_file.exists():
                if self.console:
                    self.console.print("[red]Invalid plugin: missing plugin.json[/red]")
                return False

            with open(metadata_file) as f:
                metadata_dict = json.load(f)

            plugin_name = metadata_dict["name"]

            # Check if already installed
            if plugin_name in self.installed_plugins and not force:
                if self.console:
                    self.console.print(
                        f"[yellow]Plugin {plugin_name} already installed. Use --force to overwrite.[/yellow]"
                    )
                return False

            # Install plugin
            target_path = self.plugin_dir / plugin_name
            if target_path.exists():
                shutil.rmtree(target_path)

            shutil.copytree(source_path, target_path)

            # Load the new plugin
            self._load_plugin(target_path)

            if self.console:
                self.console.print(f"[green]✅ Plugin {plugin_name} installed successfully[/green]")

            return True

        except Exception as e:
            if self.console:
                self.console.print(f"[red]Failed to install plugin: {e}[/red]")
            return False

    def uninstall_plugin(self, plugin_name: str) -> bool:
        """Uninstall a plugin"""
        try:
            if plugin_name not in self.installed_plugins:
                if self.console:
                    self.console.print(f"[yellow]Plugin {plugin_name} not found[/yellow]")
                return False

            plugin = self.installed_plugins[plugin_name]

            # Cleanup plugin
            if plugin.instance:
                plugin.instance.cleanup()

            # Remove from registries
            self.command_plugins.pop(plugin_name, None)
            self.provider_plugins.pop(plugin_name, None)
            self.validator_plugins.pop(plugin_name, None)

            # Remove from active plugins
            self.active_plugins.pop(plugin_name, None)

            # Remove files
            if plugin.path.exists():
                shutil.rmtree(plugin.path)

            # Remove from installed plugins
            del self.installed_plugins[plugin_name]

            if self.console:
                self.console.print(
                    f"[green]✅ Plugin {plugin_name} uninstalled successfully[/green]"
                )

            return True

        except Exception as e:
            if self.console:
                self.console.print(f"[red]Failed to uninstall plugin: {e}[/red]")
            return False

    def enable_plugin(self, plugin_name: str) -> bool:
        """Enable a plugin"""
        if plugin_name not in self.installed_plugins:
            return False

        plugin = self.installed_plugins[plugin_name]
        if plugin.status == PluginStatus.ACTIVE:
            return True

        try:
            if plugin.instance and plugin.instance.initialize(plugin.config):
                plugin.status = PluginStatus.ACTIVE
                self.active_plugins[plugin_name] = plugin
                self._register_plugin(plugin)
                return True
        except Exception as e:
            plugin.status = PluginStatus.ERROR
            plugin.error_message = str(e)

        return False

    def disable_plugin(self, plugin_name: str) -> bool:
        """Disable a plugin"""
        if plugin_name not in self.active_plugins:
            return False

        plugin = self.active_plugins[plugin_name]

        try:
            if plugin.instance:
                plugin.instance.cleanup()

            plugin.status = PluginStatus.INACTIVE

            # Remove from registries
            self.command_plugins.pop(plugin_name, None)
            self.provider_plugins.pop(plugin_name, None)
            self.validator_plugins.pop(plugin_name, None)

            # Remove from active plugins
            del self.active_plugins[plugin_name]

            return True

        except Exception:
            return False

    def list_plugins(self, plugin_type: Optional[PluginType] = None) -> List[InstalledPlugin]:
        """List installed plugins"""
        plugins = list(self.installed_plugins.values())

        if plugin_type:
            plugins = [p for p in plugins if p.metadata.plugin_type == plugin_type]

        return plugins

    def get_plugin(self, plugin_name: str) -> Optional[InstalledPlugin]:
        """Get plugin by name"""
        return self.installed_plugins.get(plugin_name)

    def register_command_plugins(self, subparsers: argparse._SubParsersAction):
        """Register all command plugins with CLI parser"""
        for plugin in self.command_plugins.values():
            try:
                plugin.register_commands(subparsers)
            except Exception as e:
                if self.console:
                    self.console.print(f"[red]Failed to register commands for plugin: {e}[/red]")

    def execute_plugin_command(
        self, plugin_name: str, command: str, args: argparse.Namespace, logger: logging.Logger
    ) -> int:
        """Execute a plugin command"""
        if plugin_name not in self.command_plugins:
            return 1

        plugin = self.command_plugins[plugin_name]
        return plugin.execute(command, args, logger)


# Global plugin manager instance
_plugin_manager: Optional[PluginManager] = None


def get_plugin_manager() -> PluginManager:
    """Get global plugin manager instance"""
    global _plugin_manager
    if _plugin_manager is None:
        _plugin_manager = PluginManager()
    return _plugin_manager


# CLI Integration
def register(subparsers: argparse._SubParsersAction):
    """Register the plugins command"""
    p = subparsers.add_parser(COMMAND, help="🔌 Manage FLUID plugins and extensions")

    plugin_subparsers = p.add_subparsers(dest="plugin_action", help="Plugin actions")

    # List plugins
    list_parser = plugin_subparsers.add_parser("list", help="List installed plugins")
    list_parser.add_argument(
        "--type", choices=[t.value for t in PluginType], help="Filter by plugin type"
    )
    list_parser.add_argument(
        "--status", choices=[s.value for s in PluginStatus], help="Filter by status"
    )

    # Install plugin
    install_parser = plugin_subparsers.add_parser("install", help="Install a plugin")
    install_parser.add_argument("source", help="Plugin source path or URL")
    install_parser.add_argument(
        "--force", action="store_true", help="Force reinstall if already exists"
    )

    # Uninstall plugin
    uninstall_parser = plugin_subparsers.add_parser("uninstall", help="Uninstall a plugin")
    uninstall_parser.add_argument("name", help="Plugin name to uninstall")

    # Enable/disable plugins
    enable_parser = plugin_subparsers.add_parser("enable", help="Enable a plugin")
    enable_parser.add_argument("name", help="Plugin name to enable")

    disable_parser = plugin_subparsers.add_parser("disable", help="Disable a plugin")
    disable_parser.add_argument("name", help="Plugin name to disable")

    # Show plugin info
    info_parser = plugin_subparsers.add_parser("info", help="Show plugin information")
    info_parser.add_argument("name", help="Plugin name")

    # Create plugin template
    create_parser = plugin_subparsers.add_parser("create", help="Create a new plugin template")
    create_parser.add_argument("name", help="Plugin name")
    create_parser.add_argument(
        "--type", choices=[t.value for t in PluginType], default="command", help="Plugin type"
    )
    create_parser.add_argument("--author", help="Plugin author")

    p.set_defaults(func=run)


def run(args, logger: logging.Logger) -> int:
    """Main entry point for plugins command"""
    try:
        plugin_manager = get_plugin_manager()

        if args.plugin_action == "list":
            return handle_list_plugins(args, plugin_manager, logger)
        elif args.plugin_action == "install":
            return handle_install_plugin(args, plugin_manager, logger)
        elif args.plugin_action == "uninstall":
            return handle_uninstall_plugin(args, plugin_manager, logger)
        elif args.plugin_action == "enable":
            return handle_enable_plugin(args, plugin_manager, logger)
        elif args.plugin_action == "disable":
            return handle_disable_plugin(args, plugin_manager, logger)
        elif args.plugin_action == "info":
            return handle_plugin_info(args, plugin_manager, logger)
        elif args.plugin_action == "create":
            return handle_create_plugin(args, plugin_manager, logger)
        else:
            if RICH_AVAILABLE:
                console = Console()
                console.print(
                    "[red]❌ Unknown plugin action. Use 'fluid plugins --help' for available options.[/red]"
                )
            return 1

    except Exception as e:
        logger.exception("Plugin command failed")
        if RICH_AVAILABLE:
            console = Console()
            console.print(f"[red]❌ Plugin command failed: {e}[/red]")
        return 1


def handle_list_plugins(args, plugin_manager: PluginManager, logger: logging.Logger) -> int:
    """Handle list plugins command"""
    if not RICH_AVAILABLE:
        cprint("Rich library not available for enhanced output")
        return 1

    console = Console()

    # Filter plugins
    plugins = plugin_manager.list_plugins()

    if hasattr(args, "type") and args.type:
        plugin_type = PluginType(args.type)
        plugins = [p for p in plugins if p.metadata.plugin_type == plugin_type]

    if hasattr(args, "status") and args.status:
        status = PluginStatus(args.status)
        plugins = [p for p in plugins if p.status == status]

    if not plugins:
        console.print("[dim]No plugins found matching criteria[/dim]")
        return 0

    # Create table
    table = Table(title="Installed Plugins")
    table.add_column("Name", style="cyan")
    table.add_column("Version", style="green")
    table.add_column("Type", style="blue")
    table.add_column("Status", style="yellow")
    table.add_column("Description", style="white")

    for plugin in plugins:
        status_color = {
            PluginStatus.ACTIVE: "green",
            PluginStatus.INACTIVE: "yellow",
            PluginStatus.ERROR: "red",
            PluginStatus.LOADING: "blue",
        }.get(plugin.status, "white")

        table.add_row(
            plugin.metadata.name,
            plugin.metadata.version,
            plugin.metadata.plugin_type.value,
            f"[{status_color}]{plugin.status.value}[/{status_color}]",
            (
                plugin.metadata.description[:50] + "..."
                if len(plugin.metadata.description) > 50
                else plugin.metadata.description
            ),
        )

    console.print(table)
    return 0


def handle_install_plugin(args, plugin_manager: PluginManager, logger: logging.Logger) -> int:
    """Handle install plugin command"""
    success = plugin_manager.install_plugin(args.source, args.force)
    return 0 if success else 1


def handle_uninstall_plugin(args, plugin_manager: PluginManager, logger: logging.Logger) -> int:
    """Handle uninstall plugin command"""
    success = plugin_manager.uninstall_plugin(args.name)
    return 0 if success else 1


def handle_enable_plugin(args, plugin_manager: PluginManager, logger: logging.Logger) -> int:
    """Handle enable plugin command"""
    success = plugin_manager.enable_plugin(args.name)
    if success and RICH_AVAILABLE:
        console = Console()
        console.print(f"[green]✅ Plugin {args.name} enabled[/green]")
    return 0 if success else 1


def handle_disable_plugin(args, plugin_manager: PluginManager, logger: logging.Logger) -> int:
    """Handle disable plugin command"""
    success = plugin_manager.disable_plugin(args.name)
    if success and RICH_AVAILABLE:
        console = Console()
        console.print(f"[green]✅ Plugin {args.name} disabled[/green]")
    return 0 if success else 1


def handle_plugin_info(args, plugin_manager: PluginManager, logger: logging.Logger) -> int:
    """Handle plugin info command"""
    if not RICH_AVAILABLE:
        cprint("Rich library not available for enhanced output")
        return 1

    console = Console()
    plugin = plugin_manager.get_plugin(args.name)

    if not plugin:
        console.print(f"[red]Plugin {args.name} not found[/red]")
        return 1

    # Display plugin information
    info_text = f"""
[bold]Name:[/bold] {plugin.metadata.name}
[bold]Version:[/bold] {plugin.metadata.version}
[bold]Type:[/bold] {plugin.metadata.plugin_type.value}
[bold]Author:[/bold] {plugin.metadata.author}
[bold]Status:[/bold] {plugin.status.value}

[bold]Description:[/bold]
{plugin.metadata.description}

[bold]FLUID Version:[/bold] {plugin.metadata.fluid_version_min}+
[bold]Dependencies:[/bold] {', '.join(plugin.metadata.dependencies) if plugin.metadata.dependencies else 'None'}
[bold]Permissions:[/bold] {', '.join(plugin.metadata.permissions) if plugin.metadata.permissions else 'None'}
    """

    if plugin.metadata.homepage:
        info_text += f"\n[bold]Homepage:[/bold] {plugin.metadata.homepage}"

    if plugin.metadata.documentation:
        info_text += f"\n[bold]Documentation:[/bold] {plugin.metadata.documentation}"

    if plugin.error_message:
        info_text += f"\n\n[red]Error:[/red] {plugin.error_message}"

    console.print(
        Panel(info_text.strip(), title=f"Plugin: {plugin.metadata.name}", border_style="blue")
    )
    return 0


def handle_create_plugin(args, plugin_manager: PluginManager, logger: logging.Logger) -> int:
    """Handle create plugin template command"""
    if not RICH_AVAILABLE:
        cprint("Rich library not available for enhanced output")
        return 1

    console = Console()
    plugin_name = args.name
    plugin_type = PluginType(args.type)
    author = args.author or "Unknown"

    # Create plugin directory
    plugin_path = Path.cwd() / plugin_name
    if plugin_path.exists():
        console.print(f"[red]Directory {plugin_name} already exists[/red]")
        return 1

    plugin_path.mkdir()

    # Create plugin.json
    metadata = {
        "name": plugin_name,
        "version": "1.0.0",
        "description": f"A {plugin_type.value} plugin for FLUID",
        "author": author,
        "plugin_type": plugin_type.value,
        "fluid_version_min": "2.0.0",
        "main_module": "main",
        "dependencies": [],
        "permissions": [],
    }

    with open(plugin_path / "plugin.json", "w") as f:
        json.dump(metadata, f, indent=2)

    # Create main.py template
    if plugin_type == PluginType.COMMAND:
        template = _get_command_plugin_template(plugin_name, author)
    elif plugin_type == PluginType.PROVIDER:
        template = _get_provider_plugin_template(plugin_name, author)
    else:
        template = _get_basic_plugin_template(plugin_name, author, plugin_type)

    with open(plugin_path / "main.py", "w") as f:
        f.write(template)

    # Create README.md
    readme = f"""# {plugin_name}

{metadata['description']}

## Installation

```bash
fluid plugins install {plugin_name}
```

## Usage

[Add usage instructions here]

## Development

This plugin was created using the FLUID plugin template.

## License

[Add license information here]
"""

    with open(plugin_path / "README.md", "w") as f:
        f.write(readme)

    console.print(f"[green]✅ Plugin template created at {plugin_path}[/green]")
    console.print("[dim]Edit the files and then install with 'fluid plugins install .'[/dim]")

    return 0


def _get_command_plugin_template(name: str, author: str) -> str:
    """Get command plugin template"""
    return f'''"""
{name} - FLUID Command Plugin

Created by: {author}
"""

import argparse
import logging
from typing import Dict, Any

from fluid_build.cli.plugins import CommandPlugin, PluginMetadata, PluginType


class {name.title().replace('-', '').replace('_', '')}Plugin(CommandPlugin):
    """Custom command plugin for FLUID"""
    
    def get_metadata(self) -> PluginMetadata:
        return PluginMetadata(
            name="{name}",
            version="1.0.0",
            description="Custom command plugin",
            author="{author}",
            plugin_type=PluginType.COMMAND
        )
    
    def initialize(self, config: Dict[str, Any]) -> bool:
        """Initialize the plugin"""
        self.config = config
        return True
    
    def cleanup(self) -> None:
        """Cleanup plugin resources"""
        pass
    
    def register_commands(self, subparsers: argparse._SubParsersAction) -> None:
        """Register plugin commands"""
        parser = subparsers.add_parser(
            "{name}",
            help="Custom command provided by {name} plugin"
        )
        parser.add_argument("--option", help="Example option")
        parser.set_defaults(plugin_command="{name}")
    
    def execute(self, command: str, args: argparse.Namespace, logger: logging.Logger) -> int:
        """Execute plugin command"""
        if command == "{name}":
            return self._handle_main_command(args, logger)
        return 1
    
    def _handle_main_command(self, args: argparse.Namespace, logger: logging.Logger) -> int:
        """Handle main plugin command"""
        cprint(f"Hello from {{args.plugin_command}} plugin!")
        if hasattr(args, 'option') and args.option:
            cprint(f"Option value: {{args.option}}")
        return 0
'''


def _get_provider_plugin_template(name: str, author: str) -> str:
    """Get provider plugin template"""
    return f'''"""
{name} - FLUID Provider Plugin

Created by: {author}
"""

from typing import Dict, Any

from fluid_build.cli.plugins import ProviderPlugin, PluginMetadata, PluginType


class {name.title().replace('-', '').replace('_', '')}Provider(ProviderPlugin):
    """Custom provider plugin for FLUID"""
    
    def get_metadata(self) -> PluginMetadata:
        return PluginMetadata(
            name="{name}",
            version="1.0.0",
            description="Custom provider plugin",
            author="{author}",
            plugin_type=PluginType.PROVIDER
        )
    
    def initialize(self, config: Dict[str, Any]) -> bool:
        """Initialize the provider"""
        self.config = config
        return True
    
    def cleanup(self) -> None:
        """Cleanup provider resources"""
        pass
    
    def get_provider_name(self) -> str:
        """Return provider name"""
        return "{name}"
    
    def validate_config(self, config: Dict[str, Any]) -> bool:
        """Validate provider configuration"""
        # Add your validation logic here
        return True
    
    def execute_plan(self, plan: Dict[str, Any]) -> Dict[str, Any]:
        """Execute deployment plan"""
        # Add your deployment logic here
        return {{"status": "success", "message": "Plan executed successfully"}}
'''


def _get_basic_plugin_template(name: str, author: str, plugin_type: PluginType) -> str:
    """Get basic plugin template"""
    return f'''"""
{name} - FLUID {plugin_type.value.title()} Plugin

Created by: {author}
"""

from typing import Dict, Any

from fluid_build.cli.plugins import PluginInterface, PluginMetadata, PluginType


class {name.title().replace('-', '').replace('_', '')}Plugin(PluginInterface):
    """Custom {plugin_type.value} plugin for FLUID"""
    
    def get_metadata(self) -> PluginMetadata:
        return PluginMetadata(
            name="{name}",
            version="1.0.0",
            description="Custom {plugin_type.value} plugin",
            author="{author}",
            plugin_type=PluginType.{plugin_type.name}
        )
    
    def initialize(self, config: Dict[str, Any]) -> bool:
        """Initialize the plugin"""
        self.config = config
        return True
    
    def cleanup(self) -> None:
        """Cleanup plugin resources"""
        pass
'''
