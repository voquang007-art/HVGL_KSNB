# -*- coding: utf-8 -*-
from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path
from typing import Iterable

from fastapi import UploadFile


UPLOAD_CHUNK_SIZE = 1024 * 1024

# Danh sách nguy hiểm tuyệt đối.
# Không đưa .html/.htm vào đây vì có thể dùng làm dữ liệu import nghiệp vụ.
# Tuy nhiên .html/.htm chỉ được phép khi allowed_extensions của đúng luồng import cho phép.
DANGEROUS_EXTENSIONS = {
    ".exe",
    ".bat",
    ".cmd",
    ".com",
    ".scr",
    ".ps1",
    ".vbs",
    ".js",
    ".jse",
    ".jar",
    ".msi",
    ".dll",
    ".php",
    ".py",
    ".pyw",
    ".sh",
    ".bash",
    ".zsh",
    ".svg",
}

EXCEL_EXTENSIONS = {".xls", ".xlsx"}
BUSINESS_IMPORT_EXTENSIONS = {".xls", ".xlsx", ".csv", ".xml", ".html", ".htm"}
CURRENT_EXCEL_PARSER_EXTENSIONS = {".xls", ".xlsx"}

OFFICE_DOCUMENT_EXTENSIONS = {".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx"}
PDF_EXTENSIONS = {".pdf"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
TEXT_EXTENSIONS = {".txt"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".wmv"}

DOCUMENT_LIBRARY_EXTENSIONS = (
    OFFICE_DOCUMENT_EXTENSIONS
    | PDF_EXTENSIONS
    | IMAGE_EXTENSIONS
    | VIDEO_EXTENSIONS
)

GENERAL_SAFE_UPLOAD_EXTENSIONS = (
    OFFICE_DOCUMENT_EXTENSIONS
    | PDF_EXTENSIONS
    | IMAGE_EXTENSIONS
    | TEXT_EXTENSIONS
)

DEFAULT_MAX_UPLOAD_MB = int(os.environ.get("HVGL_KSNB_MAX_UPLOAD_MB", "25"))
BUSINESS_IMPORT_MAX_UPLOAD_MB = int(os.environ.get("HVGL_KSNB_BUSINESS_IMPORT_MAX_UPLOAD_MB", "80"))
CHAT_MAX_UPLOAD_MB = int(os.environ.get("HVGL_KSNB_CHAT_MAX_UPLOAD_MB", "25"))
MEETING_MAX_UPLOAD_MB = int(os.environ.get("HVGL_KSNB_MEETING_MAX_UPLOAD_MB", "50"))
DRAFT_MAX_UPLOAD_MB = int(os.environ.get("HVGL_KSNB_DRAFT_MAX_UPLOAD_MB", "50"))
DOCUMENT_MAX_UPLOAD_MB = int(os.environ.get("HVGL_KSNB_DOCUMENT_MAX_UPLOAD_MB", "50"))


class UploadValidationError(Exception):
    pass


def safe_original_filename(filename: str) -> str:
    clean = Path(filename or "tep_dinh_kem").name
    clean = clean.replace("..", "_").replace("/", "_").replace("\\", "_").strip()
    return clean or "tep_dinh_kem"


def extension_of(filename: str) -> str:
    return Path(filename or "").suffix.lower()


def unique_stored_name(original_name: str, prefix: str = "") -> str:
    ext = extension_of(original_name)
    clean_prefix = safe_original_filename(prefix).replace(".", "_").strip("_") if prefix else ""
    uid = uuid.uuid4().hex
    if clean_prefix:
        return f"{clean_prefix}_{uid}{ext}"
    return f"{uid}{ext}"


def validate_upload_extension(
    upload: UploadFile | None,
    *,
    allowed_extensions: Iterable[str],
    parser_supported_extensions: Iterable[str] | None = None,
) -> str:
    if not upload or not upload.filename:
        raise UploadValidationError("Chưa chọn tệp để tải lên.")

    original_name = safe_original_filename(upload.filename)
    ext = extension_of(original_name)

    if not ext:
        raise UploadValidationError("Tệp tải lên phải có phần mở rộng hợp lệ.")

    if ext in DANGEROUS_EXTENSIONS:
        raise UploadValidationError("Định dạng tệp có nguy cơ mất an toàn và không được phép tải lên.")

    allowed = {str(item).lower() for item in allowed_extensions}
    if ext not in allowed:
        raise UploadValidationError("Định dạng tệp không được hỗ trợ trong luồng này.")

    if parser_supported_extensions is not None:
        supported = {str(item).lower() for item in parser_supported_extensions}
        if ext not in supported:
            raise UploadValidationError(
                "Định dạng tệp thuộc nhóm import nghiệp vụ nhưng nguồn dữ liệu này hiện chưa có bộ đọc phù hợp. "
                "Hiện tại hệ thống đang hỗ trợ đọc Excel .xls/.xlsx cho nguồn import này."
            )

    return original_name


def save_upload_file_chunked(
    upload: UploadFile,
    destination: Path | str,
    *,
    allowed_extensions: Iterable[str],
    max_size_mb: int,
    parser_supported_extensions: Iterable[str] | None = None,
) -> tuple[int, str, str]:
    original_name = validate_upload_extension(
        upload,
        allowed_extensions=allowed_extensions,
        parser_supported_extensions=parser_supported_extensions,
    )

    if int(max_size_mb or 0) <= 0:
        raise UploadValidationError("Cấu hình giới hạn dung lượng upload không hợp lệ.")

    destination_path = Path(destination)
    destination_path.parent.mkdir(parents=True, exist_ok=True)

    max_bytes = int(max_size_mb) * 1024 * 1024
    total_size = 0
    hasher = hashlib.sha256()

    try:
        with destination_path.open("wb") as output:
            while True:
                chunk = upload.file.read(UPLOAD_CHUNK_SIZE)
                if not chunk:
                    break

                total_size += len(chunk)
                if total_size > max_bytes:
                    try:
                        output.close()
                    finally:
                        destination_path.unlink(missing_ok=True)
                    raise UploadValidationError(f"Tệp tải lên vượt quá giới hạn {max_size_mb} MB.")

                hasher.update(chunk)
                output.write(chunk)
    finally:
        try:
            upload.file.close()
        except Exception:
            pass

    return total_size, hasher.hexdigest(), original_name