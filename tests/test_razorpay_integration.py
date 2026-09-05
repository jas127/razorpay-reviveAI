"""Tests for optional Razorpay Test Mode Payment Links integration."""

import os
import sqlite3
from unittest.mock import patch, MagicMock
from executor.razorpay_client import create_real_payment_link
from executor.action_executor import execute_action


def test_razorpay_client_fallback_on_missing_keys():
    with patch.dict(os.environ, {"RAZORPAY_KEY_ID": "", "RAZORPAY_KEY_SECRET": ""}):
        result = create_real_payment_link(risk_id="risk_test_1", amount_at_risk=500.0)
        assert result is None


def test_razorpay_client_success_mock():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "id": "plink_test_123",
        "short_url": "https://rzp.io/i/mockLink123",
        "status": "created",
    }

    with patch.dict(os.environ, {"RAZORPAY_KEY_ID": "rzp_test_key", "RAZORPAY_KEY_SECRET": "rzp_test_sec"}), \
         patch("requests.post", return_value=mock_resp) as mock_post:
        result = create_real_payment_link(
            risk_id="risk_test_1",
            amount_at_risk=1500.0,
            customer_name="Aarav Sharma",
            customer_email="aarav@example.com",
        )
        assert result == "https://rzp.io/i/mockLink123"
        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        assert args[0] == "https://api.razorpay.com/v1/payment_links"
        assert kwargs["auth"] == ("rzp_test_key", "rzp_test_sec")
        assert kwargs["json"]["amount"] == 150000  # in paise


def test_executor_with_use_real_razorpay_links_enabled():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE customers (
            customer_id TEXT PRIMARY KEY, name TEXT, email TEXT, phone TEXT,
            preferred_language TEXT, contact_count_last_7d INTEGER DEFAULT 0,
            opted_out INTEGER DEFAULT 0, created_at TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE risk_events (
            risk_id TEXT PRIMARY KEY, event_id TEXT, customer_id TEXT,
            risk_category TEXT, risk_score REAL, amount_at_risk REAL,
            detected_at TIMESTAMP, status TEXT, lock_token TEXT,
            locked_at TIMESTAMP, force_escalate INTEGER DEFAULT 0,
            promise_to_pay_date TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE diagnoses (
            diagnosis_id TEXT PRIMARY KEY, risk_id TEXT, root_cause TEXT,
            recommended_action TEXT, recommended_channel TEXT,
            recommended_discount_pct REAL, confidence REAL,
            llm_raw_response TEXT, diagnosed_at TIMESTAMP,
            provider_used TEXT, customer_message_hinglish TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE policy_decisions (
            decision_id TEXT PRIMARY KEY, risk_id TEXT, diagnosis_id TEXT,
            decision TEXT, final_action TEXT, final_discount_pct REAL,
            rules_applied TEXT, reason TEXT, decided_at TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE actions_taken (
            action_id TEXT PRIMARY KEY, decision_id TEXT UNIQUE, risk_id TEXT,
            action_type TEXT, channel TEXT, message_sent TEXT,
            executed_at TIMESTAMP, simulated INTEGER DEFAULT 1
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE audit_log (
            log_id TEXT PRIMARY KEY, risk_id TEXT, stage TEXT, actor TEXT,
            input_snapshot TEXT, output_snapshot TEXT, timestamp TIMESTAMP
        )
        """
    )

    conn.execute(
        "INSERT INTO customers (customer_id, name, email) VALUES ('cust_1', 'Rohan Gupta', 'rohan@example.com')"
    )
    conn.execute(
        "INSERT INTO risk_events (risk_id, customer_id, amount_at_risk, status) VALUES ('risk_1', 'cust_1', 2500.0, 'diagnosed')"
    )
    conn.execute(
        """
        INSERT INTO policy_decisions (decision_id, risk_id, decision, final_action, final_discount_pct, reason)
        VALUES ('dec_1', 'risk_1', 'auto_approved', 'send_payment_link', 0, 'Auto-approved')
        """
    )
    conn.commit()

    # When USE_REAL_RAZORPAY_LINKS is true and mock returns real url
    with patch.dict(os.environ, {"USE_REAL_RAZORPAY_LINKS": "true"}), \
         patch("executor.razorpay_client.create_real_payment_link", return_value="https://rzp.io/i/realRazorpayLink"):
        res = execute_action(conn, decision_id="dec_1")
        assert res["status"] == "executed"
        assert res["simulated"] is False

        act = conn.execute("SELECT * FROM actions_taken WHERE decision_id = 'dec_1'").fetchone()
        assert act["simulated"] == 0
        assert act["message_sent"] == "https://rzp.io/i/realRazorpayLink"


def test_executor_with_use_real_razorpay_links_disabled():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE customers (
            customer_id TEXT PRIMARY KEY, name TEXT, email TEXT, phone TEXT,
            preferred_language TEXT, contact_count_last_7d INTEGER DEFAULT 0,
            opted_out INTEGER DEFAULT 0, created_at TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE risk_events (
            risk_id TEXT PRIMARY KEY, event_id TEXT, customer_id TEXT,
            risk_category TEXT, risk_score REAL, amount_at_risk REAL,
            detected_at TIMESTAMP, status TEXT, lock_token TEXT,
            locked_at TIMESTAMP, force_escalate INTEGER DEFAULT 0,
            promise_to_pay_date TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE diagnoses (
            diagnosis_id TEXT PRIMARY KEY, risk_id TEXT, root_cause TEXT,
            recommended_action TEXT, recommended_channel TEXT,
            recommended_discount_pct REAL, confidence REAL,
            llm_raw_response TEXT, diagnosed_at TIMESTAMP,
            provider_used TEXT, customer_message_hinglish TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE policy_decisions (
            decision_id TEXT PRIMARY KEY, risk_id TEXT, diagnosis_id TEXT,
            decision TEXT, final_action TEXT, final_discount_pct REAL,
            rules_applied TEXT, reason TEXT, decided_at TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE actions_taken (
            action_id TEXT PRIMARY KEY, decision_id TEXT UNIQUE, risk_id TEXT,
            action_type TEXT, channel TEXT, message_sent TEXT,
            executed_at TIMESTAMP, simulated INTEGER DEFAULT 1
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE audit_log (
            log_id TEXT PRIMARY KEY, risk_id TEXT, stage TEXT, actor TEXT,
            input_snapshot TEXT, output_snapshot TEXT, timestamp TIMESTAMP
        )
        """
    )

    conn.execute(
        "INSERT INTO customers (customer_id, name, email) VALUES ('cust_2', 'Priya Patel', 'priya@example.com')"
    )
    conn.execute(
        "INSERT INTO risk_events (risk_id, customer_id, amount_at_risk, status) VALUES ('risk_2', 'cust_2', 3000.0, 'diagnosed')"
    )
    conn.execute(
        """
        INSERT INTO policy_decisions (decision_id, risk_id, decision, final_action, final_discount_pct, reason)
        VALUES ('dec_2', 'risk_2', 'auto_approved', 'send_payment_link', 0, 'Auto-approved')
        """
    )
    conn.commit()

    # When USE_REAL_RAZORPAY_LINKS is false
    with patch.dict(os.environ, {"USE_REAL_RAZORPAY_LINKS": "false"}):
        res = execute_action(conn, decision_id="dec_2")
        assert res["status"] == "executed"
        assert res["simulated"] is True

        act = conn.execute("SELECT * FROM actions_taken WHERE decision_id = 'dec_2'").fetchone()
        assert act["simulated"] == 1
        assert "Hi Priya Patel" in act["message_sent"]
        assert "₹3,000" in act["message_sent"]
