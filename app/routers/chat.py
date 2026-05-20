# -*- coding: utf-8 -*-
"""
app/routers/chat.py

Router giao diện cho module chat - giai đoạn 1.
Chỉ dựng khung màn hình:
- /chat
- /chat/{group_id}

Chưa triển khai sâu quyền đơn vị và WebSocket.
"""

from __future__ import annotations
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.config import settings
from app.database import SessionLocal, get_db
from app.security.deps import login_required
from app.chat.deps import get_display_name
from app.chat.realtime import manager
from app.models import UserUnitMemberships, Units, UserRoles, Roles, Users
from app.chat.service import (
    enrich_groups_for_list,
    get_available_users_for_group,
    get_group_by_id,
    get_group_members,
    get_group_messages,
    get_user_groups,
    is_group_member,
    list_message_reactions,
    mark_group_as_read,
    get_group_pinned_items,
)

from starlette.templating import Jinja2Templates
import os

router = APIRouter()
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "..", "templates"))


def _company_name() -> str:
    return getattr(settings, "COMPANY_NAME", "") or "Bệnh viện Hùng Vương Gia Lai"


def _app_name() -> str:
    return getattr(settings, "APP_NAME", "") or "Cổng làm việc Ban Kiểm soát nội bộ"


ROLE_CHAT_BOARD = {
    "TONG_GIAM_DOC",
    "PHO_TGD_THUONG_TRUC",
    "PHO_TONG_GIAM_DOC",
}

ROLE_CHAT_KSNB = {
    "TRUONG_BAN_KSNB",
    "PHO_TRUONG_BAN_KSNB",
    "NHAN_VIEN_KSNB",
}

ROLE_CHAT_TRUNG_TAP = {
    "THANH_VIEN_TRUNG_TAP",
}

UNIT_CHAT_KSNB = "BAN_KSNB"
UNIT_CHAT_HDTV = "HOI_DONG_THANH_VIEN"
UNIT_CHAT_TRUNG_TAP = "THANH_VIEN_TRUNG_TAP"


def _chat_attachment_preview_url(attachment_id: str) -> str:
    attachment_id = str(attachment_id or "").strip()
    return f"/chat/api/attachments/{attachment_id}/preview" if attachment_id else ""


def _chat_attachment_download_url(attachment_id: str) -> str:
    attachment_id = str(attachment_id or "").strip()
    return f"/chat/api/attachments/{attachment_id}/download" if attachment_id else ""

def _chat_load_role_codes(db: Session, user_id: str) -> set[str]:
    user = db.get(Users, user_id)
    direct_role_code = str(getattr(user, "role_code", "") or "").strip().upper() if user else ""
    if direct_role_code:
        return {direct_role_code}

    rows = (
        db.query(Roles.code)
        .join(UserRoles, UserRoles.role_id == Roles.id)
        .filter(UserRoles.user_id == user_id)
        .all()
    )
    result: set[str] = set()
    for (code,) in rows:
        raw = getattr(code, "value", code)
        clean_code = str(raw or "").strip().upper()
        if clean_code:
            result.add(clean_code)
    return result

def _chat_user_unit_code(user_obj) -> str:
    return str(getattr(user_obj, "unit_code", "") or "").strip().upper()


def _chat_user_role_code(user_obj) -> str:
    return str(getattr(user_obj, "role_code", "") or "").strip().upper()


def _chat_build_user_item(user_obj) -> dict:
    return {
        "id": str(getattr(user_obj, "id", "") or "").strip(),
        "label": _chat_user_option_label(user_obj),
        "search_text": (
            f"{getattr(user_obj, 'full_name', '')} "
            f"{getattr(user_obj, 'username', '')} "
            f"{getattr(user_obj, 'position_title', '')}"
        ).strip().lower(),
        "position_title": getattr(user_obj, "position_title", "") or "",
        "role_code": _chat_user_role_code(user_obj),
        "unit_code": _chat_user_unit_code(user_obj),
    }


def _chat_group_user_by_hvgl_ksnb(user_obj, role_codes: set[str]) -> str:
    unit_code = _chat_user_unit_code(user_obj)
    direct_role_code = _chat_user_role_code(user_obj)
    merged_roles = set(role_codes or set())
    if direct_role_code:
        merged_roles.add(direct_role_code)

    if unit_code == UNIT_CHAT_HDTV or (merged_roles & ROLE_CHAT_BOARD):
        return "BOARD"

    if unit_code == UNIT_CHAT_KSNB or (merged_roles & ROLE_CHAT_KSNB):
        return "KSNB"

    if unit_code == UNIT_CHAT_TRUNG_TAP or (merged_roles & ROLE_CHAT_TRUNG_TAP):
        return "TRUNG_TAP"

    return "OTHER"


def _chat_user_option_label(user_obj) -> str:
    full_name = (getattr(user_obj, "full_name", None) or "").strip()
    username = (getattr(user_obj, "username", None) or "").strip()
    if full_name and username:
        return f"{full_name} ({username})"
    return full_name or username or "Người dùng"


