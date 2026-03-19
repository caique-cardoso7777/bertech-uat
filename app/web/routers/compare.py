from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from ...db import get_conn, get_run, get_run_results, get_run_stats, list_runs

router = APIRouter()
templates = Jinja2Templates(
    directory=str(Path(__file__).parent.parent / "templates")
)

_STATUS_RANK = {"PASS": 0, "WARN": 1, "FAIL": 2, "error": 2, "SKIP": 3}


def _dominant_status(results: list) -> str:
    """Return the worst status across all users for a given step name."""
    best = "SKIP"
    for r in results:
        st = r.get("status", "SKIP")
        if _STATUS_RANK.get(st, 3) < _STATUS_RANK.get(best, 3):
            best = st
    return best


def _avg_duration(results: list) -> Optional[int]:
    vals = [r.get("duration_ms") for r in results if r.get("duration_ms")]
    return round(sum(vals) / len(vals)) if vals else None


def _build_step_map(results: list) -> dict:
    """step_name → list of {status, duration_ms} across all users."""
    m: dict = {}
    for r in results:
        for step in r.get("steps", []):
            name = step["name"]
            if name not in m:
                m[name] = []
            m[name].append({"status": step["status"], "duration_ms": step.get("duration_ms")})
    return m


def _is_regression(sa: str, sb: str) -> bool:
    return _STATUS_RANK.get(sb, 3) > _STATUS_RANK.get(sa, 3)


def _is_improvement(sa: str, sb: str) -> bool:
    return _STATUS_RANK.get(sb, 3) < _STATUS_RANK.get(sa, 3)


@router.get("/compare", response_class=HTMLResponse)
async def compare_page(request: Request, a: Optional[int] = None, b: Optional[int] = None):
    conn = get_conn()
    all_runs = list_runs(conn)

    run_a = run_b = None
    results_a = results_b = []
    stats: dict = {}
    step_rows = []
    user_rows = []

    if a:
        run_a = get_run(conn, a)
        if run_a:
            results_a = get_run_results(conn, a)
            stats[a] = get_run_stats(conn, a)

    if b:
        run_b = get_run(conn, b)
        if run_b:
            results_b = get_run_results(conn, b)
            stats[b] = get_run_stats(conn, b)

    if run_a and run_b:
        map_a = _build_step_map(results_a)
        map_b = _build_step_map(results_b)

        # canonical order: union of both, A first
        all_steps = list(map_a.keys())
        for s in map_b.keys():
            if s not in all_steps:
                all_steps.append(s)

        for name in all_steps:
            sa = _dominant_status(map_a.get(name, []))
            sb = _dominant_status(map_b.get(name, []))
            dur_a = _avg_duration(map_a.get(name, []))
            dur_b = _avg_duration(map_b.get(name, []))
            step_rows.append({
                "name":        name,
                "status_a":    sa,
                "status_b":    sb,
                "dur_a":       dur_a,
                "dur_b":       dur_b,
                "regression":  _is_regression(sa, sb),
                "improvement": _is_improvement(sa, sb),
            })

        # User rows — align by user_idx
        max_users = max(
            max((r["user_idx"] for r in results_a), default=-1),
            max((r["user_idx"] for r in results_b), default=-1),
        ) + 1
        by_user_a = {r["user_idx"]: r for r in results_a}
        by_user_b = {r["user_idx"]: r for r in results_b}

        for ui in range(max_users):
            ra = by_user_a.get(ui)
            rb = by_user_b.get(ui)
            sa = ra["status"] if ra else "SKIP"
            sb = rb["status"] if rb else "SKIP"
            user_rows.append({
                "user_idx":    ui,
                "status_a":    sa,
                "status_b":    sb,
                "dur_a":       ra.get("duration_ms") if ra else None,
                "dur_b":       rb.get("duration_ms") if rb else None,
                "regression":  _is_regression(sa, sb),
                "improvement": _is_improvement(sa, sb),
            })

    conn.close()
    return templates.TemplateResponse("compare.html", {
        "request":    request,
        "title":      "Compare Runs — BerTech UAT",
        "nav":        "compare",
        "all_runs":   all_runs,
        "run_a":      run_a,
        "run_b":      run_b,
        "stats":      stats,
        "step_rows":  step_rows,
        "user_rows":  user_rows,
    })
