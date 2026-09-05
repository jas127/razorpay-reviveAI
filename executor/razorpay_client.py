"""Razorpay Test Mode Payment Links client for ReviveAI.

This module provides optional real integration with Razorpay's Payment Links API:
POST https://api.razorpay.com/v1/payment_links

Used only when USE_REAL_RAZORPAY_LINKS=true and valid credentials exist.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any
import requests

try:
    from dotenv import load_dotenv
    _ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
    if _ENV_PATH.exists():
        load_dotenv(_ENV_PATH)
    else:
        load_dotenv()
except ImportError:
    pass

logger = logging.getLogger("reviveai.razorpay")


def create_real_payment_link(
    *,
    risk_id: str,
    amount_at_risk: float,
    customer_name: str = "Customer",
    customer_email: str | None = None,
    customer_phone: str | None = None,
    description: str | None = None,
) -> str | None:
    """Create a real payment link using Razorpay's Payment Links API.

    Returns the short_url on success, or None on failure (falling back safely).
    """
    key_id = os.getenv("RAZORPAY_KEY_ID", "").strip()
    key_secret = os.getenv("RAZORPAY_KEY_SECRET", "").strip()

    if not key_id or not key_secret:
        logger.warning(
            "Razorpay API keys (RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET) not found in env. "
            "Falling back to simulation."
        )
        return None

    # Amount in paise (1 INR = 100 paise)
    amount_in_paise = max(100, int(round(float(amount_at_risk) * 100)))
    desc = description or f"ReviveAI Revenue Recovery - Risk {risk_id}"

    payload: dict[str, Any] = {
        "amount": amount_in_paise,
        "currency": "INR",
        "accept_partial": False,
        "description": desc,
        "customer": {
            "name": customer_name or "Valued Customer",
        },
        "notify": {
            "sms": False,
            "email": False,
        },
        "reminder_enable": False,
        "notes": {
            "risk_id": risk_id,
            "source": "ReviveAI_Recovery_Agent",
        },
    }
    if customer_email and "@" in customer_email:
        payload["customer"]["email"] = customer_email
    if customer_phone:
        payload["customer"]["contact"] = customer_phone

    try:
        response = requests.post(
            "https://api.razorpay.com/v1/payment_links",
            auth=(key_id, key_secret),
            json=payload,
            timeout=10,
        )
        if response.status_code in (200, 201):
            data = response.json()
            short_url = data.get("short_url")
            if short_url:
                logger.info("Successfully generated real Razorpay link: %s", short_url)
                return str(short_url)
            logger.warning("Razorpay returned %s but missing short_url: %s", response.status_code, data)
            return None
        else:
            logger.warning(
                "Razorpay Payment Link API returned HTTP %s: %s",
                response.status_code,
                response.text,
            )
            return None
    except Exception as exc:
        logger.warning(
            "Failed to connect to Razorpay Payment Links API: %s. Falling back to simulation.",
            exc,
        )
        return None
