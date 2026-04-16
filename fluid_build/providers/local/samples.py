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
Sample data generators for Local Provider.

Provides realistic test data generators for:
- Customer data (demographics, segments)
- Order/transaction data (e-commerce, time series)
- Event/log data (clickstream, system logs)
- Time series data (metrics, sensors)
"""

import json
import random
import string
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional


class CustomerDataGenerator:
    """Generate realistic customer data."""

    FIRST_NAMES = [
        "Emma",
        "Liam",
        "Olivia",
        "Noah",
        "Ava",
        "Ethan",
        "Sophia",
        "Mason",
        "Isabella",
        "William",
        "Mia",
        "James",
        "Charlotte",
        "Benjamin",
        "Amelia",
        "Lucas",
        "Harper",
        "Henry",
        "Evelyn",
        "Alexander",
        "Abigail",
        "Michael",
    ]

    LAST_NAMES = [
        "Smith",
        "Johnson",
        "Williams",
        "Brown",
        "Jones",
        "Garcia",
        "Miller",
        "Davis",
        "Rodriguez",
        "Martinez",
        "Hernandez",
        "Lopez",
        "Gonzalez",
        "Wilson",
        "Anderson",
        "Thomas",
        "Taylor",
        "Moore",
        "Jackson",
        "Martin",
    ]

    CITIES = [
        "New York",
        "Los Angeles",
        "Chicago",
        "Houston",
        "Phoenix",
        "Philadelphia",
        "San Antonio",
        "San Diego",
        "Dallas",
        "San Jose",
        "Austin",
        "Jacksonville",
        "Fort Worth",
        "Columbus",
        "Charlotte",
        "San Francisco",
        "Indianapolis",
        "Seattle",
        "Denver",
        "Boston",
        "Nashville",
        "Detroit",
        "Portland",
    ]

    STATES = [
        "NY",
        "CA",
        "IL",
        "TX",
        "AZ",
        "PA",
        "FL",
        "OH",
        "NC",
        "WA",
        "CO",
        "MA",
        "TN",
        "MI",
        "OR",
        "GA",
        "VA",
        "NV",
        "MN",
        "WI",
    ]

    SEGMENTS = ["Enterprise", "SMB", "Startup", "Individual"]
    INDUSTRIES = ["Technology", "Healthcare", "Finance", "Retail", "Manufacturing"]

    def generate(self, count: int = 100) -> List[Dict[str, Any]]:
        """
        Generate customer records.

        Args:
            count: Number of customers to generate

        Returns:
            List of customer dictionaries
        """
        customers = []

        for i in range(1, count + 1):
            first_name = random.choice(self.FIRST_NAMES)
            last_name = random.choice(self.LAST_NAMES)
            email = f"{first_name.lower()}.{last_name.lower()}@example.com"

            customer = {
                "customer_id": f"CUST{i:06d}",
                "first_name": first_name,
                "last_name": last_name,
                "email": email,
                "phone": self._generate_phone(),
                "company": f"{last_name} {random.choice(self.INDUSTRIES)}",
                "segment": random.choice(self.SEGMENTS),
                "industry": random.choice(self.INDUSTRIES),
                "city": random.choice(self.CITIES),
                "state": random.choice(self.STATES),
                "postal_code": f"{random.randint(10000, 99999)}",
                "country": "USA",
                "created_at": self._random_date(days_ago=365 * 3),
                "last_active_at": self._random_date(days_ago=30),
                "lifetime_value": round(random.uniform(100, 100000), 2),
                "total_orders": random.randint(1, 50),
                "is_active": random.choice([True, True, True, False]),
            }

            customers.append(customer)

        return customers

    def _generate_phone(self) -> str:
        """Generate realistic US phone number."""
        area_code = random.randint(200, 999)
        exchange = random.randint(200, 999)
        number = random.randint(1000, 9999)
        return f"+1-{area_code}-{exchange}-{number}"

    def _random_date(self, days_ago: int) -> str:
        """Generate random date within past N days."""
        days = random.randint(0, days_ago)
        date = datetime.now() - timedelta(days=days)
        return date.strftime("%Y-%m-%d %H:%M:%S")


class OrderDataGenerator:
    """Generate realistic order/transaction data."""

    PRODUCTS = [
        ("Laptop", 999.99, "Electronics"),
        ("Mouse", 29.99, "Electronics"),
        ("Keyboard", 79.99, "Electronics"),
        ("Monitor", 299.99, "Electronics"),
        ("Desk Chair", 249.99, "Furniture"),
        ("Standing Desk", 499.99, "Furniture"),
        ("Office Lamp", 39.99, "Furniture"),
        ("Notebook", 4.99, "Supplies"),
        ("Pens (Pack)", 9.99, "Supplies"),
        ("Paper (Ream)", 19.99, "Supplies"),
    ]

    STATUSES = ["pending", "processing", "shipped", "delivered", "cancelled"]
    PAYMENT_METHODS = ["credit_card", "debit_card", "paypal", "bank_transfer"]

    def generate(
        self, count: int = 1000, customer_ids: Optional[List[str]] = None
    ) -> List[Dict[str, Any]]:
        """
        Generate order records.

        Args:
            count: Number of orders to generate
            customer_ids: List of customer IDs to assign orders to

        Returns:
            List of order dictionaries
        """
        if not customer_ids:
            customer_ids = [f"CUST{i:06d}" for i in range(1, 101)]

        orders = []

        for i in range(1, count + 1):
            product, price, category = random.choice(self.PRODUCTS)
            quantity = random.randint(1, 10)
            subtotal = price * quantity
            tax = subtotal * 0.08
            shipping = 0 if subtotal > 50 else 9.99
            total = subtotal + tax + shipping

            order = {
                "order_id": f"ORD{i:08d}",
                "customer_id": random.choice(customer_ids),
                "order_date": self._random_datetime(days_ago=180),
                "product_name": product,
                "product_category": category,
                "quantity": quantity,
                "unit_price": price,
                "subtotal": round(subtotal, 2),
                "tax": round(tax, 2),
                "shipping": round(shipping, 2),
                "total": round(total, 2),
                "status": self._weighted_status(),
                "payment_method": random.choice(self.PAYMENT_METHODS),
                "shipping_address": self._generate_address(),
                "tracking_number": self._generate_tracking(),
            }

            orders.append(order)

        return orders

    def _weighted_status(self) -> str:
        """Generate status with realistic distribution."""
        weights = [0.05, 0.10, 0.20, 0.60, 0.05]  # Most orders delivered
        return random.choices(self.STATUSES, weights=weights)[0]

    def _random_datetime(self, days_ago: int) -> str:
        """Generate random datetime within past N days."""
        days = random.randint(0, days_ago)
        hours = random.randint(0, 23)
        minutes = random.randint(0, 59)
        dt = datetime.now() - timedelta(days=days, hours=hours, minutes=minutes)
        return dt.strftime("%Y-%m-%d %H:%M:%S")

    def _generate_address(self) -> str:
        """Generate random shipping address."""
        street_num = random.randint(1, 9999)
        street_name = random.choice(["Main", "Oak", "Maple", "Cedar", "Pine"])
        street_type = random.choice(["St", "Ave", "Blvd", "Dr"])
        return f"{street_num} {street_name} {street_type}"

    def _generate_tracking(self) -> str:
        """Generate tracking number."""
        return f"TRK{''.join(random.choices(string.ascii_uppercase + string.digits, k=12))}"


class EventDataGenerator:
    """Generate realistic event/log data."""

    EVENT_TYPES = [
        "page_view",
        "button_click",
        "form_submit",
        "video_play",
        "search",
        "add_to_cart",
        "checkout",
        "login",
        "logout",
        "error",
    ]

    PAGES = [
        "/",
        "/products",
        "/products/laptop",
        "/products/mouse",
        "/cart",
        "/checkout",
        "/account",
        "/support",
        "/about",
    ]

    BROWSERS = ["Chrome", "Firefox", "Safari", "Edge"]
    DEVICES = ["desktop", "mobile", "tablet"]

    def generate(
        self, count: int = 10000, user_ids: Optional[List[str]] = None
    ) -> List[Dict[str, Any]]:
        """
        Generate event records.

        Args:
            count: Number of events to generate
            user_ids: List of user IDs to assign events to

        Returns:
            List of event dictionaries
        """
        if not user_ids:
            user_ids = [f"USER{i:06d}" for i in range(1, 1001)]

        events = []

        for i in range(1, count + 1):
            event_type = random.choice(self.EVENT_TYPES)

            event = {
                "event_id": f"EVT{i:010d}",
                "user_id": random.choice(user_ids),
                "session_id": f"SESS{random.randint(1, count // 10):08d}",
                "timestamp": self._random_timestamp(hours_ago=72),
                "event_type": event_type,
                "page_url": random.choice(self.PAGES),
                "referrer": self._generate_referrer(),
                "browser": random.choice(self.BROWSERS),
                "device": random.choice(self.DEVICES),
                "ip_address": self._generate_ip(),
                "country": "US",
                "properties": self._generate_properties(event_type),
            }

            events.append(event)

        return events

    def _random_timestamp(self, hours_ago: int) -> str:
        """Generate random timestamp within past N hours."""
        seconds = random.randint(0, hours_ago * 3600)
        dt = datetime.now() - timedelta(seconds=seconds)
        return dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]  # Milliseconds

    def _generate_referrer(self) -> str:
        """Generate referrer URL."""
        referrers = [
            "https://google.com/search",
            "https://facebook.com",
            "https://twitter.com",
            "direct",
            "email",
        ]
        return random.choice(referrers)

    def _generate_ip(self) -> str:
        """Generate random IP address."""
        return (
            f"{random.randint(1, 255)}.{random.randint(0, 255)}."
            f"{random.randint(0, 255)}.{random.randint(1, 255)}"
        )

    def _generate_properties(self, event_type: str) -> Dict[str, Any]:
        """Generate event-specific properties."""
        if event_type == "button_click":
            return {"button_id": f"btn_{random.randint(1, 20)}"}
        elif event_type == "search":
            return {"query": random.choice(["laptop", "mouse", "desk", "chair"])}
        elif event_type == "error":
            return {"error_code": random.choice([404, 500, 503])}
        else:
            return {}


class TimeSeriesDataGenerator:
    """Generate time series data (metrics, sensors)."""

    def generate_metrics(
        self, metric_name: str = "cpu_usage", hours: int = 24, interval_seconds: int = 60
    ) -> List[Dict[str, Any]]:
        """
        Generate time series metrics.

        Args:
            metric_name: Name of the metric
            hours: Number of hours of data
            interval_seconds: Interval between data points

        Returns:
            List of metric dictionaries
        """
        metrics = []
        start_time = datetime.now() - timedelta(hours=hours)
        current_time = start_time
        end_time = datetime.now()

        # Base value and trend
        base_value = 50.0
        trend = 0.0

        while current_time <= end_time:
            # Add noise and trend
            noise = random.uniform(-5, 5)
            trend += random.uniform(-0.5, 0.5)
            value = max(0, min(100, base_value + trend + noise))

            metric = {
                "timestamp": current_time.strftime("%Y-%m-%d %H:%M:%S"),
                "metric_name": metric_name,
                "value": round(value, 2),
                "host": f"host-{random.randint(1, 5)}",
                "tags": {
                    "environment": random.choice(["prod", "staging"]),
                    "region": random.choice(["us-east-1", "us-west-2"]),
                },
            }

            metrics.append(metric)
            current_time += timedelta(seconds=interval_seconds)

        return metrics

    def generate_sensor_data(
        self, sensor_id: str = "SENSOR001", days: int = 7, readings_per_day: int = 96
    ) -> List[Dict[str, Any]]:
        """
        Generate sensor readings (temperature, pressure, etc.).

        Args:
            sensor_id: Sensor identifier
            days: Number of days of data
            readings_per_day: Readings per day (96 = every 15 min)

        Returns:
            List of sensor reading dictionaries
        """
        readings = []
        start_time = datetime.now() - timedelta(days=days)
        interval_seconds = (24 * 60 * 60) // readings_per_day

        current_time = start_time
        end_time = datetime.now()

        # Simulate temperature with daily cycle
        while current_time <= end_time:
            hour = current_time.hour
            # Daily temperature cycle: cooler at night, warmer during day
            base_temp = 20 + 10 * abs((hour - 12) / 12)
            noise = random.uniform(-2, 2)
            temperature = base_temp + noise

            reading = {
                "sensor_id": sensor_id,
                "timestamp": current_time.strftime("%Y-%m-%d %H:%M:%S"),
                "temperature_c": round(temperature, 1),
                "humidity_pct": round(random.uniform(30, 70), 1),
                "pressure_hpa": round(random.uniform(980, 1030), 1),
                "battery_pct": round(max(0, 100 - (current_time - start_time).days * 5), 1),
                "status": "ok" if random.random() > 0.05 else "warning",
            }

            readings.append(reading)
            current_time += timedelta(seconds=interval_seconds)

        return readings


class SampleDataManager:
    """Manage sample data generation and export."""

    def __init__(self, output_dir: Optional[str] = None):
        """
        Initialize sample data manager.

        Args:
            output_dir: Directory to write sample data
        """
        self.output_dir = Path(output_dir or Path.home() / ".fluid" / "samples")
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.customer_gen = CustomerDataGenerator()
        self.order_gen = OrderDataGenerator()
        self.event_gen = EventDataGenerator()
        self.timeseries_gen = TimeSeriesDataGenerator()

    def generate_all(self, format: str = "csv") -> Dict[str, str]:
        """
        Generate all sample datasets.

        Args:
            format: Output format (csv, json, parquet)

        Returns:
            Dictionary of dataset_name -> file_path
        """
        files = {}

        # Generate customers
        customers = self.customer_gen.generate(count=100)
        files["customers"] = self._write_dataset(customers, "customers", format)

        # Generate orders
        customer_ids = [c["customer_id"] for c in customers]
        orders = self.order_gen.generate(count=1000, customer_ids=customer_ids)
        files["orders"] = self._write_dataset(orders, "orders", format)

        # Generate events
        user_ids = customer_ids  # Reuse customer IDs as user IDs
        events = self.event_gen.generate(count=10000, user_ids=user_ids)
        files["events"] = self._write_dataset(events, "events", format)

        # Generate metrics
        metrics = self.timeseries_gen.generate_metrics(
            metric_name="cpu_usage", hours=24, interval_seconds=300
        )
        files["metrics"] = self._write_dataset(metrics, "metrics", format)

        # Generate sensor data
        sensors = self.timeseries_gen.generate_sensor_data(
            sensor_id="SENSOR001", days=7, readings_per_day=96
        )
        files["sensor_readings"] = self._write_dataset(sensors, "sensor_readings", format)

        return files

    def _write_dataset(self, data: List[Dict[str, Any]], name: str, format: str) -> str:
        """Write dataset to file."""
        if format == "csv":
            return self._write_csv(data, name)
        elif format == "json":
            return self._write_json(data, name)
        elif format == "parquet":
            return self._write_parquet(data, name)
        else:
            raise ValueError(f"Unsupported format: {format}")

    def _write_csv(self, data: List[Dict[str, Any]], name: str) -> str:
        """Write data to CSV file."""
        import csv

        filepath = self.output_dir / f"{name}.csv"

        if not data:
            return str(filepath)

        with open(filepath, "w", newline="", encoding="utf-8") as f:
            # Handle nested dictionaries by converting to JSON strings
            flattened_data = []
            for row in data:
                flat_row = {}
                for key, value in row.items():
                    if isinstance(value, (dict, list)):
                        flat_row[key] = json.dumps(value)
                    else:
                        flat_row[key] = value
                flattened_data.append(flat_row)

            writer = csv.DictWriter(f, fieldnames=flattened_data[0].keys())
            writer.writeheader()
            writer.writerows(flattened_data)

        return str(filepath)

    def _write_json(self, data: List[Dict[str, Any]], name: str) -> str:
        """Write data to JSON file."""
        filepath = self.output_dir / f"{name}.json"

        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str)

        return str(filepath)

    def _write_parquet(self, data: List[Dict[str, Any]], name: str) -> str:
        """Write data to Parquet file."""
        try:
            import pandas as pd

            filepath = self.output_dir / f"{name}.parquet"
            df = pd.DataFrame(data)
            df.to_parquet(filepath, index=False)

            return str(filepath)

        except ImportError:
            raise ImportError(
                "Parquet support requires pandas and pyarrow. "
                "Install with: pip install pandas pyarrow"
            )

    def get_summary(self) -> Dict[str, Any]:
        """Get summary of generated datasets."""
        summary = {"output_directory": str(self.output_dir), "datasets": []}

        for filepath in self.output_dir.glob("*"):
            if filepath.is_file():
                size_kb = filepath.stat().st_size / 1024
                summary["datasets"].append(
                    {
                        "name": filepath.stem,
                        "format": filepath.suffix[1:],
                        "size_kb": round(size_kb, 2),
                        "path": str(filepath),
                    }
                )

        return summary
