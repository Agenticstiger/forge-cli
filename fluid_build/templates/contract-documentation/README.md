# Contract Documentation - Auto-Generated Docs

**Time**: 5 min | **Difficulty**: Beginner | **Track**: Foundation

## Overview

Auto-generate beautiful documentation from FLUID contracts. Create data catalogs, lineage diagrams, and schema docs automatically.

## Quick Start

```bash
fluid init my-docs --template contract-documentation
cd my-docs

# Generate documentation
fluid docs generate

# Start docs server
fluid docs serve  # View at http://localhost:8000
```

## What Gets Generated

### 1. Schema Documentation
- Table/column descriptions
- Data types and constraints
- Primary/foreign keys
- Sample data

### 2. Lineage Diagrams
```
raw_products → product_catalog → [downstream consumers]
```

### 3. Data Catalog
- Searchable metadata
- Business glossary
- Owner information
- SLA definitions

## Documentation in Contracts

### Inline Descriptions
```yaml
metadata:
  description: |
    ## Purpose
    Product analytics pipeline
    
    ## Owners
    Team: Data Engineering
```

### Schema Docs
```yaml
schema:
  - name: product_id
    type: INTEGER
    description: Unique product identifier (primary key)
```

### Usage Examples
```yaml
outputs:
  - name: product_catalog
    description: |
      ### Usage
      Used by: Marketing dashboard
      Query: SELECT * FROM product_catalog WHERE price_tier = 'Premium'
```

## Generated Output

### Markdown
```markdown
# Product Catalog

**Type**: Table  
**Update Frequency**: Daily

## Schema
| Column | Type | Description |
|--------|------|-------------|
| product_id | INTEGER | Primary key |
| product_name | VARCHAR | Display name |
```

### HTML
Interactive docs with search, filtering, and lineage visualization.

## Commands

```bash
# Generate docs
fluid docs generate

# Specific format
fluid docs generate --format markdown
fluid docs generate --format html

# Serve locally
fluid docs serve --port 8000

# Deploy to S3/GCS
fluid docs deploy --target s3://docs-bucket/
```

## Success Criteria

- [ ] Contract has descriptions for all outputs
- [ ] Schema columns documented
- [ ] Docs generated successfully
- [ ] Lineage diagram shows dependencies
- [ ] Docs viewable in browser

## Next Steps

- **011-first-dag**: Document orchestration
- **013-customer-360**: Production docs examples

**Pro Tip**: Good docs = fewer support questions. Document as you build!
