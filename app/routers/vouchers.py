from __future__ import annotations

import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ..database import (
    BOARD_ROLES,
    EXPORT_DIR,
    HEAD_ROLES,
    IMPORT_DIR,
    KSNB_ROLES,
    ROLE_ADMIN,
    can_board_view,
    can_create_voucher,
    can_head_approve,
    get_conn,
    voucher_hash,
)
from ..excel_service import (
    HEAD_DELEGATION_TEXT,
    REVIEW_ITEM_MASTER,
    export_voucher_xlsx,
    parse_import,
    supplier_match_key,
)

from ..upload_security import (
    BUSINESS_IMPORT_EXTENSIONS,
    BUSINESS_IMPORT_MAX_UPLOAD_MB,
    CURRENT_EXCEL_PARSER_EXTENSIONS,
    UploadValidationError,
    safe_original_filename,
    save_upload_file_chunked,
)

templates = Jinja2Templates(directory="app/templates")
router = APIRouter(prefix="/vouchers")

STATUS_LABELS = {
    "DRAFT": "Nháp",
    "SUBMITTED_TO_HEAD": "Đã trình Trưởng ban",
    "HEAD_APPROVED": "Trưởng ban đã đồng ý",
    "SUBMITTED_TO_BOARD": "Đã trình HĐTV",
    "BOARD_VIEWED": "HĐTV đã xem",
    "BOARD_SAVED": "HĐTV đã lưu",
    "NO_SIGNATURE_INTERNAL": "Không ký số - lưu nội bộ",
    "RETURNED": "Đã trả lại",
}

BOARD_LOCKED_STATUSES = {"SUBMITTED_TO_BOARD", "BOARD_VIEWED", "BOARD_SAVED"}

CONCLUSION_TEXT = (
    "Ban Kiểm soát nội bộ thực hiện kiểm soát trước đối với hồ sơ nêu trên theo chức năng "
    "kiểm soát, thẩm tra và kiến nghị; không thay thế trách nhiệm lập hồ sơ của Phòng Tài chính "
    "kế toán hoặc đơn vị liên quan, không thay thế thẩm quyền quyết định, phê duyệt của Hội đồng "
    "thành viên hoặc Chủ tịch Hội đồng thành viên."
)

ROMAN_NUMERALS = {
    1: "I",
    2: "II",
    3: "III",
    4: "IV",
    5: "V",
    6: "VI",
    7: "VII",
    8: "VIII",
}


VN_TZ = timezone(timedelta(hours=7))


def parse_datetime_to_vn(value: str | None) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None

    normalized_text = text.replace("T", " ")
    if normalized_text.endswith("Z"):
        normalized_text = normalized_text[:-1] + "+00:00"

    try:
        parsed = datetime.fromisoformat(normalized_text)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y"):
            try:
                parsed = datetime.strptime(normalized_text, fmt)
                break
            except ValueError:
                continue
        else:
            return None

    if parsed.tzinfo is not None:
        return parsed.astimezone(VN_TZ)

    return parsed


def vn_now_text() -> str:
    return datetime.now(VN_TZ).replace(microsecond=0).isoformat()


def vn_now_input_value() -> str:
    return datetime.now(VN_TZ).strftime("%Y-%m-%d")


def date_input_value(value: str | None) -> str:
    parsed = parse_datetime_to_vn(value)
    if not parsed:
        return ""
    return parsed.strftime("%Y-%m-%d")


def format_vn_datetime(value: str | None) -> str:
    parsed = parse_datetime_to_vn(value)
    if not parsed:
        return str(value or "").strip()
    return parsed.strftime("%d/%m/%Y")


def format_vn_signature_datetime(value: str | None) -> str:
    parsed = parse_datetime_to_vn(value)
    if not parsed:
        return str(value or "").strip()
    return f"{parsed.hour:02d} giờ {parsed.minute:02d} phút, ngày {parsed.day:02d}/{parsed.month:02d}/{parsed.year:04d}"


def format_vn_date_line(value: str | None) -> str:
    parsed = parse_datetime_to_vn(value)
    if not parsed:
        now = datetime.now(VN_TZ)
        return f"Gia Lai, ngày {now.day:02d} tháng {now.month:02d} năm {now.year}"
    return f"Gia Lai, ngày {parsed.day:02d} tháng {parsed.month:02d} năm {parsed.year}"


