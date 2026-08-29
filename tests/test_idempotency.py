"""Adversarial tests for ReviveAI's database-level idempotency guarantees."""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from db.idempotency import acquire_lock, sweep_stale_locks
from db.init_db import initialize_database
from executor.action_executor import execute_action


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = PROJECT_ROOT / "db" / "schema.sql"


def _seed_risk_database(database_path: Path, *, risk_id: str) -> None:
    initialize_database(database_path, reset=True)
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            """
            INSERT INTO customers (
                customer_id, name, email, phone, created_at
            ) VALUES (?, ?, ?, ?, datetime('now'))
            """,
            ("cust_test", "Test Customer", "test@example.in", "+91 9000000000"),
        )
        connection.execute(
            """
            INSERT INTO revenue_events (
                event_id, customer_id, event_type, amount,
                payment_method, occurred_at, raw_metadata
            ) VALUES (?, ?, ?, ?, ?, datetime('now'), ?)
            """,
            (
                f"event_{risk_id}",
                "cust_test",
                "payment_failed",
                1499,
                "card",
                "{}",
            ),
        )
        connection.execute(
            """
            INSERT INTO risk_events (
                risk_id, event_id, customer_id, risk_category,
                risk_score, amount_at_risk, detected_at, status
            ) VALUES (?, ?, ?, ?, ?, ?, datetime('now'), 'diagnosed')
            """,
            (
                risk_id,
                f"event_{risk_id}",
                "cust_test",
                "payment_degradation",
                0.8,
                1499,
            ),
        )


def test_concurrent_duplicate_trigger(tmp_path: Path) -> None:
    database_path = tmp_path / "concurrent.db"
    risk_id = "risk_concurrent"
    _seed_risk_database(database_path, risk_id=risk_id)
    barrier = threading.Barrier(2)
    results: list[bool] = []
    errors: list[Exception] = []

    def worker() -> None:
        connection = sqlite3.connect(database_path, timeout=10)
        try:
            barrier.wait(timeout=10)
            results.append(
                acquire_lock(
                    risk_id,
                    connection=connection,
                )
            )
        except Exception as exc:  # pragma: no cover - failure is asserted below
            errors.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    assert not errors
    assert sorted(results) == [False, True]
    print("PASS test_concurrent_duplicate_trigger")


def test_stale_lock_sweep(tmp_path: Path) -> None:
    database_path = tmp_path / "stale.db"
    risk_id = "risk_stale"
    _seed_risk_database(database_path, risk_id=risk_id)

    with sqlite3.connect(database_path) as connection:
        assert acquire_lock(risk_id, connection=connection) is True
        stale_time = (
            datetime.now(timezone.utc) - timedelta(minutes=30)
        ).replace(tzinfo=None).isoformat(sep=" ", timespec="seconds")
        connection.execute(
            "UPDATE risk_events SET locked_at = ? WHERE risk_id = ?",
            (stale_time, risk_id),
        )
        connection.commit()

        swept = sweep_stale_locks(
            stale_minutes=10,
            connection=connection,
        )
        row = connection.execute(
            """
            SELECT status, force_escalate, lock_token, locked_at
            FROM risk_events WHERE risk_id = ?
            """,
            (risk_id,),
        ).fetchone()
        audit = connection.execute(
            """
            SELECT stage FROM audit_log
            WHERE risk_id = ? AND stage = 'lock_swept'
            """,
            (risk_id,),
        ).fetchone()

    assert swept == [risk_id]
    assert row == ("diagnosed", 1, None, None)
    assert audit == ("lock_swept",)
    print("PASS test_stale_lock_sweep")


def test_duplicate_executor_call(tmp_path: Path) -> None:
    database_path = tmp_path / "executor.db"
    risk_id = "risk_executor"
    decision_id = "decision_executor"
    _seed_risk_database(database_path, risk_id=risk_id)

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO diagnoses (
                diagnosis_id, risk_id, root_cause, recommended_action,
                recommended_channel, confidence, diagnosed_at, provider_used
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "diagnosis_executor",
                risk_id,
                "synthetic test diagnosis",
                "send_reminder",
                "email",
                0.9,
                "2026-08-28 00:00:00",
                "fallback_default",
            ),
        )
        connection.execute(
            """
            INSERT INTO policy_decisions (
                decision_id, risk_id, diagnosis_id, decision,
                final_action, final_discount_pct, rules_applied,
                reason, decided_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                decision_id,
                risk_id,
                "diagnosis_executor",
                "auto_approved",
                "send_reminder",
                0,
                "[]",
                "test decision",
                "2026-08-28 00:00:00",
            ),
        )
        first = execute_action(connection, decision_id=decision_id)
        second = execute_action(connection, decision_id=decision_id)
        action_count = connection.execute(
            "SELECT COUNT(*) FROM actions_taken WHERE decision_id = ?",
            (decision_id,),
        ).fetchone()[0]
        audit = connection.execute(
            """
            SELECT COUNT(*) FROM audit_log
            WHERE risk_id = ? AND stage = 'duplicate_execution_blocked'
            """,
            (risk_id,),
        ).fetchone()[0]

    assert first["status"] == "executed"
    assert second["status"] == "duplicate_execution_blocked"
    assert action_count == 1
    assert audit == 1
    print("PASS test_duplicate_executor_call")


if __name__ == "__main__":
    raise SystemExit(
        "Run with: python -m pytest tests/test_idempotency.py -v"
    )