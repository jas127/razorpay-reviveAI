"""Run simulated approved actions and deterministic outcome simulation."""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

try:
    from .action_executor import (
        DATABASE_PATH,
        execute_action,
        load_pending_decisions,
        simulate_outcomes,
    )
except ImportError:  # Supports `python executor/run_executor.py`.
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from executor.action_executor import (
        DATABASE_PATH,
        execute_action,
        load_pending_decisions,
        simulate_outcomes,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Execute approved ReviveAI actions in simulation."
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=DATABASE_PATH,
        help="SQLite database path (default: revive.db)",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Random seed for reproducible outcomes (default: 42)",
    )
    args = parser.parse_args()

    executed = 0
    with sqlite3.connect(args.database, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        pending_decisions = load_pending_decisions(connection)
        for decision in pending_decisions:
            result = execute_action(decision, connection=connection)
            if result["status"] == "executed":
                executed += 1

        outcome_summary = simulate_outcomes(connection, seed=args.seed)
        attempted_amount = connection.execute(
            """
            SELECT COALESCE(SUM(r.amount_at_risk), 0)
            FROM actions_taken AS a
            INNER JOIN risk_events AS r ON r.risk_id = a.risk_id
            """
        ).fetchone()[0]

    recovered = float(outcome_summary["total_recovered"])
    recovery_rate = (
        (recovered / float(attempted_amount) * 100)
        if attempted_amount
        else 0.0
    )
    print(f"Total actions executed: {executed}")
    print(f"Total ₹ recovered: ₹{recovered:,.2f}")
    print(f"Recovery rate: {recovery_rate:.2f}%")
    print(
        f"Outcomes processed: {outcome_summary['outcomes_processed']} "
        f"(paid: {outcome_summary['paid_count']})"
    )


if __name__ == "__main__":
    main()