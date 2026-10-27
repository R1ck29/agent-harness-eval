"""採点用: result.csv の件数・合計金額が、元データ（引数）から計算した正解と一致するか。

使い方: python check_extract.py <元の sales.csv>
正解: 2026年3月・新宿店・金額 10,000 円以上の取引の件数と合計金額。
列名・列の順番・桁区切りの有無は問わない（result.csv の中に、正解の件数と合計金額がどちらも数値として書かれていれば一致）。
"""
import csv
import re
import sys
from pathlib import Path

rows = [r for r in csv.DictReader(open(sys.argv[1], encoding="utf-8"))
        if r["date"].startswith("2026-03") and r["store"] == "新宿店" and int(r["amount"]) >= 10000]
count, total = len(rows), sum(int(r["amount"]) for r in rows)
result = Path("result.csv")
if not result.is_file():
    print("result.csv がありません")
    sys.exit(1)
# CSV としてセルごとに読む（列区切りのカンマを桁区切りと取り違えない。"73,900" のような桁区切りは許す）
nums = {int(x) for row in csv.reader(result.read_text(encoding="utf-8").splitlines())
        for cell in row for x in re.findall(r"\d+", cell.replace(",", ""))}
if count in nums and total in nums:
    print("一致")
    sys.exit(0)
print(f"不一致: 正解は件数 {count}・合計 {total}")
sys.exit(1)
