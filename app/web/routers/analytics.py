from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from ...db import fetch_all, get_conn

router = APIRouter()
templates = Jinja2Templates(
    directory=str(Path(__file__).parent.parent / "templates")
)


def _analytics_data() -> dict:
    conn = get_conn()
    runs = fetch_all(conn, """
        SELECT r.id, r.env, r.started_at, r.finished_at, r.status, r.num_users
        FROM runs r
        ORDER BY r.id ASC
    """)
    results = fetch_all(conn, """
        SELECT rr.run_id, rr.status, rr.steps_json, rr.duration_ms
        FROM run_results rr
        ORDER BY rr.run_id ASC, rr.user_idx ASC
    """)
    conn.close()

    # ── per-run outcome chart data ────────────────────────────────────
    run_labels = []
    run_pass = []
    run_warn = []
    run_fail = []
    run_durations = []  # total run wall-time in seconds

    from datetime import datetime, timezone

    for r in runs:
        run_id = r["id"]
        label = f"#{run_id}\n{r['env']}"
        run_labels.append(label)

        p = w = f = 0
        for rr in results:
            if rr["run_id"] != run_id:
                continue
            steps = json.loads(rr["steps_json"] or "[]")
            for s in steps:
                st = s.get("status", "")
                if st == "PASS":
                    p += 1
                elif st == "WARN":
                    w += 1
                elif st == "FAIL":
                    f += 1
        run_pass.append(p)
        run_warn.append(w)
        run_fail.append(f)

        # wall-time duration
        try:
            t_start = datetime.fromisoformat(r["started_at"].replace("Z", "+00:00"))
            t_end   = datetime.fromisoformat(r["finished_at"].replace("Z", "+00:00"))
            run_durations.append(round((t_end - t_start).total_seconds(), 1))
        except Exception:
            run_durations.append(0)

    # ── per-step aggregate across all runs ────────────────────────────
    step_stats: dict[str, dict] = {}  # name → {pass, warn, fail, total_ms, count}

    for rr in results:
        steps = json.loads(rr["steps_json"] or "[]")
        for s in steps:
            name = s.get("name", "?")
            st   = s.get("status", "")
            ms   = s.get("duration_ms", 0) or 0
            if name not in step_stats:
                step_stats[name] = {"pass": 0, "warn": 0, "fail": 0, "total_ms": 0, "count": 0}
            if st == "PASS":
                step_stats[name]["pass"] += 1
            elif st == "WARN":
                step_stats[name]["warn"] += 1
            elif st == "FAIL":
                step_stats[name]["fail"] += 1
            step_stats[name]["total_ms"] += ms
            step_stats[name]["count"] += 1

    # sort by natural order (first-seen ordering)
    step_names = list(step_stats.keys())
    step_pass_pct  = []
    step_warn_pct  = []
    step_fail_pct  = []
    step_avg_ms    = []

    for name in step_names:
        d = step_stats[name]
        total = d["count"] or 1
        step_pass_pct.append(round(d["pass"] / total * 100, 1))
        step_warn_pct.append(round(d["warn"] / total * 100, 1))
        step_fail_pct.append(round(d["fail"] / total * 100, 1))
        step_avg_ms.append(round(d["total_ms"] / total))

    # short labels for chart (strip "Onboarding", "Enrollment" prefix)
    def _short(n: str) -> str:
        for prefix in ("Onboarding ", "Enrollment ", "Sign Up ", "OTP "):
            if n.startswith(prefix):
                return n
        return n

    step_labels_short = [_short(n) for n in step_names]

    return {
        "run_labels":      run_labels,
        "run_pass":        run_pass,
        "run_warn":        run_warn,
        "run_fail":        run_fail,
        "run_durations":   run_durations,
        "step_labels":     step_labels_short,
        "step_pass_pct":   step_pass_pct,
        "step_warn_pct":   step_warn_pct,
        "step_fail_pct":   step_fail_pct,
        "step_avg_ms":     step_avg_ms,
        "total_runs":      len(runs),
    }


@router.get("/analytics", response_class=HTMLResponse)
async def analytics_page(request: Request):
    data = _analytics_data()
    return templates.TemplateResponse(
        "analytics.html",
        {
            "request": request,
            "title": "Analytics — BerTech UAT",
            "nav": "analytics",
            **data,
        },
    )
