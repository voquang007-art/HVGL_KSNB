from __future__ import annotations

import hmac
import os
import re
import secrets
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from .database import init_db, fetch_user, ROLE_LABELS, UNIT_LABELS
from .routers import admin, auth, cash_control, chat, chat_api, control_statistics, draft_approval, files, future, meetings, nav_badges, revenue_control, vouchers

HVGL_KSNB_ENV = os.environ.get("HVGL_KSNB_ENV", "development").strip().lower()
HVGL_KSNB_PRODUCTION = HVGL_KSNB_ENV in {"production", "prod"}

SESSION_SECRET_KEY = os.environ.get("HVGL_KSNB_SESSION_SECRET_KEY", "").strip()
if not SESSION_SECRET_KEY:
    if HVGL_KSNB_PRODUCTION:
        raise RuntimeError("Thiếu biến môi trường HVGL_KSNB_SESSION_SECRET_KEY khi chạy production.")
    SESSION_SECRET_KEY = "DEV_ONLY_CHANGE_ME_HVGL_KSNB_SESSION_SECRET"

CSRF_SESSION_KEY = "_csrf_token"
CSRF_FORM_FIELD = "csrf_token"
CSRF_HEADER_NAME = "x-csrf-token"
CSRF_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

SESSION_COOKIE_HTTPS_ONLY = os.environ.get(
    "HVGL_KSNB_SESSION_HTTPS_ONLY",
    "1" if HVGL_KSNB_PRODUCTION else "0",
).strip().lower() in {"1", "true", "yes", "on"}

docs_url = None if HVGL_KSNB_PRODUCTION else "/docs"
redoc_url = None if HVGL_KSNB_PRODUCTION else "/redoc"
openapi_url = None if HVGL_KSNB_PRODUCTION else "/openapi.json"

app = FastAPI(
    title="HVGL KSNB",
    docs_url=docs_url,
    redoc_url=redoc_url,
    openapi_url=openapi_url,
)
app.mount("/static", StaticFiles(directory="app/static"), name="static")


@app.on_event("startup")
def startup() -> None:
    init_db()


def _ensure_csrf_token(request: Request) -> str:
    token = request.session.get(CSRF_SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        request.session[CSRF_SESSION_KEY] = token
    request.state.csrf_token = token
    return token


def _wants_json_response(request: Request) -> bool:
    requested_with = request.headers.get("x-requested-with", "").lower()
    accept = request.headers.get("accept", "").lower()
    return requested_with == "xmlhttprequest" or "application/json" in accept


def _csrf_error_response(request: Request):
    message = "Yêu cầu không hợp lệ hoặc đã hết phiên bảo mật CSRF. Vui lòng tải lại trang và thực hiện lại."
    if _wants_json_response(request):
        return JSONResponse({"ok": False, "error": message}, status_code=403)
    return PlainTextResponse(message, status_code=403)


async def _extract_csrf_token_from_request(request: Request) -> str:
    header_token = request.headers.get(CSRF_HEADER_NAME, "")
    if header_token:
        return header_token

    content_type = request.headers.get("content-type", "").lower()
    body = await request.body()

    async def receive():
        return {
            "type": "http.request",
            "body": body,
            "more_body": False,
        }

    request._receive = receive

    if "application/x-www-form-urlencoded" in content_type:
        parsed = parse_qs(body.decode("utf-8", errors="ignore"))
        values = parsed.get(CSRF_FORM_FIELD) or []
        return str(values[0] if values else "")

    if "multipart/form-data" in content_type:
        pattern = rb'name=["\']csrf_token["\'][^\r\n]*\r\n(?:[^\r\n]*\r\n)*\r\n(.*?)\r\n'
        match = re.search(pattern, body, flags=re.DOTALL)
        if match:
            return match.group(1).decode("utf-8", errors="ignore").strip()

    return ""


@app.middleware("http")
async def csrf_protect(request: Request, call_next):
    expected_token = _ensure_csrf_token(request)

    if request.method.upper() in CSRF_UNSAFE_METHODS:
        submitted_token = await _extract_csrf_token_from_request(request)
        if not submitted_token or not hmac.compare_digest(submitted_token, expected_token):
            return _csrf_error_response(request)

    return await call_next(request)


@app.middleware("http")
async def load_user(request: Request, call_next):
    user_id = request.session.get("user_id")
    request.state.user = fetch_user(user_id)
    response = await call_next(request)
    return response


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    if HVGL_KSNB_PRODUCTION:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response


# Đặt SessionMiddleware sau middleware load_user để khi chạy, session nằm ngoài và request.session đã sẵn sàng.
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET_KEY,
    same_site="lax",
    https_only=SESSION_COOKIE_HTTPS_ONLY,
)

app.include_router(auth.router)
app.include_router(admin.router)
app.include_router(vouchers.router)
app.include_router(cash_control.router)
app.include_router(control_statistics.router)
app.include_router(revenue_control.router)
app.include_router(files.router)
app.include_router(chat.router)
app.include_router(chat_api.router)
app.include_router(draft_approval.router)
app.include_router(meetings.router)
app.include_router(nav_badges.router)
app.include_router(future.router)

@app.get("/")
def home(request: Request):
    if not request.state.user:
        return RedirectResponse("/login", status_code=303)
    return RedirectResponse("/revenue-control", status_code=303)