def money_value(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def doc_check_amount(doc) -> float:
    if doc["ksnb_check_type"] == "REJECTED":
        return 0.0
    if doc["ksnb_check_type"] == "DIFFERENT":
        return money_value(doc["ksnb_checked_amount"])
    return money_value(doc["document_amount"])

def doc_payment_note(doc) -> str:
    methods = []
    if doc["payment_transfer"]:
        methods.append("Chuyển khoản")
    if doc["payment_cash"]:
        methods.append("Tiền mặt")
    note = (doc["ksnb_note"] or "").strip()
    if methods and note:
        return "; ".join(methods) + "; " + note
    if methods:
        return "; ".join(methods)
    return note


def can_edit_voucher_before_board(user: dict, voucher) -> bool:
    if not user or not voucher:
        return False
    if voucher["status"] != "DRAFT":
        return False
    if user.get("role_code") == ROLE_ADMIN:
        return True
    return voucher["created_by"] == user.get("id")


def build_payable_warnings(imported_rows: list[dict], payable_rows: list[dict]) -> list[dict]:
    if not imported_rows or not payable_rows:
        return []

    payable_by_code: dict[str, dict] = {}
    payable_by_key: dict[str, dict] = {}

    for row in payable_rows:
        supplier_code = str(row.get("supplier_code") or "").strip().upper()
        supplier_name = str(row.get("supplier_name") or "").strip()
        supplier_key = str(row.get("supplier_key") or supplier_match_key(supplier_name)).strip()
        payable_amount = money_value(row.get("payable_amount"))

        if supplier_code:
            item_by_code = payable_by_code.setdefault(
                supplier_code,
                {
                    "supplier_code": supplier_code,
                    "supplier_name": supplier_name,
                    "supplier_key": supplier_key,
                    "payable_amount": 0.0,
                },
            )
            item_by_code["payable_amount"] += payable_amount

        if supplier_key:
            item_by_key = payable_by_key.setdefault(
                supplier_key,
                {
                    "supplier_code": supplier_code,
                    "supplier_name": supplier_name,
                    "supplier_key": supplier_key,
                    "payable_amount": 0.0,
                },
            )
            item_by_key["payable_amount"] += payable_amount

    payment_by_match: dict[str, dict] = {}

    for row in imported_rows:
        supplier_code = str(row.get("supplier_code") or "").strip().upper()
        supplier_name = str(row.get("supplier") or "").strip()
        supplier_key = supplier_match_key(supplier_name)

        match_key = ""
        payable = None
        match_method = ""

        if supplier_code and supplier_code in payable_by_code:
            match_key = f"CODE::{supplier_code}"
            payable = payable_by_code[supplier_code]
            match_method = "Mã"
        elif supplier_key and supplier_key in payable_by_key:
            match_key = f"NAME::{supplier_key}"
            payable = payable_by_key[supplier_key]
            match_method = "Tên nhà cung cấp đã chuẩn hóa"

        if not match_key or not payable:
            continue

        item = payment_by_match.setdefault(
            match_key,
            {
                "supplier_code": supplier_code,
                "supplier_name": supplier_name,
                "supplier_key": supplier_key,
                "source_types": set(),
                "control_payment_amount": 0.0,
                "payable": payable,
                "match_method": match_method,
            },
        )
        item["control_payment_amount"] += money_value(row.get("document_amount"))
        item["source_types"].add(str(row.get("source_type") or "").strip())

    warnings: list[dict] = []

    for payment in payment_by_match.values():
        payable = payment["payable"]
        control_amount = money_value(payment.get("control_payment_amount"))
        payable_amount = money_value(payable.get("payable_amount"))
        over_amount = control_amount - payable_amount
        if over_amount <= 0:
            continue

        match_method = payment.get("match_method") or "Tên nhà cung cấp đã chuẩn hóa"
        warning_message = (
            "Số tiền thanh toán theo hồ sơ kiểm soát vượt số phải trả nhà cung cấp "
            f"theo Sổ THCN. Phương thức đối chiếu: {match_method}. "
            "Người kiểm soát xem xét trước khi quyết định có đưa vào Phiếu hay không."
        )

        warnings.append(
            {
                "supplier_name": payment["supplier_name"],
                "supplier_key": payment["supplier_key"] or payable.get("supplier_key") or "",
                "source_types": ", ".join(sorted(x for x in payment["source_types"] if x)),
                "control_payment_amount": control_amount,
                "payable_amount": payable_amount,
                "over_amount": over_amount,
                "warning_level": "OVER_PAYABLE",
                "warning_message": warning_message,
                "matched_payable_name": payable.get("supplier_name") or payment["supplier_name"],
            }
        )

    return warnings


async def save_upload_and_parse(voucher_id: int, upload: UploadFile | None, source_type: str) -> list[dict]:
    if not upload or not upload.filename:
        return []

    original_name = safe_original_filename(upload.filename)
    safe_name = f"voucher_{voucher_id}_{source_type}_{original_name}"
    path = IMPORT_DIR / safe_name

    try:
        _size_bytes, _sha256_value, original_name = save_upload_file_chunked(
            upload,
            path,
            allowed_extensions=BUSINESS_IMPORT_EXTENSIONS,
            max_size_mb=BUSINESS_IMPORT_MAX_UPLOAD_MB,
            parser_supported_extensions=CURRENT_EXCEL_PARSER_EXTENSIONS,
        )
    except UploadValidationError as ex:
        raise HTTPException(status_code=400, detail=str(ex)) from ex

    rows = parse_import(path, source_type)
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO voucher_import_files(voucher_id, source_type, original_name, stored_name) VALUES (?, ?, ?, ?)",
            (voucher_id, source_type, original_name, safe_name),
        )
        conn.commit()
    return rows


def parse_latest_import_rows(voucher_id: int, source_type: str) -> list[dict]:
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT stored_name
            FROM voucher_import_files
            WHERE voucher_id = ? AND source_type = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (voucher_id, source_type),
        ).fetchone()

    if not row:
        return []

    path = IMPORT_DIR / row["stored_name"]
    if not path.exists():
        return []

    return parse_import(path, source_type)


