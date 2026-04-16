# Environment Configuration - Multi-Environment Deployment

**Time**: 7 min | **Difficulty**: Beginner | **Track**: Foundation

## Overview

Master environment-specific configuration with .env files, secrets management, and multi-environment deployment patterns. Build contracts that seamlessly work across dev, staging, and production.

## Key Concepts

- **Environment Variables**: Configure contracts with ${VAR_NAME}
- **Default Values**: Use ${VAR:-default} for fallbacks
- **Feature Flags**: Enable/disable features per environment
- **Secrets Management**: Keep credentials out of code

## Quick Start

### 1. Create Project

```bash
fluid init my-env-project --template environment-configuration
cd my-env-project
```

### 2. Configure Development Environment

```bash
# Copy example to dev config
cp .env.example .env.dev

# Edit values
nano .env.dev
```

### 3. Run with Environment

```bash
# Load dev environment
export $(cat .env.dev | xargs)

# Or use direnv
echo "dotenv .env.dev" > .envrc
direnv allow

# Run pipeline
fluid apply --local
```

## Environment Files

### .env.example (Template)
```bash
FLUID_ENV=development
DB_HOST=localhost
DB_NAME=fluid_dev
OUTPUT_SCHEMA=analytics_dev
OUTPUT_TABLE_PREFIX=dev_
ENABLE_DATA_QUALITY_CHECKS=true
```

### .env.dev (Development)
```bash
FLUID_ENV=development
DB_HOST=localhost
OUTPUT_TABLE_PREFIX=dev_
ENABLE_NOTIFICATIONS=false
MAX_WORKERS=2
```

### .env.prod (Production)
```bash
FLUID_ENV=production
DB_HOST=prod-db.example.com
OUTPUT_TABLE_PREFIX=""
ENABLE_NOTIFICATIONS=true
MAX_WORKERS=16
```

## Using Variables in Contracts

### Basic Variable Substitution

```yaml
outputs:
  - name: ${OUTPUT_TABLE_PREFIX}users_cleaned
    schema_name: ${OUTPUT_SCHEMA}
    transformation: |
      SELECT *, '${FLUID_ENV}' as environment
      FROM {{ ref('raw_users') }}
```

### With Default Values

```yaml
metadata:
  environment: ${FLUID_ENV:-development}  # Defaults to "development"

orchestration:
  enabled: ${ENABLE_ORCHESTRATION:-false}  # Defaults to false
  schedule: ${SCHEDULE_CRON:-"0 2 * * *"}  # Defaults to daily 2 AM
```

### Conditional Features

```yaml
validations:
  - name: data_quality_check
    enabled: ${ENABLE_DATA_QUALITY_CHECKS:-true}
    level: ${VALIDATION_LEVEL:-error}

monitoring:
  enabled: ${ENABLE_MONITORING:-true}
  alert_channels:
    email: ${ALERT_EMAIL:-}
    slack: ${SLACK_WEBHOOK:-}
```

## Multi-Environment Strategy

### Development
- Local DuckDB execution
- Fast iterations
- Reduced data quality checks
- No notifications

### Staging
- Cloud database (BigQuery/Snowflake)
- Full production data volume (sample)
- All validations enabled
- Notifications to dev team

### Production
- Production database
- Full data quality checks
- Monitoring and alerting
- SLA tracking

## Secrets Management

### Development (Local)
```bash
# .env.dev - committed to repo
DB_PASSWORD=dev_password_ok_to_commit
API_KEY=dev_key_12345
```

### Production (Secrets Manager)
```bash
# Use cloud secrets manager
DB_PASSWORD=$(aws secretsmanager get-secret-value --secret-id prod/db/password)
API_KEY=$(gcloud secrets versions access latest --secret="api-key")

# Or use Vault
export DB_PASSWORD=$(vault kv get -field=password secret/prod/db)
```

### In CI/CD
```yaml
# GitHub Actions
env:
  DB_HOST: ${{ secrets.PROD_DB_HOST }}
  DB_PASSWORD: ${{ secrets.PROD_DB_PASSWORD }}
  API_KEY: ${{ secrets.PROD_API_KEY }}
```

## Best Practices

### 1. Never Commit Secrets
```bash
# .gitignore
.env.prod
.env.staging
*.secret
```

### 2. Use Config Files for Complexity
```yaml
# config/prod.yaml
database:
  host: prod-db.example.com
  replica_count: 3
features:
  enable_caching: true
  cache_ttl: 3600
```

### 3. Validate Environment on Startup
```bash
# validate_env.sh
required_vars=("DB_HOST" "DB_NAME" "OUTPUT_SCHEMA")
for var in "${required_vars[@]}"; do
  if [ -z "${!var}" ]; then
    echo "Error: $var not set"
    exit 1
  fi
done
```

### 4. Document Required Variables
```markdown
# Required Environment Variables

## Development
- FLUID_ENV=development
- DB_HOST=localhost

## Production
- FLUID_ENV=production
- DB_HOST=<production-db>
- DB_PASSWORD=<from-secrets-manager>
- ALERT_EMAIL=<team-email>
```

## Testing Different Environments

```bash
# Test dev config
export $(cat .env.dev | xargs)
fluid validate
fluid apply --local

# Test staging config
export $(cat .env.staging | xargs)
fluid validate
fluid deploy --target staging

# Test prod config (dry-run)
export $(cat .env.prod | xargs)
fluid validate
fluid deploy --target production --dry-run
```

## Troubleshooting

### Issue: Variables not substituted

**Solution**: Ensure variables are exported
```bash
# Wrong
cat .env.dev

# Right
export $(cat .env.dev | xargs)
```

### Issue: Default values not working

**Solution**: Check syntax - use `:-` not just `:`
```yaml
# Wrong
environment: ${FLUID_ENV:development}

# Right
environment: ${FLUID_ENV:-development}
```

### Issue: Secrets exposed in logs

**Solution**: Mask sensitive variables
```yaml
# Use secret field instead of environment
connections:
  postgres:
    password: 
      secret: ${DB_PASSWORD}  # Won't appear in logs
```

## Success Criteria

- [ ] .env.example template created
- [ ] Development config works locally
- [ ] Production config validated (dry-run)
- [ ] Environment-specific table names working
- [ ] Feature flags toggle correctly
- [ ] Secrets not committed to git
- [ ] All required variables documented

## Next Steps

- **008-incremental-processing**: Add environment-aware incremental logic
- **011-first-dag**: Environment-specific DAG generation
- **012-pipeline-orchestration**: Multi-environment orchestration

**Pro Tip**: Start with .env files, graduate to secrets managers in production.
