from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ...config import ENVIRONMENTS
from ...db import (
    add_scenario, create_run, create_suite, delete_scenario, delete_suite,
    get_conn, get_suite, list_runs, list_scenarios, list_suites,
)
from ...runner import run_suite_sync
from ...scenarios.registry import list_available

router = APIRouter()
templates = Jinja2Templates(
    directory=str(Path(__file__).parent.parent / "templates")
)


# ── suites list ──────────────────────────────────────────────────────

@router.get("/suites", response_class=HTMLResponse)
async def suites_page(request: Request):
    conn = get_conn()
    suites = list_suites(conn)
    conn.close()
    return templates.TemplateResponse(
        "suites.html",
        {
            "request": request,
            "title": "Suites — BerTech UAT",
            "nav": "suites",
            "suites": suites,
            "environments": ENVIRONMENTS,
        },
    )


@router.post("/suites")
async def create_suite_post(
    name: str = Form(...),
    description: str = Form(""),
):
    conn = get_conn()
    create_suite(conn, name, description)
    conn.close()
    return RedirectResponse("/suites", status_code=303)


@router.post("/suites/{suite_id}/delete")
async def delete_suite_post(suite_id: int):
    conn = get_conn()
    delete_suite(conn, suite_id)
    conn.close()
    return RedirectResponse("/suites", status_code=303)


# ── API endpoints (JSON) ─────────────────────────────────────────────

@router.get("/suites/api")
async def list_suites_api():
    conn = get_conn()
    suites = list_suites(conn)
    conn.close()
    return suites


@router.post("/suites/api")
async def create_suite_api(name: str = Form(...), description: str = Form("")):
    conn = get_conn()
    suite_id = create_suite(conn, name, description)
    conn.close()
    return {"id": suite_id, "name": name}


@router.post("/suites/{suite_id}/scenarios/api")
async def add_scenario_api(
    suite_id: int,
    script_key: str = Form(...),
    name: str = Form(""),
    description: str = Form(""),
):
    conn = get_conn()
    from ...scenarios.registry import REGISTRY_META
    label = name or REGISTRY_META.get(script_key, {}).get("label", script_key)
    add_scenario(conn, suite_id, label, script_key, description)
    conn.close()
    return {"suite_id": suite_id, "script_key": script_key, "label": label}


# ── suite detail ─────────────────────────────────────────────────────

@router.get("/suites/{suite_id}", response_class=HTMLResponse)
async def suite_detail(request: Request, suite_id: int):
    conn = get_conn()
    suite = get_suite(conn, suite_id)
    if not suite:
        conn.close()
        return HTMLResponse("Suite not found", status_code=404)
    scenarios = list_scenarios(conn, suite_id)
    runs = list_runs(conn, suite_id)
    conn.close()
    return templates.TemplateResponse(
        "suite_detail.html",
        {
            "request": request,
            "title": f"{suite['name']} — BerTech UAT",
            "nav": "suites",
            "suite": suite,
            "scenarios": scenarios,
            "runs": runs,
            "environments": ENVIRONMENTS,
            "available_scenarios": list_available(),
        },
    )


@router.post("/suites/{suite_id}/scenarios")
async def add_scenario_post(
    suite_id: int,
    script_key: str = Form(...),
    name: str = Form(""),
    description: str = Form(""),
):
    conn = get_conn()
    from ...scenarios.registry import REGISTRY_META
    label = name or REGISTRY_META.get(script_key, {}).get("label", script_key)
    add_scenario(conn, suite_id, label, script_key, description)
    conn.close()
    return RedirectResponse(f"/suites/{suite_id}", status_code=303)


@router.post("/suites/{suite_id}/scenarios/{scenario_id}/delete")
async def delete_scenario_post(suite_id: int, scenario_id: int):
    conn = get_conn()
    delete_scenario(conn, scenario_id)
    conn.close()
    return RedirectResponse(f"/suites/{suite_id}", status_code=303)


# ── trigger run ──────────────────────────────────────────────────────

@router.post("/suites/{suite_id}/run")
async def trigger_run(
    suite_id: int,
    background_tasks: BackgroundTasks,
    env: str = Form(...),
    num_users: int = Form(1),
    notes: str = Form(""),
):
    conn = get_conn()
    run_id = create_run(conn, suite_id, env, num_users, notes)
    conn.close()

    # run in background thread (Playwright needs its own event loop)
    background_tasks.add_task(run_suite_sync, run_id, suite_id, env, num_users)

    return RedirectResponse(f"/runs/{run_id}", status_code=303)
