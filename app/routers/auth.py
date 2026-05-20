from __future__ import annotations

import os
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from ..database import (
    ROLE_THANH_VIEN_TRUNG_TAP,
    UNIT_TRUNG_TAP,
    get_conn,
    hash_password,
    password_needs_rehash,
    verify_password,
)

templates = Jinja2Templates(directory="app/templates")
router = APIRouter()

MAX_FAILED_LOGIN_ATTEMPTS = int(os.environ.get("HVGL_KSNB_MAX_FAILED_LOGIN_ATTEMPTS", "5"))
LOGIN_LOCK_SECONDS = int(os.environ.get("HVGL_KSNB_LOGIN_LOCK_SECONDS", "900"))


def _client_ip(request: Request) -> str:
    forwarded_for = request.headers.get("x-forwarded-for", "")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    if request.client and request.client.host:
        return request.client.host
    return ""


def _login_locked(conn, username: str) -> bool:
    window = f"-{LOGIN_LOCK_SECONDS} seconds"
    row = conn.execute(
        """
        SELECT COUNT(*) AS c
        FROM login_attempts
        WHERE lower(username) = lower(?)
          AND success = 0
          AND created_at >= datetime('now', ?)
        """,
        (username, window),
    ).fetchone()
    return int(row["c"] if row else 0) >= MAX_FAILED_LOGIN_ATTEMPTS


def _record_login_attempt(conn, username: str, ip_address: str, success: bool) -> None:
    conn.execute(
        """
        INSERT INTO login_attempts(username, ip_address, success)
        VALUES (?, ?, ?)
        """,
        (username, ip_address, 1 if success else 0),
    )


@router.get("/login")
def login_page(request: Request):
    if request.state.user:
        return RedirectResponse("/revenue-control", status_code=303)
    return templates.TemplateResponse("login.html", {"request": request, "error": None, "message": request.query_params.get("msg")})


@router.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    clean_username = username.strip()
    ip_address = _client_ip(request)

    with get_conn() as conn:
        if _login_locked(conn, clean_username):
            return templates.TemplateResponse(
                "login.html",
                {
                    "request": request,
                    "error": "Tài khoản đang bị khóa tạm thời do đăng nhập sai nhiều lần. Vui lòng thử lại sau.",
                    "message": None,
                },
                status_code=429,
            )

        user = conn.execute(
            "SELECT * FROM users WHERE username = ? AND is_active = 1",
            (clean_username,),
        ).fetchone()

        login_success = bool(user and verify_password(password, user["password_hash"]))
        _record_login_attempt(conn, clean_username, ip_address, login_success)

        if login_success and password_needs_rehash(user["password_hash"]):
            conn.execute(
                "UPDATE users SET password_hash = ? WHERE id = ?",
                (hash_password(password), user["id"]),
            )

        conn.commit()

    if not login_success:
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Tên đăng nhập hoặc mật khẩu không đúng.", "message": None},
        )

    request.session["user_id"] = user["id"]
    return RedirectResponse("/revenue-control", status_code=303)


@router.get("/register")
def register_page(request: Request):
    if request.state.user:
        return RedirectResponse("/vouchers", status_code=303)
    return templates.TemplateResponse("register.html", {"request": request, "error": None})


@router.post("/register")
def register(
    request: Request,
    username: str = Form(...),
    full_name: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
):
    username = username.strip()
    full_name = full_name.strip()
    if not username or not full_name or len(password) < 6:
        return templates.TemplateResponse("register.html", {"request": request, "error": "Vui lòng nhập đủ thông tin; mật khẩu tối thiểu 6 ký tự."})
    if password != confirm_password:
        return templates.TemplateResponse("register.html", {"request": request, "error": "Mật khẩu nhập lại không khớp."})
    try:
        with get_conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO users(username, full_name, password_hash, unit_code, role_code, position_title)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    username,
                    full_name,
                    hash_password(password),
                    UNIT_TRUNG_TAP,
                    ROLE_THANH_VIEN_TRUNG_TAP,
                    "Thành viên trưng tập",
                ),
            )
            conn.commit()
    except Exception:
        return templates.TemplateResponse("register.html", {"request": request, "error": "Tên đăng nhập đã tồn tại hoặc dữ liệu không hợp lệ."})
    return RedirectResponse(f"/login?msg={quote('Tài khoản đã được tạo. Vui lòng đăng nhập.')}", status_code=303)


@router.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
