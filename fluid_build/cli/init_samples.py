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
CLI command for initializing sample data.

Provides 'fluid init-samples' command to generate realistic test datasets.
"""

import logging
from pathlib import Path
from typing import Optional

import click

from fluid_build.providers.local.samples import SampleDataManager


@click.command("init-samples")
@click.option(
    "--output-dir",
    "-o",
    type=click.Path(),
    help="Output directory for sample data (default: ~/.fluid/samples)",
)
@click.option(
    "--format",
    "-f",
    type=click.Choice(["csv", "json", "parquet"], case_sensitive=False),
    default="csv",
    help="Output format for sample data",
)
@click.option(
    "--datasets",
    "-d",
    multiple=True,
    type=click.Choice(
        ["customers", "orders", "events", "metrics", "sensor_readings", "all"], case_sensitive=False
    ),
    default=["all"],
    help="Datasets to generate (default: all)",
)
@click.option("--quiet", "-q", is_flag=True, help="Suppress output")
def init_samples(output_dir: Optional[str], format: str, datasets: tuple, quiet: bool):
    """
    Generate sample datasets for testing and development.

    Creates realistic sample data including:
    - customers: 100 customer records with demographics
    - orders: 1000 order transactions
    - events: 10000 clickstream events
    - metrics: 24 hours of time series metrics
    - sensor_readings: 7 days of sensor data

    Examples:

        # Generate all datasets in CSV format
        fluid init-samples

        # Generate specific datasets in JSON format
        fluid init-samples -d customers -d orders -f json

        # Generate to custom directory
        fluid init-samples -o ./sample_data
    """
    logger = logging.getLogger(__name__)

    if not quiet:
        click.echo("🎲 Generating sample datasets...")

    # Initialize sample data manager
    manager = SampleDataManager(output_dir=output_dir)

    # Determine which datasets to generate
    generate_all = "all" in datasets or len(datasets) == 0

    try:
        if generate_all:
            # Generate all datasets
            files = manager.generate_all(format=format)

            if not quiet:
                click.echo(f"\n✅ Generated {len(files)} datasets:")
                for dataset_name, filepath in files.items():
                    size_kb = Path(filepath).stat().st_size / 1024
                    click.echo(f"   - {dataset_name}: {filepath} ({size_kb:.2f} KB)")

        else:
            # Generate specific datasets
            files = {}

            if "customers" in datasets:
                customers = manager.customer_gen.generate(count=100)
                files["customers"] = manager._write_dataset(customers, "customers", format)

            if "orders" in datasets:
                # Need customer IDs for orders
                if "customers" not in files:
                    customer_ids = [f"CUST{i:06d}" for i in range(1, 101)]
                else:
                    import json

                    with open(files["customers"]) as f:
                        if format == "json":
                            customers_data = json.load(f)
                            customer_ids = [c["customer_id"] for c in customers_data]
                        else:
                            customer_ids = [f"CUST{i:06d}" for i in range(1, 101)]

                orders = manager.order_gen.generate(count=1000, customer_ids=customer_ids)
                files["orders"] = manager._write_dataset(orders, "orders", format)

            if "events" in datasets:
                events = manager.event_gen.generate(count=10000)
                files["events"] = manager._write_dataset(events, "events", format)

            if "metrics" in datasets:
                metrics = manager.timeseries_gen.generate_metrics(
                    metric_name="cpu_usage", hours=24, interval_seconds=300
                )
                files["metrics"] = manager._write_dataset(metrics, "metrics", format)

            if "sensor_readings" in datasets:
                sensors = manager.timeseries_gen.generate_sensor_data(
                    sensor_id="SENSOR001", days=7, readings_per_day=96
                )
                files["sensor_readings"] = manager._write_dataset(
                    sensors, "sensor_readings", format
                )

            if not quiet:
                click.echo(f"\n✅ Generated {len(files)} dataset(s):")
                for dataset_name, filepath in files.items():
                    size_kb = Path(filepath).stat().st_size / 1024
                    click.echo(f"   - {dataset_name}: {filepath} ({size_kb:.2f} KB)")

        # Print summary
        if not quiet:
            summary = manager.get_summary()
            total_size = sum(d["size_kb"] for d in summary["datasets"])
            click.echo("\n📊 Summary:")
            click.echo(f"   Output directory: {summary['output_directory']}")
            click.echo(f"   Total datasets: {len(summary['datasets'])}")
            click.echo(f"   Total size: {total_size:.2f} KB")
            click.echo("\n💡 Use these datasets with: fluid apply --provider local")

    except Exception as e:
        logger.error(f"Failed to generate samples: {e}")
        if not quiet:
            click.echo(f"❌ Error: {e}", err=True)
        raise click.ClickException(str(e))


# For registration with CLI
def register(cli_group):
    """Register command with CLI group."""
    cli_group.add_command(init_samples)
