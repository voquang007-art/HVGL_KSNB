from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

BASE_DIR = Path(__file__).resolve().parent
ROOT_DIR = BASE_DIR.parent
INSTANCE_DIR = ROOT_DIR / "instance"
STORAGE_DIR = BASE_DIR / "storage"
IMPORT_DIR = STORAGE_DIR / "imports"
EXPORT_DIR = STORAGE_DIR / "exports"
TEMPLATE_DIR = BASE_DIR / "template_excel"
DB_PATH = INSTANCE_DIR / "ksnb.sqlite3"

DATABASE_URL = f"sqlite:///{DB_PATH.as_posix()}"

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
    future=True,
)

SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine,
)

Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

ROLE_ADMIN = "ADMIN"
ROLE_TRUONG_BAN_KSNB = "TRUONG_BAN_KSNB"
ROLE_PHO_TRUONG_BAN_KSNB = "PHO_TRUONG_BAN_KSNB"
ROLE_NHAN_VIEN_KSNB = "NHAN_VIEN_KSNB"
ROLE_TONG_GIAM_DOC = "TONG_GIAM_DOC"
ROLE_PHO_TGD_THUONG_TRUC = "PHO_TGD_THUONG_TRUC"
ROLE_PHO_TONG_GIAM_DOC = "PHO_TONG_GIAM_DOC"
ROLE_THANH_VIEN_TRUNG_TAP = "THANH_VIEN_TRUNG_TAP"

UNIT_KSNB = "BAN_KSNB"
UNIT_HDTV = "HOI_DONG_THANH_VIEN"
UNIT_TRUNG_TAP = "THANH_VIEN_TRUNG_TAP"

ROLE_LABELS = {
    ROLE_ADMIN: "Admin",
    ROLE_TRUONG_BAN_KSNB: "Trưởng ban KSNB",
    ROLE_PHO_TRUONG_BAN_KSNB: "Phó Trưởng ban KSNB",
    ROLE_NHAN_VIEN_KSNB: "Nhân viên Ban KSNB",
    ROLE_TONG_GIAM_DOC: "Tổng Giám đốc",
    ROLE_PHO_TGD_THUONG_TRUC: "Phó Tổng Giám đốc thường trực",
    ROLE_PHO_TONG_GIAM_DOC: "Phó Tổng Giám đốc",
    ROLE_THANH_VIEN_TRUNG_TAP: "Thành viên trưng tập",
}

UNIT_LABELS = {
    UNIT_KSNB: "Ban Kiểm soát nội bộ",
    UNIT_HDTV: "Hội đồng thành viên",
    UNIT_TRUNG_TAP: "Thành viên trưng tập",
}

KSNB_ROLES = {ROLE_TRUONG_BAN_KSNB, ROLE_PHO_TRUONG_BAN_KSNB, ROLE_NHAN_VIEN_KSNB}
HEAD_ROLES = {ROLE_TRUONG_BAN_KSNB, ROLE_PHO_TRUONG_BAN_KSNB}
BOARD_ROLES = {ROLE_TONG_GIAM_DOC, ROLE_PHO_TGD_THUONG_TRUC, ROLE_PHO_TONG_GIAM_DOC}


def ensure_dirs() -> None:
    INSTANCE_DIR.mkdir(parents=True, exist_ok=True)
    IMPORT_DIR.mkdir(parents=True, exist_ok=True)
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)


def _legacy_hash_password(password: str) -> str:
    salt = os.environ.get("HVGL_KSNB_PASSWORD_SALT", "HVGL_KSNB_2026")
    return hashlib.sha256((salt + password).encode("utf-8")).hexdigest()


def hash_password(password: str) -> str:
    iterations = int(os.environ.get("HVGL_KSNB_PASSWORD_ITERATIONS", "260000"))
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        iterations,
    ).hex()
    return f"pbkdf2_sha256${iterations}${salt}${digest}"


