"""Initialize the ReviveAI SQLite database from schema.sql."""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"
DEFAULT_DATABASE_PATH = PROJECT_ROOT / "revive.db"


def initialize_database(
    database_path: Path = DEFAULT_DATABASE_PATH,
    *,
    reset: bool = False,
) -> Path:
    """Create the database from the checked-in schema.

    Args:
        database_path: Destination SQLite file.
        reset: Remove an existing database first for a clean Phase 0 seed.
    """
    if reset and database_path.exists():
        database_path.unlink()

    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    database_path.parent.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(schema)

    return database_path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create revive.db from db/schema.sql."
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Delete an existing revive.db before creating it.",
    )
    args = parser.parse_args()

    database_path = initialize_database(reset=args.reset)
    print(f"Initialized SQLite database: {database_path}")


if __name__ == "__main__":
    main()