"""
app/main.py
------------
FastAPI application entry point for the V2 RFP Analyzer.

Routes:
  GET  /             → home.html (file upload UI); if ?opp_id= given, launches
                       opportunity analysis and redirects to /review/{job_id}
  GET  /review/{id}  → review.html (results viewer)
  POST /analyze      → upload file, kick off pipeline, return job_id
  GET  /jobs/{id}    → job status + results
  GET  /jobs         → list all jobs
  GET  /skills       → skill registry info
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator, Optional

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.routers import analysis as analysis_router
from app.routers import edits as edits_router
from app.routers import exports as exports_router
from app.routers import skills as skills_router
from logging_config import configure_logging

configure_logging()


# ─────────────────────────────────────────────────────────────────────────────
# Lifespan
# ─────────────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Startup and shutdown lifecycle hook."""
    from app.config import get_output_dir
    from app.job_store import get_job_store

    store = get_job_store()
    store.load_from_disk(get_output_dir())

    yield  # application runs here


# ─────────────────────────────────────────────────────────────────────────────
# App construction
# ─────────────────────────────────────────────────────────────────────────────

# Version stamp used as a cache-buster for static assets.
# Incremented at process startup so that a server restart invalidates
# any browser-cached copies of app.js / review.js / review-edit.js.
_STATIC_VER: str = str(int(time.time()))

app = FastAPI(
    title="RFP Analyzer V2",
    description="Skill-based archetype-routed RFP analysis pipeline",
    version="2.0.0",
    lifespan=lifespan,
)

# Static assets
_static_path = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(_static_path)), name="static")

# Jinja2 templates
_templates_path = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_templates_path))
# Make the static version stamp available to all templates so they can
# append ?v=<stamp> to static asset URLs for cache-busting.
templates.env.globals["static_ver"] = _STATIC_VER

# Include routers
app.include_router(analysis_router.router)
app.include_router(edits_router.router)
app.include_router(exports_router.router)
app.include_router(skills_router.router)


# ─────────────────────────────────────────────────────────────────────────────
# Page routes
# ─────────────────────────────────────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse, response_model=None)
async def home(
    request: Request,
    background_tasks: BackgroundTasks,
    opp_id: Optional[str] = Query(default=None),
) -> HTMLResponse | RedirectResponse:
    """Serve the home page (file upload).

    If ``?opp_id=`` is supplied, resolves the opportunity files, kicks off
    analysis (or reuses an existing active/complete job), and issues a 303
    redirect to ``/review/{job_id}`` so the browser lands on the status page.

    On resolution failure the home page is rendered with an ``error`` message.
    """
    if opp_id:
        from app.routers.analysis import start_opportunity_analysis
        try:
            job = await start_opportunity_analysis(
                opp_id=opp_id,
                background_tasks=background_tasks,
            )
        except HTTPException as exc:
            return templates.TemplateResponse(
                "home.html",
                {"request": request, "error": exc.detail},
                status_code=exc.status_code,
            )
        return RedirectResponse(url=f"/review/{job.job_id}", status_code=303)

    return templates.TemplateResponse("home.html", {"request": request})


@app.get("/review/{job_id}", response_class=HTMLResponse)
async def review(request: Request, job_id: str) -> HTMLResponse:
    """Serve the review page for a completed analysis job."""
    return templates.TemplateResponse("review.html", {"request": request, "job_id": job_id})


@app.get("/health")
async def health() -> dict:
    """Health check endpoint."""
    return {"status": "ok", "version": "2.0.0"}
