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
FLUID IDE Integration - Advanced Developer Experience Features

This module provides comprehensive IDE integration including smart auto-completion,
interactive debugging, VS Code extension support, and enhanced developer workflows.
"""

from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

from fluid_build.cli.console import cprint, cprint_json

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.prompt import Confirm, Prompt
    from rich.syntax import Syntax
    from rich.table import Table

    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False


COMMAND = "ide"


class IDEType(Enum):
    """Supported IDE types"""

    VSCODE = "vscode"
    INTELLIJ = "intellij"
    PYCHARM = "pycharm"
    VIM = "vim"
    EMACS = "emacs"
    SUBLIME = "sublime"


class CompletionType(Enum):
    """Types of auto-completion"""

    COMMAND = "command"
    ARGUMENT = "argument"
    PROVIDER = "provider"
    CONTRACT_PATH = "contract_path"
    FIELD_NAME = "field_name"


@dataclass
class CompletionItem:
    """Auto-completion item"""

    label: str
    kind: CompletionType
    detail: str
    documentation: str
    insert_text: str
    score: float = 1.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "label": self.label,
            "kind": self.kind.value,
            "detail": self.detail,
            "documentation": self.documentation,
            "insertText": self.insert_text,
            "score": self.score,
        }


@dataclass
class DiagnosticItem:
    """Diagnostic/error item for IDE"""

    file_path: str
    line: int
    column: int
    severity: str  # 'error', 'warning', 'info'
    message: str
    source: str = "fluid"
    code: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "file": self.file_path,
            "line": self.line,
            "column": self.column,
            "severity": self.severity,
            "message": self.message,
            "source": self.source,
            "code": self.code,
        }


class FluidLanguageServer:
    """Language Server Protocol implementation for FLUID"""

    def __init__(self):
        self.console = Console() if RICH_AVAILABLE else None
        self.workspace_root: Optional[Path] = None
        self.open_files: Dict[str, str] = {}
        self.completions_cache: Dict[str, List[CompletionItem]] = {}

        # Load completion data
        self._load_completion_data()

    def _load_completion_data(self):
        """Load auto-completion data"""
        self.completion_data = {
            "commands": [
                CompletionItem(
                    label="validate",
                    kind=CompletionType.COMMAND,
                    detail="Validate FLUID contract",
                    documentation="Validate a FLUID contract against schemas and business rules",
                    insert_text="validate ${1:contract.yaml}",
                ),
                CompletionItem(
                    label="plan",
                    kind=CompletionType.COMMAND,
                    detail="Generate execution plan",
                    documentation="Compile a provider plan from a FLUID contract",
                    insert_text="plan ${1:contract.yaml} --out ${2:plan.json}",
                ),
                CompletionItem(
                    label="apply",
                    kind=CompletionType.COMMAND,
                    detail="Apply execution plan",
                    documentation="Apply a plan or contract against providers",
                    insert_text="apply ${1:plan.json}",
                ),
                CompletionItem(
                    label="market",
                    kind=CompletionType.COMMAND,
                    detail="Data marketplace",
                    documentation="Discover data products from enterprise catalogs",
                    insert_text="market search ${1:query}",
                ),
                CompletionItem(
                    label="auth",
                    kind=CompletionType.COMMAND,
                    detail="Authentication management",
                    documentation="Manage provider authentication",
                    insert_text="auth login ${1:provider}",
                ),
            ],
            "providers": [
                CompletionItem(
                    label="gcp",
                    kind=CompletionType.PROVIDER,
                    detail="Google Cloud Platform",
                    documentation="Google Cloud Platform provider",
                    insert_text="gcp",
                ),
                CompletionItem(
                    label="aws",
                    kind=CompletionType.PROVIDER,
                    detail="Amazon Web Services",
                    documentation="Amazon Web Services provider",
                    insert_text="aws",
                ),
                CompletionItem(
                    label="azure",
                    kind=CompletionType.PROVIDER,
                    detail="Microsoft Azure",
                    documentation="Microsoft Azure provider",
                    insert_text="azure",
                ),
                CompletionItem(
                    label="snowflake",
                    kind=CompletionType.PROVIDER,
                    detail="Snowflake Data Platform",
                    documentation="Snowflake Data Platform provider",
                    insert_text="snowflake",
                ),
            ],
            "contract_fields": [
                CompletionItem(
                    label="meta",
                    kind=CompletionType.FIELD_NAME,
                    detail="Contract metadata",
                    documentation="Metadata section containing contract information",
                    insert_text="meta:\n  name: ${1:contract-name}\n  version: ${2:1.0.0}",
                ),
                CompletionItem(
                    label="sources",
                    kind=CompletionType.FIELD_NAME,
                    detail="Data sources",
                    documentation="Data sources section defining input data",
                    insert_text="sources:\n  - name: ${1:source-name}\n    type: ${2:table}",
                ),
                CompletionItem(
                    label="transforms",
                    kind=CompletionType.FIELD_NAME,
                    detail="Data transformations",
                    documentation="Data transformation logic",
                    insert_text="transforms:\n  - name: ${1:transform-name}\n    type: ${2:sql}",
                ),
                CompletionItem(
                    label="exposures",
                    kind=CompletionType.FIELD_NAME,
                    detail="Data exposures",
                    documentation="Output data products and exposures",
                    insert_text="exposures:\n  - name: ${1:exposure-name}\n    type: ${2:table}",
                ),
            ],
        }

    def initialize(self, workspace_root: str) -> Dict[str, Any]:
        """Initialize language server with workspace"""
        self.workspace_root = Path(workspace_root)

        capabilities = {
            "completionProvider": {
                "resolveProvider": True,
                "triggerCharacters": [".", ":", "-", " "],
            },
            "diagnosticProvider": True,
            "documentFormattingProvider": True,
            "documentSymbolProvider": True,
            "hoverProvider": True,
            "definitionProvider": True,
            "codeActionProvider": True,
        }

        return {"capabilities": capabilities}

    def get_completions(
        self, file_path: str, line: int, column: int, context: str
    ) -> List[CompletionItem]:
        """Get auto-completion suggestions"""
        completions = []

        # Determine completion context
        current_line = context.split("\n")[line] if line < len(context.split("\n")) else ""
        prefix = current_line[:column].strip()

        # Command completions (for CLI usage)
        if prefix.startswith("fluid ") or not prefix:
            completions.extend(self.completion_data["commands"])

        # Provider completions
        elif "--provider" in prefix or "provider:" in prefix:
            completions.extend(self.completion_data["providers"])

        # Contract field completions (for YAML files)
        elif file_path.endswith((".yaml", ".yml")):
            if self._is_contract_file(file_path):
                completions.extend(self.completion_data["contract_fields"])

                # Add dynamic completions based on contract content
                completions.extend(self._get_dynamic_completions(file_path, context))

        # File path completions
        if any(keyword in prefix for keyword in ["--out", "--contract", "contract:"]):
            completions.extend(self._get_file_path_completions())

        # Sort by relevance
        completions.sort(key=lambda x: x.score, reverse=True)

        return completions[:20]  # Limit to top 20

    def _is_contract_file(self, file_path: str) -> bool:
        """Check if file is a FLUID contract"""
        try:
            with open(file_path, encoding="utf-8") as f:
                content = f.read()
                return "meta:" in content or "sources:" in content or "transforms:" in content
        except Exception:
            return False

    def _get_dynamic_completions(self, file_path: str, context: str) -> List[CompletionItem]:
        """Get dynamic completions based on file content"""
        completions = []

        try:
            # Parse existing content for source names, transform names, etc.
            lines = context.split("\n")
            sources = []
            transforms = []

            for line in lines:
                if "- name:" in line:
                    name = line.split("name:")[-1].strip()
                    if name:
                        # Determine if it's under sources or transforms
                        # This is simplified - real implementation would use proper YAML parsing
                        if any("sources:" in prev_line for prev_line in lines[: lines.index(line)]):
                            sources.append(name)
                        elif any(
                            "transforms:" in prev_line for prev_line in lines[: lines.index(line)]
                        ):
                            transforms.append(name)

            # Add source references for transforms
            for source in sources:
                completions.append(
                    CompletionItem(
                        label=f'ref("{source}")',
                        kind=CompletionType.FIELD_NAME,
                        detail=f"Reference to source {source}",
                        documentation=f"Reference to the {source} data source",
                        insert_text=f'ref("{source}")',
                    )
                )

            # Add transform references
            for transform in transforms:
                completions.append(
                    CompletionItem(
                        label=f'ref("{transform}")',
                        kind=CompletionType.FIELD_NAME,
                        detail=f"Reference to transform {transform}",
                        documentation=f"Reference to the {transform} transformation",
                        insert_text=f'ref("{transform}")',
                    )
                )

        except Exception:
            pass

        return completions

    def _get_file_path_completions(self) -> List[CompletionItem]:
        """Get file path completions"""
        completions = []

        if not self.workspace_root:
            return completions

        try:
            # Find contract files
            for file_path in self.workspace_root.rglob("*.yaml"):
                if self._is_contract_file(str(file_path)):
                    relative_path = file_path.relative_to(self.workspace_root)
                    completions.append(
                        CompletionItem(
                            label=str(relative_path),
                            kind=CompletionType.CONTRACT_PATH,
                            detail="FLUID contract",
                            documentation=f"FLUID contract file: {relative_path}",
                            insert_text=str(relative_path),
                        )
                    )

            # Find example files
            examples_dir = self.workspace_root / "examples"
            if examples_dir.exists():
                for file_path in examples_dir.rglob("*.yaml"):
                    relative_path = file_path.relative_to(self.workspace_root)
                    completions.append(
                        CompletionItem(
                            label=str(relative_path),
                            kind=CompletionType.CONTRACT_PATH,
                            detail="Example contract",
                            documentation=f"Example FLUID contract: {relative_path}",
                            insert_text=str(relative_path),
                        )
                    )

        except Exception:
            pass

        return completions

    def validate_file(self, file_path: str, content: str) -> List[DiagnosticItem]:
        """Validate file and return diagnostics"""
        diagnostics = []

        try:
            # Basic YAML validation for contract files
            if file_path.endswith((".yaml", ".yml")) and self._is_contract_file(file_path):
                diagnostics.extend(self._validate_contract_syntax(file_path, content))
                diagnostics.extend(self._validate_contract_semantics(file_path, content))

        except Exception as e:
            diagnostics.append(
                DiagnosticItem(
                    file_path=file_path,
                    line=1,
                    column=1,
                    severity="error",
                    message=f"Validation error: {str(e)}",
                    code="validation_error",
                )
            )

        return diagnostics

    def _validate_contract_syntax(self, file_path: str, content: str) -> List[DiagnosticItem]:
        """Validate contract syntax"""
        diagnostics = []

        try:
            import yaml

            yaml.safe_load(content)
        except yaml.YAMLError as e:
            line = getattr(e, "problem_mark", None)
            line_num = line.line + 1 if line else 1
            col_num = line.column + 1 if line else 1

            diagnostics.append(
                DiagnosticItem(
                    file_path=file_path,
                    line=line_num,
                    column=col_num,
                    severity="error",
                    message=f"YAML syntax error: {str(e)}",
                    code="yaml_syntax",
                )
            )
        except Exception:
            pass

        return diagnostics

    def _validate_contract_semantics(self, file_path: str, content: str) -> List[DiagnosticItem]:
        """Validate contract semantics"""
        diagnostics = []
        lines = content.split("\n")

        # Check for required sections
        has_meta = any("meta:" in line for line in lines)
        has_sources = any("sources:" in line for line in lines)

        if not has_meta:
            diagnostics.append(
                DiagnosticItem(
                    file_path=file_path,
                    line=1,
                    column=1,
                    severity="warning",
                    message="Contract should have a meta section",
                    code="missing_meta",
                )
            )

        if not has_sources:
            diagnostics.append(
                DiagnosticItem(
                    file_path=file_path,
                    line=1,
                    column=1,
                    severity="warning",
                    message="Contract should have a sources section",
                    code="missing_sources",
                )
            )

        # Check for common patterns and best practices
        for i, line in enumerate(lines):
            line_num = i + 1

            # Check for potential naming issues
            if "name:" in line:
                name = line.split("name:")[-1].strip()
                if name and " " in name:
                    diagnostics.append(
                        DiagnosticItem(
                            file_path=file_path,
                            line=line_num,
                            column=line.find("name:") + 1,
                            severity="warning",
                            message="Names should not contain spaces, use underscores or hyphens",
                            code="naming_convention",
                        )
                    )

            # Check for missing descriptions
            if line.strip().startswith("- name:") and i + 1 < len(lines):
                next_lines = lines[i + 1 : i + 5]  # Check next few lines
                has_description = any("description:" in next_line for next_line in next_lines)
                if not has_description:
                    diagnostics.append(
                        DiagnosticItem(
                            file_path=file_path,
                            line=line_num,
                            column=1,
                            severity="info",
                            message="Consider adding a description for better documentation",
                            code="missing_description",
                        )
                    )

        return diagnostics

    def format_document(self, file_path: str, content: str) -> str:
        """Format document content"""
        try:
            if file_path.endswith((".yaml", ".yml")):
                import yaml

                # Parse and reformat YAML
                data = yaml.safe_load(content)
                return yaml.dump(data, default_flow_style=False, sort_keys=False, indent=2)
        except Exception:
            pass

        return content  # Return original if formatting fails


class IDEIntegration:
    """IDE integration manager"""

    def __init__(self):
        self.console = Console() if RICH_AVAILABLE else None
        self.language_server = FluidLanguageServer()
        self.config_dir = Path.home() / ".fluid" / "ide"
        self.config_dir.mkdir(parents=True, exist_ok=True)

    def setup_vscode_extension(self) -> bool:
        """Setup VS Code extension"""
        try:
            extension_dir = self.config_dir / "vscode-extension"
            extension_dir.mkdir(exist_ok=True)

            # Create package.json for VS Code extension
            package_json = {
                "name": "fluid-language-support",
                "displayName": "FLUID Language Support",
                "description": "Language support for FLUID data product contracts",
                "version": "1.0.0",
                "engines": {"vscode": "^1.60.0"},
                "categories": ["Programming Languages"],
                "activationEvents": [
                    "onLanguage:yaml",
                    "onCommand:fluid.validate",
                    "onCommand:fluid.plan",
                ],
                "main": "./out/extension.js",
                "contributes": {
                    "languages": [
                        {
                            "id": "fluid-yaml",
                            "aliases": ["FLUID YAML", "fluid"],
                            "extensions": [".fluid.yaml", ".fluid.yml"],
                            "configuration": "./language-configuration.json",
                        }
                    ],
                    "grammars": [
                        {
                            "language": "fluid-yaml",
                            "scopeName": "source.yaml.fluid",
                            "path": "./syntaxes/fluid-yaml.tmGrammar.json",
                        }
                    ],
                    "commands": [
                        {
                            "command": "fluid.validate",
                            "title": "Validate Contract",
                            "category": "FLUID",
                        },
                        {"command": "fluid.plan", "title": "Generate Plan", "category": "FLUID"},
                        {"command": "fluid.apply", "title": "Apply Plan", "category": "FLUID"},
                    ],
                    "keybindings": [
                        {
                            "command": "fluid.validate",
                            "key": "ctrl+shift+v",
                            "when": "editorTextFocus && resourceExtname =~ /\\.(fluid\\.)?(yaml|yml)$/",
                        }
                    ],
                    "configuration": {
                        "title": "FLUID",
                        "properties": {
                            "fluid.provider": {
                                "type": "string",
                                "default": "local",
                                "description": "Default FLUID provider",
                            },
                            "fluid.autoValidate": {
                                "type": "boolean",
                                "default": True,
                                "description": "Automatically validate contracts on save",
                            },
                        },
                    },
                },
            }

            with open(extension_dir / "package.json", "w", encoding="utf-8") as f:
                json.dump(package_json, f, indent=2)

            # Create language configuration
            language_config = {
                "comments": {"lineComment": "#"},
                "brackets": [["[", "]"], ["{", "}"], ["(", ")"]],
                "autoClosingPairs": [
                    {"open": "[", "close": "]"},
                    {"open": "{", "close": "}"},
                    {"open": "(", "close": ")"},
                    {"open": '"', "close": '"', "notIn": ["string"]},
                    {"open": "'", "close": "'", "notIn": ["string", "comment"]},
                ],
            }

            with open(extension_dir / "language-configuration.json", "w", encoding="utf-8") as f:
                json.dump(language_config, f, indent=2)

            # Create basic TypeScript extension code
            extension_ts = """
