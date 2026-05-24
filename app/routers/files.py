# -*- coding: utf-8 -*-
from __future__ import annotations

import mimetypes
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ..database import (
    KSNB_ROLES,
    ROLE_ADMIN,
    STORAGE_DIR,
    UNIT_KSNB,
    get_conn,
)

from ..upload_security import (
    DOCUMENT_LIBRARY_EXTENSIONS,
    DOCUMENT_MAX_UPLOAD_MB,
    UploadValidationError,
    save_upload_file_chunked,
)
from ..path_security import ensure_safe_file_path
templates = Jinja2Templates(directory="app/templates")
router = APIRouter()

VN_TZ = timezone(timedelta(hours=7))

FILES_DIR = STORAGE_DIR / "documents"

ALLOWED_EXTENSIONS = DOCUMENT_LIBRARY_EXTENSIONS

DOCUMENT_EXTENSIONS = {".doc", ".docx"}
SPREADSHEET_EXTENSIONS = {".xls", ".xlsx"}
PDF_EXTENSIONS = {".pdf"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".wmv"}

SORT_FIELDS = {"name", "type", "owner", "uploaded_at", "size"}


def require_login(request: Request):
    if not request.state.user:
        return RedirectResponse("/login", status_code=303)
    return None


def can_access_files_module(user: dict) -> bool:
    if not user:
        return False
    if user.get("role_code") == ROLE_ADMIN:
        return True
    return user.get("role_code") in KSNB_ROLES and user.get("unit_code") == UNIT_KSNB


def can_upload_files(user: dict) -> bool:
    return can_access_files_module(user)


def can_delete_files(user: dict) -> bool:
    if not user:
        return False
    if user.get("role_code") == ROLE_ADMIN:
        return True
    return user.get("role_code") in {
        "TRUONG_BAN_KSNB",
        "PHO_TRUONG_BAN_KSNB",
    } and user.get("unit_code") == UNIT_KSNB


