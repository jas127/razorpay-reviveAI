"""Generate and insert deterministic synthetic Razorpay-shaped data for ReviveAI."""

from __future__ import annotations

import json
import random
import sqlite3
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


DB_DIRECTORY = Path(__file__).resolve().parent
PROJECT_ROOT = DB_DIRECTORY.parent
DATABASE_PATH = PROJECT_ROOT / "revive.db"
RANDOM_SEED = 20260828
CUSTOMER_COUNT = 150
REVENUE_EVENT_COUNT = 300

sys.path.insert(0, str(DB_DIRECTORY))
from init_db import initialize_database  # noqa: E402


FIRST_NAMES = [
    "Aarav", "Aditi", "Aditya", "Akash", "Akshay", "Amrita", "Ananya",
    "Aniket", "Anjali", "Ankit", "Arjun", "Arpita", "Aryan", "Ashish",
    "Ashwini", "Avinash", "Bhavna", "Chaitanya", "Charu", "Chetan",
    "Deepa", "Deepak", "Dev", "Diya", "Esha", "Farhan", "Gaurav",
    "Geeta", "Harish", "Isha", "Ishaan", "Jaya", "Karan", "Kavita",
    "Kiran", "Krishna", "Lakshmi", "Madhav", "Mahima", "Manish", "Meera",
    "Mihir", "Mohit", "Nandini", "Naveen", "Neha", "Nikhil", "Nisha",
    "Pallavi", "Parth", "Pooja", "Pradeep", "Pranav", "Priya", "Rahul",
    "Raj", "Rajat", "Rakesh", "Rhea", "Rishi", "Ritu", "Rohan",
    "Rohit", "Roshni", "Sachin", "Sakshi", "Sameer", "Sana", "Sanjay",
    "Sanjana", "Sarika", "Saurabh", "Shalini", "Shilpa", "Shivam",
    "Shraddha", "Shreya", "Siddharth", "Simran", "Sneha", "Sonali",
    "Srinivas", "Sunita", "Suraj", "Tanvi", "Tarun", "Uma", "Varun",
    "Vasudha", "Vikram", "Vinay", "Vishal", "Yash", "Yogesh",
]

LAST_NAMES = [
    "Agarwal", "Bansal", "Bhat", "Chatterjee", "Chauhan", "Desai",
    "Dutta", "Ghosh", "Goyal", "Gupta", "Iyer", "Jain", "Joshi", "Kapoor",
    "Kar", "Kaur", "Kulkarni", "Malhotra", "Mehta", "Menon", "Mishra",
    "Mukherjee", "Nair", "Naidu", "Patel", "Pillai", "Prasad", "Rao",
    "Reddy", "Roy", "Saxena", "Sengupta", "Shah", "Sharma", "Shetty",
    "Singh", "Sinha", "Soni", "Srivastava", "Subramanian", "Thakur",
    "Verma", "Wadhwa", "Yadav",
]

FAILURE_CODES = (
    "insufficient_funds",
    "card_expired",
    "otp_timeout",
    "bank_server_down",
    "do_not_honor",
)

EVENT_DISTRIBUTION = {
    "payment_failed": 90,
    "checkout_abandoned": 90,
    "subscription_failed": 75,
    "invoice_overdue": 45,
}

RISK_CATEGORY_BY_EVENT = {
    "payment_failed": "payment_degradation",
    "checkout_abandoned": "checkout_dropoff",
    "subscription_failed": "subscription_failure",
    "invoice_overdue": "receivable_overdue",
}

PAYMENT_METHODS = ("card", "upi", "netbanking", "mandate")


def iso_timestamp(value: datetime) -> str:
    """Return a SQLite-friendly UTC timestamp without a timezone suffix."""
    return value.astimezone(timezone.utc).replace(tzinfo=None).isoformat(
        sep=" ", timespec="seconds"
    )


def slugify(value: str) -> str:
    return value.lower().replace(" ", ".")