def fetch_document_import_rows(voucher_id: int) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT source_type, supplier_code, supplier, content, document_amount
            FROM voucher_documents
            WHERE voucher_id = ?
            ORDER BY row_order
            """,
            (voucher_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def fetch_payable_detail_rows(voucher_id: int) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT supplier_code, supplier_name, supplier_key, opening_debit, opening_credit,
                   period_debit, period_credit, ending_debit, ending_credit, payable_amount, row_order
            FROM voucher_payable_details
            WHERE voucher_id = ?
            ORDER BY supplier_name, row_order, id
            """,
            (voucher_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def save_payable_details(conn, voucher_id: int, payable_rows: list[dict]) -> None:
    conn.execute("DELETE FROM voucher_payable_details WHERE voucher_id = ?", (voucher_id,))
    for idx, row in enumerate(payable_rows, start=1):
        conn.execute(
            """
            INSERT INTO voucher_payable_details(
                voucher_id, supplier_code, supplier_name, supplier_key,
                opening_debit, opening_credit, period_debit, period_credit,
                ending_debit, ending_credit, payable_amount, row_order
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                voucher_id,
                row.get("supplier_code") or "",
                row.get("supplier_name") or "",
                row.get("supplier_key") or "",
                money_value(row.get("opening_debit")),
                money_value(row.get("opening_credit")),
                money_value(row.get("period_debit")),
                money_value(row.get("period_credit")),
                money_value(row.get("ending_debit")),
                money_value(row.get("ending_credit")),
                money_value(row.get("payable_amount")),
                int(row.get("row_order") or idx),
            ),
        )


def save_payable_warnings(conn, voucher_id: int, payable_warnings: list[dict]) -> None:
    conn.execute("DELETE FROM voucher_payable_warnings WHERE voucher_id = ?", (voucher_id,))
    for warning in payable_warnings:
        conn.execute(
            """
            INSERT INTO voucher_payable_warnings(
                voucher_id, supplier_name, supplier_key, source_types, control_payment_amount,
                payable_amount, over_amount, warning_level, warning_message, matched_payable_name
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                voucher_id,
                warning["supplier_name"],
                warning["supplier_key"],
                warning["source_types"],
                warning["control_payment_amount"],
                warning["payable_amount"],
                warning["over_amount"],
                warning["warning_level"],
                warning["warning_message"],
                warning["matched_payable_name"],
            ),
        )


def require_login(request: Request):
    if not request.state.user:
        return RedirectResponse("/login", status_code=303)
    return None


def signature_text(user: dict) -> str:
    return f"Đã ký số nội bộ\n{user['full_name']}\n{user['position_title']}"


def no_signature_text(user: dict) -> str:
    return f"Không ký số - in tên trên Phiếu\n{user['full_name']}\n{user['position_title']}"


def find_libreoffice_exe() -> str | None:
    candidates = [
        shutil.which("soffice"),
        shutil.which("libreoffice"),
        r"C:\Program Files\LibreOffice\program\soffice.exe",
        r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return str(candidate)
    return None


def fetch_full_voucher(voucher_id: int) -> dict:
    with get_conn() as conn:
        voucher = conn.execute(
            "SELECT v.*, u.full_name AS creator_name, u.position_title AS creator_position FROM vouchers v JOIN users u ON u.id = v.created_by WHERE v.id = ?",
            (voucher_id,),
        ).fetchone()
        if not voucher:
            raise HTTPException(404, "Không tìm thấy Phiếu.")
        docs = conn.execute("SELECT * FROM voucher_documents WHERE voucher_id = ? ORDER BY row_order", (voucher_id,)).fetchall()
        review_items = conn.execute("SELECT * FROM voucher_review_items WHERE voucher_id = ? ORDER BY item_order", (voucher_id,)).fetchall()
        payable_warnings = conn.execute(
            "SELECT * FROM voucher_payable_warnings WHERE voucher_id = ? ORDER BY over_amount DESC, id",
            (voucher_id,),
        ).fetchall()
        payable_details = conn.execute(
            """
            SELECT *
            FROM voucher_payable_details
            WHERE voucher_id = ?
            ORDER BY supplier_name, row_order, id
            """,
            (voucher_id,),
        ).fetchall()
        payable_details_by_key: dict[str, list] = {}
        for item in payable_details:
            payable_details_by_key.setdefault(item["supplier_key"], []).append(item)
        signature_rows = conn.execute("SELECT * FROM voucher_signatures WHERE voucher_id = ? ORDER BY id", (voucher_id,)).fetchall()
        route_rows = conn.execute(
            "SELECT r.*, fu.full_name AS from_name, tu.full_name AS to_name FROM voucher_routes r LEFT JOIN users fu ON fu.id = r.from_user_id LEFT JOIN users tu ON tu.id = r.to_user_id WHERE r.voucher_id = ? ORDER BY r.id",
            (voucher_id,),
        ).fetchall()

    signatures = []
    for row in signature_rows:
        item = dict(row)
        item["signed_at_vn_text"] = format_vn_signature_datetime(item.get("signed_at"))
        signatures.append(item)

    routes = []
    for row in route_rows:
        item = dict(row)
        item["created_at_vn_text"] = format_vn_signature_datetime(item.get("created_at"))
        routes.append(item)

    return {
        "voucher": voucher,
        "docs": docs,
        "review_items": review_items,
        "payable_warnings": payable_warnings,
        "payable_details": payable_details,
        "payable_details_by_key": payable_details_by_key,
        "signatures": signatures,
        "routes": routes,
    }

def can_view_voucher(user: dict, voucher) -> bool:
    if not user or not voucher:
        return False

    if user.get("role_code") == ROLE_ADMIN:
        return True

    if voucher["created_by"] == user.get("id"):
        return True

    if user.get("role_code") in KSNB_ROLES:
        return True

    if user.get("role_code") in BOARD_ROLES and voucher["status"] in ("SUBMITTED_TO_BOARD", "BOARD_VIEWED", "BOARD_SAVED"):
        return True

    return False


def mark_admin_voucher_seen(voucher_id: int, user: dict) -> None:
    if not user or user.get("role_code") != ROLE_ADMIN:
        return

    with get_conn() as conn:
        voucher = conn.execute(
            "SELECT status FROM vouchers WHERE id = ?",
            (voucher_id,),
        ).fetchone()

        if not voucher or voucher["status"] not in ("SUBMITTED_TO_HEAD", "SUBMITTED_TO_BOARD"):
            return

        existing_seen = conn.execute(
            """
            SELECT id
            FROM voucher_routes
            WHERE voucher_id = ?
              AND action = 'ADMIN_XEM_PHIEU'
              AND from_user_id = ?
            LIMIT 1
            """,
            (voucher_id, user["id"]),
        ).fetchone()

        if existing_seen:
            return

        now_vn = vn_now_text()
        conn.execute(
            """
            INSERT INTO voucher_routes(voucher_id, action, from_user_id, note, created_at)
            VALUES (?, 'ADMIN_XEM_PHIEU', ?, ?, ?)
            """,
            (voucher_id, user["id"], "Admin đã mở xem Phiếu kiểm soát.", now_vn),
        )
        conn.commit()


def print_context(request: Request, voucher_id: int) -> dict:
    data = fetch_full_voucher(voucher_id)
    if not can_view_voucher(request.state.user, data["voucher"]):
        raise HTTPException(403, "Không có quyền xem Phiếu.")

    docs = data["docs"]
    voucher = data["voucher"]
    doc_total = sum(money_value(d["document_amount"]) for d in docs)
    ksnb_total = sum(doc_check_amount(d) for d in docs)

    optional_sections = []
    if (voucher["section_v_text"] or "").strip():
        optional_sections.append(
            {
                "roman": ROMAN_NUMERALS[5 + len(optional_sections)],
                "title": "KIẾN NGHỊ CỦA BAN KIỂM SOÁT NỘI BỘ",
                "content": voucher["section_v_text"].strip(),
            }
        )
    if (voucher["section_vi_text"] or "").strip():
        optional_sections.append(
            {
                "roman": ROMAN_NUMERALS[5 + len(optional_sections)],
                "title": "Ý KIẾN CỦA ĐƠN VỊ LẬP HỒ SƠ",
                "content": voucher["section_vi_text"].strip(),
            }
        )
    conclusion_roman = ROMAN_NUMERALS[5 + len(optional_sections)]

    return {
        "request": request,
        "user": request.state.user,
        "delegation_text": HEAD_DELEGATION_TEXT,
        "status_labels": STATUS_LABELS,
        "conclusion_text": CONCLUSION_TEXT,
        "optional_sections": optional_sections,
        "conclusion_roman": conclusion_roman,
        "doc_total": doc_total,
        "ksnb_total": ksnb_total,
        "doc_payment_note": doc_payment_note,
        "format_vn_datetime": format_vn_datetime,
        "format_vn_signature_datetime": format_vn_signature_datetime,
        "format_vn_date_line": format_vn_date_line,
        **data,
    }


@router.get("")
def voucher_list(request: Request):
    denied = require_login(request)
    if denied:
        return denied

    user = request.state.user
    selected_year = str(request.query_params.get("year") or "").strip()
    selected_month = str(request.query_params.get("month") or "").strip()
    selected_day = str(request.query_params.get("day") or "").strip()

    where_parts = ["1=1"]
    params: list = []

    if user["role_code"] in KSNB_ROLES:
        where_parts.append(
            "(v.created_by = ? OR v.status IN ('SUBMITTED_TO_HEAD','HEAD_APPROVED','SUBMITTED_TO_BOARD','BOARD_VIEWED','BOARD_SAVED','NO_SIGNATURE_INTERNAL'))"
        )
        params.append(user["id"])
    elif user["role_code"] in BOARD_ROLES:
        where_parts.append("v.status IN ('SUBMITTED_TO_BOARD','BOARD_VIEWED','BOARD_SAVED')")
    elif user["role_code"] != ROLE_ADMIN:
        where_parts.append("0=1")

    if selected_year:
        where_parts.append("strftime('%Y', v.created_at) = ?")
        params.append(selected_year)
    if selected_month:
        where_parts.append("strftime('%m', v.created_at) = ?")
        params.append(selected_month.zfill(2))
    if selected_day:
        where_parts.append("strftime('%d', v.created_at) = ?")
        params.append(selected_day.zfill(2))

    where = " AND ".join(where_parts)

    with get_conn() as conn:
        years = conn.execute(
            """
            SELECT DISTINCT strftime('%Y', created_at) AS year
            FROM vouchers
            WHERE created_at IS NOT NULL
            ORDER BY year DESC
            """
        ).fetchall()

        rows = conn.execute(
            f"""
            SELECT v.*, u.full_name AS creator_name
            FROM vouchers v
            JOIN users u ON u.id = v.created_by
            WHERE {where}
            ORDER BY v.id DESC
            """,
            params,
        ).fetchall()

    admin_seen_voucher_ids: set[int] = set()
    if user["role_code"] == ROLE_ADMIN:
        with get_conn() as conn:
            seen_rows = conn.execute(
                """
                SELECT voucher_id
                FROM voucher_routes
                WHERE action = 'ADMIN_XEM_PHIEU'
                  AND from_user_id = ?
                """,
                (user["id"],),
            ).fetchall()
        admin_seen_voucher_ids = {int(row["voucher_id"]) for row in seen_rows}

    voucher_rows = []
    for row in rows:
        item = dict(row)
        item["is_new_for_current_user"] = False

        if (
            user["role_code"] == ROLE_ADMIN
            and item["status"] in ("SUBMITTED_TO_HEAD", "SUBMITTED_TO_BOARD")
            and int(item["id"]) not in admin_seen_voucher_ids
        ):
            item["is_new_for_current_user"] = True
        elif user["role_code"] in HEAD_ROLES and item["status"] == "SUBMITTED_TO_HEAD" and item["current_handler"] == user["id"]:
            item["is_new_for_current_user"] = True
        elif user["role_code"] in BOARD_ROLES and item["status"] == "SUBMITTED_TO_BOARD" and (
            item["current_handler"] == user["id"] or item["current_handler"] is None
        ):
            item["is_new_for_current_user"] = True

        voucher_rows.append(item)

    voucher_new_count = sum(1 for item in voucher_rows if item["is_new_for_current_user"])

    months = [f"{i:02d}" for i in range(1, 13)]
    days = [f"{i:02d}" for i in range(1, 32)]

    return templates.TemplateResponse(
        "voucher_list.html",
        {
            "request": request,
            "vouchers": voucher_rows,
            "user": user,
            "status_labels": STATUS_LABELS,
            "years": years,
            "months": months,
            "days": days,
            "selected_year": selected_year,
            "selected_month": selected_month.zfill(2) if selected_month else "",
            "selected_day": selected_day.zfill(2) if selected_day else "",
            "voucher_new_count": voucher_new_count,
        },
    )

@router.get("/new")
def voucher_new(request: Request):
    denied = require_login(request)
    if denied:
        return denied
    if not can_create_voucher(request.state.user):
        return RedirectResponse("/vouchers", status_code=303)
    with get_conn() as conn:
        heads = conn.execute(
            "SELECT * FROM users WHERE is_active = 1 AND role_code IN ('TRUONG_BAN_KSNB','PHO_TRUONG_BAN_KSNB') ORDER BY role_code, full_name"
        ).fetchall()
    return templates.TemplateResponse(
        "voucher_form.html",
        {
            "request": request,
            "review_items": REVIEW_ITEM_MASTER,
            "selected_review_items": set(),
            "heads": heads,
            "error": None,
            "received_at_default": vn_now_input_value(),
            "voucher": None,
            "is_edit": False,
            "form_action": "/vouchers/new",
            "submit_label": "Lưu nháp",
        },
    )


@router.post("/new")
async def voucher_create(
    request: Request,
    title: str = Form(...),
    ho_so_type: str = Form("CHI"),
    total_amount: float = Form(0),
    submitting_unit: str = Form(""),
    sender_name: str = Form(""),
    received_at: str = Form(""),
    approval_target: str = Form("Trình Hội đồng thành viên"),
    route_mode: str = Form("THROUGH_HEAD"),
    head_user_id: str = Form(""),
    review_items: list[str] = Form([]),
    section_iv_result: str = Form(""),
    section_iv_note: str = Form(""),
    section_v_text: str = Form(""),
    section_vi_text: str = Form(""),
    dntt_file: UploadFile | None = File(None),
    von_tu_co_file: UploadFile | None = File(None),
    thcn_payable_file: UploadFile | None = File(None),
):
    
    denied = require_login(request)
    if denied:
        return denied
    user = request.state.user
    if not can_create_voucher(user):
        return RedirectResponse("/vouchers", status_code=303)

    head_user_id_value = None
    head_user_id_text = str(head_user_id or "").strip()
    if head_user_id_text:
        try:
            head_user_id_value = int(head_user_id_text)
        except ValueError:
            head_user_id_value = None

    # Lưu nháp cho phép chưa chọn nội dung kiểm soát trước ở Mục II.

    with get_conn() as conn:
        now_vn = vn_now_text()
        cur = conn.execute(
            """
            INSERT INTO vouchers(title, hồ_so_type, total_amount, submitting_unit, sender_name, received_at,
                approval_target, route_mode, status, section_iv_result, section_iv_note, section_v_text, section_vi_text,
                created_by, current_handler, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'DRAFT', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                title.strip(), ho_so_type, total_amount, submitting_unit.strip(), sender_name.strip(), received_at.strip(),
                approval_target, route_mode, section_iv_result, section_iv_note, section_v_text, section_vi_text,
                user["id"], head_user_id_value, now_vn,
            ),
        )
        voucher_id = cur.lastrowid
        code = f"KSNB-{voucher_id:05d}"
        conn.execute("UPDATE vouchers SET code = ? WHERE id = ?", (code, voucher_id))
        for idx, item in enumerate(review_items, start=1):
            conn.execute("INSERT INTO voucher_review_items(voucher_id, item_order, content) VALUES (?, ?, ?)", (voucher_id, idx, item))
        conn.commit()

    imported_rows = []
    imported_rows.extend(await save_upload_and_parse(voucher_id, dntt_file, "DNTT"))
    imported_rows.extend(await save_upload_and_parse(voucher_id, von_tu_co_file, "VON_TU_CO"))
    payable_rows = await save_upload_and_parse(voucher_id, thcn_payable_file, "THCN_PAYABLE")
    payable_warnings = build_payable_warnings(imported_rows, payable_rows)

    with get_conn() as conn:
        doc_total = 0.0
        for idx, doc in enumerate(imported_rows, start=1):
            amount = money_value(doc["document_amount"])
            doc_total += amount
            conn.execute(
                """
                INSERT INTO voucher_documents(voucher_id, source_type, supplier_code, supplier, content, document_amount, ksnb_check_type, row_order)
                VALUES (?, ?, ?, ?, ?, ?, 'MATCH', ?)
                """,
                (
                    voucher_id,
                    doc["source_type"],
                    doc.get("supplier_code") or "",
                    doc["supplier"],
                    doc["content"],
                    amount,
                    idx,
                ),
            )
        save_payable_details(conn, voucher_id, payable_rows)
        save_payable_warnings(conn, voucher_id, payable_warnings)
        if imported_rows:
            conn.execute("UPDATE vouchers SET total_amount = ? WHERE id = ?", (doc_total, voucher_id))
        conn.commit()
    return RedirectResponse(f"/vouchers/{voucher_id}", status_code=303)


@router.get("/{voucher_id}/edit")
def voucher_edit(request: Request, voucher_id: int):
    denied = require_login(request)
    if denied:
        return denied

    user = request.state.user
    data = fetch_full_voucher(voucher_id)
    voucher = data["voucher"]

    if not can_edit_voucher_before_board(user, voucher):
        return RedirectResponse(f"/vouchers/{voucher_id}", status_code=303)

    with get_conn() as conn:
        heads = conn.execute(
            "SELECT * FROM users WHERE is_active = 1 AND role_code IN ('TRUONG_BAN_KSNB','PHO_TRUONG_BAN_KSNB') ORDER BY role_code, full_name"
        ).fetchall()
        import_files = conn.execute(
            """
            SELECT source_type, original_name, uploaded_at
            FROM voucher_import_files
            WHERE voucher_id = ?
            ORDER BY id DESC
            """,
            (voucher_id,),
        ).fetchall()

    latest_import_files: dict[str, str] = {}
    for item in import_files:
        if item["source_type"] not in latest_import_files:
            latest_import_files[item["source_type"]] = item["original_name"]

    selected_review_items = {item["content"] for item in data["review_items"]}

    return templates.TemplateResponse(
        "voucher_form.html",
        {
            "request": request,
            "review_items": REVIEW_ITEM_MASTER,
            "selected_review_items": selected_review_items,
            "heads": heads,
            "error": None,
            "received_at_default": date_input_value(voucher["received_at"]) or vn_now_input_value(),
            "voucher": voucher,
            "is_edit": True,
            "form_action": f"/vouchers/{voucher_id}/edit",
            "submit_label": "Lưu nháp",
            "latest_import_files": latest_import_files,
        },
    )

@router.post("/{voucher_id}/edit")
async def voucher_update(
    request: Request,
    voucher_id: int,
    title: str = Form(...),
    ho_so_type: str = Form("CHI"),
    total_amount: float = Form(0),
    submitting_unit: str = Form(""),
    sender_name: str = Form(""),
    received_at: str = Form(""),
    approval_target: str = Form("Trình Hội đồng thành viên"),
    route_mode: str = Form("THROUGH_HEAD"),
    head_user_id: str = Form(""),
    review_items: list[str] = Form([]),
    section_iv_result: str | None = Form(None),
    section_iv_note: str | None = Form(None),
    section_v_text: str | None = Form(None),
    section_vi_text: str | None = Form(None),
    dntt_file: UploadFile | None = File(None),
    von_tu_co_file: UploadFile | None = File(None),
    thcn_payable_file: UploadFile | None = File(None),
):
    denied = require_login(request)
    if denied:
        return denied

    user = request.state.user

    with get_conn() as conn:
        voucher = conn.execute("SELECT * FROM vouchers WHERE id = ?", (voucher_id,)).fetchone()

    if not voucher:
        return RedirectResponse("/vouchers", status_code=303)

    if not can_edit_voucher_before_board(user, voucher):
        return RedirectResponse(f"/vouchers/{voucher_id}", status_code=303)

    head_user_id_value = None
    head_user_id_text = str(head_user_id or "").strip()
    if head_user_id_text:
        try:
            head_user_id_value = int(head_user_id_text)
        except ValueError:
            head_user_id_value = None

    # Lưu nháp cho phép chưa chọn nội dung kiểm soát trước ở Mục II.

    replace_documents = bool((dntt_file and dntt_file.filename) or (von_tu_co_file and von_tu_co_file.filename))
    replace_payable = bool(thcn_payable_file and thcn_payable_file.filename)

    imported_rows: list[dict] = []
    payable_rows: list[dict] = []

    if replace_documents:
        if dntt_file and dntt_file.filename:
            dntt_rows = await save_upload_and_parse(voucher_id, dntt_file, "DNTT")
        else:
            dntt_rows = parse_latest_import_rows(voucher_id, "DNTT")

        if von_tu_co_file and von_tu_co_file.filename:
            von_tu_co_rows = await save_upload_and_parse(voucher_id, von_tu_co_file, "VON_TU_CO")
        else:
            von_tu_co_rows = parse_latest_import_rows(voucher_id, "VON_TU_CO")

        imported_rows.extend(dntt_rows)
        imported_rows.extend(von_tu_co_rows)
    else:
        imported_rows = fetch_document_import_rows(voucher_id)

    if replace_payable:
        payable_rows = await save_upload_and_parse(voucher_id, thcn_payable_file, "THCN_PAYABLE")
    else:
        payable_rows = fetch_payable_detail_rows(voucher_id)
        if not payable_rows:
            payable_rows = parse_latest_import_rows(voucher_id, "THCN_PAYABLE")

    should_rebuild_warnings = replace_documents or replace_payable
    payable_warnings = build_payable_warnings(imported_rows, payable_rows) if should_rebuild_warnings else []

    section_iv_result_value = voucher["section_iv_result"] if section_iv_result is None else section_iv_result
    section_iv_note_value = voucher["section_iv_note"] if section_iv_note is None else section_iv_note
    section_v_text_value = voucher["section_v_text"] if section_v_text is None else section_v_text
    section_vi_text_value = voucher["section_vi_text"] if section_vi_text is None else section_vi_text

    with get_conn() as conn:
        existing_doc_total_row = conn.execute(
            "SELECT COALESCE(SUM(document_amount), 0) AS total_amount, COUNT(*) AS doc_count FROM voucher_documents WHERE voucher_id = ?",
            (voucher_id,),
        ).fetchone()

        final_total_amount = money_value(voucher["total_amount"])
        if replace_documents:
            doc_total = 0.0
            conn.execute("DELETE FROM voucher_documents WHERE voucher_id = ?", (voucher_id,))
            for idx, doc in enumerate(imported_rows, start=1):
                amount = money_value(doc["document_amount"])
                doc_total += amount
                conn.execute(
                    """
                    INSERT INTO voucher_documents(voucher_id, source_type, supplier_code, supplier, content, document_amount, ksnb_check_type, row_order)
                    VALUES (?, ?, ?, ?, ?, ?, 'MATCH', ?)
                    """,
                    (
                        voucher_id,
                        doc["source_type"],
                        doc.get("supplier_code") or "",
                        doc["supplier"],
                        doc["content"],
                        amount,
                        idx,
                    ),
                )
            final_total_amount = doc_total
        elif existing_doc_total_row and existing_doc_total_row["doc_count"]:
            final_total_amount = money_value(voucher["total_amount"])
        else:
            final_total_amount = money_value(total_amount)

        if replace_payable:
            save_payable_details(conn, voucher_id, payable_rows)
        elif should_rebuild_warnings and payable_rows:
            current_detail_count = conn.execute(
                "SELECT COUNT(*) AS cnt FROM voucher_payable_details WHERE voucher_id = ?",
                (voucher_id,),
            ).fetchone()
            if not current_detail_count or not current_detail_count["cnt"]:
                save_payable_details(conn, voucher_id, payable_rows)

        if should_rebuild_warnings:
            save_payable_warnings(conn, voucher_id, payable_warnings)

        conn.execute(
            """
            UPDATE vouchers
            SET title = ?,
                hồ_so_type = ?,
                total_amount = ?,
                submitting_unit = ?,
                sender_name = ?,
                received_at = ?,
                approval_target = ?,
                route_mode = ?,
                current_handler = ?,
                section_iv_result = ?,
                section_iv_note = ?,
                section_v_text = ?,
                section_vi_text = ?
            WHERE id = ?
            """,
            (
                title.strip(),
                ho_so_type,
                final_total_amount,
                submitting_unit.strip(),
                sender_name.strip(),
                received_at.strip(),
                approval_target,
                route_mode,
                head_user_id_value,
                section_iv_result_value,
                section_iv_note_value,
                section_v_text_value,
                section_vi_text_value,
                voucher_id,
            ),
        )

        conn.execute("DELETE FROM voucher_review_items WHERE voucher_id = ?", (voucher_id,))
        for idx, item in enumerate(review_items, start=1):
            conn.execute(
                "INSERT INTO voucher_review_items(voucher_id, item_order, content) VALUES (?, ?, ?)",
                (voucher_id, idx, item),
            )

        conn.commit()

    return RedirectResponse(f"/vouchers/{voucher_id}", status_code=303)


@router.post("/{voucher_id}/delete")
def voucher_delete(request: Request, voucher_id: int):
    denied = require_login(request)
    if denied:
        return denied

    user = request.state.user

    with get_conn() as conn:
        voucher = conn.execute("SELECT * FROM vouchers WHERE id = ?", (voucher_id,)).fetchone()

        if not voucher:
            return RedirectResponse("/vouchers", status_code=303)

        if user.get("role_code") != ROLE_ADMIN and voucher["created_by"] != user.get("id"):
            return RedirectResponse(f"/vouchers/{voucher_id}", status_code=303)

        if voucher["status"] != "DRAFT":
            return RedirectResponse(f"/vouchers/{voucher_id}", status_code=303)

        conn.execute("DELETE FROM voucher_documents WHERE voucher_id = ?", (voucher_id,))
        conn.execute("DELETE FROM voucher_review_items WHERE voucher_id = ?", (voucher_id,))
        conn.execute("DELETE FROM voucher_signatures WHERE voucher_id = ?", (voucher_id,))
        conn.execute("DELETE FROM voucher_routes WHERE voucher_id = ?", (voucher_id,))
        conn.execute("DELETE FROM voucher_import_files WHERE voucher_id = ?", (voucher_id,))
        conn.execute("DELETE FROM voucher_payable_warnings WHERE voucher_id = ?", (voucher_id,))
        conn.execute("DELETE FROM voucher_payable_details WHERE voucher_id = ?", (voucher_id,))
        conn.execute("DELETE FROM vouchers WHERE id = ?", (voucher_id,))
        conn.commit()

    return RedirectResponse("/vouchers", status_code=303)

@router.get("/{voucher_id}")
def voucher_detail(request: Request, voucher_id: int):
    denied = require_login(request)
    if denied:
        return denied
    data = print_context(request, voucher_id)
    voucher = data["voucher"]

    if request.state.user.get("role_code") == ROLE_ADMIN:
        mark_admin_voucher_seen(voucher_id, request.state.user)

    if request.state.user.get("role_code") in BOARD_ROLES:
        return RedirectResponse(f"/vouchers/{voucher_id}/print", status_code=303)

    with get_conn() as conn:
        heads = conn.execute(
            "SELECT * FROM users WHERE is_active = 1 AND role_code IN ('TRUONG_BAN_KSNB','PHO_TRUONG_BAN_KSNB') ORDER BY role_code, full_name"
        ).fetchall()
        board_users = conn.execute(
            """
            SELECT *
            FROM users
            WHERE is_active = 1
              AND role_code IN ('TONG_GIAM_DOC','PHO_TGD_THUONG_TRUC','PHO_TONG_GIAM_DOC')
            ORDER BY
              CASE role_code
                WHEN 'TONG_GIAM_DOC' THEN 1
                WHEN 'PHO_TGD_THUONG_TRUC' THEN 2
                WHEN 'PHO_TONG_GIAM_DOC' THEN 3
                ELSE 9
              END,
              full_name
            """
        ).fetchall()
    can_edit_before_board = can_edit_voucher_before_board(request.state.user, voucher)

    return templates.TemplateResponse(
        "voucher_detail.html",
        {
            **data,
            "heads": heads,
            "board_users": board_users,
            "can_head": can_head_approve(request.state.user),
            "can_board": can_board_view(request.state.user),
            "can_creator": voucher["created_by"] == request.state.user["id"],
            "can_edit_before_board": can_edit_before_board,
        },
    )


@router.get("/{voucher_id}/print")
def voucher_print(request: Request, voucher_id: int):
    denied = require_login(request)
    if denied:
        return denied

    user = request.state.user
    data = print_context(request, voucher_id)
    voucher = data["voucher"]

    if user.get("role_code") == ROLE_ADMIN:
        mark_admin_voucher_seen(voucher_id, user)

    if user.get("role_code") in BOARD_ROLES and voucher["status"] == "SUBMITTED_TO_BOARD":
        with get_conn() as conn:
            current_voucher = conn.execute(
                "SELECT status FROM vouchers WHERE id = ?",
                (voucher_id,),
            ).fetchone()

            if current_voucher and current_voucher["status"] == "SUBMITTED_TO_BOARD":
                now_vn = vn_now_text()
                conn.execute(
                    "UPDATE vouchers SET status = 'BOARD_VIEWED', current_handler = NULL WHERE id = ?",
                    (voucher_id,),
                )
                conn.execute(
                    """
                    INSERT INTO voucher_routes(voucher_id, action, from_user_id, note, created_at)
                    VALUES (?, 'HDTV_XEM_PHIEU', ?, ?, ?)
                    """,
                    (voucher_id, user["id"], "HĐTV đã mở xem Phiếu kiểm soát.", now_vn),
                )
                conn.commit()
                data = print_context(request, voucher_id)

    return templates.TemplateResponse("voucher_print.html", data)


@router.post("/{voucher_id}/documents/update")
async def update_docs(request: Request, voucher_id: int):
    denied = require_login(request)
    if denied:
        return denied
    form = await request.form()
    section_iv_result = str(form.get("section_iv_result") or "").strip()
    section_iv_note = str(form.get("section_iv_note") or "").strip()

    with get_conn() as conn:
        voucher = conn.execute("SELECT * FROM vouchers WHERE id = ?", (voucher_id,)).fetchone()
        if not voucher or not can_edit_voucher_before_board(request.state.user, voucher):
            return RedirectResponse(f"/vouchers/{voucher_id}", status_code=303)

        docs = conn.execute("SELECT * FROM voucher_documents WHERE voucher_id = ? ORDER BY row_order", (voucher_id,)).fetchall()
        for doc in docs:
            doc_id = doc["id"]
            check_type = str(form.get(f"check_type_{doc_id}") or "MATCH")
            checked_amount_raw = str(form.get(f"checked_amount_{doc_id}") or "").replace(".", "").replace(",", "").strip()
            note = str(form.get(f"note_{doc_id}") or "").strip()
            payment_transfer = 1 if form.get(f"payment_transfer_{doc_id}") else 0
            payment_cash = 1 if form.get(f"payment_cash_{doc_id}") else 0
            checked_amount = float(checked_amount_raw) if checked_amount_raw else None
            if check_type == "REJECTED":
                checked_amount = None
            elif check_type == "DIFFERENT":
                pass
            else:
                check_type = "MATCH"
                checked_amount = None
            conn.execute(
                """
                UPDATE voucher_documents
                SET ksnb_check_type = ?, ksnb_checked_amount = ?, ksnb_note = ?,
                    payment_transfer = ?, payment_cash = ?
                WHERE id = ? AND voucher_id = ?
                """,
                (check_type, checked_amount, note, payment_transfer, payment_cash, doc_id, voucher_id),
            )
        totals = conn.execute(
            "SELECT COALESCE(SUM(document_amount), 0) AS total_amount FROM voucher_documents WHERE voucher_id = ?",
            (voucher_id,),
        ).fetchone()
        conn.execute(
            """
            UPDATE vouchers
            SET total_amount = ?,
                section_iv_result = ?,
                section_iv_note = ?
            WHERE id = ?
            """,
            (totals["total_amount"], section_iv_result, section_iv_note, voucher_id),
        )
        conn.commit()
    return RedirectResponse(f"/vouchers/{voucher_id}", status_code=303)


@router.post("/{voucher_id}/submit-no-signature")
def submit_voucher_no_signature(
    request: Request,
    voucher_id: int,
    no_sign_head_user_id: str = Form(...),
):
    denied = require_login(request)
    if denied:
        return denied
    user = request.state.user
    head_user_id_text = str(no_sign_head_user_id or "").strip()
    if not head_user_id_text:
        return RedirectResponse(f"/vouchers/{voucher_id}", status_code=303)
    try:
        head_user_id_value = int(head_user_id_text)
    except ValueError:
        return RedirectResponse(f"/vouchers/{voucher_id}", status_code=303)

    with get_conn() as conn:
        voucher = conn.execute("SELECT * FROM vouchers WHERE id = ?", (voucher_id,)).fetchone()
        if not voucher or voucher["created_by"] != user["id"] or voucher["status"] != "DRAFT":
            return RedirectResponse("/vouchers", status_code=303)

        review_count = conn.execute(
            "SELECT COUNT(*) AS cnt FROM voucher_review_items WHERE voucher_id = ?",
            (voucher_id,),
        ).fetchone()
        if not review_count or review_count["cnt"] <= 0:
            return RedirectResponse(f"/vouchers/{voucher_id}/edit", status_code=303)

        head_user = conn.execute(
            """
            SELECT * FROM users
            WHERE id = ?
              AND is_active = 1
              AND role_code IN ('TRUONG_BAN_KSNB','PHO_TRUONG_BAN_KSNB')
            """,
            (head_user_id_value,),
        ).fetchone()
        if not head_user:
            return RedirectResponse(f"/vouchers/{voucher_id}", status_code=303)

        now_vn = vn_now_text()
        conn.execute("DELETE FROM voucher_signatures WHERE voucher_id = ?", (voucher_id,))
        conn.execute(
            "INSERT INTO voucher_signatures(voucher_id, signer_id, signer_name, signer_role, signature_text, hash_value, signed_at) VALUES (?, ?, ?, 'NGUOI_KIEM_SOAT', ?, NULL, ?)",
            (voucher_id, user["id"], user["full_name"], no_signature_text(user), now_vn),
        )
        conn.execute(
            "INSERT INTO voucher_signatures(voucher_id, signer_id, signer_name, signer_role, signature_text, hash_value, signed_at) VALUES (?, ?, ?, 'TRUONG_BAN', ?, NULL, ?)",
            (voucher_id, head_user["id"], head_user["full_name"], no_signature_text(head_user), now_vn),
        )
        conn.execute(
            "UPDATE vouchers SET status = 'NO_SIGNATURE_INTERNAL', submitted_at = ?, submitted_to_board_at = NULL, current_handler = NULL WHERE id = ?",
            (now_vn, voucher_id),
        )
        conn.execute(
            "INSERT INTO voucher_routes(voucher_id, action, from_user_id, to_user_id, note, created_at) VALUES (?, 'KHONG_KY_SO_LUU_NOI_BO', ?, ?, ?, ?)",
            (voucher_id, user["id"], head_user["id"], "Không ký số; in tên người kiểm soát và Trưởng/Phó Trưởng ban KSNB trên Phiếu. Phiếu lưu nội bộ, không trình HĐTV.", now_vn),
        )
        conn.commit()
    return RedirectResponse(f"/vouchers/{voucher_id}", status_code=303)




@router.post("/{voucher_id}/submit")
def submit_voucher(
    request: Request,
    voucher_id: int,
    board_user_id: str = Form(""),
):
    denied = require_login(request)
    if denied:
        return denied
    user = request.state.user
    with get_conn() as conn:
        voucher = conn.execute("SELECT * FROM vouchers WHERE id = ?", (voucher_id,)).fetchone()
        if not voucher or voucher["created_by"] != user["id"] or voucher["status"] != "DRAFT":
            return RedirectResponse("/vouchers", status_code=303)

        review_count = conn.execute(
            "SELECT COUNT(*) AS cnt FROM voucher_review_items WHERE voucher_id = ?",
            (voucher_id,),
        ).fetchone()
        if not review_count or review_count["cnt"] <= 0:
            return RedirectResponse(f"/vouchers/{voucher_id}/edit", status_code=303)

        board_user = None
        if voucher["route_mode"] == "DIRECT_BOARD":
            board_user_id_text = str(board_user_id or "").strip()
            if not board_user_id_text:
                return RedirectResponse(f"/vouchers/{voucher_id}", status_code=303)
            try:
                board_user_id_value = int(board_user_id_text)
            except ValueError:
                return RedirectResponse(f"/vouchers/{voucher_id}", status_code=303)

            board_user = conn.execute(
                """
                SELECT *
                FROM users
                WHERE id = ?
                  AND is_active = 1
                  AND role_code IN ('TONG_GIAM_DOC','PHO_TGD_THUONG_TRUC','PHO_TONG_GIAM_DOC')
                """,
                (board_user_id_value,),
            ).fetchone()
            if not board_user:
                return RedirectResponse(f"/vouchers/{voucher_id}", status_code=303)

        if voucher["route_mode"] != "DIRECT_BOARD" and not voucher["current_handler"]:
            return RedirectResponse(f"/vouchers/{voucher_id}/edit", status_code=303)
        now_vn = vn_now_text()
        conn.execute(
            "INSERT INTO voucher_signatures(voucher_id, signer_id, signer_name, signer_role, signature_text, hash_value, signed_at) VALUES (?, ?, ?, 'NGUOI_KIEM_SOAT', ?, ?, ?)",
            (voucher_id, user["id"], user["full_name"], signature_text(user), voucher_hash(voucher_id), now_vn),
        )
        if voucher["route_mode"] == "DIRECT_BOARD":
            if user["role_code"] in HEAD_ROLES:
                conn.execute(
                    "INSERT INTO voucher_signatures(voucher_id, signer_id, signer_name, signer_role, signature_text, hash_value, signed_at) VALUES (?, ?, ?, 'TRUONG_BAN', ?, ?, ?)",
                    (voucher_id, user["id"], user["full_name"], signature_text(user), voucher_hash(voucher_id), now_vn),
                )
            else:
                conn.execute(
                    "INSERT INTO voucher_signatures(voucher_id, signer_id, signer_name, signer_role, signature_text, hash_value, signed_at) VALUES (?, NULL, 'Trưởng ban KSNB', 'TRUONG_BAN', ?, ?, ?)",
                    (voucher_id, HEAD_DELEGATION_TEXT, voucher_hash(voucher_id), now_vn),
                )
            conn.execute("UPDATE vouchers SET status = 'SUBMITTED_TO_BOARD', submitted_at = ?, submitted_to_board_at = ?, current_handler = ? WHERE id = ?", (now_vn, now_vn, board_user["id"], voucher_id))
            conn.execute(
                "INSERT INTO voucher_routes(voucher_id, action, from_user_id, to_user_id, note, created_at) VALUES (?, 'TRINH_THANG_HDTV', ?, ?, ?, ?)",
                (voucher_id, user["id"], board_user["id"], "Nhân viên ký số nội bộ và trình thẳng HĐTV", now_vn),
            )
        else:
            conn.execute("UPDATE vouchers SET status = 'SUBMITTED_TO_HEAD', submitted_at = ? WHERE id = ?", (now_vn, voucher_id))
            conn.execute("INSERT INTO voucher_routes(voucher_id, action, from_user_id, to_user_id, note, created_at) VALUES (?, 'TRINH_TRUONG_BAN', ?, ?, ?, ?)", (voucher_id, user["id"], voucher["current_handler"], "Nhân viên trình Trưởng ban KSNB", now_vn))
        conn.commit()
    return RedirectResponse(f"/vouchers/{voucher_id}", status_code=303)


@router.post("/{voucher_id}/head-approve")
def head_approve(
    request: Request,
    voucher_id: int,
    board_user_id: str = Form(...),
):
    denied = require_login(request)
    if denied:
        return denied
    user = request.state.user
    if not can_head_approve(user):
        return RedirectResponse(f"/vouchers/{voucher_id}", status_code=303)

    board_user_id_text = str(board_user_id or "").strip()
    if not board_user_id_text:
        return RedirectResponse(f"/vouchers/{voucher_id}", status_code=303)
    try:
        board_user_id_value = int(board_user_id_text)
    except ValueError:
        return RedirectResponse(f"/vouchers/{voucher_id}", status_code=303)

    with get_conn() as conn:
        voucher = conn.execute("SELECT * FROM vouchers WHERE id = ?", (voucher_id,)).fetchone()
        if not voucher or voucher["status"] != "SUBMITTED_TO_HEAD":
            return RedirectResponse(f"/vouchers/{voucher_id}", status_code=303)

        board_user = conn.execute(
            """
            SELECT *
            FROM users
            WHERE id = ?
              AND is_active = 1
              AND role_code IN ('TONG_GIAM_DOC','PHO_TGD_THUONG_TRUC','PHO_TONG_GIAM_DOC')
            """,
            (board_user_id_value,),
        ).fetchone()
        if not board_user:
            return RedirectResponse(f"/vouchers/{voucher_id}", status_code=303)

        now_vn = vn_now_text()
        conn.execute(
            "INSERT INTO voucher_signatures(voucher_id, signer_id, signer_name, signer_role, signature_text, hash_value, signed_at) VALUES (?, ?, ?, 'TRUONG_BAN', ?, ?, ?)",
            (voucher_id, user["id"], user["full_name"], signature_text(user), voucher_hash(voucher_id), now_vn),
        )
        conn.execute(
            "UPDATE vouchers SET status = 'SUBMITTED_TO_BOARD', submitted_to_board_at = ?, current_handler = ? WHERE id = ?",
            (now_vn, board_user["id"], voucher_id),
        )
        conn.execute(
            "INSERT INTO voucher_routes(voucher_id, action, from_user_id, to_user_id, note, created_at) VALUES (?, 'TRUONG_BAN_TRINH_HDTV', ?, ?, ?, ?)",
            (
                voucher_id,
                user["id"],
                board_user["id"],
                "Trưởng ban/Phó Trưởng ban ký số nội bộ và trình Hội đồng thành viên.",
                now_vn,
            ),
        )
        conn.commit()
    return RedirectResponse(f"/vouchers/{voucher_id}", status_code=303)


@router.post("/{voucher_id}/board-save")
def board_save(request: Request, voucher_id: int):
    denied = require_login(request)
    if denied:
        return denied
    user = request.state.user
    if not can_board_view(user):
        return RedirectResponse(f"/vouchers/{voucher_id}", status_code=303)
    with get_conn() as conn:
        now_vn = vn_now_text()
        conn.execute("UPDATE vouchers SET status = 'BOARD_SAVED', board_saved_at = ? WHERE id = ?", (now_vn, voucher_id))
        conn.execute("INSERT INTO voucher_routes(voucher_id, action, from_user_id, note, created_at) VALUES (?, 'HDTV_LUU_PHIEU', ?, ?, ?)", (voucher_id, user["id"], "HĐTV đã xem và lưu Phiếu", now_vn))
        conn.commit()
    return RedirectResponse(f"/vouchers/{voucher_id}", status_code=303)


@router.get("/{voucher_id}/export.xlsx")
def download_xlsx(request: Request, voucher_id: int):
    denied = require_login(request)
    if denied:
        return denied
    data = print_context(request, voucher_id)
    path = export_voucher_xlsx(voucher_id)
    filename = f"{data['voucher']['code']}.xlsx"
    return FileResponse(path, filename=filename, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@router.get("/{voucher_id}/export/pdf")
def export_pdf(request: Request, voucher_id: int):
    denied = require_login(request)
    if denied:
        return denied
    data = print_context(request, voucher_id)

    out_dir = EXPORT_DIR / str(voucher_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    html_path = out_dir / f"phieu_ksnb_{voucher_id}.html"
    pdf_path = out_dir / f"phieu_ksnb_{voucher_id}.pdf"
    lo_profile_dir = out_dir / "lo_profile"
    lo_profile_dir.mkdir(parents=True, exist_ok=True)

    if pdf_path.exists():
        try:
            pdf_path.unlink()
        except OSError:
            pass

    html = templates.get_template("voucher_print.html").render(data)
    html_path.write_text(html, encoding="utf-8")

    soffice = find_libreoffice_exe()
    if not soffice:
        raise HTTPException(
            500,
            "Không tìm thấy LibreOffice/soffice.exe. Cần cài LibreOffice hoặc thêm LibreOffice vào PATH Windows.",
        )

    cmd = [
        soffice,
        "--headless",
        "--nologo",
        "--nofirststartwizard",
        f"-env:UserInstallation=file:///{lo_profile_dir.as_posix()}",
        "--convert-to",
        "pdf:writer_pdf_Export",
        "--outdir",
        str(out_dir),
        str(html_path),
    ]
    result = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=60)

    if not pdf_path.exists():
        stderr = (result.stderr or "").strip()
        stdout = (result.stdout or "").strip()
        message = stderr or stdout or "LibreOffice không tạo được file PDF."
        raise HTTPException(500, f"Chưa xuất được PDF. Lỗi LibreOffice: {message}")

    return FileResponse(pdf_path, filename=f"{data['voucher']['code']}.pdf", media_type="application/pdf")


@router.get("/{voucher_id}/view-pdf")
def view_pdf_legacy(request: Request, voucher_id: int):
    return export_pdf(request, voucher_id)
