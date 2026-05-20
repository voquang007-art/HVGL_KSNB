# -*- coding: utf-8 -*-
"""
app/chat/service.py

Service layer tối thiểu cho giai đoạn 1.
Mục tiêu:
- Có lớp truy vấn riêng cho module chat.
- Chưa triển khai sâu WebSocket/notify/quyền đơn vị.
"""

from __future__ import annotations

from typing import List, Optional
from datetime import datetime, timedelta
import os
import re

from sqlalchemy import String, cast, func
from sqlalchemy.orm import Session

from app.chat.models import ChatGroups, ChatGroupMembers, ChatMessages, ChatAttachments
from app.models import Users, UserStatus


def get_user_groups(db: Session, user_id: str) -> List[ChatGroups]:
    if not user_id:
        return []

    return (
        db.query(ChatGroups)
        .join(ChatGroupMembers, ChatGroupMembers.group_id == ChatGroups.id)
        .filter(ChatGroupMembers.user_id == user_id)
        .filter(ChatGroups.is_active.is_(True))
        .filter(ChatGroups.group_type != "MEETING")
        .order_by(ChatGroups.created_at.desc())
        .all()
    )


def enrich_groups_for_list(
    db: Session,
    groups: List[ChatGroups],
    current_user_id: str | None = None,
) -> List[ChatGroups]:
    if not groups:
        return groups

    group_ids = [g.id for g in groups if getattr(g, "id", None)]
    if not group_ids:
        return groups

    member_counts = {
        gid: cnt
        for gid, cnt in (
            db.query(ChatGroupMembers.group_id, func.count(ChatGroupMembers.id))
            .filter(ChatGroupMembers.group_id.in_(group_ids))
            .group_by(ChatGroupMembers.group_id)
            .all()
        )
    }

    for g in groups:
        g.member_count = member_counts.get(g.id, 0)
        g.new_message_count = 0
        g.message_count = 0
        g.has_new_group_badge = False

        if current_user_id:
            unread_count = get_group_new_message_count(db, g.id, current_user_id)
            g.new_message_count = unread_count
            g.message_count = unread_count

            member_row = get_group_member_row(db, g.id, current_user_id)
            g.has_new_group_badge = bool(getattr(member_row, "is_new_group", False)) if member_row else False

    return groups

def get_group_by_id(db: Session, group_id: str) -> Optional[ChatGroups]:
    if not group_id:
        return None
    return db.get(ChatGroups, group_id)


def is_group_member(db: Session, group_id: str, user_id: str) -> bool:
    if not group_id or not user_id:
        return False

    row = (
        db.query(ChatGroupMembers.id)
        .filter(ChatGroupMembers.group_id == group_id)
        .filter(ChatGroupMembers.user_id == user_id)
        .first()
    )
    return row is not None


def get_group_member_row(
    db: Session,
    group_id: str,
    user_id: str,
) -> Optional[ChatGroupMembers]:
    if not group_id or not user_id:
        return None

    return (
        db.query(ChatGroupMembers)
        .filter(ChatGroupMembers.group_id == group_id)
        .filter(ChatGroupMembers.user_id == user_id)
        .first()
    )


def mark_group_as_read(
    db: Session,
    group_id: str,
    user_id: str,
) -> bool:
    row = get_group_member_row(db, group_id, user_id)
    if not row:
        return False

    now = datetime.utcnow()
    row.last_read_at = now
    if getattr(row, "is_new_group", False):
        row.is_new_group = False
        row.new_group_marked_at = now
    db.commit()
    return True
    
    
def get_group_messages(db: Session, group_id: str, limit: int = 100) -> List[ChatMessages]:
    if not group_id:
        return []

    rows = (
        db.query(ChatMessages)
        .filter(ChatMessages.group_id == group_id)
        .order_by(ChatMessages.created_at.asc())
        .limit(limit)
        .all()
    )
    return rows


def get_group_new_message_count(
    db: Session,
    group_id: str,
    user_id: str,
) -> int:
    if not group_id or not user_id:
        return 0

    membership = get_group_member_row(db, group_id, user_id)
    if not membership:
        return 0

    query = (
        db.query(func.count(ChatMessages.id))
        .filter(ChatMessages.group_id == group_id)
        .filter(ChatMessages.sender_user_id != user_id)
        .filter(ChatMessages.deleted_by_owner.is_(False))
    )

    if membership.last_read_at is not None:
        query = query.filter(ChatMessages.created_at > membership.last_read_at)

    return int(query.scalar() or 0)


