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

"""Tests for providers/local/samples.py — sample data generators."""

from fluid_build.providers.local.samples import (
    CustomerDataGenerator,
    EventDataGenerator,
    OrderDataGenerator,
    TimeSeriesDataGenerator,
)


class TestCustomerDataGenerator:
    def test_generate_count(self):
        gen = CustomerDataGenerator()
        customers = gen.generate(5)
        assert len(customers) == 5

    def test_generate_schema(self):
        gen = CustomerDataGenerator()
        c = gen.generate(1)[0]
        assert "customer_id" in c
        assert "first_name" in c
        assert "email" in c
        assert "phone" in c
        assert "segment" in c
        assert "is_active" in c

    def test_customer_id_format(self):
        gen = CustomerDataGenerator()
        c = gen.generate(1)[0]
        assert c["customer_id"].startswith("CUST")

    def test_generate_phone(self):
        gen = CustomerDataGenerator()
        phone = gen._generate_phone()
        assert phone.startswith("+1-")
        assert len(phone.split("-")) == 4

    def test_random_date_format(self):
        gen = CustomerDataGenerator()
        date = gen._random_date(30)
        # Should be YYYY-MM-DD HH:MM:SS
        assert len(date) == 19
        assert "-" in date
        assert ":" in date


class TestOrderDataGenerator:
    def test_generate_count(self):
        gen = OrderDataGenerator()
        orders = gen.generate(5, [f"CUST{i:06d}" for i in range(1, 4)])
        assert len(orders) == 5

    def test_generate_schema(self):
        gen = OrderDataGenerator()
        o = gen.generate(1, ["CUST000001"])[0]
        assert "order_id" in o
        assert "customer_id" in o
        assert "total" in o
        assert "status" in o

    def test_total_positive(self):
        gen = OrderDataGenerator()
        orders = gen.generate(10, ["CUST000001"])
        for o in orders:
            assert o["total"] >= 0

    def test_weighted_status_valid(self):
        gen = OrderDataGenerator()
        valid_statuses = {"pending", "processing", "shipped", "delivered", "cancelled"}
        for _ in range(20):
            s = gen._weighted_status()
            assert s in valid_statuses

    def test_generate_address(self):
        gen = OrderDataGenerator()
        addr = gen._generate_address()
        assert isinstance(addr, str)
        assert len(addr) > 0

    def test_generate_tracking(self):
        gen = OrderDataGenerator()
        tracking = gen._generate_tracking()
        assert tracking.startswith("TRK")
        assert len(tracking) == 15  # "TRK" + 12 chars


class TestEventDataGenerator:
    def test_generate_count(self):
        gen = EventDataGenerator()
        events = gen.generate(20, ["user1", "user2", "user3"])
        assert len(events) == 20

    def test_generate_schema(self):
        gen = EventDataGenerator()
        e = gen.generate(10, ["user1", "user2"])[0]
        assert "event_id" in e
        assert "event_type" in e
        assert "user_id" in e
        assert "session_id" in e

    def test_generate_ip(self):
        gen = EventDataGenerator()
        ip = gen._generate_ip()
        parts = ip.split(".")
        assert len(parts) == 4
        for p in parts:
            assert 0 <= int(p) <= 255

    def test_generate_properties_returns_dict(self):
        gen = EventDataGenerator()
        props = gen._generate_properties("page_view")
        assert isinstance(props, dict)


class TestTimeSeriesDataGenerator:
    def test_generate_metrics_count(self):
        gen = TimeSeriesDataGenerator()
        metrics = gen.generate_metrics("cpu_usage", hours=1, interval_seconds=60)
        # ~60 data points (1 hour / 60 seconds, may be off by 1)
        assert 59 <= len(metrics) <= 61

    def test_generate_metrics_schema(self):
        gen = TimeSeriesDataGenerator()
        m = gen.generate_metrics("cpu_usage", hours=1, interval_seconds=3600)[0]
        assert "timestamp" in m
        assert "metric_name" in m
        assert "value" in m

    def test_generate_sensor_data_count(self):
        gen = TimeSeriesDataGenerator()
        data = gen.generate_sensor_data("sensor_1", days=1, readings_per_day=24)
        assert 24 <= len(data) <= 25

    def test_generate_sensor_data_schema(self):
        gen = TimeSeriesDataGenerator()
        d = gen.generate_sensor_data("sensor_1", days=1, readings_per_day=1)[0]
        assert "sensor_id" in d
        assert "timestamp" in d
        assert "temperature_c" in d
