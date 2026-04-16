# OPDS Provider - Enhanced Implementation

The **OPDS (Open Data Product Specification)** provider converts FLUID contracts into industry-standard OPDS JSON format, enabling seamless integration with data catalogs and governance platforms.

## Features

### 🌟 **Core Capabilities**
- **Full OPDS v1.0 compliance** with rich metadata extraction
- **Batch processing** support for multiple contracts
- **Comprehensive validation** with detailed error reporting
- **Configurable output** with environment-based settings
- **Standards-compliant** JSON generation

### 🔧 **Enhanced Functionality**
- **Governance extraction**: Access policies, compliance metadata, data classification
- **SLA information**: Freshness schedules, availability targets, quality metrics
- **Lineage tracking**: Complete dependency graph and transformation details
- **Build metadata**: Transformation patterns, execution configuration
- **Quality metrics**: Data quality rules and monitoring configuration
- **FLUID extensions**: Preserves custom metadata under `x-fluid` namespace

### ⚙️ **Configuration Options**

Environment variables for customization:

```bash
# Include build information in export (default: true)
export OPDS_INCLUDE_BUILD_INFO=true

# Include execution details (default: false)
export OPDS_INCLUDE_EXECUTION_DETAILS=true

# Target platform for optimization (default: generic)
export OPDS_TARGET_PLATFORM=collibra

# Enable output validation (default: true)
export OPDS_VALIDATE_OUTPUT=true
```

## Usage

### Basic Export

```bash
# Single contract export
python -m fluid_build.cli \
  --provider opds \
  apply examples/customer360/contract.fluid.yaml \
  --out runtime/exports/customer360.opds.json
```

### Advanced Usage

```bash
# Batch export with enhanced metadata
OPDS_INCLUDE_BUILD_INFO=true \
OPDS_INCLUDE_EXECUTION_DETAILS=true \
python -m fluid_build.cli \
  --provider opds \
  apply examples/**/contract.fluid.yaml \
  --out runtime/exports/catalog.opds.json

# Export to stdout for pipeline processing
python -m fluid_build.cli \
  --provider opds \
  apply contract.fluid.yaml \
  --out - | jq '.artifacts'
```

## Output Format

### Enhanced OPDS Structure

```json
{
  "opds_version": "1.0",
  "generator": "fluid-forge-opds-provider",
  "generated_at": "2025-10-14T12:30:45Z",
  "target_platform": "generic",
  "count": 1,
  "status": "success",
  "artifacts": {
    "dataProductId": "gold.customer360_v1",
    "dataProductName": "Customer 360 (Demo)",
    "dataProductDescription": "Unified Customer 360 table on BigQuery",
    "dataProductOwner": {
      "name": "Customer Platform",
      "email": "cust-platform@example.com",
      "organization": null
    },
    "dataProductType": "DataProduct",
    "domain": "Customer",
    "tags": ["customer", "360", "demo"],
    "layer": "Gold",
    "status": "Published",
    "version": "1.0.0",
    "outputPorts": [
      {
        "id": "gold.customer360_tbl",
        "name": "gold.customer360_tbl",
        "description": "",
        "type": "table",
        "format": "bigquery_table",
        "location": {
          "format": "bigquery_table",
          "properties": {
            "project": "YOUR_PROJECT",
            "dataset": "gold_demo",
            "table": "customer360_v1"
          }
        },
        "schema": {
          "fields": [
            {
              "name": "customer_id",
              "type": "STRING",
              "nullable": false,
              "description": ""
            },
            {
              "name": "segment",
              "type": "STRING",
              "nullable": true,
              "description": ""
            }
          ],
          "format": "json-schema"
        }
      }
    ],
    "inputPorts": [
      {
        "id": "crm_core",
        "name": "crm_core",
        "description": "",
        "reference": "silver.crm.core_v1",
        "kind": "data",
        "required": true
      },
      {
        "id": "billing_accounts",
        "name": "billing_accounts", 
        "description": "",
        "reference": "silver.billing.accounts_v1",
        "kind": "data",
        "required": true
      }
    ],
    "governance": {
      "accessPolicy": {
        "grants": [
          {
            "principal": "group:analysts@example.com",
            "permissions": ["readData", "readMetadata"],
            "conditions": {}
          }
        ]
      }
    },
    "sla": {
      "freshness": {
        "schedule": "15 2 * * *",
        "maxAgeHours": 24
      },
      "availability": {
        "target": "99.9%",
        "measurementWindow": "monthly"
      }
    },
    "lineage": {
      "upstream": ["silver.crm.core_v1", "silver.billing.accounts_v1"],
      "transformation": {
        "pattern": "hybrid-reference",
        "engine": "dbt-bigquery",
        "language": "sql"
      }
    },
    "buildInfo": {
      "transformation": {
        "pattern": "hybrid-reference",
        "engine": "dbt-bigquery",
        "properties": {
          "model": "./examples/customer360/dbt_project",
          "vars": {"materialization": "table"}
        }
      }
    },
    "executionInfo": {
      "trigger": {"type": "schedule", "cron": "15 2 * * *"},
      "runtime": {
        "platform": "gcp",
        "resources": {"cpu": "2", "memory": "8Gi"}
      },
      "retries": {
        "count": 2,
        "delaySeconds": 60,
        "backoff": "exponential"
      }
    },
    "x-fluid": {
      "fluidVersion": "0.4.0",
      "originalId": "gold.customer360_v1",
      "build": {
        "transformation": {
          "pattern": "hybrid-reference",
          "engine": "dbt-bigquery",
          "properties": {
            "model": "./examples/customer360/dbt_project",
            "vars": {"materialization": "table"}
          }
        },
        "execution": {
          "trigger": {"type": "schedule", "cron": "15 2 * * *"},
          "runtime": {
            "platform": "gcp", 
            "resources": {"cpu": "2", "memory": "8Gi"}
          },
          "retries": {
            "count": 2,
            "delaySeconds": 60,
            "backoff": "exponential"
          }
        }
      }
    },
    "updatedAt": "2025-10-14T12:30:45Z"
  },
  "export_config": {
    "include_build_info": true,
    "include_execution_details": true,
    "validation_enabled": true
  }
}
```

