from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

router = APIRouter()
templates = Jinja2Templates(
    directory=str(Path(__file__).parent.parent / "templates")
)

# In-memory: last check results per environment
_last_results: dict = {}  # env → list of probe results


async def _probe_endpoints(env: str, base_url: str) -> list[dict]:
    """Ping key Buoy endpoints and return latency results."""
    try:
        import httpx
    except ImportError:
        return [{"url": base_url, "status": None, "latency_ms": None,
                 "error": "httpx not installed"}]

    endpoints = [
        ("Home",        "/"),
        ("Onboarding",  "/onboarding-champ/"),
        ("Sign Up",     "/champ-signup"),
        ("Verify OTP",  "/verify-champ"),
        ("Enrollment",  "/buoy-champ/"),
    ]

    results = []
    async with httpx.AsyncClient(
        timeout=10.0,
        follow_redirects=True,
        headers={"User-Agent": "BerTech-UAT-LatencyProbe/1.0"},
    ) as client:
        for name, path in endpoints:
            url = base_url.rstrip("/") + path
            t0 = time.monotonic()
            try:
                r = await client.get(url)
                latency_ms = round((time.monotonic() - t0) * 1000)
                results.append({
                    "name":       name,
                    "url":        url,
                    "status":     r.status_code,
                    "latency_ms": latency_ms,
                    "ok":         r.status_code < 400,
                    "error":      None,
                })
            except Exception as e:
                latency_ms = round((time.monotonic() - t0) * 1000)
                results.append({
                    "name":       name,
                    "url":        url,
                    "status":     None,
                    "latency_ms": latency_ms,
                    "ok":         False,
                    "error":      str(e)[:80],
                })

    return results


@router.get("/latency", response_class=HTMLResponse)
async def latency_page(request: Request, env: str = "dev"):
    from ...config import ENVIRONMENTS
    envs = ENVIRONMENTS

    # Run probe on page load
    base_url = envs.get(env, {}).get("base_url", "https://dev.buoyhub.com")
    results = await _probe_endpoints(env, base_url)
    checked_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    _last_results[env] = {"results": results, "checked_at": checked_at}

    return templates.TemplateResponse("latency.html", {
        "request":    request,
        "title":      "Latency — BerTech UAT",
        "nav":        "latency",
        "envs":       envs,
        "active_env": env,
        "results":    results,
        "checked_at": checked_at,
    })


@router.post("/latency/check", response_class=HTMLResponse)
async def latency_check(request: Request, env: str = "dev"):
    """Re-probe and redirect back to latency page."""
    from fastapi.responses import RedirectResponse
    return RedirectResponse(f"/latency?env={env}", status_code=303)
