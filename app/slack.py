"""
Slack notification helper.

Env vars:
  SLACK_WEBHOOK_URL   — Incoming Webhook URL (required to send)
  BERTECH_BASE_URL    — Public base URL for report links (default: http://localhost:8080)
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

BASE_URL = os.environ.get("BERTECH_BASE_URL", "http://localhost:8080").rstrip("/")


def _status_emoji(status: str) -> str:
    return {"complete": "✅", "error": "❌", "canceled": "⛔"}.get(status, "⚠️")


async def notify_run_complete(
    run_id: int,
    env: str,
    status: str,
    stats: dict,
) -> None:
    webhook = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
    if not webhook:
        return

    try:
        import httpx
    except ImportError:
        logger.debug("httpx not available — Slack notification skipped")
        return

    passed = stats.get("passed", 0)
    warned = stats.get("warned", 0)
    failed = stats.get("failed", 0)
    total  = stats.get("total", 0)

    status_icon = _status_emoji(status)
    env_label   = env.upper()
    report_url  = f"{BASE_URL}/runs/{run_id}/report"
    export_url  = f"{BASE_URL}/runs/{run_id}/export"

    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"{status_icon} BerTech UAT — Run #{run_id} · {env_label}",
            },
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Status*\n`{status}`"},
                {"type": "mrkdwn", "text": f"*Users*\n{total} total"},
                {"type": "mrkdwn", "text": f"*Pass / Warn / Fail*\n✅ {passed}  ⚠️ {warned}  ❌ {failed}"},
                {"type": "mrkdwn", "text": f"*Environment*\n{env_label}"},
            ],
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"✅ *{passed}* passed  ⚠️ *{warned}* warned  ❌ *{failed}* failed",
            },
        },
        {"type": "divider"},
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "📄 HTML Report"},
                    "url": report_url,
                    "style": "primary",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "⬇️ Export JSON"},
                    "url": export_url,
                },
            ],
        },
    ]

    payload = {
        "blocks": blocks,
        "text": f"{_status_emoji(status)} BerTech UAT Run #{run_id} ({env_label}) — {status}",
    }

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(webhook, json=payload)
        if resp.status_code == 200:
            logger.info("Slack notified for run #%s", run_id)
        else:
            logger.warning("Slack notification failed: HTTP %s — %s", resp.status_code, resp.text[:200])
