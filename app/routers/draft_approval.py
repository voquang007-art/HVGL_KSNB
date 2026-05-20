from __future__ import annotations

import logging
import mimetypes
import os
import shutil
import subprocess
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..chat.realtime import manager
from ..config import settings
from ..upload_security import (
    DRAFT_MAX_UPLOAD_MB,
    GENERAL_SAFE_UPLOAD_EXTENSIONS,
    UploadValidationError,
    save_upload_file_chunked,
)
from ..database import (
    BOARD_ROLES,
    HEAD_ROLES,
    KSNB_ROLES,
    ROLE_ADMIN,
    ROLE_LABELS,
    UNIT_HDTV,
    UNIT_KSNB,
    Base,
    engine,
    get_db,
)
from ..security.deps import login_required
from ..models import DocumentDraftActions, DocumentDraftFiles, DocumentDrafts, Users
from ..office_preview import (
    OfficePreviewError,
    ensure_office_pdf_preview,
    is_office_previewable,
)

logger = logging.getLogger("app.draft_approval")

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

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
_INLINE_MIME_EXACT = {
    "application/pdf",
}

STATUS_DRAFT = "DRAFT"
STATUS_RETURNED_FOR_EDIT = "RETURNED_FOR_EDIT"
STATUS_SUBMITTED_TO_KSNB_MANAGER = "SUBMITTED_TO_KSNB_MANAGER"
STATUS_SUBMITTED_TO_HDTV = "SUBMITTED_TO_HDTV"
STATUS_FINISHED = "FINISHED"
STATUS_APPROVED = "APPROVED"

ACTION_CREATE = "CREATE"
ACTION_UPLOAD_REPLACEMENT = "UPLOAD_REPLACEMENT"
ACTION_SUBMIT = "SUBMIT"
ACTION_APPROVE_FORWARD = "APPROVE_FORWARD"
ACTION_RETURN_FOR_EDIT = "RETURN_FOR_EDIT"
ACTION_RETURN_WITH_EDITED_FILE = "RETURN_WITH_EDITED_FILE"
ACTION_FINISHED = "FINISHED"
ACTION_HDTV_APPROVED = "HDTV_APPROVED"
ACTION_COORDINATE = "COORDINATE"
ACTION_COORDINATE_REPLY = "COORDINATE_REPLY"

FILE_ROLE_DRAFT_UPLOAD = "DRAFT_UPLOAD"
FILE_ROLE_REPLACEMENT = "DRAFT_REPLACEMENT"
FILE_ROLE_RETURNED_EDITED_FILE = "RETURNED_EDITED_FILE"

_STATUS_LABELS = {
    STATUS_DRAFT: "Nháp",
    STATUS_RETURNED_FOR_EDIT: "Trả lại để chỉnh sửa",
    STATUS_SUBMITTED_TO_KSNB_MANAGER: "Chờ Trưởng/Phó Ban KSNB xử lý",
    STATUS_SUBMITTED_TO_HDTV: "Chờ HĐTV phê duyệt",
    STATUS_FINISHED: "Đã kết thúc tại Ban KSNB",
    STATUS_APPROVED: "HĐTV đã phê duyệt",
}

_ACTION_LABELS = {
    ACTION_CREATE: "Tạo hồ sơ",
    ACTION_UPLOAD_REPLACEMENT: "Cập nhật tài liệu dự thảo",
    ACTION_SUBMIT: "Trình dự thảo",
    ACTION_APPROVE_FORWARD: "Đồng ý và trình cấp trên",
    ACTION_RETURN_FOR_EDIT: "Trả lại để người trình tự sửa",
    ACTION_RETURN_WITH_EDITED_FILE: "Trả lại kèm file đã sửa",
    ACTION_FINISHED: "Kết thúc luồng tại Ban KSNB",
    ACTION_HDTV_APPROVED: "HĐTV phê duyệt",
    ACTION_COORDINATE: "Gửi phối hợp",
    ACTION_COORDINATE_REPLY: "Phản hồi phối hợp",
}

_FILE_ROLE_LABELS = {
    FILE_ROLE_DRAFT_UPLOAD: "Tài liệu dự thảo",
    FILE_ROLE_REPLACEMENT: "Tài liệu cập nhật/thay thế",
    FILE_ROLE_RETURNED_EDITED_FILE: "Tài liệu trả lại đã sửa",
}


def _format_vn_dt(value: object) -> str:
    if not value:
        return "—"
    if isinstance(value, datetime):
        return value.strftime("%d/%m/%Y %H:%M")
    try:
        return str(value)
    except Exception:
        return "—"


templates.env.filters["format_vn_dt"] = _format_vn_dt


def _ensure_tables() -> None:
    Base.metadata.create_all(
        bind=engine,
        tables=[
            DocumentDrafts.__table__,
            DocumentDraftFiles.__table__,
            DocumentDraftActions.__table__,
        ],
        checkfirst=True,
    )


def _now() -> datetime:
    return datetime.utcnow()


def _project_root() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def _upload_root() -> str:
    path = os.path.join(_project_root(), "data", "draft_approvals")
    os.makedirs(path, exist_ok=True)
    return path

def _preview_root() -> str:
    path = os.path.join(_upload_root(), "_previews")
    os.makedirs(path, exist_ok=True)
    return path


def _candidate_libreoffice_paths() -> List[str]:
    candidates: List[str] = []

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

    clean: List[str] = []
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


def _ensure_office_pdf_preview_auto(source_path: str, preview_key: str, original_name: str) -> str:
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

        preview_path = Path(_preview_root()) / f"{preview_key}.pdf"
        try:
            if preview_path.is_file() and preview_path.stat().st_mtime >= source.stat().st_mtime:
                return str(preview_path)
        except OSError:
            pass

        with tempfile.TemporaryDirectory(prefix="lo_preview_") as tmp_dir:
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
                logger.warning(
                    "[draft_approval] LibreOffice preview failed. soffice=%s returncode=%s detail=%s",
                    soffice_path,
                    completed.returncode,
                    detail,
                )
                raise OfficePreviewError(
                    "LibreOffice đã được tìm thấy nhưng chưa tạo được bản xem trước PDF cho file Office. "
                    "Vui lòng kiểm tra quyền ghi thư mục data/draft_approvals/_previews hoặc thử lại với file khác."
                ) from first_error

            shutil.copyfile(str(pdf_files[0]), str(preview_path))
            return str(preview_path)


