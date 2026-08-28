"""ReviveAI application entry point.

The Streamlit dashboard is intentionally deferred to Phase 5. Phase 0 only
creates the SQLite data layer and synthetic Razorpay-shaped input data.
"""


def main() -> None:
    print(
        "ReviveAI Phase 0 data layer is ready. "
        "Run `python db/init_db.py` or `python db/seed_data.py`."
    )


if __name__ == "__main__":
    main()