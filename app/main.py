from __future__ import annotations

import logging
import threading
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .db import ensure_default_suite, get_conn, migrate
from .web.routers import suites as suites_router
from .web.routers import runs as runs_router
from .web.routers import analytics as analytics_router
from .web.routers import compare as compare_router
from .web.routers import latency as latency_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)

# ── DB startup ───────────────────────────────────────────────────────
_conn = get_conn()
migrate(_conn)
ensure_default_suite(_conn)
_conn.close()

# ── app ──────────────────────────────────────────────────────────────
app = FastAPI(title="BerTech UAT Runner", docs_url=None, redoc_url=None)

STATIC_DIR = Path(__file__).parent / "web" / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

templates = Jinja2Templates(directory=str(Path(__file__).parent / "web" / "templates"))

app.include_router(suites_router.router)
app.include_router(runs_router.router)
app.include_router(analytics_router.router)
app.include_router(compare_router.router)
app.include_router(latency_router.router)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    conn = get_conn()
    from .db import list_suites, list_runs
    from .config import ENVIRONMENTS
    suites = list_suites(conn)
    recent_runs = list_runs(conn)[:5]
    conn.close()
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "title": "BerTech UAT Runner",
            "nav": "dashboard",
            "suites": suites,
            "recent_runs": recent_runs,
            "environments": ENVIRONMENTS,
        },
    )