def _build_available_users_tree(db: Session, users: list[Users]) -> dict:
    """
    HVGL_KSNB:
    - Không dùng cây Phòng/Tổ của HVGL_Workspace.
    - Chỉ chia user theo 03 nhóm quản lý:
      1) Hội đồng thành viên
      2) Ban Kiểm soát nội bộ
      3) Thành viên trưng tập
    - Giữ nguyên cấu trúc trả về board_users/departments/others để không phải sửa room.html.
    """
    board_users: list[dict] = []
    ksnb_users: list[dict] = []
    seconded_users: list[dict] = []
    others: list[dict] = []

    for user_obj in users or []:
        uid = str(getattr(user_obj, "id", "") or "").strip()
        if not uid:
            continue

        role_codes = _chat_load_role_codes(db, uid)
        item = _chat_build_user_item(user_obj)
        group_key = _chat_group_user_by_hvgl_ksnb(user_obj, role_codes)

        if group_key == "BOARD":
            board_users.append(item)
            continue

        if group_key == "KSNB":
            ksnb_users.append(item)
            continue

        if group_key == "TRUNG_TAP":
            seconded_users.append(item)
            continue

        others.append(item)

    board_users.sort(key=lambda x: x["label"].casefold())
    ksnb_users.sort(key=lambda x: x["label"].casefold())
    seconded_users.sort(key=lambda x: x["label"].casefold())
    others.sort(key=lambda x: x["label"].casefold())

    departments = [
        {
            "id": UNIT_CHAT_KSNB,
            "name": "Ban Kiểm soát nội bộ",
            "teams": [],
            "direct_users": ksnb_users,
        },
        {
            "id": UNIT_CHAT_TRUNG_TAP,
            "name": "Thành viên trưng tập",
            "teams": [],
            "direct_users": seconded_users,
        },
    ]

    if others:
        departments.append(
            {
                "id": "OTHER",
                "name": "Khác",
                "teams": [],
                "direct_users": others,
            }
        )

    return {
        "board_users": board_users,
        "departments": departments,
        "others": [],
    }

def _ws_session_user_id(websocket: WebSocket) -> str | None:
    session = websocket.scope.get("session") or {}

    user_id = session.get("user_id")
    if user_id:
        return str(user_id)

    user_obj = session.get("user")
    if isinstance(user_obj, dict):
        uid = user_obj.get("id")
        if uid:
            return str(uid)

    uid = session.get("uid")
    if uid:
        return str(uid)

    return None
    
    
@router.get("/chat", response_class=HTMLResponse)
def chat_index(
    request: Request,
    db: Session = Depends(get_db),
):
    current_user = login_required(request, db)

    groups = get_user_groups(db, current_user.id)
    groups = enrich_groups_for_list(db, groups, current_user.id)

    return templates.TemplateResponse(
        "chat/index.html",
        {
            "request": request,
            "company_name": _company_name(),
            "app_name": _app_name(),
            "current_user": current_user,
            "current_user_display_name": get_display_name(current_user),
            "groups": groups,
            "active_group": None,
            "messages": [],
            "chat_notice": "Khung phân hệ chat đã sẵn sàng. Chúc làm việc vui vẻ.",
        },
    )


@router.get("/chat/{group_id}", response_class=HTMLResponse)
def chat_room(
    group_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    current_user = login_required(request, db)

    group = get_group_by_id(db, group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Không tìm thấy nhóm chat.")

    if not is_group_member(db, group_id, str(current_user.id)):
        raise HTTPException(status_code=403, detail="Bạn không thuộc nhóm chat này.")

    mark_group_as_read(db, group_id, str(current_user.id))

    groups = get_user_groups(db, str(current_user.id))
    groups = enrich_groups_for_list(db, groups, str(current_user.id))
    messages = get_group_messages(db, group_id, limit=100)
    group_members = get_group_members(db, group_id)
    available_users = get_available_users_for_group(db, group_id)
    available_users_tree = _build_available_users_tree(db, available_users)
    pinned_items = get_group_pinned_items(db, group_id)

    reaction_map = list_message_reactions(db, [m.id for m in messages])

    for msg in messages:
        msg.reaction_counts = reaction_map.get(msg.id, {"like": 0, "heart": 0, "laugh": 0})
        msg.is_mine = str(getattr(msg, "sender_user_id", "")) == str(current_user.id)

    for msg in messages:
        if getattr(msg, "created_at", None):
            msg.created_at_vn = msg.created_at + timedelta(hours=7)
        else:
            msg.created_at_vn = None

        for att in getattr(msg, "attachments", []) or []:
            attachment_id = str(getattr(att, "id", "") or "")
            setattr(att, "preview_url", _chat_attachment_preview_url(attachment_id))
            setattr(att, "download_url", _chat_attachment_download_url(attachment_id))

    return templates.TemplateResponse(
        "chat/room.html",
        {
            "request": request,
            "company_name": _company_name(),
            "app_name": _app_name(),
            "current_user": current_user,
            "current_user_display_name": get_display_name(current_user),
            "groups": groups,
            "active_group": group,
            "messages": messages,
            "group_members": group_members,
            "available_users": available_users,
            "available_users_tree": available_users_tree,
            "pinned_items": pinned_items,
            "chat_notice": " Đây là giao diện phòng chat. Hãy gửi tin hoặc file để trao đổi công việc nhóm.",
        },
    )


@router.websocket("/ws/chat/groups/{group_id}")
async def websocket_chat_group(
    websocket: WebSocket,
    group_id: str,
):
    user_id = _ws_session_user_id(websocket)
    if not user_id:
        await websocket.close(code=1008)
        return

    db = SessionLocal()
    try:
        if not is_group_member(db, group_id, user_id):
            await websocket.close(code=1008)
            return
    finally:
        db.close()

    await manager.connect_group(group_id, websocket)

    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect_group(group_id, websocket)
    except Exception:
        manager.disconnect_group(group_id, websocket)
        try:
            await websocket.close()
        except Exception:
            pass
            
@router.websocket("/ws/chat/notify")
async def websocket_chat_notify(
    websocket: WebSocket,
):
    user_id = _ws_session_user_id(websocket)
    if not user_id:
        await websocket.close(code=1008)
        return

    await manager.connect_notify(user_id, websocket)

    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect_notify(user_id, websocket)
    except Exception:
        manager.disconnect_notify(user_id, websocket)
        try:
            await websocket.close()
        except Exception:
            pass            