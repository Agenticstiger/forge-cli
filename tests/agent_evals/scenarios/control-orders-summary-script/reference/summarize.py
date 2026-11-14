# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Print the total amount per currency in data/orders.csv, sorted by currency code."""

import csv
from collections import defaultdict
from decimal import Decimal

totals = defaultdict(Decimal)
with open("data/orders.csv", newline="", encoding="utf-8") as handle:
    for row in csv.DictReader(handle):
        totals[row["currency"]] += Decimal(row["amount"])
for currency in sorted(totals):
    print(currency, totals[currency])
