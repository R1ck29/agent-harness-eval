"""gate-sql-extract-edge の SQLite フィクスチャ（shop.db）を作る再生成用スクリプト（採点には使わない）。

使い方: python build_sql_fixture.py <出力先の shop.db>
行はすべて合成。重複（同じ order_no）・欠損（amount が NULL）・境界値（5000 ちょうど / 4999・月末の時刻付き）・
キャンセル・同額を含む。
"""
import sqlite3
import sys
from pathlib import Path

# (id, order_no, customer, ordered_at, amount, status)
ROWS = [
    (1, "A-1001", "alpha", "2026-02-28 23:59:59", 9000, "paid"),    # 2 月: 対象外
    (2, "A-1002", "bravo", "2026-03-01 00:00:00", 5000, "paid"),    # 境界: 5000 ちょうど・月初
    (3, "A-1003", "charlie", "2026-03-02 10:15:00", 4999, "paid"),  # 境界: 5000 未満
    (4, "A-1004", "delta", "2026-03-05 09:00:00", 12000, "paid"),
    (5, "A-1005", "echo", "2026-03-07 14:30:00", None, "paid"),     # 欠損
    (6, "A-1006", "foxtrot", "2026-03-10 11:00:00", 8000, "cancelled"),  # キャンセル
    (7, "A-1007", "golf", "2026-03-12 16:45:00", 8000, "paid"),
    (8, "A-1008", "hotel", "2026-03-15 08:20:00", 8000, "paid"),    # 同額（order_no 昇順で並ぶ）
    (9, "A-1009", "india", "2026-03-18 13:05:00", 5000, "paid"),    # 同額 5000
    (10, "A-1010", "juliet", "2026-03-21 17:10:00", 7300, "paid"),
    (11, "A-1011", "kilo", "2026-03-22 12:00:00", 4500, "paid"),    # 最初の行は 5000 未満（重複の後ろは 9000）
    (12, "A-1012", "lima", "2026-03-25 10:00:00", 6100, "paid"),
    (13, "A-1013", "mike", "2026-03-31 23:59:00", 5200, "paid"),    # 月末の時刻付き
    (14, "A-1014", "november", "2026-04-01 00:00:00", 15000, "paid"),  # 4 月: 対象外
    (15, "A-1004", "delta", "2026-03-05 09:00:00", 12000, "paid"),  # 重複（完全一致の再取り込み）
    (16, "A-1007", "golf", "2026-03-12 16:45:00", 3000, "paid"),    # 重複（金額違い。id の小さい行を採用）
    (17, "A-1011", "kilo", "2026-03-22 12:00:00", 9000, "paid"),    # 重複（最初の行は 4500 なので全体として対象外）
    (18, "A-1015", "oscar", "2026-03-27 19:40:00", 5001, "paid"),   # 境界: 5000 超
    (19, "A-1016", "papa", "2026-03-29 07:30:00", None, "paid"),    # 欠損
    (20, "A-1017", "quebec", "2026-03-30 15:00:00", 10500, "paid"),
]


def build(path: Path) -> None:
    path.unlink(missing_ok=True)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, order_no TEXT NOT NULL, customer TEXT NOT NULL, "
                "ordered_at TEXT NOT NULL, amount INTEGER, status TEXT NOT NULL)")
    con.executemany("INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?)", ROWS)
    con.commit()
    con.close()


if __name__ == "__main__":
    build(Path(sys.argv[1]))
