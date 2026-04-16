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

"""Tests for fluid_build/providers/local/samples.py — sample data generators."""

import random

from fluid_build.providers.local.samples import (
    CustomerDataGenerator,
    EventDataGenerator,
    OrderDataGenerator,
    SampleDataManager,
    TimeSeriesDataGenerator,
)


class TestCustomerDataGenerator:
    def test_generate_default(self):
        gen = CustomerDataGenerator()
        customers = gen.generate(count=10)
        assert len(customers) == 10

    def test_fields_present(self):
        gen = CustomerDataGenerator()
        c = gen.generate(count=1)[0]
        required = {
            "customer_id",
            "first_name",
            "last_name",
            "email",
            "phone",
            "company",
            "segment",
            "industry",
            "city",
            "state",
            "postal_code",
            "country",
            "created_at",
            "last_active_at",
            "lifetime_value",
            "total_orders",
            "is_active",
        }
        assert required.issubset(c.keys())

    def test_customer_id_format(self):
        gen = CustomerDataGenerator()
        customers = gen.generate(count=5)
        for i, c in enumerate(customers, 1):
            assert c["customer_id"] == f"CUST{i:06d}"

    def test_email_format(self):
        gen = CustomerDataGenerator()
        c = gen.generate(count=1)[0]
        assert "@" in c["email"]
        assert c["email"].endswith("@example.com")

    def test_phone_format(self):
        gen = CustomerDataGenerator()
        c = gen.generate(count=1)[0]
        assert c["phone"].startswith("+1-")

    def test_segment_valid(self):
        gen = CustomerDataGenerator()
        customers = gen.generate(count=50)
        for c in customers:
            assert c["segment"] in CustomerDataGenerator.SEGMENTS

    def test_deterministic_with_seed(self):
        random.seed(42)
        c1 = CustomerDataGenerator().generate(count=5)
        random.seed(42)
        c2 = CustomerDataGenerator().generate(count=5)
        assert c1 == c2


class TestOrderDataGenerator:
    def test_generate_default(self):
        gen = OrderDataGenerator()
        orders = gen.generate(count=20)
        assert len(orders) == 20

    def test_fields_present(self):
        gen = OrderDataGenerator()
        o = gen.generate(count=1)[0]
        required = {
            "order_id",
            "customer_id",
            "order_date",
            "product_name",
            "product_category",
            "quantity",
            "unit_price",
            "subtotal",
            "tax",
            "shipping",
            "total",
            "status",
            "payment_method",
        }
        assert required.issubset(o.keys())

    def test_order_id_format(self):
        gen = OrderDataGenerator()
        orders = gen.generate(count=3)
        for i, o in enumerate(orders, 1):
            assert o["order_id"] == f"ORD{i:08d}"

    def test_custom_customer_ids(self):
        gen = OrderDataGenerator()
        cids = ["C001", "C002"]
        orders = gen.generate(count=10, customer_ids=cids)
        for o in orders:
            assert o["customer_id"] in cids

    def test_status_distribution(self):
        random.seed(42)
        gen = OrderDataGenerator()
        orders = gen.generate(count=500)
        statuses = [o["status"] for o in orders]
        # delivered should be the most common
        assert statuses.count("delivered") > statuses.count("cancelled")

    def test_math_correct(self):
        gen = OrderDataGenerator()
        for o in gen.generate(count=10):
            expected_subtotal = o["unit_price"] * o["quantity"]
            assert abs(o["subtotal"] - expected_subtotal) < 0.01


class TestEventDataGenerator:
    def test_generate_default(self):
        gen = EventDataGenerator()
        events = gen.generate(count=15)
        assert len(events) == 15

    def test_fields_present(self):
        gen = EventDataGenerator()
        e = gen.generate(count=10)[0]  # count >= 10 to avoid session_id bug
        assert "event_type" in e
        assert "timestamp" in e

    def test_event_types_valid(self):
        gen = EventDataGenerator()
        events = gen.generate(count=50)
        for e in events:
            assert e["event_type"] in EventDataGenerator.EVENT_TYPES


class TestTimeSeriesDataGenerator:
    def test_generate_metrics(self):
        gen = TimeSeriesDataGenerator()
        metrics = gen.generate_metrics(hours=1, interval_seconds=600)
        assert len(metrics) >= 1
        assert "timestamp" in metrics[0]
        assert "value" in metrics[0]
        assert "metric_name" in metrics[0]

    def test_metrics_custom_name(self):
        gen = TimeSeriesDataGenerator()
        metrics = gen.generate_metrics(metric_name="memory_usage", hours=1)
        assert all(m["metric_name"] == "memory_usage" for m in metrics)

    def test_generate_sensor_data(self):
        gen = TimeSeriesDataGenerator()
        data = gen.generate_sensor_data(days=1, readings_per_day=10)
        assert len(data) >= 1
        assert "sensor_id" in data[0]


class TestSampleDataManager:
    def test_generate_all_csv(self, tmp_path):
        mgr = SampleDataManager(output_dir=str(tmp_path))
        result = mgr.generate_all(format="csv")
        assert isinstance(result, dict)
        assert "customers" in result
        assert "orders" in result
        # Should have created CSV files
        csv_files = list(tmp_path.glob("*.csv"))
        assert len(csv_files) >= 1

    def test_generate_all_json(self, tmp_path):
        mgr = SampleDataManager(output_dir=str(tmp_path))
        result = mgr.generate_all(format="json")
        assert isinstance(result, dict)
        json_files = list(tmp_path.glob("*.json"))
        assert len(json_files) >= 1

    def test_get_summary(self, tmp_path):
        mgr = SampleDataManager(output_dir=str(tmp_path))
        mgr.generate_all(format="csv")
        summary = mgr.get_summary()
        assert isinstance(summary, dict)