def verify_password(password: str, password_hash: str) -> bool:
    if not password_hash:
        return False

    if password_hash.startswith("pbkdf2_sha256$"):
        try:
            _, iterations_text, salt, expected_digest = password_hash.split("$", 3)
            iterations = int(iterations_text)
            actual_digest = hashlib.pbkdf2_hmac(
                "sha256",
                password.encode("utf-8"),
                salt.encode("utf-8"),
                iterations,
            ).hex()
            return hmac.compare_digest(actual_digest, expected_digest)
        except Exception:
            return False

    return hmac.compare_digest(_legacy_hash_password(password), password_hash)


def password_needs_rehash(password_hash: str) -> bool:
    return not (password_hash or "").startswith("pbkdf2_sha256$")


def get_conn() -> sqlite3.Connection:
    ensure_dirs()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def execute(sql: str, params: Iterable[Any] = ()) -> None:
    with get_conn() as conn:
        conn.execute(sql, tuple(params))
        conn.commit()


def init_chat_db() -> None:
    """
    Tạo các bảng phục vụ chat bằng SQLAlchemy trên cùng file SQLite ksnb.sqlite3.

    Không thay đổi nghiệp vụ chat hiện có; chỉ bổ sung lớp tương thích để các file
    chat.py, chat_api.py, service.py, app/chat/models.py dùng được với HVGL_KSNB.
    """
    try:
        import app.models  # noqa: F401
        import app.chat.models  # noqa: F401

        Base.metadata.create_all(bind=engine)
    except Exception:
        raise


