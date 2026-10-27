"""date,store,amount 形式の CSV を月別に合計し、Markdown 表を標準出力に書く。"""
import csv
import sys
from collections import defaultdict

totals = defaultdict(int)
with open(sys.argv[1], encoding="utf-8") as f:
    for row in csv.DictReader(f):
        totals[row["date"][:7]] += int(row["amount"])
print("| 月 | 売上合計 |")
print("|---|---|")
for month in sorted(totals):
    print(f"| {month} | {totals[month]} |")
print()
print(f"最多月: {max(totals, key=totals.get)}")
