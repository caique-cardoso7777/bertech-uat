# BerTech UAT Runner

Automated UAT tool for the **Buoy-Champ** benefits platform.
Simulates N concurrent users executing the full onboarding and enrollment flow via Playwright.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Web server | FastAPI + Uvicorn (port 8080) |
| Browser automation | Playwright async — headless Chromium |
| Database | SQLite (WAL mode) |
| Templates | Jinja2 |
| HTTP client | httpx |
| Email / OTP | mail.tm REST API |

---

## Features

- **Concurrent users** — up to 10 parallel headless browsers per run
- **Full flow coverage** — 13 steps from onboarding intro to PandaDoc signing
- **OTP handling** — mail.tm accounts pre-created sequentially before run (avoids rate limiting)
- **Bug checks** — automated assertions for known Buoy issues (BUOYD-274, 280, 300, 310, 319, 321, 323, 324, 326, 329, 333, 350)
- **Web UI** — Dashboard, Suites, Runs, Analytics, Compare, Latency tabs
- **JSON export** — canonical step normalization with SKIP for not-reached steps
- **HTML report** — print-friendly per-run report
- **Slack notifications** — post-run summary

---

## Scenario Steps (Buoy Champ Onboarding)

| # | Step |
|---|---|
| 1 | Onboarding 1 — Intro CTA |
| 2 | Onboarding 2 — Business Owner selection |
| 3 | Onboarding 3 — Company info form |
| 4 | Onboarding 4 — Census & Payroll (MUI selects) |
| 5 | Onboarding 5 — Impact Estimate Results |
| 6 | Sign Up — Email + password |
| 7 | OTP — 6-digit code via mail.tm |
| 8 | Enrollment 1 — Account Manager role |
| 9 | Enrollment 2 — Company Information form |
| 10 | Enrollment 3B — Persona KYB (iframe) |
| 11 | Enrollment 3C — Payroll upload + LOI No |
| 12 | Enrollment 4 — Final Impact Report + bug checks |
| 13 | Enrollment 5 — Sign Agreements (PandaDoc) |

---

## Quick Start

### 1. Install dependencies

```bash
pip install -r requirements.txt
playwright install chromium
```

### 2. Configure environment

Edit `app/config.py` to set target URLs and credentials:

```python
# Target environments
DEV_URL  = "https://dev.buoyhub.com"
UAT_URL  = "https://uat.buoyhub.com"
PROD_URL = "https://buoyhub.com"
```

### 3. Run the server

```bash
uvicorn app.main:app --reload --port 8080
```

Open **http://localhost:8080**

---

## Running a Test Suite

1. Go to **Suites** → create a suite
2. Add the `buoy_champ_onboarding` scenario
3. Click **Run** → select environment and number of users (1–10)
4. Monitor live on the **Runs** page
5. Export results as JSON or download the HTML report

---

## Target Environments

| Env | URL |
|---|---|
| dev | https://dev.buoyhub.com |
| uat | https://uat.buoyhub.com |
| prod | https://buoyhub.com |

---

## Known Buoy Bugs Being Tracked

| ID | Description | Step |
|---|---|---|
| BUOYD-274 | "Initial Impact Estimate" heading present | O5 |
| BUOYD-280 | LOI question visible | E3C |
| BUOYD-300 | "Uploaded Successfully" badge visible | E3C |
| BUOYD-310 | "Signed" badge missing after PandaDoc | E5 |
| BUOYD-319 | `$undefined` in currency fields | E4 |
| BUOYD-321 | `[Amount]` placeholder not replaced | E4 |
| BUOYD-323 | "Select This Options" grammatical error | E4 |
| BUOYD-324 | "Virutal" typo | E4 |
| BUOYD-326 | Unformatted currency numbers | E4 |
| BUOYD-329 | Redirect to `/dashboard` instead of enrollment | OTP |
| BUOYD-333 | Page stuck after Sign Agreements | E5 |
| BUOYD-350 | Concurrent signup limit — OTP not delivered | OTP |

---

## Project Structure

```
app/
├── main.py                          # FastAPI entry point
├── config.py                        # URLs, fixtures, timeouts
├── db.py                            # SQLite schema & queries
├── runner.py                        # Async orchestrator
├── slack.py                         # Slack notifications
├── scenarios/
│   ├── buoy_champ_onboarding.py     # Main scenario (13 steps)
│   └── registry.py                  # Scenario key → function map
└── web/
    ├── routers/                     # FastAPI route handlers
    └── templates/                   # Jinja2 HTML templates
```
