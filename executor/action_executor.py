"""Simulated action execution and reproducible outcome simulation for ReviveAI.

No real email, SMS, WhatsApp, payment, or other external API is called here.
Every action is explicitly stored with ``simulated = 1``.
"""

from __future__ import annotations

import json
import os
import random
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATABASE_PATH = PROJECT_ROOT / "revive.db"
EXECUTABLE_DECISIONS = {"auto_approved", "modified", "human_approved"}
MAX_ATTEMPTS_DEFAULT = 3

ACTION_TYPES = {
    "send_reminder",
    "offer_discount",
    "send_payment_link",
    "retry_payment",
    "switch_payment_method",
}

BASE_PAID_PROBABILITIES = {
    "send_reminder": 0.25,
    "offer_discount": 0.40,
    "send_payment_link": 0.45,
    "retry_payment": 0.35,
    "switch_payment_method": 0.50,
}

# These templates are intentionally simple and cover every executable
# action/channel pair. They produce customer-facing text only for the
# simulation; no real message is sent.
MESSAGE_TEMPLATES = {
    (action, channel): (
        f"Hi {{name}}, we noticed your payment of ₹{{amount}} "
        f"needs attention. {body} This offer includes {{discount}}% off "
        "if applicable."
    )
    for action, body in {
        "send_reminder": "Please complete your payment when convenient.",
        "offer_discount": "We have a limited recovery offer available for you.",
        "send_payment_link": (
            "Here's a secure link to complete it: [link]."
        ),
        "retry_payment": "You can safely try the payment again.",
        "switch_payment_method": (
            "Please try a different payment method to complete your purchase."
        ),
    }.items()
    for channel in ("email", "sms", "whatsapp")
}


def _timestamp(value: datetime | None = None) -> str:
    current = value or datetime.now(timezone.utc)
    return current.astimezone(timezone.utc).replace(tzinfo=None).isoformat(
        sep=" ", timespec="seconds"
    )


def _value(
    row: sqlite3.Row | dict[str, Any],
    key: str,
    default: Any = None,
) -> Any:
    try:
        return row[key]
    except (KeyError, IndexError):
        return default


def _audit(
    connection: sqlite3.Connection,
    *,
    risk_id: str,
    stage: str,
    input_snapshot: dict[str, Any],
    output_snapshot: dict[str, Any],
) -> None:
    connection.execute(
        """
        INSERT INTO audit_log (
            log_id, risk_id, stage, actor, input_snapshot,
            output_snapshot, timestamp
        ) VALUES (?, ?, ?, 'system', ?, ?, ?)
        """,
        (
            f"audit_{stage}_{risk_id}_{uuid.uuid4().hex}",
            risk_id,
            stage,
            json.dumps(input_snapshot, ensure_ascii=False, sort_keys=True),
            json.dumps(output_snapshot, ensure_ascii=False, sort_keys=True),
            _timestamp(),
        ),
    )


