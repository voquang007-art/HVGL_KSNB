# -*- coding: utf-8 -*-
"""
app/routers/chat_api.py

API khung cho module chat - giai đoạn 1.
Mục tiêu:
- Có endpoint tạo nhóm cơ bản.
- Có endpoint gửi tin nhắn cơ bản.
- Có endpoint upload file chat.
- Có endpoint chia sẻ nội bộ sang nhóm khác mà người dùng là thành viên.
"""

from __future__ import annotations

import json
import os
import re
import uuid
import shutil
import mimetypes
from urllib.parse import quote
from datetime import timedelta

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse, FileResponse
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.security.deps import login_required
from app.chat.models import ChatAttachments
from app.chat.realtime import manager
from app.upload_security import (
    CHAT_MAX_UPLOAD_MB,
    GENERAL_SAFE_UPLOAD_EXTENSIONS,
    UploadValidationError,
    safe_original_filename,
    save_upload_file_chunked,
    unique_stored_name,
)

from app.office_preview import (
    OfficePreviewError,
    ensure_office_pdf_preview,
    is_office_previewable,
)
from app.path_security import ensure_safe_file_path

from app.chat.service import (
    add_member_to_group,
    create_group,
    create_message,
    delete_attachment,
    delete_message,
    disband_group,
    get_active_message_attachments,
    get_existing_group_by_normalized_name,
    get_group_by_id,
    get_group_member_row,
    get_group_member_user_ids,
    get_group_new_message_count,
    get_message_attachments,
    get_message_by_id,
    get_user_groups,
    is_group_member,
    mark_group_as_read,
    normalize_group_name,
    recall_attachment,
    recall_message,
    remove_member_from_group,
    save_message_attachment,
    toggle_attachment_pin,
    toggle_message_pin,
    toggle_message_reaction,
    transfer_group_owner,
)

router = APIRouter()


CHAT_STICKER_STATIC_PREFIX = "/static/sticker/"
CHAT_STICKER_ALLOWED_EXTENSIONS = {".png", ".webp", ".gif", ".jpg", ".jpeg"}


def _chat_sticker_static_dir() -> str:
    return os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "static", "sticker")
    )


def _normalize_sticker_url(sticker_path: str) -> str:
    clean_path = str(sticker_path or "").strip().replace("\\", "/")
    if not clean_path.startswith(CHAT_STICKER_STATIC_PREFIX):
        return ""

    filename = os.path.basename(clean_path)
    if not filename or filename != clean_path[len(CHAT_STICKER_STATIC_PREFIX):]:
        return ""

    ext = os.path.splitext(filename)[1].lower()
    if ext not in CHAT_STICKER_ALLOWED_EXTENSIONS:
        return ""

    abs_path = os.path.abspath(os.path.join(_chat_sticker_static_dir(), filename))
    sticker_dir = _chat_sticker_static_dir()
    if not abs_path.startswith(sticker_dir + os.sep):
        return ""
    if not os.path.isfile(abs_path):
        return ""

    return CHAT_STICKER_STATIC_PREFIX + filename


def _list_chat_stickers() -> list[dict]:
    sticker_dir = _chat_sticker_static_dir()
    if not os.path.isdir(sticker_dir):
        return []

    stickers: list[dict] = []
    for filename in sorted(os.listdir(sticker_dir), key=lambda x: x.lower()):
        ext = os.path.splitext(filename)[1].lower()
        if ext not in CHAT_STICKER_ALLOWED_EXTENSIONS:
            continue
        if filename.startswith("."):
            continue
        stickers.append({
            "name": os.path.splitext(filename)[0],
            "filename": filename,
            "url": CHAT_STICKER_STATIC_PREFIX + filename,
        })
    return stickers


def _build_reply_preview(reply_msg, group_id: str) -> dict | None:
    if not reply_msg:
        return None
    if getattr(reply_msg, "group_id", None) != group_id:
        return None

    return {
        "id": reply_msg.id,
        "sender_name": (
            getattr(getattr(reply_msg, "sender", None), "full_name", None)
            or getattr(getattr(reply_msg, "sender", None), "username", None)
            or "Người dùng"
        ),
        "content": reply_msg.content or "",
    }




def _get_sender_name(user_obj) -> str:
    return (
        getattr(user_obj, "full_name", None)
        or getattr(user_obj, "username", None)
        or "Người dùng"
    )


def _build_content_disposition(disposition: str, filename: str) -> str:
    raw_name = (filename or "file").strip() or "file"

    ascii_fallback = raw_name.encode("ascii", "ignore").decode("ascii").strip()
    if not ascii_fallback:
        ascii_fallback = "file"

    ascii_fallback = ascii_fallback.replace("\\", "_").replace('"', "_")
    encoded_name = quote(raw_name, safe="")

    return f"{disposition}; filename=\"{ascii_fallback}\"; filename*=UTF-8''{encoded_name}"


def _stored_attachment_path_to_abs_path(path_value: str) -> str:
    clean = str(path_value or "").split("?", 1)[0].strip()
    if not clean:
        return ""

    if os.path.isabs(clean):
        candidate = os.path.abspath(clean)
    elif clean.startswith("/static/"):
        rel_path = clean.replace("/static/", "static/", 1).lstrip("/").replace("/", os.sep)
        candidate = os.path.abspath(os.path.join(settings.BASE_DIR, rel_path))
    else:
        candidate = os.path.abspath(os.path.join(settings.ROOT_DIR, clean.lstrip("/\\").replace("/", os.sep)))

    return ensure_safe_file_path(candidate)