def init_db() -> None:
    ensure_dirs()
    with get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                full_name TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                unit_code TEXT NOT NULL DEFAULT 'THANH_VIEN_TRUNG_TAP',
                role_code TEXT NOT NULL DEFAULT 'THANH_VIEN_TRUNG_TAP',
                position_title TEXT NOT NULL DEFAULT 'Thành viên trưng tập',
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS login_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                ip_address TEXT,
                success INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_login_attempts_username_created
            ON login_attempts(username, created_at);

            CREATE TABLE IF NOT EXISTS vouchers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT UNIQUE,
                title TEXT NOT NULL,
                hồ_so_type TEXT NOT NULL,
                total_amount REAL DEFAULT 0,
                submitting_unit TEXT,
                sender_name TEXT,
                received_at TEXT,
                approval_target TEXT,
                route_mode TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'DRAFT',
                section_iv_result TEXT,
                section_iv_note TEXT,
                section_v_text TEXT,
                section_vi_text TEXT,
                created_by INTEGER NOT NULL,
                current_handler INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                submitted_at TEXT,
                submitted_to_board_at TEXT,
                board_saved_at TEXT,
                FOREIGN KEY(created_by) REFERENCES users(id),
                FOREIGN KEY(current_handler) REFERENCES users(id)
            );

            CREATE TABLE IF NOT EXISTS voucher_review_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                voucher_id INTEGER NOT NULL,
                item_order INTEGER NOT NULL,
                content TEXT NOT NULL,
                note TEXT,
                FOREIGN KEY(voucher_id) REFERENCES vouchers(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS voucher_documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                voucher_id INTEGER NOT NULL,
                source_type TEXT NOT NULL,
                supplier_code TEXT,
                supplier TEXT,
                content TEXT,
                document_amount REAL DEFAULT 0,
                ksnb_check_type TEXT NOT NULL DEFAULT 'MATCH',
                ksnb_checked_amount REAL,
                ksnb_note TEXT,
                payment_transfer INTEGER NOT NULL DEFAULT 0,
                payment_cash INTEGER NOT NULL DEFAULT 0,
                row_order INTEGER NOT NULL,
                FOREIGN KEY(voucher_id) REFERENCES vouchers(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS voucher_import_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                voucher_id INTEGER NOT NULL,
                source_type TEXT NOT NULL,
                original_name TEXT NOT NULL,
                stored_name TEXT NOT NULL,
                uploaded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(voucher_id) REFERENCES vouchers(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS voucher_payable_warnings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                voucher_id INTEGER NOT NULL,
                supplier_name TEXT NOT NULL,
                supplier_key TEXT NOT NULL,
                source_types TEXT,
                control_payment_amount REAL NOT NULL DEFAULT 0,
                payable_amount REAL NOT NULL DEFAULT 0,
                over_amount REAL NOT NULL DEFAULT 0,
                warning_level TEXT NOT NULL DEFAULT 'OVER_PAYABLE',
                warning_message TEXT,
                matched_payable_name TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(voucher_id) REFERENCES vouchers(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS voucher_payable_details (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                voucher_id INTEGER NOT NULL,
                supplier_code TEXT,
                supplier_name TEXT NOT NULL,
                supplier_key TEXT NOT NULL,
                opening_debit REAL NOT NULL DEFAULT 0,
                opening_credit REAL NOT NULL DEFAULT 0,
                period_debit REAL NOT NULL DEFAULT 0,
                period_credit REAL NOT NULL DEFAULT 0,
                ending_debit REAL NOT NULL DEFAULT 0,
                ending_credit REAL NOT NULL DEFAULT 0,
                payable_amount REAL NOT NULL DEFAULT 0,
                row_order INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(voucher_id) REFERENCES vouchers(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS voucher_signatures (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                voucher_id INTEGER NOT NULL,
                signer_id INTEGER,
                signer_name TEXT NOT NULL,
                signer_role TEXT NOT NULL,
                signature_text TEXT NOT NULL,
                signed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                hash_value TEXT,
                FOREIGN KEY(voucher_id) REFERENCES vouchers(id) ON DELETE CASCADE,
                FOREIGN KEY(signer_id) REFERENCES users(id)
            );

            CREATE TABLE IF NOT EXISTS voucher_routes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                voucher_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                from_user_id INTEGER,
                to_user_id INTEGER,
                note TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(voucher_id) REFERENCES vouchers(id) ON DELETE CASCADE,
                FOREIGN KEY(from_user_id) REFERENCES users(id),
                FOREIGN KEY(to_user_id) REFERENCES users(id)
            );
            CREATE TABLE IF NOT EXISTS cash_control_batches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                original_filename TEXT,
                report_date TEXT,
                created_by INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(created_by) REFERENCES users(id)
            );

            CREATE TABLE IF NOT EXISTS cash_control_employee_summaries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id INTEGER NOT NULL,
                employee_code TEXT,
                employee_name TEXT,
                patient_count INTEGER NOT NULL DEFAULT 0,
                txn_count INTEGER NOT NULL DEFAULT 0,
                tien_thu REAL NOT NULL DEFAULT 0,
                tam_ung REAL NOT NULL DEFAULT 0,
                tien_chi REAL NOT NULL DEFAULT 0,
                mien_giam REAL NOT NULL DEFAULT 0,
                huy_phieu REAL NOT NULL DEFAULT 0,
                thuc_thu REAL NOT NULL DEFAULT 0,
                tien_mat REAL NOT NULL DEFAULT 0,
                chuyen_khoan REAL NOT NULL DEFAULT 0,
                qr REAL NOT NULL DEFAULT 0,
                pos REAL NOT NULL DEFAULT 0,
                FOREIGN KEY(batch_id) REFERENCES cash_control_batches(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS cash_control_cashbook_batches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                original_filename TEXT,
                created_by INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(created_by) REFERENCES users(id)
            );

            CREATE TABLE IF NOT EXISTS cash_control_cashbook_summaries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id INTEGER NOT NULL,
                summary_type TEXT NOT NULL,
                item_name TEXT NOT NULL,
                voucher_count INTEGER NOT NULL DEFAULT 0,
                amount REAL NOT NULL DEFAULT 0,
                FOREIGN KEY(batch_id) REFERENCES cash_control_cashbook_batches(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS cash_control_cashbook_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id INTEGER NOT NULL,
                doc_type TEXT NOT NULL,
                voucher_no TEXT,
                voucher_date TEXT,
                item_name TEXT NOT NULL,
                debit_code TEXT,
                credit_code TEXT,
                description TEXT,
                amount REAL NOT NULL DEFAULT 0,
                FOREIGN KEY(batch_id) REFERENCES cash_control_cashbook_batches(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS cash_control_accounting_batches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                original_filename TEXT,
                created_by INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(created_by) REFERENCES users(id)
            );

            CREATE TABLE IF NOT EXISTS cash_control_accounting_summaries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id INTEGER NOT NULL,
                item_name TEXT NOT NULL,
                amount REAL NOT NULL DEFAULT 0,
                note TEXT,
                FOREIGN KEY(batch_id) REFERENCES cash_control_accounting_batches(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS cash_control_accounting_employee_summaries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id INTEGER NOT NULL,
                employee_code TEXT,
                employee_name TEXT,
                his_cash_amount REAL NOT NULL DEFAULT 0,
                accounting_cash_amount REAL NOT NULL DEFAULT 0,
                diff_amount REAL NOT NULL DEFAULT 0,
                note TEXT,
                FOREIGN KEY(batch_id) REFERENCES cash_control_accounting_batches(id) ON DELETE CASCADE
            );            
            """
        )
        doc_cols = {row["name"] for row in conn.execute("PRAGMA table_info(voucher_documents)").fetchall()}
        if "supplier_code" not in doc_cols:
            conn.execute("ALTER TABLE voucher_documents ADD COLUMN supplier_code TEXT")
        if "payment_transfer" not in doc_cols:
            conn.execute("ALTER TABLE voucher_documents ADD COLUMN payment_transfer INTEGER NOT NULL DEFAULT 0")
        if "payment_cash" not in doc_cols:
            conn.execute("ALTER TABLE voucher_documents ADD COLUMN payment_cash INTEGER NOT NULL DEFAULT 0")

        cash_summary_cols = {row["name"] for row in conn.execute("PRAGMA table_info(cash_control_employee_summaries)").fetchall()}
        if "patient_count" not in cash_summary_cols:
            conn.execute("ALTER TABLE cash_control_employee_summaries ADD COLUMN patient_count INTEGER NOT NULL DEFAULT 0")
            
        count = conn.execute("SELECT COUNT(*) AS c FROM users WHERE role_code = ?", (ROLE_ADMIN,)).fetchone()["c"]
        if count == 0:
            admin_username = os.environ.get("HVGL_KSNB_ADMIN_USERNAME", "admin").strip() or "admin"
            admin_full_name = os.environ.get("HVGL_KSNB_ADMIN_FULL_NAME", "Quản trị hệ thống").strip() or "Quản trị hệ thống"
            admin_password = os.environ.get("HVGL_KSNB_ADMIN_PASSWORD", "").strip()
            if not admin_password:
                admin_password = secrets.token_urlsafe(18)
                print(
                    "HVGL_KSNB: Chưa cấu hình HVGL_KSNB_ADMIN_PASSWORD. "
                    f"Hệ thống đã tạo mật khẩu admin tạm thời: {admin_password}"
                )

            conn.execute(
                """
                INSERT INTO users(username, full_name, password_hash, unit_code, role_code, position_title, is_active)
                VALUES (?, ?, ?, ?, ?, ?, 1)
                """,
                (admin_username, admin_full_name, hash_password(admin_password), UNIT_KSNB, ROLE_ADMIN, "Admin"),
            )
        conn.commit()

    init_chat_db()


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return dict(row)


def fetch_user(user_id: int | None) -> dict[str, Any] | None:
    if not user_id:
        return None
    with get_conn() as conn:
        return row_to_dict(conn.execute("SELECT * FROM users WHERE id = ? AND is_active = 1", (user_id,)).fetchone())


def can_create_voucher(user: dict[str, Any]) -> bool:
    return user.get("role_code") in KSNB_ROLES or user.get("role_code") == ROLE_ADMIN


def can_head_approve(user: dict[str, Any]) -> bool:
    return user.get("role_code") in HEAD_ROLES or user.get("role_code") == ROLE_ADMIN


def can_board_view(user: dict[str, Any]) -> bool:
    return user.get("role_code") in BOARD_ROLES or user.get("role_code") == ROLE_ADMIN


def voucher_hash(voucher_id: int) -> str:
    with get_conn() as conn:
        voucher = conn.execute("SELECT * FROM vouchers WHERE id = ?", (voucher_id,)).fetchone()
        docs = conn.execute("SELECT * FROM voucher_documents WHERE voucher_id = ? ORDER BY row_order", (voucher_id,)).fetchall()
    raw = repr(dict(voucher)) + repr([dict(x) for x in docs])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
