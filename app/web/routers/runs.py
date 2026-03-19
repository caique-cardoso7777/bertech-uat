from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ...db import (
    fetch_one, get_conn, get_run, get_run_results, get_run_stats,
    get_suite, list_runs, update_run_status,
)

router = APIRouter()
templates = Jinja2Templates(
    directory=str(Path(__file__).parent.parent / "templates")
)


def _fmt_dt(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M:%S UTC")
    except Exception:
        return iso or "—"


# ── runs list ────────────────────────────────────────────────────────

@router.get("/runs", response_class=HTMLResponse)
async def runs_page(request: Request):
    conn = get_conn()
    runs = list_runs(conn)
    conn.close()
    for r in runs:
        r["started_at_display"] = _fmt_dt(r.get("started_at"))
        r["finished_at_display"] = _fmt_dt(r.get("finished_at"))
    return templates.TemplateResponse(
        "runs.html",
        {
            "request": request,
            "title": "Runs — BerTech UAT",
            "nav": "runs",
            "runs": runs,
        },
    )


# ── run detail ───────────────────────────────────────────────────────

@router.get("/runs/{run_id}", response_class=HTMLResponse)
async def run_detail(request: Request, run_id: int):
    conn = get_conn()
    run = get_run(conn, run_id)
    if not run:
        conn.close()
        return HTMLResponse("Run not found", status_code=404)

    suite = get_suite(conn, run["suite_id"])
    results = get_run_results(conn, run_id)
    stats = get_run_stats(conn, run_id)
    conn.close()

    run["started_at_display"] = _fmt_dt(run.get("started_at"))
    run["finished_at_display"] = _fmt_dt(run.get("finished_at"))

    # Build pivot: scenario_name → { user_idx → result }
    scenarios_map: dict = {}
    user_indices: set = set()
    for r in results:
        sn = r["scenario_name"]
        ui = r["user_idx"]
        user_indices.add(ui)
        if sn not in scenarios_map:
            scenarios_map[sn] = {}
        scenarios_map[sn][ui] = r

    user_indices_sorted = sorted(user_indices)

    return templates.TemplateResponse(
        "run_detail.html",
        {
            "request": request,
            "title": f"Run #{run_id} — BerTech UAT",
            "nav": "runs",
            "run": run,
            "suite": suite or {},
            "results": results,
            "stats": stats,
            "scenarios_map": scenarios_map,
            "user_indices": user_indices_sorted,
        },
    )


# ── cancel run ───────────────────────────────────────────────────────

@router.post("/runs/{run_id}/cancel")
async def cancel_run(run_id: int):
    conn = get_conn()
    conn.execute("UPDATE runs SET cancel_requested=1 WHERE id=?", (run_id,))
    conn.commit()
    conn.close()
    return RedirectResponse(f"/runs/{run_id}", status_code=303)


# ── export run ───────────────────────────────────────────────────────

@router.get("/runs/{run_id}/export")
async def export_run(run_id: int):
    conn = get_conn()
    run = get_run(conn, run_id)
    results = get_run_results(conn, run_id)
    suite = get_suite(conn, run["suite_id"]) if run else None
    conn.close()
    if not run:
        return JSONResponse({"error": "not found"}, status_code=404)

    # ── derive canonical step order from the user with the most steps ──
    canonical_steps: list[str] = []
    for r in results:
        names = [s["name"] for s in r.get("steps", [])]
        if len(names) > len(canonical_steps):
            canonical_steps = names

    # ── build normalized per-user results ────────────────────────────
    _SKIP_STEP = {"status": "SKIP", "notes": ["Not reached — upstream step failed"], "duration_ms": 0}

    users_out = []
    status_counts = {"PASS": 0, "WARN": 0, "FAIL": 0, "SKIP": 0}

    for r in results:
        steps_by_name = {s["name"]: s for s in r.get("steps", [])}

        # Pad to full canonical list
        full_steps = []
        for step_name in canonical_steps:
            if step_name in steps_by_name:
                s = steps_by_name[step_name]
                full_steps.append({
                    "name": s["name"],
                    "status": s["status"],
                    "duration_ms": s.get("duration_ms", 0),
                    "notes": s.get("notes", []),
                })
                status_counts[s["status"]] = status_counts.get(s["status"], 0) + 1
            else:
                full_steps.append({"name": step_name, **_SKIP_STEP})
                status_counts["SKIP"] += 1

        users_out.append({
            "user_idx":      r["user_idx"],
            "overall_status": r["status"],
            "email":         r.get("email_used", ""),
            "duration_ms":   r.get("duration_ms", 0),
            "errors":        r.get("errors", []),
            "steps":         full_steps,
        })

    # ── summary block ─────────────────────────────────────────────────
    from datetime import datetime
    def _dur(start, end):
        try:
            s = datetime.fromisoformat(start.replace("Z", "+00:00"))
            e = datetime.fromisoformat(end.replace("Z", "+00:00"))
            return round((e - s).total_seconds(), 1)
        except Exception:
            return None

    total_users  = len(users_out)
    users_pass   = sum(1 for u in users_out if u["overall_status"] == "PASS")
    users_warn   = sum(1 for u in users_out if u["overall_status"] == "WARN")
    users_fail   = sum(1 for u in users_out if u["overall_status"] == "FAIL")

    payload = {
        "summary": {
            "run_id":         run_id,
            "suite":          suite["name"] if suite else "",
            "environment":    run["env"],
            "started_at":     run.get("started_at"),
            "finished_at":    run.get("finished_at"),
            "wall_time_sec":  _dur(run.get("started_at", ""), run.get("finished_at", "")),
            "total_users":    total_users,
            "users_pass":     users_pass,
            "users_warn":     users_warn,
            "users_fail":     users_fail,
            "total_steps_executed": status_counts.get("PASS", 0) + status_counts.get("WARN", 0) + status_counts.get("FAIL", 0),
            "steps_pass":     status_counts.get("PASS", 0),
            "steps_warn":     status_counts.get("WARN", 0),
            "steps_fail":     status_counts.get("FAIL", 0),
            "steps_skip":     status_counts.get("SKIP", 0),
            "canonical_step_order": canonical_steps,
        },
        "users": users_out,
    }

    return JSONResponse(
        payload,
        headers={"Content-Disposition": f'attachment; filename="run_{run_id}.json"'},
    )


# ── HTML report ──────────────────────────────────────────────────────

@router.get("/runs/{run_id}/report", response_class=HTMLResponse)
async def run_report(request: Request, run_id: int):
    conn = get_conn()
    run = get_run(conn, run_id)
    if not run:
        conn.close()
        return HTMLResponse("Run not found", status_code=404)
    suite   = get_suite(conn, run["suite_id"])
    results = get_run_results(conn, run_id)
    stats   = get_run_stats(conn, run_id)
    conn.close()

    run["started_at_display"]  = _fmt_dt(run.get("started_at"))
    run["finished_at_display"] = _fmt_dt(run.get("finished_at"))

    def _wall(s, e):
        try:
            return round((datetime.fromisoformat(e.replace("Z","+00:00")) -
                          datetime.fromisoformat(s.replace("Z","+00:00"))).total_seconds(), 1)
        except Exception:
            return None

    return templates.TemplateResponse("run_report.html", {
        "request":      request,
        "run":          run,
        "suite_name":   suite["name"] if suite else f"Suite {run['suite_id']}",
        "results":      results,
        "stats":        stats,
        "wall_time_sec": _wall(run.get("started_at",""), run.get("finished_at","")),
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    })


# ── live status (polling) ────────────────────────────────────────────

@router.get("/runs/{run_id}/status")
async def run_status(run_id: int):
    conn = get_conn()
    run = get_run(conn, run_id)
    stats = get_run_stats(conn, run_id) if run else {}
    conn.close()
    if not run:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({
        "status": run["status"],
        "stats": stats,
        "started_at": run.get("started_at"),
        "finished_at": run.get("finished_at"),
    })
