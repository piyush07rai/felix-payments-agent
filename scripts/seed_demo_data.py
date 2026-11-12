#!/usr/bin/env python3
"""Synthetic Demo Data Generator and Seeder for Felix Payments Decline Analyst.

Generates realistic payment transaction datasets with simulated decline anomalies:
- Normal baseline for Visa & Mastercard (~85-88% approval).
- High-value RuPay MCC 5732 (Electronics) decline anomaly driven by Issuer 91 timeouts.
- ISO 8583 response codes: 00 (Approved), 05 (Do Not Honor), 51 (Insufficient Funds),
  91 (System Malfunction), 96 (System Error).
"""

import csv
import io
import os
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Add project root to sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.database import get_connection, init_database, load_csv_data

NETWORKS = ["Visa", "Mastercard", "RuPay"]
ISSUERS = ["HDFC Bank", "State Bank of India", "ICICI Bank", "Axis Bank", "Kotak Mahindra Bank"]
CHANNELS = ["e-commerce", "mobile_app", "pos"]
MCCS = ["5732", "5411", "5812", "4722", "5999"]
MERCHANTS = [f"merch_{100 + i}" for i in range(15)]
PAS = [f"pa_{10 + i}" for i in range(5)]
THREE_DS_RESULTS = ["success", "frictionless", "failed", "timeout", "unavailable"]

def generate_transactions(count: int = 10000) -> str:
    """Generate CSV string of realistic payment transactions."""
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "txn_id", "ts", "network", "issuer_bank", "bin",
        "channel", "mcc", "merchant_id", "pa_id", "amount",
        "response_code", "3ds_result", "latency_ms", "is_retry"
    ])

    now = datetime.now(timezone.utc)

    for i in range(count):
        txn_id = f"txn_{1000000 + i}"
        # Spread transactions across past 24 hours
        minutes_ago = random.randint(0, 1440)
        ts = (now - timedelta(minutes=minutes_ago)).isoformat()

        network = random.choices(NETWORKS, weights=[45, 35, 20])[0]
        issuer = random.choice(ISSUERS)
        bin_num = f"{4 if network == 'Visa' else 5 if network == 'Mastercard' else 6}{random.randint(10000, 99999)}"
        channel = random.choices(CHANNELS, weights=[60, 30, 10])[0]
        mcc = random.choices(MCCS, weights=[25, 30, 20, 15, 10])[0]
        merchant_id = random.choice(MERCHANTS)
        pa_id = random.choice(PAS)
        amount = round(random.uniform(150.0, 45000.0), 2)
        is_retry = 1 if random.random() < 0.08 else 0

        # Inject Decline Anomaly:
        # High value RuPay on MCC 5732 (Electronics) suffers from Code 91 (Issuer Malfunction)
        if network == "RuPay" and mcc == "5732" and amount > 25000:
            response_code = random.choices(["91", "96", "00", "05"], weights=[70, 15, 10, 5])[0]
            three_ds = "timeout" if response_code == "91" else "success"
            latency = round(random.uniform(1800.0, 4200.0), 1)
        else:
            # Healthy baseline: ~85% approval
            roll = random.random()
            if roll < 0.85:
                response_code = "00"
                three_ds = random.choices(["success", "frictionless"], weights=[70, 30])[0]
                latency = round(random.uniform(120.0, 650.0), 1)
            elif roll < 0.91:
                response_code = "05"  # Do not honor
                three_ds = random.choices(["failed", "success"], weights=[60, 40])[0]
                latency = round(random.uniform(200.0, 900.0), 1)
            elif roll < 0.96:
                response_code = "51"  # Insufficient funds
                three_ds = "success"
                latency = round(random.uniform(180.0, 750.0), 1)
            elif roll < 0.985:
                response_code = "91"  # System malfunction
                three_ds = random.choices(["timeout", "unavailable"], weights=[60, 40])[0]
                latency = round(random.uniform(1200.0, 3000.0), 1)
            else:
                response_code = "96"  # System error
                three_ds = "unavailable"
                latency = round(random.uniform(800.0, 2500.0), 1)

        writer.writerow([
            txn_id, ts, network, issuer, bin_num,
            channel, mcc, merchant_id, pa_id, amount,
            response_code, three_ds, latency, is_retry
        ])

    return output.getvalue()

def seed(count: int = 10000, export_csv_path: str = None):
    """Seed transactions into the SQLite database and optionally save to CSV."""
    init_database()
    print(f"Generating {count} realistic synthetic payment transactions with decline anomaly cohorts...")
    csv_data = generate_transactions(count)

    if export_csv_path:
        csv_file = Path(export_csv_path)
        csv_file.parent.mkdir(parents=True, exist_ok=True)
        csv_file.write_text(csv_data, encoding="utf-8")
        print(f"Saved CSV to: {csv_file}")

    result = load_csv_data(csv_data, filename="demo_seeded_data.csv", replace=True)
    print(f"Database seeded successfully: {result.get('rows_loaded', 0)} rows loaded.")
    return result

if __name__ == "__main__":
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 10000
    export_path = sys.argv[2] if len(sys.argv) > 2 else "data/sample_transactions.csv"
    seed(count, export_path)
