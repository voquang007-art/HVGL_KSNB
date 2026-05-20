from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(directory="app/templates")
router = APIRouter()


@router.get("/{module_name}")
def placeholder(request: Request, module_name: str):
    return RedirectResponse("/vouchers", status_code=303)