# FLUID Init Templates

This directory contains all available templates for the `fluid init` command.

## Quick Start

```bash
# List all available templates
fluid init --list

# Create project from template
fluid init my-project --template <template-name>

# Get template details
fluid init --info <template-name>
```

## Available Templates

### Foundation Track (Beginner)
- **hello-world** - Simplest FLUID contract (2 min)
- **csv-basics** - CSV data ingestion and transformation (5 min)
- **multi-source** - Join multiple data sources (8 min)
- **external-sql** - Organize SQL in separate files (10 min)
- **data-quality** - Add validation rules (12 min)

### Intermediate Track
- **first-dag** - Auto-generate Airflow DAG from contract (15 min) ⭐
- **local-pipeline** - Complete orchestration setup (20 min)
- **customer-360** - Production-ready customer analytics (10 min)

### Advanced Track - Cloud Providers
- **bigquery-deploy** - Deploy to Google BigQuery (20 min)
- **gcp-storage** - GCP Cloud Storage integration (18 min)
- **gcp-full-stack** - Complete GCP pipeline (30 min)
- **redshift-deploy** - Deploy to AWS Redshift (20 min)
- **aws-s3-lake** - AWS S3 Data Lake (22 min)
- **aws-full-stack** - Complete AWS pipeline (30 min)
- **snowflake-deploy** - Deploy to Snowflake (20 min)
- **snowflake-external** - Snowflake external tables (22 min)
- **snowflake-advanced** - Snowflake CDC & tasks (25 min)
- **multi-cloud** - Multi-cloud deployment (30 min)

### Enterprise Features
- **cicd** - CI/CD integration (20 min)
- **streaming** - Real-time Kafka streaming (30 min)
- **data-mesh** - Data mesh architecture (30 min)
- **ml-features** - Machine learning features (25 min)
- **security** - Enterprise security patterns (30 min)

## Template Structure

Each template follows this standard structure:

```
template-name/
├── .template-meta.yaml       # Template metadata
├── README.md                  # Template-specific guide
├── contract.fluid.yaml        # Working FLUID contract
├── .env.example              # Environment variables (optional)
├── data/                     # Sample data files (optional)
│   └── *.csv
├── sql/                      # External SQL files (optional)
│   └── *.sql
├── dags/                     # Generated DAGs (optional)
│   └── .gitkeep
└── tests/                    # Template tests (optional)
    └── test_*.py
```

## Template Metadata Format

Each template includes a `.template-meta.yaml` file:

```yaml
name: "Template Display Name"
id: "template-id"
version: "0.7.2"
difficulty: "beginner|intermediate|advanced"
time_to_complete: "X minutes"
learning_path_order: 1
track: "foundation|intermediate|advanced|specialized"
concepts:
  - "Concept 1"
  - "Concept 2"
prerequisites:
  - "FLUID CLI installed"
quickstart_tutorial: "NNN-tutorial-name.md"
features:
  local_execution: true
  dag_generation: false
  cloud_deployment: false
  requires_docker: false
tags:
  - tag1
  - tag2
```

## Creating Your Own Templates

1. Create a new directory under `templates/`
2. Add required files: `.template-meta.yaml`, `README.md`, `contract.fluid.yaml`
3. Add sample data in `data/` if needed
4. Test with: `fluid init test-project --template your-template-name`
5. Submit PR to add to official templates

## Testing Templates

```bash
# Test template creation
cd fluid_build/templates
pytest tests/test_templates.py

# Validate all contracts
for dir in */; do
  fluid validate "${dir}contract.fluid.yaml"
done
```

## Version History

- **v0.7.1** (2026-01-19) - Initial template system with 50 templates
  - Foundation track (001-010)
  - Intermediate track (011-020)
  - Advanced track with cloud providers (021-040)
  - Specialized scenarios (041-050)

## Support

- Documentation: https://docs.fluiddata.io
- GitHub: https://github.com/yourusername/fluid-mono
- Issues: https://github.com/yourusername/fluid-mono/issues
