# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import mimetypes
import os
import shutil
import subprocess
import tempfile
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from sqlalchemy.orm import Session
from starlette.templating import Jinja2Templates

from app.chat.deps import get_display_name
from app.chat.models import (
    ChatAttachments,
    ChatGroupMembers,
    ChatGroups,
    ChatMeetingAttendances,
    ChatMeetingLeaveRequests,
    ChatMeetings,
    ChatMeetingSpeakerRequests,
    ChatMessages,
)
from app.chat.realtime import manager
from app.chat.service import (
    add_member_to_group,
    approve_speaker_request,
    assign_meeting_host,
    assign_meeting_secretary,
    create_group,
    create_meeting_session,
    create_message,
    create_speaker_request,
    ensure_meeting_attendance_rows,
    get_group_by_id,
    get_group_member_user_ids,
    get_group_messages,
    get_meeting_attendance_rows,
    get_meeting_by_group_id,
    get_message_attachments,
    get_user_meeting_groups,
    is_group_member,
    list_speaker_requests,
    mark_meeting_absent,
    mark_meeting_checkin,
    move_speaker_request,
    remove_absent_members_from_live_meeting,
    save_message_attachment,
    set_meeting_presence,
    transition_meeting_status_if_needed,
)
from app.config import settings
from app.database import (
    BOARD_ROLES,
    HEAD_ROLES,
    KSNB_ROLES,
    ROLE_ADMIN,
    ROLE_LABELS,
    ROLE_THANH_VIEN_TRUNG_TAP,
    Base,
    engine,
    get_db,
)
from app.models import Users
from app.office_preview import OfficePreviewError, ensure_office_pdf_preview, is_office_previewable
from app.security.deps import login_required
from app.upload_security import (
    GENERAL_SAFE_UPLOAD_EXTENSIONS,
    MEETING_MAX_UPLOAD_MB,
    UploadValidationError,
    save_upload_file_chunked,
)

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

MEETING_SCOPE_KSNB = "KSNB"
MEETING_SCOPE_HDTV = "HDTV"
MEETING_SCOPE_KSNB_TRUNG_TAP = "KSNB_TRUNG_TAP"
MEETING_SCOPE_HDTV_KSNB_TRUNG_TAP = "HDTV_KSNB_TRUNG_TAP"
MEETING_SCOPE_ALL = "ALL"

_ALLOWED_EXTENSIONS = {
    ".pdf",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".ppt",
    ".pptx",
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".txt",
}

_INLINE_MIME_PREFIXES = ("image/", "text/")
_INLINE_MIME_EXACT = {"application/pdf"}


def _format_vn_dt(value: object) -> str:
    if not value:
        return "—"
    if isinstance(value, datetime):
        return (value + timedelta(hours=7)).strftime("%d/%m/%Y %H:%M")
    return str(value)


templates.env.filters["format_vn_dt"] = _format_vn_dt


def _ensure_meeting_tables() -> None:
    Base.metadata.create_all(
        bind=engine,
        tables=[
            ChatGroups.__table__,
            ChatGroupMembers.__table__,
            ChatMessages.__table__,
            ChatAttachments.__table__,
            ChatMeetings.__table__,
            ChatMeetingAttendances.__table__,
            ChatMeetingSpeakerRequests.__table__,
            ChatMeetingLeaveRequests.__table__,
        ],
        checkfirst=True,
    )


def _now() -> datetime:
    return datetime.utcnow()


def _role_code(user: Optional[Users]) -> str:
    return str(getattr(user, "role_code", "") or "").strip().upper()


def _is_admin(user: Optional[Users]) -> bool:
    return _role_code(user) == ROLE_ADMIN


def _is_ksnb(user: Optional[Users]) -> bool:
    return _role_code(user) in KSNB_ROLES


def _is_ksnb_manager(user: Optional[Users]) -> bool:
    return _role_code(user) in HEAD_ROLES


def _is_board(user: Optional[Users]) -> bool:
    return _role_code(user) in BOARD_ROLES


def _can_access_meeting_module(user: Users) -> bool:
    return _is_admin(user) or _is_ksnb(user) or _is_board(user) or _role_code(user) == ROLE_THANH_VIEN_TRUNG_TAP


def _can_create_meeting(user: Users) -> bool:
    return _is_admin(user) or _is_ksnb_manager(user) or _is_board(user)


def _can_manage_meeting(meeting: ChatMeetings, user_id: object) -> bool:
    uid = str(user_id or "")
    return uid in {
        str(getattr(meeting, "designed_by_user_id", "") or ""),
        str(getattr(meeting, "host_user_id", "") or ""),
        str(getattr(meeting, "secretary_user_id", "") or ""),
    }


def _can_delete_meeting(meeting: ChatMeetings, user_id: object) -> bool:
    return _can_manage_meeting(meeting, user_id)


def _role_label(user: Users) -> str:
    return ROLE_LABELS.get(_role_code(user), getattr(user, "position_title", "") or _role_code(user))


def _meeting_scope_label(value: str) -> str:
    scope = (value or "").strip().upper()
    if scope == MEETING_SCOPE_KSNB:
        return "Họp nội bộ Ban KSNB"
    if scope == MEETING_SCOPE_HDTV:
        return "Họp Ban KSNB / HĐTV"
    if scope == MEETING_SCOPE_KSNB_TRUNG_TAP:
        return "Họp Ban KSNB và thành viên trưng tập"
    if scope == MEETING_SCOPE_HDTV_KSNB_TRUNG_TAP:
        return "Họp Hội đồng thành viên, Ban KSNB và thành viên trưng tập"
    if scope == MEETING_SCOPE_ALL:
        return "Họp toàn bộ người dùng"
    return scope or "Cuộc họp"


def _meeting_status_label(value: str) -> str:
    status_value = (value or "UPCOMING").strip().upper()
    if status_value == "LIVE":
        return "Đang họp"
    if status_value == "ENDED":
        return "Đã kết thúc"
    return "Sắp họp"


def _attendance_status_label(value: str) -> str:
    status_value = (value or "PENDING").strip().upper()
    if status_value == "CHECKED_IN":
        return "Đã điểm danh"
    if status_value == "ABSENT":
        return "Báo vắng"
    if status_value == "LEFT":
        return "Đã rời họp"
    return "Chờ điểm danh"


def _speaker_status_label(value: str) -> str:
    status_value = (value or "PENDING").strip().upper()
    if status_value == "APPROVED":
        return "Đã cho phép phát biểu"
    if status_value == "SPEAKING":
        return "Đang phát biểu"
    if status_value == "DONE":
        return "Đã phát biểu"
    return "Chờ duyệt"

def _leave_status_label(value: str) -> str:
    status_value = (value or "PENDING").strip().upper()
    if status_value == "APPROVED":
        return "Đã cho phép rời cuộc họp"
    if status_value == "REJECTED":
        return "Không đồng ý"
    return "Chờ Chủ trì cho phép"
    
    
def _parse_datetime_local(value: str) -> Optional[datetime]:
    raw = (value or "").strip()
    if not raw:
        return None

    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            local_dt = datetime.strptime(raw, fmt)
            return local_dt - timedelta(hours=7)
        except ValueError:
            continue

    raise HTTPException(status_code=400, detail="Định dạng thời gian không hợp lệ.")

def _datetime_local_value(value: object) -> str:
    if not isinstance(value, datetime):
        return ""
    return (value + timedelta(hours=7)).strftime("%Y-%m-%dT%H:%M")


def _normalize_dt_minute(value: Optional[datetime]) -> Optional[datetime]:
    if not isinstance(value, datetime):
        return None
    return value.replace(second=0, microsecond=0)


def _meeting_schedule_adjust_permissions(meeting: ChatMeetings, now: Optional[datetime] = None) -> dict[str, bool]:
    current_time = now or _now()
    start_at = getattr(meeting, "scheduled_start_at", None)
    end_at = getattr(meeting, "scheduled_end_at", None)
    status_value = (getattr(meeting, "meeting_status", "") or "UPCOMING").upper()

    before_start = bool(start_at and current_time < start_at)
    before_end = bool((end_at and current_time < end_at) or (not end_at and status_value != "ENDED"))

    return {
        "can_adjust_start": before_start,
        "can_adjust_end": before_end,
        "can_adjust_both": before_start,
    }


def _meeting_schedule_adjustment_type(changed_start: bool, changed_end: bool) -> tuple[str, str]:
    if changed_start and changed_end:
        return "meeting_start_end_time_adjusted", "Đã điều chỉnh thời gian bắt đầu và kết thúc cuộc họp."
    if changed_start:
        return "meeting_start_time_adjusted", "Đã điều chỉnh thời gian bắt đầu cuộc họp."
    if changed_end:
        return "meeting_end_time_adjusted", "Đã điều chỉnh thời gian kết thúc cuộc họp."
    return "meeting_schedule_updated", "Thời gian cuộc họp không thay đổi."

def _active_users(db: Session) -> list[Users]:
    return (
        db.query(Users)
        .filter(Users.is_active == 1)
        .order_by(Users.full_name.asc(), Users.username.asc())
        .all()
    )


def _participant_scopes_for_user(user: Users) -> list[str]:
    scopes: list[str] = []
    role = _role_code(user)

    if role in KSNB_ROLES:
        scopes.append(MEETING_SCOPE_KSNB)
        scopes.append(MEETING_SCOPE_HDTV)
        scopes.append(MEETING_SCOPE_KSNB_TRUNG_TAP)
        scopes.append(MEETING_SCOPE_HDTV_KSNB_TRUNG_TAP)
    if role in BOARD_ROLES:
        scopes.append(MEETING_SCOPE_HDTV)
        scopes.append(MEETING_SCOPE_HDTV_KSNB_TRUNG_TAP)
    if role == ROLE_THANH_VIEN_TRUNG_TAP:
        scopes.append(MEETING_SCOPE_KSNB_TRUNG_TAP)
        scopes.append(MEETING_SCOPE_HDTV_KSNB_TRUNG_TAP)

    scopes.append(MEETING_SCOPE_ALL)
    return list(dict.fromkeys(scopes))


def _participant_options(db: Session) -> list[dict[str, Any]]:
    rows = []
    for user in _active_users(db):
        label = f"{get_display_name(user)} - {_role_label(user)}"
        rows.append(
            {
                "id": str(user.id),
                "label": label,
                "role_code": _role_code(user),
                "is_board": _is_board(user),
                "scopes": _participant_scopes_for_user(user),
            }
        )
    return rows


