"""採点用: 作業ディレクトリの summary.md の月別合計が、元のフィクスチャから計算した正解と一致するか。

使い方: python check_monthly_totals.py <元の sales.csv>
正解は引数の「元のフィクスチャ」から計算する（作業ディレクトリの sales.csv はエージェントが書き換えられるため使わない）。

表の形式（ヘッダ名・列の並び・カンマ・箇条書き）は問わない。
判定: 各月について「その月（YYYY-MM / YYYY/MM / YYYY年M月）で始まる行」のどこかに、
正解の合計値がひとつの数値として書かれていれば一致とみなす。全月一致で終了コード 0。
（説明文中の「2026年1月〜3月、全96件」のような行を拾わないよう、行頭の月だけを見る）
"""
import csv
import re
import sys
from collections import defaultdict
from pathlib import Path

if len(sys.argv) != 2:
    print("使い方: check_monthly_totals.py <元の sales.csv>")
    sys.exit(2)
wd = Path.cwd()
truth = defaultdict(int)
with open(sys.argv[1], encoding="utf-8") as f:
    for row in csv.DictReader(f):
        truth[row["date"][:7]] += int(row["amount"])
if not truth:
    print("正解データが空です")
    sys.exit(2)
summary = wd / "summary.md"
if not summary.is_file():
    print("summary.md がありません")
    sys.exit(1)
seen = defaultdict(set)
for line in summary.read_text(encoding="utf-8").splitlines():
    head = line.lstrip(" \t|-*>").replace("*", "")
    m = re.match(r"(20\d\d)\s*[-/年]\s*(\d{1,2})(?!\d)", head)
    if not m:
        continue
    month = f"{m.group(1)}-{int(m.group(2)):02d}"
    nums = {int(x.replace(",", "")) for x in re.findall(r"\d[\d,]*", head[m.end():])}
    seen[month] |= nums
bad = [f"{k}: 正解 {v}" + ("（記載なし）" if k not in seen else "（一致する数値なし）")
       for k, v in sorted(truth.items()) if v not in seen.get(k, set())]
if bad:
    print("不一致: " + "; ".join(bad))
    sys.exit(1)
print("全月一致")