import * as vscode from 'vscode';
import { exec } from 'child_process';

export function activate(context: vscode.ExtensionContext) {
    // Register commands
    context.subscriptions.push(
        vscode.commands.registerCommand('fluid.validate', () => {
            const editor = vscode.window.activeTextEditor;
            if (editor) {
                validateContract(editor.document.fileName);
            }
        })
    );
    
    context.subscriptions.push(
        vscode.commands.registerCommand('fluid.plan', () => {
            const editor = vscode.window.activeTextEditor;
            if (editor) {
                generatePlan(editor.document.fileName);
            }
        })
    );
    
    // Auto-validation on save
    context.subscriptions.push(
        vscode.workspace.onDidSaveTextDocument((document) => {
            const config = vscode.workspace.getConfiguration('fluid');
            if (config.get('autoValidate') && isFluidContract(document.fileName)) {
                validateContract(document.fileName);
            }
        })
    );
}

function isFluidContract(fileName: string): boolean {
    return fileName.endsWith('.fluid.yaml') || fileName.endsWith('.fluid.yml') ||
           (fileName.endsWith('.yaml') || fileName.endsWith('.yml'));
}

function validateContract(fileName: string) {
    const terminal = vscode.window.createTerminal('FLUID Validate');
    terminal.sendText(`fluid validate "${fileName}"`);
    terminal.show();
}

