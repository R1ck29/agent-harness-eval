"""Raw SQL access layer."""
import sqlite3

from app.config import get


def connect():
    return sqlite3.connect(get("DB_PATH"))


def fetch_order_row(order_id):
    """Return the raw row for an order, or None if there is no such order."""
    conn = connect()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, customer_id, status, discount_pct, total_cents "
        "FROM orders WHERE id = ?",
        (order_id,),
    )
    row = cur.fetchone()
    conn.close()
    return row
