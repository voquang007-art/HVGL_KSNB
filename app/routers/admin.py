from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from ..database import ROLE_ADMIN, ROLE_LABELS, UNIT_LABELS, get_conn, hash_password

templates = Jinja2Templates(directory="app/templates")
router = APIRouter(prefix="/admin")


def require_admin(request: Request):
    user = request.state.user
    if not user:
        return RedirectResponse("/login", status_code=303)
    if user["role_code"] != ROLE_ADMIN:
        return RedirectResponse("/vouchers", status_code=303)
    return None


@router.get("/users")
def users_page(request: Request):
    denied = require_admin(request)
    if denied:
        return denied

    saved_user_id = str(request.query_params.get("saved_user_id") or "").strip()
    message = str(request.query_params.get("msg") or "").strip()

    with get_conn() as conn:
        users = conn.execute("SELECT * FROM users ORDER BY id DESC").fetchall()

    return templates.TemplateResponse(
        "admin_users.html",
        {
            "request": request,
            "users": users,
            "role_labels": ROLE_LABELS,
            "unit_labels": UNIT_LABELS,
            "saved_user_id": saved_user_id,
            "message": message,
        },
    )


@router.post("/users/{user_id}/assign")
def assign_user(
    request: Request,
    user_id: int,
    full_name: str = Form(...),
    unit_code: str = Form(...),
    role_code: str = Form(...),
    is_active: int = Form(1),
):
    denied = require_admin(request)
    if denied:
        return denied

    clean_full_name = full_name.strip()
    clean_position_title = ROLE_LABELS.get(role_code, role_code)

    with get_conn() as conn:
        conn.execute(
            """
            UPDATE users
            SET full_name = ?, unit_code = ?, role_code = ?, position_title = ?, is_active = ?
            WHERE id = ?
            """,
            (
                clean_full_name,
                unit_code,
                role_code,
                clean_position_title,
                int(is_active),
                user_id,
            ),
        )
        conn.commit()

    msg = quote("Đã lưu thông tin người dùng.")
    return RedirectResponse(f"/admin/users?saved_user_id={user_id}&msg={msg}", status_code=303)


@router.post("/users/create-admin-account")
def create_admin_account(
    request: Request,
    source_user_id: int = Form(...),
    admin_username: str = Form(...),
    admin_password: str = Form(...),
    confirm_text: str = Form(...),
):
    denied = require_admin(request)
    if denied:
        return denied

    clean_admin_username = admin_username.strip()
    clean_admin_password = admin_password.strip()
    clean_confirm_text = confirm_text.strip()

    if clean_confirm_text != "TOI XAC NHAN":
        msg = quote("Chưa xác nhận đúng nội dung tạo tài khoản Admin.")
        return RedirectResponse(f"/admin/users?msg={msg}", status_code=303)

    if not clean_admin_username or len(clean_admin_password) < 10:
        msg = quote("Tên tài khoản Admin không được trống và mật khẩu Admin phải tối thiểu 10 ký tự.")
        return RedirectResponse(f"/admin/users?msg={msg}", status_code=303)

    with get_conn() as conn:
        source_user = conn.execute(
            "SELECT * FROM users WHERE id = ?",
            (source_user_id,),
        ).fetchone()

        if not source_user:
            msg = quote("Không tìm thấy người dùng gốc để tạo tài khoản Admin.")
            return RedirectResponse(f"/admin/users?msg={msg}", status_code=303)

        existing = conn.execute(
            "SELECT id FROM users WHERE username = ?",
            (clean_admin_username,),
        ).fetchone()

        if existing:
            msg = quote("Tên tài khoản Admin đã tồn tại. Vui lòng chọn tên đăng nhập khác.")
            return RedirectResponse(f"/admin/users?msg={msg}", status_code=303)

        cur = conn.execute(
            """
            INSERT INTO users(username, full_name, password_hash, unit_code, role_code, position_title, is_active)
            VALUES (?, ?, ?, ?, ?, ?, 1)
            """,
            (
                clean_admin_username,
                source_user["full_name"],
                hash_password(clean_admin_password),
                source_user["unit_code"],
                ROLE_ADMIN,
                ROLE_LABELS.get(ROLE_ADMIN, "Admin"),
            ),
        )
        admin_user_id = cur.lastrowid
        conn.commit()

    msg = quote("Đã tạo tài khoản Admin riêng. Tài khoản nghiệp vụ gốc được giữ nguyên.")
    return RedirectResponse(f"/admin/users?saved_user_id={admin_user_id}&msg={msg}", status_code=303)