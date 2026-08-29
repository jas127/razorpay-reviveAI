"""Minimal deterministic action executor with database duplicate protection.

The full simulated message and outcome behavior belongs to Phase 4. This
module establishes the Phase 2.5 at-most-once insert boundary now.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any


EXECUTABLE_DECISIONS = {"auto_approved", "modified"}


def _timestamp() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(
        sep=" ", timespec="seconds"
    )


def _value(row: sqlite3.Row | dict[str, Any], key: str, default: Any = None) -> Any:
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
        "SELECT * FROM policy_decisions WHERE decision_id = ?",
        (decision_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"policy decision not found: {decision_id}")
    return row


def execute_action(
    connection: sqlite3.Connection,
    decision_row: sqlite3.Row | dict[str, Any] | str | None = None,
    *,
    decision_id: str | None = None,
) -> dict[str, Any]:
    """Insert one simulated action, catching only duplicate decision inserts.

    The UNIQUE constraint on actions_taken.decision_id is the final authority.
    A second call returns a structured ``duplicate_execution_blocked`` result
    and leaves exactly one action row in the database.
    """
    if isinstance(decision_row, str):
        decision_id = decision_row
        decision_row = None
    if decision_row is None:
        if not decision_id:
            raise ValueError("decision_row or decision_id is required")
        decision_row = _load_decision(connection, decision_id)

    decision_id = _value(decision_row, "decision_id", decision_id)
    risk_id = _value(decision_row, "risk_id")
    decision = _value(decision_row, "decision")
    if not decision_id or not risk_id:
        raise ValueError("decision_row must contain decision_id and risk_id")
    if decision not in EXECUTABLE_DECISIONS:
        raise ValueError(
            f"decision {decision!r} is not executable; "
            "only auto_approved and modified may execute"
        )

    action_id = f"action_{uuid.uuid4().hex}"
    action_type = _value(decision_row, "final_action", "send_reminder")
    channel = _value(decision_row, "channel", "email")
    try:
        connection.execute(
            """
            INSERT INTO actions_taken (
                action_id, decision_id, risk_id, action_type, channel,
                message_sent, executed_at, simulated
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (
                action_id,
                decision_id,
                risk_id,
                action_type,
                channel,
                "Simulated ReviveAI recovery action.",
                _timestamp(),
            ),
        )
        connection.commit()
        return {
            "status": "executed",
            "action_id": action_id,
            "decision_id": decision_id,
            "risk_id": risk_id,
        }
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