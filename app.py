"""ReviveAI Streamlit dashboard.

Run with:

    streamlit run app.py --server.port 5000

The dashboard reads and writes the local SQLite source of truth. Any action
shown as executed remains simulated; no customer communication API is called.
"""

from __future__ import annotations

import contextlib
import csv
import io
import json
import os
import sqlite3
import sys
import textwrap
import uuid
from collections import Counter
from pathlib import Path
from typing import Any, Callable

import pandas as pd
import streamlit as st

try:
    from dotenv import load_dotenv
    _ENV_PATH = Path(__file__).resolve().parent / ".env"
    if _ENV_PATH.exists():
        load_dotenv(_ENV_PATH)
    else:
        load_dotenv()
except ImportError:
    pass

from datetime import datetime, timezone, timedelta, date

PROJECT_ROOT = Path(__file__).resolve().parent
DATABASE_PATH = PROJECT_ROOT / "revive.db"
PAGES = (
    "Overview",
    "Risk Queue",
    "Diagnosis View",
    "Recovery Queue",
    "Approvals",
    "Promises",
    "Reports",
    "Audit Trail & Compliance",
)

RISK_FORMULAS = {
    "payment_degradation": (
        "min(1, failure_severity + 0.25 × min(1, amount / ₹25,000))"
    ),
    "checkout_dropoff": (
        "min(1, 0.55 × amount_component + 0.45 × age_component)"
    ),
    "subscription_failure": (
        "min(1, 0.65 × failure_severity + 0.35 × min(1, amount / ₹10,000))"
    ),
    "receivable_overdue": (
        "min(1, (days_overdue / 30) × 0.6 + (amount / ₹50,000) × 0.4)"
    ),
}


def ensure_phase6_columns(connection: sqlite3.Connection) -> None:
    risk_cols = {
        row[1] for row in connection.execute("PRAGMA table_info(risk_events)").fetchall()
    }
    if "promise_to_pay_date" not in risk_cols:
        connection.execute("ALTER TABLE risk_events ADD COLUMN promise_to_pay_date TIMESTAMP")
    if "force_escalate" not in risk_cols:
        connection.execute("ALTER TABLE risk_events ADD COLUMN force_escalate INTEGER DEFAULT 0")

    diag_cols = {
        row[1] for row in connection.execute("PRAGMA table_info(diagnoses)").fetchall()
    }
    if "customer_message_hinglish" not in diag_cols:
        connection.execute("ALTER TABLE diagnoses ADD COLUMN customer_message_hinglish TEXT")
    connection.commit()


def get_connection() -> sqlite3.Connection:
    if not DATABASE_PATH.exists():
        raise FileNotFoundError(
            f"{DATABASE_PATH} does not exist. Run python db/seed_data.py first."
        )
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    ensure_phase6_columns(connection)
    return connection


def log_promise_to_pay(risk_id: str, promised_date: Any) -> None:
    date_str = str(promised_date)
    with get_connection() as connection:
        connection.execute(
            "UPDATE risk_events SET promise_to_pay_date = ? WHERE risk_id = ?",
            (date_str, risk_id),
        )
        connection.execute(
            """
            INSERT INTO audit_log (
                log_id, risk_id, stage, actor, input_snapshot, output_snapshot, timestamp
            ) VALUES (?, ?, 'promise_logged', 'human:operator', ?, ?, datetime('now'))
            """,
            (
                f"audit_ptp_{risk_id}_{uuid.uuid4().hex[:8]}",
                risk_id,
                json.dumps({"risk_id": risk_id, "promised_date": date_str}),
                json.dumps({"status": "promise_logged", "promise_to_pay_date": date_str}),
            ),
        )
        connection.commit()


def rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def money(value: Any) -> str:
    return f"₹{float(value or 0):,.2f}"


def pct(value: Any) -> str:
    return f"{float(value or 0):.2f}%"


def table_or_empty(
    rows: list[sqlite3.Row] | list[dict[str, Any]],
    message: str,
) -> None:
    if not rows:
        st.info(message)
        return
    st.dataframe(rows_to_dicts(rows) if rows and isinstance(rows[0], sqlite3.Row) else rows,
                 use_container_width=True, hide_index=True)


