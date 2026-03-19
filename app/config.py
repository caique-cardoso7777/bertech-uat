from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict

BASE_DIR = Path(__file__).parent.parent

ENVIRONMENTS: Dict[str, dict] = {
    "dev": {
        "label": "Development",
        "base_url": "https://dev.buoyhub.com",
        "onboarding_url": "https://dev.buoyhub.com/onboarding-champ/",
        "enrollment_url": "https://dev.buoyhub.com/buoy-champ/",
    },
    "uat": {
        "label": "UAT",
        "base_url": "https://uat.buoyhub.com",
        "onboarding_url": "https://uat.buoyhub.com/onboarding-champ/",
        "enrollment_url": "https://uat.buoyhub.com/buoy-champ/",
    },
    "prod": {
        "label": "Production",
        "base_url": "https://buoyhub.com",
        "onboarding_url": "https://buoyhub.com/onboarding-champ/",
        "enrollment_url": "https://buoyhub.com/buoy-champ/",
    },
}

# ── playwright defaults ──────────────────────────────────────────────
PW_TIMEOUT = int(os.getenv("PW_TIMEOUT", "30000"))
PW_NAV_TIMEOUT = int(os.getenv("PW_NAV_TIMEOUT", "60000"))
PW_SLOW_MO = int(os.getenv("PW_SLOW_MO", "0"))

# ── concurrency cap ─────────────────────────────────────────────────
MAX_CONCURRENT_USERS = int(os.getenv("MAX_CONCURRENT_USERS", "10"))

# ── test fixtures ───────────────────────────────────────────────────
DEFAULT_TEST_DATA = {
    "password": "Parol123#",
    "business_name": "BerTech QA Testing",
    "full_name": "Caique Cardoso",
    "phone": "999-999-9999",
    "website": "buoy.com",
    "avg_salary": "50000",
    "num_employees": "10",
    "address_query": "texas",
    "ein": "99-9999999",
    "payroll_company_name": "test_bertech",
    "pandadoc_phone": "999999999999",
    "slack_webhook": os.getenv("SLACK_WEBHOOK", ""),
    # Payroll fixture — CSV/XLS/XLSX up to 10 MB accepted by the upload widget.
    # Path is resolved relative to the project root (bertech_uat/).
    "payroll_fixture": str(BASE_DIR / "Payroll Register_mock.xlsx"),
}

# ── 1secmail config ──────────────────────────────────────────────────
# 1secmail.com provides a free public REST API — no browser scraping needed.
# Domains available: 1secmail.com | 1secmail.org | 1secmail.net
# API: GET https://www.1secmail.com/api/v1/?action=getMessages&login=XXX&domain=1secmail.com
# Each user gets a fully isolated inbox via unique login name.
# Example: bertechqa_r42_u3@1secmail.com → login: bertechqa_r42_u3
ONEMAIL_PREFIX = os.getenv("ONEMAIL_PREFIX", "bertechqa")
ONEMAIL_DOMAIN = os.getenv("ONEMAIL_DOMAIN", "1secmail.com")

def email_for_user(run_id: int, user_idx: int) -> str:
    """Unique, deterministic email per run+user — each maps to its own 1secmail inbox."""
    return f"{ONEMAIL_PREFIX}_r{run_id}_u{user_idx}@{ONEMAIL_DOMAIN}"

def get_env_config(env: str) -> dict:
    if env not in ENVIRONMENTS:
        raise ValueError(f"Unknown environment: {env!r}. Must be one of: {list(ENVIRONMENTS)}")
    return {**ENVIRONMENTS[env], **DEFAULT_TEST_DATA}