def _chat_attachment_preview_url(attachment_id: str) -> str:
    attachment_id = str(attachment_id or "").strip()
    return f"/chat/api/attachments/{attachment_id}/preview" if attachment_id else ""


def _chat_attachment_download_url(attachment_id: str) -> str:
    attachment_id = str(attachment_id or "").strip()
    return f"/chat/api/attachments/{attachment_id}/download" if attachment_id else ""


def _is_chat_previewable(filename: str) -> bool:
    name = (filename or "").strip().lower()
    return (
        name.endswith(".pdf")
        or name.endswith(".png")
        or name.endswith(".jpg")
        or name.endswith(".jpeg")
        or name.endswith(".gif")
        or name.endswith(".webp")
        or name.endswith(".txt")
        or is_office_previewable(filename)
    )


def _ensure_chat_attachment_access(db: Session, attachment_id: str, user_id: str):
    attachment = db.get(ChatAttachments, attachment_id)
    if not attachment:
        raise HTTPException(status_code=404, detail="Không tìm thấy file.")

    if bool(getattr(attachment, "deleted_by_owner", False)) or bool(getattr(attachment, "recalled", False)):
        raise HTTPException(status_code=404, detail="File đã bị xóa hoặc thu hồi.")

    message = get_message_by_id(db, attachment.message_id)
    if not message:
        raise HTTPException(status_code=404, detail="Không tìm thấy tin nhắn chứa file.")

    if bool(getattr(message, "recalled", False)):
        raise HTTPException(status_code=404, detail="Tin nhắn chứa file đã bị thu hồi.")

    if not is_group_member(db, message.group_id, user_id):
        raise HTTPException(status_code=403, detail="Bạn không thuộc nhóm chat chứa file này.")

    return attachment, message


def _format_forwarded_content_with_attachments(
    message,
    *,
    source_group_name: str,
    source_sender_name: str,
    attachment_names: list[str],
) -> str:
    created_at_text = ""
    if getattr(message, "created_at", None):
        created_at_text = (message.created_at + timedelta(hours=7)).strftime("%d/%m/%Y %H:%M")

    lines = [
        f"[Chuyển tiếp từ nhóm: {source_group_name} | Người gửi gốc: {source_sender_name} | Thời gian gốc: {created_at_text}]"
    ]

    content = (getattr(message, "content", None) or "").strip()
    message_type = (getattr(message, "message_type", None) or "TEXT").strip().upper()

    if message_type == "FILE" and attachment_names:
        lines.append("Tệp gốc:")
        for name in attachment_names:
            lines.append(f"- {name}")

    if content:
        lines.append(content)

    return "\n".join([line for line in lines if line]).strip()
    
def _copy_attachments_to_target_group(
    db: Session,
    *,
    source_message,
    target_message,
    target_group_id: str,
) -> list[dict]:
    copied_attachments = []

    source_attachments = get_message_attachments(db, source_message.id)
    if not source_attachments:
        return copied_attachments
    abs_dir = os.path.abspath(os.path.join(settings.UPLOAD_DIR, "chat_uploads", target_group_id))
    os.makedirs(abs_dir, exist_ok=True)

    for att in source_attachments:
        src_abs_path = _stored_attachment_path_to_abs_path(att.path or "")

        if not os.path.isfile(src_abs_path):
            continue

        ext = os.path.splitext(att.filename or "")[1].lower()
        stored_name = f"{uuid.uuid4().hex}{ext}"
        dst_abs_path = os.path.join(abs_dir, stored_name)
        shutil.copy2(src_abs_path, dst_abs_path)

        rel_url = dst_abs_path

        new_attachment = save_message_attachment(
            db,
            message_id=target_message.id,
            filename=att.filename or os.path.basename(src_abs_path),
            stored_name=stored_name,
            path=rel_url,
            mime_type=getattr(att, "mime_type", None),
            size_bytes=getattr(att, "size_bytes", 0) or 0,
        )

        copied_attachments.append(_build_attachment_payload(new_attachment))

    return copied_attachments

def _build_attachment_payload(att) -> dict:
    attachment_id = str(getattr(att, "id", "") or "")
    filename = getattr(att, "filename", None) or "Tệp đính kèm"

    return {
        "id": att.id,
        "filename": filename,
        "path": att.path,
        "preview_url": _chat_attachment_preview_url(attachment_id),
        "download_url": _chat_attachment_download_url(attachment_id),
        "is_previewable": _is_chat_previewable(filename),
        "mime_type": att.mime_type,
        "size_bytes": att.size_bytes,
        "recalled": bool(getattr(att, "recalled", False)),
        "deleted_by_owner": bool(getattr(att, "deleted_by_owner", False)),
        "is_pinned": bool(getattr(att, "is_pinned", False)),
        "pinned_at_text": ((att.pinned_at + timedelta(hours=7)).strftime("%d/%m/%Y %H:%M")) if getattr(att, "pinned_at", None) else "",
        "pinned_by_name": _get_sender_name(getattr(att, "pinned_by", None)) if getattr(att, "pinned_by_user_id", None) else "",
    }