function generatePlan(fileName: string) {
    const terminal = vscode.window.createTerminal('FLUID Plan');
    terminal.sendText(`fluid plan "${fileName}"`);
    terminal.show();
}

export function deactivate() {}
"""

            src_dir = extension_dir / "src"
            src_dir.mkdir(exist_ok=True)

            with open(src_dir / "extension.ts", "w", encoding="utf-8") as f:
                f.write(extension_ts.strip())

            # Create tsconfig.json
            tsconfig = {
                "compilerOptions": {
                    "module": "commonjs",
                    "target": "es6",
                    "outDir": "out",
                    "lib": ["es6"],
                    "sourceMap": True,
                    "rootDir": "src",
                    "strict": True,
                },
                "exclude": ["node_modules", ".vscode-test"],
            }

            with open(extension_dir / "tsconfig.json", "w", encoding="utf-8") as f:
                json.dump(tsconfig, f, indent=2)

            if self.console:
                self.console.print(
                    f"[green]✅ VS Code extension template created at {extension_dir}[/green]"
                )
                self.console.print(
                    "[dim]Run 'npm install' and 'npm run compile' to build the extension[/dim]"
                )

            return True

        except Exception as e:
            if self.console:
                self.console.print(f"[red]Failed to setup VS Code extension: {e}[/red]")
            return False

    def install_shell_completion(self, shell: str = "bash") -> bool:
        """Install shell completion scripts"""
        try:
            completion_script = self._generate_completion_script(shell)

            if shell == "bash":
                completion_file = Path.home() / ".bash_completion.d" / "fluid"
                completion_file.parent.mkdir(exist_ok=True)

                with open(completion_file, "w", encoding="utf-8") as f:
                    f.write(completion_script)

                if self.console:
                    self.console.print("[green]✅ Bash completion installed[/green]")
                    self.console.print(
                        "[dim]Restart your shell or run 'source ~/.bash_completion.d/fluid'[/dim]"
                    )

            elif shell == "zsh":
                completion_file = Path.home() / ".zsh" / "completions" / "_fluid"
                completion_file.parent.mkdir(parents=True, exist_ok=True)

                with open(completion_file, "w", encoding="utf-8") as f:
                    f.write(completion_script)

                if self.console:
                    self.console.print("[green]✅ Zsh completion installed[/green]")
                    self.console.print(
                        "[dim]Add 'fpath=(~/.zsh/completions $fpath)' to ~/.zshrc[/dim]"
                    )

            return True

        except Exception as e:
            if self.console:
                self.console.print(f"[red]Failed to install shell completion: {e}[/red]")
            return False

    def _generate_completion_script(self, shell: str) -> str:
        """Generate shell completion script"""
        if shell == "bash":
            return """
