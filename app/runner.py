"""
Concurrent Playwright runner.

Each run spawns N independent browser contexts (one per user)
and executes every scenario in the suite in parallel across all users.

Pattern:
  run_suite(run_id, suite_id, env, num_users)
    └─ for each scenario in suite:
         asyncio.gather(run_user(scenario, env, user_0), ..., run_user(scenario, env, userN))
           └─ Playwright headless browser → ScenarioResult
             └─ saved to DB as run_results row
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .config import MAX_CONCURRENT_USERS, email_for_user, get_env_config
from .db import (
    get_conn, get_run, list_scenarios, save_result,
    update_run_status, fetch_one,
)
from .scenarios.registry import get_scenario_func
from .scenarios.buoy_champ_onboarding import _create_mailtm_account

logger = logging.getLogger(__name__)

SCREENSHOTS_DIR = Path(__file__).parent.parent / "reports" / "screenshots"
SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)


# ── per-user execution ───────────────────────────────────────────────

async def _run_one_user(
    scenario_key: str,
    scenario_name: str,
    env_config: dict,
    user_idx: int,
    run_id: int,
    mailtm_accounts: dict | None = None,
) -> dict:
    """
    Launches a headless browser for a single simulated user,
    runs the scenario, and returns a result dict.
    """
    from playwright.async_api import async_playwright

    result = {
        "user_idx": user_idx,
        "scenario_name": scenario_name,
        "status": "error",
        "steps": [],
        "errors": [],
        "duration_ms": 0,
        "email_used": "",
        "screenshot_path": "",
    }

    t0 = time.monotonic()
    pw = None
    browser = None

    try:
        scenario_func = get_scenario_func(scenario_key)

        # one email per user — Mailinator inbox
        user_email = email_for_user(run_id, user_idx)
        user_config = {**env_config, "payroll_email": user_email, "run_id": run_id}

        # Inject pre-created mail.tm credentials if available
        if mailtm_accounts and user_idx in mailtm_accounts:
            acct = mailtm_accounts[user_idx]
            user_config["mailtm_email"] = acct["email"]
            user_config["mailtm_token"] = acct["token"]

        pw = await async_playwright().start()
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--disable-infobars",
                "--disable-dev-shm-usage",
                "--no-sandbox",
            ],
        )
        ctx = await browser.new_context(
            viewport={"width": 1440, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
        )
        ctx.set_default_timeout(env_config.get("timeout", 30_000))
        await ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
        )

        page = await ctx.new_page()

        logger.info("user_start run_id=%s user=%s scenario=%s", run_id, user_idx, scenario_key)
        scenario_result = await scenario_func(page, user_config, user_idx)

        result["steps"] = scenario_result.get("steps", [])
        result["errors"] = scenario_result.get("errors", [])
        result["status"] = scenario_result.get("status", "FAIL")
        result["email_used"] = user_email

        # Screenshot on FAIL / WARN
        if result["status"] in ("FAIL", "error", "WARN"):
            try:
                ss_path = SCREENSHOTS_DIR / f"run{run_id}_user{user_idx}_{scenario_key}.png"
                await page.screenshot(path=str(ss_path), full_page=True)
                result["screenshot_path"] = str(ss_path)
                logger.info("screenshot_saved path=%s", ss_path)
            except Exception as ss_err:
                logger.warning("screenshot_failed: %s", ss_err)

    except Exception as exc:
        logger.exception("user_error run_id=%s user=%s: %s", run_id, user_idx, exc)
        result["errors"].append(str(exc))
        result["status"] = "error"
    finally:
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        result["duration_ms"] = elapsed_ms
        if browser:
            try:
                await browser.close()
            except Exception:
                pass
        if pw:
            try:
                await pw.stop()
            except Exception:
                pass

    logger.info(
        "user_done run_id=%s user=%s scenario=%s status=%s duration_ms=%s",
        run_id, user_idx, scenario_key, result["status"], result["duration_ms"]
    )
    return result


# ── scenario executor (N users) ──────────────────────────────────────

async def _precreate_mailtm_accounts(run_id: int, num_users: int, env_config: dict) -> dict:
    """
    Sequentially create mail.tm accounts for all users BEFORE launching browsers.
    3-second gap between each creation avoids HTTP 429 rate-limiting.
    Returns dict: user_idx → {"email": str, "token": str}
    """
    try:
        import httpx as _httpx_mod
    except ImportError:
        logger.warning("httpx not installed — skipping mail.tm pre-creation")
        return {}

    accounts = {}
    password = env_config.get("password", "Parol123#")

    try:
        async with _httpx_mod.AsyncClient(timeout=10.0) as client:
            dr = await client.get("https://api.mail.tm/domains",
                                  headers={"Accept": "application/json"})
            if dr.status_code != 200:
                logger.warning("mail.tm domains HTTP %s — skipping pre-creation", dr.status_code)
                return {}
            dr_json = dr.json()
            domains = dr_json if isinstance(dr_json, list) else dr_json.get("hydra:member", [])
            domain = domains[0].get("domain", "") if domains else ""
            if not domain:
                logger.warning("mail.tm: no domain available")
                return {}
    except Exception as e:
        logger.warning("mail.tm domain fetch failed: %s", e)
        return {}

    from .config import ONEMAIL_PREFIX
    for user_idx in range(num_users):
        email = f"{ONEMAIL_PREFIX}_r{run_id}_u{user_idx}@{domain}"
        token, msg = await _create_mailtm_account(email, password)

        # Retry once if we got rate-limited (token is None)
        if not token:
            logger.warning(
                "mailtm_precreate_429 run_id=%s user=%s — waiting 20s then retrying",
                run_id, user_idx,
            )
            await asyncio.sleep(20)
            token, msg = await _create_mailtm_account(email, password)
            if token:
                logger.info("mailtm_precreate_retry_ok run_id=%s user=%s", run_id, user_idx)
            else:
                logger.warning(
                    "mailtm_precreate_retry_fail run_id=%s user=%s msg=%s — will fall back in-scenario",
                    run_id, user_idx, msg,
                )

        accounts[user_idx] = {"email": email, "token": token}
        logger.info("mailtm_precreate run_id=%s user=%s email=%s token_ok=%s msg=%s",
                    run_id, user_idx, email, bool(token), msg)
        if user_idx < num_users - 1:
            await asyncio.sleep(12)  # 12s gap keeps creation rate under mail.tm limit (~5/min)

    success = sum(1 for v in accounts.values() if v["token"])
    logger.info("mailtm_precreate_summary run_id=%s total=%s ok=%s failed=%s",
                run_id, num_users, success, num_users - success)
    return accounts


async def _run_scenario_for_all_users(
    scenario: dict,
    env_config: dict,
    num_users: int,
    run_id: int,
    conn,
) -> None:
    """Run one scenario concurrently for num_users users."""
    cap = min(num_users, MAX_CONCURRENT_USERS)

    # Pre-create mail.tm accounts sequentially to avoid concurrent 429s
    mailtm_accounts = await _precreate_mailtm_accounts(run_id, cap, env_config)
    if mailtm_accounts:
        logger.info("mailtm_precreate_done run_id=%s accounts=%s", run_id, len(mailtm_accounts))

    tasks = [
        _run_one_user(
            scenario_key=scenario["script_key"],
            scenario_name=scenario["name"],
            env_config=env_config,
            user_idx=i,
            run_id=run_id,
            mailtm_accounts=mailtm_accounts,
        )
        for i in range(cap)
    ]

    results = await asyncio.gather(*tasks, return_exceptions=True)

    for res in results:
        if isinstance(res, Exception):
            save_result(
                conn,
                run_id=run_id,
                scenario_id=scenario["id"],
                scenario_name=scenario["name"],
                user_idx=-1,
                status="error",
                steps=[],
                errors=[str(res)],
                duration_ms=0,
            )
        else:
            save_result(
                conn,
                run_id=run_id,
                scenario_id=scenario["id"],
                scenario_name=scenario["name"],
                user_idx=res["user_idx"],
                status=res["status"],
                steps=res["steps"],
                errors=res["errors"],
                duration_ms=res["duration_ms"],
                email_used=res.get("email_used", ""),
                screenshot_path=res.get("screenshot_path", ""),
            )


# ── main entry point (called from background task) ───────────────────

def run_suite_sync(run_id: int, suite_id: int, env: str, num_users: int) -> None:
    """
    Blocking wrapper — called inside FastAPI BackgroundTasks
    so it runs in a separate thread with its own event loop.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_run_suite(run_id, suite_id, env, num_users))
    finally:
        loop.close()