def _build_pin_item_payload(*, pin_kind: str, message=None, attachment=None) -> dict:
    if pin_kind == "attachment" and attachment is not None:
        source_message = message or getattr(attachment, "message", None)
        sender_name = _get_sender_name(getattr(source_message, "sender", None)) if source_message else "Người dùng"
        pinned_by_name = _get_sender_name(getattr(attachment, "pinned_by", None)) if getattr(attachment, "pinned_by_user_id", None) else ""
        pinned_at_text = ((attachment.pinned_at + timedelta(hours=7)).strftime("%d/%m/%Y %H:%M")) if getattr(attachment, "pinned_at", None) else ""
        return {
            "pin_kind": "attachment",
            "id": attachment.id,
            "message_id": source_message.id if source_message else attachment.message_id,
            "attachment_id": attachment.id,
            "label": "File",
            "title": attachment.filename or "Tệp đính kèm",
            "filename": attachment.filename or "Tệp đính kèm",
            "sender_name": sender_name,
            "pinned_by_name": pinned_by_name,
            "pinned_at_text": pinned_at_text,
            "is_pinned": bool(getattr(attachment, "is_pinned", False)),
        }

    source_message = message
    sender_name = _get_sender_name(getattr(source_message, "sender", None)) if source_message else "Người dùng"
    pinned_by_name = _get_sender_name(getattr(source_message, "pinned_by", None)) if getattr(source_message, "pinned_by_user_id", None) else ""
    pinned_at_text = ((source_message.pinned_at + timedelta(hours=7)).strftime("%d/%m/%Y %H:%M")) if getattr(source_message, "pinned_at", None) else ""
    return {
        "pin_kind": "message",
        "id": source_message.id if source_message else "",
        "message_id": source_message.id if source_message else "",
        "attachment_id": None,
        "label": "Tin nhắn",
        "title": (getattr(source_message, "content", None) or "Tin nhắn").strip() or "Tin nhắn",
        "filename": None,
        "sender_name": sender_name,
        "pinned_by_name": pinned_by_name,
        "pinned_at_text": pinned_at_text,
        "is_pinned": bool(getattr(source_message, "is_pinned", False)),
    }
    
def _build_message_payload(
    message,
    *,
    sender_name: str,
    reply_preview: dict | None = None,
    attachments: list | None = None,
) -> dict:
    created_at_text = ""
    if getattr(message, "created_at", None):
        created_at_text = (message.created_at + timedelta(hours=7)).strftime("%d/%m/%Y %H:%M")

    safe_attachments = attachments or []
    if bool(getattr(message, "recalled", False)):
        safe_attachments = []

    return {
        "id": message.id,
        "group_id": message.group_id,
        "sender_user_id": message.sender_user_id,
        "sender_name": sender_name,
        "content": message.content or "",
        "message_type": message.message_type,
        "recalled": bool(message.recalled),
        "created_at_text": created_at_text,
        "reply_to_message_id": getattr(message, "reply_to_message_id", None) or "",
        "reply_preview": reply_preview,
        "reaction_counts": {"like": 0, "heart": 0, "laugh": 0},
        "is_pinned": bool(getattr(message, "is_pinned", False)),
        "pinned_at_text": ((message.pinned_at + timedelta(hours=7)).strftime("%d/%m/%Y %H:%M")) if getattr(message, "pinned_at", None) else "",
        "pinned_by_name": _get_sender_name(getattr(message, "pinned_by", None)) if getattr(message, "pinned_by_user_id", None) else "",
        "attachments": safe_attachments,
    }


async def _notify_group_unread_to_members(
    db: Session,
    *,
    group_id: str,
    sender_user_id: str | None = None,
) -> None:
    member_user_ids = get_group_member_user_ids(
        db,
        group_id,
        exclude_user_id=sender_user_id,
    )

    for member_user_id in member_user_ids:
        unread_count = get_group_new_message_count(db, group_id, member_user_id)
        member_row = get_group_member_row(db, group_id, member_user_id)

        payload = {
            "type": "unread_update",
            "group_id": group_id,
            "new_message_count": unread_count,
            "is_new_group": bool(getattr(member_row, "is_new_group", False)) if member_row else False,
        }

        await manager.notify_user_text(
            member_user_id,
            json.dumps(payload, ensure_ascii=False),
        )


@router.get("/chat/api/stickers")
def api_list_stickers(
    request: Request,
    db: Session = Depends(get_db),
):
    login_required(request, db)
    return JSONResponse(
        {
            "ok": True,
            "stickers": _list_chat_stickers(),
        }
    )