def query_rows(sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    with get_connection() as connection:
        return connection.execute(sql, params).fetchall()


def database_exists_or_stop() -> bool:
    if DATABASE_PATH.exists():
        return True
    st.error(
        "The SQLite database is not initialized. Run `python db/seed_data.py`, "
        "then refresh this page."
    )
    return False


def risk_queue_rows(
    categories: list[str] | None = None,
    statuses: list[str] | None = None,
) -> list[sqlite3.Row]:
    clauses: list[str] = []
    params: list[Any] = []
    if categories:
        clauses.append(
            f"r.risk_category IN ({','.join('?' for _ in categories)})"
        )
        params.extend(categories)
    if statuses:
        clauses.append(f"r.status IN ({','.join('?' for _ in statuses)})")
        params.extend(statuses)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return query_rows(
        f"""
        SELECT
            r.risk_id, r.risk_category, r.status, r.risk_score,
            r.amount_at_risk, r.detected_at, c.name AS customer_name,
            c.email, c.contact_count_last_7d
        FROM risk_events AS r
        INNER JOIN customers AS c ON c.customer_id = r.customer_id
        {where}
        ORDER BY r.amount_at_risk DESC, r.detected_at ASC
        """,
        tuple(params),
    )


def run_batch_main(function: Callable[[], None]) -> None:
    """Call an existing CLI main without leaking Streamlit's argv into argparse."""
    original_argv = sys.argv[:]
    try:
        sys.argv = [function.__module__]
        function()
    finally:
        sys.argv = original_argv


def run_full_pipeline() -> None:
    from detection.run_detection import main as detection_main
    from diagnosis.run_diagnosis import main as diagnosis_main
    from executor.run_executor import main as executor_main
    from policy.run_policy import main as policy_main

    os.environ.setdefault("DIAGNOSIS_DELAY_SECONDS", "0")
    steps: list[tuple[str, Callable[[], None]]] = [
        ("Detection", detection_main),
        ("Diagnosis", diagnosis_main),
        ("Policy", policy_main),
        ("Executor", executor_main),
    ]
    progress = st.progress(0, text="Starting full pipeline…")
    for index, (label, function) in enumerate(steps, start=1):
        with st.spinner(f"Running {label.lower()}…"):
            run_batch_main(function)
        progress.progress(index / len(steps), text=f"{label} complete")
    st.success("Full pipeline completed.")


def html(raw: str) -> None:
    """Render raw HTML safely in Streamlit without markdown treating indented lines as code blocks."""
    cleaned = "\n".join(line.strip() for line in raw.strip().splitlines())
    st.markdown(cleaned, unsafe_allow_html=True)


def inject_custom_css() -> None:
    html(
        """
        <style>
        /* Global Background & Typography */
        .stApp {
            background-color: #0b0f19;
            color: #f8fafc;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
        }
        
        /* Main Container Spacing */
        .block-container {
            padding-top: 2rem;
            padding-bottom: 3rem;
            max-width: 1200px;
        }

        /* Sidebar Styling */
        section[data-testid="stSidebar"] {
            background-color: #0f172a;
            border-right: 1px solid #1e293b;
        }
        section[data-testid="stSidebar"] .stRadio label {
            color: #94a3b8;
            font-size: 0.95rem;
            padding: 8px 12px;
            border-radius: 8px;
            margin-bottom: 2px;
            transition: all 0.2s ease;
        }
        section[data-testid="stSidebar"] .stRadio [data-checked="true"] {
            background: linear-gradient(90deg, rgba(14, 165, 233, 0.15) 0%, rgba(14, 165, 233, 0.05) 100%);
            color: #38bdf8 !important;
            font-weight: 700;
            border-left: 3px solid #0284c7;
        }

        /* ReviveAI Header */
        .app-header-container {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding-bottom: 1rem;
            margin-bottom: 1.5rem;
            border-bottom: 1px solid #1e293b;
        }
        .app-header-title {
            font-size: 1.75rem;
            font-weight: 800;
            color: #f8fafc;
            letter-spacing: -0.025em;
            margin: 0;
        }
        .app-header-tagline {
            font-size: 0.875rem;
            color: #94a3b8;
            margin-top: 0.2rem;
        }
        .app-header-badge {
            font-size: 0.75rem;
            font-weight: 600;
            color: #38bdf8;
            background: rgba(14, 165, 233, 0.12);
            border: 1px solid rgba(14, 165, 233, 0.3);
            padding: 4px 12px;
            border-radius: 20px;
            display: inline-flex;
            align-items: center;
            gap: 6px;
        }

        /* Stat Card Component */
        .revive-card {
            background: linear-gradient(145deg, #1e293b 0%, #151e2e 100%);
            border: 1px solid #334155;
            border-radius: 12px;
            padding: 16px 20px;
            margin-bottom: 14px;
            box-shadow: 0 4px 12px -2px rgba(0, 0, 0, 0.3);
            transition: transform 0.15s ease, border-color 0.15s ease;
        }
        .revive-card:hover {
            border-color: #475569;
        }
        .revive-card-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 8px;
        }
        .stat-label {
            font-size: 0.75rem;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 0.06em;
            color: #94a3b8;
        }
        .stat-value {
            font-size: 1.65rem;
            font-weight: 800;
            color: #f8fafc;
            line-height: 1.2;
            word-break: break-word;
        }
        .stat-subtext {
            font-size: 0.8rem;
            color: #64748b;
            margin-top: 4px;
        }
        .stat-delta {
            font-size: 0.8rem;
            font-weight: 600;
            color: #10b981;
            display: inline-flex;
            align-items: center;
            gap: 4px;
        }

        /* Status Badge Pill */
        .status-badge {
            display: inline-flex;
            align-items: center;
            gap: 5px;
            padding: 3px 10px;
            border-radius: 9999px;
            font-size: 0.725rem;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 0.04em;
            white-space: nowrap;
        }
        .badge-green {
            background-color: rgba(16, 185, 129, 0.15);
            color: #34d399;
            border: 1px solid rgba(16, 185, 129, 0.4);
        }
        .badge-amber {
            background-color: rgba(245, 158, 11, 0.15);
            color: #fbbf24;
            border: 1px solid rgba(245, 158, 11, 0.4);
        }
        .badge-red {
            background-color: rgba(239, 68, 68, 0.15);
            color: #f87171;
            border: 1px solid rgba(239, 68, 68, 0.4);
        }
        .badge-blue {
            background-color: rgba(14, 165, 233, 0.15);
            color: #38bdf8;
            border: 1px solid rgba(14, 165, 233, 0.4);
        }
        .badge-gray {
            background-color: rgba(148, 163, 184, 0.15);
            color: #cbd5e1;
            border: 1px solid rgba(148, 163, 184, 0.3);
        }

        /* Confidence Meter */
        .confidence-box {
            background: #1e293b;
            border: 1px solid #334155;
            border-radius: 10px;
            padding: 14px 18px;
            margin: 12px 0;
        }
        .confidence-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 8px;
        }
        .confidence-meter-bg {
            background-color: #0f172a;
            height: 14px;
            border-radius: 7px;
            overflow: hidden;
            border: 1px solid #334155;
        }
        .confidence-meter-fill {
            height: 100%;
            border-radius: 6px;
            transition: width 0.5s ease-in-out;
        }

        /* Pipeline Mini Timeline */
        .timeline-bar {
            display: flex;
            align-items: center;
            justify-content: space-between;
            background: #141d2e;
            border: 1px solid #1e293b;
            border-radius: 10px;
            padding: 14px 16px;
            margin: 14px 0;
            overflow-x: auto;
        }
        .timeline-step {
            display: flex;
            flex-direction: column;
            align-items: center;
            text-align: center;
            min-width: 90px;
            position: relative;
        }
        .timeline-icon {
            width: 28px;
            height: 28px;
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 0.8rem;
            margin-bottom: 6px;
        }
        .timeline-label {
            font-size: 0.7rem;
            font-weight: 700;
            text-transform: uppercase;
            color: #94a3b8;
        }
        .timeline-time {
            font-size: 0.65rem;
            color: #64748b;
            margin-top: 2px;
        }
        .timeline-connector {
            flex-grow: 1;
            height: 2px;
            background: #334155;
            margin: 0 8px;
            margin-bottom: 22px;
        }
        </style>
        """
    )


def short_id(val: Any) -> str:
    """Format risk_id or UUID into a clean readable string (e.g. '...388e05f')."""
    s = str(val or "")
    if len(s) > 16:
        return f"...{s[-8:]}"
    return s


def render_badge(status: str) -> str:
    """Render an HTML status badge with deliberate color coding."""
    st_clean = str(status or "").strip().lower()
    if st_clean in ("auto_approved", "resolved", "paid", "executed", "success"):
        css_class = "badge-green"
        icon = "✓"
    elif st_clean in ("modified", "needs_human_approval", "diagnosed", "promise_logged", "upcoming"):
        css_class = "badge-amber"
        icon = "⚡"
    elif st_clean in ("blocked", "expired", "broken", "rejected_by_merchant", "failed_again", "failed"):
        css_class = "badge-red"
        icon = "✕"
    elif st_clean in ("actioned", "action_taken", "detection"):
        css_class = "badge-blue"
        icon = "●"
    else:
        css_class = "badge-gray"
        icon = "○"

    label = status.replace("_", " ").title() if status else "Unknown"
    return f'<span class="status-badge {css_class}">{icon} {label}</span>'


def render_stat_card(
    label: str,
    value: str,
    subtext: str = "",
    delta: str = "",
    badge: str = "",
) -> None:
    """Render a styled metric card container."""
    delta_html = f'<div class="stat-delta">▲ {delta}</div>' if delta else ""
    subtext_html = f'<div class="stat-subtext">{subtext}</div>' if subtext else ""
    badge_html = f'<div>{badge}</div>' if badge else ""

    html(
        f"""
        <div class="revive-card">
            <div class="revive-card-header">
                <div class="stat-label">{label}</div>
                {badge_html}
            </div>
            <div class="stat-value">{value}</div>
            {delta_html}
            {subtext_html}
        </div>
        """
    )


def render_page_header(title: str, subtitle: str) -> None:
    """Render consistent page header with latest pipeline run metadata."""
    inject_custom_css()
    last_run = "Never"
    with contextlib.suppress(Exception):
        with get_connection() as conn:
            row = conn.execute(
                "SELECT timestamp FROM audit_log ORDER BY timestamp DESC LIMIT 1"
            ).fetchone()
            if row and row[0]:
                last_run = str(row[0])

    html(
        f"""
        <div class="app-header-container">
            <div>
                <h1 class="app-header-title">{title}</h1>
                <div class="app-header-tagline">{subtitle}</div>
            </div>
            <div class="app-header-badge">
                <span>⚡ Last run: {last_run}</span>
            </div>
        </div>
        """
    )


def render_confidence_meter(confidence: float, provider: str = "") -> None:
    """Render an upgraded confidence meter with colored fill and threshold tags."""
    conf = max(0.0, min(1.0, float(confidence or 0.0)))
    pct_val = conf * 100

    if conf >= 0.70:
        color = "#10b981"
        threshold_tag = '<span class="status-badge badge-green">✓ High (>70%)</span>'
    elif conf >= 0.50:
        color = "#f59e0b"
        threshold_tag = '<span class="status-badge badge-amber">⚡ Medium (50-70%)</span>'
    else:
        color = "#ef4444"
        threshold_tag = '<span class="status-badge badge-red">✕ Low (<50%)</span>'

    policy_tag = (
        '<span style="font-size: 0.75rem; color: #10b981; font-weight:600;">✓ Auto-Approval Eligible (≥60%)</span>'
        if conf >= 0.60
        else '<span style="font-size: 0.75rem; color: #f59e0b; font-weight:600;">⚠️ Escalates to Human Review (<60%)</span>'
    )

    provider_tag = f'<span style="font-size: 0.75rem; color: #94a3b8;">Provider: <strong style="color:#38bdf8;">{provider.upper()}</strong></span>' if provider else ""

    html(
        f"""
        <div class="confidence-box">
            <div class="confidence-header">
                <div>
                    <span style="font-size:0.75rem; font-weight:700; text-transform:uppercase; color:#94a3b8; letter-spacing:0.05em;">AI Diagnosis Confidence</span>
                    <span style="font-size:1.1rem; font-weight:800; color:{color}; margin-left:8px;">{pct_val:.1f}%</span>
                </div>
                <div>{threshold_tag}</div>
            </div>
            <div class="confidence-meter-bg">
                <div class="confidence-meter-fill" style="width: {pct_val}%; background-color: {color};"></div>
            </div>
            <div style="display:flex; justify-content:space-between; align-items:center; margin-top:8px;">
                <div>{policy_tag}</div>
                <div>{provider_tag}</div>
            </div>
        </div>
        """
    )


def render_pipeline_mini_timeline(risk_id: str) -> None:
    """Render a compact horizontal 5-stage mini timeline widget from audit logs."""
    stages_order = [
        ("detection", "Detected", "🔍"),
        ("diagnosis", "Diagnosed", "🤖"),
        ("policy", "Policy", "⚖️"),
        ("execution", "Action", "🚀"),
        ("outcome", "Outcome", "💰"),
    ]
    with get_connection() as conn:
        logs = conn.execute(
            """
            SELECT stage, actor, timestamp, output_snapshot
            FROM audit_log
            WHERE risk_id = ?
            ORDER BY timestamp ASC
            """,
            (risk_id,),
        ).fetchall()

    logged_stages = {row["stage"]: row for row in logs}

    html_steps = []
    for idx, (stg_key, stg_label, stg_icon) in enumerate(stages_order):
        if stg_key in logged_stages:
            row = logged_stages[stg_key]
            time_str = str(row["timestamp"]).split(" ")[-1] if " " in str(row["timestamp"]) else str(row["timestamp"])
            bg_color = "rgba(16, 185, 129, 0.2)"
            border = "1px solid #10b981"
            icon_color = "#34d399"
        else:
            time_str = "Pending"
            bg_color = "rgba(51, 65, 85, 0.3)"
            border = "1px solid #334155"
            icon_color = "#64748b"

        step_html = f"""
        <div class="timeline-step">
            <div class="timeline-icon" style="background:{bg_color}; border:{border}; color:{icon_color};">
                {stg_icon}
            </div>
            <div class="timeline-label">{stg_label}</div>
            <div class="timeline-time">{time_str}</div>
        </div>
        """
        html_steps.append(step_html)
        if idx < len(stages_order) - 1:
            html_steps.append('<div class="timeline-connector"></div>')

    html(
        f"""
        <div style="margin: 10px 0 16px 0;">
            <span style="font-size:0.75rem; font-weight:700; text-transform:uppercase; color:#94a3b8; letter-spacing:0.05em;">Lifecycle Provenance Timeline</span>
            <div class="timeline-bar">
                {''.join(html_steps)}
            </div>
        </div>
        """
    )


def render_overview() -> None:
    render_page_header("ReviveAI", "Autonomous, policy-governed revenue recovery control center")

    with get_connection() as connection:
        # Funnel stage queries
        c_detected = connection.execute(
            "SELECT COUNT(*), COALESCE(SUM(amount_at_risk), 0) FROM risk_events"
        ).fetchone()

        c_diagnosed = connection.execute(
            """
            SELECT COUNT(DISTINCT r.risk_id), COALESCE(SUM(r.amount_at_risk), 0)
            FROM risk_events r
            JOIN diagnoses d ON d.risk_id = r.risk_id
            """
        ).fetchone()

        c_approved = connection.execute(
            """
            SELECT COUNT(DISTINCT r.risk_id), COALESCE(SUM(r.amount_at_risk), 0)
            FROM risk_events r
            WHERE r.risk_id IN (
                SELECT p.risk_id FROM policy_decisions p
                WHERE p.decision IN ('auto_approved', 'modified')
                   OR (p.decision = 'needs_human_approval' AND EXISTS (
                       SELECT 1 FROM actions_taken a WHERE a.decision_id = p.decision_id
                   ))
            )
            """
        ).fetchone()

        c_actioned = connection.execute(
            """
            SELECT COUNT(DISTINCT a.action_id), COALESCE(SUM(r.amount_at_risk), 0)
            FROM actions_taken a
            JOIN risk_events r ON r.risk_id = a.risk_id
            """
        ).fetchone()

        c_recovered = connection.execute(
            """
            SELECT COUNT(*), COALESCE(SUM(amount_recovered), 0)
            FROM outcomes
            WHERE outcome_type = 'paid'
            """
        ).fetchone()

        c_blocked_esc = connection.execute(
            """
            SELECT COUNT(DISTINCT r.risk_id), COALESCE(SUM(r.amount_at_risk), 0)
            FROM risk_events r
            WHERE r.risk_id IN (
                SELECT p.risk_id FROM policy_decisions p
                WHERE p.decision = 'blocked'
                   OR (p.decision = 'needs_human_approval' AND NOT EXISTS (
                       SELECT 1 FROM actions_taken a WHERE a.decision_id = p.decision_id
                   ))
            ) OR r.status IN ('expired', 'rejected_by_merchant')
            """
        ).fetchone()

        # Impact Calculator metrics
        estimated_loss = float(c_detected[1] or 0)
        actual_recovered = float(c_recovered[1] or 0)
        revenue_saved = actual_recovered
        recovery_roi = (actual_recovered / estimated_loss * 100) if estimated_loss else 0.0

        # Existing category & decision rows
        category_rows = connection.execute(
            """
            SELECT risk_category, COALESCE(SUM(amount_at_risk), 0) AS amount
            FROM risk_events
            GROUP BY risk_category
            ORDER BY amount DESC
            """
        ).fetchall()
        decision_rows = connection.execute(
            """
            SELECT decision, COUNT(*) AS count
            FROM policy_decisions
            GROUP BY decision
            """
        ).fetchall()

    # -------------------------------------------------------------
    # 1. RECOVERY FUNNEL VISUALIZATION
    # -------------------------------------------------------------
    st.subheader("Recovery Funnel")
    st.caption("End-to-end pipeline progression: from initial leak detection to settled revenue recovery")

    funnel_stages = [
        {"Stage": "1. Detected", "Count": int(c_detected[0] or 0), "Amount": float(c_detected[1] or 0)},
        {"Stage": "2. Diagnosed", "Count": int(c_diagnosed[0] or 0), "Amount": float(c_diagnosed[1] or 0)},
        {"Stage": "3. Approved", "Count": int(c_approved[0] or 0), "Amount": float(c_approved[1] or 0)},
        {"Stage": "4. Action Taken", "Count": int(c_actioned[0] or 0), "Amount": float(c_actioned[1] or 0)},
        {"Stage": "5. Recovered", "Count": int(c_recovered[0] or 0), "Amount": float(c_recovered[1] or 0)},
        {"Stage": "Escalated/Blocked", "Count": int(c_blocked_esc[0] or 0), "Amount": float(c_blocked_esc[1] or 0)},
    ]
    df_funnel = pd.DataFrame(funnel_stages)

    funnel_cols = st.columns(2)
    with funnel_cols[0]:
        st.markdown("**Transaction Volume (Count)**")
        st.bar_chart(df_funnel.set_index("Stage")["Count"], horizontal=True, color="#0284c7")

    with funnel_cols[1]:
        st.markdown("**Revenue Value (₹)**")
        st.bar_chart(df_funnel.set_index("Stage")["Amount"], horizontal=True, color="#0f766e")

    # Funnel details summary table
    funnel_display = []
    base_count = df_funnel.loc[0, "Count"] or 1
    for item in funnel_stages:
        conv_pct = (item["Count"] / base_count * 100) if item["Stage"] != "Escalated/Blocked" else (item["Count"] / base_count * 100)
        funnel_display.append({
            "Pipeline Stage": item["Stage"],
            "Transactions": f"{item['Count']:,}",
            "Value at Stage": money(item["Amount"]),
            "Stage Share": f"{conv_pct:.1f}%",
        })
    st.dataframe(pd.DataFrame(funnel_display), use_container_width=True, hide_index=True)

    st.divider()

    # -------------------------------------------------------------
    # 2. RECOVERY IMPACT CALCULATOR (Styled Card Containers)
    # -------------------------------------------------------------
    st.subheader("Recovery Impact Calculator")
    st.caption("Business financial impact vs baseline inaction scenario")
    
    impact_cols = st.columns(4)
    with impact_cols[0]:
        render_stat_card(
            "Estimated Loss (No Recovery)",
            money(estimated_loss),
            subtext="Baseline inaction loss scenario",
            badge='<span class="status-badge badge-red">At Risk</span>',
        )
    with impact_cols[1]:
        render_stat_card(
            "Actual Revenue Recovered",
            money(actual_recovered),
            subtext=f"{int(c_recovered[0] or 0)} transactions settled",
            badge='<span class="status-badge badge-green">Settled</span>',
        )
    with impact_cols[2]:
        render_stat_card(
            "Revenue Saved",
            money(revenue_saved),
            delta=money(revenue_saved),
            subtext="Net preserved merchant revenue",
            badge='<span class="status-badge badge-green">Preserved</span>',
        )
    with impact_cols[3]:
        render_stat_card(
            "Recovery ROI",
            pct(recovery_roi),
            subtext="Pipeline recovery efficiency",
            badge='<span class="status-badge badge-blue">ROI</span>',
        )

    st.divider()

    # -------------------------------------------------------------
    # Category and Policy Decision Breakdowns
    # -------------------------------------------------------------
    chart_columns = st.columns(2)
    with chart_columns[0]:
        st.subheader("Amount at Risk by Category")
        if category_rows:
            st.bar_chart(
                {
                    row["risk_category"]: float(row["amount"])
                    for row in category_rows
                },
                horizontal=True,
            )
        else:
            st.info("No risk events available yet.")
    with chart_columns[1]:
        st.subheader("Policy Decision Breakdown")
        if decision_rows:
            pie_data = [
                {"decision": row["decision"], "count": row["count"]}
                for row in decision_rows
            ]
            st.vega_lite_chart(
                pie_data,
                {
                    "mark": {"type": "arc", "tooltip": True},
                    "encoding": {
                        "theta": {"field": "count", "type": "quantitative"},
                        "color": {
                            "field": "decision",
                            "type": "nominal",
                            "scale": {
                                "domain": [
                                    "auto_approved",
                                    "modified",
                                    "blocked",
                                    "needs_human_approval",
                                ],
                                "range": ["#0f766e", "#d97706", "#dc2626", "#f59e0b"],
                            },
                        },
                    },
                },
                use_container_width=True,
            )
        else:
            st.info("No policy decisions available yet.")

    st.divider()
    st.subheader("Pipeline controls")
    st.caption(
        "Detection, diagnosis, policy, and executor stages run sequentially. "
        "Provider calls remain optional and all messages are simulated."
    )
    if st.button("Run Full Pipeline", type="primary", use_container_width=True):
        run_full_pipeline()
        st.rerun()


def render_risk_queue() -> None:
    render_page_header("Risk Queue", "Prioritized revenue risks detected from payment events")
    
    all_categories = [
        row["risk_category"]
        for row in query_rows(
            "SELECT DISTINCT risk_category FROM risk_events ORDER BY risk_category"
        )
    ]
    all_statuses = [
        row["status"]
        for row in query_rows(
            "SELECT DISTINCT status FROM risk_events ORDER BY status"
        )
    ]
    filter_columns = st.columns(2)
    selected_categories = filter_columns[0].multiselect(
        "Risk category", all_categories, default=all_categories
    )
    selected_statuses = filter_columns[1].multiselect(
        "Status", all_statuses, default=all_statuses
    )
    rows = risk_queue_rows(selected_categories, selected_statuses)
    if not rows:
        st.info("No risk events match the current filters.")
        return

    def get_priority_badge(score: float) -> str:
        if score > 0.8:
            return "🔴 Critical"
        elif score >= 0.6:
            return "🟠 High"
        elif score >= 0.3:
            return "🟡 Medium"
        else:
            return "🟢 Low"

    queue_display = [
        {
            "Priority": get_priority_badge(float(row["risk_score"] or 0)),
            "Risk ID": short_id(row["risk_id"]),
            "Customer": row["customer_name"],
            "Category": row["risk_category"].replace("_", " ").title(),
            "Amount at Risk": money(row["amount_at_risk"]),
            "Risk Score": f"{float(row['risk_score'] or 0):.3f}",
            "Status": row["status"].replace("_", " ").title(),
            "Detected At": str(row["detected_at"]).split(".")[0],
        }
        for row in rows
    ]
    st.dataframe(pd.DataFrame(queue_display), use_container_width=True, hide_index=True)

    st.divider()
    id_map = {row["risk_id"]: f"{row['customer_name']} ({money(row['amount_at_risk'])}) · {short_id(row['risk_id'])}" for row in rows}
    selected_risk_id = st.selectbox(
        "Inspect Risk Event Details",
        [row["risk_id"] for row in rows],
        format_func=lambda x: id_map.get(x, x),
    )
    detail = dict(
        query_rows(
            """
            SELECT
                r.*, re.event_type, re.amount, re.currency, re.payment_method,
                re.failure_code, re.occurred_at, re.raw_metadata,
                c.name AS customer_name, c.email, c.opted_out,
                c.contact_count_last_7d
            FROM risk_events r
            JOIN revenue_events re ON re.event_id = r.event_id
            JOIN customers c ON c.customer_id = r.customer_id
            WHERE r.risk_id = ?
            """,
            (selected_risk_id,),
        )[0]
    )
    audit_reason = query_rows(
        """
        SELECT output_snapshot FROM audit_log
        WHERE risk_id = ? AND stage = 'detection'
        ORDER BY timestamp DESC LIMIT 1
        """,
        (selected_risk_id,),
    )
    
    st.subheader(f"Risk Detail · {detail['customer_name']}")
    detail_columns = st.columns(4)
    with detail_columns[0]:
        render_stat_card(
            "Amount at Risk",
            money(detail["amount_at_risk"]),
            subtext=f"Event: {detail['event_type'].replace('_', ' ').title()}",
            badge='<span class="status-badge badge-red">At Risk</span>',
        )
    with detail_columns[1]:
        score_val = float(detail["risk_score"] or 0)
        render_stat_card(
            "Risk Score",
            f"{score_val:.3f}",
            subtext=get_priority_badge(score_val),
            badge='<span class="status-badge badge-amber">Severity</span>',
        )
    with detail_columns[2]:
        render_stat_card(
            "Current Status",
            detail["status"].replace("_", " ").title(),
            subtext=f"Contacts (7d): {detail.get('contact_count_last_7d', 0)}",
            badge=render_badge(detail["status"]),
        )
    with detail_columns[3]:
        method_str = (detail.get("payment_method") or "UPI / Card").upper()
        render_stat_card(
            "Payment Method",
            method_str,
            subtext=detail.get("failure_code") or "Transaction failure",
            badge='<span class="status-badge badge-blue">Gateway</span>',
        )

    # Pipeline Mini Timeline
    render_pipeline_mini_timeline(selected_risk_id)

    # Detection logic card
    detection_msg = "Deterministic detector matched rules"
    if audit_reason:
        try:
            detection_msg = json.loads(audit_reason[0]["output_snapshot"]).get(
                "detection_reason", detection_msg
            )
        except (TypeError, json.JSONDecodeError):
            pass

    html(
        f"""
        <div class="revive-card" style="margin-top: 4px;">
            <div class="revive-card-header">
                <div class="stat-label">Deterministic Detection Formula & Logic ({detail['risk_category'].replace('_', ' ').title()})</div>
                <div style="font-size:0.75rem; color:#94a3b8;">Policy-Engine Evaluated</div>
            </div>
            <div style="font-size: 0.95rem; font-family: monospace; color: #38bdf8; background: #0f172a; padding: 8px 12px; border-radius: 6px; border: 1px solid #1e293b;">
                Formula: {RISK_FORMULAS.get(detail['risk_category'], 'min(1, failure_severity + amount_factor)')}
            </div>
            <div style="font-size: 0.9rem; color: #cbd5e1; margin-top: 8px;">
                💡 <strong>Explanation:</strong> {detection_msg}
            </div>
        </div>
        """
    )

    if detail.get("promise_to_pay_date"):
        html(
            f"""
            <div class="revive-card" style="background: linear-gradient(145deg, #1e293b 0%, #1a2e1d 100%); border-left: 4px solid #10b981;">
                <div class="stat-label" style="color:#34d399;">🤝 Active Promise-to-Pay Logged</div>
                <div style="font-size: 1.1rem; font-weight:700; color: #f8fafc; margin-top:4px;">
                    Promised Payment Date: {detail['promise_to_pay_date']}
                </div>
            </div>
            """
        )

    with st.expander("🤝 Log / Update Promise to Pay"):
        ptp_col1, ptp_col2 = st.columns([2, 1])
        default_ptp = date.today() + timedelta(days=3)
        ptp_date = ptp_col1.date_input(
            "Promised Payment Date",
            value=default_ptp,
            key=f"ptp_date_rq_{selected_risk_id}",
        )
        if ptp_col2.button("Log Promise to Pay", key=f"btn_ptp_rq_{selected_risk_id}"):
            log_promise_to_pay(selected_risk_id, ptp_date)
            st.success(f"Promise to pay logged for {ptp_date}!")
            st.rerun()

    with st.expander("🔍 Original Revenue Event Raw Payload"):
        st.write(f"**Full Risk ID:** `{detail['risk_id']}`")
        original = dict(detail)
        try:
            original["raw_metadata"] = json.loads(original["raw_metadata"] or "{}")
        except (TypeError, json.JSONDecodeError):
            pass
        st.json(original)

    if st.button("View AI Diagnosis →", key=f"diagnosis_link_{selected_risk_id}", type="primary"):
        st.session_state["selected_risk_id"] = selected_risk_id
        st.session_state["page"] = "Diagnosis View"
        st.rerun()


def render_diagnosis_view() -> None:
    render_page_header("Diagnosis View", "Advisory LLM provider output · Policy Engine retains final authority")
    
    rows = query_rows(
        """
        SELECT
            d.*, r.risk_category, r.amount_at_risk, r.status,
            c.name AS customer_name, c.preferred_language, c.email
        FROM diagnoses d
        JOIN risk_events r ON r.risk_id = d.risk_id
        JOIN customers c ON c.customer_id = r.customer_id
        ORDER BY d.diagnosed_at DESC, d.risk_id
        """
    )
    if not rows:
        st.info("No diagnoses available. Run the pipeline from Overview.")
        return

    # Selectbox with short_id + customer name
    id_map = {row["risk_id"]: f"{row['customer_name']} ({money(row['amount_at_risk'])}) · {short_id(row['risk_id'])}" for row in rows}
    ids = [row["risk_id"] for row in rows]
    default_id = st.session_state.pop("selected_risk_id", ids[0])
    default_index = ids.index(default_id) if default_id in ids else 0

    selected = st.selectbox(
        "Select Risk Event",
        ids,
        index=default_index,
        format_func=lambda x: id_map.get(x, x),
    )
    diagnosis = dict(next(row for row in rows if row["risk_id"] == selected))

    # Top Row: 4 Metric Cards
    top_cols = st.columns(4)
    with top_cols[0]:
        render_stat_card(
            "Customer",
            diagnosis["customer_name"],
            subtext=diagnosis.get("email", ""),
            badge=render_badge(diagnosis["status"]),
        )
    with top_cols[1]:
        render_stat_card(
            "Amount at Risk",
            money(diagnosis["amount_at_risk"]),
            subtext=f"Category: {str(diagnosis['risk_category']).replace('_', ' ').title()}",
            badge='<span class="status-badge badge-red">At Risk</span>',
        )
    with top_cols[2]:
        render_stat_card(
            "Proposed Action",
            diagnosis["recommended_action"].replace("_", " ").title(),
            subtext=f"Channel: {diagnosis.get('recommended_channel', 'email').upper()}",
            badge='<span class="status-badge badge-blue">Advisory</span>',
        )
    with top_cols[3]:
        discount_val = float(diagnosis.get("recommended_discount_pct", 0) or 0)
        render_stat_card(
            "Proposed Discount",
            f"{discount_val:g}%",
            subtext="Policy cap: 10% max",
            badge='<span class="status-badge badge-amber">Discount</span>' if discount_val > 0 else '<span class="status-badge badge-gray">No Disc</span>',
        )

    # Full Width Root Cause Card (Prevents Text Truncation)
    html(
        f"""
        <div class="revive-card" style="margin-top: 4px;">
            <div class="revive-card-header">
                <div class="stat-label">AI Root Cause Diagnosis</div>
                <div style="font-size:0.75rem; color:#94a3b8;">Full Analysis</div>
            </div>
            <div style="font-size: 1.05rem; font-weight: 500; color: #f1f5f9; line-height: 1.5;">
                "{diagnosis['root_cause']}"
            </div>
        </div>
        """
    )

    # Upgraded Confidence Meter
    confidence = float(diagnosis.get("confidence", 0) or 0)
    provider_name = str(diagnosis.get("provider_used", "LLM"))
    render_confidence_meter(confidence, provider=provider_name)

    # Mini Lifecycle Provenance Timeline
    render_pipeline_mini_timeline(selected)

    # Localized Outreach Copy Box (Always visible)
    cust_msg = diagnosis.get("customer_message_hinglish")
    pref_lang = diagnosis.get("preferred_language") or "hi-en"
    if not cust_msg:
        # Fallback generated Hinglish preview if customer preferred language was English in seed data
        name_val = diagnosis.get('customer_name', 'there')
        amt_val = float(diagnosis.get('amount_at_risk', 0) or 0)
        cust_msg = f"Hi {name_val}, aapka ₹{amt_val:,.0f} ka payment complete nahi ho paya tha. Niche diye link se turant complete karein."

    badge_label = "Hinglish (hi-en)" if pref_lang == "hi-en" else "English (en) / Hinglish"
    html(
        f"""
        <div class="revive-card" style="background: linear-gradient(145deg, #1e293b 0%, #0d2847 100%); border-left: 4px solid #38bdf8; margin-top: 12px;">
            <div class="revive-card-header">
                <div class="stat-label" style="color:#38bdf8;">💬 Localized Outreach Copy ({badge_label})</div>
                <div><span class="status-badge badge-blue">Ready to Dispatch</span></div>
            </div>
            <div style="font-size: 1.05rem; color: #f8fafc; font-style: italic; margin-top: 6px; line-height: 1.5;">
                "{cust_msg}"
            </div>
        </div>
        """
    )

    # Dispatched Action / Live Razorpay Link box
    action_info = query_rows(
        """
        SELECT action_id, action_type, channel, message_sent, executed_at, simulated
        FROM actions_taken
        WHERE risk_id = ?
        ORDER BY executed_at DESC LIMIT 1
        """,
        (selected,),
    )
    if action_info:
        act = dict(action_info[0])
        if act.get("simulated") == 0 and act.get("message_sent", "").startswith("http"):
            html(
                f"""
                <div class="revive-card" style="background: linear-gradient(145deg, #1e293b 0%, #0d2847 100%); border-left: 4px solid #10b981; margin-top: 12px;">
                    <div class="revive-card-header">
                        <div class="stat-label" style="color:#34d399;">🔗 Live Razorpay Payment Link Generated</div>
                        <div><span class="status-badge badge-green">LIVE (Razorpay Test Mode)</span></div>
                    </div>
                    <div style="font-size: 1.05rem; color: #f8fafc; margin-top: 6px;">
                        Payment Checkout Link: <a href="{act['message_sent']}" target="_blank" style="color: #38bdf8; font-weight:700; text-decoration: underline;">{act['message_sent']} ↗</a>
                    </div>
                    <div style="font-size: 0.8rem; color: #94a3b8; margin-top: 4px;">
                        Created via Razorpay Test Mode API · Fully clickable and payable in sandbox
                    </div>
                </div>
                """
            )
        else:
            html(
                f"""
                <div class="revive-card" style="margin-top: 12px;">
                    <div class="revive-card-header">
                        <div class="stat-label">Dispatched Outreach Payload ({act.get('channel', 'email').upper()})</div>
                        <div><span class="status-badge badge-gray">Simulated Dispatch</span></div>
                    </div>
                    <div style="font-size: 0.95rem; color: #cbd5e1; margin-top: 4px;">
                        "{act['message_sent']}"
                    </div>
                </div>
                """
            )

    with st.expander("🔍 Inspect Full Technical Details & Raw LLM Payload"):
        st.write(f"**Full Risk ID:** `{diagnosis['risk_id']}`")
        st.write(f"**Diagnosis ID:** `{diagnosis['diagnosis_id']}`")
        try:
            st.json(json.loads(diagnosis["llm_raw_response"] or "{}"))
        except (TypeError, json.JSONDecodeError):
            st.code(diagnosis["llm_raw_response"] or "")


def render_recovery_queue() -> None:
    render_page_header("Recovery Queue", "Every policy decision, clipped discount, and hard business constraint")
    rows = query_rows(
        """
        SELECT
            p.decision_id, p.risk_id, c.name AS customer_name,
            r.amount_at_risk, p.decision, p.final_action,
            p.final_discount_pct, p.reason, p.decided_at,
            a.action_id, a.message_sent, a.simulated
        FROM policy_decisions p
        JOIN risk_events r ON r.risk_id = p.risk_id
        JOIN customers c ON c.customer_id = r.customer_id
        LEFT JOIN actions_taken a ON a.decision_id = p.decision_id
        ORDER BY p.decided_at DESC
        """
    )
    if not rows:
        st.info("No policy decisions available yet.")
        return

    # Summary strip
    counts = Counter(row["decision"] for row in rows)
    dec_cols = st.columns(4)
    with dec_cols[0]:
        render_stat_card("Auto Approved", str(counts.get("auto_approved", 0)), subtext="Passed policy thresholds", badge='<span class="status-badge badge-green">Approved</span>')
    with dec_cols[1]:
        render_stat_card("Modified / Clipped", str(counts.get("modified", 0)), subtext="Discounts capped to 10%", badge='<span class="status-badge badge-amber">Modified</span>')
    with dec_cols[2]:
        render_stat_card("Needs Approval", str(counts.get("needs_human_approval", 0)), subtext="High value / Low confidence", badge='<span class="status-badge badge-amber">Escalated</span>')
    with dec_cols[3]:
        render_stat_card("Blocked", str(counts.get("blocked", 0)), subtext="Safety constraints triggered", badge='<span class="status-badge badge-red">Blocked</span>')

    st.divider()
    st.subheader("Policy Decision Ledger")

    queue_display = []
    live_links = []
    for row in rows:
        sim_status = "Pending"
        if row["action_id"]:
            if row["simulated"] == 0 and row["message_sent"]:
                sim_status = "LIVE (Razorpay)"
                live_links.append((row["customer_name"], row["risk_id"], row["message_sent"]))
            else:
                sim_status = "Simulated"

        queue_display.append({
            "Decision": row["decision"].replace("_", " ").title(),
            "Customer": row["customer_name"],
            "Amount": money(row["amount_at_risk"]),
            "Action": row["final_action"].replace("_", " ").title(),
            "Discount": f"{float(row['final_discount_pct'] or 0):g}%",
            "Execution Mode": sim_status,
            "Policy Reason": row["reason"],
            "Risk ID": short_id(row["risk_id"]),
            "Decided At": str(row["decided_at"]).split(".")[0],
        })

    dataframe = pd.DataFrame(queue_display)
    st.dataframe(dataframe, use_container_width=True, hide_index=True)

    if live_links:
        st.markdown("#### ⚡ Live Razorpay Test Mode Payment Links")
        for cust, r_id, link in live_links:
            html(
                f"""
                <div style="background: #0f172a; border: 1px solid #10b981; border-radius: 8px; padding: 12px 16px; margin-bottom: 8px; display:flex; justify-content:space-between; align-items:center;">
                    <div>
                        <strong style="color:#f8fafc;">{cust}</strong> (<code style="color:#38bdf8;">{short_id(r_id)}</code>): 
                        <a href="{link}" target="_blank" style="color: #38bdf8; font-weight:700; text-decoration: underline; margin-left: 8px;">{link} ↗</a>
                    </div>
                    <div><span class="status-badge badge-green">LIVE (Razorpay Test Mode)</span></div>
                </div>
                """
            )


def reject_decision(decision: sqlite3.Row) -> None:
    with get_connection() as connection:
        connection.execute(
            """
            UPDATE risk_events
            SET status = 'rejected_by_merchant'
            WHERE risk_id = ?
            """,
            (decision["risk_id"],),
        )
        connection.execute(
            """
            INSERT INTO audit_log (
                log_id, risk_id, stage, actor, input_snapshot,
                output_snapshot, timestamp
            ) VALUES (?, ?, 'policy', 'human:demo_merchant', ?, ?, datetime('now'))
            """,
            (
                f"audit_rejected_{decision['decision_id']}",
                decision["risk_id"],
                json.dumps({"decision_id": decision["decision_id"]}),
                json.dumps(
                    {
                        "decision": "rejected_by_merchant",
                        "actor": "human:demo_merchant",
                    }
                ),
            ),
        )
        connection.commit()


def render_approvals() -> None:
    render_page_header("Approvals Inbox", "Human-in-the-loop review required before executing sensitive recovery actions")
    rows = query_rows(
        """
        SELECT
            p.*, r.amount_at_risk, r.customer_id, r.promise_to_pay_date,
            c.name AS customer_name, c.email,
            d.recommended_channel AS channel, d.root_cause
        FROM policy_decisions p
        JOIN risk_events r ON r.risk_id = p.risk_id
        JOIN customers c ON c.customer_id = r.customer_id
        LEFT JOIN diagnoses d ON d.diagnosis_id = p.diagnosis_id
        WHERE p.decision = 'needs_human_approval'
          AND NOT EXISTS (
              SELECT 1 FROM actions_taken a
              WHERE a.decision_id = p.decision_id
          )
          AND r.status <> 'rejected_by_merchant'
        ORDER BY p.decided_at ASC
        """
    )
    if not rows:
        st.info("✅ No pending human approvals. All actionable cases are processed.")
        return

    st.caption(f"Showing {len(rows)} pending action(s) awaiting merchant sign-off")
    for decision in rows:
        html(
            f"""
            <div class="revive-card" style="border-left: 4px solid #f59e0b;">
                <div class="revive-card-header">
                    <div>
                        <span class="stat-label">Pending Approval</span> · 
                        <strong style="color:#f8fafc; font-size:1.1rem;">{decision['customer_name']}</strong> 
                        <span style="color:#94a3b8; font-size:0.85rem;">({decision['email']})</span>
                    </div>
                    <div><span class="status-badge badge-amber">⚠️ High Value / Flagged</span></div>
                </div>
                <div style="display:flex; justify-content:space-between; align-items:center; margin: 8px 0;">
                    <div>
                        <span style="font-size:0.8rem; text-transform:uppercase; color:#94a3b8; font-weight:700;">Amount at Risk: </span>
                        <span style="font-size:1.25rem; font-weight:800; color:#34d399;">{money(decision['amount_at_risk'])}</span>
                    </div>
                    <div>
                        <span style="font-size:0.8rem; text-transform:uppercase; color:#94a3b8; font-weight:700;">Proposed Action: </span>
                        <span style="font-size:1rem; font-weight:700; color:#38bdf8;">{decision['final_action'].replace('_', ' ').title()}</span>
                        <span style="font-size:0.8rem; color:#94a3b8;">via {str(decision['channel'] or 'email').upper()}</span>
                    </div>
                </div>
                <div style="font-size:0.9rem; color:#cbd5e1; background:rgba(15, 23, 42, 0.6); padding:8px 12px; border-radius:6px; border: 1px solid #334155;">
                    ⚖️ <strong>Policy Reason:</strong> {decision['reason']}
                </div>
            </div>
            """
        )

        btn_cols = st.columns([1, 1, 4])
        with btn_cols[0]:
            if st.button("✓ Approve & Execute", key=f"approve_{decision['decision_id']}", type="primary", use_container_width=True):
                with get_connection() as connection:
                    diag_row = connection.execute(
                        "SELECT recommended_action, recommended_channel, recommended_discount_pct FROM diagnoses WHERE diagnosis_id = ?",
                        (decision["diagnosis_id"],),
                    ).fetchone()
                    approved_action = (diag_row["recommended_action"] if diag_row and diag_row["recommended_action"] != "escalate_to_human" else None) or "send_payment_link"
                    approved_discount = float(diag_row["recommended_discount_pct"] if diag_row else 0)

                    connection.execute(
                        """
                        UPDATE policy_decisions 
                        SET decision = 'human_approved',
                            final_action = ?,
                            final_discount_pct = min(?, 10.0),
                            reason = reason || ' · Approved by merchant'
                        WHERE decision_id = ?
                        """,
                        (approved_action, approved_discount, decision["decision_id"]),
                    )
                    connection.execute(
                        """
                        INSERT INTO audit_log (
                            log_id, risk_id, stage, actor, input_snapshot, output_snapshot, timestamp
                        ) VALUES (?, ?, 'policy', 'human:merchant_operator', ?, ?, datetime('now'))
                        """,
                        (
                            f"audit_human_approved_{decision['decision_id']}",
                            decision["risk_id"],
                            json.dumps({"decision_id": decision["decision_id"]}),
                            json.dumps({"decision": "human_approved", "final_action": approved_action}),
                        ),
                    )
                    connection.commit()

                    from executor.action_executor import execute_action, simulate_outcomes
                    result = execute_action(decision["decision_id"], connection=connection)
                    if result.get("status") == "executed":
                        simulate_outcomes(connection, action_id=result["action_id"], seed=42)
                st.success(f"Approved recovery action for {decision['customer_name']}!")
                st.rerun()
        with btn_cols[1]:
            if st.button("✕ Reject", key=f"reject_{decision['decision_id']}", use_container_width=True):
                reject_decision(decision)
                st.warning("Action rejected by merchant.")
                st.rerun()

        with st.expander(f"🤝 Log Promise to Pay / Technical Details for {decision['customer_name']}"):
            ptp_c1, ptp_c2 = st.columns([2, 1])
            default_ptp = date.today() + timedelta(days=3)
            ptp_dt = ptp_c1.date_input("Promise Date", value=default_ptp, key=f"ptp_app_{decision['decision_id']}")
            if ptp_c2.button("Log Promise", key=f"btn_ptp_app_{decision['decision_id']}"):
                log_promise_to_pay(decision["risk_id"], ptp_dt)
                st.success(f"Promise logged for {ptp_dt}!")
                st.rerun()
            st.write(f"**Full Risk ID:** `{decision['risk_id']}`")
            st.write(f"**Decision ID:** `{decision['decision_id']}`")
        html("<div style='margin-bottom:16px;'></div>")


def check_and_escalate_broken_promises() -> int:
    """Escalate all overdue unresolved promises and record in audit trail."""
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with get_connection() as connection:
        broken_rows = connection.execute(
            """
            SELECT r.*, c.name as customer_name
            FROM risk_events r
            JOIN customers c ON c.customer_id = r.customer_id
            WHERE r.promise_to_pay_date IS NOT NULL
              AND date(r.promise_to_pay_date) < date(?)
              AND r.status NOT IN ('resolved', 'expired', 'rejected_by_merchant')
            """,
            (today_str,),
        ).fetchall()

        count = 0
        now_ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        for row in broken_rows:
            current_score = float(row["risk_score"] or 0.5)
            escalated_score = min(1.0, round(current_score * 1.25 + 0.15, 3))

            # Escalate the risk event: mark force_escalate = 1, status = 'diagnosed'
            connection.execute(
                """
                UPDATE risk_events
                SET force_escalate = 1,
                    status = 'diagnosed',
                    risk_score = ?
                WHERE risk_id = ?
                """,
                (escalated_score, row["risk_id"]),
            )

            # Insert broken promise escalation into audit log
            connection.execute(
                """
                INSERT INTO audit_log (
                    log_id, risk_id, stage, actor, input_snapshot, output_snapshot, timestamp
                ) VALUES (?, ?, 'broken_promise_escalated', 'system:ptp_checker', ?, ?, ?)
                """,
                (
                    f"audit_broken_promise_{row['risk_id']}_{uuid.uuid4().hex[:8]}",
                    row["risk_id"],
                    json.dumps({
                        "risk_id": row["risk_id"],
                        "customer_name": row["customer_name"],
                        "promise_to_pay_date": str(row["promise_to_pay_date"]),
                        "previous_risk_score": current_score,
                    }),
                    json.dumps({
                        "status": "escalated_to_human",
                        "escalated_risk_score": escalated_score,
                        "reason": "customer promise date expired without resolution",
                        "force_escalate": 1,
                    }),
                    now_ts,
                ),
            )
            count += 1

        connection.commit()
        return count


def render_promises() -> None:
    render_page_header("Promise-to-Pay Tracker", "Track payment commitments and automatically escalate broken promises")

    rows = query_rows(
        """
        SELECT
            r.risk_id, r.customer_id, r.risk_category, r.risk_score,
            r.amount_at_risk, r.status, r.promise_to_pay_date,
            c.name AS customer_name, c.email, c.phone
        FROM risk_events r
        JOIN customers c ON c.customer_id = r.customer_id
        WHERE r.promise_to_pay_date IS NOT NULL
        ORDER BY r.promise_to_pay_date ASC
        """
    )

    today = date.today()
    upcoming_count = 0
    broken_count = 0
    resolved_count = 0
    total_promised_amount = 0.0

    table_data: list[dict[str, Any]] = []
    for row in rows:
        amount = float(row["amount_at_risk"] or 0)
        total_promised_amount += amount
        status = row["status"]
        ptp_raw = str(row["promise_to_pay_date"]).split()[0]
        try:
            ptp_dt = datetime.fromisoformat(ptp_raw).date()
        except Exception:
            ptp_dt = today

        if status == "resolved":
            resolved_count += 1
            badge = "⚪ Resolved"
            days_info = "Completed"
        elif ptp_dt >= today:
            upcoming_count += 1
            days_left = (ptp_dt - today).days
            badge = "🟢 Upcoming"
            days_info = f"Due in {days_left} day(s)" if days_left > 0 else "Due today"
        else:
            broken_count += 1
            days_overdue = (today - ptp_dt).days
            badge = "🔴 Broken"
            days_info = f"Overdue by {days_overdue} day(s)"

        table_data.append({
            "Status": badge,
            "Customer": row["customer_name"],
            "Promised Amount": money(amount),
            "Promise Date": str(ptp_dt),
            "Timeline Status": days_info,
            "Risk Score": f"{float(row['risk_score'] or 0):.3f}",
            "Risk ID": short_id(row["risk_id"]),
            "Current State": status.replace("_", " ").title(),
        })

    m_cols = st.columns(4)
    with m_cols[0]:
        render_stat_card("Total Promises", str(len(rows)), subtext="Logged commitments", badge='<span class="status-badge badge-blue">Tracked</span>')
    with m_cols[1]:
        render_stat_card("Upcoming", str(upcoming_count), subtext="Active grace period", badge='<span class="status-badge badge-green">Upcoming</span>')
    with m_cols[2]:
        render_stat_card("Broken / Overdue", str(broken_count), subtext="Eligible for auto-escalation", badge='<span class="status-badge badge-red">Broken</span>')
    with m_cols[3]:
        render_stat_card("Promised Revenue", money(total_promised_amount), subtext="Committed volume", badge='<span class="status-badge badge-amber">Committed</span>')

    st.divider()
    action_c1, action_c2 = st.columns([3, 1])
    with action_c1:
        st.subheader("Promised Recoveries")
        st.caption("Auto-escalator evaluates overdue commitments and flags them for human intervention")
    with action_c2:
        if st.button("⚡ Check Broken Promises", type="primary", use_container_width=True):
            escalated = check_and_escalate_broken_promises()
            if escalated > 0:
                st.warning(f"⚠️ Flagged & escalated {escalated} broken promise(s) to human review!")
            else:
                st.success("✅ No new broken promises found.")
            st.rerun()

    if not table_data:
        st.info("No Promise-to-Pay commitments logged yet. You can log promises from the Risk Queue or Approvals pages.")
    else:
        df = pd.DataFrame(table_data)
        st.dataframe(df, use_container_width=True, hide_index=True)


def render_reports() -> None:
    render_page_header("Recovery Performance Reports", "Outcome tracking, conversion analytics, and recovery ROI")
    with get_connection() as connection:
        totals = connection.execute(
            """
            SELECT
                COALESCE((SELECT SUM(amount_at_risk) FROM risk_events), 0),
                COALESCE((SELECT SUM(amount_recovered) FROM outcomes), 0),
                COALESCE((SELECT COUNT(*) FROM actions_taken), 0)
            """
        ).fetchone()
        category_rows = connection.execute(
            """
            SELECT
                r.risk_category,
                COALESCE(SUM(r.amount_at_risk), 0) AS amount_at_risk,
                COALESCE(SUM(o.amount_recovered), 0) AS recovered
            FROM risk_events r
            LEFT JOIN outcomes o ON o.risk_id = r.risk_id
            GROUP BY r.risk_category
            ORDER BY r.risk_category
            """
        ).fetchall()
        export_rows = connection.execute(
            """
            SELECT
                o.outcome_id, o.action_id, o.risk_id, o.outcome_type,
                o.amount_recovered, o.occurred_at, r.risk_category,
                r.amount_at_risk, r.status, c.customer_id, c.name AS customer_name
            FROM outcomes o
            JOIN risk_events r ON r.risk_id = o.risk_id
            JOIN customers c ON c.customer_id = r.customer_id
            ORDER BY o.occurred_at DESC
            """
        ).fetchall()
        action_rows = connection.execute(
            """
            SELECT 
                a.action_type,
                COUNT(o.outcome_id) AS total_actions,
                SUM(CASE WHEN o.outcome_type = 'paid' THEN 1 ELSE 0 END) AS paid_count,
                COALESCE(SUM(o.amount_recovered), 0) AS total_recovered
            FROM actions_taken a
            LEFT JOIN outcomes o ON o.action_id = a.action_id
            GROUP BY a.action_type
            ORDER BY total_recovered DESC
            """
        ).fetchall()

    total_at_risk, recovered, action_count = totals
    recovery_rate = float(recovered) / float(total_at_risk) * 100 if total_at_risk else 0
    contact_efficiency = float(recovered) / int(action_count) if action_count else 0
    
    rep_cols = st.columns(4)
    with rep_cols[0]:
        render_stat_card("Total Recovered", money(recovered), subtext="Settled revenue", badge='<span class="status-badge badge-green">Settled</span>')
    with rep_cols[1]:
        render_stat_card("Total At Risk", money(total_at_risk), subtext="Initial leak volume", badge='<span class="status-badge badge-red">Identified</span>')
    with rep_cols[2]:
        render_stat_card("Recovery Rate", pct(recovery_rate), subtext="Pipeline conversion", badge='<span class="status-badge badge-blue">Conversion</span>')
    with rep_cols[3]:
        render_stat_card("Contact Efficiency", money(contact_efficiency), subtext="Revenue per action taken", badge='<span class="status-badge badge-green">Efficiency</span>')

    st.divider()
    st.subheader("Recovery Rate by Risk Category")
    if category_rows:
        category_data = {
            row["risk_category"].replace("_", " ").title(): (
                float(row["recovered"]) / float(row["amount_at_risk"]) * 100
                if row["amount_at_risk"]
                else 0
            )
            for row in category_rows
        }
        st.bar_chart(category_data, horizontal=True, color="#0284c7")
    else:
        st.info("No risk categories available yet.")

    # Action Effectiveness Analytics
    st.divider()
    st.subheader("Action Effectiveness Analytics")
    st.caption("Recovery performance, dispatch volume, and conversion rate grouped by action strategy")

    if action_rows:
        eff_cols = st.columns(2)
        with eff_cols[0]:
            st.markdown("**Total Recovered (₹) by Action Type**")
            st.bar_chart(
                {row["action_type"].replace("_", " ").title(): float(row["total_recovered"]) for row in action_rows},
                horizontal=True,
                color="#0f766e",
            )

        with eff_cols[1]:
            st.markdown("**Strategy Performance Breakdown**")
            table_data = []
            for row in action_rows:
                tot = int(row["total_actions"] or 0)
                paid = int(row["paid_count"] or 0)
                rate = (paid / tot * 100) if tot > 0 else 0.0
                table_data.append({
                    "Action Strategy": row["action_type"].replace("_", " ").title(),
                    "Dispatched": tot,
                    "Paid Recoveries": paid,
                    "Success Rate": f"{rate:.1f}%",
                    "Total Recovered": money(row["total_recovered"]),
                })
            st.dataframe(pd.DataFrame(table_data), use_container_width=True, hide_index=True)
    else:
        st.info("No executed actions to analyze yet.")

    st.divider()
    output = io.StringIO()
    if export_rows:
        writer = csv.DictWriter(output, fieldnames=list(export_rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows_to_dicts(export_rows))
    else:
        output.write("outcome_id,action_id,risk_id,outcome_type,amount_recovered\n")
    st.download_button(
        "📥 Download Outcomes CSV Ledger",
        data=output.getvalue(),
        file_name="reviveai_outcomes.csv",
        mime="text/csv",
        use_container_width=True,
    )


def render_audit_trail() -> None:
    render_page_header("Audit Trail & Compliance", "Chronological provenance record, AI recommendation governance, and regulatory compliance ledger")

    with get_connection() as connection:
        ai_recs = connection.execute("SELECT COUNT(*) FROM diagnoses").fetchone()[0]
        policy_mods = connection.execute("SELECT COUNT(*) FROM policy_decisions WHERE decision = 'modified'").fetchone()[0]
        human_approvals = connection.execute(
            """
            SELECT COUNT(*) FROM audit_log 
            WHERE (actor LIKE 'human%' AND output_snapshot LIKE '%approved%') 
               OR (stage = 'execution' AND risk_id IN (SELECT risk_id FROM policy_decisions WHERE decision = 'needs_human_approval'))
            """
        ).fetchone()[0]
        human_rejections = connection.execute(
            "SELECT COUNT(*) FROM risk_events WHERE status = 'rejected_by_merchant'"
        ).fetchone()[0]
        escalations = connection.execute(
            "SELECT COUNT(*) FROM policy_decisions WHERE decision = 'needs_human_approval'"
        ).fetchone()[0]
        blocked = connection.execute(
            "SELECT COUNT(*) FROM policy_decisions WHERE decision = 'blocked'"
        ).fetchone()[0]

    st.subheader("Compliance & Governance Summary")
    comp_cols = st.columns(6)
    with comp_cols[0]:
        render_stat_card("AI Recs", f"{ai_recs:,}", subtext="Advisory suggestions", badge='<span class="status-badge badge-blue">AI</span>')
    with comp_cols[1]:
        render_stat_card("Policy Overrides", f"{policy_mods:,}", subtext="Clipped to 10%", badge='<span class="status-badge badge-amber">Modified</span>')
    with comp_cols[2]:
        render_stat_card("Human Approved", f"{human_approvals:,}", subtext="Signed off", badge='<span class="status-badge badge-green">Approved</span>')
    with comp_cols[3]:
        render_stat_card("Human Rejected", f"{human_rejections:,}", subtext="Merchant veto", badge='<span class="status-badge badge-red">Vetoed</span>')
    with comp_cols[4]:
        render_stat_card("Escalations", f"{escalations:,}", subtext="Gated for review", badge='<span class="status-badge badge-amber">Gated</span>')
    with comp_cols[5]:
        render_stat_card("Blocked", f"{blocked:,}", subtext="Safety rules enforced", badge='<span class="status-badge badge-red">Halted</span>')

    st.divider()

    st.subheader("Lifecycle Event Timeline")
    story_filter = st.selectbox(
        "Show Risk Stories",
        ["All", "Successful Recoveries Only", "Failed & Escalated Only"],
        help="Filter the audit log to investigate successful recoveries or debug blocked and failed cases.",
    )

    if story_filter == "Successful Recoveries Only":
        sql_filter = """
            SELECT DISTINCT r.risk_id 
            FROM risk_events r 
            JOIN outcomes o ON o.risk_id = r.risk_id 
            WHERE o.outcome_type = 'paid' OR r.status = 'resolved'
            ORDER BY r.risk_id
        """
    elif story_filter == "Failed & Escalated Only":
        sql_filter = """
            SELECT DISTINCT r.risk_id 
            FROM risk_events r 
            LEFT JOIN outcomes o ON o.risk_id = r.risk_id 
            WHERE r.status IN ('expired', 'rejected_by_merchant') 
               OR r.risk_id IN (SELECT risk_id FROM policy_decisions WHERE decision IN ('blocked', 'needs_human_approval'))
               OR (o.outcome_type IS NOT NULL AND o.outcome_type <> 'paid')
            ORDER BY r.risk_id
        """
    else:
        sql_filter = "SELECT DISTINCT risk_id FROM audit_log WHERE risk_id IS NOT NULL ORDER BY risk_id"

    risk_rows = query_rows(sql_filter)
    if not risk_rows:
        st.info(f"No risk events match the filter '{story_filter}'.")
        return

    id_map = {row["risk_id"]: f"Risk · {short_id(row['risk_id'])}" for row in risk_rows}
    risk_ids = [row["risk_id"] for row in risk_rows]
    selected = st.selectbox(
        "Inspect Risk Event Lifecycle",
        risk_ids,
        format_func=lambda x: id_map.get(x, x),
    )
    
    event_info = query_rows(
        """
        SELECT r.risk_id, r.amount_at_risk, r.status, r.risk_category, c.name AS customer_name, c.email
        FROM risk_events r
        JOIN customers c ON c.customer_id = r.customer_id
        WHERE r.risk_id = ?
        """,
        (selected,),
    )
    if event_info:
        ev = dict(event_info[0])
        html(
            f"""
            <div class="revive-card" style="margin-bottom:16px;">
                <div class="revive-card-header">
                    <div>
                        <span class="stat-label">Inspecting Risk Event</span> · 
                        <strong style="color:#f8fafc; font-size:1.05rem;">{ev['customer_name']}</strong> 
                        <span style="color:#94a3b8; font-size:0.85rem;">({ev['email']})</span>
                    </div>
                    <div>{render_badge(ev['status'])}</div>
                </div>
                <div style="display:flex; justify-content:space-between; align-items:center; margin-top:8px;">
                    <div><span style="color:#94a3b8;">Category:</span> <strong style="color:#38bdf8;">{ev['risk_category'].replace('_', ' ').title()}</strong></div>
                    <div><span style="color:#94a3b8;">Amount at Risk:</span> <strong style="color:#34d399;">{money(ev['amount_at_risk'])}</strong></div>
                    <div><span style="color:#94a3b8;">Risk ID:</span> <code>{ev['risk_id']}</code></div>
                </div>
            </div>
            """
        )

    # Mini timeline
    render_pipeline_mini_timeline(selected)

    audit_rows = query_rows(
        """
        SELECT stage, actor, timestamp, input_snapshot, output_snapshot
        FROM audit_log
        WHERE risk_id = ?
        ORDER BY timestamp ASC, log_id ASC
        """,
        (selected,),
    )
    if not audit_rows:
        st.info("No audit records for this risk event.")
        return

    st.markdown("#### Chronological Audit Milestones")
    for index, row in enumerate(audit_rows, start=1):
        html(
            f"""
            <div style="background:#131c2d; border:1px solid #1e293b; border-left:3px solid #0284c7; border-radius:8px; padding:10px 14px; margin-bottom:8px;">
                <div style="display:flex; justify-content:space-between; align-items:center;">
                    <div>
                        <strong style="color:#38bdf8; font-size:0.95rem;">Step {index}: {row['stage'].replace('_', ' ').title()}</strong> · 
                        <span style="color:#94a3b8; font-size:0.8rem;">Actor: <code>{row['actor']}</code></span>
                    </div>
                    <div style="font-size:0.75rem; color:#64748b;">🕒 {row['timestamp']}</div>
                </div>
            </div>
            """
        )
        with st.expander(f"Inspect Stage {index} ({row['stage']}) Input / Output Snapshots"):
            left, right = st.columns(2)
            try:
                input_snapshot = json.loads(row["input_snapshot"] or "{}")
            except (TypeError, json.JSONDecodeError):
                input_snapshot = row["input_snapshot"]
            try:
                output_snapshot = json.loads(row["output_snapshot"] or "{}")
            except (TypeError, json.JSONDecodeError):
                output_snapshot = row["output_snapshot"]
            left.caption("Input Payload")
            left.json(input_snapshot)
            right.caption("Output Snapshot")
            right.json(output_snapshot)


def main() -> None:
    st.set_page_config(
        page_title="ReviveAI — Revenue Recovery Control Center",
        page_icon="⚡",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    inject_custom_css()
    if not database_exists_or_stop():
        return
    
    sidebar_header = """
    <div style="padding: 10px 0 16px 0; border-bottom: 1px solid #1e293b; margin-bottom: 12px;">
        <div style="font-size: 1.35rem; font-weight: 800; color: #f8fafc; letter-spacing: -0.02em;">
            ⚡ ReviveAI
        </div>
        <div style="font-size: 0.8rem; color: #94a3b8; margin-top: 2px;">
            Autonomous Revenue Recovery
        </div>
    </div>
    """
    st.sidebar.markdown("\n".join(l.strip() for l in sidebar_header.strip().splitlines()), unsafe_allow_html=True)
    
    default_page = st.session_state.get("page", "Overview")
    page = st.sidebar.radio(
        "Navigation",
        PAGES,
        index=PAGES.index(default_page) if default_page in PAGES else 0,
    )
    st.session_state["page"] = page
    
    sidebar_footer = """
    <div style="margin-top: 28px; padding-top: 14px; border-top: 1px solid #1e293b; font-size: 0.75rem; color: #64748b;">
        <div>🔒 <strong>Policy Governed</strong></div>
        <div style="margin-top: 2px;">Deterministic execution authority</div>
        <div style="margin-top: 8px; color: #475569;">Track 03 · Razorpay Buildathon</div>
    </div>
    """
    st.sidebar.markdown("\n".join(l.strip() for l in sidebar_footer.strip().splitlines()), unsafe_allow_html=True)

    renderers = {
        "Overview": render_overview,
        "Risk Queue": render_risk_queue,
        "Diagnosis View": render_diagnosis_view,
        "Recovery Queue": render_recovery_queue,
        "Approvals": render_approvals,
        "Promises": render_promises,
        "Reports": render_reports,
        "Audit Trail & Compliance": render_audit_trail,
    }
    renderers[page]()


if __name__ == "__main__":
    main()