def _meeting_scope_options(user: Users) -> list[tuple[str, str]]:
    options: list[tuple[str, str]] = []

    if _is_admin(user):
        options.append((MEETING_SCOPE_ALL, "Họp toàn bộ người dùng"))

    if _is_admin(user) or _is_ksnb_manager(user):
        options.append((MEETING_SCOPE_KSNB, "Họp nội bộ Ban KSNB"))
        options.append((MEETING_SCOPE_HDTV, "Họp Ban KSNB / HĐTV"))
        options.append((MEETING_SCOPE_KSNB_TRUNG_TAP, "Họp Ban KSNB và thành viên trưng tập"))
        options.append((MEETING_SCOPE_HDTV_KSNB_TRUNG_TAP, "Họp Hội đồng thành viên, Ban KSNB và thành viên trưng tập"))

    if _is_board(user):
        options.append((MEETING_SCOPE_HDTV, "Họp Ban KSNB / HĐTV"))
        options.append((MEETING_SCOPE_HDTV_KSNB_TRUNG_TAP, "Họp Hội đồng thành viên, Ban KSNB và thành viên trưng tập"))

    seen: set[str] = set()
    clean: list[tuple[str, str]] = []
    for code, label in options:
        if code in seen:
            continue
        seen.add(code)
        clean.append((code, label))
    return clean


def _user_ids_for_scope(db: Session, scope: str) -> list[str]:
    clean_scope = (scope or MEETING_SCOPE_KSNB).strip().upper()
    users = _active_users(db)

    allowed: list[str] = []
    for user in users:
        role = _role_code(user)
        if clean_scope == MEETING_SCOPE_ALL:
            allowed.append(str(user.id))
        elif clean_scope == MEETING_SCOPE_HDTV and (role in KSNB_ROLES or role in BOARD_ROLES):
            allowed.append(str(user.id))
        elif clean_scope == MEETING_SCOPE_KSNB and role in KSNB_ROLES:
            allowed.append(str(user.id))
        elif clean_scope == MEETING_SCOPE_KSNB_TRUNG_TAP and (role in KSNB_ROLES or role == ROLE_THANH_VIEN_TRUNG_TAP):
            allowed.append(str(user.id))
        elif clean_scope == MEETING_SCOPE_HDTV_KSNB_TRUNG_TAP and (role in KSNB_ROLES or role in BOARD_ROLES or role == ROLE_THANH_VIEN_TRUNG_TAP):
            allowed.append(str(user.id))

    return list(dict.fromkeys(allowed))


def _validate_meeting_scope_for_user(user: Users, scope: str) -> str:
    clean_scope = (scope or MEETING_SCOPE_KSNB).strip().upper()
    allowed = {code for code, _label in _meeting_scope_options(user)}
    if clean_scope not in allowed:
        raise HTTPException(status_code=403, detail="Anh/chị không có quyền tạo loại cuộc họp này.")
    return clean_scope


def _ensure_user_in_group(db: Session, group_id: str, user_id: object) -> bool:
    return is_group_member(db, group_id, str(user_id))


def _meeting_upload_dir(group_id: str) -> str:
    abs_dir = os.path.abspath(os.path.join(settings.UPLOAD_DIR, "meetings", str(group_id)))
    os.makedirs(abs_dir, exist_ok=True)
    return abs_dir


def _meeting_preview_dir() -> str:
    abs_dir = os.path.abspath(os.path.join(settings.UPLOAD_DIR, "meetings", "_previews"))
    os.makedirs(abs_dir, exist_ok=True)
    return abs_dir


def _candidate_libreoffice_paths() -> list[str]:
    candidates: list[str] = []

    for key in (
        "LIBREOFFICE_PATH",
        "SOFFICE_PATH",
        "OFFICE_PREVIEW_SOFFICE",
        "OFFICE_PREVIEW_LIBREOFFICE",
    ):
        value = os.environ.get(key, "")
        if value:
            candidates.append(value)

    for attr in (
        "LIBREOFFICE_PATH",
        "SOFFICE_PATH",
        "OFFICE_PREVIEW_SOFFICE",
        "OFFICE_PREVIEW_LIBREOFFICE",
    ):
        value = getattr(settings, attr, "")
        if value:
            candidates.append(str(value))

    for exe_name in ("soffice", "soffice.exe", "libreoffice", "libreoffice.exe"):
        found = shutil.which(exe_name)
        if found:
            candidates.append(found)

    program_files_roots = [
        os.environ.get("PROGRAMFILES", ""),
        os.environ.get("PROGRAMFILES(X86)", ""),
        os.environ.get("PROGRAMW6432", ""),
        r"C:\Program Files",
        r"C:\Program Files (x86)",
    ]
    for root in program_files_roots:
        if not root:
            continue
        candidates.extend(
            [
                os.path.join(root, "LibreOffice", "program", "soffice.exe"),
                os.path.join(root, "LibreOffice", "program", "soffice.com"),
            ]
        )

    candidates.extend(
        [
            "/usr/bin/soffice",
            "/usr/local/bin/soffice",
            "/snap/bin/libreoffice",
            "/Applications/LibreOffice.app/Contents/MacOS/soffice",
        ]
    )

    clean: list[str] = []
    for item in candidates:
        path = os.path.abspath(os.path.expandvars(os.path.expanduser(str(item).strip().strip('"'))))
        if path and path not in clean:
            clean.append(path)
    return clean


def _find_libreoffice_executable() -> str:
    for path in _candidate_libreoffice_paths():
        if os.path.isfile(path):
            return path
    return ""


def _prime_libreoffice_env() -> str:
    soffice_path = _find_libreoffice_executable()
    if not soffice_path:
        return ""

    soffice_dir = os.path.dirname(soffice_path)
    current_path = os.environ.get("PATH", "")
    path_parts = [part for part in current_path.split(os.pathsep) if part]
    if soffice_dir and soffice_dir not in path_parts:
        os.environ["PATH"] = soffice_dir + os.pathsep + current_path

    os.environ.setdefault("LIBREOFFICE_PATH", soffice_path)
    os.environ.setdefault("SOFFICE_PATH", soffice_path)
    os.environ.setdefault("OFFICE_PREVIEW_SOFFICE", soffice_path)
    os.environ.setdefault("OFFICE_PREVIEW_LIBREOFFICE", soffice_path)
    return soffice_path


def _ensure_meeting_office_pdf_preview_auto(source_path: str, preview_key: str, original_name: str) -> str:
    soffice_path = _prime_libreoffice_env()

    try:
        return ensure_office_pdf_preview(
            source_path=source_path,
            preview_key=preview_key,
            original_name=original_name,
        )
    except OfficePreviewError as first_error:
        if not soffice_path:
            raise first_error

        source = Path(source_path)
        if not source.is_file():
            raise OfficePreviewError("Tệp nguồn không còn tồn tại trên máy chủ.") from first_error

        preview_path = Path(_meeting_preview_dir()) / f"{preview_key}.pdf"
        try:
            if preview_path.is_file() and preview_path.stat().st_mtime >= source.stat().st_mtime:
                return str(preview_path)
        except OSError:
            pass

        with tempfile.TemporaryDirectory(prefix="meeting_lo_preview_") as tmp_dir:
            profile_dir = Path(tmp_dir) / "profile"
            out_dir = Path(tmp_dir) / "out"
            profile_dir.mkdir(parents=True, exist_ok=True)
            out_dir.mkdir(parents=True, exist_ok=True)

            cmd = [
                soffice_path,
                "--headless",
                "--nologo",
                "--nofirststartwizard",
                "--nolockcheck",
                "--nodefault",
                f"-env:UserInstallation=file:///{profile_dir.resolve().as_posix()}",
                "--convert-to",
                "pdf",
                "--outdir",
                str(out_dir),
                str(source),
            ]

            completed = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=90,
                check=False,
            )

            pdf_files = sorted(out_dir.glob("*.pdf"), key=lambda item: item.stat().st_mtime, reverse=True)
            if completed.returncode != 0 or not pdf_files:
                detail = (completed.stderr or completed.stdout or "").strip()
                raise OfficePreviewError(
                    "LibreOffice đã được tìm thấy nhưng chưa tạo được bản xem trước PDF cho file Office. "
                    f"Chi tiết: {detail or 'Không có thông tin lỗi từ LibreOffice.'}"
                ) from first_error

            shutil.copyfile(str(pdf_files[0]), str(preview_path))
            return str(preview_path)


def _safe_filename(filename: str) -> str:
    clean_name = Path(filename or "tep_dinh_kem").name
    return clean_name.replace("..", "_").strip() or "tep_dinh_kem"


def _is_allowed_file(filename: str) -> bool:
    return Path(filename or "").suffix.lower() in _ALLOWED_EXTENSIONS


def _stored_attachment_path_to_abs_path(path_value: str) -> str:
    clean = (path_value or "").strip()
    if not clean:
        return ""

    if os.path.isabs(clean):
        return os.path.abspath(clean)

    if clean.startswith("/static/"):
        rel = clean.replace("/static/", "", 1).lstrip("/\\")
        return os.path.abspath(os.path.join(settings.BASE_DIR, "static", rel.replace("/", os.sep)))

    return os.path.abspath(os.path.join(settings.ROOT_DIR, clean.lstrip("/\\").replace("/", os.sep)))


def _attachment_media_type(attachment: ChatAttachments) -> str:
    return attachment.mime_type or mimetypes.guess_type(attachment.filename or "")[0] or "application/octet-stream"


def _is_inline_viewable(filename: str, mime_type: str = "") -> bool:
    media_type = mime_type or mimetypes.guess_type(filename or "")[0] or ""
    if media_type in _INLINE_MIME_EXACT:
        return True
    return any(media_type.startswith(prefix) for prefix in _INLINE_MIME_PREFIXES)


def _attachment_preview_url(attachment_id: str) -> str:
    return f"/meetings/attachments/{attachment_id}/preview"


def _attachment_download_url(attachment_id: str) -> str:
    return f"/meetings/attachments/{attachment_id}/download"


def _build_content_disposition(kind: str, filename: str) -> str:
    return f"{kind}; filename*=UTF-8''{quote(filename or 'tep_dinh_kem')}"


def _get_attendance_row_for_user(db: Session, meeting_id: str, user_id: object) -> Optional[ChatMeetingAttendances]:
    return (
        db.query(ChatMeetingAttendances)
        .filter(ChatMeetingAttendances.meeting_id == meeting_id)
        .filter(ChatMeetingAttendances.user_id == str(user_id))
        .first()
    )


def _current_speaker_permission(db: Session, meeting_id: str, user_id: object) -> Optional[ChatMeetingSpeakerRequests]:
    return (
        db.query(ChatMeetingSpeakerRequests)
        .filter(ChatMeetingSpeakerRequests.meeting_id == meeting_id)
        .filter(ChatMeetingSpeakerRequests.user_id == str(user_id))
        .filter(ChatMeetingSpeakerRequests.request_status.in_(["APPROVED", "SPEAKING"]))
        .order_by(ChatMeetingSpeakerRequests.approved_at.desc(), ChatMeetingSpeakerRequests.created_at.desc())
        .first()
    )


