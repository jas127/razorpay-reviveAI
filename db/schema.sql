-- ReviveAI Phase 0 database schema.
-- SQLite is used as the state store for the recovery pipeline.

-- Core entity: who we're recovering money from
CREATE TABLE customers (
    customer_id TEXT PRIMARY KEY,
    name TEXT,
    email TEXT,
    phone TEXT,
    preferred_language TEXT DEFAULT 'en', -- for Hinglish angle
    contact_count_last_7d INTEGER DEFAULT 0,
    opted_out INTEGER DEFAULT 0,          -- hard stop flag
    created_at TIMESTAMP
);

-- Raw revenue events (simulates Razorpay webhook data)
CREATE TABLE revenue_events (
    event_id TEXT PRIMARY KEY,
    customer_id TEXT REFERENCES customers(customer_id),
    event_type TEXT, -- 'payment_failed' | 'checkout_abandoned' |
                     -- 'subscription_failed' | 'invoice_overdue'
    amount REAL,
    currency TEXT DEFAULT 'INR',
    payment_method TEXT, -- 'card' | 'upi' | 'netbanking' | 'mandate'
    failure_code TEXT,   -- raw gateway/bank decline code, if any
    subscription_id TEXT,
    invoice_id TEXT,
    checkout_session_id TEXT,
    occurred_at TIMESTAMP,
    raw_metadata TEXT    -- JSON blob, mimics Razorpay payload
);

-- Output of Detection Engine
CREATE TABLE risk_events (
    risk_id TEXT PRIMARY KEY,
    event_id TEXT UNIQUE REFERENCES revenue_events(event_id), -- UNIQUE =
                                                               -- idempotency guard #1:
                                                               -- one risk_event per source event, ever
    customer_id TEXT REFERENCES customers(customer_id),
    risk_category TEXT, -- 'payment_degradation' | 'checkout_dropoff' |
                        -- 'subscription_failure' | 'receivable_overdue'
    risk_score REAL,    -- 0-1, from detection logic
    amount_at_risk REAL,
    detected_at TIMESTAMP,
    status TEXT DEFAULT 'new', -- new | locked | diagnosed | actioned | resolved | expired |
                               -- rejected_by_merchant
    lock_token TEXT,           -- idempotency guard #2: set atomically before execution begins
    locked_at TIMESTAMP,       -- if set and stale (>N minutes) without a resulting action, the lock
                               -- is considered abandoned and is swept back to
                               -- 'diagnosed'/STOP_AND_ESCALATE
    force_escalate INTEGER DEFAULT 0, -- set after a stale lock; Policy Engine must require human review
    promise_to_pay_date TIMESTAMP     -- Phase 6: Promise-to-Pay tracker target date
);

-- Output of Diagnosis Agent (LLM) — advisory only
CREATE TABLE diagnoses (
    diagnosis_id TEXT PRIMARY KEY,
    risk_id TEXT REFERENCES risk_events(risk_id),
    root_cause TEXT, -- LLM's explanation
    recommended_action TEXT, -- 'send_reminder' | 'retry_payment' |
                             -- 'offer_discount' | 'send_payment_link' |
                             -- 'escalate_to_human' | 'switch_payment_method'
    recommended_channel TEXT, -- 'email' | 'sms' | 'whatsapp'
    recommended_discount_pct REAL DEFAULT 0,
    confidence REAL,           -- 0-1
    llm_raw_response TEXT,     -- full response, stored for audit
    diagnosed_at TIMESTAMP,
    provider_used TEXT,        -- 'gemini' | 'groq' | 'fallback_default'
    customer_message_hinglish TEXT -- Phase 6: Hinglish message for hi-en customers
);

-- Output of Policy Engine — the FINAL decision, always deterministic
CREATE TABLE policy_decisions (
    decision_id TEXT PRIMARY KEY,
    risk_id TEXT REFERENCES risk_events(risk_id),
    diagnosis_id TEXT REFERENCES diagnoses(diagnosis_id),
    decision TEXT, -- 'auto_approved' | 'modified' | 'blocked' | 'needs_human_approval'
    final_action TEXT, -- what will actually execute (may differ from LLM's suggestion)
    final_discount_pct REAL,
    rules_applied TEXT, -- JSON list of which rules fired, e.g.
                        -- ["discount_cap","cooldown_check"]
    reason TEXT,         -- human-readable explanation of the decision
    decided_at TIMESTAMP
);

-- Output of Action Executor
CREATE TABLE actions_taken (
    action_id TEXT PRIMARY KEY,
    decision_id TEXT UNIQUE REFERENCES policy_decisions(decision_id), -- idempotency
                                                                       -- guard #3:
                                                                       -- one execution per decision, enforced
                                                                       -- by the DB, not application logic
    risk_id TEXT REFERENCES risk_events(risk_id),
    action_type TEXT,
    channel TEXT,
    message_sent TEXT, -- simulated message content
    executed_at TIMESTAMP,
    simulated INTEGER DEFAULT 1 -- always 1 in MVP — be explicit about this
);

-- Outcome tracking (simulated customer behavior over time)
CREATE TABLE outcomes (
    outcome_id TEXT PRIMARY KEY,
    action_id TEXT REFERENCES actions_taken(action_id),
    risk_id TEXT REFERENCES risk_events(risk_id),
    outcome_type TEXT, -- 'paid' | 'ignored' | 'opted_out' | 'failed_again'
    amount_recovered REAL DEFAULT 0,
    occurred_at TIMESTAMP
);

-- The append-only audit trail — every single stage writes here
CREATE TABLE audit_log (
    log_id TEXT PRIMARY KEY,
    risk_id TEXT,
    stage TEXT, -- 'detection' | 'diagnosis' | 'diagnosis_failed' |
                -- 'lock_contended' | 'lock_swept' | 'policy' |
                -- 'duplicate_execution_blocked' | 'execution' | 'outcome'
    actor TEXT, -- 'system' | 'gemini_llm' | 'policy_engine' | 'human:<user_id>'
    input_snapshot TEXT,  -- JSON of what went in
    output_snapshot TEXT, -- JSON of what came out
    timestamp TIMESTAMP
);

-- Merchant-configurable policy rules (drives Policy Engine)
CREATE TABLE policy_rules (
    rule_id TEXT PRIMARY KEY,
    rule_name TEXT,
    rule_type TEXT, -- 'max_contact_frequency' | 'discount_cap' |
                    -- 'approval_threshold_amount' | 'cooldown_hours' |
                    -- 'max_attempts' | 'opt_out_respect'
    rule_value TEXT, -- JSON, e.g. {"max_discount_pct": 10}
    active INTEGER DEFAULT 1
);