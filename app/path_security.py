# -*- coding: utf-8 -*-
from __future__ import annotations

import os
from pathlib import Path

from fastapi import HTTPException

from .config import settings


def _safe_roots() -> list[Path]:
    raw_roots = os.environ.get("HVGL_KSNB_SAFE_FILE_ROOTS", "").strip()
    roots: list[Path] = []

    if raw_roots:
        for item in raw_roots.split(";"):
            clean = item.strip()
            if clean:
                roots.append(Path(clean).resolve())

    roots.extend(
        [
            Path(settings.UPLOAD_DIR).resolve(),
            Path(settings.INSTANCE_DIR).resolve(),
            Path(settings.BASE_DIR).resolve() / "storage",
            Path(settings.ROOT_DIR).resolve() / "data",
        ]
    )

    unique_roots: list[Path] = []
    for root in roots:
        if root not in unique_roots:
            unique_roots.append(root)

    return unique_roots


def ensure_safe_file_path(path_value: str | os.PathLike, *, must_exist: bool = True) -> str:
    if not path_value:
        raise HTTPException(status_code=404, detail="Không tìm thấy đường dẫn tệp.")

    try:
        resolved_path = Path(path_value).resolve()
    except Exception:
        raise HTTPException(status_code=400, detail="Đường dẫn tệp không hợp lệ.")

    if must_exist and not resolved_path.is_file():
        raise HTTPException(status_code=404, detail="Tệp không còn tồn tại trên máy chủ.")

    allowed = False
    for root in _safe_roots():
        try:
            resolved_path.relative_to(root)
            allowed = True
            break
        except ValueError:
            continue

    if not allowed:
        raise HTTPException(status_code=403, detail="Đường dẫn tệp nằm ngoài phạm vi được phép truy cập.")

    return str(resolved_path)