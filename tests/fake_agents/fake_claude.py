"""テスト用の偽 claude CLI。実機（v2.1.304）で観測した stream-json の形を再現する。

振る舞いはプロンプトと CLAUDE_CONFIG_DIR の中身で決まる:
- FAKE_TIMEOUT を含む → 長時間 sleep（タイムアウト試験）
- FAKE_RATELIMIT を含む → 初回は 429 で失敗、2 回目以降成功（FAKE_STATE_DIR にカウンタ）
- sales.csv を含む → 月別集計。CLAUDE.md に「売上合計」があればその形式で書く。
  スキルがあり「月ごと」を含めば Skill ツールを呼ぶ
- 17 × 23 → 391 と答える
- 判定（--tools "" かつ json 出力）→ 対象に「2026-03」と店舗名があれば PASS
"""
import csv
import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

args = sys.argv[1:]
if args == ["--version"]:
    print("9.9.9 (Fake Claude Code)")
    sys.exit(0)
log = os.environ.get("FAKE_ARGV_LOG")
if log:
    with open(log, "a", encoding="utf-8") as f:
        f.write(json.dumps({"argv": args, "home": os.environ.get("HOME"),
                            "cfg": os.environ.get("CLAUDE_CONFIG_DIR"), "cwd": os.getcwd(),
                            "claudecode": os.environ.get("CLAUDECODE"),
                            "marker": os.environ.get("FAKE_MARKER")}) + "\n")
prompt = sys.stdin.read()  # 本物と同じく -p のプロンプトは標準入力から読む
fmt = args[args.index("--output-format") + 1]
cfg = Path(os.environ["CLAUDE_CONFIG_DIR"])
skills = sorted(p.name for p in (cfg / "skills").iterdir()) if (cfg / "skills").is_dir() else []
claude_md = (cfg / "CLAUDE.md").read_text(encoding="utf-8") if (cfg / "CLAUDE.md").exists() else ""


def out(o):
    print(json.dumps(o, ensure_ascii=False), flush=True)


def result(text, cost=0.01, turns=3, **kw):
    r = {"type": "result", "subtype": "success", "is_error": False, "duration_ms": 1200, "num_turns": turns,
         "result": text, "stop_reason": "end_turn", "total_cost_usd": cost, "api_error_status": None,
         "usage": {"input_tokens": 10, "cache_creation_input_tokens": 100, "cache_read_input_tokens": 1000,
                   "output_tokens": 50}, "terminal_reason": "completed"}
    r.update(kw)
    out(r)


if fmt == "json" and "--json-schema" in args:  # harvest の書き換え（構造化出力）
    if os.environ.get("FAKE_JUDGE_FAIL"):  # 未ログイン
        print(json.dumps({"type": "result", "subtype": "success", "is_error": True, "result": "Not logged in · Please run /login"}))
        sys.exit(1)
    request = prompt.split("<<<", 1)[-1].split(">>>", 1)[0]
    if "雑談" in request:
        draft = {"usable": False, "id": "", "description": "", "category": "", "prompt": "", "needs_clarification": False,
                 "files": [], "reference_outputs": [], "final_answer_example": "", "check_script": "", "graders": []}
    else:
        leak = "acme_orders_2026" if "acme_orders_2026" in request and "前回の案" not in prompt else "orders"
        check = ("import csv, re, sys\nfrom pathlib import Path\n"
                 "t = sum(int(r['amount']) for r in csv.DictReader(open(Path(sys.argv[1]) / 'orders.csv', encoding='utf-8')))\n"
                 "p = Path('total.txt')\n"
                 "sys.exit(0 if p.is_file() and str(t) in re.sub(r'[,\\s]', '', p.read_text(encoding='utf-8')) else 1)\n")
        draft = {"usable": True, "id": "Monthly Orders!", "description": "注文の合計を出す", "category": "集計",
                 "prompt": f"{leak}.csv ではなく orders.csv の amount の合計を total.txt に書いてください。",
                 "needs_clarification": False,
                 "files": [{"path": "orders.csv", "content": "id,amount\n1,1200\n2,3400\n"}],
                 "reference_outputs": [{"path": "total.txt", "content": "4600\n"}],
                 "final_answer_example": "合計は 4600 です。", "check_script": check,
                 "graders": [{"type": "file_exists", "path": "total.txt"}]}
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": json.dumps(draft, ensure_ascii=False),
                      "structured_output": draft, "total_cost_usd": 0.005}))
    sys.exit(0)

if fmt == "json":  # LLM 判定
    if os.environ.get("FAKE_JUDGE_FAIL"):  # 未ログイン時の実機の形（is_error で result にメッセージ）
        print(json.dumps({"type": "result", "subtype": "success", "is_error": True,
                          "result": "Not logged in · Please run /login", "total_cost_usd": 0}))
        sys.exit(1)
    content = prompt.split("<<<", 1)[-1]
    verdict = "PASS" if "2026-03" in content and "店" in content else "FAIL"
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": f"{verdict}\n偽の判定",
                      "total_cost_usd": 0.001}))
    sys.exit(0)