def _safe_filename(filename: str) -> str:
    clean_name = Path(filename or "tep_dinh_kem").name
    return clean_name.replace("..", "_").strip() or "tep_dinh_kem"


def _is_allowed_file(filename: str) -> bool:
    return Path(filename or "").suffix.lower() in _ALLOWED_EXTENSIONS


def _view_media_type(file_rec: DocumentDraftFiles) -> str:
    return file_rec.mime_type or mimetypes.guess_type(file_rec.file_name or "")[0] or "application/octet-stream"


def _is_inline_viewable(file_rec: DocumentDraftFiles) -> bool:
    media_type = _view_media_type(file_rec)
    if media_type in _INLINE_MIME_EXACT:
        return True
    return any(media_type.startswith(prefix) for prefix in _INLINE_MIME_PREFIXES)


def _draft_file_preview_url(file_rec: Optional[DocumentDraftFiles]) -> str:
    if not file_rec:
        return ""
    return f"/draft-approvals/files/{file_rec.id}/preview"


def _status_label(value: Optional[str]) -> str:
    return _STATUS_LABELS.get((value or "").strip(), value or "—")


def _action_label(value: Optional[str]) -> str:
    return _ACTION_LABELS.get((value or "").strip(), value or "—")


def _file_role_label(value: Optional[str]) -> str:
    return _FILE_ROLE_LABELS.get((value or "").strip(), value or "Tài liệu")


def _user_label(user: Optional[Users]) -> str:
    if not user:
        return ""
    return getattr(user, "full_name", None) or getattr(user, "username", None) or str(getattr(user, "id", "") or "")


def _role_code(user: Optional[Users]) -> str:
    return str(getattr(user, "role_code", "") or "").strip().upper()


def _role_codes_for_user(user: Optional[Users]) -> Set[str]:
    code = _role_code(user)
    return {code} if code else set()


def _is_admin(user: Optional[Users]) -> bool:
    return _role_code(user) == ROLE_ADMIN


def _is_ksnb(user: Optional[Users]) -> bool:
    return _role_code(user) in KSNB_ROLES


def _is_ksnb_staff(user: Optional[Users]) -> bool:
    return _role_code(user) in KSNB_ROLES


def _is_ksnb_manager(user: Optional[Users]) -> bool:
    return _role_code(user) in HEAD_ROLES


def _is_board(user: Optional[Users]) -> bool:
    return _role_code(user) in BOARD_ROLES


def _role_label(user: Optional[Users]) -> str:
    return ROLE_LABELS.get(_role_code(user), getattr(user, "position_title", "") or _role_code(user) or "Người dùng")


def _ensure_draft_module_allowed(user: Users) -> None:
    if _is_admin(user) or _is_ksnb(user) or _is_board(user):
        return
    raise HTTPException(status_code=403, detail="Tài khoản không có quyền truy cập phân hệ Phê duyệt dự thảo văn bản.")


def _active_file(db: Session, draft_id: str) -> Optional[DocumentDraftFiles]:
    return (
        db.query(DocumentDraftFiles)
        .filter(
            DocumentDraftFiles.draft_id == draft_id,
            DocumentDraftFiles.is_deleted.is_(False),
            DocumentDraftFiles.is_active.is_(True),
        )
        .order_by(DocumentDraftFiles.uploaded_at.desc())
        .first()
    )


def _load_user(db: Session, user_id: object) -> Optional[Users]:
    try:
        if user_id in (None, ""):
            return None
        return db.get(Users, int(user_id))
    except Exception:
        return None


def _active_users_by_role(db: Session, role_codes: Iterable[str], exclude_user_id: Optional[int] = None) -> List[Users]:
    clean_roles = [str(x or "").strip().upper() for x in role_codes if str(x or "").strip()]
    if not clean_roles:
        return []

    query = (
        db.query(Users)
        .filter(Users.is_active == 1)
        .filter(Users.role_code.in_(clean_roles))
    )

    if exclude_user_id is not None:
        query = query.filter(Users.id != int(exclude_user_id))

    return query.order_by(Users.full_name.asc(), Users.username.asc()).all()


def _find_ksnb_manager_users(db: Session, exclude_user_id: Optional[int] = None) -> List[Users]:
    return _active_users_by_role(db, HEAD_ROLES, exclude_user_id=exclude_user_id)


def _find_board_users(db: Session, exclude_user_id: Optional[int] = None) -> List[Users]:
    return _active_users_by_role(db, BOARD_ROLES, exclude_user_id=exclude_user_id)


def _find_ksnb_users(db: Session, exclude_user_id: Optional[int] = None) -> List[Users]:
    return _active_users_by_role(db, KSNB_ROLES, exclude_user_id=exclude_user_id)


def _submit_status_for_recipient(recipient: Users) -> str:
    if _is_ksnb_manager(recipient):
        return STATUS_SUBMITTED_TO_KSNB_MANAGER
    if _is_board(recipient):
        return STATUS_SUBMITTED_TO_HDTV
    return ""


def _display_candidate(user: Users, next_status: str) -> Dict[str, Any]:
    label_parts = [_user_label(user)]
    role_label = _role_label(user)
    if role_label:
        label_parts.append(role_label)

    if next_status == STATUS_SUBMITTED_TO_KSNB_MANAGER:
        label_parts.append("xử lý cấp Ban KSNB")
    elif next_status == STATUS_SUBMITTED_TO_HDTV:
        label_parts.append("HĐTV phê duyệt")

    return {
        "user": user,
        "next_status": next_status,
        "display_label": " - ".join([x for x in label_parts if x]),
    }


def _get_submit_candidates(db: Session, draft: DocumentDrafts, user: Users) -> List[Dict[str, Any]]:
    if not draft or not user:
        return []

    candidates: List[Dict[str, Any]] = []

    if _can_edit_draft(draft, user):
        for item in _find_ksnb_manager_users(db, exclude_user_id=user.id):
            candidates.append(_display_candidate(item, STATUS_SUBMITTED_TO_KSNB_MANAGER))
        return candidates

    if _is_ksnb_manager(user) and draft.current_handler_user_id == user.id and draft.current_status == STATUS_SUBMITTED_TO_KSNB_MANAGER:
        for item in _find_board_users(db, exclude_user_id=user.id):
            candidates.append(_display_candidate(item, STATUS_SUBMITTED_TO_HDTV))
        return candidates

    return candidates


