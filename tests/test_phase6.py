"""Tests for Phase 6 features: Hinglish messages and Promise-to-Pay tracker."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone, date
from pathlib import Path

from db.init_db import initialize_database
from diagnosis.diagnosis_agent import (
    build_prompt,
    diagnose_risk_event,
)
from diagnosis.base_provider import LLMProvider


class MockHinglishProvider(LLMProvider):
    name = "mock_groq"

    def generate_diagnosis(self, prompt: str) -> str:
        return json.dumps({
            "root_cause": "Card declined due to temporary gateway timeout",
            "recommended_action": "send_payment_link",
            "recommended_channel": "whatsapp",
            "recommended_discount_pct": 5,
            "confidence": 0.88,
            "customer_message": "Hi Rahul Sharma, aapka ₹1,499 ka payment complete nahi hua tha. Is link par click karke 5% discount ke sath pay karein.",
        })


def test_hinglish_prompt_and_storage(tmp_path: Path) -> None:
    db_path = tmp_path / "hinglish.db"
    initialize_database(db_path, reset=True)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            """
            INSERT INTO customers (customer_id, name, email, preferred_language, created_at)
            VALUES ('cust_hi', 'Rahul Sharma', 'rahul@example.in', 'hi-en', datetime('now'))
            """
        )
        conn.execute(
            """
            INSERT INTO revenue_events (event_id, customer_id, event_type, amount, payment_method, occurred_at, raw_metadata)
            VALUES ('event_hi', 'cust_hi', 'payment_failed', 1499, 'card', datetime('now'), '{}')
            """
        )
        conn.execute(
            """
            INSERT INTO risk_events (risk_id, event_id, customer_id, risk_category, risk_score, amount_at_risk, detected_at, status)
            VALUES ('risk_hi', 'event_hi', 'cust_hi', 'payment_degradation', 0.8, 1499, datetime('now'), 'new')
            """
        )
        conn.commit()

        row = conn.execute(
            """
            SELECT r.*, re.event_type, re.failure_code, re.occurred_at as event_occurred_at,
                   re.payment_method, c.name as customer_name, c.email as customer_email,
                   c.preferred_language as customer_preferred_language,
                   c.contact_count_last_7d as customer_contact_count_last_7d,
                   c.opted_out as customer_opted_out
            FROM risk_events r
            JOIN revenue_events re ON re.event_id = r.event_id
            JOIN customers c ON c.customer_id = r.customer_id
            WHERE r.risk_id = 'risk_hi'
            """
        ).fetchone()

        # 1. Verify prompt contains Hinglish instruction
        prompt = build_prompt(row)
        assert "hi-en" in prompt
        assert "Hinglish" in prompt
        assert "customer_message" in prompt

        # 2. Verify diagnosis parse and store
        res = diagnose_risk_event(row, conn, provider_chain=[MockHinglishProvider()])
        assert res["customer_message"] is not None
        assert "aapka" in res["customer_message"]

        # Check DB column
        db_row = conn.execute(
            "SELECT customer_message_hinglish FROM diagnoses WHERE risk_id = 'risk_hi'"
        ).fetchone()
        assert "aapka" in db_row["customer_message_hinglish"]


def test_promise_to_pay_logging_and_escalation(tmp_path: Path) -> None:
    db_path = tmp_path / "ptp.db"
    initialize_database(db_path, reset=True)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            """
            INSERT INTO customers (customer_id, name, email, created_at)
            VALUES ('cust_ptp', 'Aarav Gupta', 'aarav@example.in', datetime('now'))
            """
        )
        conn.execute(
            """
            INSERT INTO revenue_events (event_id, customer_id, event_type, amount, payment_method, occurred_at, raw_metadata)
            VALUES ('event_ptp', 'cust_ptp', 'payment_failed', 5000, 'card', datetime('now'), '{}')
            """
        )
        past_date = (date.today() - timedelta(days=2)).isoformat()
        conn.execute(
            """
            INSERT INTO risk_events (risk_id, event_id, customer_id, risk_category, risk_score, amount_at_risk, detected_at, status, promise_to_pay_date)
            VALUES ('risk_ptp', 'event_ptp', 'cust_ptp', 'payment_degradation', 0.6, 5000, datetime('now'), 'diagnosed', ?)
            """,
            (past_date,),
        )
        conn.commit()

        # Check broken promise logic
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        broken_rows = conn.execute(
            """
            SELECT * FROM risk_events
            WHERE promise_to_pay_date IS NOT NULL
              AND date(promise_to_pay_date) < date(?)
              AND status NOT IN ('resolved', 'expired')
            """,
            (today_str,),
        ).fetchall()
        assert len(broken_rows) == 1

        # Escalate broken promise
        conn.execute(
            """
            UPDATE risk_events
            SET force_escalate = 1, status = 'diagnosed', risk_score = min(1.0, risk_score * 1.25 + 0.15)
            WHERE risk_id = 'risk_ptp'
            """
        )
        conn.execute(
            """
            INSERT INTO audit_log (log_id, risk_id, stage, actor, input_snapshot, output_snapshot, timestamp)
            VALUES ('audit_broken_ptp', 'risk_ptp', 'broken_promise_escalated', 'system:ptp_checker', '{}', '{}', datetime('now'))
            """
        )
        conn.commit()

        updated = conn.execute(
            "SELECT force_escalate, risk_score FROM risk_events WHERE risk_id = 'risk_ptp'"
        ).fetchone()
        assert updated["force_escalate"] == 1
        assert updated["risk_score"] > 0.6

        audit = conn.execute(
            "SELECT stage FROM audit_log WHERE risk_id = 'risk_ptp' AND stage = 'broken_promise_escalated'"
        ).fetchone()
        assert audit["stage"] == "broken_promise_escalated"
