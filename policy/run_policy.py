"""Run the deterministic ReviveAI Policy Engine over diagnosed risk events."""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from pathlib import Path

try:
    from .policy_engine import (
        DATABASE_PATH,
        evaluate_policy,
        load_diagnosed_risk_events,
        seed_default_policy_rules,
    )
except ImportError:  # Supports `python policy/run_policy.py`.
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from policy.policy_engine import (
        DATABASE_PATH,
        evaluate_policy,
        load_diagnosed_risk_events,
        seed_default_policy_rules,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Evaluate deterministic policy decisions for diagnosed risks."
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=DATABASE_PATH,
        help="SQLite database path (default: revive.db)",
    )
    args = parser.parse_args()

    with sqlite3.connect(args.database) as connection:
        connection.row_factory = sqlite3.Row
        seed_default_policy_rules(connection)
        risk_and_diagnoses = load_diagnosed_risk_events(connection)
        counts: Counter[str] = Counter()
        print(f"Evaluating {len(risk_and_diagnoses)} diagnosed risk events")
        for risk_event, diagnosis in risk_and_diagnoses:
            result = evaluate_policy(
                risk_event,
                diagnosis,
                connection=connection,
            )
            counts[result["decision"]] += 1
            print(
                f"[{result['decision']}] risk_id={result['risk_id']} "
                f"action={result['final_action']} "
                f"rules={json.dumps(result['rules_applied'])}"
            )

    print("Policy summary:")
    for decision in (
        "auto_approved",
        "modified",
        "blocked",
        "needs_human_approval",
    ):
        print(f"  - {decision}: {counts[decision]}")


if __name__ == "__main__":
    main()