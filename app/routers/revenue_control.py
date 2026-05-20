from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from ..database import BOARD_ROLES, KSNB_ROLES, ROLE_ADMIN

templates = Jinja2Templates(directory="app/templates")
router = APIRouter(prefix="/revenue-control")


def _user_value(user, field_name: str, default: str = "") -> str:
    if not user:
        return default

    try:
        return str(user[field_name] or default)
    except Exception:
        return str(getattr(user, field_name, default) or default)


@router.get("")
def revenue_control_index(request: Request):
    user = request.state.user
    if not user:
        return RedirectResponse("/login", status_code=303)

    role_code = _user_value(user, "role_code").strip().upper()
    if role_code not in (set(KSNB_ROLES) | set(BOARD_ROLES) | {ROLE_ADMIN}):
        return RedirectResponse("/chat", status_code=303)

    return templates.TemplateResponse(
        "revenue_control.html",
        {
            "request": request,
            "title": "Kiểm soát công tác thu, chi",
        },
    )