def get_group_member_user_ids(
    db: Session,
    group_id: str,
    exclude_user_id: str | None = None,
) -> list[str]:
    if not group_id:
        return []

    rows = (
        db.query(ChatGroupMembers.user_id)
        .filter(ChatGroupMembers.group_id == group_id)
        .all()
    )

    user_ids: list[str] = []
    for row in rows:
        uid = str(row[0] or "").strip()
        if not uid:
            continue
        if exclude_user_id and uid == exclude_user_id:
            continue
        user_ids.append(uid)

    return user_ids

def normalize_group_name(value: str) -> str:
    cleaned = re.sub(r"\s+", " ", (value or "").strip())
    return cleaned.casefold()


def get_existing_group_by_normalized_name(db: Session, normalized_name: str) -> Optional[ChatGroups]:
    if not normalized_name:
        return None

    groups = db.query(ChatGroups).filter(ChatGroups.is_active.is_(True)).all()
    for group in groups:
        if normalize_group_name(getattr(group, "name", "")) == normalized_name:
            return group
    return None

def create_group(
    db: Session,
    *,
    name: str,
    owner_user_id: str,
    group_type: str = "PRIVATE",
    unit_id: str | None = None,
    task_id: str | None = None,
) -> ChatGroups:
    clean_name = re.sub(r"\s+", " ", (name or "").strip())

    group = ChatGroups(
        name=clean_name,
        owner_user_id=owner_user_id,
        group_type=(group_type or "PRIVATE").strip().upper(),
        unit_id=unit_id,
        task_id=task_id,
        is_active=True,
    )
    db.add(group)
    db.flush()

    owner_member = ChatGroupMembers(
        group_id=group.id,
        user_id=owner_user_id,
        member_role="owner",
        is_new_group=False,
        new_group_marked_at=datetime.utcnow(),
    )
    db.add(owner_member)
    db.commit()
    db.refresh(group)
    return group


