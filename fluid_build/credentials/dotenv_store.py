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
.env file credential storage.

Loads credentials from .env files with environment-specific overrides.
Supports .env, .env.{environment}, and .env.local patterns.
"""

import logging
import os
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)

try:
    from dotenv import dotenv_values, load_dotenv

    DOTENV_AVAILABLE = True
except ImportError:
    DOTENV_AVAILABLE = False


class DotEnvCredentialStore:
    """
    Load credentials from .env files with security best practices.

    Search order:
    1. .env (base configuration)
    2. .env.{environment} (e.g., .env.dev, .env.prod)
    3. .env.local (local overrides, highest priority)

    All values are cached and automatically loaded into os.environ
    for backward compatibility.
    """

    def __init__(self, project_root: Optional[Path] = None, environment: Optional[str] = None):
        """
        Initialize .env credential store.

        Args:
            project_root: Root directory to search for .env files (default: cwd)
            environment: Environment name (dev, staging, prod)
        """
        if not DOTENV_AVAILABLE:
            raise ImportError(
                "python-dotenv package required for .env file support. "
                "Install with: pip install python-dotenv"
            )

        self.project_root = Path(project_root) if project_root else Path.cwd()
        self.environment = environment or os.environ.get("FLUID_ENV", "dev")
        self._cache: Optional[Dict[str, str]] = None
        self._loaded = False

        logger.debug(f"Initialized .env store: {self.project_root} (env: {self.environment})")

    def load(self) -> Dict[str, str]:
        """
        Load all .env files in priority order.

        Returns:
            Combined dictionary of all loaded values
        """
        if self._cache is not None:
            return self._cache

        combined = {}

        # Load in reverse priority order (later files override earlier)
        env_files = [
            self.project_root / ".env",  # Base config
            self.project_root / f".env.{self.environment}",  # Environment-specific
            self.project_root / ".env.local",  # Local overrides (highest priority)
        ]

        for env_file in env_files:
            if env_file.exists():
                logger.debug(f"Loading credentials from {env_file.name}")
                try:
                    values = dotenv_values(env_file)
                    combined.update(values)

                    # Also load into os.environ for backward compatibility
                    load_dotenv(env_file, override=True)
                except Exception as e:
                    logger.warning(f"Failed to load {env_file}: {e}")

        self._cache = combined
        self._loaded = True

        # Security: Log loaded keys (NOT values)
        if combined:
            logger.info(f"Loaded {len(combined)} credentials from .env files")
            logger.debug(f"Available keys: {', '.join(sorted(combined.keys()))}")

        return combined

    def get_credential(self, key: str) -> Optional[str]:
        """
        Get a credential value from .env files.

        Args:
            key: Credential key (e.g., "SNOWFLAKE_PASSWORD")

        Returns:
            Credential value or None if not found
        """
        values = self.load()
        return values.get(key)

    def has_credential(self, key: str) -> bool:
        """Check if credential exists."""
        return self.get_credential(key) is not None

    @staticmethod
    def create_example_file(
        output_path: Path, credentials: Dict[str, str], provider: str = "example"
    ):
        """
        Create a .env.example file with placeholder values.

        Args:
            output_path: Path to .env.example file
            credentials: Dict of key -> description
            provider: Provider name for documentation
        """
        output_path.parent.mkdir(parents=True, exist_ok=True)

        with open(output_path, "w") as f:
            f.write(f"# {provider.upper()} Credentials\n")
            f.write("# Copy to .env and fill in your actual values\n")
            f.write("# DO NOT commit .env to Git!\n\n")

            for key, description in credentials.items():
                f.write(f"# {description}\n")
                f.write(f"{key}=your_{key.lower()}_here\n\n")

        logger.info(f"Created example file: {output_path}")


def ensure_gitignore(project_root: Path):
    """
    Ensure .env files are in .gitignore.

    Adds if not already present:
    - .env
    - .env.local
    - .env.*.local
    """
    gitignore = project_root / ".gitignore"

    entries = [".env", ".env.local", ".env.*.local"]

    if gitignore.exists():
        content = gitignore.read_text()
        existing_entries = set(line.strip() for line in content.splitlines())
    else:
        content = ""
        existing_entries = set()

    new_entries = [entry for entry in entries if entry not in existing_entries]

    if new_entries:
        with open(gitignore, "a") as f:
            f.write("\n# FLUID CLI - Environment files\n")
            for entry in new_entries:
                f.write(f"{entry}\n")

        logger.info(f"Added {len(new_entries)} entries to .gitignore")
