"""Tests for simulated action execution and deterministic outcomes."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from db.init_db import initialize_database
from executor.action_executor import (
    execute_action,
    load_pending_decisions,
    simulate_outcomes,
)


def _seed_case(
    tmp_path: Path,
    name: str,
    *,
    action_type: str = "send_payment_link",
    channel: str = "email",
    amount: float = 1499,
) -> sqlite3.Connection:
    database_path = tmp_path / f"{name}.db"
    initialize_database(database_path, reset=True)
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    connection.execute(
        """
        INSERT INTO customers (
            customer_id, name, email, contact_count_last_7d, created_at
        ) VALUES ('cust_executor', 'Asha', 'asha@example.in', 0, datetime('now'))
        """
    )
    connection.execute(
        """
        INSERT INTO revenue_events (
            event_id, customer_id, event_type, amount, occurred_at, raw_metadata
        ) VALUES ('event_executor', 'cust_executor', 'payment_failed', ?,
                  datetime('now'), '{}')
        """,
        (amount,),
    )
    connection.execute(
        """
        INSERT INTO risk_events (
            risk_id, event_id, customer_id, risk_category, risk_score,
            amount_at_risk, detected_at, status
        ) VALUES ('risk_executor', 'event_executor', 'cust_executor',
                  'payment_degradation', 0.8, ?, datetime('now'), 'diagnosed')
        """,
        (amount,),
    )
    connection.execute(
        """
        INSERT INTO diagnoses (
            diagnosis_id, risk_id, root_cause, recommended_action,
            recommended_channel, recommended_discount_pct, confidence,
            diagnosed_at, provider_used
        ) VALUES ('diagnosis_executor', 'risk_executor', 'test cause', ?,
                  ?, 2, 0.9, datetime('now'), 'fallback_default')
        """,
        (action_type, channel),
    )
    connection.execute(
        """
        INSERT INTO policy_decisions (
            decision_id, risk_id, diagnosis_id, decision, final_action,
            final_discount_pct, rules_applied, reason, decided_at
        ) VALUES ('decision_executor', 'risk_executor', 'diagnosis_executor',
                  'auto_approved', ?, 2, '[]', 'test decision', datetime('now'))
        """,
        (action_type,),
    )
    connection.commit()
    return connection


def test_execute_action_renders_message_and_increments_contact_count(
    tmp_path: Path,
) -> None:
    connection = _seed_case(tmp_path, "message")
    decision = load_pending_decisions(connection)[0]

    result = execute_action(decision, connection=connection)
    action = connection.execute(
        "SELECT * FROM actions_taken WHERE action_id = ?",
        (result["action_id"],),
    ).fetchone()
    contact_count = connection.execute(
        "SELECT contact_count_last_7d FROM customers WHERE customer_id = 'cust_executor'"
    ).fetchone()[0]
    audit = connection.execute(
        """
        SELECT actor, stage FROM audit_log
        WHERE risk_id = 'risk_executor' AND stage = 'execution'
        """
    ).fetchone()

    assert result["status"] == "executed"
    assert result["simulated"] is True
    assert "Hi Asha" in action["message_sent"]
    assert "₹1,499" in action["message_sent"]
    assert "[link]" in action["message_sent"]
    assert "2% off" in action["message_sent"]
    assert action["simulated"] == 1
    assert contact_count == 1
    assert tuple(audit) == ("system", "execution")
    connection.close()
    print("PASS test_execute_action_renders_message_and_increments_contact_count")


def test_simulate_paid_outcome_resolves_risk(tmp_path: Path) -> None:
    connection = _seed_case(tmp_path, "paid")
    decision = load_pending_decisions(connection)[0]
    executed = execute_action(decision, connection=connection)

    summary = simulate_outcomes(connection, seed=1)
    outcome = connection.execute(
        "SELECT outcome_type, amount_recovered FROM outcomes WHERE action_id = ?",
        (executed["action_id"],),
    ).fetchone()
    status = connection.execute(
        "SELECT status FROM risk_events WHERE risk_id = 'risk_executor'"
    ).fetchone()[0]

    assert summary["paid_count"] == 1
    assert tuple(outcome) == ("paid", 1499)
    assert status == "resolved"
    connection.close()
    print("PASS test_simulate_paid_outcome_resolves_risk")


def test_simulate_ignored_outcome_leaves_risk_open(tmp_path: Path) -> None:
    connection = _seed_case(
        tmp_path,
        "ignored",
        action_type="send_reminder",
        amount=9000,
    )
    decision = load_pending_decisions(connection)[0]
    executed = execute_action(decision, connection=connection)

    simulate_outcomes(connection, seed=42)
    outcome = connection.execute(
        "SELECT outcome_type, amount_recovered FROM outcomes WHERE action_id = ?",
        (executed["action_id"],),
    ).fetchone()
    status = connection.execute(
        "SELECT status FROM risk_events WHERE risk_id = 'risk_executor'"
    ).fetchone()[0]

    assert tuple(outcome) == ("ignored", 0)
    assert status == "diagnosed"
    connection.close()
    print("PASS test_simulate_ignored_outcome_leaves_risk_open")


def test_max_attempt_outcome_expires_risk(tmp_path: Path) -> None:
    connection = _seed_case(tmp_path, "max_attempts", action_type="send_reminder")
    old_time = "2026-08-20 10:00:00"
    for index in range(2):
        decision_id = f"decision_prior_{index}"
        connection.execute(
            """
            INSERT INTO policy_decisions (
                decision_id, risk_id, diagnosis_id, decision, final_action,
                final_discount_pct, rules_applied, reason, decided_at
            ) VALUES (?, 'risk_executor', 'diagnosis_executor',
                      'auto_approved', 'send_reminder', 0, '[]', 'prior', ?)
            """,
            (decision_id, old_time),
        )
        connection.execute(
            """
            INSERT INTO actions_taken (
                action_id, decision_id, risk_id, action_type, channel,
                message_sent, executed_at, simulated
            ) VALUES (?, ?, 'risk_executor', 'send_reminder', 'email',
                      'prior', ?, 1)
            """,
            (f"action_prior_{index}", decision_id, old_time),
        )
        connection.execute(
            """
            INSERT INTO outcomes (
                outcome_id, action_id, risk_id, outcome_type,
                amount_recovered, occurred_at
            ) VALUES (?, ?, 'risk_executor', 'ignored', 0, ?)
            """,
            (f"outcome_prior_{index}", f"action_prior_{index}", old_time),
        )
    connection.commit()

    decision = load_pending_decisions(connection)[0]
    executed = execute_action(decision, connection=connection)
    simulate_outcomes(connection, seed=42)

    outcome = connection.execute(
        "SELECT outcome_type FROM outcomes WHERE action_id = ?",
        (executed["action_id"],),
    ).fetchone()[0]
    status = connection.execute(
        "SELECT status FROM risk_events WHERE risk_id = 'risk_executor'"
    ).fetchone()[0]

    assert outcome == "ignored"
    assert status == "expired"
    connection.close()
    print("PASS test_max_attempt_outcome_expires_risk")