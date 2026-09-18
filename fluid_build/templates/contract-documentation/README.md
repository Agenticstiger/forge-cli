# Contract Documentation - Auto-Generated Docs

**Time**: 5 min | **Difficulty**: Beginner | **Track**: Foundation

## Overview

Auto-generate beautiful documentation from FLUID contracts. Create data catalogs, lineage diagrams, and schema docs automatically.

## Quick Start

```bash
fluid init my-docs --template contract-documentation
cd my-docs

# Generate documentation
fluid docs --src . --out docs/

# Start docs server
fluid docs --src . --out docs/   # then open docs/index.html
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
fluid docs --src . --out docs/

# `fluid docs` emits a static HTML catalogue; there is no format switch.
# `--src` is required -- without it the catalogue comes out empty.

# Serve locally: generate, then open the file
fluid docs --src . --out docs/   # then open docs/index.html

# Publish: generate, then upload docs/ with your own tooling
fluid docs --src . --out docs/
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
