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
Project History Extension for FLUID Forge

Tracks and enables reuse of previous project configurations.
Helps teams quickly bootstrap similar projects by reusing proven configurations.
"""

import json
from pathlib import Path
from typing import Any, Dict, List

from ..core.interfaces import Extension, GenerationContext


class ProjectHistoryExtension(Extension):
    """Extension for tracking and reusing project configurations"""

    def __init__(self):
        self.history_file = Path.home() / ".fluid" / "forge_history.json"
        self.history_file.parent.mkdir(exist_ok=True)

    def get_metadata(self) -> Dict[str, Any]:
        return {
            "name": "Project History Tracker",
            "description": "Track and reuse previous project configurations",
            "version": "1.0.0",
            "author": "FLUID Build Team",
        }

    def on_generation_complete(self, context: GenerationContext) -> None:
        """Save project configuration to history"""
        try:
            # Load existing history
            history = self._load_history()

            # Create new history entry
            entry = {
                "name": context.project_config.get("name"),
                "template": context.project_config.get("template"),
                "provider": context.project_config.get("provider"),
                "domain": context.project_config.get("domain"),
                "owner": context.project_config.get("owner"),
                "created_at": context.creation_time,
                "forge_version": context.forge_version,
                "success": True,
            }

            # Add to history
            history.append(entry)

            # Keep only last 50 entries
            history = history[-50:]

            # Save updated history
            self._save_history(history)

        except Exception:
            # Don't fail the entire process if history tracking fails
            pass

    def _load_history(self) -> List[Dict[str, Any]]:
        """Load project history from file"""
        if self.history_file.exists():
            try:
                with self.history_file.open("r") as f:
                    return json.load(f)
            except (OSError, json.JSONDecodeError):
                return []
        return []

    def _save_history(self, history: List[Dict[str, Any]]) -> None:
        """Save project history to file"""
        try:
            with self.history_file.open("w") as f:
                json.dump(history, f, indent=2)
        except OSError:
            pass
