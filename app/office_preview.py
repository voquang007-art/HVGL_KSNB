# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import uuid
from functools import lru_cache
from pathlib import Path


class OfficePreviewError(Exception):
    pass


def is_office_previewable(filename: str) -> bool:
    name = (filename or "").strip().lower()
    return name.endswith((".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx"))


def _clean_path(value: object) -> str:
    return str(value or "").strip().strip('"').strip("'")


def _is_soffice_file(path_value: object) -> bool:
    clean = _clean_path(path_value)
    if not clean:
        return False

    path = Path(clean)
    return path.is_file() and path.name.lower() in {"soffice.exe", "soffice"}


def _candidate_from_dir(path_value: object) -> str:
    clean = _clean_path(path_value)
    if not clean:
        return ""

    path = Path(clean)
    if path.is_file():
        return str(path)

    candidates = [
        path / "soffice.exe",
        path / "soffice",
        path / "program" / "soffice.exe",
        path / "program" / "soffice",
        path / "LibreOffice" / "program" / "soffice.exe",
        path / "LibreOffice" / "program" / "soffice",
    ]

    for item in candidates:
        if _is_soffice_file(item):
            return str(item)

    return ""


def _iter_env_candidates() -> list[str]:
    candidates: list[str] = []

    for key in (
        "SOFFICE_PATH",
        "LIBREOFFICE_PATH",
        "LIBREOFFICE_HOME",
        "LIBREOFFICE_PROGRAM",
    ):
        raw = _clean_path(os.environ.get(key))
        if not raw:
            continue

        if _is_soffice_file(raw):
            candidates.append(raw)
            continue

        from_dir = _candidate_from_dir(raw)
        if from_dir:
            candidates.append(from_dir)

    return candidates


def _iter_known_windows_candidates() -> list[str]:
    roots: list[str] = []

    for key in (
        "ProgramFiles",
        "ProgramFiles(x86)",
        "ProgramW6432",
        "LOCALAPPDATA",
        "PROGRAMDATA",
    ):
        raw = _clean_path(os.environ.get(key))
        if raw:
            roots.append(raw)

    roots.extend(
        [
            r"C:\Program Files",
            r"C:\Program Files (x86)",
            r"C:\LibreOffice",
            r"D:\LibreOffice",
        ]
    )

    candidates: list[str] = []
    for root in roots:
        if not root:
            continue

        candidates.extend(
            [
                str(Path(root) / "LibreOffice" / "program" / "soffice.exe"),
                str(Path(root) / "LibreOffice" / "program" / "soffice"),
                str(Path(root) / "LibreOffice 7" / "program" / "soffice.exe"),
                str(Path(root) / "LibreOffice 7" / "program" / "soffice"),
                str(Path(root) / "LibreOffice 24" / "program" / "soffice.exe"),
                str(Path(root) / "LibreOffice 24" / "program" / "soffice"),
                str(Path(root) / "LibreOffice 25" / "program" / "soffice.exe"),
                str(Path(root) / "LibreOffice 25" / "program" / "soffice"),
            ]
        )

    return candidates


def _iter_registry_candidates() -> list[str]:
    if os.name != "nt":
        return []

    try:
        import winreg
    except Exception:
        return []

    registry_roots = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\LibreOffice\UNO\InstallPath"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\LibreOffice\UNO\InstallPath"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\LibreOffice\UNO\InstallPath"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\The Document Foundation\LibreOffice"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\The Document Foundation\LibreOffice"),
    ]

    candidates: list[str] = []

    for root, key_path in registry_roots:
        try:
            with winreg.OpenKey(root, key_path) as key:
                for value_name in ("", "Path", "InstallPath"):
                    try:
                        value, _value_type = winreg.QueryValueEx(key, value_name)
                    except Exception:
                        continue

                    from_dir = _candidate_from_dir(value)
                    if from_dir:
                        candidates.append(from_dir)
        except Exception:
            continue

    return candidates


def _iter_path_candidates() -> list[str]:
    candidates: list[str] = []

    for command in ("soffice", "soffice.exe", "libreoffice", "libreoffice.exe"):
        found = shutil.which(command)
        if found:
            candidates.append(found)

    return candidates


def _iter_drive_roots() -> list[Path]:
    if os.name == "nt":
        roots: list[Path] = []
        for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
            root = Path(f"{letter}:\\")
            if root.exists():
                roots.append(root)
        return roots

    return [Path("/usr/bin"), Path("/usr/local/bin"), Path("/opt"), Path("/Applications")]