def ensure_files_table() -> None:
    FILES_DIR.mkdir(parents=True, exist_ok=True)
    with get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS document_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                original_name TEXT NOT NULL,
                stored_name TEXT NOT NULL,
                path TEXT NOT NULL,
                mime_type TEXT,
                size_bytes INTEGER NOT NULL DEFAULT 0,
                sha256 TEXT,
                uploaded_by INTEGER NOT NULL,
                uploaded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                is_deleted INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY(uploaded_by) REFERENCES users(id)
            )
            """
        )
        conn.commit()


def file_ext(filename: str) -> str:
    return os.path.splitext(filename or "")[1].lower().strip()


def is_allowed_file(filename: str) -> bool:
    return file_ext(filename) in ALLOWED_EXTENSIONS


def file_kind(filename: str) -> str:
    ext = file_ext(filename)
    if ext in DOCUMENT_EXTENSIONS:
        return "document"
    if ext in SPREADSHEET_EXTENSIONS:
        return "spreadsheet"
    if ext in PDF_EXTENSIONS:
        return "pdf"
    if ext in IMAGE_EXTENSIONS:
        return "image"
    if ext in VIDEO_EXTENSIONS:
        return "video"
    return "other"


def file_kind_label(filename: str) -> str:
    mapping = {
        "document": "Word",
        "spreadsheet": "Excel",
        "pdf": "PDF",
        "image": "Ảnh",
        "video": "Video",
        "other": "Khác",
    }
    return mapping.get(file_kind(filename), "Khác")


def can_preview_inline(filename: str) -> bool:
    return file_kind(filename) in {"pdf", "image", "video"}


def guess_mime(path: str) -> str:
    mime_type, _ = mimetypes.guess_type(path)
    return mime_type or "application/octet-stream"


def build_content_disposition(disposition: str, filename: str) -> str:
    raw_name = (filename or "file").strip() or "file"
    ascii_fallback = raw_name.encode("ascii", "ignore").decode("ascii").strip() or "file"
    ascii_fallback = ascii_fallback.replace("\\", "_").replace('"', "_")
    encoded_name = quote(raw_name, safe="")
    return f'{disposition}; filename="{ascii_fallback}"; filename*=UTF-8\'\'{encoded_name}'


def format_size(size_bytes: int | None) -> str:
    size = int(size_bytes or 0)
    units = ["B", "KB", "MB", "GB", "TB"]
    idx = 0
    value = float(size)
    while value >= 1024 and idx < len(units) - 1:
        value = value / 1024.0
        idx += 1
    if idx == 0:
        return f"{int(value)} {units[idx]}"
    return f"{value:.1f} {units[idx]}"


def to_vietnam_datetime(value: str | None) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(VN_TZ)


def safe_unique_path(original_name: str) -> tuple[str, Path]:
    FILES_DIR.mkdir(parents=True, exist_ok=True)
    raw_name = Path(original_name or "file").name
    stem = Path(raw_name).stem or "file"
    ext = file_ext(raw_name)
    timestamp = datetime.now(VN_TZ).strftime("%Y%m%d_%H%M%S")
    stored_name = f"{stem}_{timestamp}{ext}"
    path = FILES_DIR / stored_name
    counter = 1
    while path.exists():
        stored_name = f"{stem}_{timestamp}_{counter}{ext}"
        path = FILES_DIR / stored_name
        counter += 1
    return stored_name, path


def save_upload_file(upload: UploadFile) -> tuple[str, Path, int, str]:
    stored_name, path = safe_unique_path(upload.filename or "file")
    try:
        size, sha256_value, _original_name = save_upload_file_chunked(
            upload,
            path,
            allowed_extensions=ALLOWED_EXTENSIONS,
            max_size_mb=DOCUMENT_MAX_UPLOAD_MB,
        )
    except UploadValidationError as ex:
        raise HTTPException(status_code=400, detail=str(ex)) from ex
    return stored_name, path, size, sha256_value


def parse_positive_int(raw: str | None, default: int, min_value: int = 1, max_value: int = 100) -> int:
    try:
        value = int(raw or default)
    except ValueError:
        value = default
    if value < min_value:
        value = min_value
    if value > max_value:
        value = max_value
    return value


def serialize_file(row: dict, user: dict) -> dict:
    uploaded_at_vn = to_vietnam_datetime(row.get("uploaded_at"))
    return {
        "id": row["id"],
        "original_name": row["original_name"],
        "file_ext": file_ext(row["original_name"]).replace(".", "").upper(),
        "file_kind": file_kind(row["original_name"]),
        "file_kind_label": file_kind_label(row["original_name"]),
        "mime_type": row.get("mime_type") or "",
        "size_bytes": row.get("size_bytes") or 0,
        "size_display": format_size(row.get("size_bytes")),
        "uploaded_at": uploaded_at_vn,
        "uploaded_at_display": uploaded_at_vn.strftime("%d/%m/%Y %H:%M") if uploaded_at_vn else "",
        "owner_name": row.get("owner_name") or "",
        "scope_label": "Kho KSNB",
        "scope_name": "Ban Kiểm soát nội bộ",
        "can_preview_inline": can_preview_inline(row["original_name"]),
        "preview_url": f"/documents/view/{row['id']}",
        "preview_kind": file_kind(row["original_name"]),
        "can_delete": can_delete_files(user),
    }


def sort_rows(rows: list[dict], sort: str, direction: str) -> list[dict]:
    reverse = direction == "desc"
    if sort == "name":
        return sorted(rows, key=lambda x: (x["original_name"] or "").lower(), reverse=reverse)
    if sort == "type":
        return sorted(rows, key=lambda x: (x["file_kind_label"] or "").lower(), reverse=reverse)
    if sort == "owner":
        return sorted(rows, key=lambda x: (x["owner_name"] or "").lower(), reverse=reverse)
    if sort == "size":
        return sorted(rows, key=lambda x: int(x["size_bytes"] or 0), reverse=reverse)
    return sorted(rows, key=lambda x: x["uploaded_at"] or datetime.min.replace(tzinfo=VN_TZ), reverse=reverse)


def paginate_rows(rows: list[dict], page: int, per_page: int) -> tuple[list[dict], int, int]:
    total = len(rows)
    total_pages = max(1, (total + per_page - 1) // per_page)
    if page > total_pages:
        page = total_pages
    start = (page - 1) * per_page
    end = start + per_page
    return rows[start:end], total, total_pages


def get_file_record(file_id: int) -> dict | None:
    ensure_files_table()
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT f.*, u.full_name AS owner_name
            FROM document_files f
            JOIN users u ON u.id = f.uploaded_by
            WHERE f.id = ? AND f.is_deleted = 0
            """,
            (file_id,),
        ).fetchone()
    return dict(row) if row else None