def _has_pending_speaker_request(db: Session, meeting_id: str, user_id: object) -> bool:
    return (
        db.query(ChatMeetingSpeakerRequests.id)
        .filter(ChatMeetingSpeakerRequests.meeting_id == meeting_id)
        .filter(ChatMeetingSpeakerRequests.user_id == str(user_id))
        .filter(ChatMeetingSpeakerRequests.request_status == "PENDING")
        .first()
        is not None
    )


def _has_pending_leave_request(db: Session, meeting_id: str, user_id: object) -> bool:
    return (
        db.query(ChatMeetingLeaveRequests.id)
        .filter(ChatMeetingLeaveRequests.meeting_id == meeting_id)
        .filter(ChatMeetingLeaveRequests.user_id == str(user_id))
        .filter(ChatMeetingLeaveRequests.request_status == "PENDING")
        .first()
        is not None
    )


def _load_leave_requests(db: Session, meeting_id: str) -> list[ChatMeetingLeaveRequests]:
    rows = (
        db.query(ChatMeetingLeaveRequests)
        .filter(ChatMeetingLeaveRequests.meeting_id == meeting_id)
        .order_by(ChatMeetingLeaveRequests.created_at.asc())
        .all()
    )

    for row in rows:
        row.request_status_label = _leave_status_label(row.request_status)

    return rows


def _is_meeting_host(meeting: ChatMeetings, user_id: object) -> bool:
    return str(getattr(meeting, "host_user_id", "") or "") == str(user_id or "")


def _is_meeting_participant_not_host(meeting: ChatMeetings, user_id: object) -> bool:
    return bool(user_id) and not _is_meeting_host(meeting, user_id)

def _can_user_send_meeting_message(db: Session, meeting: ChatMeetings, user_id: object) -> bool:
    uid = str(user_id or "")
    if uid == str(meeting.host_user_id or ""):
        return True

    attendance = _get_attendance_row_for_user(db, meeting.id, uid)
    if (getattr(attendance, "attendance_status", "") or "").upper() != "CHECKED_IN":
        return False

    return bool(_current_speaker_permission(db, meeting.id, uid))


def _consume_speaker_permission(db: Session, meeting_id: str, user_id: object) -> None:
    row = _current_speaker_permission(db, meeting_id, user_id)
    if not row:
        return
    row.request_status = "DONE"
    row.updated_at = _now()
    db.add(row)
    db.commit()


def _ensure_meeting_runtime_rules(db: Session, meeting: Optional[ChatMeetings]) -> Optional[ChatMeetings]:
    if not meeting:
        return None

    old_status = (meeting.meeting_status or "UPCOMING").upper()
    meeting = transition_meeting_status_if_needed(db, meeting) or meeting
    new_status = (meeting.meeting_status or "UPCOMING").upper()

    if old_status != new_status and new_status == "LIVE":
        remove_absent_members_from_live_meeting(db, meeting.id)
        meeting = db.get(ChatMeetings, meeting.id) or meeting

    return meeting


def _message_vm(db: Session, message: ChatMessages, current_user_id: object) -> dict[str, Any]:
    attachments = get_message_attachments(db, message.id)
    return {
        "id": message.id,
        "sender_name": get_display_name(message.sender),
        "is_mine": str(message.sender_user_id or "") == str(current_user_id or ""),
        "content": message.content or "",
        "message_type": message.message_type or "TEXT",
        "created_at_text": _format_vn_dt(message.created_at),
        "attachments": [
            {
                "id": att.id,
                "filename": att.filename,
                "preview_url": _attachment_preview_url(att.id),
                "download_url": _attachment_download_url(att.id),
                "is_previewable": _is_inline_viewable(att.filename or "", att.mime_type or "") or is_office_previewable(att.filename or ""),
            }
            for att in attachments
            if not att.deleted_by_owner and not att.recalled
        ],
    }


def _meeting_documents(db: Session, group_id: str, current_user_id: object) -> list[dict[str, Any]]:
    rows = (
        db.query(ChatMessages)
        .filter(ChatMessages.group_id == group_id)
        .filter(ChatMessages.message_type == "MEETING_DOC")
        .filter(ChatMessages.deleted_by_owner.is_(False))
        .order_by(ChatMessages.created_at.desc())
        .all()
    )

    docs: list[dict[str, Any]] = []
    for msg in rows:
        vm = _message_vm(db, msg, current_user_id)
        for att in vm["attachments"]:
            docs.append(
                {
                    "message_id": msg.id,
                    "sender_name": vm["sender_name"],
                    "created_at_text": vm["created_at_text"],
                    **att,
                }
            )
    return docs


def _get_latest_meeting_conclusion_message(db: Session, group_id: str) -> Optional[ChatMessages]:
    return (
        db.query(ChatMessages)
        .filter(ChatMessages.group_id == group_id)
        .filter(ChatMessages.message_type == "MEETING_CONCLUSION")
        .filter(ChatMessages.deleted_by_owner.is_(False))
        .order_by(ChatMessages.created_at.desc())
        .first()
    )


def _is_host_or_secretary(meeting: ChatMeetings, user_id: object) -> bool:
    uid = str(user_id or "")
    return uid in {
        str(getattr(meeting, "host_user_id", "") or ""),
        str(getattr(meeting, "secretary_user_id", "") or ""),
    }