def _get_coordination_candidates(db: Session, draft: DocumentDrafts, user: Users) -> List[Users]:
    if not draft or not user:
        return []

    if draft.current_handler_user_id != user.id:
        return []

    if _is_ksnb_manager(user):
        return _find_ksnb_users(db, exclude_user_id=user.id)

    if _is_board(user):
        return _find_board_users(db, exclude_user_id=user.id)

    return []


def _log_action(
    db: Session,
    draft: DocumentDrafts,
    action_type: str,
    from_user_id: Optional[int] = None,
    to_user_id: Optional[int] = None,
    from_unit_code: Optional[str] = None,
    to_unit_code: Optional[str] = None,
    comment: str = "",
    linked_file_id: Optional[str] = None,
    is_pending: bool = False,
    response_text: Optional[str] = None,
    responded_at: Optional[datetime] = None,
) -> DocumentDraftActions:
    action = DocumentDraftActions(
        draft_id=draft.id,
        action_type=action_type,
        from_user_id=from_user_id,
        to_user_id=to_user_id,
        from_unit_code=from_unit_code,
        to_unit_code=to_unit_code,
        comment=(comment or "").strip(),
        linked_file_id=linked_file_id,
        is_pending=bool(is_pending),
        response_text=response_text,
        responded_at=responded_at,
    )
    db.add(action)
    db.flush()
    return action


def _save_upload(upload: UploadFile, draft_id: str) -> Tuple[str, int, str]:
    original_name = _safe_filename(upload.filename or "tep_dinh_kem")
    ext = Path(original_name).suffix.lower()

    if ext not in _ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Định dạng tệp không được hỗ trợ.")

    folder = os.path.join(_upload_root(), draft_id)
    os.makedirs(folder, exist_ok=True)

    stored_name = f"{datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex}{ext}"
    dest = os.path.join(folder, stored_name)

    try:
        size, _sha256_value, _original_name = save_upload_file_chunked(
            upload,
            dest,
            allowed_extensions=_ALLOWED_EXTENSIONS,
            max_size_mb=DRAFT_MAX_UPLOAD_MB,
        )
    except UploadValidationError as ex:
        raise HTTPException(status_code=400, detail=str(ex)) from ex

    mime_type = upload.content_type or mimetypes.guess_type(original_name)[0] or "application/octet-stream"
    return dest, size, mime_type


def _deactivate_current_files(db: Session, draft_id: str) -> None:
    rows = (
        db.query(DocumentDraftFiles)
        .filter(
            DocumentDraftFiles.draft_id == draft_id,
            DocumentDraftFiles.is_deleted.is_(False),
            DocumentDraftFiles.is_active.is_(True),
        )
        .all()
    )
    for row in rows:
        row.is_active = False


def _add_file_record(
    db: Session,
    draft: DocumentDrafts,
    upload: UploadFile,
    uploaded_by_user_id: int,
    file_role: str,
    activate: bool = True,
) -> DocumentDraftFiles:
    original_name = _safe_filename(upload.filename or "tep_dinh_kem")
    path, size, mime_type = _save_upload(upload, draft.id)

    if activate:
        _deactivate_current_files(db, draft.id)

    file_rec = DocumentDraftFiles(
        draft_id=draft.id,
        uploaded_by_user_id=uploaded_by_user_id,
        file_role=file_role,
        file_name=original_name,
        stored_name=os.path.basename(path),
        file_path=path,
        mime_type=mime_type,
        size_bytes=size,
        is_active=bool(activate),
        is_deleted=False,
        uploaded_at=_now(),
    )
    db.add(file_rec)
    db.flush()

    if activate:
        draft.current_file_id = file_rec.id

    return file_rec


def _can_view_draft(db: Session, draft: DocumentDrafts, user: Users) -> bool:
    if not draft or not user:
        return False

    if _is_admin(user):
        return True

    user_id = int(user.id)

    if int(draft.created_by or 0) == user_id:
        return True

    if int(draft.current_handler_user_id or 0) == user_id:
        return True

    existed = (
        db.query(DocumentDraftActions.id)
        .filter(DocumentDraftActions.draft_id == draft.id)
        .filter(
            or_(
                DocumentDraftActions.from_user_id == user_id,
                DocumentDraftActions.to_user_id == user_id,
            )
        )
        .first()
    )
    if existed:
        return True

    return False


def _ensure_draft_access(db: Session, draft_id: str, user: Users) -> DocumentDrafts:
    draft = (
        db.query(DocumentDrafts)
        .filter(DocumentDrafts.id == draft_id)
        .filter(DocumentDrafts.is_deleted.is_(False))
        .first()
    )
    if not draft:
        raise HTTPException(status_code=404, detail="Không tìm thấy hồ sơ dự thảo.")

    if not _can_view_draft(db, draft, user):
        raise HTTPException(status_code=403, detail="Bạn không có quyền xem hồ sơ này.")

    return draft


def _can_edit_draft(draft: Optional[DocumentDrafts], user: Optional[Users]) -> bool:
    if not draft or not user:
        return False

    return (
        int(draft.created_by or 0) == int(user.id)
        and int(draft.current_handler_user_id or 0) == int(user.id)
        and draft.current_status in {STATUS_DRAFT, STATUS_RETURNED_FOR_EDIT}
        and _is_ksnb_staff(user)
    )


def _can_approve_forward(draft: Optional[DocumentDrafts], user: Optional[Users]) -> bool:
    if not draft or not user:
        return False

    if int(draft.current_handler_user_id or 0) != int(user.id):
        return False

    if _is_ksnb_manager(user) and draft.current_status == STATUS_SUBMITTED_TO_KSNB_MANAGER:
        return True

    if _is_board(user) and draft.current_status == STATUS_SUBMITTED_TO_HDTV:
        return True

    return False


def _can_finish_draft(draft: Optional[DocumentDrafts], user: Optional[Users]) -> bool:
    if not draft or not user:
        return False

    return (
        int(draft.current_handler_user_id or 0) == int(user.id)
        and _is_ksnb_manager(user)
        and draft.current_status == STATUS_SUBMITTED_TO_KSNB_MANAGER
    )