@router.post("/chat/api/messages/sticker")
async def api_send_sticker_message(
    request: Request,
    group_id: str = Form(...),
    sticker_path: str = Form(...),
    reply_to_message_id: str = Form(""),
    db: Session = Depends(get_db),
):
    current_user = login_required(request, db)

    clean_group_id = (group_id or "").strip()
    clean_sticker_path = _normalize_sticker_url(sticker_path)
    clean_reply_to_message_id = (reply_to_message_id or "").strip()

    if not clean_group_id:
        raise HTTPException(status_code=400, detail="group_id không được để trống.")

    if not is_group_member(db, clean_group_id, str(current_user.id)):
        raise HTTPException(status_code=403, detail="Bạn không thuộc nhóm chat này.")

    if not clean_sticker_path:
        raise HTTPException(status_code=400, detail="Sticker không hợp lệ hoặc không tồn tại.")

    reply_preview = None
    if clean_reply_to_message_id:
        reply_msg = get_message_by_id(db, clean_reply_to_message_id)
        reply_preview = _build_reply_preview(reply_msg, clean_group_id)
        if not reply_preview:
            clean_reply_to_message_id = ""

    message = create_message(
        db,
        group_id=clean_group_id,
        sender_user_id=str(current_user.id),
        content=clean_sticker_path,
        message_type="STICKER",
        reply_to_message_id=clean_reply_to_message_id or None,
    )

    sender_name = _get_sender_name(current_user)

    message_payload = _build_message_payload(
        message,
        sender_name=sender_name,
        reply_preview=reply_preview,
        attachments=[],
    )

    await manager.broadcast_group_text(
        clean_group_id,
        json.dumps(
            {
                "type": "new_message",
                "message": message_payload,
            },
            ensure_ascii=False,
        ),
    )

    await _notify_group_unread_to_members(
        db,
        group_id=clean_group_id,
        sender_user_id=str(current_user.id),
    )

    return JSONResponse(
        {
            "ok": True,
            "message": {
                **message_payload,
                "is_mine": True,
            },
        }
    )


@router.post("/chat/api/groups/create")
def api_create_group(
    request: Request,
    name: str = Form(...),
    db: Session = Depends(get_db),
):
    current_user = login_required(request, db)

    clean_name = re.sub(r"\s+", " ", (name or "").strip())
    if not clean_name:
        return RedirectResponse(
            url="/chat?error=Tên nhóm không được để trống.",
            status_code=303,
        )

    normalized_name = normalize_group_name(clean_name)
    existing_group = get_existing_group_by_normalized_name(db, normalized_name)
    if existing_group:
        return RedirectResponse(
            url="/chat?error=Tên nhóm chat đã tồn tại. Vui lòng đặt tên khác.",
            status_code=303,
        )

    group = create_group(
        db,
        name=clean_name,
        owner_user_id=str(current_user.id),
        group_type="PRIVATE",
    )

    return RedirectResponse(
        url=f"/chat/{group.id}",
        status_code=303,
    )