def add_member_to_group(
    db: Session,
    *,
    group_id: str,
    user_id: str,
    member_role: str = "member",
    mark_as_new: bool = True,
) -> ChatGroupMembers:
    existing = (
        db.query(ChatGroupMembers)
        .filter(ChatGroupMembers.group_id == group_id)
        .filter(ChatGroupMembers.user_id == user_id)
        .first()
    )
    if existing:
        return existing

    row = ChatGroupMembers(
        group_id=group_id,
        user_id=user_id,
        member_role=(member_role or "member").strip().lower(),
        is_new_group=bool(mark_as_new),
        new_group_marked_at=None if mark_as_new else datetime.utcnow(),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def create_message(
    db: Session,
    *,
    group_id: str,
    sender_user_id: str,
    content: str,
    message_type: str = "TEXT",
    reply_to_message_id: str | None = None,
) -> ChatMessages:
    message = ChatMessages(
        group_id=group_id,
        sender_user_id=sender_user_id,
        content=(content or "").strip(),
        message_type=(message_type or "TEXT").strip().upper(),
        reply_to_message_id=(reply_to_message_id or None),
    )
    db.add(message)
    db.commit()
    db.refresh(message)
    return message
    
def get_message_by_id(db: Session, message_id: str) -> Optional[ChatMessages]:
    if not message_id:
        return None
    return db.get(ChatMessages, message_id)


def get_message_attachments(db: Session, message_id: str) -> list[ChatAttachments]:
    if not message_id:
        return []

    return (
        db.query(ChatAttachments)
        .filter(ChatAttachments.message_id == message_id)
        .order_by(ChatAttachments.uploaded_at.asc())
        .all()
    )


def _reset_message_pin_state(message: ChatMessages | None) -> None:
    if not message:
        return
    message.is_pinned = False
    message.pinned_at = None
    message.pinned_by_user_id = None
    
    
def get_active_message_attachments(db: Session, message_id: str) -> list[ChatAttachments]:
    if not message_id:
        return []

    return (
        db.query(ChatAttachments)
        .filter(ChatAttachments.message_id == message_id)
        .filter(ChatAttachments.deleted_by_owner.is_(False))
        .order_by(ChatAttachments.uploaded_at.asc())
        .all()
    )


def get_attachment_by_id(db: Session, attachment_id: str) -> Optional[ChatAttachments]:
    if not attachment_id:
        return None
    return db.get(ChatAttachments, attachment_id)


def _reset_attachment_pin_state(att: ChatAttachments | None) -> None:
    if not att:
        return
    att.is_pinned = False
    att.pinned_at = None
    att.pinned_by_user_id = None


def get_group_pinned_items(db: Session, group_id: str) -> list[dict]:
    if not group_id:
        return []

    message_rows = (
        db.query(ChatMessages)
        .filter(ChatMessages.group_id == group_id)
        .filter(ChatMessages.is_pinned.is_(True))
        .filter(ChatMessages.deleted_by_owner.is_(False))
        .order_by(ChatMessages.pinned_at.desc(), ChatMessages.created_at.desc())
        .all()
    )

    attachment_rows = (
        db.query(ChatAttachments)
        .join(ChatMessages, ChatMessages.id == ChatAttachments.message_id)
        .filter(ChatMessages.group_id == group_id)
        .filter(ChatAttachments.is_pinned.is_(True))
        .filter(ChatAttachments.deleted_by_owner.is_(False))
        .order_by(ChatAttachments.pinned_at.desc(), ChatAttachments.uploaded_at.desc())
        .all()
    )

    items: list[dict] = []

    for msg in message_rows:
        pinned_by_name = (
            getattr(getattr(msg, "pinned_by", None), "full_name", None)
            or getattr(getattr(msg, "pinned_by", None), "username", None)
            or "Người dùng"
        )
        sender_name = (
            getattr(getattr(msg, "sender", None), "full_name", None)
            or getattr(getattr(msg, "sender", None), "username", None)
            or "Người dùng"
        )
        items.append({
            "pin_kind": "message",
            "id": msg.id,
            "message_id": msg.id,
            "attachment_id": None,
            "label": "Tin nhắn",
            "title": (msg.content or "Tin nhắn").strip() or "Tin nhắn",
            "filename": None,
            "sender_name": sender_name,
            "pinned_by_name": pinned_by_name,
            "pinned_at": msg.pinned_at,
            "pinned_at_text": (msg.pinned_at + timedelta(hours=7)).strftime("%d/%m/%Y %H:%M") if msg.pinned_at else "",
        })

    for att in attachment_rows:
        message = getattr(att, "message", None)
        if not message:
            continue
        pinned_by_name = (
            getattr(getattr(att, "pinned_by", None), "full_name", None)
            or getattr(getattr(att, "pinned_by", None), "username", None)
            or "Người dùng"
        )
        sender_name = (
            getattr(getattr(message, "sender", None), "full_name", None)
            or getattr(getattr(message, "sender", None), "username", None)
            or "Người dùng"
        )
        items.append({
            "pin_kind": "attachment",
            "id": att.id,
            "message_id": message.id,
            "attachment_id": att.id,
            "label": "File",
            "title": att.filename or "Tệp đính kèm",
            "filename": att.filename or "Tệp đính kèm",
            "sender_name": sender_name,
            "pinned_by_name": pinned_by_name,
            "pinned_at": att.pinned_at,
            "pinned_at_text": (att.pinned_at + timedelta(hours=7)).strftime("%d/%m/%Y %H:%M") if att.pinned_at else "",
        })

    items.sort(key=lambda x: x.get("pinned_at") or datetime.min, reverse=True)
    return items


def toggle_message_pin(
    db: Session,
    *,
    message_id: str,
    user_id: str,
) -> ChatMessages | None:
    message = get_message_by_id(db, message_id)
    if not message or not is_group_member(db, message.group_id, user_id):
        return None

    if message.deleted_by_owner or message.recalled:
        return None

    if bool(getattr(message, "is_pinned", False)):
        _reset_message_pin_state(message)
    else:
        message.is_pinned = True
        message.pinned_at = datetime.utcnow()
        message.pinned_by_user_id = user_id

    message.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(message)
    return message


def toggle_attachment_pin(
    db: Session,
    *,
    attachment_id: str,
    user_id: str,
) -> ChatAttachments | None:
    att = get_attachment_by_id(db, attachment_id)
    if not att:
        return None

    message = getattr(att, "message", None)
    if not message:
        message = get_message_by_id(db, att.message_id)
    if not message or not is_group_member(db, message.group_id, user_id):
        return None

    if att.deleted_by_owner or att.recalled:
        return None

    if bool(getattr(att, "is_pinned", False)):
        _reset_attachment_pin_state(att)
    else:
        att.is_pinned = True
        att.pinned_at = datetime.utcnow()
        att.pinned_by_user_id = user_id

    att.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(att)
    return att
    
    
def can_manage_attachment(
    db: Session,
    attachment: ChatAttachments | None,
    user_id: str,
) -> bool:
    if not attachment or not user_id:
        return False

    message = getattr(attachment, "message", None)
    if not message:
        message = get_message_by_id(db, attachment.message_id)

    return bool(message and str(message.sender_user_id or "") == str(user_id or ""))


def _remove_attachment_file(att: ChatAttachments | None) -> None:
    if not att:
        return

    try:
        rel_path = (att.path or "").lstrip("/").replace("/", os.sep)
        if not rel_path:
            return

        abs_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", rel_path))
        if os.path.isfile(abs_path):
            os.remove(abs_path)
    except Exception:
        pass
        
        
def can_manage_message(message: ChatMessages | None, user_id: str) -> bool:
    if not message or not user_id:
        return False
    return str(message.sender_user_id or "") == str(user_id or "")


def recall_message(
    db: Session,
    *,
    message_id: str,
    user_id: str,
) -> ChatMessages | None:
    message = get_message_by_id(db, message_id)
    if not message or not can_manage_message(message, user_id):
        return None

    message.recalled = True
    message.updated_at = datetime.utcnow()
    _reset_message_pin_state(message)
    for att in get_message_attachments(db, message.id):
        _reset_attachment_pin_state(att)

    if (message.message_type or "").upper() == "FILE":
        message.content = "Tệp đính kèm đã được thu hồi."
    else:
        message.content = ""

    db.commit()
    db.refresh(message)
    return message


def delete_message(
    db: Session,
    *,
    message_id: str,
    user_id: str,
) -> bool:
    message = get_message_by_id(db, message_id)
    if not message or not can_manage_message(message, user_id):
        return False

    attachments = get_message_attachments(db, message.id)
    _reset_message_pin_state(message)
    for att in attachments:
        _reset_attachment_pin_state(att)
        if not att.deleted_by_owner and not att.recalled:
            _remove_attachment_file(att)

    db.delete(message)
    db.commit()
    return True
    

def recall_attachment(
    db: Session,
    *,
    attachment_id: str,
    user_id: str,
) -> tuple[ChatAttachments, ChatMessages] | None:
    att = get_attachment_by_id(db, attachment_id)
    if not att:
        return None

    message = get_message_by_id(db, att.message_id)
    if not message or not can_manage_attachment(db, att, user_id):
        return None

    if not att.recalled and not att.deleted_by_owner:
        _remove_attachment_file(att)

    _reset_attachment_pin_state(att)
    att.recalled = True
    att.updated_at = datetime.utcnow()
    att.path = None
    att.stored_name = None
    att.mime_type = None
    att.size_bytes = 0

    if not (att.filename or "").startswith("[Đã thu hồi] "):
        att.filename = "[Đã thu hồi] " + (att.filename or "Tệp đính kèm")

    active_rows = get_active_message_attachments(db, message.id)
    active_non_recalled = [row for row in active_rows if not row.recalled]

    if not active_non_recalled and (message.message_type or "").upper() == "FILE":
        message.content = "Tệp đính kèm đã được thu hồi."
        message.updated_at = datetime.utcnow()

    db.commit()
    db.refresh(att)
    db.refresh(message)
    return att, message


def delete_attachment(
    db: Session,
    *,
    attachment_id: str,
    user_id: str,
) -> tuple[ChatAttachments, ChatMessages] | None:
    att = get_attachment_by_id(db, attachment_id)
    if not att:
        return None

    message = get_message_by_id(db, att.message_id)
    if not message or not can_manage_attachment(db, att, user_id):
        return None

    if not att.deleted_by_owner:
        _remove_attachment_file(att)

    _reset_attachment_pin_state(att)
    att.deleted_by_owner = True
    att.updated_at = datetime.utcnow()
    att.path = None
    att.stored_name = None
    att.mime_type = None
    att.size_bytes = 0

    if not (att.filename or "").startswith("[Đã xóa] "):
        att.filename = "[Đã xóa] " + (att.filename or "Tệp đính kèm")

    active_rows = get_active_message_attachments(db, message.id)
    active_non_recalled = [row for row in active_rows if not row.recalled and not row.deleted_by_owner]

    if not active_non_recalled and (message.message_type or "").upper() == "FILE":
        message.content = "Tệp đính kèm đã bị xóa."
        message.updated_at = datetime.utcnow()

    db.commit()
    db.refresh(att)
    db.refresh(message)
    return att, message

    
def save_message_attachment(
    db: Session,
    *,
    message_id: str,
    filename: str,
    stored_name: str,
    path: str,
    mime_type: str | None,
    size_bytes: int,
):
    from app.chat.models import ChatAttachments

    row = ChatAttachments(
        message_id=message_id,
        filename=filename,
        stored_name=stored_name,
        path=path,
        mime_type=mime_type,
        size_bytes=size_bytes or 0,
        recalled=False,
        deleted_by_owner=False,
        updated_at=None,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def list_message_reactions(db: Session, message_ids: list[str]) -> dict[str, dict[str, int]]:
    from app.chat.models import ChatMessageLikes

    result: dict[str, dict[str, int]] = {}
    if not message_ids:
        return result

    rows = (
        db.query(
            ChatMessageLikes.message_id,
            ChatMessageLikes.reaction_type,
            func.count(ChatMessageLikes.id),
        )
        .filter(ChatMessageLikes.message_id.in_(message_ids))
        .group_by(ChatMessageLikes.message_id, ChatMessageLikes.reaction_type)
        .all()
    )

    for message_id, reaction_type, count in rows:
        bucket = result.setdefault(message_id, {})
        bucket[reaction_type or "like"] = int(count or 0)

    return result


def toggle_message_reaction(
    db: Session,
    *,
    message_id: str,
    user_id: str,
    reaction_type: str,
) -> dict[str, int]:
    from app.chat.models import ChatMessageLikes

    clean_reaction = (reaction_type or "like").strip().lower()
    if clean_reaction not in {"like", "heart", "laugh"}:
        clean_reaction = "like"

    row = (
        db.query(ChatMessageLikes)
        .filter(ChatMessageLikes.message_id == message_id)
        .filter(ChatMessageLikes.user_id == user_id)
        .first()
    )

    if row and (row.reaction_type or "like") == clean_reaction:
        db.delete(row)
        db.commit()
    elif row:
        row.reaction_type = clean_reaction
        db.commit()
    else:
        row = ChatMessageLikes(
            message_id=message_id,
            user_id=user_id,
            reaction_type=clean_reaction,
        )
        db.add(row)
        db.commit()

    counts = (
        db.query(
            ChatMessageLikes.reaction_type,
            func.count(ChatMessageLikes.id),
        )
        .filter(ChatMessageLikes.message_id == message_id)
        .group_by(ChatMessageLikes.reaction_type)
        .all()
    )

    result = {"like": 0, "heart": 0, "laugh": 0}
    for rt, cnt in counts:
        result[(rt or "like")] = int(cnt or 0)
    return result    

def get_group_members(db: Session, group_id: str):
    if not group_id:
        return []

    return (
        db.query(ChatGroupMembers)
        .join(Users, cast(Users.id, String) == ChatGroupMembers.user_id)
        .filter(ChatGroupMembers.group_id == group_id)
        .order_by(Users.full_name.asc(), Users.username.asc())
        .all()
    )


def get_available_users_for_group(db: Session, group_id: str):
    if not group_id:
        return []

    return (
        db.query(Users)
        .outerjoin(
            ChatGroupMembers,
            (ChatGroupMembers.user_id == cast(Users.id, String)) & (ChatGroupMembers.group_id == group_id),
        )
        .filter(Users.is_active == 1)
        .filter(ChatGroupMembers.id.is_(None))
        .order_by(Users.full_name.asc(), Users.username.asc())
        .all()
    )
    
def remove_member_from_group(
    db: Session,
    *,
    group_id: str,
    user_id: str,
) -> bool:
    row = (
        db.query(ChatGroupMembers)
        .filter(ChatGroupMembers.group_id == group_id)
        .filter(ChatGroupMembers.user_id == user_id)
        .first()
    )
    if not row:
        return False

    db.delete(row)
    db.commit()
    return True


def transfer_group_owner(
    db: Session,
    *,
    group_id: str,
    new_owner_user_id: str,
) -> Optional[ChatGroups]:
    group = db.get(ChatGroups, group_id)
    if not group:
        return None

    old_owner_member = (
        db.query(ChatGroupMembers)
        .filter(ChatGroupMembers.group_id == group_id)
        .filter(ChatGroupMembers.user_id == group.owner_user_id)
        .first()
    )

    new_owner_member = (
        db.query(ChatGroupMembers)
        .filter(ChatGroupMembers.group_id == group_id)
        .filter(ChatGroupMembers.user_id == new_owner_user_id)
        .first()
    )

    if not new_owner_member:
        return None

    if old_owner_member:
        old_owner_member.member_role = "member"

    new_owner_member.member_role = "owner"
    group.owner_user_id = new_owner_user_id

    db.commit()
    db.refresh(group)
    return group


def disband_group(
    db: Session,
    *,
    group_id: str,
) -> Optional[ChatGroups]:
    group = db.get(ChatGroups, group_id)
    if not group:
        return None

    group.is_active = False
    db.commit()
    db.refresh(group)
    return group

# =========================
# MEETING SERVICE HELPERS
# =========================

def get_user_meeting_groups(db: Session, user_id: str):
    if not user_id:
        return []

    return (
        db.query(ChatGroups)
        .join(ChatGroupMembers, ChatGroupMembers.group_id == ChatGroups.id)
        .filter(ChatGroupMembers.user_id == user_id)
        .filter(ChatGroups.group_type == "MEETING")
        .filter(ChatGroups.is_active.is_(True))
        .order_by(ChatGroups.created_at.desc())
        .all()
    )


def get_meeting_by_group_id(db: Session, group_id: str):
    if not group_id:
        return None

    from app.chat.models import ChatMeetings

    return (
        db.query(ChatMeetings)
        .filter(ChatMeetings.group_id == group_id)
        .first()
    )


def create_meeting_session(
    db: Session,
    *,
    group_id: str,
    designed_by_user_id: str,
    host_user_id: str | None,
    secretary_user_id: str | None,
    meeting_scope: str,
    scheduled_start_at,
    scheduled_end_at,
    agenda: str | None = None,
    committee_id: str | None = None,
):
    from app.chat.models import ChatMeetings

    row = ChatMeetings(
        group_id=group_id,
        designed_by_user_id=designed_by_user_id,
        host_user_id=host_user_id,
        secretary_user_id=secretary_user_id,
        meeting_scope=(meeting_scope or "TEAM").strip().upper(),
        committee_id=(committee_id or None),
        meeting_status="UPCOMING",
        scheduled_start_at=scheduled_start_at,
        scheduled_end_at=scheduled_end_at,
        agenda=(agenda or "").strip() or None,
        updated_at=None,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def ensure_meeting_attendance_rows(
    db: Session,
    meeting_id: str,
    user_ids: list[str],
):
    from app.chat.models import ChatMeetingAttendances

    clean_user_ids: list[str] = []
    for user_id in user_ids or []:
        uid = str(user_id or "").strip()
        if uid and uid not in clean_user_ids:
            clean_user_ids.append(uid)

    for uid in clean_user_ids:
        existing = (
            db.query(ChatMeetingAttendances)
            .filter(ChatMeetingAttendances.meeting_id == meeting_id)
            .filter(ChatMeetingAttendances.user_id == uid)
            .first()
        )
        if existing:
            continue

        row = ChatMeetingAttendances(
            meeting_id=meeting_id,
            user_id=uid,
            attendance_status="PENDING",
            presence_status="OFFLINE",
        )
        db.add(row)

    db.commit()

    return (
        db.query(ChatMeetingAttendances)
        .filter(ChatMeetingAttendances.meeting_id == meeting_id)
        .all()
    )


def get_meeting_attendance_rows(db: Session, meeting_id: str):
    from app.chat.models import ChatMeetingAttendances

    if not meeting_id:
        return []

    return (
        db.query(ChatMeetingAttendances)
        .join(Users, Users.id == ChatMeetingAttendances.user_id)
        .filter(ChatMeetingAttendances.meeting_id == meeting_id)
        .order_by(Users.full_name.asc(), Users.username.asc())
        .all()
    )


def set_meeting_presence(
    db: Session,
    meeting_id: str,
    user_id: str,
    is_online: bool,
):
    from app.chat.models import ChatMeetingAttendances

    row = (
        db.query(ChatMeetingAttendances)
        .filter(ChatMeetingAttendances.meeting_id == meeting_id)
        .filter(ChatMeetingAttendances.user_id == user_id)
        .first()
    )
    if not row:
        row = ChatMeetingAttendances(
            meeting_id=meeting_id,
            user_id=user_id,
            attendance_status="PENDING",
            presence_status="ONLINE" if is_online else "OFFLINE",
        )
        db.add(row)
    else:
        row.presence_status = "ONLINE" if is_online else "OFFLINE"
        row.updated_at = datetime.utcnow()

    db.commit()
    db.refresh(row)
    return row


def mark_meeting_absent(
    db: Session,
    meeting_id: str,
    user_id: str,
    reason: str | None = None,
):
    from app.chat.models import ChatMeetingAttendances

    row = (
        db.query(ChatMeetingAttendances)
        .filter(ChatMeetingAttendances.meeting_id == meeting_id)
        .filter(ChatMeetingAttendances.user_id == user_id)
        .first()
    )
    if not row:
        row = ChatMeetingAttendances(
            meeting_id=meeting_id,
            user_id=user_id,
        )
        db.add(row)

    row.attendance_status = "ABSENT"
    row.absent_reason = (reason or "").strip() or None
    row.presence_status = "OFFLINE"
    row.updated_at = datetime.utcnow()

    db.commit()
    db.refresh(row)
    return row


def mark_meeting_checkin(
    db: Session,
    meeting_id: str,
    user_id: str,
):
    from app.chat.models import ChatMeetingAttendances

    row = (
        db.query(ChatMeetingAttendances)
        .filter(ChatMeetingAttendances.meeting_id == meeting_id)
        .filter(ChatMeetingAttendances.user_id == user_id)
        .first()
    )
    if not row:
        row = ChatMeetingAttendances(
            meeting_id=meeting_id,
            user_id=user_id,
        )
        db.add(row)

    row.attendance_status = "CHECKED_IN"
    row.presence_status = "ONLINE"
    row.checked_in_at = datetime.utcnow()
    row.updated_at = datetime.utcnow()

    db.commit()
    db.refresh(row)
    return row


def list_speaker_requests(db: Session, meeting_id: str):
    from app.chat.models import ChatMeetingSpeakerRequests

    if not meeting_id:
        return []

    return (
        db.query(ChatMeetingSpeakerRequests)
        .join(Users, Users.id == ChatMeetingSpeakerRequests.user_id)
        .filter(ChatMeetingSpeakerRequests.meeting_id == meeting_id)
        .order_by(ChatMeetingSpeakerRequests.queue_no.asc(), ChatMeetingSpeakerRequests.created_at.asc())
        .all()
    )


def create_speaker_request(
    db: Session,
    meeting_id: str,
    user_id: str,
    note: str | None = None,
):
    from app.chat.models import ChatMeetingSpeakerRequests

    existing_pending = (
        db.query(ChatMeetingSpeakerRequests)
        .filter(ChatMeetingSpeakerRequests.meeting_id == meeting_id)
        .filter(ChatMeetingSpeakerRequests.user_id == user_id)
        .filter(ChatMeetingSpeakerRequests.request_status.in_(["PENDING", "APPROVED", "SPEAKING"]))
        .first()
    )
    if existing_pending:
        return existing_pending

    max_queue = (
        db.query(func.max(ChatMeetingSpeakerRequests.queue_no))
        .filter(ChatMeetingSpeakerRequests.meeting_id == meeting_id)
        .scalar()
    ) or 0

    row = ChatMeetingSpeakerRequests(
        meeting_id=meeting_id,
        user_id=user_id,
        queue_no=int(max_queue) + 1,
        request_status="PENDING",
        note=(note or "").strip() or None,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def approve_speaker_request(db: Session, speaker_request_id: str):
    from app.chat.models import ChatMeetingSpeakerRequests

    row = db.get(ChatMeetingSpeakerRequests, speaker_request_id)
    if not row:
        return None

    # Khóa các lượt APPROVED/SPEAKING khác
    (
        db.query(ChatMeetingSpeakerRequests)
        .filter(ChatMeetingSpeakerRequests.meeting_id == row.meeting_id)
        .filter(ChatMeetingSpeakerRequests.id != row.id)
        .filter(ChatMeetingSpeakerRequests.request_status.in_(["APPROVED", "SPEAKING"]))
        .update(
            {
                "request_status": "PENDING",
                "updated_at": datetime.utcnow(),
            },
            synchronize_session=False,
        )
    )

    row.request_status = "APPROVED"
    row.approved_at = datetime.utcnow()
    row.updated_at = datetime.utcnow()

    db.commit()
    db.refresh(row)
    return row


def move_speaker_request(
    db: Session,
    speaker_request_id: str,
    direction: str,
):
    from app.chat.models import ChatMeetingSpeakerRequests

    row = db.get(ChatMeetingSpeakerRequests, speaker_request_id)
    if not row:
        return None

    rows = (
        db.query(ChatMeetingSpeakerRequests)
        .filter(ChatMeetingSpeakerRequests.meeting_id == row.meeting_id)
        .order_by(ChatMeetingSpeakerRequests.queue_no.asc(), ChatMeetingSpeakerRequests.created_at.asc())
        .all()
    )

    ids = [r.id for r in rows]
    if row.id not in ids:
        return row

    idx = ids.index(row.id)
    if direction == "up" and idx > 0:
        rows[idx].queue_no, rows[idx - 1].queue_no = rows[idx - 1].queue_no, rows[idx].queue_no
    elif direction == "down" and idx < len(rows) - 1:
        rows[idx].queue_no, rows[idx + 1].queue_no = rows[idx + 1].queue_no, rows[idx].queue_no
    else:
        return row

    now = datetime.utcnow()
    for r in rows:
        r.updated_at = now
        db.add(r)

    db.commit()
    db.refresh(row)
    return row


def assign_meeting_secretary(
    db: Session,
    meeting_id: str,
    secretary_user_id: str | None,
):
    from app.chat.models import ChatMeetings

    row = db.get(ChatMeetings, meeting_id)
    if not row:
        return None

    row.secretary_user_id = (secretary_user_id or None)
    row.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(row)
    return row


def assign_meeting_host(
    db: Session,
    meeting_id: str,
    host_user_id: str | None,
):
    from app.chat.models import ChatMeetings

    row = db.get(ChatMeetings, meeting_id)
    if not row:
        return None

    row.host_user_id = (host_user_id or None)
    row.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(row)
    return row


def transition_meeting_status_if_needed(db: Session, meeting):
    from app.chat.models import ChatMeetings

    if not meeting:
        return None

    now = datetime.utcnow()
    changed = False

    start_at = getattr(meeting, "scheduled_start_at", None)
    end_at = getattr(meeting, "scheduled_end_at", None)
    status = (getattr(meeting, "meeting_status", "") or "UPCOMING").upper()

    if status == "UPCOMING" and start_at and now >= start_at:
        meeting.meeting_status = "LIVE"
        changed = True

    if meeting.meeting_status == "LIVE" and end_at and now >= end_at:
        meeting.meeting_status = "ENDED"
        changed = True

    if changed:
        meeting.updated_at = now
        db.add(meeting)
        db.commit()
        db.refresh(meeting)

    refreshed = db.get(ChatMeetings, meeting.id)
    return refreshed or meeting


def auto_assign_meeting_host(db: Session, meeting_id: str):
    from app.chat.models import ChatMeetings, ChatMeetingAttendances

    meeting = db.get(ChatMeetings, meeting_id)
    if not meeting:
        return None

    host_user_id = getattr(meeting, "host_user_id", None)
    if not host_user_id:
        if getattr(meeting, "secretary_user_id", None):
            meeting.host_user_id = meeting.secretary_user_id
        elif getattr(meeting, "designed_by_user_id", None):
            meeting.host_user_id = meeting.designed_by_user_id

        meeting.updated_at = datetime.utcnow()
        db.add(meeting)
        db.commit()
        db.refresh(meeting)
        return meeting

    host_row = (
        db.query(ChatMeetingAttendances)
        .filter(ChatMeetingAttendances.meeting_id == meeting.id)
        .filter(ChatMeetingAttendances.user_id == host_user_id)
        .first()
    )
    host_status = (getattr(host_row, "attendance_status", "") or "PENDING").upper() if host_row else "PENDING"

    if host_status in {"ABSENT", "LEFT"}:
        if getattr(meeting, "secretary_user_id", None):
            secretary_row = (
                db.query(ChatMeetingAttendances)
                .filter(ChatMeetingAttendances.meeting_id == meeting.id)
                .filter(ChatMeetingAttendances.user_id == meeting.secretary_user_id)
                .first()
            )
            secretary_status = (getattr(secretary_row, "attendance_status", "") or "PENDING").upper() if secretary_row else "PENDING"
            if secretary_status not in {"ABSENT", "LEFT"}:
                meeting.host_user_id = meeting.secretary_user_id
            else:
                meeting.host_user_id = meeting.designed_by_user_id
        else:
            meeting.host_user_id = meeting.designed_by_user_id

        meeting.updated_at = datetime.utcnow()
        db.add(meeting)
        db.commit()
        db.refresh(meeting)

    return meeting


def remove_absent_members_from_live_meeting(db: Session, meeting_id: str):
    from app.chat.models import ChatMeetings, ChatMeetingAttendances

    meeting = db.get(ChatMeetings, meeting_id)
    if not meeting:
        return []

    if (meeting.meeting_status or "").upper() != "LIVE":
        return []

    absent_rows = (
        db.query(ChatMeetingAttendances)
        .filter(ChatMeetingAttendances.meeting_id == meeting_id)
        .filter(ChatMeetingAttendances.attendance_status == "ABSENT")
        .all()
    )

    removed_user_ids: list[str] = []
    for row in absent_rows:
        uid = str(getattr(row, "user_id", "") or "").strip()
        if not uid:
            continue
        if remove_member_from_group(db, group_id=meeting.group_id, user_id=uid):
            removed_user_ids.append(uid)

    return removed_user_ids    