def _build_minutes_speaker_sections(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sections: list[dict[str, Any]] = []
    by_user: dict[str, dict[str, Any]] = {}

    for msg in messages or []:
        message_type = (msg.get("message_type") or "").strip().upper()
        if message_type not in {"TEXT", "FILE"}:
            continue

        sender_name = (msg.get("sender_name") or "Người dùng").strip() or "Người dùng"
        bucket = by_user.get(sender_name)
        if not bucket:
            bucket = {
                "sender_name": sender_name,
                "entries": [],
            }
            by_user[sender_name] = bucket
            sections.append(bucket)

        content = (msg.get("content") or "").strip()
        attachments = msg.get("attachments") or []

        parts: list[str] = []
        if content:
            parts.append(content)

        if attachments:
            file_names: list[str] = []
            for att in attachments:
                file_name = (att.get("filename") or "").strip()
                if file_name:
                    file_names.append(file_name)
            if file_names:
                parts.append("Tệp trao đổi: " + ", ".join(file_names))

        if not parts:
            continue

        bucket["entries"].append(
            {
                "created_at_text": (msg.get("created_at_text") or "").strip(),
                "text": " ".join(parts).strip(),
            }
        )

    return sections


def _build_meeting_minutes_text(detail: dict[str, Any]) -> str:
    meeting = detail.get("meeting")
    group = detail.get("group")
    attendance_rows = detail.get("attendance_rows") or []
    leave_rows = detail.get("leave_requests") or []
    messages = detail.get("messages") or []
    conclusion_text = (detail.get("conclusion_text") or "").strip()

    checked_in_count = int(detail.get("attendance_checked_in_count") or 0)
    absent_count = int(detail.get("attendance_absent_count") or 0)
    pending_count = int(detail.get("attendance_pending_count") or 0)

    leave_approved_count = 0
    for row in leave_rows:
        if (getattr(row, "request_status", "") or "").upper() == "APPROVED":
            leave_approved_count += 1

    sections = _build_minutes_speaker_sections(messages)

    lines: list[str] = []
    lines.append("BIÊN BẢN HỌP TRỰC TUYẾN")
    lines.append("")
    lines.append(f"Tên cuộc họp: {getattr(group, 'name', '') or '—'}")
    lines.append(f"Loại cuộc họp: {detail.get('meeting_scope_label') or '—'}")
    lines.append(f"Thời gian bắt đầu: {_format_vn_dt(getattr(meeting, 'scheduled_start_at', None))}")
    lines.append(f"Thời gian kết thúc: {_format_vn_dt(getattr(meeting, 'scheduled_end_at', None))}")
    lines.append(f"Chủ trì: {detail.get('host_name') or '—'}")
    lines.append(f"Thư ký: {detail.get('secretary_name') or '—'}")
    lines.append("")
    lines.append("I. NỘI DUNG DỰ KIẾN")
    lines.append(getattr(meeting, "agenda", "") or "—")
    lines.append("")
    lines.append("II. THÀNH PHẦN THAM DỰ")
    lines.append(f"- Đã điểm danh: {checked_in_count}")
    lines.append(f"- Báo vắng: {absent_count}")
    lines.append(f"- Chưa điểm danh: {pending_count}")
    lines.append(f"- Đã được Chủ trì cho phép rời họp: {leave_approved_count}")
    lines.append("")

    for row in attendance_rows:
        user_name = get_display_name(row.user) if getattr(row, "user", None) else str(row.user_id or "")
        status_text = _attendance_status_label(row.attendance_status)
        lines.append(f"- {user_name}: {status_text}")

    lines.append("")
    lines.append("III. NỘI DUNG PHÁT BIỂU / TRAO ĐỔI")
    if sections:
        for section in sections:
            lines.append(f"1. {section['sender_name']}")
            for entry in section["entries"]:
                time_text = entry.get("created_at_text") or ""
                text = entry.get("text") or ""
                lines.append(f"   - [{time_text}] {text}")
    else:
        lines.append("—")

    lines.append("")
    lines.append("IV. KẾT LUẬN CUỘC HỌP")
    lines.append(conclusion_text or "—")

    return "\n".join(lines)

def _meeting_member_count(db: Session, group_id: str) -> int:
    return (
        db.query(ChatGroupMembers)
        .filter(ChatGroupMembers.group_id == group_id)
        .count()
    )


def _group_list_vm(db: Session, groups: list[ChatGroups], current_user: Users, selected_id: str = "") -> list[ChatGroups]:
    result: list[ChatGroups] = []

    for group in groups:
        meeting = _ensure_meeting_runtime_rules(db, get_meeting_by_group_id(db, group.id))
        group.meeting_row = meeting
        group.member_count = _meeting_member_count(db, group.id)

        if meeting:
            group.list_status_label = _meeting_status_label(meeting.meeting_status)
            group.can_delete_meeting = _can_delete_meeting(meeting, current_user.id)
        else:
            group.list_status_label = "Chưa có thông tin"
            group.can_delete_meeting = str(group.owner_user_id or "") == str(current_user.id)

        group.is_selected = str(group.id) == str(selected_id or "")
        result.append(group)

    return result


def _group_by_month(groups: list[ChatGroups], selected_id: str = "") -> list[dict[str, Any]]:
    buckets: dict[tuple[int, int], list[ChatGroups]] = {}

    for group in groups:
        meeting = getattr(group, "meeting_row", None)
        dt = getattr(meeting, "scheduled_start_at", None) or getattr(group, "created_at", None) or _now()
        key = (dt.year, dt.month)
        buckets.setdefault(key, []).append(group)

    years: dict[int, list[dict[str, Any]]] = {}
    for (year, month), month_groups in sorted(buckets.items(), reverse=True):
        is_open = any(str(g.id) == str(selected_id or "") for g in month_groups)
        month_bucket = {
            "month_label": f"Tháng {month:02d}/{year}",
            "count": len(month_groups),
            "groups": month_groups,
            "is_open": is_open,
        }
        years.setdefault(year, []).append(month_bucket)

    result: list[dict[str, Any]] = []
    for year in sorted(years.keys(), reverse=True):
        months = years[year]
        result.append(
            {
                "year_label": f"Năm {year}",
                "months": months,
                "is_open": any(m["is_open"] for m in months),
            }
        )

    return result


def _selected_meeting_detail(db: Session, group_id: str, current_user: Users) -> Optional[dict[str, Any]]:
    if not group_id:
        return None

    group = get_group_by_id(db, group_id)
    if not group or (group.group_type or "").upper() != "MEETING":
        return None

    if not _ensure_user_in_group(db, group_id, current_user.id):
        return None

    meeting = _ensure_meeting_runtime_rules(db, get_meeting_by_group_id(db, group_id))
    if not meeting:
        return None

    attendance_rows = get_meeting_attendance_rows(db, meeting.id)
    speaker_rows = list_speaker_requests(db, meeting.id)
    messages = get_group_messages(db, group_id, limit=200)
    current_attendance = _get_attendance_row_for_user(db, meeting.id, current_user.id)

    if current_attendance:
        current_attendance = set_meeting_presence(db, meeting.id, str(current_user.id), True)

        for idx, attendance_row in enumerate(attendance_rows):
            if str(getattr(attendance_row, "user_id", "") or "") == str(current_user.id):
                attendance_rows[idx] = current_attendance
                break

    for row in attendance_rows:
        row.attendance_status_label = _attendance_status_label(row.attendance_status)
        row.presence_status_label = (
            "Đang có mặt trong cuộc họp"
            if (row.presence_status or "").upper() == "ONLINE"
            else "Không có mặt trong cuộc họp"
        )

    for row in speaker_rows:
        row.user_name = get_display_name(row.user) if getattr(row, "user", None) else str(row.user_id or "")
        row.request_status_label = _speaker_status_label(row.request_status)

    leave_rows = _load_leave_requests(db, meeting.id)
    for row in leave_rows:
        row.user_name = get_display_name(row.user) if getattr(row, "user", None) else str(row.user_id or "")

    meeting_status = (meeting.meeting_status or "UPCOMING").upper()
    current_attendance_status = (
        (getattr(current_attendance, "attendance_status", "") or "PENDING").upper()
        if current_attendance
        else "PENDING"
    )
    is_host = _is_meeting_host(meeting, current_user.id)
    is_participant_not_host = _is_meeting_participant_not_host(meeting, current_user.id)
    has_pending_speaker_request = _has_pending_speaker_request(db, meeting.id, current_user.id)
    has_pending_leave_request = _has_pending_leave_request(db, meeting.id, current_user.id)

    can_register_speaker = (
        meeting_status == "LIVE"
        and current_attendance_status == "CHECKED_IN"
        and is_participant_not_host
        and not has_pending_speaker_request
        and not _can_user_send_meeting_message(db, meeting, current_user.id)
    )
    can_cancel_absent = meeting_status == "UPCOMING" and current_attendance_status == "ABSENT"

    can_request_leave = (
        meeting_status == "LIVE"
        and current_attendance_status == "CHECKED_IN"
        and is_participant_not_host
        and not has_pending_leave_request
    )

    attendance_pending_count = 0
    attendance_absent_count = 0
    attendance_checked_in_count = 0
    for row in attendance_rows:
        row_status = (getattr(row, "attendance_status", "") or "PENDING").upper()
        if row_status == "ABSENT":
            attendance_absent_count += 1
        elif row_status == "CHECKED_IN":
            attendance_checked_in_count += 1
        else:
            attendance_pending_count += 1

    latest_conclusion_message = _get_latest_meeting_conclusion_message(db, group_id)
    conclusion_text = (getattr(latest_conclusion_message, "content", "") or "").strip()
    conclusion_updated_text = ""
    if getattr(latest_conclusion_message, "created_at", None):
        conclusion_updated_text = _format_vn_dt(latest_conclusion_message.created_at)

    meeting_documents = _meeting_documents(db, group_id, current_user.id)
    can_upload_documents = _is_host_or_secretary(meeting, current_user.id)
    can_export_minutes = str(getattr(meeting, "secretary_user_id", "") or "") == str(current_user.id)

    return {
        "group": group,
        "meeting": meeting,
        "meeting_status_label": _meeting_status_label(meeting.meeting_status),
        "meeting_scope_label": _meeting_scope_label(meeting.meeting_scope),
        "host_name": get_display_name(meeting.host) if meeting.host else "—",
        "secretary_name": get_display_name(meeting.secretary) if meeting.secretary else "—",
        "designed_by_name": get_display_name(meeting.designed_by) if meeting.designed_by else "—",
        "attendance_rows": attendance_rows,
        "speaker_rows": speaker_rows,
        "speaker_requests": speaker_rows,
        "messages": [_message_vm(db, msg, current_user.id) for msg in messages if not msg.deleted_by_owner],
        "documents": meeting_documents,
        "meeting_documents": meeting_documents,
        "current_attendance": current_attendance,
        "current_attendance_status": current_attendance_status,
        "can_manage": _can_manage_meeting(meeting, current_user.id),
        "schedule_start_local_value": _datetime_local_value(meeting.scheduled_start_at),
        "schedule_end_local_value": _datetime_local_value(meeting.scheduled_end_at),
        **_meeting_schedule_adjust_permissions(meeting),
        "can_delete": _can_delete_meeting(meeting, current_user.id),
        "is_host": is_host,
        "is_participant_not_host": is_participant_not_host,
        "can_checkin": meeting_status == "LIVE" and current_attendance_status != "CHECKED_IN",
        "can_absent": meeting_status == "UPCOMING" and current_attendance_status != "ABSENT",
        "can_cancel_absent": can_cancel_absent,
        "can_register_speaker": can_register_speaker,
        "has_pending_speaker_request": has_pending_speaker_request,
        "can_request_leave": can_request_leave,
        "has_pending_leave_request": has_pending_leave_request,
        "leave_rows": leave_rows,
        "leave_requests": leave_rows,
        "attendance_pending_count": attendance_pending_count,
        "attendance_absent_count": attendance_absent_count,
        "attendance_checked_in_count": attendance_checked_in_count,
        "can_upload_documents": can_upload_documents,
        "can_export_minutes": can_export_minutes,
        "conclusion_text": conclusion_text,
        "conclusion_updated_text": conclusion_updated_text,
        "can_edit_conclusion": _is_meeting_host(meeting, current_user.id),
        "can_send_message": _can_user_send_meeting_message(db, meeting, current_user.id),
        "can_send_meeting_message": _can_user_send_meeting_message(db, meeting, current_user.id),
        "host_options": _active_users(db),
        "secretary_options": _active_users(db),
    }


def _notify_user_ids_for_group(db: Session, group_id: str, *extra_ids: object) -> list[str]:
    ids = get_group_member_user_ids(db, group_id)
    for item in extra_ids:
        uid = str(item or "").strip()
        if uid and uid not in ids:
            ids.append(uid)
    return ids


async def _notify_meeting_users(user_ids: Iterable[object], payload: dict[str, Any]) -> None:
    clean: list[str] = []
    for item in user_ids:
        uid = str(item or "").strip()
        if uid and uid not in clean:
            clean.append(uid)

    group_id = str((payload or {}).get("group_id") or "").strip()
    if group_id:
        await manager.broadcast_group_text(group_id, json.dumps(payload, ensure_ascii=False))

    if clean:
        await manager.notify_users_json(clean, payload)


def _redirect(group_id: str = "", msg: str = "", error: str = "") -> RedirectResponse:
    url = "/meetings"
    if group_id:
        url += f"?selected_id={quote(str(group_id))}"
    if msg:
        url += ("&" if "?" in url else "?") + f"msg={quote(msg)}"
    if error:
        url += ("&" if "?" in url else "?") + f"error={quote(error)}"
    return RedirectResponse(url=url, status_code=status.HTTP_303_SEE_OTHER)


@router.get("/meetings")
def meetings_index(
    request: Request,
    selected_id: str = "",
    db: Session = Depends(get_db),
):
    _ensure_meeting_tables()

    current_user = login_required(request, db)
    if not _can_access_meeting_module(current_user):
        raise HTTPException(status_code=403, detail="Anh/chị không có quyền truy cập Họp trực tuyến.")

    groups = get_user_meeting_groups(db, str(current_user.id))
    groups = _group_list_vm(db, groups, current_user, selected_id=selected_id)

    if not selected_id and groups:
        selected_id = str(groups[0].id)

    selected_detail = _selected_meeting_detail(db, selected_id, current_user)

    return templates.TemplateResponse(
        "meetings/index.html",
        {
            "request": request,
            "groups": groups,
            "meeting_groups_by_month": _group_by_month(groups, selected_id=selected_id),
            "selected_id": selected_id,
            "selected_detail": selected_detail,
            "total_meeting_count": len(groups),
            "invited_meeting_count": len(groups),
            "current_user_can_create_meeting": _can_create_meeting(current_user),
            "meeting_scope_options": _meeting_scope_options(current_user),
            "meeting_participant_options": _participant_options(db),
            "me": current_user,
        },
    )


@router.post("/meetings/create")
async def meeting_create(
    request: Request,
    name: str = Form(...),
    meeting_scope: str = Form(MEETING_SCOPE_KSNB),
    scheduled_start_at: str = Form(...),
    scheduled_end_at: str = Form(""),
    agenda: str = Form(""),
    participant_ids: list[str] = Form([]),
    host_user_id: str = Form(""),
    secretary_user_id: str = Form(""),
    db: Session = Depends(get_db),
):
    _ensure_meeting_tables()

    current_user = login_required(request, db)
    if not _can_create_meeting(current_user):
        raise HTTPException(status_code=403, detail="Vị trí hiện tại không được tạo cuộc họp.")

    clean_name = (name or "").strip()
    if not clean_name:
        raise HTTPException(status_code=400, detail="Tên buổi họp không được để trống.")

    scope = _validate_meeting_scope_for_user(current_user, meeting_scope)
    start_dt = _parse_datetime_local(scheduled_start_at)
    end_dt = _parse_datetime_local(scheduled_end_at)

    if end_dt and start_dt and end_dt <= start_dt:
        raise HTTPException(status_code=400, detail="Thời điểm kết thúc phải sau thời điểm bắt đầu.")

    allowed_user_ids = set(_user_ids_for_scope(db, scope))

    clean_participants: list[str] = []
    for uid in participant_ids or []:
        clean = str(uid or "").strip()
        if clean and clean in allowed_user_ids and clean not in clean_participants:
            clean_participants.append(clean)

    creator_id = str(current_user.id)
    if creator_id not in clean_participants:
        clean_participants.append(creator_id)

    clean_host_id = str(host_user_id or "").strip()
    if clean_host_id and clean_host_id not in allowed_user_ids:
        raise HTTPException(status_code=400, detail="Người chủ trì không thuộc phạm vi cuộc họp.")

    if not clean_host_id:
        clean_host_id = creator_id

    if clean_host_id not in clean_participants:
        clean_participants.append(clean_host_id)

    clean_secretary_id = str(secretary_user_id or "").strip()
    if clean_secretary_id:
        if clean_secretary_id not in allowed_user_ids:
            raise HTTPException(status_code=400, detail="Thư ký không thuộc phạm vi cuộc họp.")
        if clean_secretary_id not in clean_participants:
            clean_participants.append(clean_secretary_id)

    group = create_group(
        db,
        name=clean_name,
        owner_user_id=creator_id,
        group_type="MEETING",
        unit_id=None,
    )

    for uid in clean_participants:
        if uid == creator_id:
            continue
        add_member_to_group(
            db,
            group_id=group.id,
            user_id=uid,
            member_role="member",
            mark_as_new=True,
        )

    meeting = create_meeting_session(
        db,
        group_id=group.id,
        designed_by_user_id=creator_id,
        host_user_id=clean_host_id,
        secretary_user_id=clean_secretary_id or None,
        meeting_scope=scope,
        scheduled_start_at=start_dt,
        scheduled_end_at=end_dt,
        agenda=agenda,
        committee_id=None,
    )

    ensure_meeting_attendance_rows(db, meeting.id, clean_participants)

    payload = {
        "module": "meeting",
        "type": "meeting_invited",
        "group_id": str(group.id),
        "meeting_id": str(meeting.id),
        "group_name": clean_name,
        "timestamp": datetime.utcnow().isoformat(),
    }

    await _notify_meeting_users(clean_participants, payload)

    return _redirect(group.id, msg="Đã tạo cuộc họp.")


@router.post("/meetings/{group_id}/delete")
async def meeting_delete(
    group_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    _ensure_meeting_tables()

    current_user = login_required(request, db)
    group = get_group_by_id(db, group_id)
    if not group or (group.group_type or "").upper() != "MEETING":
        raise HTTPException(status_code=404, detail="Không tìm thấy cuộc họp.")

    if not _ensure_user_in_group(db, group_id, current_user.id):
        raise HTTPException(status_code=403, detail="Anh/chị không thuộc cuộc họp này.")

    meeting = get_meeting_by_group_id(db, group_id)
    if meeting and not _can_delete_meeting(meeting, current_user.id):
        raise HTTPException(status_code=403, detail="Chỉ người thiết kế, Chủ trì hoặc Thư ký mới được xóa cuộc họp.")

    notify_user_ids = _notify_user_ids_for_group(db, group_id)

    if meeting:
        db.query(ChatMeetingSpeakerRequests).filter(ChatMeetingSpeakerRequests.meeting_id == meeting.id).delete(synchronize_session=False)
        db.query(ChatMeetingLeaveRequests).filter(ChatMeetingLeaveRequests.meeting_id == meeting.id).delete(synchronize_session=False)
        db.query(ChatMeetingAttendances).filter(ChatMeetingAttendances.meeting_id == meeting.id).delete(synchronize_session=False)
        db.delete(meeting)

    group.is_active = False
    db.add(group)
    db.commit()

    payload = {
        "module": "meeting",
        "type": "meeting_deleted",
        "group_id": group_id,
        "deleted_by_user_id": str(current_user.id),
        "timestamp": datetime.utcnow().isoformat(),
    }
    await _notify_meeting_users(notify_user_ids, payload)

    return _redirect(msg="Đã xóa cuộc họp.")


@router.post("/meetings/{group_id}/schedule")
async def meeting_update_schedule(
    group_id: str,
    request: Request,
    scheduled_start_at: str = Form(""),
    scheduled_end_at: str = Form(""),
    db: Session = Depends(get_db),
):
    _ensure_meeting_tables()

    current_user = login_required(request, db)
    meeting = get_meeting_by_group_id(db, group_id)
    if not meeting:
        raise HTTPException(status_code=404, detail="Không tìm thấy cuộc họp.")

    if not _ensure_user_in_group(db, group_id, current_user.id):
        raise HTTPException(status_code=403, detail="Anh/chị không thuộc cuộc họp này.")

    if not _can_manage_meeting(meeting, current_user.id):
        raise HTTPException(status_code=403, detail="Chỉ người thiết kế, Chủ trì hoặc Thư ký mới được điều chỉnh thời gian cuộc họp.")

    old_start = _normalize_dt_minute(getattr(meeting, "scheduled_start_at", None))
    old_end = _normalize_dt_minute(getattr(meeting, "scheduled_end_at", None))

    new_start = _normalize_dt_minute(_parse_datetime_local(scheduled_start_at))
    new_end = _normalize_dt_minute(_parse_datetime_local(scheduled_end_at))

    if not new_start:
        return _redirect(group_id, error="Thời gian bắt đầu cuộc họp không được để trống.")

    if new_end and new_start and new_end <= new_start:
        return _redirect(group_id, error="Thời điểm kết thúc phải sau thời điểm bắt đầu.")

    changed_start = new_start != old_start
    changed_end = new_end != old_end

    if not changed_start and not changed_end:
        return _redirect(group_id, msg="Thời gian cuộc họp không thay đổi.")

    now = _now()
    permissions = _meeting_schedule_adjust_permissions(meeting, now=now)

    if changed_start and changed_end and not permissions["can_adjust_both"]:
        return _redirect(
            group_id,
            error="Chỉ được điều chỉnh đồng thời thời gian bắt đầu và kết thúc khi cuộc họp chưa bắt đầu.",
        )

    if changed_start and not permissions["can_adjust_start"]:
        return _redirect(
            group_id,
            error="Chỉ được điều chỉnh thời gian bắt đầu khi cuộc họp chưa bắt đầu.",
        )

    if changed_end and not permissions["can_adjust_end"]:
        return _redirect(
            group_id,
            error="Chỉ được điều chỉnh thời gian kết thúc trước thời gian kết thúc hiện tại của cuộc họp.",
        )

    payload_type, success_msg = _meeting_schedule_adjustment_type(changed_start, changed_end)

    meeting.scheduled_start_at = new_start
    meeting.scheduled_end_at = new_end
    meeting.updated_at = _now()
    db.add(meeting)
    db.commit()
    db.refresh(meeting)

    await _notify_meeting_users(
        _notify_user_ids_for_group(db, group_id),
        {
            "module": "meeting",
            "type": payload_type,
            "schedule_adjustment_type": payload_type,
            "group_id": group_id,
            "meeting_id": meeting.id,
            "scheduled_start_at": meeting.scheduled_start_at.isoformat() if meeting.scheduled_start_at else "",
            "scheduled_end_at": meeting.scheduled_end_at.isoformat() if meeting.scheduled_end_at else "",
            "scheduled_start_at_text": _format_vn_dt(meeting.scheduled_start_at),
            "scheduled_end_at_text": _format_vn_dt(meeting.scheduled_end_at),
            "timestamp": datetime.utcnow().isoformat(),
        },
    )

    return _redirect(group_id, msg=success_msg)


@router.post("/meetings/{group_id}/host")
async def meeting_assign_host(
    group_id: str,
    request: Request,
    host_user_id: str = Form(...),
    db: Session = Depends(get_db),
):
    _ensure_meeting_tables()

    current_user = login_required(request, db)
    meeting = get_meeting_by_group_id(db, group_id)
    if not meeting:
        raise HTTPException(status_code=404, detail="Không tìm thấy cuộc họp.")

    if not _can_manage_meeting(meeting, current_user.id):
        raise HTTPException(status_code=403, detail="Chỉ người thiết kế, Chủ trì hoặc Thư ký mới được đổi Chủ trì.")

    if not _ensure_user_in_group(db, group_id, host_user_id):
        raise HTTPException(status_code=400, detail="Người được chọn chưa thuộc cuộc họp.")

    assign_meeting_host(db, meeting.id, str(host_user_id))
    return _redirect(group_id, msg="Đã cập nhật Chủ trì.")


@router.post("/meetings/{group_id}/secretary")
async def meeting_assign_secretary(
    group_id: str,
    request: Request,
    secretary_user_id: str = Form(""),
    db: Session = Depends(get_db),
):
    _ensure_meeting_tables()

    current_user = login_required(request, db)
    meeting = get_meeting_by_group_id(db, group_id)
    if not meeting:
        raise HTTPException(status_code=404, detail="Không tìm thấy cuộc họp.")

    if not _can_manage_meeting(meeting, current_user.id):
        raise HTTPException(status_code=403, detail="Chỉ người thiết kế, Chủ trì hoặc Thư ký mới được đổi Thư ký.")

    clean_secretary_id = str(secretary_user_id or "").strip()
    if clean_secretary_id and not _ensure_user_in_group(db, group_id, clean_secretary_id):
        raise HTTPException(status_code=400, detail="Người được chọn chưa thuộc cuộc họp.")

    assign_meeting_secretary(db, meeting.id, clean_secretary_id or None)
    return _redirect(group_id, msg="Đã cập nhật Thư ký.")


@router.post("/meetings/{group_id}/presence/join")
async def meeting_presence_join(
    group_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    _ensure_meeting_tables()

    current_user = login_required(request, db)
    if not _ensure_user_in_group(db, group_id, current_user.id):
        raise HTTPException(status_code=403, detail="Anh/chị không thuộc cuộc họp này.")

    meeting = _ensure_meeting_runtime_rules(db, get_meeting_by_group_id(db, group_id))
    if not meeting:
        raise HTTPException(status_code=404, detail="Không tìm thấy cuộc họp.")

    row = set_meeting_presence(db, meeting.id, str(current_user.id), True)
    attendance_status = (getattr(row, "attendance_status", "") or "PENDING").upper()
    can_send = _can_user_send_meeting_message(db, meeting, current_user.id)

    payload = {
        "type": "meeting_presence_joined",
        "group_id": group_id,
        "meeting_id": meeting.id,
        "user_id": str(current_user.id),
        "user_name": get_display_name(current_user),
        "attendance_status": attendance_status,
        "attendance_status_label": _attendance_status_label(attendance_status),
        "presence_status": "ONLINE",
        "presence_status_label": "Đang có mặt trong cuộc họp",
        "can_send_meeting_message": can_send,
        "can_register_speaker": (
            (meeting.meeting_status or "").upper() == "LIVE"
            and attendance_status == "CHECKED_IN"
            and not _is_meeting_host(meeting, current_user.id)
            and not _has_pending_speaker_request(db, meeting.id, current_user.id)
            and not can_send
        ),
    }
    await manager.broadcast_group_text(group_id, json.dumps(payload, ensure_ascii=False))
    return JSONResponse({"ok": True, **payload})


@router.post("/meetings/{group_id}/presence/leave")
async def meeting_presence_leave(
    group_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    _ensure_meeting_tables()

    current_user = login_required(request, db)
    meeting = get_meeting_by_group_id(db, group_id)
    if not meeting:
        return JSONResponse(
            {
                "ok": True,
                "type": "meeting_deleted",
                "group_id": group_id,
                "redirect_url": "/meetings",
            }
        )

    if not _ensure_user_in_group(db, group_id, current_user.id):
        return JSONResponse(
            {
                "ok": True,
                "type": "meeting_deleted",
                "group_id": group_id,
                "redirect_url": "/meetings",
            }
        )

    set_meeting_presence(db, meeting.id, str(current_user.id), False)
    return JSONResponse({"ok": True})


@router.post("/meetings/{group_id}/sync")
async def meeting_sync(
    group_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    _ensure_meeting_tables()

    current_user = login_required(request, db)
    meeting = _ensure_meeting_runtime_rules(db, get_meeting_by_group_id(db, group_id))
    if not meeting:
        return JSONResponse(
            {
                "ok": True,
                "type": "meeting_deleted",
                "group_id": group_id,
                "removed_from_meeting": True,
                "redirect_url": "/meetings",
            }
        )

    if not _ensure_user_in_group(db, group_id, current_user.id):
        return JSONResponse(
            {
                "ok": True,
                "type": "meeting_status_sync",
                "removed_from_meeting": True,
                "redirect_url": "/meetings",
                "meeting_status": meeting.meeting_status,
            }
        )

    attendance = _get_attendance_row_for_user(db, meeting.id, current_user.id)
    can_send = _can_user_send_meeting_message(db, meeting, current_user.id)

    meeting_status = (meeting.meeting_status or "UPCOMING").upper()
    attendance_status = (getattr(attendance, "attendance_status", "PENDING") if attendance else "PENDING") or "PENDING"
    attendance_status = attendance_status.upper()

    if meeting_status == "LIVE" and attendance_status == "CHECKED_IN":
        action_mode = "checked_in"
    elif meeting_status == "LIVE":
        action_mode = "checkin"
    elif meeting_status == "ENDED":
        action_mode = "closed"
    elif attendance_status == "ABSENT":
        action_mode = "absent_cancel"
    else:
        action_mode = "absent"

    return JSONResponse(
        {
            "ok": True,
            "type": "meeting_status_sync",
            "group_id": group_id,
            "meeting_id": meeting.id,
            "meeting_status": meeting_status,
            "meeting_status_label": _meeting_status_label(meeting_status),
            "action_mode": action_mode,
            "current_attendance_status": attendance_status,
            "can_absent": meeting_status == "UPCOMING" and attendance_status != "ABSENT",
            "can_cancel_absent": meeting_status == "UPCOMING" and attendance_status == "ABSENT",
            "can_checkin": meeting_status == "LIVE" and attendance_status != "CHECKED_IN",
            "can_export_minutes": str(getattr(meeting, "secretary_user_id", "") or "") == str(current_user.id),
            "can_send_meeting_message": can_send,
            "can_register_speaker": meeting_status == "LIVE"
            and attendance_status == "CHECKED_IN"
            and not _is_meeting_host(meeting, current_user.id)
            and not _has_pending_speaker_request(db, meeting.id, current_user.id)
            and not can_send,
            "can_request_leave": meeting_status == "LIVE"
            and attendance_status == "CHECKED_IN"
            and not _is_meeting_host(meeting, current_user.id)
            and not _has_pending_leave_request(db, meeting.id, current_user.id),
            "has_pending_speaker_request": _has_pending_speaker_request(db, meeting.id, current_user.id),
            "has_pending_leave_request": _has_pending_leave_request(db, meeting.id, current_user.id),
            "is_host": _is_meeting_host(meeting, current_user.id),
        }
    )


@router.post("/meetings/{group_id}/absent")
async def meeting_absent(
    group_id: str,
    request: Request,
    reason: str = Form(""),
    db: Session = Depends(get_db),
):
    _ensure_meeting_tables()

    current_user = login_required(request, db)
    if not _ensure_user_in_group(db, group_id, current_user.id):
        raise HTTPException(status_code=403, detail="Anh/chị không thuộc cuộc họp này.")

    meeting = _ensure_meeting_runtime_rules(db, get_meeting_by_group_id(db, group_id))
    if not meeting:
        raise HTTPException(status_code=404, detail="Không tìm thấy cuộc họp.")

    if meeting.meeting_status != "UPCOMING":
        raise HTTPException(status_code=400, detail="Chỉ được báo vắng trước khi cuộc họp bắt đầu.")

    row = mark_meeting_absent(db, meeting.id, str(current_user.id), reason=reason)
    set_meeting_presence(db, meeting.id, str(current_user.id), False)

    await _notify_meeting_users(
        _notify_user_ids_for_group(db, group_id),
        {
            "module": "meeting",
            "type": "meeting_absent_reported",
            "group_id": group_id,
            "meeting_id": meeting.id,
            "user_id": str(current_user.id),
            "user_name": get_display_name(current_user),
            "meeting_status": (meeting.meeting_status or "UPCOMING").upper(),
            "meeting_status_label": _meeting_status_label(meeting.meeting_status),
            "current_attendance_status": "ABSENT",
            "attendance_status_label": "Báo vắng",
            "action_mode": "absent_cancel",
            "can_absent": False,
            "can_cancel_absent": True,
            "can_checkin": False,
            "can_export_minutes": str(getattr(meeting, "secretary_user_id", "") or "") == str(current_user.id),
            "timestamp": datetime.utcnow().isoformat(),
        },
    )

    return _redirect(group_id, msg="Đã báo vắng cuộc họp.")


@router.post("/meetings/{group_id}/absent/cancel")
async def meeting_absent_cancel(
    group_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    _ensure_meeting_tables()

    current_user = login_required(request, db)
    if not _ensure_user_in_group(db, group_id, current_user.id):
        raise HTTPException(status_code=403, detail="Anh/chị không thuộc cuộc họp này.")

    meeting = _ensure_meeting_runtime_rules(db, get_meeting_by_group_id(db, group_id))
    if not meeting:
        raise HTTPException(status_code=404, detail="Không tìm thấy cuộc họp.")

    if (meeting.meeting_status or "").upper() != "UPCOMING":
        raise HTTPException(status_code=400, detail="Chỉ được hủy báo vắng trước khi cuộc họp bắt đầu.")

    attendance = _get_attendance_row_for_user(db, meeting.id, current_user.id)
    if not attendance or (attendance.attendance_status or "").upper() != "ABSENT":
        return _redirect(group_id, error="Anh/chị chưa báo vắng cuộc họp này.")

    attendance.attendance_status = "PENDING"
    attendance.presence_status = "OFFLINE"
    attendance.absent_reason = None
    attendance.updated_at = datetime.utcnow()
    db.add(attendance)
    db.commit()

    await _notify_meeting_users(
        _notify_user_ids_for_group(db, group_id),
        {
            "module": "meeting",
            "type": "meeting_absent_cancelled",
            "group_id": group_id,
            "meeting_id": meeting.id,
            "user_id": str(current_user.id),
            "user_name": get_display_name(current_user),
            "timestamp": datetime.utcnow().isoformat(),
        },
    )

    return _redirect(group_id, msg="Đã hủy báo vắng cuộc họp.")


@router.post("/meetings/{group_id}/checkin")
async def meeting_checkin(
    group_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    _ensure_meeting_tables()

    current_user = login_required(request, db)
    if not _ensure_user_in_group(db, group_id, current_user.id):
        raise HTTPException(status_code=403, detail="Anh/chị không thuộc cuộc họp này.")

    meeting = _ensure_meeting_runtime_rules(db, get_meeting_by_group_id(db, group_id))
    if not meeting:
        raise HTTPException(status_code=404, detail="Không tìm thấy cuộc họp.")

    if meeting.meeting_status != "LIVE":
        raise HTTPException(status_code=400, detail="Chỉ điểm danh khi cuộc họp đang diễn ra.")

    mark_meeting_checkin(db, meeting.id, str(current_user.id))
    set_meeting_presence(db, meeting.id, str(current_user.id), True)

    await _notify_meeting_users(
        _notify_user_ids_for_group(db, group_id),
        {
            "module": "meeting",
            "type": "meeting_checkin_done",
            "group_id": group_id,
            "meeting_id": meeting.id,
            "user_id": str(current_user.id),
            "user_name": get_display_name(current_user),
            "meeting_status": (meeting.meeting_status or "LIVE").upper(),
            "meeting_status_label": _meeting_status_label(meeting.meeting_status),
            "current_attendance_status": "CHECKED_IN",
            "attendance_status_label": "Đã điểm danh",
            "action_mode": "checked_in",
            "can_absent": False,
            "can_cancel_absent": False,
            "can_checkin": False,
            "can_export_minutes": str(getattr(meeting, "secretary_user_id", "") or "") == str(current_user.id),
            "timestamp": datetime.utcnow().isoformat(),
        },
    )

    return _redirect(group_id, msg="Đã điểm danh.")


@router.post("/meetings/{group_id}/documents/upload")
async def meeting_upload_document(
    group_id: str,
    request: Request,
    upload: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    _ensure_meeting_tables()

    current_user = login_required(request, db)
    meeting = get_meeting_by_group_id(db, group_id)
    if not meeting:
        raise HTTPException(status_code=404, detail="Không tìm thấy cuộc họp.")

    if not _ensure_user_in_group(db, group_id, current_user.id):
        raise HTTPException(status_code=403, detail="Anh/chị không thuộc cuộc họp này.")

    if not _can_manage_meeting(meeting, current_user.id):
        raise HTTPException(status_code=403, detail="Chỉ Chủ trì, Thư ký hoặc người thiết kế được tải tài liệu họp.")

    if not upload or not upload.filename:
        raise HTTPException(status_code=400, detail="Chưa chọn tệp để tải lên.")

    original_name = _safe_filename(upload.filename)
    ext = Path(original_name).suffix.lower()
    stored_name = f"{datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex}{ext}"

    abs_dir = _meeting_upload_dir(group_id)
    abs_path = os.path.join(abs_dir, stored_name)
    try:
        size_bytes, _sha256_value, original_name = save_upload_file_chunked(
            upload,
            abs_path,
            allowed_extensions=GENERAL_SAFE_UPLOAD_EXTENSIONS,
            max_size_mb=MEETING_MAX_UPLOAD_MB,
        )
    except UploadValidationError as ex:
        return _redirect(group_id, error=str(ex))

    message = create_message(
        db,
        group_id=group_id,
        sender_user_id=str(current_user.id),
        content=original_name,
        message_type="MEETING_DOC",
        reply_to_message_id=None,
    )

    rel_url = abs_path
    attachment = save_message_attachment(
        db,
        message_id=message.id,
        filename=original_name,
        stored_name=stored_name,
        path=rel_url,
        mime_type=getattr(upload, "content_type", None),
        size_bytes=size_bytes,
    )

    await _notify_meeting_users(
        _notify_user_ids_for_group(db, group_id),
        {
            "module": "meeting",
            "type": "meeting_document_uploaded",
            "group_id": group_id,
            "meeting_id": meeting.id,
            "attachment_id": attachment.id,
            "filename": original_name,
            "timestamp": datetime.utcnow().isoformat(),
        },
    )

    return _redirect(group_id, msg="Đã tải tài liệu họp.")


@router.post("/meetings/{group_id}/messages/send")
async def meeting_send_message(
    group_id: str,
    request: Request,
    content: str = Form(...),
    db: Session = Depends(get_db),
):
    _ensure_meeting_tables()

    current_user = login_required(request, db)
    meeting = _ensure_meeting_runtime_rules(db, get_meeting_by_group_id(db, group_id))
    if not meeting:
        raise HTTPException(status_code=404, detail="Không tìm thấy cuộc họp.")

    if not _ensure_user_in_group(db, group_id, current_user.id):
        raise HTTPException(status_code=403, detail="Anh/chị không thuộc cuộc họp này.")

    clean_content = (content or "").strip()
    if not clean_content:
        return _redirect(group_id, error="Nội dung tin nhắn không được để trống.")

    if not _can_user_send_meeting_message(db, meeting, current_user.id):
        return _redirect(group_id, error="Anh/chị chỉ được gửi nội dung phát biểu sau khi được Chủ trì cho phép.")

    message = create_message(
        db,
        group_id=group_id,
        sender_user_id=str(current_user.id),
        content=clean_content,
        message_type="TEXT",
        reply_to_message_id=None,
    )

    if str(meeting.host_user_id or "") != str(current_user.id):
        _consume_speaker_permission(db, meeting.id, current_user.id)

    payload = {
        "module": "meeting",
        "type": "new_message",
        "group_id": group_id,
        "meeting_id": meeting.id,
        "message": _message_vm(db, message, current_user.id),
        "can_send_meeting_message": _can_user_send_meeting_message(db, meeting, current_user.id),
        "timestamp": datetime.utcnow().isoformat(),
    }

    await _notify_meeting_users(_notify_user_ids_for_group(db, group_id), payload)

    return _redirect(group_id, msg="Đã gửi nội dung phát biểu.")


@router.post("/meetings/{group_id}/speaker/register")
async def meeting_speaker_register(
    group_id: str,
    request: Request,
    note: str = Form(""),
    db: Session = Depends(get_db),
):
    _ensure_meeting_tables()

    wants_json = (request.headers.get("x-requested-with") or "").lower() == "xmlhttprequest"

    def error_response(message: str):
        if wants_json:
            return JSONResponse({"ok": False, "detail": message}, status_code=400)
        return _redirect(group_id, error=message)

    current_user = login_required(request, db)
    meeting = _ensure_meeting_runtime_rules(db, get_meeting_by_group_id(db, group_id))
    if not meeting:
        raise HTTPException(status_code=404, detail="Không tìm thấy cuộc họp.")

    if not _ensure_user_in_group(db, group_id, current_user.id):
        raise HTTPException(status_code=403, detail="Anh/chị không thuộc cuộc họp này.")

    if _is_meeting_host(meeting, current_user.id):
        return error_response("Chủ trì không cần đăng ký phát biểu.")

    attendance = _get_attendance_row_for_user(db, meeting.id, current_user.id)
    attendance_status = (getattr(attendance, "attendance_status", "") or "PENDING").upper()

    if (meeting.meeting_status or "").upper() != "LIVE" or attendance_status != "CHECKED_IN":
        return error_response("Chỉ người đã điểm danh trong cuộc họp đang diễn ra mới được đăng ký phát biểu.")

    if _has_pending_speaker_request(db, meeting.id, current_user.id):
        return error_response("Anh/chị đã có đăng ký phát biểu đang chờ Chủ trì cho phép.")

    if _can_user_send_meeting_message(db, meeting, current_user.id):
        return error_response("Anh/chị đã được phép phát biểu.")

    clean_note = (note or "").strip() or "Tôi xin phát biểu."
    row = create_speaker_request(db, meeting.id, str(current_user.id), note=clean_note)

    payload = {
        "module": "meeting",
        "type": "meeting_speaker_registered",
        "group_id": group_id,
        "meeting_id": meeting.id,
        "speaker_request_id": row.id,
        "user_id": str(current_user.id),
        "user_name": get_display_name(current_user),
        "note": clean_note,
        "timestamp": datetime.utcnow().isoformat(),
    }

    await _notify_meeting_users(
        _notify_user_ids_for_group(db, group_id, meeting.host_user_id),
        payload,
    )

    if wants_json:
        return JSONResponse({"ok": True, **payload})

    return _redirect(group_id, msg="Đã gửi đăng ký phát biểu, chờ Chủ trì cho phép.")


@router.post("/meetings/{group_id}/leave/request")
async def meeting_leave_request(
    group_id: str,
    request: Request,
    note: str = Form(""),
    db: Session = Depends(get_db),
):
    _ensure_meeting_tables()

    current_user = login_required(request, db)
    meeting = _ensure_meeting_runtime_rules(db, get_meeting_by_group_id(db, group_id))
    if not meeting:
        raise HTTPException(status_code=404, detail="Không tìm thấy cuộc họp.")

    if not _ensure_user_in_group(db, group_id, current_user.id):
        raise HTTPException(status_code=403, detail="Anh/chị không thuộc cuộc họp này.")

    if _is_meeting_host(meeting, current_user.id):
        return _redirect(group_id, error="Chủ trì không thực hiện xin phép rời cuộc họp.")

    attendance = _get_attendance_row_for_user(db, meeting.id, current_user.id)
    attendance_status = (getattr(attendance, "attendance_status", "") or "PENDING").upper()

    if (meeting.meeting_status or "").upper() != "LIVE" or attendance_status != "CHECKED_IN":
        return _redirect(group_id, error="Chỉ người đã điểm danh trong cuộc họp đang diễn ra mới được xin phép rời cuộc họp.")

    if _has_pending_leave_request(db, meeting.id, current_user.id):
        return _redirect(group_id, error="Anh/chị đã có yêu cầu xin rời cuộc họp đang chờ Chủ trì cho phép.")

    row = ChatMeetingLeaveRequests(
        meeting_id=meeting.id,
        user_id=str(current_user.id),
        request_status="PENDING",
        note=(note or "").strip() or None,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    await _notify_meeting_users(
        _notify_user_ids_for_group(db, group_id, meeting.host_user_id),
        {
            "module": "meeting",
            "type": "meeting_leave_requested",
            "group_id": group_id,
            "meeting_id": meeting.id,
            "leave_request_id": row.id,
            "user_id": str(current_user.id),
            "user_name": get_display_name(current_user),
            "timestamp": datetime.utcnow().isoformat(),
        },
    )

    return _redirect(group_id, msg="Đã gửi yêu cầu xin phép rời cuộc họp, chờ Chủ trì cho phép.")


@router.post("/meetings/{group_id}/leave/{leave_request_id}/approve")
async def meeting_leave_approve(
    group_id: str,
    leave_request_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    _ensure_meeting_tables()

    current_user = login_required(request, db)
    meeting = get_meeting_by_group_id(db, group_id)
    if not meeting:
        raise HTTPException(status_code=404, detail="Không tìm thấy cuộc họp.")

    if not _is_meeting_host(meeting, current_user.id):
        raise HTTPException(status_code=403, detail="Chỉ Chủ trì mới được cho phép rời cuộc họp.")

    row = db.get(ChatMeetingLeaveRequests, leave_request_id)
    if not row or row.meeting_id != meeting.id:
        raise HTTPException(status_code=404, detail="Không tìm thấy yêu cầu xin rời cuộc họp.")

    row.request_status = "APPROVED"
    row.decided_by_user_id = str(current_user.id)
    row.decided_at = datetime.utcnow()
    row.updated_at = datetime.utcnow()
    db.add(row)

    attendance = _get_attendance_row_for_user(db, meeting.id, row.user_id)
    if attendance:
        attendance.attendance_status = "LEFT"
        attendance.presence_status = "OFFLINE"
        attendance.updated_at = datetime.utcnow()
        db.add(attendance)

    member_row = (
        db.query(ChatGroupMembers)
        .filter(ChatGroupMembers.group_id == group_id)
        .filter(ChatGroupMembers.user_id == str(row.user_id))
        .first()
    )
    if member_row:
        db.delete(member_row)

    db.commit()

    await _notify_meeting_users(
        _notify_user_ids_for_group(db, group_id, row.user_id),
        {
            "module": "meeting",
            "type": "meeting_leave_approved",
            "group_id": group_id,
            "meeting_id": meeting.id,
            "leave_request_id": row.id,
            "user_id": str(row.user_id),
            "timestamp": datetime.utcnow().isoformat(),
        },
    )

    return _redirect(group_id, msg="Đã cho phép thành viên rời cuộc họp.")


@router.post("/meetings/{group_id}/leave/{leave_request_id}/reject")
async def meeting_leave_reject(
    group_id: str,
    leave_request_id: str,
    request: Request,
    response_note: str = Form(""),
    db: Session = Depends(get_db),
):
    _ensure_meeting_tables()

    current_user = login_required(request, db)
    meeting = get_meeting_by_group_id(db, group_id)
    if not meeting:
        raise HTTPException(status_code=404, detail="Không tìm thấy cuộc họp.")

    if not _is_meeting_host(meeting, current_user.id):
        raise HTTPException(status_code=403, detail="Chỉ Chủ trì mới được xử lý yêu cầu xin rời cuộc họp.")

    row = db.get(ChatMeetingLeaveRequests, leave_request_id)
    if not row or row.meeting_id != meeting.id:
        raise HTTPException(status_code=404, detail="Không tìm thấy yêu cầu xin rời cuộc họp.")

    row.request_status = "REJECTED"
    row.response_note = (response_note or "").strip() or None
    row.decided_by_user_id = str(current_user.id)
    row.decided_at = datetime.utcnow()
    row.updated_at = datetime.utcnow()
    db.add(row)
    db.commit()

    await _notify_meeting_users(
        _notify_user_ids_for_group(db, group_id, row.user_id),
        {
            "module": "meeting",
            "type": "meeting_leave_rejected",
            "group_id": group_id,
            "meeting_id": meeting.id,
            "leave_request_id": row.id,
            "user_id": str(row.user_id),
            "timestamp": datetime.utcnow().isoformat(),
        },
    )

    return _redirect(group_id, msg="Đã không đồng ý yêu cầu rời cuộc họp.")


@router.post("/meetings/{group_id}/speaker/{speaker_request_id}/approve")
async def meeting_speaker_approve(
    group_id: str,
    speaker_request_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    _ensure_meeting_tables()

    current_user = login_required(request, db)
    meeting = get_meeting_by_group_id(db, group_id)
    if not meeting:
        raise HTTPException(status_code=404, detail="Không tìm thấy cuộc họp.")

    if str(meeting.host_user_id or "") != str(current_user.id):
        raise HTTPException(status_code=403, detail="Chỉ Chủ trì mới được cho phép phát biểu.")

    row = approve_speaker_request(db, speaker_request_id)
    if not row or row.meeting_id != meeting.id:
        raise HTTPException(status_code=404, detail="Không tìm thấy đăng ký phát biểu.")

    await _notify_meeting_users(
        _notify_user_ids_for_group(db, group_id, row.user_id),
        {
            "module": "meeting",
            "type": "meeting_speaker_approved",
            "group_id": group_id,
            "meeting_id": meeting.id,
            "speaker_request_id": row.id,
            "user_id": str(row.user_id),
            "timestamp": datetime.utcnow().isoformat(),
        },
    )

    return _redirect(group_id, msg="Đã cho phép phát biểu.")


@router.post("/meetings/{group_id}/conclusion/save")
async def meeting_save_conclusion(
    group_id: str,
    request: Request,
    conclusion_text: str = Form(""),
    db: Session = Depends(get_db),
):
    _ensure_meeting_tables()

    current_user = login_required(request, db)
    meeting = get_meeting_by_group_id(db, group_id)
    if not meeting:
        raise HTTPException(status_code=404, detail="Không tìm thấy cuộc họp.")

    if not _ensure_user_in_group(db, group_id, current_user.id):
        raise HTTPException(status_code=403, detail="Anh/chị không thuộc cuộc họp này.")

    if not _is_meeting_host(meeting, current_user.id):
        raise HTTPException(status_code=403, detail="Chỉ Chủ trì mới được cập nhật kết luận cuộc họp.")

    clean_text = (conclusion_text or "").strip()
    if not clean_text:
        return _redirect(group_id, error="Nội dung kết luận cuộc họp không được để trống.")

    now = datetime.utcnow()
    latest_message = _get_latest_meeting_conclusion_message(db, group_id)

    if latest_message:
        old_text = (latest_message.content or "").strip()
        if old_text and old_text != clean_text and (meeting.meeting_status or "").upper() == "ENDED":
            create_message(
                db,
                group_id=group_id,
                sender_user_id=str(current_user.id),
                content=old_text,
                message_type="MEETING_CONCLUSION_HISTORY",
                reply_to_message_id=latest_message.id,
            )

        latest_message.content = clean_text
        latest_message.updated_at = now
        db.add(latest_message)
        db.commit()
        db.refresh(latest_message)
        message = latest_message
    else:
        message = create_message(
            db,
            group_id=group_id,
            sender_user_id=str(current_user.id),
            content=clean_text,
            message_type="MEETING_CONCLUSION",
            reply_to_message_id=None,
        )

    await _notify_meeting_users(
        _notify_user_ids_for_group(db, group_id),
        {
            "module": "meeting",
            "type": "meeting_conclusion_saved",
            "group_id": group_id,
            "meeting_id": meeting.id,
            "message_id": message.id,
            "conclusion_text": clean_text,
            "conclusion_updated_text": _format_vn_dt(getattr(message, "updated_at", None) or getattr(message, "created_at", None)),
            "timestamp": now.isoformat(),
        },
    )

    return _redirect(group_id, msg="Đã lưu kết luận cuộc họp.")

@router.post("/meetings/{group_id}/speaker/{speaker_request_id}/move")
async def meeting_speaker_move(
    group_id: str,
    speaker_request_id: str,
    request: Request,
    direction: str = Form(...),
    db: Session = Depends(get_db),
):
    _ensure_meeting_tables()

    current_user = login_required(request, db)
    meeting = get_meeting_by_group_id(db, group_id)
    if not meeting:
        raise HTTPException(status_code=404, detail="Không tìm thấy cuộc họp.")

    if str(meeting.host_user_id or "") != str(current_user.id):
        raise HTTPException(status_code=403, detail="Chỉ Chủ trì mới được sắp xếp thứ tự phát biểu.")

    row = move_speaker_request(db, speaker_request_id, (direction or "").strip().lower())
    if not row or row.meeting_id != meeting.id:
        raise HTTPException(status_code=404, detail="Không tìm thấy đăng ký phát biểu.")

    return _redirect(group_id, msg="Đã cập nhật thứ tự phát biểu.")


@router.get("/meetings/{group_id}/minutes.txt")
def meeting_minutes_txt(
    group_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    _ensure_meeting_tables()

    current_user = login_required(request, db)
    meeting = get_meeting_by_group_id(db, group_id)
    if not meeting:
        raise HTTPException(status_code=404, detail="Không tìm thấy cuộc họp.")

    if not _ensure_user_in_group(db, group_id, current_user.id):
        raise HTTPException(status_code=403, detail="Anh/chị không thuộc cuộc họp này.")

    if str(getattr(meeting, "secretary_user_id", "") or "") != str(current_user.id):
        raise HTTPException(status_code=403, detail="Chỉ Thư ký cuộc họp mới được xuất biên bản.")

    detail = _selected_meeting_detail(db, group_id, current_user)
    if not detail:
        raise HTTPException(status_code=404, detail="Không tải được dữ liệu biên bản cuộc họp.")

    content = _build_meeting_minutes_text(detail)
    filename = f"bien_ban_hop_{group_id}.txt"

    return PlainTextResponse(
        content,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": _build_content_disposition("attachment", filename)},
    )


@router.get("/meetings/attachments/{attachment_id}/preview")
def meeting_attachment_preview(
    attachment_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    _ensure_meeting_tables()

    current_user = login_required(request, db)
    attachment = db.get(ChatAttachments, attachment_id)
    if not attachment or attachment.deleted_by_owner or attachment.recalled:
        raise HTTPException(status_code=404, detail="Không tìm thấy tệp.")

    message = db.get(ChatMessages, attachment.message_id)
    if not message:
        raise HTTPException(status_code=404, detail="Không tìm thấy tin nhắn chứa tệp.")

    group = get_group_by_id(db, message.group_id)
    if not group or (group.group_type or "").upper() != "MEETING":
        raise HTTPException(status_code=404, detail="Không tìm thấy cuộc họp.")

    if not _ensure_user_in_group(db, group.id, current_user.id):
        raise HTTPException(status_code=403, detail="Anh/chị không thuộc cuộc họp này.")

    file_path = _stored_attachment_path_to_abs_path(attachment.path or "")
    if not file_path or not os.path.isfile(file_path):
        raise HTTPException(status_code=404, detail="Tệp không còn tồn tại trên máy chủ.")

    filename = attachment.filename or "tep_dinh_kem"
    if _is_inline_viewable(filename, attachment.mime_type or ""):
        return FileResponse(
            file_path,
            media_type=_attachment_media_type(attachment),
            headers={"Content-Disposition": _build_content_disposition("inline", filename)},
        )

    if is_office_previewable(filename):
        try:
            preview_path = _ensure_meeting_office_pdf_preview_auto(
                source_path=file_path,
                preview_key=f"meeting_{attachment.id}",
                original_name=filename,
            )
            preview_filename = f"{Path(filename).stem}.pdf"
            return FileResponse(
                preview_path,
                media_type="application/pdf",
                headers={"Content-Disposition": _build_content_disposition("inline", preview_filename)},
            )
        except OfficePreviewError as ex:
            return HTMLResponse(
                f"""
                <html>
                  <head><meta charset="utf-8"></head>
                  <body style="font-family:Arial;padding:24px;">
                    <h3>Chưa xem trước được file Office</h3>
                    <p>{str(ex)}</p>
                    <p><a href="/meetings/attachments/{attachment.id}/download">Tải file về máy</a></p>
                  </body>
                </html>
                """,
                status_code=200,
            )

    return HTMLResponse(
        f"""
        <html>
          <head><meta charset="utf-8"></head>
          <body style="font-family:Arial;padding:24px;">
            <h3>Định dạng này không hỗ trợ xem trực tiếp</h3>
            <p><a href="/meetings/attachments/{attachment.id}/download">Tải file về máy</a></p>
          </body>
        </html>
        """,
        status_code=200,
    )


@router.get("/meetings/attachments/{attachment_id}/download")
def meeting_attachment_download(
    attachment_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    _ensure_meeting_tables()

    current_user = login_required(request, db)
    attachment = db.get(ChatAttachments, attachment_id)
    if not attachment or attachment.deleted_by_owner or attachment.recalled:
        raise HTTPException(status_code=404, detail="Không tìm thấy tệp.")

    message = db.get(ChatMessages, attachment.message_id)
    if not message:
        raise HTTPException(status_code=404, detail="Không tìm thấy tin nhắn chứa tệp.")

    group = get_group_by_id(db, message.group_id)
    if not group or (group.group_type or "").upper() != "MEETING":
        raise HTTPException(status_code=404, detail="Không tìm thấy cuộc họp.")

    if not _ensure_user_in_group(db, group.id, current_user.id):
        raise HTTPException(status_code=403, detail="Anh/chị không thuộc cuộc họp này.")

    file_path = _stored_attachment_path_to_abs_path(attachment.path or "")
    if not file_path or not os.path.isfile(file_path):
        raise HTTPException(status_code=404, detail="Tệp không còn tồn tại trên máy chủ.")

    return FileResponse(
        file_path,
        media_type=_attachment_media_type(attachment),
        filename=attachment.filename or "tep_dinh_kem",
    )