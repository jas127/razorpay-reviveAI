"""Deterministic, database-backed Policy Engine for ReviveAI."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATABASE_PATH = PROJECT_ROOT / "revive.db"

DEFAULT_POLICY_RULES: tuple[tuple[str, str, dict[str, Any]], ...] = (
    ("max_contact_frequency", "max_contact_frequency", {"max_per_7_days": 2}),
    ("cooldown_hours", "cooldown_hours", {"hours": 24}),
    ("max_attempts", "max_attempts", {"max": 3}),
    ("discount_cap", "discount_cap", {"max_discount_pct": 10}),
    (
        "approval_threshold_amount",
        "approval_threshold_amount",
        {"amount": 10000},
    ),
    ("confidence_threshold", "confidence_threshold", {"min_confidence": 0.6}),
    ("channel_hours", "channel_hours", {"start_hour": 9, "end_hour": 20}),
)


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


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def ensure_policy_schema(connection: sqlite3.Connection) -> None:
    """Apply the Phase 2.5 column migration to older local databases."""
    columns = {
        row[1]
        for row in connection.execute("PRAGMA table_info(risk_events)").fetchall()
    }
    if "force_escalate" not in columns:
        connection.execute(
            "ALTER TABLE risk_events ADD COLUMN force_escalate INTEGER DEFAULT 0"
        )
        connection.commit()


def seed_default_policy_rules(connection: sqlite3.Connection) -> None:
    """Insert missing defaults without overwriting merchant customizations."""
    ensure_policy_schema(connection)
    for rule_id, rule_type, rule_value in DEFAULT_POLICY_RULES:
        existing = connection.execute(
            "SELECT 1 FROM policy_rules WHERE rule_type = ? LIMIT 1",
            (rule_type,),
        ).fetchone()
        if existing is None:
            connection.execute(
                """
                INSERT INTO policy_rules (rule_id, rule_name, rule_type, rule_value, active)
                VALUES (?, ?, ?, ?, 1)
                """,
                (
                    f"rule_{rule_id}",
                    rule_id.replace("_", " ").title(),
                    rule_type,
                    _json(rule_value),
                ),
            )
    connection.commit()


def _load_policy_rules(connection: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    rules: dict[str, dict[str, Any]] = {}
    rows = connection.execute(
        """
        SELECT rule_type, rule_value
        FROM policy_rules
        WHERE active = 1
        """
    ).fetchall()
    for row in rows:
        try:
            value = json.loads(row[1])
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError(
                f"invalid JSON for active policy rule {row[0]!r}"
            ) from exc
        if not isinstance(value, dict):
            raise ValueError(f"policy rule {row[0]!r} must contain a JSON object")
        rules[row[0]] = value

    missing = {
        rule_type
        for _, rule_type, _ in DEFAULT_POLICY_RULES
        if rule_type not in rules
    }
    if missing:
        raise ValueError(f"missing active policy rules: {sorted(missing)}")
    return rules


def _customer_context(
    connection: sqlite3.Connection,
    risk_event_row: sqlite3.Row | dict[str, Any],
) -> tuple[int, int]:
    opted_out = _value(
        risk_event_row,
        "customer_opted_out",
        _value(risk_event_row, "opted_out"),
    )
    contact_count = _value(
        risk_event_row,
        "customer_contact_count_last_7d",
        _value(risk_event_row, "contact_count_last_7d"),
    )
    if opted_out is None or contact_count is None:
        customer_id = _value(risk_event_row, "customer_id")
        customer = connection.execute(
            """
            SELECT opted_out, contact_count_last_7d
            FROM customers WHERE customer_id = ?
            """,
            (customer_id,),
        ).fetchone()
        if customer is None:
            raise ValueError(f"customer not found: {customer_id}")
        opted_out = customer[0] if opted_out is None else opted_out
        contact_count = customer[1] if contact_count is None else contact_count
    return int(opted_out or 0), int(contact_count or 0)


def _has_recent_action(
    connection: sqlite3.Connection,
    risk_id: str,
    cooldown_hours: int,
) -> bool:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=cooldown_hours)
    return (
        connection.execute(
            """
            SELECT 1
            FROM actions_taken
            WHERE risk_id = ?
              AND executed_at IS NOT NULL
              AND datetime(executed_at) >= datetime(?)
            LIMIT 1
            """,
            (risk_id, _timestamp(cutoff)),
        ).fetchone()
        is not None
    )


def _count_actions(connection: sqlite3.Connection, risk_id: str) -> int:
    return int(
        connection.execute(
            "SELECT COUNT(*) FROM actions_taken WHERE risk_id = ?",
            (risk_id,),
        ).fetchone()[0]
    )


def _insert_policy_result(
    connection: sqlite3.Connection,
    *,
    risk_event_row: sqlite3.Row | dict[str, Any],
    diagnosis_row: sqlite3.Row | dict[str, Any],
    decision: str,
    final_action: str,
    final_discount_pct: float,
    rules_applied: list[str],
    reason: str,
) -> dict[str, Any]:
    risk_id = _value(risk_event_row, "risk_id")
    diagnosis_id = _value(diagnosis_row, "diagnosis_id")
    decision_id = f"decision_{risk_id}_{uuid.uuid4().hex}"
    decided_at = _timestamp()
    result = {
        "decision_id": decision_id,
        "risk_id": risk_id,
        "diagnosis_id": diagnosis_id,
        "decision": decision,
        "final_action": final_action,
        "final_discount_pct": final_discount_pct,
        "rules_applied": rules_applied,
        "reason": reason,
        "decided_at": decided_at,
    }
    connection.execute(
        """
        INSERT INTO policy_decisions (
            decision_id, risk_id, diagnosis_id, decision, final_action,
            final_discount_pct, rules_applied, reason, decided_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            decision_id,
            risk_id,
            diagnosis_id,
            decision,
            final_action,
            final_discount_pct,
            _json(rules_applied),
            reason,
            decided_at,
        ),
    )
    connection.execute(
        """
        INSERT INTO audit_log (
            log_id, risk_id, stage, actor, input_snapshot,
            output_snapshot, timestamp
        ) VALUES (?, ?, 'policy', 'policy_engine', ?, ?, ?)
        """,
        (
            f"audit_policy_{risk_id}_{uuid.uuid4().hex}",
            risk_id,
            _json(
                {
                    "risk_event": dict(risk_event_row),
                    "diagnosis": dict(diagnosis_row),
                }
            ),
            _json(result),
            decided_at,
        ),
    )
    connection.commit()
    return result