def _safe_walk_for_soffice() -> list[str]:
    """
    Dò rộng khi LibreOffice cài ngoài vị trí chuẩn.
    Hàm này chỉ chạy khi các cách nhanh thất bại và đã được cache bởi _find_soffice().
    """
    matches: list[str] = []
    target_names = {"soffice.exe", "soffice"}

    skip_dir_names = {
        "$recycle.bin",
        "appdata",
        "cache",
        "node_modules",
        "windows\\winsxs",
        "system volume information",
        "temp",
        "tmp",
        ".git",
        "__pycache__",
    }

    for root in _iter_drive_roots():
        try:
            for current_root, dirs, files in os.walk(root):
                current_lower = current_root.lower()

                if any(skip_name in current_lower for skip_name in skip_dir_names):
                    dirs[:] = []
                    continue

                for filename in files:
                    if filename.lower() in target_names:
                        full_path = Path(current_root) / filename
                        if _is_soffice_file(full_path):
                            matches.append(str(full_path))
                            if "libreoffice" in str(full_path).lower():
                                return matches

                dirs[:] = [
                    d for d in dirs
                    if d.lower() not in {"$recycle.bin", "system volume information", "__pycache__"}
                ]
        except Exception:
            continue

    return matches


def _unique_existing_candidates(candidates: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()

    for item in candidates:
        clean = _clean_path(item)
        if not clean:
            continue

        normalized = os.path.normcase(os.path.abspath(clean))
        if normalized in seen:
            continue

        if _is_soffice_file(clean):
            seen.add(normalized)
            result.append(clean)

    return result


@lru_cache(maxsize=1)
def _find_soffice() -> str:
    candidates: list[str] = []
    candidates.extend(_iter_env_candidates())
    candidates.extend(_iter_path_candidates())
    candidates.extend(_iter_registry_candidates())
    candidates.extend(_iter_known_windows_candidates())

    existing = _unique_existing_candidates(candidates)
    if existing:
        return existing[0]

    broad_matches = _unique_existing_candidates(_safe_walk_for_soffice())
    if broad_matches:
        return broad_matches[0]

    checked = "\n".join(candidates[:30])
    if len(candidates) > 30:
        checked += "\n..."

    raise OfficePreviewError(
        "Không tìm thấy soffice.exe/libreoffice trên máy chủ. "
        "Hệ thống đã thử dò theo biến môi trường SOFFICE_PATH/LIBREOFFICE_PATH, PATH, Registry Windows, "
        "các thư mục Program Files và dò rộng trên các ổ đĩa. "
        "Nếu LibreOffice đã cài, hãy khai báo SOFFICE_PATH trỏ đúng tới soffice.exe. "
        f"Các đường dẫn đã kiểm tra nhanh:\n{checked}"
    )


def _preview_output_dir() -> Path:
    configured = _clean_path(os.environ.get("HVGL_KSNB_OFFICE_PREVIEW_DIR"))
    if configured:
        base_dir = Path(configured)
    else:
        base_dir = Path("instance") / "office_previews"

    base_dir.mkdir(parents=True, exist_ok=True)
    return base_dir


def _safe_preview_key(preview_key: str) -> str:
    clean = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in str(preview_key or "").strip())
    return clean or uuid.uuid4().hex


def ensure_office_pdf_preview(
    *,
    source_path: str,
    preview_key: str,
    original_name: str,
) -> str:
    if not is_office_previewable(original_name):
        raise OfficePreviewError("Định dạng tệp không thuộc nhóm Office có thể xem trước.")

    source = Path(source_path)
    if not source.is_file():
        raise OfficePreviewError("Không tìm thấy tệp Office nguồn để tạo bản xem trước.")

    soffice = _find_soffice()
    output_dir = _preview_output_dir()
    safe_key = _safe_preview_key(preview_key)

    source_ext = source.suffix.lower()
    work_dir = Path(tempfile.mkdtemp(prefix=f"{safe_key}_", dir=str(output_dir)))
    temp_source = work_dir / f"source{source_ext}"
    shutil.copy2(source, temp_source)

    cmd = [
        soffice,
        "--headless",
        "--nologo",
        "--nofirststartwizard",
        "--nodefault",
        "--nolockcheck",
        "--norestore",
        "--convert-to",
        "pdf",
        "--outdir",
        str(work_dir),
        str(temp_source),
    ]

    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=int(os.environ.get("HVGL_KSNB_OFFICE_PREVIEW_TIMEOUT", "90")),
            check=False,
            text=True,
            encoding="utf-8",
            errors="ignore",
        )
    except subprocess.TimeoutExpired as ex:
        raise OfficePreviewError("LibreOffice xử lý quá thời gian cho phép khi tạo bản xem trước PDF.") from ex
    except Exception as ex:
        raise OfficePreviewError(f"Không chạy được LibreOffice tại: {soffice}. Chi tiết: {ex}") from ex

    expected_pdf = work_dir / "source.pdf"
    if result.returncode != 0 or not expected_pdf.is_file():
        stdout = (result.stdout or "").strip()
        stderr = (result.stderr or "").strip()
        raise OfficePreviewError(
            "LibreOffice không tạo được file PDF xem trước. "
            f"SOFFICE={soffice}; returncode={result.returncode}; stdout={stdout}; stderr={stderr}"
        )

    final_pdf = output_dir / f"{safe_key}.pdf"
    if final_pdf.exists():
        final_pdf.unlink()
    shutil.move(str(expected_pdf), str(final_pdf))

    try:
        shutil.rmtree(work_dir, ignore_errors=True)
    except Exception:
        pass

    return str(final_pdf)