def _get_pending_coordination_for_user(db: Session, draft_id: str, user_id: int) -> List[DocumentDraftActions]:
    return (
        db.query(DocumentDraftActions)
        .filter(
            DocumentDraftActions.draft_id == draft_id,
            DocumentDraftActions.action_type == ACTION_COORDINATE,
            DocumentDraftActions.to_user_id == int(user_id),
            DocumentDraftActions.is_pending.is_(True),
        )
        .order_by(DocumentDraftActions.created_at.asc())
        .all()
    )


def _build_draft_row(db: Session, draft: DocumentDrafts) -> Dict[str, object]:
    active_file = _active_file(db, draft.id)
    creator = _load_user(db, draft.created_by)
    handler = _load_user(db, draft.current_handler_user_id)

    pending_coord_count = (
        db.query(DocumentDraftActions)
        .filter(
            DocumentDraftActions.draft_id == draft.id,
            DocumentDraftActions.action_type == ACTION_COORDINATE,
            DocumentDraftActions.is_pending.is_(True),
        )
        .count()
    )

    return {
        "obj": draft,
        "id": draft.id,
        "title": draft.title,
        "document_type": draft.document_type,
        "creator_name": _user_label(creator),
        "created_unit_name": "Ban Kiểm soát nội bộ",
        "handler_name": _user_label(handler),
        "status": draft.current_status,
        "status_label": _status_label(draft.current_status),
        "active_file": active_file,
        "pending_coord_count": pending_coord_count,
    }


def _load_visible_drafts(db: Session, user: Users, only_mode: str = "") -> List[Dict[str, object]]:
    rows = (
        db.query(DocumentDrafts)
        .filter(DocumentDrafts.is_deleted.is_(False))
        .order_by(DocumentDrafts.updated_at.desc())
        .all()
    )

    result: List[Dict[str, object]] = []
    user_id = int(user.id)

    for draft in rows:
        if not _can_view_draft(db, draft, user):
            continue

        if only_mode == "mine" and int(draft.created_by or 0) != user_id:
            continue

        if only_mode == "pending":
            is_current_handler = int(draft.current_handler_user_id or 0) == user_id
            has_pending_coordination = bool(_get_pending_coordination_for_user(db, draft.id, user_id))
            if not is_current_handler and not has_pending_coordination:
                continue

        if only_mode == "finished" and draft.current_status not in {STATUS_FINISHED, STATUS_APPROVED}:
            continue

        if only_mode not in {"", "mine", "pending", "finished"} and draft.current_status != only_mode:
            continue

        result.append(_build_draft_row(db, draft))

    return result


def _load_status_options() -> List[Tuple[str, str]]:
    return [
        ("", "Tất cả hồ sơ trong phạm vi"),
        ("pending", "Đang chờ tôi xử lý"),
        ("mine", "Hồ sơ tôi tạo"),
        (STATUS_DRAFT, "Nháp"),
        (STATUS_RETURNED_FOR_EDIT, "Trả lại để chỉnh sửa"),
        (STATUS_SUBMITTED_TO_KSNB_MANAGER, "Chờ Trưởng/Phó Ban KSNB xử lý"),
        (STATUS_SUBMITTED_TO_HDTV, "Chờ HĐTV phê duyệt"),
        ("finished", "Đã kết thúc / đã phê duyệt"),
    ]


def _recipient_unit_code(user: Optional[Users]) -> str:
    if not user:
        return ""
    if _role_code(user) in BOARD_ROLES:
        return UNIT_HDTV
    if _role_code(user) in KSNB_ROLES:
        return UNIT_KSNB
    return str(getattr(user, "unit_code", "") or "")


async def _notify_draft_users(user_ids: Iterable[object], payload: Dict[str, Any]) -> None:
    clean_ids: List[str] = []

    for raw_user_id in user_ids:
        uid = str(raw_user_id or "").strip()
        if uid and uid not in clean_ids:
            clean_ids.append(uid)

    if not clean_ids:
        return

    await manager.notify_users_json(clean_ids, payload)


def _redirect_selected(draft_id: str, msg: str = "", error: str = "") -> RedirectResponse:
    url = f"/draft-approvals?selected_id={quote(str(draft_id))}"
    if msg:
        url += f"&msg={quote(msg)}"
    if error:
        url += f"&error={quote(error)}"
    return RedirectResponse(url=url, status_code=303)


@router.get("/draft-approval")
def draft_approval_legacy_redirect() -> RedirectResponse:
    return RedirectResponse("/draft-approvals", status_code=303)


@router.get("/draft-approvals")
def draft_approval_page(
    request: Request,
    status: str = "",
    selected_id: str = "",
    db: Session = Depends(get_db),
):
    _ensure_tables()

    user = login_required(request, db)
    _ensure_draft_module_allowed(user)

    selected_status = (status or "").strip()
    drafts = _load_visible_drafts(db, user, selected_status)

    selected_draft: Optional[DocumentDrafts] = None
    if selected_id:
        selected_draft = (
            db.query(DocumentDrafts)
            .filter(DocumentDrafts.id == selected_id)
            .filter(DocumentDrafts.is_deleted.is_(False))
            .first()
        )
        if selected_draft and not _can_view_draft(db, selected_draft, user):
            selected_draft = None

    if not selected_draft and drafts:
        selected_draft = drafts[0]["obj"]

    detail = None
    if selected_draft:
        active_file = _active_file(db, selected_draft.id)

        files = (
            db.query(DocumentDraftFiles)
            .filter(
                DocumentDraftFiles.draft_id == selected_draft.id,
                DocumentDraftFiles.is_deleted.is_(False),
            )
            .order_by(DocumentDraftFiles.uploaded_at.desc())
            .all()
        )

        actions = (
            db.query(DocumentDraftActions)
            .filter(DocumentDraftActions.draft_id == selected_draft.id)
            .order_by(DocumentDraftActions.created_at.asc())
            .all()
        )

        pending_coord = _get_pending_coordination_for_user(db, selected_draft.id, int(user.id))

        if selected_draft.current_handler_user_id == user.id:
            submit_candidates = _get_submit_candidates(db, selected_draft, user)
            coord_candidates = _get_coordination_candidates(db, selected_draft, user)
        else:
            submit_candidates = []
            coord_candidates = []

        if active_file:
            setattr(active_file, "file_role_label", _file_role_label(getattr(active_file, "file_role", None)))
            setattr(active_file, "preview_url", _draft_file_preview_url(active_file))

        for file_row in files:
            setattr(file_row, "file_role_label", _file_role_label(getattr(file_row, "file_role", None)))
            setattr(file_row, "preview_url", _draft_file_preview_url(file_row))

        for action_row in actions:
            setattr(action_row, "action_label", _action_label(getattr(action_row, "action_type", None)))
            linked_file = getattr(action_row, "linked_file", None)
            if linked_file:
                setattr(linked_file, "file_role_label", _file_role_label(getattr(linked_file, "file_role", None)))
                setattr(linked_file, "preview_url", _draft_file_preview_url(linked_file))

        detail = {
            "draft": selected_draft,
            "active_file": active_file,
            "files": files,
            "actions": actions,
            "status_label": _status_label(selected_draft.current_status),
            "pending_coord": pending_coord,
            "coord_candidates": coord_candidates,
            "submit_candidates": submit_candidates,
            "can_edit": _can_edit_draft(selected_draft, user),
            "can_approve_forward": _can_approve_forward(selected_draft, user),
            "can_finish": _can_finish_draft(selected_draft, user),
            "is_ksnb_manager_handler": selected_draft.current_handler_user_id == user.id and _is_ksnb_manager(user),
            "is_hdtv_handler": selected_draft.current_handler_user_id == user.id and _is_board(user),
            "is_admin": _is_admin(user),
        }

    return templates.TemplateResponse(
        "draft_approval.html",
        {
            "request": request,
            "app_name": getattr(settings, "APP_NAME", "Cổng làm việc Ban Kiểm soát nội bộ"),
            "company_name": getattr(settings, "COMPANY_NAME", ""),
            "draft_rows": drafts,
            "selected_detail": detail,
            "status_options": _load_status_options(),
            "selected_status": selected_status,
            "selected_id": selected_draft.id if selected_draft else "",
            "me": user,
            "me_role_code": _role_code(user),
            "can_create_draft": _is_admin(user) or _is_ksnb(user),
        },
    )