_fluid_completion() {
    local cur prev opts
    COMPREPLY=()
    cur="${COMP_WORDS[COMP_CWORD]}"
    prev="${COMP_WORDS[COMP_CWORD-1]}"
    
    # Top-level commands
    opts="validate plan apply viz-graph market auth forge admin doctor version providers"
    
    case "${prev}" in
        fluid)
            COMPREPLY=( $(compgen -W "${opts}" -- ${cur}) )
            return 0
            ;;
        --provider)
            COMPREPLY=( $(compgen -W "local gcp aws azure snowflake" -- ${cur}) )
            return 0
            ;;
        validate|plan|apply)
            # Complete with .yaml files
            COMPREPLY=( $(compgen -f -X '!*.yaml' -- ${cur}) $(compgen -f -X '!*.yml' -- ${cur}) )
            return 0
            ;;
        *)
            ;;
    esac
    
    COMPREPLY=( $(compgen -W "${opts}" -- ${cur}) )
    return 0
}
complete -F _fluid_completion fluid
"""

        elif shell == "zsh":
            return """
#compdef fluid

_fluid() {
    local context state line
    typeset -A opt_args
    
    _arguments \\
        '1: :_fluid_commands' \\
        '*::arg:->args'
    
    case $state in
        args)
            case $words[1] in
                validate|plan|apply)
                    _files -g "*.yaml" -g "*.yml"
                    ;;
                --provider)
                    _arguments \\
                        '*:provider:(local gcp aws azure snowflake)'
                    ;;
            esac
            ;;
    esac
}