assert "--verbose" in args, "stream-json には --verbose が必要"
settings = cfg / "settings.json"
if "--include-hook-events" in args and settings.exists() and "SessionStart" in settings.read_text(encoding="utf-8"):
    # 実機（2.1.306）と同じ形の hook イベント
    out({"type": "system", "subtype": "hook_started", "hook_id": "h1", "hook_name": "SessionStart:startup", "hook_event": "SessionStart"})
    out({"type": "system", "subtype": "hook_response", "hook_id": "h1", "hook_name": "SessionStart:startup",
         "hook_event": "SessionStart", "output": "", "stdout": "", "stderr": "", "exit_code": 0, "outcome": "success"})
out({"type": "system", "subtype": "init", "claude_code_version": "9.9.9", "model": "fake-model",
     "skills": skills + ["bundled-skill"], "plugins": [], "mcp_servers": [], "agents": []})

if "FAKE_TIMEOUT" in prompt:
    time.sleep(60)
if "FAKE_RATELIMIT" in prompt:
    state = Path(os.environ["FAKE_STATE_DIR"]) / "ratelimit"
    n = int(state.read_text()) if state.exists() else 0
    state.write_text(str(n + 1))
    if n == 0:
        result("API Error: 429 rate limited", cost=0.0, is_error=True, api_error_status=429)
        sys.exit(1)
    result("ok after retry")
    sys.exit(0)

if "17 × 23" in prompt:
    out({"type": "assistant", "parent_tool_use_id": None, "message": {"content": [{"type": "text", "text": "391"}]}})
    result("391", cost=0.002, turns=1)
    sys.exit(0)

def extract():
    """抽出依頼: 2026年3月・新宿店・10,000 円以上の件数と合計（正しく抽出する）。"""
    rows = [r for r in csv.DictReader(open("sales.csv", encoding="utf-8"))
            if r["date"].startswith("2026-03") and r["store"] == "新宿店" and int(r["amount"]) >= 10000]
    Path("result.csv").write_text(f"件数,合計金額\n{len(rows)},{sum(int(r['amount']) for r in rows)}\n", encoding="utf-8")
    Path("query.sql").write_text("SELECT COUNT(*), SUM(amount) FROM sales WHERE date LIKE '2026-03%' AND store = '新宿店' AND amount >= 10000;\n",
                                 encoding="utf-8")


if "先月の売上" in prompt:  # 曖昧な依頼: 配布物に「確認する」ルールがあれば質問を返す
    if "確認" in claude_md:
        result("対象の店舗・期間（データは 2026年1〜3月）・必要な指標を教えていただけますか？")
    else:
        out({"type": "assistant", "parent_tool_use_id": None, "message": {"content": [
            {"type": "tool_use", "id": "w0", "name": "Write", "input": {"file_path": "out.csv", "content": "..."}}]}})
        Path("out.csv").write_text("推測で抽出\n", encoding="utf-8")
        result("2026-03 のデータを抽出しました。")
    sys.exit(0)

if "抽出依頼" in prompt:
    out({"type": "assistant", "parent_tool_use_id": None, "message": {"content": [
        {"type": "tool_use", "id": "b0", "name": "Bash", "input": {"command": "python3 - <<'SQL' ..."}}]}})
    extract()
    result("result.csv と query.sql を保存しました。")
    sys.exit(0)

if "sales.csv" in prompt:
    n = 0
    if "csv-monthly-summary" in skills and "月ごと" in prompt:
        out({"type": "assistant", "parent_tool_use_id": None, "message": {"content": [
            {"type": "tool_use", "id": "t0", "name": "Skill", "input": {"skill": "csv-monthly-summary", "args": "sales.csv"}}]}})
        out({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t0", "content": "loaded"}]}})
    totals = defaultdict(int)
    by_store = defaultdict(int)
    with open("sales.csv", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            totals[row["date"][:7]] += int(row["amount"])
            if row["date"].startswith("2026-03"):
                by_store[row["store"]] += int(row["amount"])
    if "売上合計" in claude_md:
        lines = ["| 月 | 売上合計 |", "|---|---|"] + [f"| {m} | {v} |" for m, v in sorted(totals.items())]
        lines += ["", f"最多月: {max(totals, key=totals.get)}"]
    else:
        lines = ["# Monthly", ""] + [f"- {m}: {v:,}円" for m, v in sorted(totals.items())]
    if "考察" in prompt:
        top = max(by_store, key=by_store.get)
        lines += ["", f"2026-03 が最多で、{top} が {by_store[top]} と最も貢献した。"]
    out({"type": "assistant", "parent_tool_use_id": None, "message": {"content": [
        {"type": "tool_use", "id": "t1", "name": "Write", "input": {"file_path": "summary.md", "content": "..."}}]}})
    Path("summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    out({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "is_error": False}]}})
    result("summary.md を作成しました")
    sys.exit(0)

result("何もしませんでした")
