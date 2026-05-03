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
Starter Template for FLUID Forge

A simple, beginner-friendly template for creating basic FLUID data products.
Perfect for learning, prototyping, and quick MVP development.

Features:
- Minimal configuration and dependencies
- Basic SQL-based data transformation
- Local execution support
- Comprehensive documentation
- Test framework setup
- Git repository initialization

This template follows FLUID 0.7.x specification and provides a solid
foundation that can be extended as projects mature.
"""

from pathlib import Path
from typing import Any, Dict, List

from ..core.interfaces import (
    ComplexityLevel,
    GenerationContext,
    ProjectTemplate,
    TemplateMetadata,
    ValidationResult,
)


class StarterTemplate(ProjectTemplate):
    """
    Starter template for basic FLUID data products

    This template creates a minimal but complete data product structure
    suitable for learning FLUID concepts and rapid prototyping.
    """

    def get_metadata(self) -> TemplateMetadata:
        """Return starter template metadata"""
        return TemplateMetadata(
            name="Starter Data Product",
            description="Simple MVP template for quick setup with minimal configuration",
            complexity=ComplexityLevel.BEGINNER,
            provider_support=["local", "gcp", "snowflake", "bigquery"],
            use_cases=[
                "Quick prototyping and experimentation",
                "Learning FLUID concepts",
                "Simple data processing tasks",
                "Getting started with data products",
            ],
            technologies=["SQL", "YAML", "Python"],
            estimated_time="5-10 minutes",
            tags=["starter", "mvp", "basic", "beginner"],
            category="foundation",
            version="1.0.0",
            author="FLUID Build Team",
            license="MIT",
        )

    def generate_structure(self, context: GenerationContext) -> Dict[str, Any]:
        """Generate basic project folder structure"""
        return {
            "sql/": {"queries/": {}, "transforms/": {}},
            "data/": {"raw/": {}, "processed/": {}},
            "docs/": {},
            "tests/": {"unit/": {}, "integration/": {}},
            "config/": {"environments/": {}},
            "scripts/": {},
            ".github/": {"workflows/": {}},
        }

    def generate_contract(self, context: GenerationContext) -> Dict[str, Any]:
        """Generate a v0.7.3-canonical contract via the shared builder.

        Per-template specifics (product type, build pattern, engine,
        default columns) live in :class:`TemplateSpec` below;
        :func:`build_contract` populates the canonical fluidVersion,
        layer↔productType pair, owner, exposes[].binding, builds[],
        and consumes[] structure so every template stays in lockstep
        with schema changes.
        """
        from ._v073_builder import TemplateSpec, build_contract

        spec = TemplateSpec(
            template_name="starter",
            product_type="SDP",
            pattern="embedded-logic",
            engine="sql",
            properties={"sql": "SELECT 1 AS id, CURRENT_TIMESTAMP AS created_at"},
            expose_id="starter_table",
            binding_format="csv",
            description_suffix="Minimal starter contract",
        )
        return build_contract(spec=spec, project_config=context.project_config)

    def validate_configuration(self, config: Dict[str, Any]) -> ValidationResult:
        """Validate starter template configuration"""
        errors = []

        # Basic validation
        if not config.get("name"):
            errors.append("Project name is required")

        if not config.get("description"):
            errors.append("Project description is required")

        # Validate project name format
        name = config.get("name", "")
        if name and not name.replace("-", "").replace("_", "").isalnum():
            errors.append(
                "Project name must contain only letters, numbers, hyphens, and underscores"
            )

        return len(errors) == 0, errors

    def get_recommended_providers(self) -> List[str]:
        """Get recommended providers for starter template"""
        # Local is best for getting started
        return ["local", "gcp", "snowflake"]

    def get_customization_prompts(self) -> List[Dict[str, Any]]:
        """Return additional customization prompts"""
        return [
            {
                "name": "include_examples",
                "type": "confirm",
                "message": "Include sample data and queries?",
                "default": True,
            },
            {
                "name": "setup_git",
                "type": "confirm",
                "message": "Initialize Git repository?",
                "default": True,
            },
            {
                "name": "create_readme",
                "type": "confirm",
                "message": "Generate comprehensive README?",
                "default": True,
            },
        ]

    def post_generation_hooks(self, context: GenerationContext) -> None:
        """Execute post-generation setup"""
        project_dir = context.target_dir
        user_selections = context.user_selections

        # Create sample files if requested
        if user_selections.get("include_examples", True):
            self._create_sample_files(project_dir)

        # Initialize Git repository if requested
        if user_selections.get("setup_git", True):
            self._initialize_git_repo(project_dir)

        # Create README if requested
        if user_selections.get("create_readme", True):
            self._create_readme(project_dir, context)

    def _create_sample_files(self, project_dir: Path) -> None:
        """Create sample data and query files"""

        # Sample SQL query
        sql_dir = project_dir / "sql" / "queries"
        sql_dir.mkdir(parents=True, exist_ok=True)

        sample_query = """-- Sample query for starter template
