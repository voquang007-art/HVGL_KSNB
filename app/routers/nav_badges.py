from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..database import BOARD_ROLES, HEAD_ROLES, KSNB_ROLES, ROLE_ADMIN, get_conn

router = APIRouter(prefix="/api/nav-badges")


def _user_value(user: Any, field_name: str, default: Any = None) -> Any:
    if not user:
        return default

    try:
        return user[field_name]
    except Exception:
        return getattr(user, field_name, default)


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def _safe_count(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> int:
    try:
        row = conn.execute(sql, params).fetchone()
    except sqlite3.Error:
        return 0

    if not row:
        return 0

    try:
        return int(row["c"] or 0)
    except Exception:
        try:
            return int(row[0] or 0)
        except Exception:
            return 0


def _badge_value(count: int) -> str:
    if count <= 0:
        return ""
    if count == 1:
        return "●"
    if count > 99:
        return "99+"
    return str(count)


def _voucher_badge_count(conn: sqlite3.Connection, user_id: int, role_code: str) -> int:
    if not _table_exists(conn, "vouchers"):
        return 0

    if role_code == ROLE_ADMIN:
        return _safe_count(
            conn,
            """
            SELECT COUNT(*) AS c
            FROM vouchers
            WHERE status IN ('SUBMITTED_TO_HEAD', 'SUBMITTED_TO_BOARD')
            """,
        )

    if role_code in HEAD_ROLES:
        return _safe_count(
            conn,
            """
            SELECT COUNT(*) AS c
            FROM vouchers
            WHERE status = 'SUBMITTED_TO_HEAD'
              AND current_handler = ?
            """,
            (user_id,),
        )

    if role_code in BOARD_ROLES:
        return _safe_count(
            conn,
            """
            SELECT COUNT(*) AS c
            FROM vouchers
            WHERE status = 'SUBMITTED_TO_BOARD'
              AND (
                  current_handler = ?
                  OR current_handler IS NULL
              )
            """,
            (user_id,),
        )

    return 0


def _cash_control_badge_count(conn: sqlite3.Connection, user_id: int, role_code: str) -> int:
    if not _table_exists(conn, "cash_control_vouchers"):
        return 0

    if role_code == ROLE_ADMIN:
        return _safe_count(
            conn,
            """
            SELECT COUNT(*) AS c
            FROM cash_control_vouchers
            WHERE status IN ('SUBMITTED_TO_HEAD', 'SUBMITTED_TO_BOARD')
            """,
        )

    if role_code in HEAD_ROLES:
        return _safe_count(
            conn,
            """
            SELECT COUNT(*) AS c
            FROM cash_control_vouchers
            WHERE status = 'SUBMITTED_TO_HEAD'
              AND current_handler = ?
            """,
            (user_id,),
        )

    if role_code in BOARD_ROLES:
        return _safe_count(
            conn,
            """
            SELECT COUNT(*) AS c
            FROM cash_control_vouchers
            WHERE status = 'SUBMITTED_TO_BOARD'
              AND (
                  current_handler = ?
                  OR board_user_id = ?
                  OR current_handler IS NULL
              )
            """,
            (user_id, user_id),
        )

    return 0


def _chat_badge_info(conn: sqlite3.Connection, user_id: int) -> dict[str, Any]:
    empty_result = {
        "count": 0,
        "display": "",
        "unread_count": 0,
        "new_group_count": 0,
        "title": "",
    }

    if not (
        _table_exists(conn, "chat_groups")
        and _table_exists(conn, "chat_group_members")
        and _table_exists(conn, "chat_messages")
    ):
        return empty_result

    user_key = str(user_id)

    unread_count = _safe_count(
        conn,
        """
        SELECT COUNT(*) AS c
        FROM chat_messages m
        JOIN chat_group_members gm ON gm.group_id = m.group_id
        JOIN chat_groups g ON g.id = m.group_id
        WHERE gm.user_id = ?
          AND g.is_active = 1
          AND COALESCE(g.group_type, '') != 'MEETING'
          AND m.sender_user_id != ?
          AND COALESCE(m.deleted_by_owner, 0) = 0
          AND (
              gm.last_read_at IS NULL
              OR m.created_at > gm.last_read_at
          )
        """,
        (user_key, user_key),
    )

    new_group_count = _safe_count(
        conn,
        """
        SELECT COUNT(*) AS c
        FROM chat_group_members gm
        JOIN chat_groups g ON g.id = gm.group_id
        WHERE gm.user_id = ?
          AND g.is_active = 1
          AND COALESCE(g.group_type, '') != 'MEETING'
          AND COALESCE(gm.is_new_group, 0) = 1
        """,
        (user_key,),
    )

    total_count = unread_count + new_group_count
    if total_count <= 0:
        return empty_result

    title_parts: list[str] = []
    if unread_count > 0:
        title_parts.append(f"Có {unread_count} tin nhắn mới chưa đọc")
    if new_group_count > 0:
        title_parts.append(f"Có {new_group_count} nhóm mới được thêm vào")

    return {
        "count": total_count,
        "display": "99+" if total_count > 99 else str(total_count),
        "unread_count": unread_count,
        "new_group_count": new_group_count,
        "title": "; ".join(title_parts),
    }


def _meeting_badge_info(conn: sqlite3.Connection, user_id: int) -> dict[str, Any]:
    empty_result = {
        "count": 0,
        "display": "",
        "pending_invite_count": 0,
        "title": "",
    }

    if not (_table_exists(conn, "chat_meetings") and _table_exists(conn, "chat_meeting_attendances")):
        return empty_result

    pending_invite_count = _safe_count(
        conn,
        """
        SELECT COUNT(*) AS c
        FROM chat_meetings m
        JOIN chat_meeting_attendances a ON a.meeting_id = m.id
        WHERE a.user_id = ?
          AND COALESCE(m.meeting_status, 'UPCOMING') IN ('UPCOMING', 'LIVE')
          AND COALESCE(a.attendance_status, 'PENDING') = 'PENDING'
        """,
        (str(user_id),),
    )

    if pending_invite_count <= 0:
        return empty_result

    return {
        "count": pending_invite_count,
        "display": "99+" if pending_invite_count > 99 else str(pending_invite_count),
        "pending_invite_count": pending_invite_count,
        "title": f"Có {pending_invite_count} cuộc họp được mời, chờ báo vắng hoặc điểm danh",
    }


def _draft_approval_badge_info(conn: sqlite3.Connection, user_id: int) -> dict[str, Any]:
    empty_result = {
        "count": 0,
        "display": "",
        "submitted_count": 0,
        "returned_count": 0,
        "title": "",
    }

    if not _table_exists(conn, "document_drafts"):
        return empty_result

    submitted_count = _safe_count(
        conn,
        """
        SELECT COUNT(*) AS c
        FROM document_drafts
        WHERE COALESCE(is_deleted, 0) = 0
          AND current_handler_user_id = ?
          AND current_status IN ('SUBMITTED_TO_KSNB_MANAGER', 'SUBMITTED_TO_HDTV')
        """,
        (user_id,),
    )

    returned_count = _safe_count(
        conn,
        """
        SELECT COUNT(*) AS c
        FROM document_drafts
        WHERE COALESCE(is_deleted, 0) = 0
          AND current_handler_user_id = ?
          AND current_status = 'RETURNED_FOR_EDIT'
        """,
        (user_id,),
    )

    total_count = submitted_count + returned_count
    if total_count <= 0:
        return empty_result

    title_parts: list[str] = []
    if submitted_count > 0:
        title_parts.append(f"Có {submitted_count} văn bản được trình chờ xử lý")
    if returned_count > 0:
        title_parts.append(f"Có {returned_count} văn bản bị trả lại chờ chỉnh sửa/trình lại")

    return {
        "count": total_count,
        "display": "99+" if total_count > 99 else str(total_count),
        "submitted_count": submitted_count,
        "returned_count": returned_count,
        "title": "; ".join(title_parts),
    }


@router.get("")
def nav_badges(request: Request):
    user = request.state.user
    if not user:
        return JSONResponse(
            {
                "ok": False,
                "badges": {},
            }
        )

    user_id = int(_user_value(user, "id", 0) or 0)
    role_code = str(_user_value(user, "role_code", "") or "").strip().upper()

    with get_conn() as conn:
        voucher_count = _voucher_badge_count(conn, user_id, role_code)
        cash_control_count = _cash_control_badge_count(conn, user_id, role_code)
        chat_badge = _chat_badge_info(conn, user_id)
        meeting_badge = _meeting_badge_info(conn, user_id)
        draft_badge = _draft_approval_badge_info(conn, user_id)

    badges = {
        "vouchers": {
            "count": voucher_count,
            "display": _badge_value(voucher_count),
            "title": f"Có {voucher_count} phiếu kiểm soát hồ sơ thu, chi quan trọng mới chờ xử lý" if voucher_count > 0 else "",
        },
        "cash_control": {
            "count": cash_control_count,
            "display": _badge_value(cash_control_count),
            "title": f"Có {cash_control_count} phiếu kiểm soát thu, chi tiền mặt mới chờ xử lý" if cash_control_count > 0 else "",
        },
        "chat": chat_badge,
        "meetings": meeting_badge,
        "draft_approvals": draft_badge,
    }

    return JSONResponse(
        {
            "ok": True,
            "badges": badges,
        }
    )