async def _run_suite(run_id: int, suite_id: int, env: str, num_users: int) -> None:
    conn = get_conn()
    try:
        env_config = get_env_config(env)
    except ValueError as e:
        logger.error("bad_env run_id=%s: %s", run_id, e)
        update_run_status(conn, run_id, "error")
        conn.close()
        return

    scenarios = list_scenarios(conn, suite_id)
    if not scenarios:
        logger.error("no_scenarios run_id=%s suite_id=%s", run_id, suite_id)
        update_run_status(conn, run_id, "error")
        conn.close()
        return

    logger.info(
        "suite_start run_id=%s suite=%s env=%s users=%s scenarios=%s",
        run_id, suite_id, env, num_users, len(scenarios)
    )

    all_ok = True
    for scenario in scenarios:
        # Check for cancellation
        row = fetch_one(conn, "SELECT cancel_requested FROM runs WHERE id=?", (run_id,))
        if row and int(row["cancel_requested"] or 0) == 1:
            update_run_status(conn, run_id, "canceled")
            conn.close()
            return

        try:
            await _run_scenario_for_all_users(
                scenario=scenario,
                env_config=env_config,
                num_users=num_users,
                run_id=run_id,
                conn=conn,
            )
        except Exception as exc:
            logger.exception("scenario_error run_id=%s scenario=%s: %s", run_id, scenario["id"], exc)
            all_ok = False

    final_status = "complete" if all_ok else "error"
    update_run_status(conn, run_id, final_status)
    logger.info("suite_done run_id=%s status=%s", run_id, final_status)

    # ── Slack notification ────────────────────────────────────────────
    try:
        from .db import get_run_stats
        from .slack import notify_run_complete

        stats = get_run_stats(conn, run_id)
        await notify_run_complete(run_id, env, final_status, stats)

    except Exception as _notify_err:
        logger.debug("Post-run notification skipped: %s", _notify_err)

    conn.close()