-- This demonstrates basic data transformation patterns

SELECT 
    id,
    UPPER(value) as value_upper,
    created_at,
    CURRENT_TIMESTAMP() as processed_at
FROM sample_data 
WHERE created_at >= CURRENT_DATE - INTERVAL 7 DAY
    AND value IS NOT NULL
ORDER BY created_at DESC
"""

        (sql_dir / "sample_query.sql").write_text(sample_query)

        # Sample data transformation
        transforms_dir = project_dir / "sql" / "transforms"
        transforms_dir.mkdir(parents=True, exist_ok=True)

        transform_sql = """-- Data transformation for starter template
-- Clean and standardize input data

WITH cleaned_data AS (
    SELECT 
        TRIM(id) as id,
        TRIM(UPPER(value)) as value,
        created_at
    FROM sample_data
    WHERE id IS NOT NULL 
        AND id != ''
        AND created_at IS NOT NULL
)

SELECT 
    id,
    value,
    created_at,
    CASE 
        WHEN LENGTH(value) > 100 THEN 'long'
        WHEN LENGTH(value) > 10 THEN 'medium' 
        ELSE 'short'
    END as value_category
FROM cleaned_data
"""

        (transforms_dir / "clean_data.sql").write_text(transform_sql)

        # Sample test
        test_dir = project_dir / "tests" / "unit"
        test_dir.mkdir(parents=True, exist_ok=True)

        test_sql = """-- Unit test for data transformation
-- Validates that transformation logic works correctly

-- Test 1: Ensure no null IDs pass through
SELECT 
    COUNT(*) as null_id_count
FROM (
    -- Include your transformation logic here
    SELECT id FROM sample_data WHERE id IS NULL
) 
-- Expect: 0 rows

-- Test 2: Ensure value categories are assigned correctly  
SELECT 
    value_category,
    COUNT(*) as count
FROM (
    -- Include your transformation logic here  
    SELECT 
        CASE 
            WHEN LENGTH(value) > 100 THEN 'long'
            WHEN LENGTH(value) > 10 THEN 'medium'
            ELSE 'short' 
        END as value_category
    FROM sample_data WHERE value IS NOT NULL
)
GROUP BY value_category
-- Expect: categories should match business rules
"""

        (test_dir / "test_transformations.sql").write_text(test_sql)

    def _initialize_git_repo(self, project_dir: Path) -> None:
        """Initialize Git repository with basic setup"""
        import subprocess

        try:
            # Initialize Git repo
            subprocess.run(["git", "init"], cwd=project_dir, check=True, capture_output=True)

            # Create .gitignore
            gitignore_content = """# Python
__pycache__/
*.py[cod]
*$py.class
*.so
.Python
env/
venv/
ENV/

