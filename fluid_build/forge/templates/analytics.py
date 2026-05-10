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
Analytics Template for FLUID Forge

A comprehensive template for business intelligence and analytics data products.
Optimized for reporting, dashboards, and business metrics.

Features:
- dbt-based transformation framework
- Dimensional modeling patterns
- Data mart generation
- BI tool integration
- Comprehensive testing
- Performance optimization
- Data lineage tracking

This template follows analytics engineering best practices and provides
a scalable foundation for business intelligence workloads.
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


class AnalyticsTemplate(ProjectTemplate):
    """
    Analytics template for business intelligence data products

    This template creates a comprehensive analytics platform structure
    with dbt transformations, dimensional modeling, and BI integration.
    """

    def get_metadata(self) -> TemplateMetadata:
        """Return analytics template metadata"""
        return TemplateMetadata(
            name="Analytics Data Product",
            description="Business intelligence and reporting data products with SQL transforms",
            complexity=ComplexityLevel.INTERMEDIATE,
            provider_support=["local", "gcp", "snowflake", "bigquery", "redshift"],
            use_cases=[
                "Business dashboards and KPI tracking",
                "Customer analytics and segmentation",
                "Financial reporting and analysis",
                "Marketing performance metrics",
                "Operational analytics and insights",
                "Executive reporting and scorecards",
            ],
            technologies=["SQL", "dbt", "BigQuery", "Looker", "Tableau", "Python"],
            estimated_time="10-20 minutes",
            tags=["analytics", "reporting", "bi", "dashboard", "metrics"],
            category="analytics",
            version="1.0.0",
            author="FLUID Build Team",
            license="MIT",
        )

    def generate_structure(self, context: GenerationContext) -> Dict[str, Any]:
        """Generate analytics project folder structure"""
        return {
            "dbt/": {
                "models/": {
                    "staging/": {},
                    "intermediate/": {},
                    "marts/": {"core/": {}, "finance/": {}, "marketing/": {}, "operations/": {}},
                },
                "macros/": {},
                "tests/": {},
                "snapshots/": {},
                "seeds/": {},
                "analysis/": {},
            },
            "sql/": {
                "queries/": {"adhoc/": {}, "reports/": {}, "monitoring/": {}},
                "views/": {},
                "procedures/": {},
            },
            "data/": {"raw/": {}, "staged/": {}, "marts/": {}},
            "dashboards/": {"looker/": {}, "tableau/": {}, "powerbi/": {}},
            "docs/": {"data_dictionary/": {}, "lineage/": {}, "reports/": {}},
            "tests/": {"unit/": {}, "integration/": {}, "data_quality/": {}, "performance/": {}},
            "config/": {"dbt/": {}, "environments/": {}, "connections/": {}},
            "scripts/": {"deployment/": {}, "monitoring/": {}, "etl/": {}},
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
            template_name="analytics",
            product_type="ADP",
            pattern="hybrid-reference",
            engine="dbt",
            properties={"model": "analytics_model"},
            expose_id="analytics_facts",
            binding_format="csv",
            description_suffix="Star-schema analytics with metrics layer",
            columns=[
                {"name": "customer_id", "type": "string", "required": True},
                {"name": "metric_value", "type": "decimal", "required": True},
                {"name": "as_of", "type": "timestamp", "required": True},
            ],
        )
        return build_contract(spec=spec, project_config=context.project_config)

    def validate_configuration(self, config: Dict[str, Any]) -> ValidationResult:
        """Validate analytics template configuration"""
        errors = []
        warnings = []

        # Basic validation
        if not config.get("name"):
            errors.append("Project name is required")

        if not config.get("description"):
            errors.append("Project description is required")

        # Analytics-specific validation
        provider = config.get("provider")
        if provider not in ["gcp", "snowflake", "bigquery", "redshift", "local"]:
            warnings.append(f"Provider '{provider}' may not be optimal for analytics workloads")

        # Check if domain is analytics-related
        domain = config.get("domain", "").lower()
        analytics_domains = ["analytics", "bi", "business-intelligence", "reporting", "metrics"]
        if domain and not any(ad in domain for ad in analytics_domains):
            warnings.append("Consider using an analytics-related domain for better organization")

        return len(errors) == 0, errors + [f"Warning: {w}" for w in warnings]

    def get_recommended_providers(self) -> List[str]:
        """Get recommended providers for analytics template"""
        # BigQuery and Snowflake are optimal for analytics
        return ["bigquery", "snowflake", "gcp", "redshift"]

    def get_customization_prompts(self) -> List[Dict[str, Any]]:
        """Return analytics-specific customization prompts"""
        return [
            {
                "name": "include_dbt",
                "type": "confirm",
                "message": "Include dbt transformation framework?",
                "default": True,
            },
            {
                "name": "bi_tool",
                "type": "select",
                "message": "Primary BI tool integration?",
                "choices": ["looker", "tableau", "powerbi", "none"],
                "default": "looker",
            },
            {
                "name": "dimensional_modeling",
                "type": "confirm",
                "message": "Use dimensional modeling patterns?",
                "default": True,
            },
            {
                "name": "include_sample_dashboards",
                "type": "confirm",
                "message": "Include sample dashboards and reports?",
                "default": True,
            },
            {
                "name": "data_lineage",
                "type": "confirm",
                "message": "Enable data lineage tracking?",
                "default": True,
            },
        ]

    def post_generation_hooks(self, context: GenerationContext) -> None:
        """Execute analytics-specific post-generation setup"""
        project_dir = context.target_dir
        user_selections = context.user_selections

        # Create dbt project if requested
        if user_selections.get("include_dbt", True):
            self._create_dbt_project(project_dir, context)

        # Create sample dashboards if requested
        if user_selections.get("include_sample_dashboards", True):
            self._create_sample_dashboards(project_dir, context)

        # Set up data lineage if requested
        if user_selections.get("data_lineage", True):
            self._setup_data_lineage(project_dir)

        # Create dimensional models if requested
        if user_selections.get("dimensional_modeling", True):
            self._create_dimensional_models(project_dir)

    def _create_dbt_project(self, project_dir: Path, context: GenerationContext) -> None:
        """Create dbt project structure and configuration"""
        dbt_dir = project_dir / "dbt"

        # dbt_project.yml
        project_name = context.project_config.get("name", "analytics-product")
        dbt_project_config = f"""name: '{project_name.replace("-", "_")}'
version: '1.0.0'
config-version: 2

# This setting configures which "profile" dbt uses for this project.
profile: '{project_name.replace("-", "_")}'

# These configurations specify where dbt should look for different types of files.
model-paths: ["models"]
analysis-paths: ["analysis"]
test-paths: ["tests"]
seed-paths: ["seeds"]
macro-paths: ["macros"]
snapshot-paths: ["snapshots"]

target-path: "target"  # directory which will store compiled SQL files
clean-targets:         # directories to be removed by `dbt clean`
  - "target"
  - "dbt_packages"

# Model configurations
models:
  {project_name.replace("-", "_")}:
    # Applies to all files under models/.../
    staging:
      +materialized: view
      +docs:
        node_color: "lightblue"
    intermediate:
      +materialized: ephemeral
      +docs:
        node_color: "orange"
    marts:
      +materialized: table
      +docs:
        node_color: "green"
      core:
        +materialized: table
      finance:
        +materialized: table
      marketing:
        +materialized: table
      operations:
        +materialized: table

# Test configurations
tests:
  +store_failures: true
  +schema: dbt_test_failures

# Documentation
docs:
  generate: true

# Variables
vars:
  # dbt_utils timespan for tests
  'dbt_date:time_zone': 'UTC'
  # Analytics specific variables
  customer_acquisition_start_date: '2020-01-01'
  revenue_calculation_method: 'gross'
"""

        (dbt_dir / "dbt_project.yml").write_text(dbt_project_config, encoding="utf-8")

        # profiles.yml template
        profiles_dir = project_dir / "config" / "dbt"
        profiles_dir.mkdir(parents=True, exist_ok=True)

        provider = context.project_config.get("provider", "gcp")
        if provider in ["gcp", "bigquery"]:
            profiles_config = f"""# dbt profiles for {project_name}
{project_name.replace("-", "_")}:
  target: dev
  outputs:
    dev:
      type: bigquery
      method: service-account
      project: "{{{{ env_var('DBT_PROJECT_ID') }}}}"
      dataset: "{{{{ env_var('DBT_DATASET') }}}}"
      location: US
      keyfile: "{{{{ env_var('DBT_SERVICE_ACCOUNT_PATH') }}}}"
      threads: 4
      timeout_seconds: 300
    
    prod:
      type: bigquery
      method: service-account  
      project: "{{{{ env_var('DBT_PROD_PROJECT_ID') }}}}"
      dataset: "{{{{ env_var('DBT_PROD_DATASET') }}}}"
      location: US
      keyfile: "{{{{ env_var('DBT_PROD_SERVICE_ACCOUNT_PATH') }}}}"
      threads: 8
      timeout_seconds: 300
"""
        elif provider == "snowflake":
            profiles_config = f"""# dbt profiles for {project_name}
{project_name.replace("-", "_")}:
  target: dev
  outputs:
    dev:
      type: snowflake
      account: "{{{{ env_var('SNOWFLAKE_ACCOUNT') }}}}"
      user: "{{{{ env_var('SNOWFLAKE_USER') }}}}"
      password: "{{{{ env_var('SNOWFLAKE_PASSWORD') }}}}"
      role: "{{{{ env_var('SNOWFLAKE_ROLE') }}}}"
      database: "{{{{ env_var('SNOWFLAKE_DATABASE') }}}}"
      warehouse: "{{{{ env_var('SNOWFLAKE_WAREHOUSE') }}}}"
      schema: "{{{{ env_var('SNOWFLAKE_SCHEMA') }}}}"
      threads: 4
      
    prod:
      type: snowflake
      account: "{{{{ env_var('SNOWFLAKE_PROD_ACCOUNT') }}}}"
      user: "{{{{ env_var('SNOWFLAKE_PROD_USER') }}}}"
      password: "{{{{ env_var('SNOWFLAKE_PROD_PASSWORD') }}}}"
      role: "{{{{ env_var('SNOWFLAKE_PROD_ROLE') }}}}"
      database: "{{{{ env_var('SNOWFLAKE_PROD_DATABASE') }}}}"
      warehouse: "{{{{ env_var('SNOWFLAKE_PROD_WAREHOUSE') }}}}"
      schema: "{{{{ env_var('SNOWFLAKE_PROD_SCHEMA') }}}}"
      threads: 8
"""
        else:
            # Default/local configuration
            profiles_config = f"""# dbt profiles for {project_name}
{project_name.replace("-", "_")}:
  target: dev
  outputs:
    dev:
      type: postgres
      host: localhost
      user: "{{{{ env_var('DB_USER') }}}}"
      password: "{{{{ env_var('DB_PASSWORD') }}}}"
      port: 5432
      dbname: "{{{{ env_var('DB_NAME') }}}}"
      schema: public
      threads: 4
"""

        (profiles_dir / "profiles.yml").write_text(profiles_config, encoding="utf-8")

        # Create sample models
        self._create_sample_dbt_models(dbt_dir)

    def _create_sample_dbt_models(self, dbt_dir: Path) -> None:
        """Create sample dbt models for analytics"""
        models_dir = dbt_dir / "models"

        # Staging models
        staging_dir = models_dir / "staging"
        staging_dir.mkdir(parents=True, exist_ok=True)

        # Customer staging model
        staging_customers = """{{
  config(
    materialized='view'
  )
}}

WITH source AS (
  SELECT * FROM {{ source('raw', 'customers') }}
),

cleaned AS (
  SELECT
    customer_id,
    TRIM(UPPER(first_name)) AS first_name,
    TRIM(UPPER(last_name)) AS last_name,
    LOWER(email) AS email,
    phone,
    address,
    city,
    state,
    zip_code,
    country,
    created_at,
    updated_at
  FROM source
  WHERE customer_id IS NOT NULL
    AND email IS NOT NULL
)

SELECT * FROM cleaned
"""

        (staging_dir / "stg_customers.sql").write_text(staging_customers, encoding="utf-8")

        # Orders staging model
        staging_orders = """{{
  config(
    materialized='view'
  )
}}

WITH source AS (
  SELECT * FROM {{ source('raw', 'orders') }}
),

cleaned AS (
  SELECT
    order_id,
    customer_id,
    order_date,
    status,
    CAST(total_amount AS DECIMAL(10,2)) AS total_amount,
    currency,
    created_at,
    updated_at
  FROM source
  WHERE order_id IS NOT NULL
    AND customer_id IS NOT NULL
    AND total_amount >= 0
)

SELECT * FROM cleaned
"""

        (staging_dir / "stg_orders.sql").write_text(staging_orders, encoding="utf-8")

        # Intermediate models
        intermediate_dir = models_dir / "intermediate"
        intermediate_dir.mkdir(parents=True, exist_ok=True)

        # Customer order summary
        int_customer_orders = """{{
  config(
    materialized='ephemeral'
  )
}}

WITH customer_orders AS (
  SELECT
    customer_id,
    COUNT(*) AS total_orders,
    SUM(total_amount) AS total_spent,
    AVG(total_amount) AS avg_order_value,
    MIN(order_date) AS first_order_date,
    MAX(order_date) AS last_order_date
  FROM {{ ref('stg_orders') }}
  WHERE status IN ('completed', 'shipped', 'delivered')
  GROUP BY customer_id
)

SELECT * FROM customer_orders
"""

        (intermediate_dir / "int_customer_orders.sql").write_text(
            int_customer_orders, encoding="utf-8"
        )

        # Data marts
        marts_core_dir = models_dir / "marts" / "core"
        marts_core_dir.mkdir(parents=True, exist_ok=True)

        # Customer analytics mart
        customer_analytics = """{{
  config(
    materialized='table',
    indexes=[
      {'columns': ['customer_id'], 'unique': True},
      {'columns': ['customer_segment']},
      {'columns': ['acquisition_date']}
    ]
  )
}}

WITH customers AS (
  SELECT * FROM {{ ref('stg_customers') }}
),

orders AS (
  SELECT * FROM {{ ref('int_customer_orders') }}
),

customer_analytics AS (
  SELECT
    c.customer_id,
    c.first_name,
    c.last_name,
    c.email,
    c.city,
    c.state,
    c.country,
    c.created_at AS acquisition_date,
    
    -- Order metrics
    COALESCE(o.total_orders, 0) AS total_orders,
    COALESCE(o.total_spent, 0) AS lifetime_value,
    COALESCE(o.avg_order_value, 0) AS avg_order_value,
    o.first_order_date,
    o.last_order_date,
    
    -- Calculated fields
    CASE 
      WHEN COALESCE(o.total_spent, 0) >= 1000 THEN 'High Value'
      WHEN COALESCE(o.total_spent, 0) >= 500 THEN 'Medium Value'
      WHEN COALESCE(o.total_spent, 0) > 0 THEN 'Low Value'
      ELSE 'No Orders'
    END AS customer_segment,
    
    CASE 
      WHEN o.last_order_date >= CURRENT_DATE - INTERVAL 30 DAY THEN 'Active'
      WHEN o.last_order_date >= CURRENT_DATE - INTERVAL 90 DAY THEN 'At Risk'
      WHEN o.last_order_date IS NOT NULL THEN 'Inactive'
      ELSE 'Never Ordered'
    END AS activity_status,
    
    CURRENT_TIMESTAMP() AS calculated_at
    
  FROM customers c
  LEFT JOIN orders o ON c.customer_id = o.customer_id
)

SELECT * FROM customer_analytics
"""

        (marts_core_dir / "customer_analytics.sql").write_text(customer_analytics, encoding="utf-8")

        # Revenue metrics mart
        revenue_metrics = """{{
  config(
    materialized='table',
    indexes=[
      {'columns': ['date'], 'unique': True}
    ]
  )
}}

WITH daily_orders AS (
  SELECT
    DATE(order_date) AS date,
    COUNT(*) AS orders,
    COUNT(DISTINCT customer_id) AS customers,
    SUM(total_amount) AS revenue,
    AVG(total_amount) AS avg_order_value
  FROM {{ ref('stg_orders') }}
  WHERE status IN ('completed', 'shipped', 'delivered')
  GROUP BY DATE(order_date)
),

metrics_with_calculations AS (
  SELECT
    date,
    orders,
    customers,
    revenue,
    avg_order_value,
    
    -- Moving averages
    AVG(revenue) OVER (
      ORDER BY date 
      ROWS BETWEEN 6 PRECEDING AND CURRENT ROW
    ) AS revenue_7day_avg,
    
    AVG(orders) OVER (
      ORDER BY date 
      ROWS BETWEEN 6 PRECEDING AND CURRENT ROW  
    ) AS orders_7day_avg,
    
    -- Growth calculations
    LAG(revenue, 1) OVER (ORDER BY date) AS prev_day_revenue,
    LAG(revenue, 7) OVER (ORDER BY date) AS prev_week_revenue
    
  FROM daily_orders
)

SELECT
  date,
  orders,
  customers, 
  revenue,
  avg_order_value,
  revenue_7day_avg,
  orders_7day_avg,
  
  -- Growth metrics
  CASE 
    WHEN prev_day_revenue > 0 
    THEN ((revenue - prev_day_revenue) / prev_day_revenue) * 100
    ELSE NULL
  END AS day_over_day_growth,
  
  CASE 
    WHEN prev_week_revenue > 0 
    THEN ((revenue - prev_week_revenue) / prev_week_revenue) * 100
    ELSE NULL  
  END AS week_over_week_growth

FROM metrics_with_calculations
ORDER BY date
"""

        (marts_core_dir / "revenue_metrics.sql").write_text(revenue_metrics, encoding="utf-8")

    def _create_sample_dashboards(self, project_dir: Path, context: GenerationContext) -> None:
        """Create sample dashboard configurations"""
        dashboards_dir = project_dir / "dashboards"

        # Create Looker dashboard if selected
        bi_tool = context.user_selections.get("bi_tool", "looker")

        if bi_tool == "looker":
            looker_dir = dashboards_dir / "looker"
            looker_dir.mkdir(parents=True, exist_ok=True)

            # Customer analytics dashboard
            looker_dashboard = """- dashboard: customer_analytics
  title: Customer Analytics Dashboard
  layout: tile
  tile_size: 100

  filters:
  - name: date_range
    title: Date Range
    type: field_filter
    default_value: 30 days
    allow_multiple_values: true
    required: false
    ui_config:
      type: advanced
      display: popover

  elements:
  - title: Total Customers
    name: total_customers
    model: analytics
    explore: customer_analytics
    type: single_value
    fields: [customer_analytics.total_customers]
    limit: 500
    query_timezone: America/Los_Angeles
    custom_color_enabled: true
    show_single_value_title: true
    show_comparison: false
    comparison_type: value
    comparison_reverse_colors: false
    show_comparison_label: true
    enable_conditional_formatting: false
    conditional_formatting_include_totals: false
    conditional_formatting_include_nulls: false
    series_types: {}
    defaults_version: 1
    row: 0
    col: 0
    width: 6
    height: 4

  - title: Revenue by Segment
    name: revenue_by_segment
    model: analytics
    explore: customer_analytics
    type: looker_pie
    fields: [customer_analytics.customer_segment, customer_analytics.total_lifetime_value]
    sorts: [customer_analytics.total_lifetime_value desc]
    limit: 500
    query_timezone: America/Los_Angeles
    value_labels: legend
    label_type: labPer
    series_types: {}
    defaults_version: 1
    row: 0
    col: 6
    width: 6
    height: 4

  - title: Customer Acquisition Trend
    name: customer_acquisition_trend
    model: analytics
    explore: customer_analytics
    type: looker_line
    fields: [customer_analytics.acquisition_date, customer_analytics.count]
    fill_fields: [customer_analytics.acquisition_date]
    sorts: [customer_analytics.acquisition_date desc]
    limit: 500
    query_timezone: America/Los_Angeles
    x_axis_gridlines: false
    y_axis_gridlines: true
    show_view_names: false
    show_y_axis_labels: true
    show_y_axis_ticks: true
    y_axis_tick_density: default
    y_axis_tick_density_custom: 5
    show_x_axis_label: true
    show_x_axis_ticks: true
    y_axis_scale_mode: linear
    x_axis_reversed: false
    y_axis_reversed: false
    plot_size_by_field: false
    trellis: ''
    stacking: ''
    limit_displayed_rows: false
    legend_position: center
    point_style: none
    show_value_labels: false
    label_density: 25
    x_axis_scale: auto
    y_axis_combined: true
    show_null_points: true
    interpolation: linear
    series_types: {}
    defaults_version: 1
    row: 4
    col: 0
    width: 12
    height: 6
"""

            (looker_dir / "customer_analytics.dashboard.lookml").write_text(
                looker_dashboard, encoding="utf-8"
            )

    def _setup_data_lineage(self, project_dir: Path) -> None:
        """Set up data lineage tracking configuration"""
        docs_dir = project_dir / "docs" / "lineage"
        docs_dir.mkdir(parents=True, exist_ok=True)

        # Create lineage configuration
        lineage_config = """# Data Lineage Configuration

This directory contains data lineage documentation and configuration.

## Tools

- **dbt docs**: Automatic lineage from dbt transformations
- **DataHub**: Enterprise data discovery and lineage
- **OpenLineage**: Open standard for data lineage collection

## Setup

1. Generate dbt documentation:
   ```bash
   cd dbt/
   dbt docs generate
   dbt docs serve
   ```

2. View lineage graph at: http://localhost:8080

## Custom Lineage

For custom lineage tracking, create YAML files in this directory
following the OpenLineage specification.
"""

        (docs_dir / "README.md").write_text(lineage_config, encoding="utf-8")

    def _create_dimensional_models(self, project_dir: Path) -> None:
        """Create dimensional modeling examples"""
        models_dir = project_dir / "dbt" / "models"

        # Create dimensions directory
        dims_dir = models_dir / "marts" / "core" / "dimensions"
        dims_dir.mkdir(parents=True, exist_ok=True)

        # Customer dimension
        dim_customer = """{{
  config(
    materialized='table',
    indexes=[
      {'columns': ['customer_key'], 'unique': True},
      {'columns': ['customer_id'], 'unique': True}
    ]
  )
}}

WITH customers AS (
  SELECT * FROM {{ ref('stg_customers') }}
),

customer_dimension AS (
  SELECT
    {{ dbt_utils.generate_surrogate_key(['customer_id']) }} AS customer_key,
    customer_id,
    first_name,
    last_name,
    CONCAT(first_name, ' ', last_name) AS full_name,
    email,
    phone,
    address,
    city,
    state,
    zip_code,
    country,
    created_at AS customer_since,
    CURRENT_TIMESTAMP() AS updated_at
  FROM customers
)

SELECT * FROM customer_dimension
"""

        (dims_dir / "dim_customer.sql").write_text(dim_customer, encoding="utf-8")

        # Date dimension
        dim_date = """{{
  config(
    materialized='table',
    indexes=[
      {'columns': ['date_key'], 'unique': True},
      {'columns': ['date'], 'unique': True}
    ]
  )
}}

{{ dbt_utils.date_spine(
    datepart="day",
    start_date="cast('2020-01-01' as date)",
    end_date="cast('2030-12-31' as date)"
   )
}}

SELECT
  {{ dbt_utils.generate_surrogate_key(['date_day']) }} AS date_key,
  date_day AS date,
  EXTRACT(YEAR FROM date_day) AS year,
  EXTRACT(QUARTER FROM date_day) AS quarter,
  EXTRACT(MONTH FROM date_day) AS month,
  EXTRACT(WEEK FROM date_day) AS week,
  EXTRACT(DAY FROM date_day) AS day,
  EXTRACT(DAYOFWEEK FROM date_day) AS day_of_week,
  FORMAT_DATE('%A', date_day) AS day_name,
  FORMAT_DATE('%B', date_day) AS month_name,
  CASE 
    WHEN EXTRACT(DAYOFWEEK FROM date_day) IN (1, 7) THEN 'Weekend'
    ELSE 'Weekday'
  END AS day_type

FROM {{ ref('date_spine') }}
"""

        (dims_dir / "dim_date.sql").write_text(dim_date, encoding="utf-8")

        # Create facts directory
        facts_dir = models_dir / "marts" / "core" / "facts"
        facts_dir.mkdir(parents=True, exist_ok=True)

        # Order fact table
        fact_orders = """{{
  config(
    materialized='table',
    indexes=[
      {'columns': ['order_key'], 'unique': True},
      {'columns': ['customer_key']},
      {'columns': ['order_date_key']}
    ]
  )
}}

WITH orders AS (
  SELECT * FROM {{ ref('stg_orders') }}
),

customers AS (
  SELECT * FROM {{ ref('dim_customer') }}
),

dates AS (
  SELECT * FROM {{ ref('dim_date') }}
),

order_facts AS (
  SELECT
    {{ dbt_utils.generate_surrogate_key(['o.order_id']) }} AS order_key,
    o.order_id,
    c.customer_key,
    d.date_key AS order_date_key,
    o.order_date,
    o.status,
    o.total_amount,
    o.currency,
    o.created_at,
    o.updated_at
  FROM orders o
  LEFT JOIN customers c ON o.customer_id = c.customer_id
  LEFT JOIN dates d ON DATE(o.order_date) = d.date
)

SELECT * FROM order_facts
"""

        (facts_dir / "fact_orders.sql").write_text(fact_orders, encoding="utf-8")
