"""
db.py - SQLite audit log cho trade executions.
Dùng sqlite3 built-in, không ORM.
"""
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "audit_log.db"


def _get_connection() -> sqlite3.Connection:
    """Tạo connection với row_factory để trả về dict."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Tạo table trade_logs nếu chưa tồn tại."""
    conn = _get_connection()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trade_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                ticker TEXT NOT NULL,
                action TEXT NOT NULL,
                volume TEXT NOT NULL,
                price REAL NOT NULL,
                reason TEXT
            )
        """)
        conn.commit()
    finally:
        conn.close()


def insert_log(order: dict) -> None:
    """Insert 1 record vào audit log.

    Args:
        order: dict chứa keys: ticker, action, volume, price, reason
    """
    conn = _get_connection()
    try:
        conn.execute(
            """
            INSERT INTO trade_logs (ticker, action, volume, price, reason)
            VALUES (:ticker, :action, :volume, :price, :reason)
            """,
            {
                "ticker": order["ticker"],
                "action": order["action"],
                "volume": str(order["volume"]),
                "price": order["price"],
                "reason": order.get("reason", ""),
            },
        )
        conn.commit()
    finally:
        conn.close()


def get_logs(limit: int = 20) -> list[dict]:
    """Đọc audit log gần nhất.

    Args:
        limit: Số lượng record tối đa trả về.

    Returns:
        List of dict, mỗi dict là 1 trade log record.
    """
    conn = _get_connection()
    try:
        cursor = conn.execute(
            "SELECT id, timestamp, ticker, action, volume, price, reason "
            "FROM trade_logs ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        return [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()


def get_all_logs_for_analysis(days: int = 30) -> list[dict]:
    """Đọc toàn bộ lịch sử giao dịch trong N ngày gần nhất cho Analyst Agent.

    Khác với get_logs(), hàm này không giới hạn số lượng record
    và lọc theo khoảng thời gian để phục vụ phân tích hiệu suất dài hạn.

    Args:
        days: Số ngày nhìn lại (mặc định 30 ngày).

    Returns:
        List of dict, mỗi dict là 1 trade log record, sắp xếp theo thời gian tăng dần.
    """
    conn = _get_connection()
    try:
        cursor = conn.execute(
            "SELECT id, timestamp, ticker, action, volume, price, reason "
            "FROM trade_logs "
            "WHERE timestamp >= datetime('now', ? || ' days') "
            "ORDER BY id ASC",
            (f"-{days}",),
        )
        return [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()

