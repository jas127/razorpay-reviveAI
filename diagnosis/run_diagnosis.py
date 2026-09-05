"""Standalone batch runner for the ReviveAI Diagnosis Agent.

Run from the project root:

    python diagnosis/run_diagnosis.py

Set LLM_PROVIDER to gemini or groq to choose the primary provider. The other
provider is always attempted as a fallback, and the hardcoded human-escalation
diagnosis is used if both providers fail.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

try:
    from db.idempotency import acquire_lock, log_lock_contended
except ModuleNotFoundError:  # Supports `python diagnosis/run_diagnosis.py`.
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from db.idempotency import acquire_lock, log_lock_contended

try:
    from .diagnosis_agent import (
        diagnose_risk_event,
        get_provider_chain,
        load_new_risk_events,
    )
except ImportError:  # Supports `python diagnosis/run_diagnosis.py`.
    from diagnosis_agent import (
        diagnose_risk_event,
        get_provider_chain,
        load_new_risk_events,
    )


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATABASE_PATH = PROJECT_ROOT / "revive.db"


def ensure_provider_used_column(connection: sqlite3.Connection) -> None:
    """Migrate a Phase 0 database created before provider_used or customer_message_hinglish existed."""
    columns = {
        row[1]
        for row in connection.execute("PRAGMA table_info(diagnoses)").fetchall()
    }
    if "provider_used" not in columns:
        connection.execute(
            "ALTER TABLE diagnoses ADD COLUMN provider_used TEXT"
        )
    if "customer_message_hinglish" not in columns:
        connection.execute(
            "ALTER TABLE diagnoses ADD COLUMN customer_message_hinglish TEXT"
        )


def configured_delay_seconds() -> float:
    raw_value = os.getenv("DIAGNOSIS_DELAY_SECONDS", "0.2")
    try:
        return max(0.0, float(raw_value))
    except ValueError:
        return 0.2


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Run diagnosis on new risk events.")
    parser.add_argument("--limit", type=int, default=None, help="Limit number of risk events to diagnose")
    parser.add_argument("--delay", type=float, default=None, help="Delay between API calls in seconds")
    args, _ = parser.parse_known_args()

    if not DATABASE_PATH.exists():
        raise SystemExit(
            f"Database not found at {DATABASE_PATH}. "
            "Run `python db/seed_data.py` and `python detection/run_detection.py` first."
        )

    connection = sqlite3.connect(DATABASE_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 10000")
    try:
        with connection:
            ensure_provider_used_column(connection)

        providers = get_provider_chain()
        primary_provider = providers[0].name
        risk_events = load_new_risk_events(connection)
        if args.limit:
            risk_events = risk_events[:args.limit]

        delay_seconds = args.delay if args.delay is not None else configured_delay_seconds()
        primary_count = 0
        fallback_count = 0
        default_count = 0

        print(
            f"Diagnosing {len(risk_events)} new risk events "
            f"(primary={primary_provider}, delay={delay_seconds:.2f}s)"
        )

        for risk_event in risk_events:
            risk_id = risk_event["risk_id"]
            with connection:
                promoted = connection.execute(
                    """
                    UPDATE risk_events
                    SET status = 'diagnosed'
                    WHERE risk_id = ? AND status = 'new'
                    """,
                    (risk_id,),
                ).rowcount
                if promoted != 1 or not acquire_lock(risk_id, connection=connection):
                    log_lock_contended(risk_id, connection=connection)
                    print(f"[lock_contended] risk_id={risk_id} skipped", flush=True)
                    continue

            # LLM Provider Call is performed outside SQLite write lock
            result = diagnose_risk_event(
                risk_event,
                connection,
                provider_chain=providers,
                delay_seconds=delay_seconds,
            )

            with connection:
                connection.execute(
                    """
                    UPDATE risk_events
                    SET status = 'diagnosed',
                        lock_token = NULL,
                        locked_at = NULL
                    WHERE risk_id = ? AND status = 'locked'
                    """,
                    (risk_id,),
                )

            provider_used = result["provider_used"]
            if result["used_default"]:
                default_count += 1
            elif result["used_fallback"]:
                fallback_count += 1
            else:
                primary_count += 1
            print(f"[{provider_used}] risk_id={risk_id} diagnosed", flush=True)

        print("Diagnosis summary:", flush=True)
        print(f"  - primary provider ({primary_provider}): {primary_count}", flush=True)
        print(f"  - fallback provider: {fallback_count}", flush=True)
        print(f"  - hardcoded default: {default_count}", flush=True)
    finally:
        connection.close()


if __name__ == "__main__":
    main()