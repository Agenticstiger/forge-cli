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
import stat
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# H17 — one-shot log gate so the "Loaded N credentials from .env files" line
# only renders once per process even when multiple credential consumers
# (connector / credential-store / catalog-resolver / …) each instantiate
# their own ``DotEnvCredentialStore``. Tests reset this via the public
# ``reset_dotenv_load_log()`` helper at the bottom of the module.
_LOGGED_CRED_COUNT: bool = False


def _warn_if_world_readable(path: Path) -> None:
    """Warn when a credential-bearing ``.env`` file is group/other-readable.

    Real secrets live in ``.env`` / ``.env.local`` — on a shared host a
    group- or other-readable mode leaks them. POSIX-only (Windows
    ``st_mode`` does not carry these permission bits).
    """
    if os.name != "posix":
        return
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return
    if mode & 0o077:
        logger.warning(
            "Credential file %s is mode %o — readable by group/other. " "Run: chmod 600 %s",
            path,
            mode,
            path,
        )


try:
    from dotenv import dotenv_values, load_dotenv

    DOTENV_AVAILABLE = True
except ImportError:
    DOTENV_AVAILABLE = False

    # Provide a no-op ``load_dotenv`` so callers (``cli/_common.hydrate_dotenv``)
    # that ``from fluid_build.credentials.dotenv_store import load_dotenv`` keep
    # importing cleanly when ``python-dotenv`` is absent. The helper logs a
    # debug skip in that path and returns early, so a missing optional dep is
    # graceful rather than a hard ImportError on every CLI invocation.
    def load_dotenv(*args, **kwargs):  # type: ignore[no-redef]
        return False


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
        # Snapshot env vars the operator set BEFORE this store runs. ``load()``
        # uses this snapshot to decide which keys ``.env`` is allowed to push
        # into ``os.environ``: preexisting keys represent the operator's
        # explicit intent (e.g. ``set -a; . fluid.local.env; set +a``) and must
        # not be clobbered by a project ``.env`` placeholder like
        # ``DMM_API_KEY=``. The secrets-file override path in
        # ``cli._common.hydrate_dotenv`` is a separate, explicit override
        # (``load_dotenv(path, override=True)``) and still wins — that's the
        # documented way to push operator-supplied values in.
        self._preexisting_env_keys = frozenset(os.environ)

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
                _warn_if_world_readable(env_file)
                logger.debug(f"Loading credentials from {env_file.name}")
                try:
                    values = dotenv_values(env_file)
                    combined.update(values)
                except Exception as e:
                    logger.warning(f"Failed to load {env_file}: {e}")

        # Export to ``os.environ`` for backward compatibility, but never
        # override an env var the operator set BEFORE this store ran. Previous
        # behavior called ``load_dotenv(path, override=True)`` per file, which
        # silently clobbered shell-set values (e.g. a valid ``DMM_API_KEY``
        # sourced from ``fluid.local.env``) with empty placeholders from
        # project ``.env``. The explicit override path for operator secrets is
        # ``cli._common.hydrate_dotenv`` (loads ``FLUID_SECRETS_FILE`` with
        # ``override=True`` AFTER this store runs) — that still works and is
        # the documented way to push operator values into the process.
        for key, value in combined.items():
            if value is None:
                continue
            if key in self._preexisting_env_keys:
                continue
            os.environ[key] = value

        self._cache = combined
        self._loaded = True

        # Security: Log loaded keys (NOT values)
        if combined:
            global _LOGGED_CRED_COUNT
            if not _LOGGED_CRED_COUNT:
                # H17 — first-loader wins. Subsequent stores from the same
                # process drop to DEBUG so ``fluid auth status`` doesn't
                # echo the same banner 7× across connector / store /
                # resolver bootstraps.
                logger.info(f"Loaded {len(combined)} credentials from .env files")
                _LOGGED_CRED_COUNT = True
            else:
                logger.debug(f"Loaded {len(combined)} credentials from .env files (duplicate)")
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

        with open(output_path, "w", encoding="utf-8") as f:
            f.write(f"# {provider.upper()} Credentials\n")
            f.write("# Copy to .env and fill in your actual values\n")
            f.write("# DO NOT commit .env to Git!\n\n")

            for key, description in credentials.items():
                f.write(f"# {description}\n")
                f.write(f"{key}=your_{key.lower()}_here\n\n")

        # Defence-in-depth: the example file carries only placeholders, but
        # operators routinely ``cp .env.example .env`` and fill in real
        # secrets — start it owner-only so a copy inherits a safe mode.
        try:
            os.chmod(output_path, 0o600)
        except (NotImplementedError, OSError):
            pass

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
        content = gitignore.read_text(encoding="utf-8")
        existing_entries = set(line.strip() for line in content.splitlines())
    else:
        content = ""
        existing_entries = set()

    new_entries = [entry for entry in entries if entry not in existing_entries]

    if new_entries:
        with open(gitignore, "a", encoding="utf-8") as f:
            f.write("\n# FLUID CLI - Environment files\n")
            for entry in new_entries:
                f.write(f"{entry}\n")

        logger.info(f"Added {len(new_entries)} entries to .gitignore")


def reset_dotenv_load_log() -> None:
    """Reset the one-shot "Loaded N credentials" log gate.

    Used by tests that exercise the first-time log path more than once
    in a single process. Production code should not need this — the gate
    intentionally lasts for the lifetime of the process so multi-store
    bootstraps (connector / credential-store / catalog-resolver) don't
    each emit the banner.
    """
    global _LOGGED_CRED_COUNT
    _LOGGED_CRED_COUNT = False