@router.get("/documents")
@router.get("/files")
def files_home(request: Request):
    denied = require_login(request)
    if denied:
        return denied

    user = request.state.user
    if not can_access_files_module(user):
        return RedirectResponse("/vouchers", status_code=303)

    ensure_files_table()

    keyword = str(request.query_params.get("q") or "").strip()
    kind = str(request.query_params.get("kind") or "all").strip().lower()
    sort = str(request.query_params.get("sort") or "uploaded_at").strip().lower()
    direction = str(request.query_params.get("direction") or "desc").strip().lower()
    page = parse_positive_int(request.query_params.get("page"), default=1)
    per_page = parse_positive_int(request.query_params.get("per_page"), default=10, min_value=5, max_value=100)

    if sort not in SORT_FIELDS:
        sort = "uploaded_at"
    if direction not in {"asc", "desc"}:
        direction = "desc"
    if kind not in {"all", "document", "spreadsheet", "pdf", "image", "video"}:
        kind = "all"

    with get_conn() as conn:
        query = """
            SELECT f.*, u.full_name AS owner_name
            FROM document_files f
            JOIN users u ON u.id = f.uploaded_by
            WHERE f.is_deleted = 0
        """
        params: list = []
        if keyword:
            query += " AND lower(f.original_name) LIKE ?"
            params.append(f"%{keyword.lower()}%")
        query += " ORDER BY f.uploaded_at DESC"
        rows_raw = conn.execute(query, params).fetchall()

    rows = [serialize_file(dict(row), user) for row in rows_raw]
    if kind != "all":
        rows = [row for row in rows if row["file_kind"] == kind]

    rows = sort_rows(rows, sort=sort, direction=direction)
    paged_rows, total_files, total_pages = paginate_rows(rows, page=page, per_page=per_page)

    return templates.TemplateResponse(
        "files.html",
        {
            "request": request,
            "rows": paged_rows,
            "msg": request.query_params.get("msg", ""),
            "error": request.query_params.get("error", ""),
            "q": keyword,
            "kind": kind,
            "sort": sort,
            "direction": direction,
            "page": page,
            "per_page": per_page,
            "total_files": total_files,
            "total_pages": total_pages,
            "accept_types": ",".join(sorted(ALLOWED_EXTENSIONS)),
            "can_upload_files": can_upload_files(user),
            "can_delete_files": can_delete_files(user),
        },
    )


@router.post("/files/upload")
async def upload_file(request: Request, upfile: UploadFile = File(...)):
    denied = require_login(request)
    if denied:
        return denied

    user = request.state.user
    if not can_upload_files(user):
        return RedirectResponse("/documents?error=Bạn không có quyền tải tài liệu lên Kho tài liệu KSNB.", status_code=303)

    if not upfile or not upfile.filename:
        return RedirectResponse("/documents?error=Chưa chọn tệp để tải lên.", status_code=303)

    if not is_allowed_file(upfile.filename):
        return RedirectResponse("/documents?error=Định dạng tệp không được hỗ trợ.", status_code=303)

    ensure_files_table()
    stored_name, path, size, sha256_value = save_upload_file(upfile)
    mime_type = guess_mime(str(path))

    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO document_files(
                original_name, stored_name, path, mime_type, size_bytes, sha256, uploaded_by
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                Path(upfile.filename).name,
                stored_name,
                str(path),
                mime_type,
                size,
                sha256_value,
                user["id"],
            ),
        )
        conn.commit()

    return RedirectResponse("/documents?msg=Tải tệp lên thành công.", status_code=303)


@router.get("/files/view/{file_id}")
def view_file(request: Request, file_id: int):
    denied = require_login(request)
    if denied:
        return denied

    user = request.state.user
    if not can_access_files_module(user):
        raise HTTPException(status_code=403, detail="Bạn không có quyền truy cập Kho tài liệu KSNB.")

    rec = get_file_record(file_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Không tìm thấy tệp.")

    path = ensure_safe_file_path(rec.get("path") or "")

    filename = rec.get("original_name") or os.path.basename(path)
    return FileResponse(
        path,
        media_type=rec.get("mime_type") or guess_mime(path),
        headers={"Content-Disposition": build_content_disposition("inline", filename)},
    )


@router.get("/files/download/{file_id}")
def download_file(request: Request, file_id: int):
    denied = require_login(request)
    if denied:
        return denied

    user = request.state.user
    if not can_access_files_module(user):
        raise HTTPException(status_code=403, detail="Bạn không có quyền tải tệp trong Kho tài liệu KSNB.")

    rec = get_file_record(file_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Không tìm thấy tệp.")

    path = ensure_safe_file_path(rec.get("path") or "")

    filename = rec.get("original_name") or os.path.basename(path)
    return FileResponse(
        path,
        media_type=rec.get("mime_type") or guess_mime(path),
        headers={"Content-Disposition": build_content_disposition("attachment", filename)},
    )


@router.post("/files/delete/{file_id}")
def delete_file(request: Request, file_id: int):
    denied = require_login(request)
    if denied:
        return denied

    user = request.state.user
    if not can_delete_files(user):
        return RedirectResponse("/documents?error=Bạn không có quyền xóa tài liệu.", status_code=303)

    rec = get_file_record(file_id)
    if not rec:
        return RedirectResponse("/documents?error=Không tìm thấy tài liệu.", status_code=303)

    with get_conn() as conn:
        conn.execute(
            "UPDATE document_files SET is_deleted = 1 WHERE id = ?",
            (file_id,),
        )
        conn.commit()

    return RedirectResponse("/documents?msg=Xóa tài liệu thành công.", status_code=303)