"""gate-refactor-preserve-api の採点: 公開 API・例外型・戻り値スキーマを保ったまま整理され、境界の不具合が直っているか。

使い方: python check_refactor_preserve_api.py（作業ディレクトリで実行。作業ディレクトリには書き込まない）
確認すること:
  1. 見えているテスト（tests/test_summary.py）が無改変で、かつ通る
  2. 公開名・引数名と種類と既定値（型注釈は問わない）・戻り値のキーと型・例外型が、整理前（正解の実装）と同じ
  3. tax_rate=0 を 0% として扱う（整理前は 0 が既定値 10% に化けていた）
  4. summarize_orders が 20 行以内で、`_` で始まる補助関数が 2 つ以上ある（実際に整理されている）
"""
import ast
import hashlib
import inspect
import subprocess
import sys
from pathlib import Path

# 見えているテストを LF にそろえた sha256（Windows の CRLF チェックアウトでも一致させる）
VISIBLE = {"tests/test_summary.py": "9eaf7eca528a393f1967976a8de866600a77dfbc85d7aed1235682e862a80f02"}

# 正解の挙動（整理前の実装 + tax_rate=0 の修正だけを入れたもの）
ORACLE = '''
def summarize_orders(orders, tax_rate=None):
    if not isinstance(orders, list):
        raise TypeError("orders must be a list")
    rate = 0.1 if tax_rate is None else tax_rate
    lines = 0
    subtotal = 0
    by_sku = {}
    for o in orders:
        if o["qty"] < 0:
            raise ValueError("negative qty for " + o["sku"])
        if o["unit_price"] < 0:
            raise ValueError("negative price for " + o["sku"])
        lines = lines + 1
        subtotal = subtotal + o["qty"] * o["unit_price"]
        by_sku[o["sku"]] = by_sku.get(o["sku"], 0) + o["qty"]
    tax = round(subtotal * rate, 2)
    total = round(subtotal + tax, 2)
    return {"lines": lines, "subtotal": round(subtotal, 2), "tax": tax, "total": total, "by_sku": by_sku}


def format_summary(summary):
    text = ""
    text = text + "lines: " + str(summary["lines"]) + "\\n"
    text = text + "subtotal: " + str(summary["subtotal"]) + "\\n"
    text = text + "tax: " + str(summary["tax"]) + "\\n"
    text = text + "total: " + str(summary["total"]) + "\\n"
    for sku in summary["by_sku"]:
        text = text + "  " + sku + " x" + str(summary["by_sku"][sku]) + "\\n"
    return text
'''

CASES = [
    ([], None),
    ([{"sku": "pen", "qty": 3, "unit_price": 120}, {"sku": "ink", "qty": 1, "unit_price": 450}], None),
    ([{"sku": "a", "qty": 3, "unit_price": 19.99}, {"sku": "b", "qty": 2, "unit_price": 0.1},
      {"sku": "a", "qty": 1, "unit_price": 19.99}], 0.08),
    ([{"sku": "z", "qty": 0, "unit_price": 999}, {"sku": "y", "qty": 4, "unit_price": 25}], 0.1),
    ([{"sku": "x", "qty": 7, "unit_price": 13}], 0.15),
    ([{"sku": "x", "qty": 7, "unit_price": 13}], 0),
    ([{"sku": "x", "qty": 2, "unit_price": 50}, {"sku": "w", "qty": 1, "unit_price": 5}], 0.0),
]
BAD = [
    ("orders must be a list", ("x",), TypeError),
    ("tuple is not a list", (({"sku": "a", "qty": 1, "unit_price": 1},),), TypeError),
    ("negative qty", ([{"sku": "a", "qty": -1, "unit_price": 1}],), ValueError),
    ("negative price", ([{"sku": "a", "qty": 1, "unit_price": -1}],), ValueError),
]


def params(fn) -> list:
    """引数の (名前, 種類, 既定値)。型注釈は比べない（注釈の追加は許す）。"""
    return [(p.name, p.kind, p.default) for p in inspect.signature(fn).parameters.values()]


def fail(msg: str) -> None:
    print(msg)
    sys.exit(1)


def main() -> None:
    root = Path.cwd()
    for rel, digest in VISIBLE.items():
        f = root / rel
        if not f.is_file() or hashlib.sha256(f.read_bytes().replace(b"\r\n", b"\n")).hexdigest() != digest:
            fail(f"見えているテスト {rel} が変更されています")
    r = subprocess.run([sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests", "-t", "."], cwd=root,
                       capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    if r.returncode != 0:
        fail("見えているテストが失敗: " + (r.stderr.strip().splitlines() or [""])[-1][:150])

    sys.dont_write_bytecode = True
    sys.path.insert(0, str(root))
    try:
        import orderkit
        from orderkit import summary
    except Exception as e:  # 壊れた実装は不合格
        fail(f"orderkit を読み込めません: {e!r}")
    oracle: dict = {}
    exec(ORACLE, oracle)
    for name in ("summarize_orders", "format_summary"):
        for mod in (orderkit, summary):
            if not callable(getattr(mod, name, None)):
                fail(f"{mod.__name__}.{name} がありません")
        if params(getattr(orderkit, name)) != params(oracle[name]):
            fail(f"{name} の引数が変わっています: {inspect.signature(getattr(orderkit, name))}")
    for orders, rate in CASES:
        args = (orders,) if rate is None else (orders, rate)
        want, got = oracle["summarize_orders"](*args), orderkit.summarize_orders(*args)
        if want != got or list(want) != list(got) or list(want["by_sku"]) != list(got["by_sku"]):  # 値・キーの順・by_sku の順序が同じ
            fail(f"戻り値が違います: 入力 {args} 期待 {want} 実際 {got}")
        if oracle["format_summary"](want) != orderkit.format_summary(got):
            fail(f"format_summary の出力が違います: 入力 {args}")
    for label, args, exc in BAD:
        try:
            orderkit.summarize_orders(*args)
        except exc:
            continue
        except Exception as e:
            fail(f"{label}: 例外型が違います（期待 {exc.__name__}・実際 {type(e).__name__}）")
        fail(f"{label}: 例外が出ません")

    tree = ast.parse((root / "orderkit" / "summary.py").read_text(encoding="utf-8"))
    funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    main_fn = funcs.get("summarize_orders")
    if main_fn is None:
        fail("summarize_orders の定義が見つかりません")
    length = main_fn.end_lineno - main_fn.lineno + 1
    helpers = [n for n in funcs if n.startswith("_")]
    if length > 20 or len(helpers) < 2:
        fail(f"整理されていません（summarize_orders {length} 行・補助関数 {len(helpers)} 個）")
    print("一致")


main()