## Implementation Details

### Architecture

- **Provider-based**: Implements `BaseProvider` interface for consistency
- **Render-focused**: Primary method is `render()` for export functionality  
- **Validation-enabled**: Built-in OPDS compliance validation
- **Error-resilient**: Graceful handling of malformed contracts
- **Extensible**: Environment-based configuration and platform targeting

### Key Methods

- `render(src, *, out=None, fmt="opds")`: Primary export method
- `_contract_to_opds(contract)`: Core FLUID→OPDS conversion logic
- `_validate_opds_artifact(artifact)`: OPDS compliance validation
- `_extract_*_info()`: Specialized metadata extraction methods

### Error Handling

- **Graceful degradation**: Continues processing when individual contracts fail
- **Detailed reporting**: Comprehensive error information with context
- **Validation feedback**: Clear validation error messages and warnings
- **Logging integration**: Structured logging for debugging and monitoring

## Platform Integration

### Data Catalog Registration

```bash
# Apache Atlas
fluid --provider opds apply contract.fluid.yaml --out - | \
  curl -X POST -H "Content-Type: application/json" \
    -d @- "https://atlas.company.com/api/atlas/v2/entity"

# Collibra
OPDS_TARGET_PLATFORM=collibra \
fluid --provider opds apply contract.fluid.yaml --out collibra.json

# DataHub  
fluid --provider opds apply contract.fluid.yaml --out datahub.json
datahub ingest --source opds --config '{"path": "datahub.json"}'
```

### CI/CD Integration

```yaml
# .gitlab-ci.yml
export-catalog:
  stage: publish
  script:
    - fluid --provider opds apply products/*/contract.fluid.yaml --out catalog.opds.json
    - curl -X POST -H "Content-Type: application/json" \
        -d @catalog.opds.json \
        https://datacatalog.company.com/api/v1/products
  artifacts:
    paths:
      - catalog.opds.json
```

## Validation and Quality

### OPDS Compliance

The provider validates exported artifacts against OPDS requirements:

- **Required fields**: dataProductId, dataProductName, dataProductOwner
- **Schema validation**: Output port and input port structure
- **Data integrity**: Owner information completeness
- **Format compliance**: JSON schema adherence

### Quality Metrics

- **Coverage analysis**: Metadata extraction completeness
- **Validation reporting**: Error and warning categorization  
- **Processing statistics**: Success/failure rates and performance metrics
- **Configuration tracking**: Export settings and environment details