def make_customers(rng: random.Random) -> list[dict[str, Any]]:
    """Create 150 customers with exactly 10% opted out for demo coverage."""
    names = [
        f"{first} {last}"
        for first in FIRST_NAMES
        for last in LAST_NAMES
    ]
    rng.shuffle(names)
    names = names[:CUSTOMER_COUNT]
    opted_out_ids = set(rng.sample(range(CUSTOMER_COUNT), 15))
    now = datetime.now(timezone.utc)

    customers = []
    for index, name in enumerate(names, start=1):
        customer_id = f"cust_{index:04d}"
        email_local = f"{slugify(name)}{index:03d}"
        phone_number = f"+91 {rng.randint(6, 9)}{rng.randint(100000000, 999999999)}"
        preferred_language = (
            "hi-en" if rng.random() < 0.22 else "en"
        )
        contact_count = rng.choices([0, 1, 2], weights=[65, 25, 10])[0]
        created_at = now - timedelta(days=rng.randint(30, 540))
        customers.append(
            {
                "customer_id": customer_id,
                "name": name,
                "email": f"{email_local}@example.in",
                "phone": phone_number,
                "preferred_language": preferred_language,
                "contact_count_last_7d": contact_count,
                "opted_out": int((index - 1) in opted_out_ids),
                "created_at": iso_timestamp(created_at),
            }
        )
    return customers


def amount_for_event(event_type: str, rng: random.Random) -> int:
    """Choose INR amounts from category-specific merchant-like distributions."""
    if event_type == "subscription_failed":
        return rng.choice([799, 999, 1499, 1999, 2499, 3999, 5999, 8999])
    if event_type == "checkout_abandoned":
        return rng.choice([499, 799, 999, 1499, 2499, 3999, 5999, 7999, 11999])
    if event_type == "payment_failed":
        return rng.choice(
            [199, 299, 499, 799, 999, 1499, 2499, 3999, 5999, 9999, 14999, 24999]
        )
    return rng.choice(
        [15000, 18000, 22500, 27500, 35000, 42000, 50000, 65000, 75000, 85000]
    )


def failure_code_for_event(event_type: str, rng: random.Random) -> str | None:
    if event_type == "invoice_overdue":
        return rng.choices(
            [None, "insufficient_funds", "do_not_honor", "bank_server_down"],
            weights=[55, 20, 15, 10],
        )[0]
    if event_type == "checkout_abandoned":
        return rng.choice(["otp_timeout", "bank_server_down", "insufficient_funds"])
    if event_type == "subscription_failed":
        return rng.choice(
            ["insufficient_funds", "card_expired", "bank_server_down", "do_not_honor"]
        )
    return rng.choice(FAILURE_CODES)


def payment_method_for_event(event_type: str, rng: random.Random) -> str:
    if event_type == "subscription_failed":
        return "mandate"
    if event_type == "checkout_abandoned":
        return rng.choice(["card", "upi", "netbanking"])
    return rng.choice(PAYMENT_METHODS[:-1])


def make_webhook_metadata(
    event: dict[str, Any],
    *,
    rng: random.Random,
) -> str:
    """Serialize source columns plus a Razorpay-shaped webhook envelope."""
    event_type = event["event_type"]
    webhook_name = {
        "payment_failed": "payment.failed",
        "checkout_abandoned": "checkout.session.abandoned",
        "subscription_failed": "subscription.charged.failed",
        "invoice_overdue": "invoice.overdue",
    }[event_type]
    payload = {
        "id": f"hook_{uuid.UUID(event['event_id']).hex[:20]}",
        "entity": "event",
        "event": webhook_name,
        "created_at": event["occurred_at"],
        "account_id": "acc_revive_demo",
        "data": {
            "customer": {
                "id": event["customer_id"],
            },
            "payment": {
                "amount": int(event["amount"] * 100),
                "currency": event["currency"],
                "method": event["payment_method"],
                "error": {"code": event["failure_code"]}
                if event["failure_code"]
                else None,
            },
            "subscription": {"id": event["subscription_id"]}
            if event["subscription_id"]
            else None,
            "invoice": {"id": event["invoice_id"]}
            if event["invoice_id"]
            else None,
            "checkout": {"id": event["checkout_session_id"]}
            if event["checkout_session_id"]
            else None,
        },
        "source_row": {
            key: value
            for key, value in event.items()
            if key != "raw_metadata"
        },
        "synthetic": True,
        "generator_seed": RANDOM_SEED,
        "demo_note": "Synthetic Razorpay-shaped payload for ReviveAI Phase 0.",
    }
    if event_type == "invoice_overdue":
        due_at = datetime.fromisoformat(event["occurred_at"])
        payload["data"]["invoice"]["due_date"] = event["occurred_at"]
        payload["data"]["invoice"]["days_overdue"] = max(
            1, (datetime.now(timezone.utc).replace(tzinfo=None) - due_at).days
        )
    if event_type == "checkout_abandoned":
        payload["data"]["checkout"]["abandoned_reason"] = rng.choice(
            ["payment_method_friction", "price_hesitation", "technical_failure"]
        )
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def make_revenue_events(
    customers: list[dict[str, Any]],
    rng: random.Random,
) -> list[dict[str, Any]]:
    """Create the exact requested event mix over the previous 14 days."""
    customer_ids = [customer["customer_id"] for customer in customers]
    event_types = [
        event_type
        for event_type, count in EVENT_DISTRIBUTION.items()
        for _ in range(count)
    ]
    rng.shuffle(event_types)
    now = datetime.now(timezone.utc)
    events = []

    for event_type in event_types:
        customer_id = rng.choice(customer_ids)
        occurred_at = now - timedelta(
            days=rng.uniform(0.1, 13.9),
            hours=rng.uniform(0, 5),
            minutes=rng.randint(0, 59),
        )
        event_id = str(uuid.UUID(int=rng.getrandbits(128)))
        event = {
            "event_id": event_id,
            "customer_id": customer_id,
            "event_type": event_type,
            "amount": amount_for_event(event_type, rng),
            "currency": "INR",
            "payment_method": payment_method_for_event(event_type, rng),
            "failure_code": failure_code_for_event(event_type, rng),
            "subscription_id": (
                f"sub_{rng.getrandbits(64):016x}"
                if event_type == "subscription_failed"
                else None
            ),
            "invoice_id": (
                f"inv_{rng.getrandbits(64):016x}"
                if event_type == "invoice_overdue"
                else None
            ),
            "checkout_session_id": (
                f"cs_{rng.getrandbits(64):016x}"
                if event_type == "checkout_abandoned"
                else None
            ),
            "occurred_at": iso_timestamp(occurred_at),
        }
        event["raw_metadata"] = make_webhook_metadata(event, rng=rng)
        events.append(event)
    return events


def clear_seedable_tables(connection: sqlite3.Connection) -> None:
    """Reset Phase 0 tables so repeated runs remain reproducible."""
    for table in (
        "outcomes",
        "actions_taken",
        "policy_decisions",
        "diagnoses",
        "risk_events",
        "audit_log",
        "revenue_events",
        "customers",
        "policy_rules",
    ):
        connection.execute(f"DELETE FROM {table}")


def insert_data(
    connection: sqlite3.Connection,
    customers: list[dict[str, Any]],
    events: list[dict[str, Any]],
) -> None:
    connection.executemany(
        """
        INSERT INTO customers (
            customer_id, name, email, phone, preferred_language,
            contact_count_last_7d, opted_out, created_at
        ) VALUES (
            :customer_id, :name, :email, :phone, :preferred_language,
            :contact_count_last_7d, :opted_out, :created_at
        )
        """,
        customers,
    )
    connection.executemany(
        """
        INSERT INTO revenue_events (
            event_id, customer_id, event_type, amount, currency,
            payment_method, failure_code, subscription_id, invoice_id,
            checkout_session_id, occurred_at, raw_metadata
        ) VALUES (
            :event_id, :customer_id, :event_type, :amount, :currency,
            :payment_method, :failure_code, :subscription_id, :invoice_id,
            :checkout_session_id, :occurred_at, :raw_metadata
        )
        """,
        events,
    )


def print_summary(connection: sqlite3.Connection) -> None:
    customer_count = connection.execute(
        "SELECT COUNT(*) FROM customers"
    ).fetchone()[0]
    opted_out_count = connection.execute(
        "SELECT COUNT(*) FROM customers WHERE opted_out = 1"
    ).fetchone()[0]
    event_count = connection.execute(
        "SELECT COUNT(*) FROM revenue_events"
    ).fetchone()[0]
    total_amount = connection.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM revenue_events"
    ).fetchone()[0]

    print(f"Customers created: {customer_count} ({opted_out_count} opted out)")
    print(f"Revenue events created: {event_count}")
    print("Events by category:")
    rows = connection.execute(
        """
        SELECT event_type, COUNT(*) AS event_count, ROUND(SUM(amount), 2) AS amount
        FROM revenue_events
        GROUP BY event_type
        ORDER BY event_type
        """
    ).fetchall()
    for event_type, count, amount in rows:
        print(f"  - {event_type}: {count} events, ₹{amount:,.2f}")
    print(f"Total synthetic revenue at source: ₹{total_amount:,.2f}")


def main() -> None:
    rng = random.Random(RANDOM_SEED)
    initialize_database(DATABASE_PATH, reset=True)
    customers = make_customers(rng)
    events = make_revenue_events(customers, rng)

    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        clear_seedable_tables(connection)
        insert_data(connection, customers, events)
        print_summary(connection)

    print(f"Seeded database: {DATABASE_PATH}")


if __name__ == "__main__":
    main()