from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from ..database import BOARD_ROLES, KSNB_ROLES, ROLE_ADMIN, get_conn

templates = Jinja2Templates(directory="app/templates")
router = APIRouter(prefix="/control-statistics")

VN_TZ = timezone(timedelta(hours=7))

ALLOWED_ROLES = set(KSNB_ROLES) | set(BOARD_ROLES) | {ROLE_ADMIN}

IMPORTANT_VOUCHER_STATUSES = (
    "SUBMITTED_TO_HEAD",
    "HEAD_APPROVED",
    "SUBMITTED_TO_BOARD",
    "BOARD_VIEWED",
    "BOARD_SAVED",
)

CASH_CONTROL_VOUCHER_STATUSES = (
    "SUBMITTED_TO_HEAD",
    "SUBMITTED_TO_BOARD",
    "BOARD_VIEWED",
    "BOARD_SAVED",
)


def require_allowed_user(request: Request):
    user = request.state.user
    if not user:
        return RedirectResponse("/login", status_code=303)

    role_code = user["role_code"] if isinstance(user, dict) else getattr(user, "role_code", "")
    if role_code not in ALLOWED_ROLES:
        return RedirectResponse("/", status_code=303)

    return None


def today_vn() -> date:
    return datetime.now(VN_TZ).date()


def parse_date_param(value: Any) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None

    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def date_input_value(value: date) -> str:
    return value.strftime("%Y-%m-%d")


def date_text_vn(value: date) -> str:
    return value.strftime("%d/%m/%Y")


def money_text(value: Any) -> str:
    try:
        amount = float(value or 0)
    except (TypeError, ValueError):
        amount = 0.0
    return f"{amount:,.0f}".replace(",", ".")


def default_date_range() -> tuple[date, date]:
    current = today_vn()
    return current.replace(day=1), current


def build_quick_ranges() -> dict[str, dict[str, str]]:
    current = today_vn()
    week_start = current - timedelta(days=current.weekday())
    week_end = week_start + timedelta(days=6)
    month_start = current.replace(day=1)
    year_start = current.replace(month=1, day=1)

    return {
        "today": {
            "label": "Hôm nay",
            "start_date": date_input_value(current),
            "end_date": date_input_value(current),
        },
        "week": {
            "label": "Tuần này",
            "start_date": date_input_value(week_start),
            "end_date": date_input_value(week_end),
        },
        "month": {
            "label": "Tháng này",
            "start_date": date_input_value(month_start),
            "end_date": date_input_value(current),
        },
        "year": {
            "label": "Năm này",
            "start_date": date_input_value(year_start),
            "end_date": date_input_value(current),
        },
    }


def resolve_date_range(request: Request) -> tuple[date, date]:
    default_start, default_end = default_date_range()
    start_date = parse_date_param(request.query_params.get("start_date")) or default_start
    end_date = parse_date_param(request.query_params.get("end_date")) or default_end

    if start_date > end_date:
        start_date, end_date = end_date, start_date

    return start_date, end_date


def date_filter_sql(alias: str) -> str:
    return (
        "substr(COALESCE("
        f"{alias}.board_saved_at, "
        f"{alias}.submitted_to_board_at, "
        f"{alias}.submitted_at, "
        f"{alias}.created_at"
        "), 1, 10) BETWEEN ? AND ?"
    )


def status_placeholders(statuses: tuple[str, ...]) -> str:
    return ",".join("?" for _ in statuses)


def fetch_important_voucher_stats(start_date: date, end_date: date) -> dict[str, Any]:
    start_text = date_input_value(start_date)
    end_text = date_input_value(end_date)
    status_sql = status_placeholders(IMPORTANT_VOUCHER_STATUSES)

    sql = f"""
        SELECT
            COUNT(d.id) AS document_count,
            COALESCE(SUM(
                CASE
                    WHEN d.ksnb_check_type = 'REJECTED' THEN 0
                    WHEN d.ksnb_check_type = 'DIFFERENT' THEN COALESCE(d.ksnb_checked_amount, 0)
                    ELSE COALESCE(d.document_amount, 0)
                END
            ), 0) AS checked_amount
        FROM voucher_documents d
        JOIN vouchers v ON v.id = d.voucher_id
        WHERE d.source_type = ?
          AND v.status IN ({status_sql})
          AND {date_filter_sql("v")}
    """

    with get_conn() as conn:
        dntt_row = conn.execute(
            sql,
            ("DNTT", *IMPORTANT_VOUCHER_STATUSES, start_text, end_text),
        ).fetchone()

        own_capital_row = conn.execute(
            sql,
            ("VON_TU_CO", *IMPORTANT_VOUCHER_STATUSES, start_text, end_text),
        ).fetchone()

    return {
        "dntt_document_count": int((dntt_row or {})["document_count"] or 0),
        "dntt_checked_amount": float((dntt_row or {})["checked_amount"] or 0),
        "own_capital_document_count": int((own_capital_row or {})["document_count"] or 0),
        "own_capital_checked_amount": float((own_capital_row or {})["checked_amount"] or 0),
    }