# Data files
*.csv
*.parquet
*.json
*.xlsx
data/raw/*
data/processed/*
!data/raw/.gitkeep
!data/processed/.gitkeep

# Logs
*.log
logs/

# Environment variables
.env
.env.local

# IDE
.vscode/
.idea/
*.swp
*.swo

# OS
.DS_Store
Thumbs.db

# Temporary files
tmp/
temp/
"""

            (project_dir / ".gitignore").write_text(gitignore_content)

            # Create initial commit
            subprocess.run(["git", "add", "."], cwd=project_dir, check=True, capture_output=True)
            subprocess.run(
                ["git", "commit", "-m", "Initial commit: FLUID starter template"],
                cwd=project_dir,
                check=True,
                capture_output=True,
            )

        except subprocess.CalledProcessError:
            # Git setup failed, but don't fail the entire process
            pass

    def _create_readme(self, project_dir: Path, context: GenerationContext) -> None:
        """Create comprehensive README file"""
        project_config = context.project_config
        project_name = project_config.get("name", "Starter Data Product")
        description = project_config.get("description", "A FLUID data product")
        owner = project_config.get("owner", "data-team")
        provider = project_config.get("provider", "local")

        readme_content = f"""# {project_name}

{description}

This is a FLUID data product created using the starter template. It provides a
solid foundation for building data products with best practices and modern tooling.

## Overview

- **Domain**: {context.project_config.get('domain', 'analytics')}
- **Owner**: {owner}
- **Provider**: {provider}
- **FLUID Version**: {context.project_config.get('fluid_version', '0.5.7')}
- **Created**: {context.creation_time}

## Getting Started

### Prerequisites

- Python 3.10+
- SQL database (for local development)
- Git

### Quick Start

1. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

2. **Review the FLUID contract**:
   ```bash
   cat contract.fluid.yaml
   ```

3. **Run sample queries**:
   ```bash
   # Execute sample transformation
   python scripts/run_query.py sql/transforms/clean_data.sql
   ```

4. **Run tests**:
   ```bash
   python -m pytest tests/
   ```

## Project Structure

```
{project_name}/
├── contract.fluid.yaml     # FLUID contract definition
├── sql/                    # SQL queries and transformations
│   ├── queries/           # Data queries
│   └── transforms/        # Data transformations
├── data/                  # Data files (gitignored)
│   ├── raw/              # Raw input data
│   └── processed/        # Processed output data
├── tests/                # Test suite
│   ├── unit/             # Unit tests
│   └── integration/      # Integration tests
├── docs/                 # Documentation
├── config/               # Configuration files
├── scripts/              # Utility scripts
└── README.md            # This file
```

## Development

### Local Development

1. **Set up local environment**:
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\\Scripts\\activate
   pip install -r requirements.txt
   ```

2. **Configure environment variables**:
   ```bash
   cp .env.example .env
   # Edit .env with your configuration
   ```

3. **Run data pipeline**:
   ```bash
   python scripts/run_pipeline.py
   ```

### Testing

Run the test suite to validate your data transformations:

```bash
# Run all tests
python -m pytest tests/

# Run specific test file
python -m pytest tests/unit/test_transformations.py

# Run with coverage
python -m pytest tests/ --cov=src
```

### Data Quality

This template includes basic data quality checks:

- **Schema validation**: Ensures data matches expected schema
- **Completeness checks**: Validates required fields are present
- **Freshness monitoring**: Tracks data currency
- **Uniqueness constraints**: Prevents duplicate records

## Deployment

### Local Deployment

```bash
# Run locally
python scripts/deploy_local.py
```

### Cloud Deployment

Refer to the provider-specific deployment guides:

- [GCP Deployment](docs/deployment/gcp.md)
- [AWS Deployment](docs/deployment/aws.md)  
- [Snowflake Deployment](docs/deployment/snowflake.md)

## Monitoring

Monitor your data product using:

- **Data Quality Metrics**: Track completeness, accuracy, and timeliness
- **Performance Metrics**: Monitor query execution time and resource usage
- **Business Metrics**: Track downstream usage and business value

## Contributing

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'Add amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

## Support

- **Documentation**: [FLUID Documentation](https://agenticstiger.github.io/forge_docs/)
- **Community**: [FLUID Community](https://github.com/Agenticstiger/forge-cli/discussions)
- **Issues**: [GitHub Issues](https://github.com/your-org/{project_name}/issues)

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

---

*Generated by FLUID Forge v{context.forge_version} using the starter template*
"""

        (project_dir / "README.md").write_text(readme_content, encoding="utf-8")