def evaluate_policy(
    risk_event_row: sqlite3.Row | dict[str, Any],
    diagnosis_row: sqlite3.Row | dict[str, Any],
    connection: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Evaluate one diagnosis in the fixed, auditable rule order."""
    owns_connection = connection is None
    db = connection or sqlite3.connect(DATABASE_PATH)
    db.row_factory = sqlite3.Row
    try:
        seed_default_policy_rules(db)
        rules = _load_policy_rules(db)
        risk_id = _value(risk_event_row, "risk_id")
        opted_out, contact_count = _customer_context(db, risk_event_row)
        applied: list[str] = []

        if opted_out == 1:
            applied.append("opt_out_respect")
            return _insert_policy_result(
                db,
                risk_event_row=risk_event_row,
                diagnosis_row=diagnosis_row,
                decision="blocked",
                final_action="none",
                final_discount_pct=0.0,
                rules_applied=applied,
                reason="customer opted out",
            )

        frequency_limit = int(
            rules["max_contact_frequency"]["max_per_7_days"]
        )
        if contact_count >= frequency_limit:
            applied.append("max_contact_frequency")
            return _insert_policy_result(
                db,
                risk_event_row=risk_event_row,
                diagnosis_row=diagnosis_row,
                decision="blocked",
                final_action="none",
                final_discount_pct=0.0,
                rules_applied=applied,
                reason="frequency cap reached",
            )

        cooldown_hours = int(rules["cooldown_hours"]["hours"])
        if _has_recent_action(db, risk_id, cooldown_hours):
            applied.append("cooldown_hours")
            return _insert_policy_result(
                db,
                risk_event_row=risk_event_row,
                diagnosis_row=diagnosis_row,
                decision="blocked",
                final_action="none",
                final_discount_pct=0.0,
                rules_applied=applied,
                reason="cooldown active",
            )

        max_attempts = int(rules["max_attempts"]["max"])
        if _count_actions(db, risk_id) >= max_attempts:
            applied.append("max_attempts")
            db.execute(
                "UPDATE risk_events SET status = 'expired' WHERE risk_id = ?",
                (risk_id,),
            )
            db.commit()
            return _insert_policy_result(
                db,
                risk_event_row=risk_event_row,
                diagnosis_row=diagnosis_row,
                decision="blocked",
                final_action="none",
                final_discount_pct=0.0,
                rules_applied=applied,
                reason="max attempts reached, marking risk_event as expired",
            )

        if int(_value(risk_event_row, "force_escalate", 0) or 0) == 1:
            applied.append("force_escalate")
            return _insert_policy_result(
                db,
                risk_event_row=risk_event_row,
                diagnosis_row=diagnosis_row,
                decision="needs_human_approval",
                final_action="escalate_to_human",
                final_discount_pct=0.0,
                rules_applied=applied,
                reason="stale lock recovered; human approval required",
            )

        min_confidence = float(rules["confidence_threshold"]["min_confidence"])
        confidence = float(_value(diagnosis_row, "confidence", 0) or 0)
        if confidence < min_confidence:
            applied.append("confidence_threshold")
            return _insert_policy_result(
                db,
                risk_event_row=risk_event_row,
                diagnosis_row=diagnosis_row,
                decision="needs_human_approval",
                final_action="escalate_to_human",
                final_discount_pct=0.0,
                rules_applied=applied,
                reason="low LLM confidence",
            )

        approval_threshold = float(
            rules["approval_threshold_amount"]["amount"]
        )
        amount_at_risk = float(_value(risk_event_row, "amount_at_risk", 0) or 0)
        if amount_at_risk > approval_threshold:
            applied.append("approval_threshold_amount")
            return _insert_policy_result(
                db,
                risk_event_row=risk_event_row,
                diagnosis_row=diagnosis_row,
                decision="needs_human_approval",
                final_action="escalate_to_human",
                final_discount_pct=0.0,
                rules_applied=applied,
                reason="amount exceeds auto-approval threshold",
            )

        recommended_action = _value(
            diagnosis_row, "recommended_action", "escalate_to_human"
        )
        if recommended_action == "escalate_to_human":
            applied.append("escalate_to_human")
            return _insert_policy_result(
                db,
                risk_event_row=risk_event_row,
                diagnosis_row=diagnosis_row,
                decision="needs_human_approval",
                final_action="escalate_to_human",
                final_discount_pct=0.0,
                rules_applied=applied,
                reason="LLM advised human escalation",
            )

        recommended_discount = float(
            _value(diagnosis_row, "recommended_discount_pct", 0) or 0
        )
        discount_cap = float(rules["discount_cap"]["max_discount_pct"])
        final_discount = min(recommended_discount, discount_cap)
        if recommended_discount > discount_cap:
            applied.append("discount_cap")
            return _insert_policy_result(
                db,
                risk_event_row=risk_event_row,
                diagnosis_row=diagnosis_row,
                decision="modified",
                final_action=recommended_action,
                final_discount_pct=final_discount,
                rules_applied=applied,
                reason=(
                    f"discount capped at {discount_cap:g}% "
                    f"(recommended {recommended_discount:g}%)"
                ),
            )

        return _insert_policy_result(
            db,
            risk_event_row=risk_event_row,
            diagnosis_row=diagnosis_row,
            decision="auto_approved",
            final_action=recommended_action,
            final_discount_pct=final_discount,
            rules_applied=applied,
            reason="all policy checks passed",
        )
    finally:
        if owns_connection:
            db.close()


def load_diagnosed_risk_events(
    connection: sqlite3.Connection,
) -> list[tuple[sqlite3.Row, sqlite3.Row]]:
    """Load diagnosed events and their diagnosis, excluding already-decided risks."""
    connection.row_factory = sqlite3.Row
    rows = connection.execute(
        """
        SELECT
            r.*,
            c.name AS customer_name,
            c.email AS customer_email,
            c.contact_count_last_7d AS customer_contact_count_last_7d,
            c.opted_out AS customer_opted_out,
            d.diagnosis_id,
            d.root_cause,
            d.recommended_action,
            d.recommended_channel,
            d.recommended_discount_pct,
            d.confidence,
            d.llm_raw_response,
            d.diagnosed_at
        FROM risk_events AS r
        INNER JOIN customers AS c ON c.customer_id = r.customer_id
        INNER JOIN diagnoses AS d ON d.risk_id = r.risk_id
        WHERE r.status = 'diagnosed'
          AND NOT EXISTS (
              SELECT 1
              FROM policy_decisions AS prior
              WHERE prior.risk_id = r.risk_id
                AND r.force_escalate = 0
          )
        ORDER BY r.detected_at ASC, r.risk_id ASC
        """
    ).fetchall()
    return [(row, row) for row in rows]