def fetch_cash_control_stats(start_date: date, end_date: date) -> dict[str, Any]:
    start_text = date_input_value(start_date)
    end_text = date_input_value(end_date)
    status_sql = status_placeholders(CASH_CONTROL_VOUCHER_STATUSES)

    sql = f"""
        SELECT
            COALESCE(SUM(v.receipt_count), 0) AS receipt_count,
            COALESCE(SUM(v.revenue_ksnb_total), 0) AS cash_revenue_total,
            COALESCE(SUM(
                (
                    SELECT COALESCE(SUM(
                        COALESCE(s.chuyen_khoan, 0)
                        + COALESCE(s.qr, 0)
                        + COALESCE(s.pos, 0)
                    ), 0)
                    FROM cash_control_employee_summaries s
                    WHERE s.batch_id = v.his_batch_id
                )
            ), 0) AS non_cash_revenue_total,
            COALESCE(SUM(v.expense_count), 0) AS expense_count,
            COALESCE(SUM(v.expense_ksnb_total), 0) AS cash_expense_total
        FROM cash_control_vouchers v
        WHERE v.status IN ({status_sql})
          AND {date_filter_sql("v")}
    """

    with get_conn() as conn:
        row = conn.execute(
            sql,
            (*CASH_CONTROL_VOUCHER_STATUSES, start_text, end_text),
        ).fetchone()

    return {
        "receipt_count": int((row or {})["receipt_count"] or 0),
        "cash_revenue_total": float((row or {})["cash_revenue_total"] or 0),
        "non_cash_revenue_total": float((row or {})["non_cash_revenue_total"] or 0),
        "expense_count": int((row or {})["expense_count"] or 0),
        "cash_expense_total": float((row or {})["cash_expense_total"] or 0),
    }


def build_statistics(start_date: date, end_date: date) -> dict[str, Any]:
    important_stats = fetch_important_voucher_stats(start_date, end_date)
    cash_stats = fetch_cash_control_stats(start_date, end_date)

    return {
        **important_stats,
        **cash_stats,
    }


def build_report_text(start_date: date, end_date: date, stats: dict[str, Any]) -> str:
    return "\n".join(
        [
            "CÔNG TY TNHH BỆNH VIỆN HÙNG VƯƠNG GIA LAI",
            "BAN KIỂM SOÁT NỘI BỘ",
            "",
            "BÁO CÁO THỐNG KÊ KIỂM SOÁT THU, CHI",
            f"Thời gian thống kê: từ {date_text_vn(start_date)} đến {date_text_vn(end_date)}",
            "",
            "I. KIỂM SOÁT HỒ SƠ THU, CHI QUAN TRỌNG",
            "1. Hồ sơ đề nghị thanh toán",
            f"- Số chứng từ đã kiểm soát: {stats['dntt_document_count']}",
            f"- Tổng số tiền đã kiểm soát: {money_text(stats['dntt_checked_amount'])} đồng",
            "",
            "2. Hồ sơ vốn tự có",
            f"- Số chứng từ đã kiểm soát: {stats['own_capital_document_count']}",
            f"- Tổng số tiền đã kiểm soát: {money_text(stats['own_capital_checked_amount'])} đồng",
            "",
            "II. KIỂM SOÁT THU, CHI TIỀN MẶT",
            f"- Số phiếu thu tiền mặt đã kiểm soát: {stats['receipt_count']}",
            f"- Tổng số thu bằng tiền mặt đã kiểm soát: {money_text(stats['cash_revenue_total'])} đồng",
            f"- Tổng số thu không dùng tiền mặt đã kiểm soát: {money_text(stats['non_cash_revenue_total'])} đồng",
            f"- Số phiếu chi đã kiểm soát: {stats['expense_count']}",
            f"- Tổng số chi bằng tiền mặt đã kiểm soát: {money_text(stats['cash_expense_total'])} đồng",
            "",
        ]
    )


@router.get("")
def statistics_index(request: Request):
    denied = require_allowed_user(request)
    if denied:
        return denied

    start_date, end_date = resolve_date_range(request)
    stats = build_statistics(start_date, end_date)

    return templates.TemplateResponse(
        "control_statistics.html",
        {
            "request": request,
            "title": "Thống kê kiểm soát thu, chi",
            "start_date": date_input_value(start_date),
            "end_date": date_input_value(end_date),
            "start_date_text": date_text_vn(start_date),
            "end_date_text": date_text_vn(end_date),
            "quick_ranges": build_quick_ranges(),
            "stats": stats,
            "money_text": money_text,
        },
    )


@router.get("/export.txt")
def export_statistics_txt(request: Request):
    denied = require_allowed_user(request)
    if denied:
        return denied

    start_date, end_date = resolve_date_range(request)
    stats = build_statistics(start_date, end_date)
    report_text = build_report_text(start_date, end_date, stats)

    filename = f"bao_cao_thong_ke_kiem_soat_thu_chi_{date_input_value(start_date)}_{date_input_value(end_date)}.txt"

    return Response(
        content=report_text,
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )