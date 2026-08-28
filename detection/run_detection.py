"""Deterministic ReviveAI Detection Engine.

This module intentionally contains no LLM or external API calls. It converts
raw revenue events into risk events and records the detection decision in the
append-only audit log.

Run from the project root:

    python detection/run_detection.py
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATABASE_PATH = PROJECT_ROOT / "revive.db"

RISK_CATEGORY_BY_EVENT = {
    "payment_failed": "payment_degradation",
    "checkout_abandoned": "checkout_dropoff",
    "subscription_failed": "subscription_failure",
    "invoice_overdue": "receivable_overdue",
}

EVENT_QUERY = """
    SELECT
        re.*,
        c.name AS customer_name,
        c.email AS customer_email,
        c.preferred_language AS customer_preferred_language,
        c.contact_count_last_7d AS customer_contact_count_last_7d,
        c.opted_out AS customer_opted_out
    FROM revenue_events AS re
    INNER JOIN customers AS c ON c.customer_id = re.customer_id
    WHERE re.event_type = ?
    ORDER BY re.occurred_at ASC, re.event_id ASC
"""

# Higher values represent failure causes that are less likely to resolve
# without intervention. Unknown codes receive a conservative middle score.
FAILURE_SEVERITY = {
    "bank_server_down": 0.30,
    "otp_timeout": 0.35,
    "insufficient_funds": 0.55,
    "do_not_honor": 0.70,
    "card_expired": 0.80,
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def timestamp_for_sql(value: datetime) -> str:
    """Format a UTC datetime consistently with the Phase 0 seed data."""
    return value.astimezone(timezone.utc).replace(tzinfo=None).isoformat(
        sep=" ", timespec="seconds"
    )


def parse_timestamp(value: str) -> datetime:
    """Parse a SQLite timestamp and attach UTC when it has no timezone."""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def event_age_hours(occurred_at: str, *, now: datetime | None = None) -> float:
    reference_time = now or utc_now()
    return max(
        0.0,
        (reference_time - parse_timestamp(occurred_at)).total_seconds() / 3600,
    )


def event_age_days(occurred_at: str, *, now: datetime | None = None) -> float:
    return event_age_hours(occurred_at, now=now) / 24


def failure_severity(failure_code: str | None) -> float:
    return FAILURE_SEVERITY.get(failure_code or "", 0.50)


def row_snapshot(row: sqlite3.Row) -> dict[str, Any]:
    """Convert a database row to a JSON-serializable audit input snapshot."""
    return {key: row[key] for key in row.keys()}


def create_risk_event(
    connection: sqlite3.Connection,
    *,
    row: sqlite3.Row,
    risk_category: str,
    risk_score: float,
    detection_reason: str,
) -> dict[str, Any] | None:
    """Insert one risk event and its audit record, once per source event.

    The UNIQUE constraint on risk_events.event_id is the database-level
    idempotency guard. INSERT OR IGNORE makes a rerun a clean no-op when a
    source event was already detected.
    """
    event_id = row["event_id"]
    risk_id = f"risk_{event_id}"
    detected_at = timestamp_for_sql(utc_now())
    normalized_score = max(0.0, min(1.0, float(risk_score)))
    amount_at_risk = float(row["amount"] or 0)

    cursor = connection.execute(
        """
        INSERT OR IGNORE INTO risk_events (
            risk_id, event_id, customer_id, risk_category, risk_score,
            amount_at_risk, detected_at, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'new')
        """,
        (
            risk_id,
            event_id,
            row["customer_id"],
            risk_category,
            normalized_score,
            amount_at_risk,
            detected_at,
        ),
    )
    if cursor.rowcount != 1:
        return None

    input_snapshot = row_snapshot(row)
    output_snapshot = {
        "risk_id": risk_id,
        "event_id": event_id,
        "customer_id": row["customer_id"],
        "risk_category": risk_category,
        "risk_score": normalized_score,
        "amount_at_risk": amount_at_risk,
        "status": "new",
        "detection_reason": detection_reason,
    }
    connection.execute(
        """
        INSERT INTO audit_log (
            log_id, risk_id, stage, actor, input_snapshot,
            output_snapshot, timestamp
        ) VALUES (?, ?, 'detection', 'system', ?, ?, ?)
        """,
        (
            f"audit_detection_{event_id}",
            risk_id,
            json.dumps(input_snapshot, ensure_ascii=False, sort_keys=True),
            json.dumps(output_snapshot, ensure_ascii=False, sort_keys=True),
            detected_at,
        ),
    )
    return output_snapshot


def detect_payment_failures(
    connection: sqlite3.Connection,
) -> list[dict[str, Any]]:
    """Detect one-off payment failures using failure cause and amount.

    Formula:
        risk_score = min(
            1,
            failure_severity
            + 0.25 * min(1, amount / 25_000)
        )

    Failure severity is a deterministic lookup from 0.30 for a likely
    transient bank outage to 0.80 for an expired card. The amount component
    makes larger failed payments more consequential without dominating the
    cause signal.
    """
    created: list[dict[str, Any]] = []
    rows = connection.execute(EVENT_QUERY, ("payment_failed",))
    for row in rows:
        amount_component = min(1.0, float(row["amount"] or 0) / 25_000)
        score = min(
            1.0,
            failure_severity(row["failure_code"]) + 0.25 * amount_component,
        )
        result = create_risk_event(
            connection,
            row=row,
            risk_category="payment_degradation",
            risk_score=score,
            detection_reason=(
                f"failure_code={row['failure_code'] or 'unknown'}; "
                f"amount_component={amount_component:.3f}"
            ),
        )
        if result:
            created.append(result)
    return created


def detect_checkout_abandonment(
    connection: sqlite3.Connection,
) -> list[dict[str, Any]]:
    """Detect abandoned checkouts using cart amount and time left unresolved.

    Formula:
        risk_score = min(
            1,
            0.55 * min(1, amount / 10_000)
            + 0.45 * min(1, hours_since_event / 24)
        )

    Cart value represents the recoverable revenue opportunity. The age
    component increases the score as a checkout remains unresolved through
    the first 24 hours.
    """
    created: list[dict[str, Any]] = []
    rows = connection.execute(EVENT_QUERY, ("checkout_abandoned",))
    for row in rows:
        hours_since_event = event_age_hours(row["occurred_at"])
        amount_component = min(1.0, float(row["amount"] or 0) / 10_000)
        age_component = min(1.0, hours_since_event / 24)
        score = min(
            1.0,
            0.55 * amount_component + 0.45 * age_component,
        )
        result = create_risk_event(
            connection,
            row=row,
            risk_category="checkout_dropoff",
            risk_score=score,
            detection_reason=(
                f"amount_component={amount_component:.3f}; "
                f"age_hours={hours_since_event:.2f}; "
                f"age_component={age_component:.3f}"
            ),
        )
        if result:
            created.append(result)
    return created


def detect_subscription_failures(
    connection: sqlite3.Connection,
) -> list[dict[str, Any]]:
    """Detect failed recurring debits using failure cause and subscription value.

    Formula:
        risk_score = min(
            1,
            0.65 * failure_severity
            + 0.35 * min(1, amount / 10_000)
        )

    Recurring-payment failures are weighted primarily by the deterministic
    failure severity, with a smaller amount component for subscription value.
    """
    created: list[dict[str, Any]] = []
    rows = connection.execute(EVENT_QUERY, ("subscription_failed",))
    for row in rows:
        amount_component = min(1.0, float(row["amount"] or 0) / 10_000)
        score = min(
            1.0,
            0.65 * failure_severity(row["failure_code"])
            + 0.35 * amount_component,
        )
        result = create_risk_event(
            connection,
            row=row,
            risk_category="subscription_failure",
            risk_score=score,
            detection_reason=(
                f"failure_code={row['failure_code'] or 'unknown'}; "
                f"amount_component={amount_component:.3f}"
            ),
        )
        if result:
            created.append(result)
    return created


def detect_overdue_invoices(
    connection: sqlite3.Connection,
) -> list[dict[str, Any]]:
    """Detect overdue invoices using days overdue and invoice amount.

    Formula:
        risk_score = min(
            1,
            (days_overdue / 30) * 0.6
            + (amount / 50_000) * 0.4
        )

    The due timestamp is the event's occurred_at value in the synthetic
    Phase 0 data. The time component is capped at 30 days and the amount
    component is capped at ₹50,000 before the final score is capped at 1.
    """
    created: list[dict[str, Any]] = []
    rows = connection.execute(EVENT_QUERY, ("invoice_overdue",))
    now = utc_now()
    for row in rows:
        days_overdue = event_age_days(row["occurred_at"], now=now)
        amount = float(row["amount"] or 0)
        score = min(
            1.0,
            (days_overdue / 30) * 0.6 + (amount / 50_000) * 0.4,
        )
        result = create_risk_event(
            connection,
            row=row,
            risk_category="receivable_overdue",
            risk_score=score,
            detection_reason=(
                f"days_overdue={days_overdue:.2f}; "
                f"amount={amount:.2f}"
            ),
        )
        if result:
            created.append(result)
    return created


def database_summary(
    connection: sqlite3.Connection,
) -> list[tuple[str, int, float]]:
    rows = connection.execute(
        """
        SELECT
            risk_category,
            COUNT(*) AS risk_count,
            COALESCE(SUM(amount_at_risk), 0) AS amount_at_risk
        FROM risk_events
        GROUP BY risk_category
        ORDER BY risk_category
        """
    ).fetchall()
    return [
        (row["risk_category"], row["risk_count"], float(row["amount_at_risk"]))
        for row in rows
    ]


def print_summary(
    connection: sqlite3.Connection,
    created: list[dict[str, Any]],
) -> None:
    total_count, total_amount = connection.execute(
        """
        SELECT COUNT(*), COALESCE(SUM(amount_at_risk), 0)
        FROM risk_events
        """
    ).fetchone()
    created_by_category = Counter(item["risk_category"] for item in created)

    print(f"New risk_events created this run: {len(created)}")
    print(
        "Total risk_events tracked: "
        f"{total_count} | Total amount_at_risk: ₹{float(total_amount):,.2f}"
    )
    print("Risk events by category:")
    for category, count, amount in database_summary(connection):
        print(
            f"  - {category}: {count} events, "
            f"₹{amount:,.2f} at risk "
            f"({created_by_category.get(category, 0)} created this run)"
        )


def main() -> None:
    if not DATABASE_PATH.exists():
        raise SystemExit(
            f"Database not found at {DATABASE_PATH}. "
            "Run `python db/seed_data.py` first."
        )

    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        with connection:
            created: list[dict[str, Any]] = []
            created.extend(detect_payment_failures(connection))
            created.extend(detect_checkout_abandonment(connection))
            created.extend(detect_subscription_failures(connection))
            created.extend(detect_overdue_invoices(connection))
        print_summary(connection, created)
    finally:
        connection.close()


if __name__ == "__main__":
    main()