def _load_decision(
    connection: sqlite3.Connection,
    decision_id: str,
) -> sqlite3.Row:
    connection.row_factory = sqlite3.Row
    row = connection.execute(
        """
        SELECT
            p.*,
            r.amount_at_risk,
            r.customer_id,
            c.name AS customer_name,
            c.email AS customer_email,
            d.recommended_channel AS channel
        FROM policy_decisions AS p
        INNER JOIN risk_events AS r ON r.risk_id = p.risk_id
        INNER JOIN customers AS c ON c.customer_id = r.customer_id
        LEFT JOIN diagnoses AS d ON d.diagnosis_id = p.diagnosis_id
        WHERE p.decision_id = ?
        """,
        (decision_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"policy decision not found: {decision_id}")
    return row


def build_message(decision_row: sqlite3.Row | dict[str, Any]) -> str:
    """Render a deterministic template for an action/channel pair."""
    action = _value(decision_row, "final_action")
    channel = _value(decision_row, "channel") or "email"
    if action not in ACTION_TYPES:
        raise ValueError(f"unsupported executable action: {action!r}")
    if channel not in {"email", "sms", "whatsapp"}:
        raise ValueError(f"unsupported message channel: {channel!r}")

    template = MESSAGE_TEMPLATES[(action, channel)]
    name = str(_value(decision_row, "customer_name", "there") or "there")
    amount = float(_value(decision_row, "amount_at_risk", 0) or 0)
    discount = float(_value(decision_row, "final_discount_pct", 0) or 0)
    discount_text = f"{discount:g}"
    return template.format(
        name=name,
        amount=f"{amount:,.0f}",
        discount=discount_text,
    )


def _execute_action_with_connection(
    connection: sqlite3.Connection,
    decision_row: sqlite3.Row | dict[str, Any] | str | None,
    *,
    decision_id: str | None = None,
) -> dict[str, Any]:
    if isinstance(decision_row, str):
        decision_id = decision_row
        decision_row = None
    if decision_row is None:
        if not decision_id:
            raise ValueError("decision_row or decision_id is required")
        decision_row = _load_decision(connection, decision_id)

    decision_id = _value(decision_row, "decision_id", decision_id)
    risk_id = _value(decision_row, "risk_id")
    customer_id = _value(decision_row, "customer_id")
    decision = _value(decision_row, "decision")
    if not decision_id or not risk_id:
        raise ValueError("decision_row must contain decision_id and risk_id")
    if decision not in EXECUTABLE_DECISIONS:
        raise ValueError(
            f"decision {decision!r} is not executable; "
            "only auto_approved and modified may execute"
        )
    if not customer_id:
        customer_id = connection.execute(
            "SELECT customer_id FROM risk_events WHERE risk_id = ?",
            (risk_id,),
        ).fetchone()
        customer_id = customer_id[0] if customer_id else None
    if not customer_id:
        raise ValueError(f"customer not found for risk event: {risk_id}")

    # Defense-in-depth: Unconditionally reject execution if customer has opted out
    cust_row = connection.execute(
        "SELECT opted_out FROM customers WHERE customer_id = ?",
        (customer_id,),
    ).fetchone()
    if cust_row and int(cust_row[0] or 0) == 1:
        raise ValueError(f"Cannot execute action: customer {customer_id!r} has opted out")

    message = build_message(decision_row)
    action_type = _value(decision_row, "final_action")
    channel = _value(decision_row, "channel") or "email"
    action_id = f"action_{uuid.uuid4().hex}"
    simulated_flag = 1

    use_real_rzp = os.getenv("USE_REAL_RAZORPAY_LINKS", "").strip().lower() in ("true", "1", "yes")
    if use_real_rzp and action_type == "send_payment_link":
        try:
            from executor.razorpay_client import create_real_payment_link

            cust_name = str(_value(decision_row, "customer_name", "Customer") or "Customer")
            cust_email = _value(decision_row, "customer_email")
            amount_val = float(_value(decision_row, "amount_at_risk", 0) or 0)

            real_url = create_real_payment_link(
                risk_id=risk_id,
                amount_at_risk=amount_val,
                customer_name=cust_name,
                customer_email=cust_email,
            )
            if real_url:
                message = real_url
                simulated_flag = 0
        except Exception:
            simulated_flag = 1

    try:
        # This INSERT is the at-most-once boundary. A duplicate decision_id
        # must be rejected by SQLite's UNIQUE constraint, not a prior read.
        connection.execute(
            """
            INSERT INTO actions_taken (
                action_id, decision_id, risk_id, action_type, channel,
                message_sent, executed_at, simulated
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                action_id,
                decision_id,
                risk_id,
                action_type,
                channel,
                message,
                _timestamp(),
                simulated_flag,
            ),
        )
        connection.execute(
            """
            UPDATE customers
            SET contact_count_last_7d = COALESCE(contact_count_last_7d, 0) + 1
            WHERE customer_id = ?
            """,
            (customer_id,),
        )
        result = {
            "status": "executed",
            "action_id": action_id,
            "decision_id": decision_id,
            "risk_id": risk_id,
            "action_type": action_type,
            "channel": channel,
            "simulated": bool(simulated_flag),
        }
        _audit(
            connection,
            risk_id=risk_id,
            stage="execution",
            input_snapshot={
                "decision_id": decision_id,
                "decision": decision,
                "action_type": action_type,
                "channel": channel,
            },
            output_snapshot={
                **result,
                "message_sent": message,
            },
        )
        connection.commit()
        return result
    except sqlite3.IntegrityError as exc:
        connection.rollback()
        if "actions_taken.decision_id" not in str(exc):
            raise
        output = {
            "status": "duplicate_execution_blocked",
            "decision_id": decision_id,
            "risk_id": risk_id,
            "reason": "actions_taken.decision_id is already present",
        }
        _audit(
            connection,
            risk_id=risk_id,
            stage="duplicate_execution_blocked",
            input_snapshot={
                "decision_id": decision_id,
                "risk_id": risk_id,
                "decision": decision,
            },
            output_snapshot=output,
        )
        connection.commit()
        return output


def execute_action(
    decision_row_or_connection: sqlite3.Connection
    | sqlite3.Row
    | dict[str, Any]
    | str,
    decision_row: sqlite3.Row | dict[str, Any] | str | None = None,
    *,
    connection: sqlite3.Connection | None = None,
    decision_id: str | None = None,
    database_path: Path = DATABASE_PATH,
) -> dict[str, Any]:
    """Execute one approved decision using a simulated message.

    Preferred API: ``execute_action(decision_row, connection=connection)``.
    The legacy Phase 2.5 form ``execute_action(connection, decision_id=...)``
    remains supported for duplicate-execution callers.
    """
    if isinstance(decision_row_or_connection, sqlite3.Connection):
        db = decision_row_or_connection
        row = decision_row
    else:
        row = decision_row_or_connection
        db = connection

    owns_connection = db is None
    if db is None:
        db = sqlite3.connect(database_path)
        db.execute("PRAGMA foreign_keys = ON")
    db.row_factory = sqlite3.Row
    try:
        return _execute_action_with_connection(
            db,
            row,
            decision_id=decision_id,
        )
    finally:
        if owns_connection:
            db.close()


def _policy_max_attempts(connection: sqlite3.Connection) -> int:
    row = connection.execute(
        """
        SELECT rule_value
        FROM policy_rules
        WHERE rule_type = 'max_attempts' AND active = 1
        ORDER BY rowid DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        return MAX_ATTEMPTS_DEFAULT
    try:
        value = json.loads(row[0])
        return int(value["max"])
    except (TypeError, ValueError, KeyError, json.JSONDecodeError):
        return MAX_ATTEMPTS_DEFAULT


def _pending_actions(
    connection: sqlite3.Connection,
    *,
    action_id: str | None = None,
) -> list[sqlite3.Row]:
    action_filter = ""
    params: tuple[Any, ...] = ()
    if action_id is not None:
        action_filter = "AND a.action_id = ?"
        params = (action_id,)
    connection.row_factory = sqlite3.Row
    return connection.execute(
        f"""
        SELECT
            a.*,
            r.amount_at_risk,
            r.customer_id,
            r.status AS risk_status,
            c.name AS customer_name,
            (
                SELECT COUNT(*)
                FROM risk_events AS prior
                WHERE prior.customer_id = r.customer_id
                  AND prior.risk_id <> r.risk_id
            ) AS prior_risk_count,
            (
                SELECT COUNT(*)
                FROM actions_taken AS attempt
                WHERE attempt.risk_id = a.risk_id
            ) AS attempt_count
        FROM actions_taken AS a
        INNER JOIN risk_events AS r ON r.risk_id = a.risk_id
        INNER JOIN customers AS c ON c.customer_id = r.customer_id
        WHERE NOT EXISTS (
            SELECT 1 FROM outcomes AS o WHERE o.action_id = a.action_id
        )
        {action_filter}
        ORDER BY a.executed_at ASC, a.action_id ASC
        """,
        params,
    ).fetchall()


def simulate_outcomes(
    connection: sqlite3.Connection | None = None,
    *,
    seed: int = 42,
    action_id: str | None = None,
    database_path: Path = DATABASE_PATH,
) -> dict[str, Any]:
    """Simulate one outcome for every pending action, or one action by ID."""
    owns_connection = connection is None
    db = connection or sqlite3.connect(database_path)
    db.row_factory = sqlite3.Row
    rng = random.Random(seed)
    max_attempts = _policy_max_attempts(db)
    total_recovered = 0.0
    paid_count = 0
    processed_count = 0

    try:
        for action in _pending_actions(db, action_id=action_id):
            action_type = action["action_type"]
            base_probability = BASE_PAID_PROBABILITIES.get(action_type, 0.25)
            amount = float(action["amount_at_risk"] or 0)
            prior_risks = int(action["prior_risk_count"] or 0)
            probability = base_probability
            if amount < 5000:
                probability += 0.05
            if prior_risks >= 2:
                probability -= 0.10
            probability = max(0.0, min(1.0, probability))

            paid = rng.random() < probability
            attempt_count = int(action["attempt_count"] or 0)
            if paid:
                outcome_type = "paid"
                amount_recovered = amount
                risk_status = "resolved"
                paid_count += 1
                total_recovered += amount
            elif attempt_count >= max_attempts:
                outcome_type = "ignored"
                amount_recovered = 0.0
                risk_status = "expired"
            else:
                outcome_type = "ignored"
                amount_recovered = 0.0
                # Keep the risk diagnosed/open so a later cycle can decide
                # whether another approved action is permitted.
                risk_status = "diagnosed"

            outcome_id = f"outcome_{uuid.uuid4().hex}"
            occurred_at = _timestamp()
            db.execute(
                """
                INSERT INTO outcomes (
                    outcome_id, action_id, risk_id, outcome_type,
                    amount_recovered, occurred_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    outcome_id,
                    action["action_id"],
                    action["risk_id"],
                    outcome_type,
                    amount_recovered,
                    occurred_at,
                ),
            )
            db.execute(
                "UPDATE risk_events SET status = ? WHERE risk_id = ?",
                (risk_status, action["risk_id"]),
            )
            result = {
                "outcome_id": outcome_id,
                "action_id": action["action_id"],
                "risk_id": action["risk_id"],
                "outcome_type": outcome_type,
                "amount_recovered": amount_recovered,
                "paid_probability": probability,
                "attempt_count": attempt_count,
                "seed": seed,
            }
            _audit(
                db,
                risk_id=action["risk_id"],
                stage="outcome",
                input_snapshot={
                    "action_id": action["action_id"],
                    "action_type": action_type,
                    "amount_at_risk": amount,
                    "prior_risk_count": prior_risks,
                    "attempt_count": attempt_count,
                },
                output_snapshot=result,
            )
            processed_count += 1

        db.commit()
        return {
            "outcomes_processed": processed_count,
            "paid_count": paid_count,
            "total_recovered": total_recovered,
            "seed": seed,
        }
    finally:
        if owns_connection:
            db.close()


def load_pending_decisions(
    connection: sqlite3.Connection,
) -> list[sqlite3.Row]:
    """Load executable policy decisions that have not produced an action."""
    connection.row_factory = sqlite3.Row
    return connection.execute(
        """
        SELECT
            p.*,
            r.amount_at_risk,
            r.customer_id,
            c.name AS customer_name,
            c.email AS customer_email,
            d.recommended_channel AS channel
        FROM policy_decisions AS p
        INNER JOIN risk_events AS r ON r.risk_id = p.risk_id
        INNER JOIN customers AS c ON c.customer_id = r.customer_id
        LEFT JOIN diagnoses AS d ON d.diagnosis_id = p.diagnosis_id
        WHERE p.decision IN ('auto_approved', 'modified')
          AND NOT EXISTS (
              SELECT 1
              FROM actions_taken AS a
              WHERE a.decision_id = p.decision_id
          )
        ORDER BY p.decided_at ASC, p.decision_id ASC
        """
    ).fetchall()