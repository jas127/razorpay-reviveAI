"""Comprehensive regression tests for opt-out enforcement across Policy Engine & Executor."""

import sqlite3
import pytest
from policy.policy_engine import evaluate_policy
from executor.action_executor import execute_action, load_pending_decisions


def create_test_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE customers (
            customer_id TEXT PRIMARY KEY,
            name TEXT,
            email TEXT,
            phone TEXT,
            preferred_language TEXT DEFAULT 'en',
            contact_count_last_7d INTEGER DEFAULT 0,
            opted_out INTEGER DEFAULT 0,
            created_at TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE risk_events (
            risk_id TEXT PRIMARY KEY,
            event_id TEXT UNIQUE,
            customer_id TEXT REFERENCES customers(customer_id),
            risk_category TEXT,
            risk_score REAL,
            amount_at_risk REAL,
            detected_at TIMESTAMP,
            status TEXT DEFAULT 'new',
            lock_token TEXT,
            locked_at TIMESTAMP,
            force_escalate INTEGER DEFAULT 0,
            promise_to_pay_date TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE diagnoses (
            diagnosis_id TEXT PRIMARY KEY,
            risk_id TEXT REFERENCES risk_events(risk_id),
            root_cause TEXT,
            recommended_action TEXT,
            recommended_channel TEXT,
            recommended_discount_pct REAL DEFAULT 0,
            confidence REAL,
            llm_raw_response TEXT,
            diagnosed_at TIMESTAMP,
            provider_used TEXT,
            customer_message_hinglish TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE policy_rules (
            rule_id TEXT PRIMARY KEY,
            rule_name TEXT,
            rule_type TEXT,
            rule_value TEXT,
            active INTEGER DEFAULT 1
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE policy_decisions (
            decision_id TEXT PRIMARY KEY,
            risk_id TEXT REFERENCES risk_events(risk_id),
            diagnosis_id TEXT REFERENCES diagnoses(diagnosis_id),
            decision TEXT,
            final_action TEXT,
            final_discount_pct REAL,
            rules_applied TEXT,
            reason TEXT,
            decided_at TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE actions_taken (
            action_id TEXT PRIMARY KEY,
            decision_id TEXT UNIQUE REFERENCES policy_decisions(decision_id),
            risk_id TEXT REFERENCES risk_events(risk_id),
            action_type TEXT,
            channel TEXT,
            message_sent TEXT,
            executed_at TIMESTAMP,
            simulated INTEGER DEFAULT 1
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE audit_log (
            log_id TEXT PRIMARY KEY,
            risk_id TEXT,
            stage TEXT,
            actor TEXT,
            input_snapshot TEXT,
            output_snapshot TEXT,
            timestamp TIMESTAMP
        )
        """
    )
    return conn


def test_opt_out_policy_blocks_unconditionally():
    conn = create_test_db()
    # Insert opted-out customer
    conn.execute(
        """
        INSERT INTO customers (customer_id, name, email, opted_out, contact_count_last_7d)
        VALUES ('cust_optout_01', 'Arjun Verma', 'arjun@example.com', 1, 0)
        """
    )
    # Insert high-confidence risk event for this customer
    conn.execute(
        """
        INSERT INTO risk_events (risk_id, customer_id, risk_category, risk_score, amount_at_risk, status, force_escalate)
        VALUES ('risk_optout_01', 'cust_optout_01', 'payment_degradation', 0.95, 5000.0, 'diagnosed', 0)
        """
    )
    # Insert aggressive discount recommendation with 1.0 confidence
    conn.execute(
        """
        INSERT INTO diagnoses (
            diagnosis_id, risk_id, root_cause, recommended_action, recommended_channel,
            recommended_discount_pct, confidence, provider_used
        ) VALUES (
            'diag_optout_01', 'risk_optout_01', 'Temporary gateway glitch', 'send_payment_link',
            'whatsapp', 10.0, 1.0, 'groq'
        )
        """
    )
    conn.commit()

    risk_row = conn.execute("SELECT * FROM risk_events WHERE risk_id = 'risk_optout_01'").fetchone()
    diag_row = conn.execute("SELECT * FROM diagnoses WHERE diagnosis_id = 'diag_optout_01'").fetchone()

    # 1. Run Policy Engine
    result = evaluate_policy(risk_row, diag_row, connection=conn)

    # 2. Assert decision is blocked and reason is customer opted out
    assert result["decision"] == "blocked"
    assert result["final_action"] == "none"
    assert result["final_discount_pct"] == 0.0
    assert "opt_out_respect" in result["rules_applied"]
    assert result["reason"] == "customer opted out"

    # 3. Assert load_pending_decisions returns zero executable items
    pending = load_pending_decisions(conn)
    assert len(pending) == 0

    # 4. Assert execute_action rejects blocked decision
    decision_row = conn.execute("SELECT * FROM policy_decisions WHERE risk_id = 'risk_optout_01'").fetchone()
    with pytest.raises(ValueError, match="not executable"):
        execute_action(decision_row, connection=conn)

    # 5. Assert 0 rows exist in actions_taken
    actions = conn.execute("SELECT * FROM actions_taken WHERE risk_id = 'risk_optout_01'").fetchall()
    assert len(actions) == 0


def test_executor_defense_in_depth_blocks_opted_out_even_if_forged():
    """Even if an invalid auto_approved decision exists, Executor physically refuses to dispatch to opted_out customer."""
    conn = create_test_db()
    conn.execute(
        """
        INSERT INTO customers (customer_id, name, email, opted_out)
        VALUES ('cust_optout_02', 'Kiran Rao', 'kiran@example.com', 1)
        """
    )
    conn.execute(
        """
        INSERT INTO risk_events (risk_id, customer_id, amount_at_risk, status)
        VALUES ('risk_optout_02', 'cust_optout_02', 12000.0, 'diagnosed')
        """
    )
    # Fabricated auto_approved decision row
    conn.execute(
        """
        INSERT INTO policy_decisions (decision_id, risk_id, decision, final_action, final_discount_pct, reason)
        VALUES ('dec_forged_02', 'risk_optout_02', 'auto_approved', 'send_payment_link', 0.0, 'Forged approval')
        """
    )
    conn.commit()

    decision_row = conn.execute("SELECT * FROM policy_decisions WHERE decision_id = 'dec_forged_02'").fetchone()

    # Executor must catch opted-out flag directly from customer record and abort
    with pytest.raises(ValueError, match="opted out"):
        execute_action(decision_row, connection=conn)

    # Verify zero actions recorded
    actions = conn.execute("SELECT * FROM actions_taken WHERE risk_id = 'risk_optout_02'").fetchall()
    assert len(actions) == 0
