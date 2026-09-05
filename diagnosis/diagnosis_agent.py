"""Provider-agnostic, advisory-only diagnosis agent for ReviveAI."""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable

try:
    from .base_provider import LLMProvider, ProviderError
    from .gemini_provider import GeminiProvider
    from .groq_provider import GroqProvider
except ImportError:  # Supports `python diagnosis/run_diagnosis.py`.
    from base_provider import LLMProvider, ProviderError
    from gemini_provider import GeminiProvider
    from groq_provider import GroqProvider


ALLOWED_ACTIONS = {
    "send_reminder",
    "retry_payment",
    "offer_discount",
    "send_payment_link",
    "switch_payment_method",
    "escalate_to_human",
}
ALLOWED_CHANNELS = {"email", "sms", "whatsapp"}

RISK_EVENT_QUERY = """
    SELECT
        r.*,
        re.event_type,
        re.failure_code,
        re.occurred_at AS event_occurred_at,
        re.payment_method,
        c.name AS customer_name,
        c.email AS customer_email,
        c.preferred_language AS customer_preferred_language,
        c.contact_count_last_7d AS customer_contact_count_last_7d,
        c.opted_out AS customer_opted_out,
        (
            SELECT COUNT(*)
            FROM risk_events AS prior
            WHERE prior.customer_id = r.customer_id
              AND prior.risk_id <> r.risk_id
        ) AS prior_risk_count
    FROM risk_events AS r
    INNER JOIN revenue_events AS re ON re.event_id = r.event_id
    INNER JOIN customers AS c ON c.customer_id = r.customer_id
    WHERE r.status = 'new'
    ORDER BY r.detected_at ASC, r.risk_id ASC
"""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def timestamp_for_sql(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(tzinfo=None).isoformat(
        sep=" ", timespec="seconds"
    )


def parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def days_since_event(occurred_at: str) -> float:
    return max(
        0.0,
        (utc_now() - parse_timestamp(occurred_at)).total_seconds() / 86_400,
    )


def value_from_row(
    risk_event_row: sqlite3.Row | dict[str, Any],
    key: str,
    default: Any = None,
) -> Any:
    try:
        return risk_event_row[key]
    except (KeyError, IndexError):
        return default


def build_prompt(risk_event_row: sqlite3.Row | dict[str, Any]) -> str:
    """Build a strict JSON-only diagnosis prompt from one risk event."""
    occurred_at = value_from_row(
        risk_event_row,
        "event_occurred_at",
        value_from_row(risk_event_row, "occurred_at"),
    )
    prior_risk_count = int(value_from_row(risk_event_row, "prior_risk_count", 0) or 0)
    contact_count = int(
        value_from_row(
            risk_event_row,
            "customer_contact_count_last_7d",
            value_from_row(risk_event_row, "contact_count_last_7d", 0),
        )
        or 0
    )
    customer_name = value_from_row(
        risk_event_row,
        "customer_name",
        value_from_row(risk_event_row, "name", "Customer"),
    )
    preferred_language = value_from_row(
        risk_event_row,
        "customer_preferred_language",
        value_from_row(risk_event_row, "preferred_language", "en"),
    )
    failure_code = value_from_row(risk_event_row, "failure_code")
    failure_text = failure_code or "not applicable / not supplied"
    amount = float(value_from_row(risk_event_row, "amount_at_risk", 0) or 0)
    risk_category = value_from_row(risk_event_row, "risk_category", "unknown")
    event_days = days_since_event(occurred_at) if occurred_at else 0.0

    is_hinglish = (preferred_language == "hi-en")
    hinglish_instruction = ""
    json_shape = """{
  "root_cause": "<one sentence explanation>",
  "recommended_action": "<one of: send_reminder, retry_payment, offer_discount, send_payment_link, switch_payment_method, escalate_to_human>",
  "recommended_channel": "<one of: email, sms, whatsapp>",
  "recommended_discount_pct": <number 0-100>,
  "confidence": <number 0-1>
}"""

    if is_hinglish:
        hinglish_instruction = f"""
Customer name: {customer_name}
Customer preferred language: Hinglish (hi-en)
IMPORTANT HINGLISH INSTRUCTION: Since the customer preferred language is 'hi-en', include a "customer_message" field in the JSON response containing a natural Hinglish recovery message (written in Roman script, mixing conversational Hindi and English as urban Indian customers text, e.g. "Hi {customer_name}, aapka ₹{amount:,.0f} ka payment fail ho gaya tha, yahan click karke turant complete karein"). Ensure it aligns with your recommended_action.
""".strip()

        json_shape = """{
  "root_cause": "<one sentence explanation>",
  "recommended_action": "<one of: send_reminder, retry_payment, offer_discount, send_payment_link, switch_payment_method, escalate_to_human>",
  "recommended_channel": "<one of: email, sms, whatsapp>",
  "recommended_discount_pct": <number 0-100>,
  "confidence": <number 0-1>,
  "customer_message": "<natural Hinglish recovery message in Roman script>"
}"""

    return f"""
You are the advisory diagnosis component of ReviveAI, an AI revenue recovery
system. Diagnose the risk event below. Your output is advisory text only:
never execute an action, contact a customer, move money, or make a policy
decision.

Risk category: {risk_category}
Amount at risk (INR): {amount:.2f}
Gateway failure code: {failure_text}
Days since event: {event_days:.2f}
Customer contacts in the last 7 days: {contact_count}
Prior risk events for this customer: {prior_risk_count}
Repeat offender: {"yes" if prior_risk_count > 0 else "no"}
{hinglish_instruction}

Respond ONLY with valid JSON. Do not use markdown fences, a preamble, or
additional keys. Use exactly this shape:
{json_shape}
""".strip()


def get_provider_chain() -> list[LLMProvider]:
    """Return the configured provider first and the other provider second."""
    requested_provider = os.getenv("LLM_PROVIDER", "groq").strip().lower()
    if requested_provider not in {"gemini", "groq"}:
        requested_provider = "groq"

    providers: dict[str, LLMProvider] = {
        "gemini": GeminiProvider(),
        "groq": GroqProvider(),
    }
    fallback_provider = "gemini" if requested_provider == "groq" else "groq"
    return [providers[requested_provider], providers[fallback_provider]]


def _strip_markdown_fences(raw_response: str) -> str:
    cleaned = raw_response.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        lines = lines[1:] if lines else lines
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
    return cleaned


def _validate_diagnosis(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("diagnosis response must be a JSON object")

    required_keys = {
        "root_cause",
        "recommended_action",
        "recommended_channel",
        "recommended_discount_pct",
        "confidence",
    }
    missing_keys = required_keys - payload.keys()
    if missing_keys:
        raise ValueError(f"diagnosis response is missing keys: {sorted(missing_keys)}")

    root_cause = payload["root_cause"]
    action = payload["recommended_action"]
    channel = payload["recommended_channel"]
    discount = payload["recommended_discount_pct"]
    confidence = payload["confidence"]

    if not isinstance(root_cause, str) or not root_cause.strip():
        raise ValueError("root_cause must be a non-empty string")
    if action not in ALLOWED_ACTIONS:
        raise ValueError(f"unsupported recommended_action: {action!r}")
    if channel not in ALLOWED_CHANNELS:
        raise ValueError(f"unsupported recommended_channel: {channel!r}")
    if isinstance(discount, bool) or not isinstance(discount, (int, float)):
        raise ValueError("recommended_discount_pct must be a number")
    if not 0 <= float(discount) <= 100:
        raise ValueError("recommended_discount_pct must be between 0 and 100")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise ValueError("confidence must be a number")
    if not 0 <= float(confidence) <= 1:
        raise ValueError("confidence must be between 0 and 1")

    customer_message = payload.get("customer_message")
    if customer_message is not None and not isinstance(customer_message, str):
        customer_message = str(customer_message)
    if customer_message and not customer_message.strip():
        customer_message = None

    return {
        "root_cause": root_cause.strip(),
        "recommended_action": action,
        "recommended_channel": channel,
        "recommended_discount_pct": float(discount),
        "confidence": float(confidence),
        "customer_message": customer_message.strip() if customer_message else None,
    }


def parse_diagnosis_response(raw_response: str) -> dict[str, Any]:
    """Parse a provider response, tolerating fences or a short preamble."""
    cleaned = _strip_markdown_fences(raw_response)
    try:
        return _validate_diagnosis(json.loads(cleaned))
    except (json.JSONDecodeError, ValueError) as first_error:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start < 0 or end <= start:
            raise ValueError(f"invalid diagnosis JSON: {first_error}") from first_error
        try:
            return _validate_diagnosis(json.loads(cleaned[start : end + 1]))
        except (json.JSONDecodeError, ValueError) as second_error:
            raise ValueError(f"invalid diagnosis JSON: {second_error}") from second_error


def row_snapshot(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    if isinstance(row, sqlite3.Row):
        return {key: row[key] for key in row.keys()}
    return dict(row)


def insert_audit_log(
    connection: sqlite3.Connection,
    *,
    risk_id: str,
    stage: str,
    actor: str,
    input_snapshot: dict[str, Any],
    output_snapshot: dict[str, Any],
) -> None:
    connection.execute(
        """
        INSERT INTO audit_log (
            log_id, risk_id, stage, actor, input_snapshot,
            output_snapshot, timestamp
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            f"audit_{stage}_{risk_id}_{actor}_{uuid.uuid4().hex}",
            risk_id,
            stage,
            actor,
            json.dumps(input_snapshot, ensure_ascii=False, sort_keys=True),
            json.dumps(output_snapshot, ensure_ascii=False, sort_keys=True),
            timestamp_for_sql(utc_now()),
        ),
    )


def _default_diagnosis() -> dict[str, Any]:
    return {
        "root_cause": "diagnosis unavailable — all LLM providers failed",
        "recommended_action": "escalate_to_human",
        "recommended_channel": "email",
        "recommended_discount_pct": 0.0,
        "confidence": 0.0,
        "customer_message": None,
    }


def _store_diagnosis(
    connection: sqlite3.Connection,
    *,
    risk_event_row: sqlite3.Row | dict[str, Any],
    diagnosis: dict[str, Any],
    raw_response: str,
    provider_used: str,
) -> dict[str, Any]:
    risk_id = value_from_row(risk_event_row, "risk_id")
    diagnosis_id = f"diagnosis_{risk_id}"
    diagnosed_at = timestamp_for_sql(utc_now())
    hinglish_msg = diagnosis.get("customer_message")

    connection.execute(
        """
        INSERT OR IGNORE INTO diagnoses (
            diagnosis_id, risk_id, root_cause, recommended_action,
            recommended_channel, recommended_discount_pct, confidence,
            llm_raw_response, diagnosed_at, provider_used,
            customer_message_hinglish
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            diagnosis_id,
            risk_id,
            diagnosis["root_cause"],
            diagnosis["recommended_action"],
            diagnosis["recommended_channel"],
            diagnosis["recommended_discount_pct"],
            diagnosis["confidence"],
            raw_response,
            diagnosed_at,
            provider_used,
            hinglish_msg,
        ),
    )
    output_snapshot = {
        "diagnosis_id": diagnosis_id,
        "risk_id": risk_id,
        **diagnosis,
        "provider_used": provider_used,
    }
    insert_audit_log(
        connection,
        risk_id=risk_id,
        stage="diagnosis",
        actor=provider_used,
        input_snapshot=row_snapshot(risk_event_row),
        output_snapshot=output_snapshot,
    )
    return output_snapshot


def diagnose_risk_event(
    risk_event_row: sqlite3.Row | dict[str, Any],
    connection: sqlite3.Connection,
    *,
    provider_chain: Iterable[LLMProvider] | None = None,
    delay_seconds: float = 0.2,
) -> dict[str, Any]:
    """Diagnose one event with primary/fallback providers and persist the result."""
    providers = list(provider_chain or get_provider_chain())
    if len(providers) < 1:
        raise ValueError("provider_chain must contain at least one provider")

    risk_id = value_from_row(risk_event_row, "risk_id")
    prompt = build_prompt(risk_event_row)
    attempt_errors: list[dict[str, str]] = []

    for attempt_index, provider in enumerate(providers):
        try:
            raw_response = provider.generate_diagnosis(prompt)
            diagnosis = parse_diagnosis_response(raw_response)
            stored = _store_diagnosis(
                connection,
                risk_event_row=risk_event_row,
                diagnosis=diagnosis,
                raw_response=raw_response,
                provider_used=provider.name,
            )
            stored["used_default"] = False
            stored["used_fallback"] = attempt_index > 0
            return stored
        except (ProviderError, ValueError, json.JSONDecodeError) as exc:
            error_type = "provider_error" if isinstance(exc, ProviderError) else "json_error"
            error_details = {
                "provider": provider.name,
                "error_type": error_type,
                "error": str(exc),
            }
            attempt_errors.append(error_details)
            insert_audit_log(
                connection,
                risk_id=risk_id,
                stage="diagnosis_failed",
                actor=provider.name,
                input_snapshot={
                    "risk_id": risk_id,
                    "provider": provider.name,
                    "attempt": attempt_index + 1,
                    "prompt": prompt,
                },
                output_snapshot=error_details,
            )
            if attempt_index < len(providers) - 1 and delay_seconds > 0:
                time.sleep(delay_seconds)

    default_diagnosis = _default_diagnosis()
    stored = _store_diagnosis(
        connection,
        risk_event_row=risk_event_row,
        diagnosis=default_diagnosis,
        raw_response=json.dumps(default_diagnosis, ensure_ascii=False),
        provider_used="fallback_default",
    )
    stored["used_default"] = True
    stored["used_fallback"] = False
    stored["provider_errors"] = attempt_errors
    return stored


def load_new_risk_events(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return connection.execute(RISK_EVENT_QUERY).fetchall()