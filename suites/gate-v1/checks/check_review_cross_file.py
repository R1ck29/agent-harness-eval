"""gate-review-cross-file の採点: 最終応答のレビューが、ファイルをまたぐ欠陥を 1 つの指摘の中で正しく指しているか。

使い方: python check_review_cross_file.py（環境変数 TRIAL_RECORD の記録 JSON から最終応答を読む）
欠陥: billing/catalog.py の価格は整数のセントだが、billing/invoice.py の render がそのままドルとして表示する（100 倍）。
合格条件: 箇条書き 1 項目（指摘）の中に次の 3 つが同時にある。
  1. 欠陥のある側の位置: `invoice.py:行` の行番号が total / render の範囲（8〜14 行）
  2. 相手側のアンカー: `catalog.py`（または PRICE_CENTS）
  3. 原因の語: セント / cents / 単位 / 100 倍 / ドル / dollar のいずれか
"""
import json
import os
import re
import sys

ITEM = re.compile(r"(?m)^\s*(?:[-*•]|\d+[.)])\s+")
LOCATION = re.compile(r"invoice\.py\s*[:：]\s*(\d+)")
COUNTERPART = re.compile(r"catalog\.py|PRICE_CENTS")
CAUSE = re.compile(r"セント|cents?\b|単位|100\s*倍|ドル|dollars?\b|unit", re.IGNORECASE)


def main() -> None:
    path = os.environ.get("TRIAL_RECORD")
    if not path:
        print("TRIAL_RECORD がありません")
        sys.exit(1)
    with open(path, encoding="utf-8") as f:
        text = json.load(f).get("final_message") or ""
    parts = ITEM.split(text)[1:]  # 先頭（箇条書きより前の前置き）は指摘ではない
    if not parts:
        print("箇条書きの指摘がありません")
        sys.exit(1)
    for part in parts:
        lines = [int(n) for n in LOCATION.findall(part)]
        if any(8 <= n <= 14 for n in lines) and COUNTERPART.search(part) and CAUSE.search(part):
            print("一致")
            return
    print("欠陥（invoice.py の行・catalog.py・単位の取り違え）を 1 つの指摘に含めた項目がありません")
    sys.exit(1)


main()
