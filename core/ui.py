"""hone-core — the operator web UI (see ../ARCHITECTURE.md → Operator web UI).

Server-rendered: Jinja2 + Bootstrap 5 + HTMX. Skeleton: the page routes
render placeholders; the real views (live stats, queue/node actions, the
merge gate, manual-submission upload) and the operator login are TODO.
"""
import os

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

_HERE = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(_HERE, "templates"))

router = APIRouter(tags=["ui"])


@router.get("/", response_class=HTMLResponse)
async def overview(request: Request):
    return templates.TemplateResponse(request, "overview.html")


@router.get("/queue", response_class=HTMLResponse)
async def queue(request: Request):
    return templates.TemplateResponse(request, "page.html",
                                      {"page_title": "Queue management"})


@router.get("/submissions", response_class=HTMLResponse)
async def submissions(request: Request):
    return templates.TemplateResponse(request, "page.html",
                                      {"page_title": "Manual submissions"})


@router.get("/nodes", response_class=HTMLResponse)
async def nodes(request: Request):
    return templates.TemplateResponse(request, "page.html",
                                      {"page_title": "Node management"})


@router.get("/merge-gate", response_class=HTMLResponse)
async def merge_gate(request: Request):
    return templates.TemplateResponse(request, "page.html",
                                      {"page_title": "Merge gate"})


@router.get("/settings", response_class=HTMLResponse)
async def settings(request: Request):
    return templates.TemplateResponse(request, "page.html",
                                      {"page_title": "Settings"})