@router.post("/chat/api/groups/{group_id}/members/add")
async def api_add_group_member(
    request: Request,
    group_id: str,
    user_ids: list[str] = Form([]),
    db: Session = Depends(get_db),
):
    current_user = login_required(request, db)

    group = get_group_by_id(db, group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Không tìm thấy nhóm chat.")

    if str(group.owner_user_id) != str(current_user.id):
        raise HTTPException(status_code=403, detail="Chỉ chủ nhóm mới được thêm thành viên ở giai đoạn này.")

    clean_user_ids: list[str] = []
    for raw_user_id in user_ids or []:
        uid = str(raw_user_id or "").strip()
        if uid and uid not in clean_user_ids:
            clean_user_ids.append(uid)

    if not clean_user_ids:
        raise HTTPException(status_code=400, detail="Chưa chọn người dùng để thêm vào nhóm.")

    for clean_user_id in clean_user_ids:
        add_member_to_group(
            db,
            group_id=group_id,
            user_id=clean_user_id,
            member_role="member",
            mark_as_new=True,
        )

        unread_count = get_group_new_message_count(db, group_id, clean_user_id)
        member_count = len(get_group_member_user_ids(db, group_id))

        payload = {
            "type": "group_new_badge",
            "group_id": group.id,
            "group_name": group.name,
            "group_type": group.group_type,
            "member_count": member_count,
            "new_message_count": unread_count,
            "is_new_group": True,
        }

        await manager.notify_user_text(
            clean_user_id,
            json.dumps(payload, ensure_ascii=False),
        )

    return RedirectResponse(
        url=f"/chat/{group_id}",
        status_code=303,
    )

@router.get("/chat/api/nav-badge")
def api_chat_nav_badge(
    request: Request,
    db: Session = Depends(get_db),
):
    current_user = login_required(request, db)

    groups = get_user_groups(db, current_user.id)

    has_new_group_badge = False
    new_group_count = 0

    for g in groups:
        row = get_group_member_row(db, g.id, current_user.id)
        if row and bool(getattr(row, "is_new_group", False)):
            has_new_group_badge = True
            new_group_count += 1

    return JSONResponse(
        {
            "ok": True,
            "has_new_group_badge": has_new_group_badge,
            "new_group_count": new_group_count,
        }
    )

@router.post("/chat/api/groups/{group_id}/members/{user_id}/remove")
def api_remove_group_member(
    request: Request,
    group_id: str,
    user_id: str,
    db: Session = Depends(get_db),
):
    current_user = login_required(request, db)

    group = get_group_by_id(db, group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Không tìm thấy nhóm chat.")

    is_self = current_user.id == user_id
    is_owner = group.owner_user_id == current_user.id

    if not (is_owner or is_self):
        raise HTTPException(status_code=403, detail="Bạn không có quyền thực hiện thao tác này.")

    if user_id == group.owner_user_id:
        raise HTTPException(status_code=400, detail="Trưởng nhóm không thể rời nhóm theo cách này. Hãy trao quyền hoặc giải tán nhóm.")

    remove_member_from_group(
        db,
        group_id=group_id,
        user_id=user_id,
    )

    if is_self:
        return RedirectResponse(url="/chat", status_code=303)

    return RedirectResponse(url=f"/chat/{group_id}", status_code=303)


@router.post("/chat/api/groups/{group_id}/owner/transfer")
def api_transfer_group_owner(
    request: Request,
    group_id: str,
    new_owner_user_id: str = Form(...),
    db: Session = Depends(get_db),
):
    current_user = login_required(request, db)

    group = get_group_by_id(db, group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Không tìm thấy nhóm chat.")

    if str(group.owner_user_id) != str(current_user.id):
        raise HTTPException(status_code=403, detail="Chỉ Trưởng nhóm hiện tại mới được trao quyền.")

    clean_new_owner_user_id = (new_owner_user_id or "").strip()
    if not clean_new_owner_user_id:
        raise HTTPException(status_code=400, detail="new_owner_user_id không được để trống.")

    if clean_new_owner_user_id == current_user.id:
        raise HTTPException(status_code=400, detail="Người nhận quyền đang là Trưởng nhóm hiện tại.")

    updated_group = transfer_group_owner(
        db,
        group_id=group_id,
        new_owner_user_id=clean_new_owner_user_id,
    )
    if not updated_group:
        raise HTTPException(status_code=400, detail="Không thể trao quyền Trưởng nhóm.")

    return RedirectResponse(url=f"/chat/{group_id}", status_code=303)


@router.post("/chat/api/groups/{group_id}/disband")
def api_disband_group(
    request: Request,
    group_id: str,
    db: Session = Depends(get_db),
):
    current_user = login_required(request, db)

    group = get_group_by_id(db, group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Không tìm thấy nhóm chat.")

    if group.owner_user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Chỉ Trưởng nhóm mới được giải tán nhóm.")

    disband_group(db, group_id=group_id)

    return RedirectResponse(url="/chat", status_code=303)


@router.post("/chat/api/messages/send")
async def api_send_message(
    request: Request,
    group_id: str = Form(...),
    content: str = Form(...),
    reply_to_message_id: str = Form(""),
    db: Session = Depends(get_db),
):
    current_user = login_required(request, db)

    clean_group_id = (group_id or "").strip()
    clean_content = (content or "").strip()
    clean_reply_to_message_id = (reply_to_message_id or "").strip()

    if not clean_group_id:
        raise HTTPException(status_code=400, detail="group_id không được để trống.")
    if not clean_content:
        raise HTTPException(status_code=400, detail="Nội dung tin nhắn không được để trống.")
    if not is_group_member(db, clean_group_id, str(current_user.id)):
        raise HTTPException(status_code=403, detail="Bạn không thuộc nhóm chat này.")

    reply_preview = None
    if clean_reply_to_message_id:
        reply_msg = get_message_by_id(db, clean_reply_to_message_id)
        reply_preview = _build_reply_preview(reply_msg, clean_group_id)
        if not reply_preview:
            clean_reply_to_message_id = ""

    message = create_message(
        db,
        group_id=clean_group_id,
        sender_user_id=str(current_user.id),
        content=clean_content,
        message_type="TEXT",
        reply_to_message_id=clean_reply_to_message_id or None,
    )

    sender_name = (
        getattr(current_user, "full_name", None)
        or getattr(current_user, "username", None)
        or "Người dùng"
    )

    message_payload = _build_message_payload(
        message,
        sender_name=sender_name,
        reply_preview=reply_preview,
        attachments=[],
    )

    await manager.broadcast_group_text(
        clean_group_id,
        json.dumps(
            {
                "type": "new_message",
                "message": message_payload,
            },
            ensure_ascii=False,
        ),
    )

    await _notify_group_unread_to_members(
        db,
        group_id=clean_group_id,
        sender_user_id=str(current_user.id),
    )

    wants_json = (
        request.headers.get("x-requested-with") == "XMLHttpRequest"
        or "application/json" in (request.headers.get("accept") or "")
    )

    if wants_json:
        return JSONResponse(
            {
                "ok": True,
                "message": {
                    **message_payload,
                    "is_mine": True,
                },
            }
        )
    
    return RedirectResponse(
        url=f"/chat/{clean_group_id}",
        status_code=303,
    )


@router.post("/chat/api/messages/{message_id}/react")
async def api_react_message(
    request: Request,
    message_id: str,
    reaction_type: str = Form(...),
    db: Session = Depends(get_db),
):
    current_user = login_required(request, db)

    message = get_message_by_id(db, message_id)
    if not message:
        raise HTTPException(status_code=404, detail="Không tìm thấy tin nhắn.")

    if not is_group_member(db, message.group_id, str(current_user.id)):
        raise HTTPException(status_code=403, detail="Bạn không thuộc nhóm chat này.")

    counts = toggle_message_reaction(
        db,
        message_id=message_id,
        user_id=str(current_user.id),
        reaction_type=reaction_type,
    )

    payload = {
        "type": "reaction_update",
        "message_id": message_id,
        "reaction_counts": counts,
    }

    await manager.broadcast_group_text(message.group_id, json.dumps(payload, ensure_ascii=False))

    return JSONResponse({"ok": True, "message_id": message_id, "reaction_counts": counts})


@router.post("/chat/api/messages/{message_id}/recall")
async def api_recall_message(
    request: Request,
    message_id: str,
    db: Session = Depends(get_db),
):
    current_user = login_required(request, db)

    message = get_message_by_id(db, message_id)
    if not message:
        raise HTTPException(status_code=404, detail="Không tìm thấy tin nhắn.")

    if not is_group_member(db, message.group_id, str(current_user.id)):
        raise HTTPException(status_code=403, detail="Bạn không thuộc nhóm chat này.")

    updated = recall_message(
        db,
        message_id=message_id,
        user_id=str(current_user.id),
    )
    if not updated:
        raise HTTPException(status_code=403, detail="Bạn không có quyền thu hồi tin nhắn này.")

    payload = {
        "type": "message_recalled",
        "message_id": updated.id,
        "message_type": updated.message_type,
        "content": updated.content or "",
    }

    await manager.broadcast_group_text(updated.group_id, json.dumps(payload, ensure_ascii=False))
    return JSONResponse({"ok": True, "message_id": updated.id})


@router.post("/chat/api/messages/{message_id}/delete")
async def api_delete_message(
    request: Request,
    message_id: str,
    db: Session = Depends(get_db),
):
    current_user = login_required(request, db)

    message = get_message_by_id(db, message_id)
    if not message:
        raise HTTPException(status_code=404, detail="Không tìm thấy tin nhắn.")

    if not is_group_member(db, message.group_id, str(current_user.id)):
        raise HTTPException(status_code=403, detail="Bạn không thuộc nhóm chat này.")

    group_id = message.group_id
    ok = delete_message(
        db,
        message_id=message_id,
        user_id=str(current_user.id),
    )
    if not ok:
        raise HTTPException(status_code=403, detail="Bạn không có quyền xóa tin nhắn này.")

    payload = {
        "type": "message_deleted",
        "message_id": message_id,
    }

    await manager.broadcast_group_text(group_id, json.dumps(payload, ensure_ascii=False))
    return JSONResponse({"ok": True, "message_id": message_id})
    

@router.post("/chat/api/messages/{message_id}/pin")
async def api_toggle_message_pin(
    request: Request,
    message_id: str,
    db: Session = Depends(get_db),
):
    current_user = login_required(request, db)

    updated = toggle_message_pin(
        db,
        message_id=message_id,
        user_id=str(current_user.id),
    )
    if not updated:
        raise HTTPException(status_code=403, detail="Không thể ghim/bỏ ghim tin nhắn này.")

    payload = {
        "type": "message_pin_toggled",
        "group_id": updated.group_id,
        "message_id": updated.id,
        "is_pinned": bool(updated.is_pinned),
        "pin_item": _build_pin_item_payload(pin_kind="message", message=updated),
    }
    await manager.broadcast_group_text(updated.group_id, json.dumps(payload, ensure_ascii=False))
    return JSONResponse({"ok": True, **payload})


@router.post("/chat/api/attachments/{attachment_id}/pin")
async def api_toggle_attachment_pin(
    request: Request,
    attachment_id: str,
    db: Session = Depends(get_db),
):
    current_user = login_required(request, db)

    updated = toggle_attachment_pin(
        db,
        attachment_id=attachment_id,
        user_id=str(current_user.id),
    )
    if not updated:
        raise HTTPException(status_code=403, detail="Không thể ghim/bỏ ghim file này.")

    message = get_message_by_id(db, updated.message_id)
    payload = {
        "type": "attachment_pin_toggled",
        "group_id": message.group_id if message else "",
        "message_id": updated.message_id,
        "attachment_id": updated.id,
        "is_pinned": bool(updated.is_pinned),
        "pin_item": _build_pin_item_payload(pin_kind="attachment", message=message, attachment=updated),
    }
    if message:
        await manager.broadcast_group_text(message.group_id, json.dumps(payload, ensure_ascii=False))
    return JSONResponse({"ok": True, **payload})
    
@router.post("/chat/api/groups/{group_id}/read")
async def api_mark_group_read(
    request: Request,
    group_id: str,
    db: Session = Depends(get_db),
):
    current_user = login_required(request, db)

    clean_group_id = (group_id or "").strip()
    if not clean_group_id:
        raise HTTPException(status_code=400, detail="group_id không được để trống.")

    if not is_group_member(db, clean_group_id, str(current_user.id)):
        raise HTTPException(status_code=403, detail="Bạn không thuộc nhóm chat này.")

    ok = mark_group_as_read(db, clean_group_id, str(current_user.id))
    if not ok:
        raise HTTPException(status_code=400, detail="Không thể cập nhật trạng thái đã đọc.")

    payload = {
        "type": "unread_update",
        "group_id": clean_group_id,
        "new_message_count": 0,
        "is_new_group": False,
    }

    await manager.notify_user_text(
        str(current_user.id),
        json.dumps(payload, ensure_ascii=False),
    )

    return JSONResponse(
        {
            "ok": True,
            "group_id": clean_group_id,
            "new_message_count": 0,
            "is_new_group": False,
        }
    )


@router.get("/chat/api/attachments/{attachment_id}/preview")
def api_preview_attachment(
    request: Request,
    attachment_id: str,
    db: Session = Depends(get_db),
):
    current_user = login_required(request, db)
    attachment, _message = _ensure_chat_attachment_access(db, attachment_id, str(current_user.id))

    filename = getattr(attachment, "filename", None) or getattr(attachment, "stored_name", None) or "tep_dinh_kem"
    file_path = _stored_attachment_path_to_abs_path(getattr(attachment, "path", "") or "")

    if not file_path or not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="Không tìm thấy file trên máy chủ.")

    if is_office_previewable(filename):
        try:
            preview_path = ensure_office_pdf_preview(
                source_path=file_path,
                preview_key=f"chat_{attachment_id}",
                original_name=filename,
            )
        except OfficePreviewError as ex:
            raise HTTPException(
                status_code=500,
                detail=(
                    "Không tạo được bản xem trước PDF. "
                    "Vui lòng kiểm tra LibreOffice/soffice trên máy chủ. "
                    f"Chi tiết: {str(ex)}"
                ),
            )

        preview_filename = f"{os.path.splitext(filename)[0]}.pdf"
        return FileResponse(
            preview_path,
            media_type="application/pdf",
            headers={"Content-Disposition": _build_content_disposition("inline", preview_filename)},
        )

    media_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    return FileResponse(
        file_path,
        media_type=media_type,
        headers={"Content-Disposition": _build_content_disposition("inline", filename)},
    )


@router.get("/chat/api/attachments/{attachment_id}/download")
def api_download_attachment(
    request: Request,
    attachment_id: str,
    db: Session = Depends(get_db),
):
    current_user = login_required(request, db)
    attachment, _message = _ensure_chat_attachment_access(db, attachment_id, str(current_user.id))

    filename = getattr(attachment, "filename", None) or getattr(attachment, "stored_name", None) or "tep_dinh_kem"
    file_path = _stored_attachment_path_to_abs_path(getattr(attachment, "path", "") or "")

    if not file_path or not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="Không tìm thấy file trên máy chủ.")

    media_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"

    return FileResponse(
        file_path,
        media_type=media_type,
        headers={"Content-Disposition": _build_content_disposition("attachment", filename)},
    )
    
@router.post("/chat/api/attachments/{attachment_id}/recall")
async def api_recall_attachment(
    request: Request,
    attachment_id: str,
    db: Session = Depends(get_db),
):
    current_user = login_required(request, db)

    result = recall_attachment(
        db,
        attachment_id=attachment_id,
        user_id=str(current_user.id),
    )
    if not result:
        raise HTTPException(status_code=403, detail="Bạn không có quyền thu hồi file này.")

    attachment, message = result

    payload = {
        "type": "attachment_recalled",
        "attachment_id": attachment.id,
        "message_id": message.id,
        "filename": attachment.filename,
        "content": message.content or "",
    }

    await manager.broadcast_group_text(message.group_id, json.dumps(payload, ensure_ascii=False))
    return JSONResponse(
        {
            "ok": True,
            "attachment_id": attachment.id,
            "message_id": message.id,
            "filename": attachment.filename,
            "content": message.content or "",
        }
    )


@router.post("/chat/api/attachments/{attachment_id}/delete")
async def api_delete_attachment(
    request: Request,
    attachment_id: str,
    db: Session = Depends(get_db),
):
    current_user = login_required(request, db)

    result = delete_attachment(
        db,
        attachment_id=attachment_id,
        user_id=str(current_user.id),
    )
    if not result:
        raise HTTPException(status_code=403, detail="Bạn không có quyền xóa file này.")

    attachment, message = result

    payload = {
        "type": "attachment_deleted",
        "attachment_id": attachment.id,
        "message_id": message.id,
        "filename": attachment.filename,
        "content": message.content or "",
    }

    await manager.broadcast_group_text(message.group_id, json.dumps(payload, ensure_ascii=False))
    return JSONResponse(
        {
            "ok": True,
            "attachment_id": attachment.id,
            "message_id": message.id,
            "filename": attachment.filename,
            "content": message.content or "",
        }
    )

    
@router.post("/chat/api/messages/upload")
async def api_upload_message_file(
    request: Request,
    group_id: str = Form(...),
    file: UploadFile = File(...),
    reply_to_message_id: str = Form(""),
    db: Session = Depends(get_db),
):
    current_user = login_required(request, db)

    clean_group_id = (group_id or "").strip()
    clean_reply_to_message_id = (reply_to_message_id or "").strip()

    if not clean_group_id:
        raise HTTPException(status_code=400, detail="group_id không được để trống.")
    if not is_group_member(db, clean_group_id, str(current_user.id)):
        raise HTTPException(status_code=403, detail="Bạn không thuộc nhóm chat này.")
    if not file or not file.filename:
        raise HTTPException(status_code=400, detail="Chưa chọn file.")

    original_name = safe_original_filename(file.filename)
    stored_name = unique_stored_name(original_name)
    abs_dir = os.path.abspath(os.path.join(settings.UPLOAD_DIR, "chat_uploads", clean_group_id))
    os.makedirs(abs_dir, exist_ok=True)

    abs_path = os.path.join(abs_dir, stored_name)
    try:
        size_bytes, _sha256_value, original_name = save_upload_file_chunked(
            file,
            abs_path,
            allowed_extensions=GENERAL_SAFE_UPLOAD_EXTENSIONS,
            max_size_mb=CHAT_MAX_UPLOAD_MB,
        )
    except UploadValidationError as ex:
        raise HTTPException(status_code=400, detail=str(ex)) from ex

    reply_preview = None
    if clean_reply_to_message_id:
        reply_msg = get_message_by_id(db, clean_reply_to_message_id)
        reply_preview = _build_reply_preview(reply_msg, clean_group_id)
        if not reply_preview:
            clean_reply_to_message_id = ""

    message = create_message(
        db,
        group_id=clean_group_id,
        sender_user_id=str(current_user.id),
        content=original_name,
        message_type="FILE",
        reply_to_message_id=clean_reply_to_message_id or None,
    )

    rel_url = abs_path

    attachment = save_message_attachment(
        db,
        message_id=message.id,
        filename=original_name,
        stored_name=stored_name,
        path=rel_url,
        mime_type=file.content_type,
        size_bytes=size_bytes,
    )

    sender_name = (
        getattr(current_user, "full_name", None)
        or getattr(current_user, "username", None)
        or "Người dùng"
    )

    payload_message = _build_message_payload(
        message,
        sender_name=sender_name,
        reply_preview=reply_preview,
        attachments=[_build_attachment_payload(attachment)],
    )

    await manager.broadcast_group_text(
        clean_group_id,
        json.dumps(
            {
                "type": "new_message",
                "message": payload_message,
            },
            ensure_ascii=False,
        ),
    )

    await _notify_group_unread_to_members(
        db,
        group_id=clean_group_id,
        sender_user_id=str(current_user.id),
    )

    return JSONResponse({"ok": True, "message": {**payload_message, "is_mine": True}})

@router.post("/chat/api/messages/{message_id}/share")
async def api_share_message(
    request: Request,
    message_id: str,
    target_group_id: str = Form(...),
    db: Session = Depends(get_db),
):
    current_user = login_required(request, db)

    message = get_message_by_id(db, message_id)
    if not message:
        raise HTTPException(status_code=404, detail="Không tìm thấy tin nhắn.")

    if not is_group_member(db, message.group_id, str(current_user.id)):
        raise HTTPException(status_code=403, detail="Bạn không thuộc nhóm chứa tin nhắn gốc.")

    clean_target_group_id = (target_group_id or "").strip()
    if not clean_target_group_id:
        raise HTTPException(status_code=400, detail="Chưa chọn nhóm nhận.")

    target_group = get_group_by_id(db, clean_target_group_id)
    if not target_group or not getattr(target_group, "is_active", False):
        raise HTTPException(status_code=404, detail="Không tìm thấy nhóm nhận.")

    if clean_target_group_id == message.group_id:
        raise HTTPException(status_code=400, detail="Không chuyển tiếp vào chính nhóm hiện tại.")

    allowed_group_ids = {g.id for g in get_user_groups(db, str(current_user.id))}
    if clean_target_group_id not in allowed_group_ids:
        raise HTTPException(status_code=403, detail="Bạn không thuộc nhóm nhận đã chọn.")

    if bool(getattr(message, "recalled", False)):
        raise HTTPException(status_code=400, detail="Tin nhắn gốc đã được thu hồi, không thể chuyển tiếp.")

    source_group = get_group_by_id(db, message.group_id)
    source_group_name = getattr(source_group, "name", None) or "Nhóm chat"
    source_sender_name = _get_sender_name(getattr(message, "sender", None))
    source_attachments = get_message_attachments(db, message.id)
    attachment_names = [
        att.filename
        for att in source_attachments
        if getattr(att, "filename", None)
    ]

    forwarded_content = _format_forwarded_content_with_attachments(
        message,
        source_group_name=source_group_name,
        source_sender_name=source_sender_name,
        attachment_names=attachment_names,
    )

    if not forwarded_content.strip() and not source_attachments:
        raise HTTPException(status_code=400, detail="Không có nội dung hợp lệ để chuyển tiếp.")

    target_message_type = "FILE" if source_attachments else "TEXT"

    new_message = create_message(
        db,
        group_id=clean_target_group_id,
        sender_user_id=str(current_user.id),
        content=forwarded_content,
        message_type=target_message_type,
        reply_to_message_id=None,
    )

    copied_attachments = _copy_attachments_to_target_group(
        db,
        source_message=message,
        target_message=new_message,
        target_group_id=clean_target_group_id,
    )

    sender_name = _get_sender_name(current_user)

    payload_message = _build_message_payload(
        new_message,
        sender_name=sender_name,
        reply_preview=None,
        attachments=copied_attachments,
    )

    await manager.broadcast_group_text(
        clean_target_group_id,
        json.dumps({"type": "new_message", "message": payload_message}, ensure_ascii=False),
    )

    await _notify_group_unread_to_members(
        db,
        group_id=clean_target_group_id,
        sender_user_id=str(current_user.id),
    )

    return JSONResponse(
        {
            "ok": True,
            "message": {**payload_message, "is_mine": False},
            "target_group_id": clean_target_group_id,
        }
    )