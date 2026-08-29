"""Focused branch tests for the deterministic Policy Engine."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from db.init_db import initialize_database
from policy.policy_engine import evaluate_policy, seed_default_policy_rules


def _case(
    tmp_path: Path,
    name: str,
    *,
    opted_out: int = 0,
    contact_count: int = 0,
    amount_at_risk: float = 1000,
    confidence: float = 0.9,
    recommended_discount_pct: float = 0,
    force_escalate: int = 0,
) -> tuple[sqlite3.Connection, sqlite3.Row, sqlite3.Row]:
    database_path = tmp_path / f"{name}.db"
    initialize_database(database_path, reset=True)
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    connection.execute(
        """
        INSERT INTO customers (
            customer_id, name, contact_count_last_7d, opted_out, created_at
        ) VALUES ('cust_policy', 'Policy Customer', ?, ?, datetime('now'))
        """,
        (contact_count, opted_out),
    )
    connection.execute(
        """
        INSERT INTO revenue_events (
            event_id, customer_id, event_type, amount, occurred_at, raw_metadata
        ) VALUES ('event_policy', 'cust_policy', 'payment_failed', ?, datetime('now'), '{}')
        """,
        (amount_at_risk,),
    )
    connection.execute(
        """
        INSERT INTO risk_events (
            risk_id, event_id, customer_id, risk_category, risk_score,
            amount_at_risk, detected_at, status, force_escalate
        ) VALUES ('risk_policy', 'event_policy', 'cust_policy',
                  'payment_degradation', 0.8, ?, datetime('now'),
                  'diagnosed', ?)
        """,
        (amount_at_risk, force_escalate),
    )
    connection.execute(
        """
        INSERT INTO diagnoses (
            diagnosis_id, risk_id, root_cause, recommended_action,
            recommended_channel, recommended_discount_pct, confidence,
            llm_raw_response, diagnosed_at, provider_used
        ) VALUES ('diagnosis_policy', 'risk_policy', 'test cause',
                  'offer_discount', 'email', ?, ?, '{}', datetime('now'),
                  'fallback_default')
        """,
        (recommended_discount_pct, confidence),
    )
    connection.commit()
    risk_event = connection.execute(
        """
        SELECT r.*, c.opted_out AS customer_opted_out,
               c.contact_count_last_7d AS customer_contact_count_last_7d
        FROM risk_events r
        JOIN customers c ON c.customer_id = r.customer_id
        """
    ).fetchone()
    diagnosis = connection.execute(
        "SELECT * FROM diagnoses WHERE diagnosis_id = 'diagnosis_policy'"
    ).fetchone()
    return connection, risk_event, diagnosis


def test_policy_seeds_defaults_and_stops_at_opt_out(tmp_path: Path) -> None:
    connection, risk_event, diagnosis = _case(
        tmp_path,
        "optout",
        opted_out=1,
        confidence=0.1,
    )
    result = evaluate_policy(risk_event, diagnosis, connection)
    assert result["decision"] == "blocked"
    assert result["reason"] == "customer opted out"
    assert result["rules_applied"] == ["opt_out_respect"]
    assert connection.execute("SELECT COUNT(*) FROM policy_rules").fetchone()[0] == 7
    connection.close()
    print("PASS test_policy_seeds_defaults_and_stops_at_opt_out")


def test_policy_blocks_frequency_cooldown_and_attempts(tmp_path: Path) -> None:
    connection, risk_event, diagnosis = _case(
        tmp_path,
        "frequency",
        contact_count=2,
    )
    result = evaluate_policy(risk_event, diagnosis, connection)
    assert result["decision"] == "blocked"
    assert result["reason"] == "frequency cap reached"
    connection.close()

    connection, risk_event, diagnosis = _case(tmp_path, "cooldown")
    connection.execute(
        """
        INSERT INTO policy_decisions (
            decision_id, risk_id, diagnosis_id, decision, final_action,
            final_discount_pct, rules_applied, reason, decided_at
        ) VALUES ('decision_cooldown', 'risk_policy', 'diagnosis_policy',
                  'auto_approved', 'send_reminder', 0, '[]', 'test', datetime('now'))
        """
    )
    connection.execute(
        """
        INSERT INTO actions_taken (
            action_id, decision_id, risk_id, action_type, channel,
            message_sent, executed_at
        ) VALUES ('action_cooldown', 'decision_cooldown', 'risk_policy',
                  'send_reminder', 'email', 'test', datetime('now'))
        """
    )
    connection.commit()
    result = evaluate_policy(risk_event, diagnosis, connection)
    assert result["decision"] == "blocked"
    assert result["reason"] == "cooldown active"
    connection.close()

    connection, risk_event, diagnosis = _case(tmp_path, "attempts")
    old_time = (
        datetime.now(timezone.utc) - timedelta(days=3)
    ).replace(tzinfo=None).isoformat(sep=" ", timespec="seconds")
    for index in range(3):
        decision_id = f"decision_attempt_{index}"
        connection.execute(
            """
            INSERT INTO policy_decisions (
                decision_id, risk_id, diagnosis_id, decision, final_action,
                final_discount_pct, rules_applied, reason, decided_at
            ) VALUES (?, 'risk_policy', 'diagnosis_policy', 'auto_approved',
                      'send_reminder', 0, '[]', 'test', ?)
            """,
            (decision_id, old_time),
        )
        connection.execute(
            """
            INSERT INTO actions_taken (
                action_id, decision_id, risk_id, action_type, channel,
                message_sent, executed_at
            ) VALUES (?, ?, 'risk_policy', 'send_reminder', 'email', 'test', ?)
            """,
            (f"action_attempt_{index}", decision_id, old_time),
        )
    connection.commit()
    result = evaluate_policy(risk_event, diagnosis, connection)
    assert result["decision"] == "blocked"
    assert result["reason"] == "max attempts reached, marking risk_event as expired"
    assert connection.execute(
        "SELECT status FROM risk_events WHERE risk_id = 'risk_policy'"
    ).fetchone()[0] == "expired"
    connection.close()
    print("PASS test_policy_blocks_frequency_cooldown_and_attempts")


def test_policy_human_thresholds_and_discount_modification(tmp_path: Path) -> None:
    connection, risk_event, diagnosis = _case(
        tmp_path,
        "confidence",
        confidence=0.59,
    )
    result = evaluate_policy(risk_event, diagnosis, connection)
    assert result["decision"] == "needs_human_approval"
    assert result["reason"] == "low LLM confidence"
    connection.close()

    connection, risk_event, diagnosis = _case(
        tmp_path,
        "amount",
        amount_at_risk=10001,
    )
    result = evaluate_policy(risk_event, diagnosis, connection)
    assert result["decision"] == "needs_human_approval"
    assert result["reason"] == "amount exceeds auto-approval threshold"
    connection.close()

    connection, risk_event, diagnosis = _case(
        tmp_path,
        "discount",
        recommended_discount_pct=25,
    )
    result = evaluate_policy(risk_event, diagnosis, connection)
    assert result["decision"] == "modified"
    assert result["final_discount_pct"] == 10
    assert result["rules_applied"] == ["discount_cap"]
    connection.close()
    print("PASS test_policy_human_thresholds_and_discount_modification")


def test_policy_force_escalate_overrides_confidence(tmp_path: Path) -> None:
    connection, risk_event, diagnosis = _case(
        tmp_path,
        "stale_lock",
        confidence=0.99,
        force_escalate=1,
    )
    result = evaluate_policy(risk_event, diagnosis, connection)
    assert result["decision"] == "needs_human_approval"
    assert result["final_action"] == "escalate_to_human"
    assert result["rules_applied"] == ["force_escalate"]
    connection.close()
    print("PASS test_policy_force_escalate_overrides_confidence")