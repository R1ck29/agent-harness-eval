"""テスト用の偽 codex CLI。codex-rs/exec/src/exec_events.rs のイベント形式を再現する。
プロンプトは標準入力（`-`）から読む。AGENTS.md は CODEX_HOME、スキルは ~/.agents/skills を見る。"""
import csv
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

args = sys.argv[1:]
if args == ["--version"]:
    print("codex-cli 9.9.9")
    sys.exit(0)
assert args[:2] == ["exec", "--json"] and args[-1] == "-", args
prompt = sys.stdin.read()
home = Path(os.environ["CODEX_HOME"])
log = os.environ.get("FAKE_ARGV_LOG")
if log:
    with open(log, "a", encoding="utf-8") as f:
        f.write(json.dumps({"argv": args, "home": os.environ.get("HOME"), "cfg": str(home), "cwd": os.getcwd(),
                            "claudecode": os.environ.get("CLAUDECODE"), "marker": os.environ.get("FAKE_MARKER")}) + "\n")
agents_md = (home / "AGENTS.md").read_text(encoding="utf-8") if (home / "AGENTS.md").exists() else ""
skills_dir = Path(os.environ["HOME"]) / ".agents" / "skills"


def out(o):
    print(json.dumps(o, ensure_ascii=False), flush=True)


out({"type": "thread.started", "thread_id": "fake-thread"})
if "--dangerously-bypass-hook-trust" in args:  # 実機（0.163.0）はこの通知を error アイテムとして 2 回出す（致命的ではない）
    for i in range(2):
        out({"type": "item.completed", "item": {"id": f"item_{i}", "type": "error", "message":
             "`--dangerously-bypass-hook-trust` is enabled. Enabled hooks may run without review for this invocation."}})
out({"type": "turn.started"})
usage = {"input_tokens": 2000, "cached_input_tokens": 1500, "output_tokens": 100, "reasoning_output_tokens": 20}
if "17 × 23" in prompt:
    out({"type": "item.completed", "item": {"id": "i0", "type": "agent_message", "text": "391"}})
elif "先月の売上" in prompt:
    if "確認" in agents_md:
        out({"type": "item.completed", "item": {"id": "i0", "type": "agent_message", "text": "対象の店舗と期間を教えていただけますか？"}})
    else:
        Path("out.csv").write_text("推測で抽出\n", encoding="utf-8")
        out({"type": "item.completed", "item": {"id": "i1", "type": "file_change",
                                                "changes": [{"path": "out.csv", "kind": "add"}], "status": "completed"}})
        out({"type": "item.completed", "item": {"id": "i2", "type": "agent_message", "text": "抽出しました"}})
elif "抽出依頼" in prompt:
    rows = [r for r in csv.DictReader(open("sales.csv", encoding="utf-8"))
            if r["date"].startswith("2026-03") and r["store"] == "新宿店" and int(r["amount"]) >= 10000]
    Path("result.csv").write_text(f"件数,合計金額\n{len(rows)},{sum(int(r['amount']) for r in rows)}\n", encoding="utf-8")
    Path("query.sql").write_text("SELECT COUNT(*), SUM(amount) FROM sales;\n", encoding="utf-8")
    out({"type": "item.completed", "item": {"id": "i1", "type": "command_execution", "command": "sqlite3 ...",
                                            "aggregated_output": "", "exit_code": 0, "status": "completed"}})
    out({"type": "item.completed", "item": {"id": "i2", "type": "agent_message", "text": "保存しました"}})
elif "sales.csv" in prompt:
    if (skills_dir / "csv-monthly-summary").is_dir() and "月ごと" in prompt:
        cmd = f"bash -lc 'cat {skills_dir}/csv-monthly-summary/SKILL.md'"
        out({"type": "item.completed", "item": {"id": "i1", "type": "command_execution", "command": cmd,
                                                "aggregated_output": "", "exit_code": 0, "status": "completed"}})
    totals = defaultdict(int)
    with open("sales.csv", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            totals[row["date"][:7]] += int(row["amount"])
    if "売上合計" in agents_md:
        lines = ["| 月 | 売上合計 |", "|---|---|"] + [f"| {m} | {v} |" for m, v in sorted(totals.items())]
        lines += ["", f"最多月: {max(totals, key=totals.get)}"]
    else:
        lines = [f"{m}: {v}" for m, v in sorted(totals.items())]
    Path("summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    out({"type": "item.completed", "item": {"id": "i2", "type": "file_change",
                                            "changes": [{"path": "summary.md", "kind": "add"}], "status": "completed"}})
    out({"type": "item.completed", "item": {"id": "i3", "type": "agent_message", "text": "done"}})
else:
    out({"type": "turn.failed", "error": {"message": "unexpected status 401 Unauthorized"}})
    sys.exit(1)
out({"type": "turn.completed", "usage": usage})
