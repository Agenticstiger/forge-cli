#!/usr/bin/env python3
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
Update forge templates to use FLUID schema 0.5.7 field names
"""

import re
from pathlib import Path


def update_template(filepath):
    """Update a single template file"""
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    original = content

    # Replace 'build': { with 'builds': [{
    content = re.sub(
        r"(\s+)'build': \{",
        r"\1'builds': [  # Changed from 'build' to 'builds' array\n\1    {",
        content,
    )

    # Close builds array - find the closing brace of build object before exposes
    content = re.sub(
        r"(\s+)\}\s*\n(\s+)'exposes':",
        r"\1    }\n\1],  # Close builds array\n\2'exposes':",
        content,
    )

    # Replace expose 'id': with 'exposeId':
    content = re.sub(
        r"(\s+)'id': '([^']+)',(\s*#[^\n]*)?\n(\s+)'type': '(table|view|file|api)',",
        r"\1'exposeId': '\2',  # Changed from 'id'\n\4'kind': '\5',  # Changed from 'type'",
        content,
    )

    # Replace 'location': { ... 'properties': { with 'binding': {
    # This is complex - let's do it step by step

    # Pattern 1: location with format and properties containing dataset/table
    content = re.sub(
        r"'location': \{\s*'format': '([^']+)',\s*'properties': \{\s*'dataset': '([^']+)',\s*'table': '([^']+)'\s*\}\s*\},",
        r"'binding': {  # Changed from 'location'\n                        'format': '\1',\n                        'dataset': '\2',\n                        'table': '\3'\n                    },",
        content,
        flags=re.MULTILINE | re.DOTALL,
    )

    # Pattern 2: location with format and properties containing path
    content = re.sub(
        r"'location': \{\s*'format': '([^']+)',\s*'properties': \{\s*'path': '([^']+)'\s*\}\s*\},",
        r"'binding': {  # Changed from 'location'\n                        'format': '\1',\n                        'location': '\2'\n                    },",
        content,
        flags=re.MULTILINE | re.DOTALL,
    )

    if content != original:
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"✅ Updated {filepath.name}")
        return True
    else:
        print(f"⚠️  No changes needed for {filepath.name}")
        return False


# Update all templates
templates_dir = Path("fluid_build/forge/templates")
templates = ["analytics.py", "etl_pipeline.py", "ml_pipeline.py", "streaming.py"]

updated = 0
for template in templates:
    filepath = templates_dir / template
    if filepath.exists():
        if update_template(filepath):
            updated += 1

print(f"\n✅ Updated {updated} template(s)")