@router.post("/draft-approvals/create")
async def create_draft(
    request: Request,
    title: str = Form(...),
    document_type: str = Form("Dự thảo văn bản"),
    summary: str = Form(""),
    upfile: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    _ensure_tables()

    user = login_required(request, db)
    _ensure_draft_module_allowed(user)

    if not (_is_admin(user) or _is_ksnb(user)):
        return RedirectResponse(
            url="/draft-approvals?error=Chỉ Ban KSNB được tạo hồ sơ dự thảo.",
            status_code=303,
        )

    clean_title = (title or "").strip()
    if not clean_title:
        return RedirectResponse(
            url="/draft-approvals?error=Vui lòng nhập trích yếu / tiêu đề hồ sơ.",
            status_code=303,
        )

    if not upfile or not upfile.filename or not _is_allowed_file(upfile.filename):
        return RedirectResponse(
            url="/draft-approvals?error=Vui lòng upload tài liệu dự thảo đúng định dạng được hỗ trợ.",
            status_code=303,
        )

    draft = DocumentDrafts(
        id=str(uuid.uuid4()),
        title=clean_title,
        document_type=(document_type or "Dự thảo văn bản").strip() or "Dự thảo văn bản",
        summary=(summary or "").strip(),
        created_by=int(user.id),
        created_unit_code=UNIT_KSNB,
        current_status=STATUS_DRAFT,
        current_handler_user_id=int(user.id),
        current_handler_unit_code=UNIT_KSNB,
        current_role_code=_role_code(user),
        last_submitter_id=None,
        current_file_id=None,
        is_deleted=False,
        created_at=_now(),
        updated_at=_now(),
    )
    db.add(draft)
    db.flush()

    file_rec = _add_file_record(
        db,
        draft,
        upfile,
        uploaded_by_user_id=int(user.id),
        file_role=FILE_ROLE_DRAFT_UPLOAD,
        activate=True,
    )

    _log_action(
        db,
        draft,
        action_type=ACTION_CREATE,
        from_user_id=int(user.id),
        to_user_id=int(user.id),
        from_unit_code=UNIT_KSNB,
        to_unit_code=UNIT_KSNB,
        comment="Tạo hồ sơ dự thảo văn bản.",
        linked_file_id=file_rec.id,
    )

    db.commit()

    return _redirect_selected(draft.id, msg="Đã tạo hồ sơ dự thảo.")


@router.post("/draft-approvals/{draft_id}/upload")
async def upload_replacement(
    draft_id: str,
    request: Request,
    comment: str = Form(""),
    upfile: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    _ensure_tables()

    user = login_required(request, db)
    _ensure_draft_module_allowed(user)

    draft = _ensure_draft_access(db, draft_id, user)
    if not _can_edit_draft(draft, user):
        return _redirect_selected(draft.id, error="Chỉ người tạo hồ sơ đang giữ hồ sơ mới được cập nhật tài liệu.")

    if not upfile or not upfile.filename or not _is_allowed_file(upfile.filename):
        return _redirect_selected(draft.id, error="Vui lòng upload tài liệu hợp lệ.")

    file_rec = _add_file_record(
        db,
        draft,
        upfile,
        uploaded_by_user_id=int(user.id),
        file_role=FILE_ROLE_REPLACEMENT,
        activate=True,
    )

    draft.updated_at = _now()

    _log_action(
        db,
        draft,
        action_type=ACTION_UPLOAD_REPLACEMENT,
        from_user_id=int(user.id),
        to_user_id=int(user.id),
        from_unit_code=UNIT_KSNB,
        to_unit_code=UNIT_KSNB,
        comment=(comment or "").strip() or "Cập nhật tài liệu dự thảo.",
        linked_file_id=file_rec.id,
    )

    db.commit()

    return _redirect_selected(draft.id, msg="Đã cập nhật tài liệu dự thảo.")


@router.post("/draft-approvals/{draft_id}/submit")
async def submit_draft(
    draft_id: str,
    request: Request,
    recipient_id: int = Form(...),
    comment: str = Form(""),
    db: Session = Depends(get_db),
):
    _ensure_tables()

    user = login_required(request, db)
    _ensure_draft_module_allowed(user)

    draft = _ensure_draft_access(db, draft_id, user)
    if not _can_edit_draft(draft, user):
        return _redirect_selected(draft.id, error="Hồ sơ không ở trạng thái được phép trình.")

    recipient = _load_user(db, recipient_id)
    if not recipient or not _is_ksnb_manager(recipient):
        return _redirect_selected(draft.id, error="Người nhận phải là Trưởng Ban hoặc Phó Trưởng Ban KSNB.")

    draft.current_status = STATUS_SUBMITTED_TO_KSNB_MANAGER
    draft.current_handler_user_id = int(recipient.id)
    draft.current_handler_unit_code = UNIT_KSNB
    draft.current_role_code = _role_code(recipient)
    draft.last_submitter_id = int(user.id)
    draft.submitted_at = _now()
    draft.updated_at = _now()

    _log_action(
        db,
        draft,
        action_type=ACTION_SUBMIT,
        from_user_id=int(user.id),
        to_user_id=int(recipient.id),
        from_unit_code=UNIT_KSNB,
        to_unit_code=UNIT_KSNB,
        comment=(comment or "").strip(),
    )

    db.commit()

    try:
        await _notify_draft_users(
            [recipient.id, user.id],
            {
                "module": "draft",
                "type": "draft_submitted",
                "draft_id": str(draft.id),
                "from_user_id": str(user.id),
                "to_user_id": str(recipient.id),
                "timestamp": datetime.utcnow().isoformat(),
            },
        )
    except Exception as ex:
        logger.exception("[draft_approval] Lỗi realtime sau trình dự thảo: %s", ex)

    return _redirect_selected(draft.id, msg="Đã trình dự thảo đến Trưởng/Phó Ban KSNB.")


@router.post("/draft-approvals/{draft_id}/approve")
async def approve_forward_or_final(
    draft_id: str,
    request: Request,
    recipient_id: str = Form(""),
    comment: str = Form(""),
    db: Session = Depends(get_db),
):
    _ensure_tables()

    user = login_required(request, db)
    _ensure_draft_module_allowed(user)

    draft = _ensure_draft_access(db, draft_id, user)
    if not _can_approve_forward(draft, user):
        return _redirect_selected(draft.id, error="Bạn không phải người đang xử lý hồ sơ hoặc hồ sơ không ở trạng thái phù hợp.")

    clean_comment = (comment or "").strip()

    if _is_ksnb_manager(user):
        if not recipient_id:
            return _redirect_selected(draft.id, error="Vui lòng chọn người nhận thuộc HĐTV để trình tiếp.")

        recipient = _load_user(db, recipient_id)
        if not recipient or not _is_board(recipient):
            return _redirect_selected(draft.id, error="Người nhận phải là Tổng Giám đốc, Phó Tổng Giám đốc thường trực hoặc Phó Tổng Giám đốc.")

        draft.current_status = STATUS_SUBMITTED_TO_HDTV
        draft.current_handler_user_id = int(recipient.id)
        draft.current_handler_unit_code = UNIT_HDTV
        draft.current_role_code = _role_code(recipient)
        draft.last_submitter_id = int(user.id)
        draft.updated_at = _now()

        _log_action(
            db,
            draft,
            action_type=ACTION_APPROVE_FORWARD,
            from_user_id=int(user.id),
            to_user_id=int(recipient.id),
            from_unit_code=UNIT_KSNB,
            to_unit_code=UNIT_HDTV,
            comment=clean_comment,
        )

        db.commit()

        try:
            await _notify_draft_users(
                [recipient.id, user.id, draft.created_by],
                {
                    "module": "draft",
                    "type": "draft_forwarded_to_board",
                    "draft_id": str(draft.id),
                    "from_user_id": str(user.id),
                    "to_user_id": str(recipient.id),
                    "timestamp": datetime.utcnow().isoformat(),
                },
            )
        except Exception as ex:
            logger.exception("[draft_approval] Lỗi realtime sau trình HĐTV: %s", ex)

        return _redirect_selected(draft.id, msg="Đã đồng ý và trình HĐTV phê duyệt.")

    if _is_board(user):
        draft.current_status = STATUS_APPROVED
        draft.current_handler_user_id = None
        draft.current_handler_unit_code = UNIT_HDTV
        draft.current_role_code = _role_code(user)
        draft.approved_at = _now()
        draft.finished_at = _now()
        draft.updated_at = _now()

        _log_action(
            db,
            draft,
            action_type=ACTION_HDTV_APPROVED,
            from_user_id=int(user.id),
            to_user_id=int(draft.created_by or 0) if draft.created_by else None,
            from_unit_code=UNIT_HDTV,
            to_unit_code=UNIT_KSNB,
            comment=clean_comment,
        )

        db.commit()

        try:
            await _notify_draft_users(
                [draft.created_by, draft.last_submitter_id, user.id],
                {
                    "module": "draft",
                    "type": "draft_approved",
                    "draft_id": str(draft.id),
                    "from_user_id": str(user.id),
                    "timestamp": datetime.utcnow().isoformat(),
                },
            )
        except Exception as ex:
            logger.exception("[draft_approval] Lỗi realtime sau HĐTV phê duyệt: %s", ex)

        return _redirect_selected(draft.id, msg="HĐTV đã phê duyệt dự thảo.")

    return _redirect_selected(draft.id, error="Vai trò hiện tại không được phép phê duyệt hồ sơ.")


@router.post("/draft-approvals/{draft_id}/finish")
async def finish_draft_at_ksnb_manager(
    draft_id: str,
    request: Request,
    comment: str = Form(""),
    db: Session = Depends(get_db),
):
    _ensure_tables()

    user = login_required(request, db)
    _ensure_draft_module_allowed(user)

    draft = _ensure_draft_access(db, draft_id, user)
    if not _can_finish_draft(draft, user):
        return _redirect_selected(draft.id, error="Chỉ Trưởng/Phó Ban KSNB đang xử lý hồ sơ mới được kết thúc luồng tại cấp Ban.")

    draft.current_status = STATUS_FINISHED
    draft.current_handler_user_id = None
    draft.current_handler_unit_code = UNIT_KSNB
    draft.current_role_code = _role_code(user)
    draft.finished_at = _now()
    draft.updated_at = _now()

    _log_action(
        db,
        draft,
        action_type=ACTION_FINISHED,
        from_user_id=int(user.id),
        to_user_id=int(draft.created_by or 0) if draft.created_by else None,
        from_unit_code=UNIT_KSNB,
        to_unit_code=UNIT_KSNB,
        comment=(comment or "").strip(),
    )

    db.commit()

    try:
        await _notify_draft_users(
            [draft.created_by, user.id],
            {
                "module": "draft",
                "type": "draft_finished",
                "draft_id": str(draft.id),
                "from_user_id": str(user.id),
                "timestamp": datetime.utcnow().isoformat(),
            },
        )
    except Exception as ex:
        logger.exception("[draft_approval] Lỗi realtime sau kết thúc luồng: %s", ex)

    return _redirect_selected(draft.id, msg="Đã kết thúc luồng tại Trưởng/Phó Ban KSNB.")


@router.post("/draft-approvals/{draft_id}/return")
async def return_draft_for_edit(
    draft_id: str,
    request: Request,
    comment: str = Form(...),
    db: Session = Depends(get_db),
):
    _ensure_tables()

    user = login_required(request, db)
    _ensure_draft_module_allowed(user)

    draft = _ensure_draft_access(db, draft_id, user)

    if int(draft.current_handler_user_id or 0) != int(user.id):
        return _redirect_selected(draft.id, error="Bạn không phải người đang xử lý hồ sơ.")

    if not (comment or "").strip():
        return _redirect_selected(draft.id, error="Phải nhập ý kiến sửa đổi, bổ sung khi trả lại.")

    return_user_id = int(draft.last_submitter_id or draft.created_by or 0)
    if not return_user_id:
        return _redirect_selected(draft.id, error="Không xác định được người nhận trả lại.")

    return_user = _load_user(db, return_user_id)
    draft.current_status = STATUS_RETURNED_FOR_EDIT
    draft.current_handler_user_id = return_user_id
    draft.current_handler_unit_code = _recipient_unit_code(return_user) or UNIT_KSNB
    draft.current_role_code = "RETURNED"
    draft.updated_at = _now()

    _log_action(
        db,
        draft,
        action_type=ACTION_RETURN_FOR_EDIT,
        from_user_id=int(user.id),
        to_user_id=return_user_id,
        from_unit_code=_recipient_unit_code(user),
        to_unit_code=draft.current_handler_unit_code,
        comment=(comment or "").strip(),
    )

    db.commit()

    try:
        await _notify_draft_users(
            [return_user_id, user.id],
            {
                "module": "draft",
                "type": "draft_returned",
                "draft_id": str(draft.id),
                "from_user_id": str(user.id),
                "to_user_id": str(return_user_id),
                "timestamp": datetime.utcnow().isoformat(),
            },
        )
    except Exception as ex:
        logger.exception("[draft_approval] Lỗi realtime sau trả lại hồ sơ: %s", ex)

    return _redirect_selected(draft.id, msg="Đã trả lại hồ sơ để chỉnh sửa.")


@router.post("/draft-approvals/{draft_id}/return-edited")
async def return_with_edited_file(
    draft_id: str,
    request: Request,
    comment: str = Form(...),
    upfile: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    _ensure_tables()

    user = login_required(request, db)
    _ensure_draft_module_allowed(user)

    draft = _ensure_draft_access(db, draft_id, user)

    if int(draft.current_handler_user_id or 0) != int(user.id):
        return _redirect_selected(draft.id, error="Bạn không phải người đang xử lý hồ sơ.")

    if not (comment or "").strip():
        return _redirect_selected(draft.id, error="Phải nhập ý kiến sửa đổi, bổ sung khi trả lại.")

    if not upfile or not upfile.filename or not _is_allowed_file(upfile.filename):
        return _redirect_selected(draft.id, error="Phải upload file đã sửa hợp lệ khi trả lại theo luồng này.")

    return_user_id = int(draft.last_submitter_id or draft.created_by or 0)
    if not return_user_id:
        return _redirect_selected(draft.id, error="Không xác định được người nhận trả lại.")

    return_user = _load_user(db, return_user_id)

    returned_file = _add_file_record(
        db,
        draft,
        upfile,
        uploaded_by_user_id=int(user.id),
        file_role=FILE_ROLE_RETURNED_EDITED_FILE,
        activate=True,
    )

    draft.current_status = STATUS_RETURNED_FOR_EDIT
    draft.current_handler_user_id = return_user_id
    draft.current_handler_unit_code = _recipient_unit_code(return_user) or UNIT_KSNB
    draft.current_role_code = "RETURNED"
    draft.updated_at = _now()

    _log_action(
        db,
        draft,
        action_type=ACTION_RETURN_WITH_EDITED_FILE,
        from_user_id=int(user.id),
        to_user_id=return_user_id,
        from_unit_code=_recipient_unit_code(user),
        to_unit_code=draft.current_handler_unit_code,
        comment=(comment or "").strip(),
        linked_file_id=returned_file.id,
    )

    db.commit()

    try:
        await _notify_draft_users(
            [return_user_id, user.id],
            {
                "module": "draft",
                "type": "draft_returned",
                "draft_id": str(draft.id),
                "from_user_id": str(user.id),
                "to_user_id": str(return_user_id),
                "timestamp": datetime.utcnow().isoformat(),
            },
        )
    except Exception as ex:
        logger.exception("[draft_approval] Lỗi realtime sau trả lại hồ sơ kèm file sửa: %s", ex)

    return _redirect_selected(draft.id, msg="Đã trả lại hồ sơ kèm file đã sửa.")


@router.post("/draft-approvals/{draft_id}/coordinate")
async def send_for_coordination(
    draft_id: str,
    request: Request,
    recipient_ids: List[int] = Form([]),
    comment: str = Form(""),
    db: Session = Depends(get_db),
):
    _ensure_tables()

    user = login_required(request, db)
    _ensure_draft_module_allowed(user)

    draft = _ensure_draft_access(db, draft_id, user)

    if int(draft.current_handler_user_id or 0) != int(user.id):
        return _redirect_selected(draft.id, error="Chỉ người đang xử lý chính mới được gửi phối hợp.")

    allowed_candidates = _get_coordination_candidates(db, draft, user)
    allowed_ids = {int(item.id) for item in allowed_candidates}

    clean_recipient_ids: List[int] = []
    for rid in recipient_ids or []:
        try:
            rid_int = int(rid)
        except Exception:
            continue

        if rid_int in allowed_ids and rid_int not in clean_recipient_ids:
            clean_recipient_ids.append(rid_int)

    if not clean_recipient_ids:
        return _redirect_selected(draft.id, error="Chưa chọn người nhận phối hợp hợp lệ.")

    for rid in clean_recipient_ids:
        target = _load_user(db, rid)
        _log_action(
            db,
            draft,
            action_type=ACTION_COORDINATE,
            from_user_id=int(user.id),
            to_user_id=rid,
            from_unit_code=_recipient_unit_code(user),
            to_unit_code=_recipient_unit_code(target),
            comment=(comment or "").strip(),
            is_pending=True,
        )

    draft.updated_at = _now()
    db.commit()

    try:
        await _notify_draft_users(
            clean_recipient_ids + [int(user.id)],
            {
                "module": "draft",
                "type": "draft_coordination_requested",
                "draft_id": str(draft.id),
                "from_user_id": str(user.id),
                "to_user_ids": [str(x) for x in clean_recipient_ids],
                "timestamp": datetime.utcnow().isoformat(),
            },
        )
    except Exception as ex:
        logger.exception("[draft_approval] Lỗi realtime sau gửi phối hợp: %s", ex)

    return _redirect_selected(draft.id, msg="Đã gửi yêu cầu phối hợp.")


@router.post("/draft-approvals/{draft_id}/coordinate-reply/{action_id}")
async def coordination_reply(
    draft_id: str,
    action_id: str,
    request: Request,
    response_text: str = Form(...),
    db: Session = Depends(get_db),
):
    _ensure_tables()

    user = login_required(request, db)
    _ensure_draft_module_allowed(user)

    draft = _ensure_draft_access(db, draft_id, user)

    action = (
        db.query(DocumentDraftActions)
        .filter(
            DocumentDraftActions.id == action_id,
            DocumentDraftActions.draft_id == draft.id,
            DocumentDraftActions.action_type == ACTION_COORDINATE,
            DocumentDraftActions.to_user_id == int(user.id),
            DocumentDraftActions.is_pending.is_(True),
        )
        .first()
    )

    if not action:
        return _redirect_selected(draft.id, error="Không tìm thấy yêu cầu phối hợp đang chờ phản hồi.")

    if not (response_text or "").strip():
        return _redirect_selected(draft.id, error="Vui lòng nhập nội dung phản hồi phối hợp.")

    action.is_pending = False
    action.response_text = (response_text or "").strip()
    action.responded_at = _now()

    _log_action(
        db,
        draft,
        action_type=ACTION_COORDINATE_REPLY,
        from_user_id=int(user.id),
        to_user_id=action.from_user_id,
        from_unit_code=_recipient_unit_code(user),
        to_unit_code=action.from_unit_code,
        comment=(response_text or "").strip(),
        is_pending=False,
    )

    draft.updated_at = _now()
    db.commit()

    try:
        await _notify_draft_users(
            [action.from_user_id, user.id],
            {
                "module": "draft",
                "type": "draft_coordination_replied",
                "draft_id": str(draft.id),
                "from_user_id": str(user.id),
                "to_user_id": str(action.from_user_id or ""),
                "timestamp": datetime.utcnow().isoformat(),
            },
        )
    except Exception as ex:
        logger.exception("[draft_approval] Lỗi realtime sau phản hồi phối hợp: %s", ex)

    return _redirect_selected(draft.id, msg="Đã gửi phản hồi phối hợp.")


@router.get("/draft-approvals/files/{file_id}/download")
def download_draft_file(
    file_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    _ensure_tables()

    user = login_required(request, db)
    _ensure_draft_module_allowed(user)

    file_rec = (
        db.query(DocumentDraftFiles)
        .filter(
            DocumentDraftFiles.id == file_id,
            DocumentDraftFiles.is_deleted.is_(False),
        )
        .first()
    )

    if not file_rec:
        raise HTTPException(status_code=404, detail="Không tìm thấy tệp.")

    draft = _ensure_draft_access(db, file_rec.draft_id, user)

    if not os.path.isfile(file_rec.file_path or ""):
        raise HTTPException(status_code=404, detail="Tệp không còn tồn tại trên máy chủ.")

    return FileResponse(
        file_rec.file_path,
        media_type=_view_media_type(file_rec),
        filename=file_rec.file_name or "tep_dinh_kem",
    )


@router.get("/draft-approvals/files/{file_id}/preview")
def preview_draft_file(
    file_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    _ensure_tables()

    user = login_required(request, db)
    _ensure_draft_module_allowed(user)

    file_rec = (
        db.query(DocumentDraftFiles)
        .filter(
            DocumentDraftFiles.id == file_id,
            DocumentDraftFiles.is_deleted.is_(False),
        )
        .first()
    )

    if not file_rec:
        raise HTTPException(status_code=404, detail="Không tìm thấy tệp.")

    draft = _ensure_draft_access(db, file_rec.draft_id, user)

    if not os.path.isfile(file_rec.file_path or ""):
        raise HTTPException(status_code=404, detail="Tệp không còn tồn tại trên máy chủ.")

    if _is_inline_viewable(file_rec):
        return FileResponse(
            file_rec.file_path,
            media_type=_view_media_type(file_rec),
            filename=file_rec.file_name or "tep_dinh_kem",
            headers={
                "Content-Disposition": f'inline; filename="{quote(file_rec.file_name or "tep_dinh_kem")}"'
            },
        )

    if is_office_previewable(file_rec.file_name or ""):
        try:
            preview_path = _ensure_office_pdf_preview_auto(
                source_path=file_rec.file_path,
                preview_key=f"draft_{file_rec.id}",
                original_name=file_rec.file_name or "tep_dinh_kem",
            )
            return FileResponse(
                preview_path,
                media_type="application/pdf",
                filename=f"{Path(file_rec.file_name or 'preview').stem}.pdf",
                headers={
                    "Content-Disposition": f'inline; filename="{quote(Path(file_rec.file_name or "preview").stem)}.pdf"'
                },
            )
        except OfficePreviewError as ex:
            return HTMLResponse(
                f"""
                <html>
                  <head><meta charset="utf-8"></head>
                  <body style="font-family:Arial;padding:24px;">
                    <h3>Chưa xem trước được file Office</h3>
                    <p>{str(ex)}</p>
                    <p><a href="/draft-approvals/files/{file_rec.id}/download">Tải file về máy</a></p>
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
            <p><a href="/draft-approvals/files/{file_rec.id}/download">Tải file về máy</a></p>
          </body>
        </html>
        """,
        status_code=200,
    )