"""Database-level idempotency locks for the ReviveAI pipeline."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATABASE_PATH = PROJECT_ROOT / "revive.db"


def utc_timestamp(value: datetime | None = None) -> str:
    current = value or datetime.now(timezone.utc)
    return current.astimezone(timezone.utc).replace(tzinfo=None).isoformat(
        sep=" ", timespec="seconds"
    )


def _open_connection(
    connection: sqlite3.Connection | None,
    database_path: Path,
) -> tuple[sqlite3.Connection, bool]:
    if connection is not None:
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection, False
    owned_connection = sqlite3.connect(database_path, timeout=10)
    owned_connection.execute("PRAGMA foreign_keys = ON")
    owned_connection.execute("PRAGMA busy_timeout = 10000")
    return owned_connection, True


def _ensure_force_escalate_column(connection: sqlite3.Connection) -> None:
    columns = {
        row[1]
        for row in connection.execute("PRAGMA table_info(risk_events)").fetchall()
    }
    modified = False
    if "force_escalate" not in columns:
        connection.execute(
            "ALTER TABLE risk_events ADD COLUMN force_escalate INTEGER DEFAULT 0"
        )
        modified = True
    if "promise_to_pay_date" not in columns:
        connection.execute(
            "ALTER TABLE risk_events ADD COLUMN promise_to_pay_date TIMESTAMP"
        )
        modified = True
    if modified:
        connection.commit()


def _json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _insert_audit(
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
            _json(input_snapshot),
            _json(output_snapshot),
            utc_timestamp(),
        ),
    )


def acquire_lock(
    risk_id: str,
    connection: sqlite3.Connection | None = None,
    database_path: Path = DATABASE_PATH,
) -> bool:
    """Atomically claim a diagnosed risk event for one pipeline pass.

    The UPDATE is intentionally guarded by ``status = 'diagnosed'`` and the
    caller relies on cursor.rowcount, not a preceding read, to decide whether
    it won. SQLite serializes the competing writes, so concurrent callers
    cannot both acquire the same risk event.
    """
    db, owns_connection = _open_connection(connection, database_path)
    try:
        _ensure_force_escalate_column(db)
        cursor = db.execute(
            """
            UPDATE risk_events
            SET status = 'locked',
                lock_token = ?,
                locked_at = ?,
                force_escalate = COALESCE(force_escalate, 0)
            WHERE risk_id = ? AND status = 'diagnosed'
            """,
            (str(uuid.uuid4()), utc_timestamp(), risk_id),
        )
        acquired = cursor.rowcount == 1
        db.commit()
        return acquired
    finally:
        if owns_connection:
            db.close()


def log_lock_contended(
    risk_id: str,
    connection: sqlite3.Connection | None = None,
    database_path: Path = DATABASE_PATH,
) -> None:
    """Record a non-winning lock attempt without changing pipeline state."""
    db, owns_connection = _open_connection(connection, database_path)
    try:
        _insert_audit(
            db,
            risk_id=risk_id,
            stage="lock_contended",
            input_snapshot={"risk_id": risk_id},
            output_snapshot={
                "risk_id": risk_id,
                "lock_acquired": False,
                "reason": "risk event was not in diagnosed state",
            },
        )
        db.commit()
    finally:
        if owns_connection:
            db.close()


def sweep_stale_locks(
    stale_minutes: int = 10,
    connection: sqlite3.Connection | None = None,
    database_path: Path = DATABASE_PATH,
) -> list[str]:
    """Reset abandoned locks and force their next decision to human review.

    Only locked risk events without a matching action are swept. A matching
    action means the pipeline progressed successfully and the lock must not be
    treated as abandoned.
    """
    if stale_minutes < 0:
        raise ValueError("stale_minutes cannot be negative")

    db, owns_connection = _open_connection(connection, database_path)
    swept_risk_ids: list[str] = []
    try:
        _ensure_force_escalate_column(db)
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=stale_minutes)
        rows = db.execute(
            """
            SELECT r.risk_id, r.status, r.lock_token, r.locked_at
            FROM risk_events AS r
            WHERE r.status = 'locked'
              AND r.locked_at IS NOT NULL
              AND datetime(r.locked_at) <= datetime(?)
              AND NOT EXISTS (
                  SELECT 1
                  FROM actions_taken AS a
                  WHERE a.risk_id = r.risk_id
              )
            ORDER BY r.risk_id
            """,
            (utc_timestamp(cutoff),),
        ).fetchall()

        for row in rows:
            cursor = db.execute(
                """
                UPDATE risk_events
                SET status = 'diagnosed',
                    lock_token = NULL,
                    locked_at = NULL,
                    force_escalate = 1
                WHERE risk_id = ? AND status = 'locked'
                """,
                (row[0],),
            )
            if cursor.rowcount != 1:
                continue
            swept_risk_ids.append(row[0])
            _insert_audit(
                db,
                risk_id=row[0],
                stage="lock_swept",
                input_snapshot={
                    "risk_id": row[0],
                    "previous_status": row[1],
                    "previous_lock_token": row[2],
                    "previous_locked_at": row[3],
                    "stale_minutes": stale_minutes,
                },
                output_snapshot={
                    "risk_id": row[0],
                    "status": "diagnosed",
                    "force_escalate": 1,
                    "next_action": "needs_human_approval",
                },
            )

        db.commit()
        return swept_risk_ids
    finally:
        if owns_connection:
            db.close()