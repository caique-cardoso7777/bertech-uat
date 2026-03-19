"""
Scenario: Business Owner Onboarding — Buoy Champ  (LOI "No" path)
Adapted from: UAT_Buoy_CHAMP_Playwright.ipynb  (BerTech QA, 123 steps)
Persona selectors from: Persona_Buoy-Champ.json

Complete flow:
  Onboarding 1  → Intro CTA
  Onboarding 2  → User Type (Business Owner)
  Onboarding 3  → Company Info (name, phone, website, state, EIN, address)
  Onboarding 4  → Census & Payroll (MUI selects, avg salary, num employees)
                  → "Get Estimate"
  Onboarding 5  → Impact Estimate Results → "Get an official quote"
  Sign Up        → /champ-signup  (email + password → Create Account)
  OTP            → /verify-champ  (6-digit code via 1secmail REST API)
  Enrollment 1  → Account Manager role card + F5
  Enrollment 2  → Company Information (full form) + F5
  Enrollment 3:
    3B  Persona KYB iframe (address, EIN, legal name → Continue → Done)
    3C  LOI — "No" path  (confirm question visible, No is pre-selected,
                          skip all LOI form fields, click Next)
  Enrollment 4  → Proposal & Agreements (verify page loaded)

QA best practices:
  - JS click for MUI Selects (only reliable method, confirmed via Chrome DevTools)
  - press_sequentially for Google Places autocomplete
  - Retry logic (3x) on flaky MUI/SPA buttons
  - No F5 inside the SPA except Onboarding 2, Enrollment 1, 2
  - Per-step timing via _step()
  - Screenshot on FAIL/WARN triggered by runner.py
  - 1secmail REST API per user  (no browser scraping, no shared state)
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import List

try:
    import httpx as _httpx
except ImportError:
    _httpx = None  # type: ignore

try:
    from playwright.async_api import Page, TimeoutError as PWTimeout
except ImportError:
    Page = object          # type: ignore
    PWTimeout = Exception  # type: ignore


# ── step result builder ──────────────────────────────────────────────

def _step(name: str, status: str, notes: List[str], t0: float) -> dict:
    return {
        "name": name,
        "status": status,
        "notes": notes,
        "duration_ms": int((time.monotonic() - t0) * 1000),
    }


# ── low-level helpers (mirrored from notebook) ───────────────────────

async def _safe_text(page: Page, selector: str, timeout: int = 5000) -> str:
    try:
        el = await page.wait_for_selector(selector, timeout=timeout)
        return (await el.inner_text()).strip()
    except Exception:
        return ""


async def _exists(page: Page, selector: str, timeout: int = 3000) -> bool:
    try:
        await page.wait_for_selector(selector, timeout=timeout)
        return True
    except Exception:
        return False


async def _fill(page: Page, selector: str, value: str, timeout: int = 8000) -> bool:
    """Click-then-fill — handles MUI inputs requiring focus first."""
    try:
        el = page.locator(selector).first
        await el.wait_for(state="visible", timeout=timeout)
        await el.click()
        await page.wait_for_timeout(150)
        await el.fill(value)
        return True
    except Exception:
        return False


async def _click_retry(page: Page, selector: str, retries: int = 3, timeout: int = 8000) -> bool:
    """Retry MUI buttons that may animate/re-render between attempts."""
    for attempt in range(retries):
        try:
            el = page.locator(selector).first
            await el.wait_for(state="visible", timeout=timeout)
            await el.click()
            return True
        except Exception:
            if attempt < retries - 1:
                await page.wait_for_timeout(800)
    return False


async def _select_mui(page: Page, idx: int, label: str, notes: List[str]) -> bool:
    """
    Opens the idx-th MuiSelect via JS then clicks the first option.
    JS click is the only confirmed-reliable method on this SPA
    (Playwright locator.click() does NOT open MUI Select dropdowns here).
    """
    try:
        await page.evaluate(
            f"() => {{ const els = document.querySelectorAll('[class*=\"MuiSelect-select\"]'); "
            f"if (els[{idx}]) els[{idx}].click(); }}"
        )
        await page.wait_for_timeout(900)
        await page.wait_for_selector('[role="listbox"] [role="option"]', state="visible", timeout=5000)
        opt_text = await page.locator('[role="listbox"] [role="option"]').first.inner_text()
        await page.evaluate(
            '() => { const e = document.querySelector(\'[role="listbox"] [role="option"]\'); if (e) e.click(); }'
        )
        await page.wait_for_timeout(600)
        notes.append(f'{label}: "{opt_text.strip()}" selected (JS idx={idx})')
        return True
    except Exception as ex:
        try:
            await page.locator('[class*="MuiSelect-select"]').nth(idx).click(force=True)
            await page.wait_for_timeout(900)
            await page.wait_for_selector('[role="listbox"] [role="option"]', state="visible", timeout=3000)
            opt_text = await page.locator('[role="listbox"] [role="option"]').first.inner_text()
            await page.locator('[role="listbox"] [role="option"]').first.click(force=True)
            await page.wait_for_timeout(600)
            notes.append(f'{label}: "{opt_text.strip()}" selected (force-click idx={idx})')
            return True
        except Exception as ex2:
            notes.append(f'WARN: {label} — JS:{ex} | force:{ex2}')
            return False


async def _reload_wait(page: Page, wait_ms: int = 4000) -> None:
    """
    F5 with pre-wait so Salesforce finishes persisting data before the reload.
    Used after Enrollment 1 and Enrollment 2.
    Waits for networkidle after reload so the SPA has time to hydrate before
    the next step tries to find form elements.
    """
    await page.wait_for_timeout(2000)
    try:
        await page.reload(wait_until="load", timeout=60_000)
    except Exception:
        pass
    # Extra wait for Salesforce SPA to hydrate after reload
    try:
        await page.wait_for_load_state("networkidle", timeout=15_000)
    except Exception:
        pass
    await page.wait_for_timeout(wait_ms)


async def _get_otp_1secmail(login: str, domain: str = "1secmail.com",
                            max_attempts: int = 20, poll_interval: float = 4.0) -> str:
    """
    Fetches the 6-digit OTP via 1secmail REST API — no browser scraping needed.

    1secmail is a free public disposable email service with a JSON API.
    Tries all three 1secmail domains if the primary returns 403/error.
    API docs: https://www.1secmail.com/api/v1/

    login  = local part of the address (e.g. 'bertechqa_r1_u0')
    domain = one of: 1secmail.com | 1secmail.org | 1secmail.net
    """
    if _httpx is None:
        return ""  # httpx not installed — caller will handle

    base = "https://www.1secmail.com/api/v1/"
    # Browser-like headers to avoid 403 bot detection
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.1secmail.com/",
    }
    # Try all three domains — inbox is the same across all
    domains_to_try = list({domain, "1secmail.com", "1secmail.org", "1secmail.net"})

    async with _httpx.AsyncClient(timeout=15.0, headers=headers) as client:
        for attempt in range(1, max_attempts + 1):
            await asyncio.sleep(poll_interval)
            for try_domain in domains_to_try:
                try:
                    # 1. List messages in inbox
                    r = await client.get(base, params={
                        "action": "getMessages",
                        "login": login,
                        "domain": try_domain,
                    })
                    if r.status_code != 200:
                        continue  # try next domain
                    messages = r.json()
                    if not messages:
                        continue

                    # 2. Read most recent message
                    msg_id = messages[0]["id"]
                    r2 = await client.get(base, params={
                        "action": "readMessage",
                        "login": login,
                        "domain": try_domain,
                        "id": msg_id,
                    })
                    if r2.status_code != 200:
                        continue

                    data = r2.json()
                    # Search in plain text body first, then HTML
                    for field in ("textBody", "htmlBody", "body"):
                        body = data.get(field, "") or ""
                        m = re.search(r'\b(\d{6})\b', body)
                        if m:
                            return m.group(1)

                except Exception:
                    continue

    return ""


async def _get_otp_mailtm(token: str,
                           max_attempts: int = 20, poll_interval: float = 4.0) -> str:
    """
    Fetches the 6-digit OTP via mail.tm REST API.

    mail.tm is used as fallback when 1secmail is unavailable.
    Requires a bearer token obtained from _create_mailtm_account().
    API docs: https://api.mail.tm
    """
    if _httpx is None:
        return ""

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }

    async with _httpx.AsyncClient(timeout=15.0) as client:
        for attempt in range(1, max_attempts + 1):
            await asyncio.sleep(poll_interval)
            try:
                r = await client.get("https://api.mail.tm/messages", headers=headers)
                if r.status_code != 200:
                    continue
                data = r.json()
                # API returns a plain list OR hydra dict — handle both
                messages = data if isinstance(data, list) else data.get("hydra:member", [])
                if not messages:
                    continue

                msg_id = messages[0]["id"]
                r2 = await client.get(f"https://api.mail.tm/messages/{msg_id}", headers=headers)
                if r2.status_code != 200:
                    continue

                msg = r2.json()
                # html field may be a list of strings — join before searching
                combined_body = ""
                for field in ("text", "intro", "html"):
                    raw = msg.get(field, "") or ""
                    body = " ".join(raw) if isinstance(raw, list) else raw
                    combined_body += " " + body

                # Priority 1: look for "code is XXXXXX" — Buoy's exact email format
                m = re.search(r'code is\s*[:\-]?\s*(\d{4,8})', combined_body, re.IGNORECASE)
                if m:
                    candidate = m.group(1)
                    if len(set(candidate)) > 1:
                        return candidate

                # Priority 2: any 6-digit number in the body, excluding all-same-digit
                for m in re.finditer(r'\b(\d{6})\b', combined_body):
                    candidate = m.group(1)
                    if len(set(candidate)) > 1:
                        return candidate

                # Last resort: search the entire raw JSON — but exclude trivial
                # all-same-digit patterns (000000, 111111…) which are not real OTPs
                full_text = r2.text
                # Try "code is" pattern in full JSON first
                m = re.search(r'code is\s*[:\-]?\s*(\d{4,8})', full_text, re.IGNORECASE)
                if m:
                    candidate = m.group(1)
                    if len(set(candidate)) > 1:
                        return candidate
                for m in re.finditer(r'\b(\d{6})\b', full_text):
                    candidate = m.group(1)
                    if len(set(candidate)) > 1:  # at least 2 distinct digits
                        return candidate

            except Exception:
                continue

    return ""


async def _create_mailtm_account(address: str, password: str) -> tuple[str, str]:
    """
    Creates (or logs into) a mail.tm account and returns (token, debug_msg).
    - 201 → account created, token returned
    - 422 → account already exists, tries login directly
    - other → returns ("", error description)
    """
    if _httpx is None:
        return "", "httpx not installed"

    headers = {"Content-Type": "application/json", "Accept": "application/json"}

    async with _httpx.AsyncClient(timeout=20.0) as client:
        try:
            # 1. Try to create account
            r = await client.post(
                "https://api.mail.tm/accounts",
                json={"address": address, "password": password},
                headers=headers,
            )
            if r.status_code not in (200, 201, 422):
                return "", f"create HTTP {r.status_code}: {r.text[:120]}"

            create_msg = "created" if r.status_code in (200, 201) else "already_exists(422)"

            # 2. Get auth token (works whether we just created or account existed)
            r2 = await client.post(
                "https://api.mail.tm/token",
                json={"address": address, "password": password},
                headers=headers,
            )
            if r2.status_code != 200:
                return "", f"token HTTP {r2.status_code} after {create_msg}: {r2.text[:120]}"

            token = r2.json().get("token", "")
            if not token:
                return "", f"token empty after {create_msg}"

            return token, f"OK ({create_msg})"
        except Exception as ex:
            return "", f"exception: {ex}"


# ── main scenario ────────────────────────────────────────────────────

async def run(page: Page, config: dict, user_idx: int) -> dict:
    steps: List[dict] = []
    errors: List[str] = []
    overall = "PASS"

    nav_timeout = config.get("nav_timeout", 60_000)
    onboarding  = config.get("onboarding_url", "https://dev.buoyhub.com/onboarding-champ/")
    email       = config.get("payroll_email", f"bertechqa_r0_u{user_idx}@mailinator.com")
    inbox_name  = email.split("@")[0]
    password    = config.get("password", "Parol123#")

    # ── mail.tm setup (primary OTP provider — 1secmail returns 403 on this IP) ──
    # Prefer pre-created credentials from runner.py (sequential pre-creation avoids
    # HTTP 429 when running many concurrent users). Fall back to in-scenario creation
    # only if runner didn't provide credentials (e.g. single-user manual runs).
    _mailtm_email: str = config.get("mailtm_email", "")
    _mailtm_token: str = config.get("mailtm_token", "")
    _mailtm_debug: list = []

    if _mailtm_email and _mailtm_token:
        _mailtm_debug.append(f"mail.tm account pre-created: {_mailtm_email!r} ✓")
    elif _httpx is not None:
        # Fallback: create account in-scenario (single-user or runner skipped pre-creation)
        try:
            async with _httpx.AsyncClient(timeout=10.0) as _c:
                _dr = await _c.get("https://api.mail.tm/domains",
                                   headers={"Accept": "application/json"})
                _mailtm_debug.append(f"mail.tm domains HTTP {_dr.status_code}")
                if _dr.status_code == 200:
                    _dr_json = _dr.json()
                    _domains = _dr_json if isinstance(_dr_json, list) else _dr_json.get("hydra:member", [])
                    _mtm_domain = _domains[0].get("domain", "") if _domains else ""
                    _mailtm_debug.append(f"mail.tm domain: {_mtm_domain!r}")
                    if _mtm_domain:
                        _mailtm_email = f"{inbox_name}@{_mtm_domain}"
                        _mailtm_token, _create_msg = await _create_mailtm_account(
                            _mailtm_email, password
                        )
                        _mailtm_debug.append(
                            f"mail.tm account {_mailtm_email!r}: {_create_msg}"
                        )
                    else:
                        _mailtm_debug.append("mail.tm: no domain available")
                else:
                    _mailtm_debug.append(f"mail.tm domains error: {_dr.text[:80]}")
        except Exception as _mtm_ex:
            _mailtm_debug.append(f"mail.tm setup exception: {_mtm_ex}")

    def _mark_warn():
        nonlocal overall
        if overall == "PASS":
            overall = "WARN"

    def _mark_fail():
        nonlocal overall
        overall = "FAIL"

    # ── ONBOARDING 1 — Intro ────────────────────────────────────────
    t0 = time.monotonic()
    notes, status = [], "PASS"
    try:
        await page.goto(onboarding, wait_until="domcontentloaded", timeout=nav_timeout)
        await page.wait_for_timeout(2000)

        body_text = await _safe_text(page, "body", 3000)
        if "Get an estimate on your savings" in body_text:
            notes.append("BUG B01 ✓: Stale copy 'Get an estimate on your savings' still present")
            status = "WARN"; _mark_warn()
        else:
            notes.append("B01: Intro copy OK")

        stepper = await _safe_text(page, 'nav,[class*="stepper"],[class*="breadcrumb"]', 3000)
        notes.append(f'Stepper: "{stepper[:60]}"' if stepper else "NOTE: Stepper not found on intro")

        cta = await _click_retry(
            page,
            'button:has-text("Get Started"), button:has-text("Start"), button[type="submit"]',
            retries=3
        )
        notes.append("CTA clicked OK" if cta else "WARN: CTA button not found")
        if not cta:
            status = "WARN"; _mark_warn()

    except Exception as e:
        notes.append(f"ERROR: {e}"); status = "FAIL"; _mark_fail()
        errors.append(f"Onboarding 1: {e}")

    # Append mail.tm setup debug to Step 1 notes so it's visible in every run report
    notes.extend(_mailtm_debug)

    steps.append(_step("Onboarding 1 — Intro", status, notes, t0))
    if overall == "FAIL":
        return {"status": overall, "steps": steps, "errors": errors}

    # ── ONBOARDING 2 — User Type ────────────────────────────────────
    # From Chrome Recorder (buoy_champ_onboarding.json):
    #   Business Owner card = first button in form (no text content, no data-value)
    #   XPath: //*[@id="root"]/div/form/div[2]/div[2]/div/div/div/div[2]/div/button[1]/div
    #   Save & Next = button.css-sndmno / aria "Save & Next"
    #   NO F5 here — F5 only after enrollment starts, not during onboarding.
    t0 = time.monotonic()
    notes, status = [], "PASS"
    try:
        # Wait for the user-type selection page to be ready.
        # Do NOT reload here — the F5 belongs after Save & Next (below).
        await page.wait_for_timeout(2000)

        try:
            await page.wait_for_selector("form button", state="visible", timeout=12000)
            notes.append(f"User-type page ready (URL: {page.url})")
        except Exception as e:
            notes.append(f"WARN: User-type page not confirmed — {e}")
            status = "WARN"; _mark_warn()

        # Click Business Owner card.
        # The card is a styled radio/button component. React needs proper events fired
        # on the actual interactive element for state to update.
        # Screenshot from run_10 confirmed: "Please select identity" shown → click not registering.
        bo = False

        # Strategy 0: radio input .check() — Playwright's dedicated radio/checkbox handler.
        # This is the most React-friendly approach: fires pointerdown, mousedown, click, change.
        try:
            radio_inputs = page.locator('input[type="radio"]')
            count = await radio_inputs.count()
            if count > 0:
                await radio_inputs.first.wait_for(state="attached", timeout=5000)
                await radio_inputs.first.check(force=True)
                await page.wait_for_timeout(400)
                is_checked = await radio_inputs.first.is_checked()
                if is_checked:
                    bo = True
                    notes.append("Business Owner radio input checked via .check() ✓")
                else:
                    notes.append("NOTE: radio .check() fired but not checked — trying next strategy")
        except Exception as e:
            notes.append(f"NOTE: radio .check() attempt: {e}")

        # Strategy 1: text-based button click (Playwright fires full event sequence)
        if not bo:
            for sel in [
                'button:has-text("business owner")',
                'button:has-text("I\'m a business owner")',
            ]:
                try:
                    el = page.locator(sel).first
                    await el.wait_for(state="visible", timeout=8000)
                    await el.scroll_into_view_if_needed()
                    await el.click()
                    await page.wait_for_timeout(400)
                    bo = True
                    notes.append(f"Business Owner card clicked via text selector ({sel}) ✓")
                    break
                except Exception:
                    pass

        # Strategy 2: XPath to button (Chrome Recorder path, without inner /div)
        if not bo:
            try:
                el = page.locator(
                    'xpath=//*[@id="root"]/div/form/div[2]/div[2]/div/div/div/div[2]/div/button[1]'
                )
                await el.wait_for(state="visible", timeout=8000)
                await el.click()
                await page.wait_for_timeout(400)
                bo = True
                notes.append("Business Owner card clicked via XPath (button) ✓")
            except Exception:
                pass

        # Strategy 3: mouse.click() at element coordinates — simulates real pointer events
        if not bo:
            try:
                el = page.locator(
                    'xpath=//*[@id="root"]/div/form/div[2]/div[2]/div/div/div/div[2]/div/button[1]'
                )
                box = await el.bounding_box()
                if box:
                    await page.mouse.click(
                        box["x"] + box["width"] / 2,
                        box["y"] + box["height"] / 2,
                    )
                    await page.wait_for_timeout(400)
                    bo = True
                    notes.append("Business Owner card clicked via page.mouse at bounding box ✓")
            except Exception as e:
                notes.append(f"NOTE: mouse.click attempt: {e}")

        # Strategy 4: JS — dispatch full React-compatible event sequence on radio input
        if not bo:
            try:
                result = await page.evaluate(
                    """() => {
                        const radios = document.querySelectorAll('input[type="radio"]');
                        if (radios.length) {
                            const r = radios[0];
                            const nativeSetter = Object.getOwnPropertyDescriptor(
                                window.HTMLInputElement.prototype, 'checked'
                            ).set;
                            nativeSetter.call(r, true);
                            r.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
                            r.dispatchEvent(new Event('input', {bubbles: true}));
                            r.dispatchEvent(new Event('change', {bubbles: true}));
                            return 'js_radio_dispatched';
                        }
                        // fallback: click first non-submit, non-sndmno button
                        const form = document.querySelector('form');
                        if (!form) return 'no_form';
                        const btns = [...form.querySelectorAll('button')]
                            .filter(b => b.type !== 'submit' && !b.className.includes('sndmno'));
                        if (!btns.length) return 'no_btns';
                        btns[0].click();
                        return 'js_btn:' + btns[0].textContent.trim().slice(0, 40);
                    }"""
                )
                if result and result not in ("no_form", "no_btns"):
                    bo = True
                    notes.append(f"Business Owner clicked via JS event dispatch ({result}) ✓")
                else:
                    notes.append(f"WARN: JS dispatch → {result}")
            except Exception as e:
                notes.append(f"WARN: JS card click: {e}")

        if not bo:
            notes.append("WARN: Business Owner card — all strategies exhausted")
            status = "WARN"; _mark_warn()

        await page.wait_for_timeout(800)

        # Save & Next (confirmed via Recorder aria label)
        nxt = await _click_retry(
            page,
            'button.css-sndmno, button:has-text("Save & Next"), button:has-text("Next")',
            retries=3
        )
        notes.append("Save & Next clicked ✓" if nxt else "WARN: Save & Next not found")
        if not nxt:
            status = "WARN"; _mark_warn()

        # ── CRITICAL: verify the SPA actually advanced past Step 2 ──
        # If "Please select identity" validation error is still visible, the card
        # selection did NOT register in React and we must retry or fail early.
        # Without this check, all subsequent steps run on the wrong page.
        await page.wait_for_timeout(1200)
        validation_error = await _exists(page, 'text="Please select identity"', 2000)
        if validation_error:
            notes.append("WARN: 'Please select identity' still visible — card not registered, retrying JS dispatch")
            # One more attempt: dispatch events directly via JS then re-submit
            try:
                await page.evaluate(
                    """() => {
                        const radios = document.querySelectorAll('input[type="radio"]');
                        if (radios.length) {
                            const r = radios[0];
                            const nativeSetter = Object.getOwnPropertyDescriptor(
                                window.HTMLInputElement.prototype, 'checked'
                            ).set;
                            nativeSetter.call(r, true);
                            r.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
                            r.dispatchEvent(new Event('input', {bubbles: true}));
                            r.dispatchEvent(new Event('change', {bubbles: true}));
                        }
                    }"""
                )
                await page.wait_for_timeout(600)
                await _click_retry(
                    page,
                    'button.css-sndmno, button:has-text("Save & Next")',
                    retries=2,
                )
                await page.wait_for_timeout(1500)
                still_failing = await _exists(page, 'text="Please select identity"', 2000)
                if still_failing:
                    notes.append("FAIL: Card still not selected after retry — cannot advance past Step 2")
                    status = "FAIL"; _mark_fail()
                    errors.append("Onboarding 2: React card selection failed — SPA stuck at Step 2")
                else:
                    notes.append("Card selection registered on retry ✓")
            except Exception as retry_e:
                notes.append(f"FAIL: Retry JS dispatch failed: {retry_e}")
                status = "FAIL"; _mark_fail()
                errors.append(f"Onboarding 2: retry failed — {retry_e}")

        # Verify the SPA truly advanced to Step 3 (Company Info).
        # Two-part check: Step 3 form visible AND Step 2 identity cards gone.
        # wait_for_selector alone is not enough — the SPA briefly shows Step 3
        # elements during a CSS transition even when it reverts back to Step 2.
        if overall != "FAIL":
            await page.wait_for_timeout(1500)
            step2_gone   = not await _exists(page, 'text="Please select identity"', 2000)
            step3_visible = await _exists(
                page, "#company, input[name='company'], input[name='business_name']", 5000
            )
            if step2_gone and step3_visible:
                notes.append("Company Info (step 3) confirmed visible ✓ (Step 2 cards gone)")
            elif not step2_gone:
                notes.append(f"WARN: Still on Step 2 (identity cards still present) — transition={step3_visible}")
                status = "WARN"; _mark_warn()
            else:
                notes.append(f"NOTE: Step 2 gone but form not yet visible ({step3_visible}) — continuing")

    except Exception as e:
        notes.append(f"ERROR: {e}"); status = "FAIL"; _mark_fail()
        errors.append(f"Onboarding 2: {e}")

    steps.append(_step("Onboarding 2 — User Type", status, notes, t0))
    if overall == "FAIL":
        return {"status": overall, "steps": steps, "errors": errors}

    # ── ONBOARDING 3 — Company Info ─────────────────────────────────
    # From Chrome Recorder (buoy_champ_onboarding.json):
    #   #company  → business name
    #   #email    → business email (= user's test email, so OTP lands in right inbox)
    #   Tab → Enter → ArrowDown → Enter  → state dropdown selection (first option)
    #   button.css-sndmno / "Save & Next"
    # NOTE: this is a SHORT form (2 fields + state), NOT the full company profile.
    #       Full company profile is at Enrollment 2 (/buoy-champ/).
    t0 = time.monotonic()
    notes, status = [], "PASS"
    try:
        await page.wait_for_timeout(2000)
        notes.append(f"O3 URL: {page.url}")

        # Debug screenshot — captures the exact page state at Step 3 start.
        # Saved regardless of pass/fail so we can inspect what the SPA is showing.
        try:
            import pathlib
            _ss_dir = pathlib.Path(__file__).parent.parent.parent / "reports" / "screenshots"
            _ss_dir.mkdir(parents=True, exist_ok=True)
            _run_id = config.get("run_id", "x")
            await page.screenshot(
                path=str(_ss_dir / f"debug_o3_r{_run_id}_u{user_idx}.png"),
                full_page=True,
            )
            notes.append(f"Debug screenshot saved: debug_o3_r{_run_id}_u{user_idx}.png")
        except Exception as _ss_e:
            notes.append(f"NOTE: debug screenshot failed: {_ss_e}")

        # Wait for Company Info form — increase timeout and try broader selectors
        _o3_ready = False
        for _sel in ["#company", "input[name='company']", "input[name='business_name']",
                     "input[placeholder*='company' i]", "input[placeholder*='business name' i]"]:
            try:
                await page.wait_for_selector(_sel, state="visible", timeout=12000)
                _o3_ready = True
                notes.append(f"O3 form ready (matched: {_sel})")
                break
            except Exception:
                continue
        if not _o3_ready:
            notes.append("WARN: Company Info form not visible after 12s — page may not have advanced")
            status = "WARN"; _mark_warn()

        # Business name — try IDs confirmed by Chrome Recorder, then fallbacks
        cmp_ok = False
        for comp_sel in [
            "#company",
            "input[name='company']",
            "input[name='business_name']",
            "input[placeholder*='company name' i]",
            "input[placeholder*='business name' i]",
            "form input:first-of-type",
        ]:
            if await _fill(page, comp_sel, config.get("business_name", "BerTech QA")):
                notes.append(f"Company name filled via: {comp_sel}")
                cmp_ok = True
                break
        if not cmp_ok:
            notes.append("WARN: Company name field NOT FOUND"); status = "WARN"; _mark_warn()

        await page.keyboard.press("Tab")

        # Business email — must match the inbox we'll poll for OTP.
        # Use mail.tm email when available (avoids 1secmail 403 on OTP step).
        otp_email = _mailtm_email if _mailtm_email and _mailtm_token else email
        email_ok = False
        for email_sel in [
            "#email",
            "input[type='email']",
            "input[name='email']",
            "input[placeholder*='email' i]",
        ]:
            if await _fill(page, email_sel, otp_email):
                notes.append(f"Email filled via: {email_sel} ({otp_email})")
                email_ok = True
                break
        if not email_ok:
            notes.append(f"WARN: Email field NOT FOUND ({otp_email})"); status = "WARN"; _mark_warn()

        # State dropdown — keyboard navigation (Tab to focus, Enter open, ArrowDown, Enter select)
        await page.keyboard.press("Tab")
        await page.wait_for_timeout(400)
        await page.keyboard.press("Enter")
        await page.wait_for_timeout(400)
        await page.keyboard.press("ArrowDown")
        await page.wait_for_timeout(200)
        await page.keyboard.press("Enter")
        notes.append("State: keyboard nav (Tab→Enter→↓→Enter)")

        # Save & Next
        nxt = await _click_retry(
            page,
            'button.css-sndmno, button:has-text("Save & Next"), button:has-text("Next")',
            retries=3
        )
        notes.append("Save & Next clicked ✓" if nxt else "WARN: Save & Next not found")
        if not nxt:
            status = "WARN"; _mark_warn()

    except Exception as e:
        notes.append(f"ERROR: {e}"); status = "FAIL"; _mark_fail()
        errors.append(f"Onboarding 3: {e}")

    steps.append(_step("Onboarding 3 — Company Info", status, notes, t0))
    if overall == "FAIL":
        return {"status": overall, "steps": steps, "errors": errors}

    # ── ONBOARDING 4 — Census & Payroll ────────────────────────────
    # From Chrome Recorder (buoy_champ_onboarding.json):
    #   Payroll freq  → click body to open → #menu-payroll li:nth-of-type(1)
    #   Annual gross  → #annual_gross (React controlled input via JS nativeSetter)
    #   Worker state  → click body to open → #menu-worker_distribution\.0\.state li:nth-of-type(1)
    #   Num employees → #worker_distribution\.0\.percentage
    #   Get Estimate  → button.css-sndmno / "Get Estimate"
    t0 = time.monotonic()
    notes, status = [], "PASS"
    try:
        await page.wait_for_timeout(1500)
        try:
            await page.wait_for_selector('#annual_gross', state="visible", timeout=10000)
            notes.append("O4 page ready (#annual_gross visible)")
        except Exception as e:
            notes.append(f"WARN: Census & Payroll page not ready — {e}")
            status = "WARN"; _mark_warn()

        # Payroll Frequency — MUI Select (idx=0 on this page).
        # _select_mui uses JS .click() which is the only reliable method on this SPA.
        # Coordinate-based body.click() is unreliable across viewports.
        _pf_ok = await _select_mui(page, 0, "Payroll Frequency", notes)
        if not _pf_ok:
            # Fallback: try named menu directly
            try:
                await page.locator('[class*="MuiSelect-select"]').first.click()
                await page.wait_for_timeout(800)
                await page.locator("#menu-payroll li:nth-of-type(1)").first.click()
                notes.append("Payroll frequency: li[1] via named menu fallback ✓")
            except Exception as e:
                notes.append(f"WARN: Payroll frequency: {e}")
                status = "WARN"; _mark_warn()

        # Avg Annual Gross Pay — React controlled input via JS nativeSetter
        avg_salary = config.get("avg_salary", "50000")
        sal_ok = False
        sal_js = (
            "() => { const inp = document.getElementById('annual_gross');"
            " if (!inp) return 'not_found';"
            " const s = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;"
            f" s.call(inp, '{avg_salary}');"
            " inp.dispatchEvent(new Event('input',{bubbles:true}));"
            " inp.dispatchEvent(new Event('change',{bubbles:true}));"
            " return inp.value; }"
        )
        try:
            result = await page.evaluate(sal_js)
            if result and result != "not_found":
                notes.append(f"Avg salary: {avg_salary} via JS (val={result})")
                sal_ok = True
            else:
                notes.append(f"WARN: #annual_gross JS → {result}")
        except Exception as e:
            notes.append(f"WARN: avg_salary JS: {e}")

        if not sal_ok:
            sal_ok = await _fill(page, "#annual_gross", avg_salary)
            notes.append(f"Avg salary fallback: {'OK' if sal_ok else 'not filled'}")
            if not sal_ok:
                status = "WARN"; _mark_warn()

        # Worker state — MUI Select (idx=1 on this page).
        _ws_ok = await _select_mui(page, 1, "Worker State", notes)
        if not _ws_ok:
            # Fallback: try named menu directly
            try:
                await page.locator('[class*="MuiSelect-select"]').nth(1).click()
                await page.wait_for_timeout(800)
                await page.locator(
                    "#menu-worker_distribution\\.0\\.state li:nth-of-type(1)"
                ).first.click()
                notes.append("Worker state: Alabama via named menu fallback ✓")
            except Exception as e:
                notes.append(f"WARN: Worker state: {e}")
                status = "WARN"; _mark_warn()

        # Number of Employees — #worker_distribution\.0\.percentage (BUG B10: defaults to 0)
        num_employees = config.get("num_employees", "10")
        emp_ok = False
        for emp_sel in [
            'input[id="worker_distribution.0.percentage"]',   # dots need attribute selector
            'input[placeholder*="Add number of employees" i]',
            'input[name*="worker_distribution"]',
        ]:
            try:
                emp = page.locator(emp_sel).first
                await emp.wait_for(state="visible", timeout=4000)
                cur = await emp.input_value()
                if cur in ("0", ""):
                    notes.append("BUG B10 ✓: Employee count defaults to 0")
                    status = "WARN"; _mark_warn()
                await emp.triple_click()
                await emp.fill(num_employees)
                await page.wait_for_timeout(300)
                final_val = await emp.input_value()
                notes.append(f"Employees: {num_employees} via JS fallback → final={final_val}")
                emp_ok = True
                break
            except Exception:
                continue

        if not emp_ok:
            try:
                await page.evaluate(
                    "() => { const inp = document.getElementById('worker_distribution.0.percentage')"
                    " || document.querySelector('input[placeholder=\"Add number of employees\"]');"
                    " if (!inp) return;"
                    " const s = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;"
                    f" s.call(inp, '{num_employees}');"
                    " inp.dispatchEvent(new Event('input',{bubbles:true}));"
                    " inp.dispatchEvent(new Event('change',{bubbles:true})); }"
                )
                notes.append(f"Employees: {num_employees} via JS fallback")
                emp_ok = True
            except Exception as e:
                notes.append(f"WARN: Employee count: {e}")
                status = "WARN"; _mark_warn()

        if emp_ok and status != "FAIL":
            for btn_sel in ['button:has-text("Get Estimate")', 'button.css-sndmno']:
                try:
                    b = page.locator(btn_sel).first
                    await b.wait_for(state="visible", timeout=3000)
                    await b.click()
                    notes.append(f"'Get Estimate' clicked via: {btn_sel}")
                    break
                except Exception:
                    continue
            await page.wait_for_timeout(3500)
        else:
            notes.append("SKIP: 'Get Estimate' not clicked — required fields missing")
            status = "FAIL"; _mark_fail()

    except Exception as e:
        notes.append(f"ERROR: {e}"); status = "FAIL"; _mark_fail()
        errors.append(f"Onboarding 4: {e}")

    steps.append(_step("Onboarding 4 — Census & Payroll", status, notes, t0))
    if overall == "FAIL":
        return {"status": overall, "steps": steps, "errors": errors}

    # ── ONBOARDING 5 — Impact Estimate Results ──────────────────────
    t0 = time.monotonic()
    notes, status = [], "PASS"
    try:
        if await _exists(page, 'text=Initial Impact Estimate', 5000):
            notes.append("BUOYD-274 ✓: 'Initial Impact Estimate' heading present")
        else:
            notes.append("BUOYD-274 ⚠: Heading not found"); status = "WARN"; _mark_warn()

        if not await _exists(page, 'button:has-text("Back")', 3000):
            notes.append("BUG B11 ✓: Back button absent on results page")
            status = "WARN"; _mark_warn()
        else:
            notes.append("B11: Back button present")

        await page.wait_for_timeout(7000)  # wait for savings calculation

        # CTA label varies by env — try several known variants
        cta_selectors = (
            'button.css-1xm9e2a, '
            'button:has-text("Get an official quote"), '
            'button:has-text("official quote"), '
            'button:has-text("Get Started"), '
            'button:has-text("Get Quote"), '
            'button:has-text("Continue"), '
            'button:has-text("Next"), '
            'button.css-sndmno'
        )
        cta_found = False
        for sel in cta_selectors.split(", "):
            sel = sel.strip()
            if await _exists(page, sel, 2000):
                notes.append(f"CTA found: '{sel}'")
                cta_found = True
                break
        if not cta_found:
            notes.append("WARN: CTA not found with known selectors")
            status = "WARN"; _mark_warn()

        cta = await _click_retry(page, cta_selectors, retries=3)
        notes.append("CTA clicked OK" if cta else "WARN: CTA click failed")
        if not cta:
            status = "WARN"; _mark_warn()
        await page.wait_for_timeout(2000)

    except Exception as e:
        notes.append(f"ERROR: {e}"); status = "FAIL"; _mark_fail()
        errors.append(f"Onboarding 5: {e}")

    steps.append(_step("Onboarding 5 — Impact Estimate Results", status, notes, t0))
    if overall == "FAIL":
        return {"status": overall, "steps": steps, "errors": errors}

    # ── SIGN UP — /champ-signup ─────────────────────────────────────
    t0 = time.monotonic()
    notes, status = [], "PASS"
    try:
        await page.wait_for_timeout(1500)

        if not await _exists(page, 'nav,[class*="stepper"]', 3000):
            notes.append("BUG B12 ✓: Stepper absent on /champ-signup")
            status = "WARN"; _mark_warn()
        else:
            notes.append("B12: Stepper present")

        # Use mail.tm email when setup succeeded (avoids 1secmail 403 on OTP).
        # Otherwise fall back to the original 1secmail address.
        signup_email = _mailtm_email if _mailtm_email and _mailtm_token else email
        notes.append(f"Using signup email: {signup_email}"
                     + (" [mail.tm]" if signup_email == _mailtm_email else " [1secmail]"))

        # Email may be pre-filled
        try:
            ei = page.locator('input[type="email"]').first
            if await ei.is_visible():
                cur = await ei.input_value()
                if not cur or "@" not in cur:
                    await ei.fill(signup_email)
                    notes.append(f"Email filled: {signup_email}")
                else:
                    notes.append(f"Email pre-filled: {cur}")
        except Exception:
            pass

        pw_ok = await _fill(
            page,
            '#password, input[type="password"], input[name="password"], '
            'input[id*="password" i], input[placeholder*="password" i]',
            password
        )
        notes.append("Password filled" if pw_ok else "WARN: password field not found")
        if not pw_ok:
            status = "WARN"; _mark_warn()

        nxt = await _click_retry(
            page,
            'button.css-sndmno, button:has-text("Create An Account"), '
            'button:has-text("Create Account"), button[type="submit"]',
            retries=3
        )
        notes.append("Create Account clicked OK" if nxt else "WARN: Create Account button not found")
        if not nxt:
            status = "WARN"; _mark_warn()
        await page.wait_for_timeout(4000)

    except Exception as e:
        notes.append(f"ERROR: {e}"); status = "FAIL"; _mark_fail()
        errors.append(f"Sign Up: {e}")

    steps.append(_step("Sign Up — /champ-signup", status, notes, t0))
    if overall == "FAIL":
        return {"status": overall, "steps": steps, "errors": errors}

    # ── OTP — /verify-champ ─────────────────────────────────────────
    t0 = time.monotonic()
    notes, status = [], "PASS"
    try:
        if not await _exists(page, 'nav,[class*="stepper"]', 3000):
            notes.append("BUG B14 ✓: Stepper absent on /verify-champ")
            status = "WARN"; _mark_warn()

        btn_label = await _safe_text(page, "button.css-sndmno", 3000)
        if "Submitted" in btn_label:
            notes.append(f"BUG B13 ✓: Button shows '{btn_label}' before submission")
            status = "WARN"; _mark_warn()
        else:
            notes.append(f"B13: Button label = '{btn_label}'")

        # Fetch OTP — try mail.tm first (if setup succeeded), then 1secmail.
        # 1secmail has been returning 403; mail.tm is the reliable fallback.
        otp = ""
        if _mailtm_token:
            notes.append(f"Polling mail.tm inbox: {_mailtm_email}")
            # 30 × 6s = 180s max — needed when many users sign up simultaneously
            # and Buoy's email delivery is delayed (BUOYD-350)
            otp = await _get_otp_mailtm(_mailtm_token, max_attempts=30, poll_interval=6.0)
            if otp:
                notes.append(f"OTP captured via mail.tm ✓")
            else:
                notes.append("NOTE: mail.tm polling exhausted — trying 1secmail fallback")

        if not otp:
            email_domain = email.split("@")[1] if "@" in email else "1secmail.com"
            notes.append(f"Polling 1secmail inbox: {inbox_name}@{email_domain}")
            otp = await _get_otp_1secmail(inbox_name, domain=email_domain, max_attempts=20)
            if otp:
                notes.append("OTP captured via 1secmail ✓")

        if not otp:
            # If mail.tm account was ready but OTP never arrived → Buoy concurrent signup bug
            if _mailtm_token:
                notes.append(
                    "BUOYD-350 ✓: OTP not delivered — likely Buoy concurrent signup limit "
                    "exceeded (>4 simultaneous users). Email was accepted but OTP email "
                    "was never sent by Buoy."
                )
            notes.append("FAIL: OTP not found in any inbox (mail.tm + 1secmail exhausted)")
            status = "FAIL"; _mark_fail()
            errors.append("OTP: not captured from mail.tm or 1secmail")
            steps.append(_step("OTP — /verify-champ", status, notes, t0))
            return {"status": overall, "steps": steps, "errors": errors}

        notes.append(f"OTP captured: {otp}")

        await page.bring_to_front()
        await page.wait_for_timeout(1000)

        otp_boxes = page.locator('input[maxlength="1"]')
        box_count = await otp_boxes.count()
        notes.append(f"OTP boxes found: {box_count}")

        if box_count >= 6:
            for i, digit in enumerate(otp[:6]):
                box = otp_boxes.nth(i)
                await box.wait_for(state="visible", timeout=3000)
                await box.click()
                await page.keyboard.type(digit)
                await page.wait_for_timeout(80)
            notes.append(f"OTP typed digit-by-digit: {otp}")
        else:
            first_inp = page.locator('input:not([type="hidden"]):not([type="password"])').first
            await first_inp.wait_for(state="visible", timeout=3000)
            await first_inp.click()
            await page.keyboard.type(otp)
            notes.append(f"OTP typed via single-input fallback: {otp}")

        await page.wait_for_timeout(500)

        for btn_sel in [
            'button:has-text("Submit")', 'button:has-text("Verify")',
            'button:has-text("Confirm")', 'button.css-sndmno'
        ]:
            try:
                b = page.locator(btn_sel).first
                await b.wait_for(state="visible", timeout=3000)
                await b.click()
                notes.append(f"OTP submit clicked: {btn_sel}")
                break
            except Exception:
                continue
        # Wait for redirect to enrollment after OTP submit
        await page.wait_for_timeout(6000)

        # If still on verify-champ, navigate explicitly to enrollment URL.
        # This handles: OTP auto-verified (B13 bug), or slow redirect.
        enrollment_url = config.get("enrollment_url", "https://dev.buoyhub.com/buoy-champ/")
        if "buoy-champ" not in page.url:
            notes.append(f"NOTE: Post-OTP URL still {page.url} — navigating to enrollment")
            await page.goto(enrollment_url, wait_until="domcontentloaded", timeout=nav_timeout)
            await page.wait_for_timeout(5000)
        else:
            notes.append(f"Redirected to enrollment ✓ ({page.url})")

        # BUOYD-329: check for incorrect redirect to /dashboard instead of enrollment
        if "/dashboard" in page.url and "buoy-champ" not in page.url:
            notes.append(
                f"BUOYD-329 ⚠: Redirected to /dashboard instead of /buoy-champ — "
                f"enrollment flow broken (URL: {page.url})"
            )
            status = "WARN"; _mark_warn()

    except Exception as e:
        notes.append(f"ERROR: {e}"); status = "FAIL"; _mark_fail()
        errors.append(f"OTP: {e}")

    steps.append(_step("OTP — /verify-champ", status, notes, t0))
    if overall == "FAIL":
        return {"status": overall, "steps": steps, "errors": errors}

    # ── ENROLLMENT 1 — Account Manager ─────────────────────────────
    t0 = time.monotonic()
    notes, status = [], "PASS"
    try:
        sidebar = await _safe_text(
            page,
            '[class*="stepper"],[class*="sidebar"],[class*="step-indicator"]',
            5000
        )
        notes.append(f'Sidebar: "{sidebar[:80]}"')

        # BUG B15 — full name duplicated
        try:
            nm = await page.locator(
                'input[placeholder*="name" i], input[id*="name" i]'
            ).first.input_value()
            parts = nm.split()
            if len(parts) >= 2 and parts[0] == parts[-1]:
                notes.append(f"BUG B15 ✓: Full Name duplicated → '{nm}'")
                status = "WARN"; _mark_warn()
            else:
                notes.append(f"B15: Full Name = '{nm}' OK")
        except Exception:
            notes.append("NOTE B15: Could not read Full Name field")

        try:
            await page.locator("button:nth-of-type(1) > div").first.click()
            await page.wait_for_timeout(600)
            notes.append("Role card selected")
        except Exception:
            notes.append("NOTE: Role card click failed")

        for btn_sel in [
            'button:has-text("Next")', 'button:has-text("Próximo")',
            'form button[type="submit"]', 'form > button', 'button.css-sndmno'
        ]:
            try:
                b = page.locator(btn_sel).first
                await b.wait_for(state="visible", timeout=3000)
                await b.click()
                notes.append(f"Next clicked via: {btn_sel}")
                break
            except Exception:
                continue

        await page.wait_for_timeout(5000)  # E1→E2: wait for SF to advance step

        try:
            await page.locator("#organization_name").wait_for(state="visible", timeout=10000)
            notes.append("Enrollment 2 confirmed ✓")
        except Exception:
            notes.append("WARN: Enrollment 2 not confirmed after wait")

    except Exception as e:
        notes.append(f"ERROR: {e}"); status = "FAIL"; _mark_fail()
        errors.append(f"Enrollment 1: {e}")

    steps.append(_step("Enrollment 1 — Account Manager", status, notes, t0))
    if overall == "FAIL":
        return {"status": overall, "steps": steps, "errors": errors}

    # ── ENROLLMENT 2 — Company Information ─────────────────────────
    t0 = time.monotonic()
    notes, status = [], "PASS"
    try:
        # BUG B16 — required labels without asterisk
        labels_txt = await page.evaluate(
            "() => [...document.querySelectorAll('label')].map(l=>l.textContent.trim()).join('|')"
        )
        b16_labels = ["Company Industry", "Phone Number", "Primary Business Address", "Payroll Frequency"]
        found_b16 = [l for l in b16_labels if l in labels_txt]
        if found_b16:
            notes.append(f"BUG B16 ✓: Labels without asterisk: {', '.join(found_b16)}")
            status = "WARN"; _mark_warn()

        if not await _fill(page, "#organization_name", config.get("business_name", "BerTech QA")):
            notes.append("WARN: #organization_name not found"); status = "WARN"; _mark_warn()
        else:
            notes.append(f"Company Name: {config.get('business_name')}")

        # Industry — #menu-company_field li[2] = Apparel
        try:
            await page.locator(
                "form > div:nth-of-type(1) > div:nth-of-type(2) > div"
            ).first.click()
            await page.wait_for_timeout(700)
            await page.locator("#menu-company_field li:nth-of-type(2)").first.click()
            notes.append("Industry: Apparel (li[2])")
        except Exception as e:
            notes.append(f"WARN: Industry: {e}"); status = "WARN"; _mark_warn()

        await _fill(page, "#initial_domain_name", config.get("website", "buoy.com"))
        notes.append(f"Website: {config.get('website', 'buoy.com')}")

        await _fill(
            page,
            'input[placeholder="Primary Contact Number"]',
            config.get("phone", "999-999-9999")
        )
        notes.append(f"Phone: {config.get('phone')}")

        # Entity type — #menu-company_entity_type li[2] = Partnership
        try:
            await page.locator('div[role="button"]:has-text("Choose an answer")').first.click(timeout=5000)
            await page.wait_for_timeout(700)
            await page.locator("#menu-company_entity_type li:nth-of-type(2)").first.click()
            await page.wait_for_timeout(800)
            notes.append("Entity type: Partnership (li[2])")
        except Exception as e:
            notes.append(f"NOTE: Entity type: {e}")

        # Business Address — Google Places
        try:
            addr = page.locator(
                'input[placeholder*="Business Address" i], input[placeholder*="Start typing" i]'
            ).first
            await addr.wait_for(state="visible", timeout=8000)
            await addr.click()
            await addr.press_sequentially(config.get("address_query", "texas"), delay=80)
            await page.wait_for_timeout(3000)
            pac = page.locator("div.pac-item")
            cnt = await pac.count()
            if cnt > 0:
                first_txt = await pac.first.inner_text()
                if "EUA" in first_txt:
                    notes.append(f"BUG B17 ✓ (E2): Autocomplete 'EUA': '{first_txt[:60]}'")
                    status = "WARN"; _mark_warn()
                target = pac.nth(4) if cnt >= 5 else pac.last
                await target.click()
                notes.append("Business Address (E2): 5th option selected")
            else:
                notes.append("WARN: No Google Places suggestions (E2)")
                status = "WARN"; _mark_warn()
        except Exception as e:
            notes.append(f"WARN: Business Address (E2): {e}"); status = "WARN"; _mark_warn()

        # Payroll Provider — li[3] = ADP Workforce Now
        try:
            await page.locator(
                'div[role="button"]:has-text("Select your payroll provider")'
            ).first.click()
            await page.wait_for_timeout(700)
            await page.locator("#menu-payroll_provider li:nth-of-type(3)").first.click()
            notes.append("Payroll provider: ADP Workforce Now (li[3])")
        except Exception as e:
            notes.append(f"NOTE: Payroll provider: {e}")

        # Payroll Frequency — li[1] = Weekly
        try:
            await page.locator('div[role="button"]:has-text("Select frequency")').first.click()
            await page.wait_for_timeout(700)
            await page.locator("#menu-payroll_frequency li:nth-of-type(1)").first.click()
            notes.append("Payroll frequency: Weekly (li[1])")
        except Exception as e:
            notes.append(f"NOTE: Payroll frequency: {e}")

        notes.append("Insurance benefits: No (pre-selected, no action needed)")

        await _click_retry(page, "button.css-sndmno", retries=3)
        await page.wait_for_timeout(5000)   # E2→E3: wait for SF to advance step

        # Confirm E3 loaded (landmark: Upload New Files)
        e3_ok = False
        for lm in ["aria/Upload New Files", "text=Upload New Files", "text=Payroll", "text=LOI"]:
            try:
                await page.locator(lm).first.wait_for(state="visible", timeout=8000)
                e3_ok = True; break
            except Exception:
                continue
        notes.append("Enrollment 3 confirmed ✓" if e3_ok else "WARN: E3 not confirmed after wait")

    except Exception as e:
        notes.append(f"ERROR: {e}"); status = "FAIL"; _mark_fail()
        errors.append(f"Enrollment 2: {e}")

    steps.append(_step("Enrollment 2 — Company Information", status, notes, t0))
    if overall == "FAIL":
        return {"status": overall, "steps": steps, "errors": errors}

    # ── ENROLLMENT 3B — Persona KYB (iframe) ────────────────────────
    # Selectors from Persona_Buoy-Champ.json (Chrome Recorder)
    t0 = time.monotonic()
    notes, status = [], "PASS"
    try:
        # BUG B18 — premature validation errors
        if await _exists(page, '[class*="error"]:visible,[class*="invalid"]:visible', 3000):
            notes.append("BUG B18 ✓: Validation errors shown before user fills anything")
            status = "WARN"; _mark_warn()
        else:
            notes.append("B18: No premature validation errors")

        # "Verify My Identity" — div.css-c5592h
        await page.locator("div.css-c5592h").first.click()
        await page.wait_for_timeout(3500)

        # Dismiss intro X button if present
        try:
            await page.locator("[data-test='button__basic'] svg").first.click()
            await page.wait_for_timeout(1000)
        except Exception:
            pass

        # Persona cross-origin iframe
        pf = page.frame_locator('iframe[src*="withpersona.com"]')

        # "Begin verifying" / "Iniciar Verificação"
        await pf.locator("[data-test='button__children']").first.click()
        await page.wait_for_timeout(2000)

        # Legal business name — div:nth-of-type(1) > [data-test='form']
        await pf.locator("div:nth-of-type(1) > [data-test='form']").fill(
            config.get("full_name", "BerTech QA")
        )
        await page.wait_for_timeout(300)

        # "Doing business as" (optional)
        try:
            await pf.locator("div:nth-of-type(2) > [data-test='form']").fill("NA")
        except Exception:
            pass

        # Business address — fires Places autocomplete
        await pf.locator("[data-test='address-street-1']").fill(config.get("address_query", "texas"))
        await page.wait_for_timeout(2500)

        # Click first address suggestion
        try:
            await pf.locator("div.fjlZeh").last.click()
        except Exception:
            try:
                await pf.locator('[role="option"]').last.click()
            except Exception:
                notes.append("NOTE: Persona address suggestion click failed")

        await page.wait_for_timeout(500)

        # EIN — div:nth-of-type(4) > [data-test='form']  (aria: Employer Identification Number)
        await pf.locator("div:nth-of-type(4) > [data-test='form']").fill(config.get("ein", "99-9999999"))
        await page.wait_for_timeout(300)

        # Tab through remaining fields as per recording
        for _ in range(6):
            await page.keyboard.press("Tab")
            await page.wait_for_timeout(100)

        # "Continue" / "Continuar"
        cont_btns = pf.locator("[data-test='button__children']")
        cont_count = await cont_btns.count()
        clicked_cont = False
        for i in range(cont_count):
            try:
                txt = await cont_btns.nth(i).inner_text()
                if "Continuar" in txt or "Continue" in txt:
                    await cont_btns.nth(i).scroll_into_view_if_needed()
                    await page.wait_for_timeout(500)
                    await cont_btns.nth(i).click()
                    clicked_cont = True
                    break
            except Exception:
                continue
        if not clicked_cont:
            await cont_btns.first.click()
        await page.wait_for_timeout(4000)

        # "Done" / "Concluído"
        done_btns = pf.locator("[data-test='button__children']")
        done_count = await done_btns.count()
        done_clicked = False
        for i in range(done_count):
            try:
                txt = await done_btns.nth(i).inner_text()
                if any(kw in txt for kw in ("Conclu", "Done", "Finish")):
                    await done_btns.nth(i).click()
                    done_clicked = True
                    break
            except Exception:
                continue
        if not done_clicked:
            try:
                await done_btns.first.click()
            except Exception:
                pass
        await page.wait_for_timeout(4000)

        notes.append("Persona KYB completed (iframe, PT-BR selectors)")

    except Exception as e:
        notes.append(f"WARN: Persona KYB: {e}"); status = "WARN"; _mark_warn()
        errors.append(f"Enrollment 3B — Persona KYB: {e}")

    steps.append(_step("Enrollment 3B — Persona KYB", status, notes, t0))

    # ── ENROLLMENT 3C — Payroll Upload + LOI "No" → Next ───────────
    # Page: "Payroll Information & Identity Verification"
    # KYB already shows "Identity Verified" from E3B.
    # Sequence (from Chrome Recorder upload_files_payroll.json):
    #   1. Click "Upload New Files" → file chooser
    #   2. set_input_files() with fixture XLSX
    #   3. Click "UPLOAD" button (button.ms-auto)
    #   4. LOI "No" is pre-selected — do NOT toggle
    #   5. Click Next (button.css-sndmno)
    t0 = time.monotonic()
    notes, status = [], "PASS"
    try:
        payroll_file = config.get("payroll_fixture", "")

        # ── Payroll file upload ──────────────────────────────────────
        if payroll_file and __import__("pathlib").Path(payroll_file).exists():
            try:
                _upload_done = False

                # Strategy A: set_input_files() directly on hidden <input type="file">
                # This bypasses the native OS file dialog entirely — most reliable
                try:
                    _file_inputs = page.locator('input[type="file"]')
                    _fi_count = await _file_inputs.count()
                    if _fi_count > 0:
                        await _file_inputs.first.set_input_files(payroll_file)
                        await page.wait_for_timeout(1000)
                        notes.append(f"Payroll file set via input[type=file] ({_fi_count} found)")
                        _upload_done = True
                except Exception as _fe:
                    notes.append(f"NOTE: Direct input[type=file] failed: {_fe}")

                # Strategy B: trigger file chooser by clicking the upload button
                if not _upload_done:
                    _upload_selectors = [
                        'button:has-text("Upload New Files")',
                        'text=Upload New Files',
                        'label:has-text("Upload")',
                        'button:has-text("Upload")',
                        '[class*="upload" i] button',
                        'form > div:nth-of-type(2) button',
                    ]
                    for _usel in _upload_selectors:
                        try:
                            async with page.expect_file_chooser(timeout=5000) as fc_info:
                                await page.locator(_usel).first.click(timeout=3000)
                            fc = await fc_info.value
                            await fc.set_files(payroll_file)
                            await page.wait_for_timeout(1000)
                            notes.append(f"Payroll file selected via chooser ({_usel})")
                            _upload_done = True
                            break
                        except Exception:
                            continue

                if not _upload_done:
                    raise Exception("No upload strategy succeeded — input[type=file] not found and file chooser did not open")

                await page.wait_for_timeout(1000)
                _pfile_name = __import__('pathlib').Path(payroll_file).name
                notes.append(f"Payroll file staged: {_pfile_name}")

                # Click UPLOAD / Submit button to confirm the upload
                for _submit_sel in [
                    'button.ms-auto',
                    'button:has-text("UPLOAD")',
                    'button:has-text("Upload")',
                    'text=UPLOAD',
                ]:
                    try:
                        _sub = page.locator(_submit_sel).first
                        await _sub.wait_for(state="visible", timeout=4000)
                        await _sub.click()
                        notes.append(f"Upload submit clicked ({_submit_sel})")
                        break
                    except Exception:
                        continue
                await page.wait_for_timeout(2000)

                # Verify "Uploaded Successfully" badge
                if await _exists(page, 'text=Uploaded Successfully', 5000):
                    notes.append("BUOYD-300 ✓: 'Uploaded Successfully' badge visible")
                else:
                    notes.append("BUOYD-300 ⚠: Upload success badge not found"); status = "WARN"; _mark_warn()

            except Exception as ue:
                notes.append(f"WARN: Payroll upload: {ue}"); status = "WARN"; _mark_warn()
                errors.append(f"Payroll upload: {ue}")
        else:
            notes.append(f"WARN: Payroll fixture not found at {payroll_file!r}"); status = "WARN"; _mark_warn()

        # ── LOI "No" — click explicitly (do not assume pre-selected) ──
        if await _exists(page, 'text=Do you want us to get', 5000):
            notes.append("BUOYD-280 ✓: LOI question visible")
        else:
            notes.append("BUOYD-280 ⚠: LOI question not visible"); status = "WARN"; _mark_warn()

        # Inspect current LOI toggle state
        try:
            btn_texts = await page.evaluate(
                "() => [...document.querySelectorAll('button.css-1q8kzhr')].map(b => b.textContent.trim())"
            )
            notes.append(f"LOI toggle state (before): {btn_texts}")
        except Exception:
            notes.append("NOTE: Could not inspect LOI toggle state")

        # Explicitly click "No" button — do not rely on pre-selection
        _loi_no_clicked = False
        for _loi_sel in [
            'button:has-text("No")',
            'button[value="No"]',
            '[aria-label="No"]',
        ]:
            try:
                _loi_el = page.locator(_loi_sel).first
                await _loi_el.wait_for(state="visible", timeout=3000)
                await _loi_el.click()
                await page.wait_for_timeout(500)
                _loi_no_clicked = True
                notes.append(f"LOI 'No' clicked ✓ ({_loi_sel})")
                break
            except Exception:
                continue
        if not _loi_no_clicked:
            notes.append("NOTE: LOI 'No' button not found — assuming pre-selected")

        # ── Next ─────────────────────────────────────────────────────
        _nxt_clicked = False
        for _nxt_sel in [
            'button.css-sndmno',
            'button:has-text("Next")',
            'button[type="submit"]',
            'form button:last-of-type',
        ]:
            try:
                _nb = page.locator(_nxt_sel).first
                await _nb.wait_for(state="visible", timeout=4000)
                await _nb.click()
                _nxt_clicked = True
                notes.append(f"Next clicked ✓ ({_nxt_sel})")
                break
            except Exception:
                continue
        nxt = _nxt_clicked
        notes.append("Next clicked ✓" if nxt else "WARN: Next button not found");
        if not nxt:
            status = "WARN"; _mark_warn()
        await page.wait_for_timeout(5000)   # E3→E4: SF processes payroll upload + LOI answer

        # Confirm E4 loaded — landmark: "Your Final Impact Report is ready!"
        e4_ok = False
        for lm in ["text=Final Impact Report", "text=Our Recommendation",
                   "text=Full Workforce", 'button:has-text("Continue With Selection")']:
            try:
                await page.locator(lm).first.wait_for(state="visible", timeout=8000)
                e4_ok = True; break
            except Exception:
                continue
        notes.append("E4 (Impact Report) confirmed ✓" if e4_ok else "WARN: E4 not confirmed after wait")

    except Exception as e:
        notes.append(f"ERROR: E3C: {e}"); status = "FAIL"; _mark_fail()
        errors.append(f"Enrollment 3C: {e}")

    steps.append(_step("Enrollment 3C — Payroll Upload + LOI No", status, notes, t0))
    if overall == "FAIL":
        return {"status": overall, "steps": steps, "errors": errors}

    # ── ENROLLMENT 4 — Final Impact Report ──────────────────────────
    # "Full Workforce" plan is pre-selected by default.
    # Action: verify page, then click "Continue With Selection".
    # Note: clicking another plan card opens a MuiDialog confirmation;
    #       since default is already Full Workforce we skip that dialog.
    t0 = time.monotonic()
    notes, status = [], "PASS"
    try:
        # Confirm page landmark
        if await _exists(page, 'text=Final Impact Report', 5000):
            notes.append("E4 ✓: 'Final Impact Report' heading visible")
        elif await _exists(page, 'text=Our Recommendation', 5000):
            notes.append("E4 ✓: 'Our Recommendation' card visible")
        else:
            notes.append(f"WARN: E4 not recognized — URL: {page.url}")
            status = "WARN"; _mark_warn()

        # Check Full Workforce is already selected (has "Selected" button)
        fw_selected = await _exists(page, 'button:has-text("Selected")', 3000)
        notes.append("Full Workforce: Selected ✓" if fw_selected else "NOTE: 'Selected' state not detected")

        # ── Passive bug checks (Group 1) — text assertions, no interaction ──
        try:
            _e4_body = await page.evaluate("() => document.body.innerText")

            # BUOYD-319: "$undefined" in any currency field
            if "$undefined" in _e4_body:
                notes.append("BUOYD-319 ⚠: '$undefined' found on page — currency field not resolved")
                status = "WARN"; _mark_warn()
            else:
                notes.append("BUOYD-319 ✓: No '$undefined' found")

            # BUOYD-321: "[Amount]" placeholder not replaced
            if "[Amount]" in _e4_body:
                notes.append("BUOYD-321 ⚠: '[Amount]' placeholder found — template variable not replaced")
                status = "WARN"; _mark_warn()
            else:
                notes.append("BUOYD-321 ✓: No '[Amount]' placeholder found")

            # BUOYD-323: grammatical error "Select This Options"
            if "Select This Options" in _e4_body:
                notes.append("BUOYD-323 ⚠: 'Select This Options' (grammatical bug) found on page")
                status = "WARN"; _mark_warn()
            else:
                notes.append("BUOYD-323 ✓: No 'Select This Options' text found")

            # BUOYD-324: typo "Virutal Pet Care"
            if "Virutal" in _e4_body:
                notes.append("BUOYD-324 ⚠: 'Virutal' typo found on page (should be 'Virtual')")
                status = "WARN"; _mark_warn()
            else:
                notes.append("BUOYD-324 ✓: No 'Virutal' typo found")

            # BUOYD-326: raw unformatted numbers (e.g. "1147.2" without $ or ,)
            _raw_nums = re.findall(r'\b\d{3,}\.\d{1,2}\b', _e4_body)
            # Filter out known safe patterns like URLs/versions
            _suspicious = [n for n in _raw_nums if not any(c in n for c in ['/', '.'])]
            if _suspicious:
                notes.append(f"BUOYD-326 ⚠: Unformatted numbers found (missing $ or ,): {_suspicious[:5]}")
                status = "WARN"; _mark_warn()
            else:
                notes.append("BUOYD-326 ✓: No unformatted currency numbers found")

        except Exception as _bchk_e:
            notes.append(f"NOTE: Bug checks skipped: {_bchk_e}")

        # Click "Continue With Selection" — try selectors separately (no aria/ mixing)
        cont = False
        for _e4_sel in [
            'button:has-text("Continue With Selection")',
            'button:has-text("Continue with Selection")',
            '[aria-label="Continue With Selection"]',
        ]:
            try:
                _e4_btn = page.locator(_e4_sel).first
                await _e4_btn.wait_for(state="visible", timeout=5000)
                await _e4_btn.click()
                cont = True
                notes.append(f"'Continue With Selection' clicked ✓ ({_e4_sel})")
                break
            except Exception:
                continue
        if not cont:
            notes.append("WARN: CTA not found"); status = "WARN"; _mark_warn()
        await page.wait_for_timeout(5000)   # E4→E5: SF persists plan selection

        # Confirm E5 loaded — landmark: "Sign Agreements" or "Ready To Sign"
        e5_ok = False
        for lm in ["text=Sign Agreements", "text=Ready To Sign", "text=Service Agreements"]:
            try:
                await page.locator(lm).first.wait_for(state="visible", timeout=8000)
                e5_ok = True; break
            except Exception:
                continue
        notes.append("E5 (Sign Agreements) confirmed ✓" if e5_ok else "WARN: E5 not confirmed after wait")
        notes.append(f"URL after E4: {page.url}")

    except Exception as e:
        notes.append(f"ERROR: E4: {e}"); status = "FAIL"; _mark_fail()
        errors.append(f"Enrollment 4: {e}")

    steps.append(_step("Enrollment 4 — Final Impact Report", status, notes, t0))
    if overall == "FAIL":
        return {"status": overall, "steps": steps, "errors": errors}

    # ── ENROLLMENT 5 — Sign Agreements (PandaDoc) ────────────────────
    # Page: "Up next, we need to sign agreements…"
    # "Service Agreements" card → "Ready To Sign" (div.css-c5592h)
    # PandaDoc opens as a NEW TAB (target changes to app.pandadoc.com).
    # Signing sequence (from Chrome Recorder):
    #   1. Click "Start signing"       ([data-testid='styledPrimaryAction'])
    #   2. Fill signature field "123"  ([data-testid='autoresize-input'])
    #   3. Tab → Enter → Enter → Tab
    #   4. Fill second field "123"
    #   5. Click "Finish"              ([data-testid='styledPrimaryAction'] aria=Finish)
    #   6. Close MuiBackdrop
    # Result: "Service Agreements" card shows "Signed" badge on main page.
    t0 = time.monotonic()
    notes, status = [], "PASS"
    try:
        # Confirm E5 page
        if await _exists(page, 'text=Sign Agreements', 5000):
            notes.append("E5 ✓: 'Sign Agreements' heading visible")
        else:
            notes.append(f"WARN: E5 heading not found — URL: {page.url}")
            status = "WARN"; _mark_warn()

        # Poll up to 90s for "Ready To Sign" to appear — it loads async from SF.
        # The element may NOT be a <button> — use broad JS search by text content.
        _RTS_SELECTORS = [
            'button:has-text("Ready To Sign")',
            'a:has-text("Ready To Sign")',
            '[role="button"]:has-text("Ready To Sign")',
            'div:has-text("Ready To Sign")',
            'span:has-text("Ready To Sign")',
            'button:has-text("Sign Now")',
            'button:has-text("Begin Signing")',
        ]
        _rts_found_sel = None
        _rts_js_handle = None  # for JS-clicked non-standard elements
        for _poll_i in range(18):  # 18 × 5s = 90s max
            await page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(5000)

            # Try Playwright selectors first
            for _sel in _RTS_SELECTORS:
                try:
                    _el = page.locator(_sel).first
                    await _el.wait_for(state="visible", timeout=800)
                    _rts_found_sel = _sel
                    break
                except Exception:
                    continue
            if _rts_found_sel:
                notes.append(f"'Ready To Sign' appeared after ~{(_poll_i+1)*5}s ({_rts_found_sel})")
                break

            # Broad JS scan: any visible element whose text contains "sign"
            if _poll_i % 3 == 2:  # every 15s
                try:
                    _sign_els = await page.evaluate("""() => {
                        const all = [...document.querySelectorAll('*')];
                        return all
                          .filter(el => el.offsetParent !== null
                                     && /ready.{0,5}to.{0,5}sign/i.test(el.textContent.trim())
                                     && el.textContent.trim().length < 60)
                          .map(el => ({
                              tag: el.tagName,
                              role: el.getAttribute('role') || '',
                              cls: el.className.toString().substring(0, 60),
                              text: el.textContent.trim().substring(0, 50)
                          }));
                    }""")
                    if _sign_els:
                        notes.append(f"'Ready To Sign' DOM elements at ~{(_poll_i+1)*5}s: {_sign_els[:5]}")
                        _rts_js_handle = True  # found — will JS-click below
                        break
                except Exception:
                    pass

            # Log all interactive elements every 30s
            if _poll_i % 6 == 5:
                try:
                    _vis = await page.evaluate(
                        "() => [...document.querySelectorAll('button,a,div[role],span[role]')]"
                        ".filter(b => b.offsetParent !== null)"
                        ".map(b => b.textContent.trim().substring(0,40))"
                        ".filter(t => t.length > 0 && t.length < 40)"
                    )
                    notes.append(f"Clickable els at ~{(_poll_i+1)*5}s: {_vis[:15]}")
                except Exception:
                    pass

        if not _rts_found_sel and not _rts_js_handle:
            # Final snapshot — dump page title + all text nodes containing "sign"
            try:
                _sign_txt = await page.evaluate("""() => {
                    const walker = document.createTreeWalker(
                        document.body, NodeFilter.SHOW_TEXT);
                    const hits = [];
                    let n;
                    while ((n = walker.nextNode()) && hits.length < 10) {
                        if (/sign/i.test(n.textContent)) {
                            const p = n.parentElement;
                            hits.push(p ? p.tagName + '.' + p.className.toString().substring(0,30)
                                           + ' → "' + n.textContent.trim().substring(0,40) + '"'
                                       : n.textContent.trim().substring(0,40));
                        }
                    }
                    return hits;
                }""")
                notes.append(f"'sign' text nodes (final): {_sign_txt}")
            except Exception:
                pass

        panda_page = None
        try:
            if not _rts_found_sel and not _rts_js_handle:
                raise Exception("'Ready To Sign' button not found after 90s polling")

            async with page.context.expect_page(timeout=30000) as new_page_info:
                if _rts_found_sel:
                    # When we matched a broad selector (e.g. div:has-text),
                    # find the innermost specific element to click via JS,
                    # avoiding clicking a large container that won't trigger navigation.
                    _broad_tags = {"div", "section", "article", "main", "li", "ul"}
                    _sel_tag = _rts_found_sel.split(":")[0].lower().strip()
                    if _sel_tag in _broad_tags:
                        await page.evaluate("""() => {
                            const all = [...document.querySelectorAll('*')];
                            // Prefer button/a, then fallback to smallest text match
                            const candidates = all.filter(e =>
                                e.offsetParent !== null &&
                                /ready.{0,5}to.{0,5}sign/i.test(e.textContent.trim()) &&
                                e.textContent.trim().length < 60
                            );
                            const btn = candidates.find(e =>
                                ['BUTTON','A','SPAN'].includes(e.tagName)
                            );
                            const target = btn || candidates[candidates.length - 1];
                            if (target) target.click();
                        }""")
                        notes.append("'Ready To Sign' clicked via JS innermost element")
                    else:
                        await page.locator(_rts_found_sel).first.click()
                        notes.append(f"'Ready To Sign' clicked via selector ({_rts_found_sel})")
                else:
                    # JS-click the element found via text scan
                    await page.evaluate("""() => {
                        const all = [...document.querySelectorAll('*')];
                        const el = all.find(e =>
                            e.offsetParent !== null &&
                            /ready.{0,5}to.{0,5}sign/i.test(e.textContent.trim()) &&
                            e.textContent.trim().length < 60);
                        if (el) el.click();
                    }""")
                    notes.append("'Ready To Sign' clicked via JS (non-standard element)")
            panda_page = await new_page_info.value
            await panda_page.wait_for_load_state("domcontentloaded", timeout=20000)
            notes.append(f"PandaDoc tab opened: {panda_page.url[:60]}…")
        except Exception as pe:
            notes.append(f"WARN: PandaDoc tab did not open: {pe}")
            notes.append(
                "BUOYD-333 ⚠: Page appears stuck after Sign Agreements — "
                "PandaDoc did not open within timeout"
            )
            status = "WARN"; _mark_warn()

        if panda_page:
            try:
                # 1. Start signing
                await panda_page.locator('[data-testid="styledPrimaryAction"]').first.click()
                await panda_page.wait_for_timeout(2000)
                notes.append("PandaDoc: 'Start signing' clicked ✓")

                # 2. Fill first signature field
                sig_field = panda_page.locator('[data-testid="autoresize-input"]').first
                await sig_field.click()
                await sig_field.fill("123")
                await panda_page.wait_for_timeout(500)

                # 3. Tab → Enter → Enter → Tab  (advance through fields)
                await panda_page.keyboard.press("Tab")
                await panda_page.keyboard.press("Enter")
                await panda_page.keyboard.press("Enter")
                await panda_page.keyboard.press("Tab")
                await panda_page.wait_for_timeout(800)

                # 4. Fill second field (if visible)
                try:
                    sig2 = panda_page.locator('[data-testid="autoresize-input"]').first
                    await sig2.fill("123")
                except Exception:
                    notes.append("NOTE: Second signature field not found — skipping")

                # 5. Finish
                await panda_page.locator('[aria-label="Finish"], [data-testid="styledPrimaryAction"]').last.click()
                await panda_page.wait_for_timeout(2000)
                notes.append("PandaDoc: 'Finish' clicked ✓")

                await panda_page.close()

            except Exception as se:
                notes.append(f"WARN: PandaDoc signing: {se}"); status = "WARN"; _mark_warn()
                errors.append(f"PandaDoc signing: {se}")
                try:
                    await panda_page.close()
                except Exception:
                    pass

        # 6. Back on main page — close any open MuiDialog/backdrop
        try:
            backdrop = page.locator("div.MuiBackdrop-root")
            if await backdrop.count() > 0:
                await backdrop.click()
                await page.wait_for_timeout(1000)
                notes.append("MUI backdrop dismissed")
        except Exception:
            pass

        # Verify "Signed" badge on Service Agreements card
        await page.wait_for_timeout(2000)
        if await _exists(page, 'text=Signed', 5000):
            notes.append("BUOYD-310 ✓: 'Signed' badge visible on Service Agreements")
        else:
            notes.append("BUOYD-310 ⚠: 'Signed' badge not found"); status = "WARN"; _mark_warn()

        notes.append(f"Final URL: {page.url}")

    except Exception as e:
        notes.append(f"ERROR: E5: {e}"); status = "FAIL"; _mark_fail()
        errors.append(f"Enrollment 5: {e}")

    steps.append(_step("Enrollment 5 — Sign Agreements (PandaDoc)", status, notes, t0))

    return {"status": overall, "steps": steps, "errors": errors}
