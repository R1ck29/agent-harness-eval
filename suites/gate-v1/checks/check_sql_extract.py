"""gate-sql-extract-edge の採点: result.csv が正解と一致し、query.sql を再実行しても同じ結果になるか。

使い方: python check_sql_extract.py <元の shop.db>
条件: 2026-03 に注文された・status が cancelled でない・amount が NULL でない・同一 order_no は id が最小の行だけ採用し、
      その行の amount が 5000 以上の注文。amount の大きい順（同額は order_no の昇順）。列は order_no, customer, amount。
"""
import csv
import sqlite3
import sys
from pathlib import Path


def expected(db: Path) -> list[tuple[str, str, int]]:
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    first: dict[str, tuple] = {}
    for row in con.execute("SELECT id, order_no, customer, ordered_at, amount, status FROM orders ORDER BY id"):
        first.setdefault(row[1], row)
    con.close()
    rows = [(r[1], r[2], r[4]) for r in first.values()
            if r[3].startswith("2026-03") and r[5] != "cancelled" and r[4] is not None and r[4] >= 5000]
    return sorted(rows, key=lambda r: (-r[2], r[0]))


def fail(msg: str) -> None:
    print(msg)
    sys.exit(1)


def main() -> None:
    db = Path(sys.argv[1])
    want = expected(db)
    result = Path("result.csv")
    if not result.is_file():
        fail("result.csv がありません")
    got = list(csv.reader(result.read_text(encoding="utf-8").splitlines()))
    if not got or [c.strip() for c in got[0]] != ["order_no", "customer", "amount"]:
        fail(f"ヘッダが order_no,customer,amount ではありません: {got[:1]}")
    try:
        got_rows = [(r[0].strip(), r[1].strip(), int(r[2].strip().replace(",", ""))) for r in got[1:] if r]
    except (IndexError, ValueError):
        fail("result.csv の行を読めません")
    if got_rows != want:
        fail(f"result.csv が正解と違います（正解 {len(want)} 件・実際 {len(got_rows)} 件）")
    sql_file = Path("query.sql")
    if not sql_file.is_file():
        fail("query.sql がありません")
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)  # 元の DB を読み取り専用で開く（再実行しても書き換えられない）
    rows: list[tuple] = []
    buf = ""
    try:
        for line in sql_file.read_text(encoding="utf-8").splitlines(keepends=True):
            buf += line
            if sqlite3.complete_statement(buf):
                rows = con.execute(buf).fetchall()
                buf = ""
        if buf.strip():
            rows = con.execute(buf).fetchall()
    except sqlite3.Error as e:
        fail(f"query.sql の再実行に失敗: {e}")
    if [tuple(r[:3]) for r in rows] != want:
        fail("query.sql を再実行した結果が正解と違います")
    print("一致")


main()