_fluid_commands() {
    local commands
    commands=(
        'validate:Validate FLUID contract'
        'plan:Generate execution plan'
        'apply:Apply execution plan'
        'viz-graph:Generate data lineage graph'
        'market:Data marketplace discovery'
        'auth:Authentication management'
        'forge:Interactive project creation'
        'admin:Administrative commands'
        'doctor:Environment diagnostics'
        'version:Show version information'
        'providers:List available providers'
    )
    _describe 'commands' commands
}

_fluid
"""

        return ""


# CLI Integration
def register(subparsers: argparse._SubParsersAction):
    """Register the ide command"""
    p = subparsers.add_parser(COMMAND, help="💻 IDE integration and developer experience features")

    ide_subparsers = p.add_subparsers(dest="ide_action", help="IDE actions")

    # Setup IDE integration
    setup_parser = ide_subparsers.add_parser("setup", help="Setup IDE integration")
    setup_parser.add_argument(
        "--ide", choices=[ide.value for ide in IDEType], default="vscode", help="IDE type"
    )

    # Language server
    lsp_parser = ide_subparsers.add_parser("lsp", help="Language server operations")
    lsp_subparsers = lsp_parser.add_subparsers(dest="lsp_action")

    start_lsp_parser = lsp_subparsers.add_parser("start", help="Start language server")
    start_lsp_parser.add_argument("--port", type=int, default=9257, help="Server port")

    completions_parser = lsp_subparsers.add_parser("completions", help="Get completions")
    completions_parser.add_argument("file", help="File path")
    completions_parser.add_argument("line", type=int, help="Line number")
    completions_parser.add_argument("column", type=int, help="Column number")

    # Shell completion
    completion_parser = ide_subparsers.add_parser("completion", help="Install shell completion")
    completion_parser.add_argument(
        "--shell", choices=["bash", "zsh", "fish"], default="bash", help="Shell type"
    )

    # Validate integration
    validate_parser = ide_subparsers.add_parser("validate", help="Validate file")
    validate_parser.add_argument("file", help="File to validate")

    p.set_defaults(func=run)


def run(args, logger: logging.Logger) -> int:
    """Main entry point for ide command"""
    try:
        ide_integration = IDEIntegration()

        if args.ide_action == "setup":
            return handle_setup_ide(args, ide_integration, logger)
        elif args.ide_action == "lsp":
            return handle_language_server(args, ide_integration, logger)
        elif args.ide_action == "completion":
            return handle_shell_completion(args, ide_integration, logger)
        elif args.ide_action == "validate":
            return handle_file_validation(args, ide_integration, logger)
        else:
            if RICH_AVAILABLE:
                console = Console()
                console.print(
                    "[red]❌ Unknown IDE action. Use 'fluid ide --help' for available options.[/red]"
                )
            return 1

    except Exception as e:
        logger.exception("IDE command failed")
        if RICH_AVAILABLE:
            console = Console()
            console.print(f"[red]❌ IDE command failed: {e}[/red]")
        return 1


def handle_setup_ide(args, ide_integration: IDEIntegration, logger: logging.Logger) -> int:
    """Handle IDE setup"""
    if not RICH_AVAILABLE:
        cprint("IDE setup requires rich library")
        return 1

    console = Console()
    ide_type = IDEType(args.ide)

    if ide_type == IDEType.VSCODE:
        success = ide_integration.setup_vscode_extension()
        if success:
            console.print("[green]✅ VS Code integration setup complete[/green]")
            console.print("\n[bold]Next steps:[/bold]")
            console.print("1. Install the extension: code --install-extension <path-to-extension>")
            console.print("2. Open a FLUID contract file (.yaml)")
            console.print("3. Use Ctrl+Shift+V to validate contracts")
        return 0 if success else 1
    else:
        console.print(
            f"[yellow]⚠️ {ide_type.value.title()} integration not yet implemented[/yellow]"
        )
        console.print("[dim]Currently supported: VS Code[/dim]")
        return 1


def handle_language_server(args, ide_integration: IDEIntegration, logger: logging.Logger) -> int:
    """Handle language server operations"""
    if not RICH_AVAILABLE:
        cprint("Language server requires rich library")
        return 1

    console = Console()

    if args.lsp_action == "start":
        console.print(f"[blue]🚀 Starting FLUID Language Server on port {args.port}[/blue]")
        console.print("[dim]Language server functionality would be implemented here[/dim]")
        console.print(
            "[dim]This would start a Language Server Protocol server for IDE integration[/dim]"
        )
        return 0

    elif args.lsp_action == "completions":
        try:
            with open(args.file, encoding="utf-8") as f:
                content = f.read()

            completions = ide_integration.language_server.get_completions(
                args.file, args.line - 1, args.column, content
            )

            # Output completions as JSON for IDE consumption
            completion_response = {"completions": [comp.to_dict() for comp in completions]}

            cprint_json(json.dumps(completion_response, indent=2))
            return 0

        except Exception as e:
            console.print(f"[red]❌ Failed to get completions: {e}[/red]")
            return 1

    return 0


def handle_shell_completion(args, ide_integration: IDEIntegration, logger: logging.Logger) -> int:
    """Handle shell completion installation"""
    success = ide_integration.install_shell_completion(args.shell)
    return 0 if success else 1


def handle_file_validation(args, ide_integration: IDEIntegration, logger: logging.Logger) -> int:
    """Handle file validation for IDE"""
    if not RICH_AVAILABLE:
        cprint("File validation requires rich library")
        return 1

    console = Console()

    try:
        with open(args.file, encoding="utf-8") as f:
            content = f.read()

        diagnostics = ide_integration.language_server.validate_file(args.file, content)

        if not diagnostics:
            console.print("[green]✅ No issues found[/green]")
            return 0

        # Display diagnostics
        table = Table(title="Validation Results")
        table.add_column("Line", style="cyan")
        table.add_column("Severity", style="yellow")
        table.add_column("Message", style="white")
        table.add_column("Code", style="dim")

        for diag in diagnostics:
            severity_color = {"error": "red", "warning": "yellow", "info": "blue"}.get(
                diag.severity, "white"
            )

            table.add_row(
                str(diag.line),
                f"[{severity_color}]{diag.severity}[/{severity_color}]",
                diag.message,
                diag.code or "",
            )

        console.print(table)

        # Return error if any errors found
        has_errors = any(diag.severity == "error" for diag in diagnostics)
        return 1 if has_errors else 0

    except Exception as e:
        console.print(f"[red]❌ Failed to validate file: {e}[/red]")
        return 1
