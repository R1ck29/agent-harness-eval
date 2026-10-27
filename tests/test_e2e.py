"""E2E テスト: 偽の claude / codex CLI を使い、CLI 経由で実行〜集計〜ゲートまで通す。"""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness_eval.cli import main  # noqa: E402

FAKE = ROOT / "tests" / "fake_agents"


def run_cli(*args: str) -> tuple[int, str]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        code = main(list(args))
    return code, buf.getvalue()


class E2E(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="he eval "))  # 空白を含むパスでも壊れないこと（Windows のユーザー名など）
        ex = self.tmp / "examples"
        shutil.copytree(ROOT / "examples", ex)
        # 実物の代わりに偽 CLI を使うようプロファイルを書き換える
        for f in (ex / "profiles").glob("*.yaml"):
            fake = FAKE / ("fake_claude.py" if "claude" in f.name else "fake_codex.py")
            f.write_text(f.read_text(encoding="utf-8") + f"\nexecutable: [{json.dumps(sys.executable)}, {json.dumps(str(fake))}]\n",
                         encoding="utf-8")
        self.ex = ex
        self.out = self.tmp / "results"
        self.cache = self.tmp / "cache"
        self.argv_log = self.tmp / "argv.jsonl"
        os.environ["FAKE_ARGV_LOG"] = str(self.argv_log)
        os.environ["FAKE_STATE_DIR"] = str(self.tmp)

    def tearDown(self):
        os.environ.pop("FAKE_ARGV_LOG", None)
        os.environ.pop("FAKE_STATE_DIR", None)
        os.environ.pop("FAKE_JUDGE_FAIL", None)

    def runs(self) -> list[Path]:
        """実行ごとのフォルダ（results/ 直下の spend.jsonl などは除く）。"""
        return sorted(d for d in self.out.iterdir() if d.is_dir())

    def run_eval(self, *extra) -> tuple[int, str, dict, list]:
        args = ["run", "--tasks", str(self.ex / "tasks"), "--profiles", str(self.ex / "profiles"),
                "--out", str(self.out), "--cache-dir", str(self.cache), *extra]
        code, out = run_cli(*args)
        runs = self.runs()
        summary = json.loads((runs[-1] / "summary.json").read_text(encoding="utf-8"))
        recs = [json.loads(l) for l in (runs[-1] / "trials.jsonl").read_text(encoding="utf-8").splitlines()]
        return code, out, summary, recs

    def test_full_flow(self):
        code, out, s, recs = self.run_eval("--trials", "2", "--no-llm", "--baseline", "claude-baseline", "--tag", "smoke", "--tag", "skills")
        self.assertEqual(code, 0, out)
        P = s["profiles"]
        # 配布物ありはフォーマット・スキル・否定タスクすべて合格
        self.assertEqual(P["claude-ds-lite"]["pass_hat_k"], 1.0, out)
        self.assertEqual(P["codex-ds-lite"]["pass_hat_k"], 1.0, out)
        # ベースラインは形式ルールを知らない / スキルがない → 否定タスクだけ合格
        # ベースラインは計算と抽出（配布物に依らない）だけ合格: 5 タスク中 2
        self.assertAlmostEqual(P["claude-baseline"]["pass_hat_k"], 2 / 5)
        self.assertIn("clarification", P["claude-baseline"]["failure_tags"])  # 曖昧な依頼で確認せずに書いた
        self.assertIn("instruction_violation", P["claude-baseline"]["failure_tags"])
        self.assertIn("skill_selection", P["claude-baseline"]["failure_tags"])
        self.assertEqual(P["codex-ds-lite"]["total_cost_usd"], None)  # Codex は pricing なし → 不明（0 で埋めない）
        self.assertAlmostEqual(P["claude-ds-lite"]["total_cost_usd"], (0.01 * 4 + 0.002) * 2)  # 4 タスク × $0.01 + 計算 $0.002、2 試行
        self.assertEqual(len(recs), 4 * 5 * 2)
        codex_skill = [r for r in recs if r["profile_id"] == "codex-ds-lite" and r["task_id"] == "csv-summary-skill"]
        self.assertEqual(codex_skill[0]["skill_detection"], "heuristic")
        # 隔離: 一時ホーム・設定ディレクトリ・作業ディレクトリが試行ごとに別、親の CLAUDECODE は消える
        logs = [json.loads(l) for l in self.argv_log.read_text().splitlines()]
        self.assertEqual(len({l["cfg"] for l in logs}), len(logs))
        self.assertTrue(all(l["home"] and l["cfg"].startswith(l["home"]) for l in logs))
        self.assertTrue(all(l["claudecode"] is None for l in logs))
        claude_argv = [l["argv"] for l in logs if l["argv"][0] == "-p"]
        codex_argv = [l["argv"] for l in logs if l["argv"][0] == "exec"]
        for flag in ("--output-format", "--verbose", "--max-turns", "--model", "--allowedTools",
                     "--max-budget-usd", "--settings"):
            self.assertIn(flag, claude_argv[0])
        self.assertTrue(all("--include-hook-events" not in a for a in claude_argv))  # hook_used のないタスクでは記録しない
        # Codex は配布物の hooks を実行するため信頼確認を省略する
        self.assertTrue(codex_argv and all("--dangerously-bypass-hook-trust" in a for a in codex_argv))
        run_dir = self.runs()[-1]
        html = (run_dir / "report.html").read_text(encoding="utf-8")
        self.assertIn("基準 <code>claude-baseline</code> との差", html)
        self.assertNotIn("<script", html)
        # 合格試行の作業ディレクトリと一時ホームは消えている（--keep failed）
        passed = next(r for r in recs if r["passed"])
        self.assertFalse((Path(passed["trial_dir"]) / "workdir").exists())
        self.assertFalse((Path(passed["trial_dir"]) / "home").exists())
        failed = next(r for r in recs if not r["passed"])
        self.assertTrue((Path(failed["trial_dir"]) / "workdir").exists())

        # 2 回目: 何も変えなければ全試行がキャッシュから再利用され、エージェントは呼ばれない
        n_calls = len(logs)
        code, out, s2, recs2 = self.run_eval("--trials", "2", "--no-llm", "--tag", "smoke", "--tag", "skills")
        self.assertEqual(code, 0)
        self.assertTrue(all(r["from_cache"] for r in recs2))
        self.assertEqual(len(self.argv_log.read_text().splitlines()), n_calls)
        self.assertEqual(s2["profiles"]["claude-baseline"]["pass_hat_k"], P["claude-baseline"]["pass_hat_k"])
        runs = self.runs()
        gate_args = ["--margin", "0.1", "--min-tasks", "1"]
        self.assertEqual(run_cli("gate", "--current", str(runs[-1]), "--previous", str(runs[0]), *gate_args)[0], 0)
        code, out = run_cli("gate", "--current", str(runs[-1]), "--previous", str(runs[0]), "--margin", "0.1")
        self.assertEqual(code, 3, out)  # 5 タスクでは既定の最低 10 タスクに届かない → 判定不能
        with self.assertRaises(SystemExit):  # マージンは毎回明示する（結果を見てから決めない）
            run_cli("gate", "--current", str(runs[-1]), "--previous", str(runs[0]))

        # 配布物を変えると、そのプロファイルの試行だけ再実行される
        claude_md = self.ex / "harnesses" / "ds-lite" / "CLAUDE.md"
        claude_md.write_text("# 形式ルールを削除した版\n", encoding="utf-8")
        code, out, s3, recs3 = self.run_eval("--trials", "2", "--no-llm", "--tag", "smoke", "--tag", "skills")
        rerun = {r["profile_id"] for r in recs3 if not r["from_cache"]}
        self.assertEqual(rerun, {"claude-ds-lite", "codex-ds-lite"})
        self.assertLess(s3["profiles"]["claude-ds-lite"]["pass_hat_k"], 1.0)
        code, out = run_cli("gate", "--current", str(self.runs()[-1]), "--previous", str(runs[0]), *gate_args)
        self.assertIn(code, (1, 3), out)  # 5 タスクでは悪化が区間で言い切れるとは限らない。通過はしない
        self.assertIn("claude-ds-lite/csv-summary-format", out)  # 下がったタスクは診断として出る

    def test_llm_judge_only_when_code_passes(self):
        args = ["--trials", "1", "--task", "csv-summary-insight", "--judge-executable", sys.executable, str(FAKE / "fake_claude.py")]
        code, out, s, recs = self.run_eval(*args)
        self.assertEqual(code, 0, out)
        by = {r["profile_id"]: r for r in recs}
        llm = lambda r: [g for g in r["grader_results"] if g["type"] == "llm"][0]
        # 偽 claude は「考察」を頼まれると店舗名入りの考察を書く → コード判定合格 → 判定を実行して PASS
        self.assertEqual(llm(by["claude-ds-lite"])["status"], "pass", by["claude-ds-lite"]["grader_results"])
        self.assertEqual(by["claude-ds-lite"]["judge_cost_usd"], 0.001)
        self.assertTrue(by["claude-ds-lite"]["passed"])
        # 偽 codex は考察を書かないが数値は正しい → 判定を実行して FAIL
        self.assertEqual(llm(by["codex-ds-lite"])["status"], "fail")
        # --no-llm なら not_run（分母から除外）
        code, out, s, recs = self.run_eval("--trials", "1", "--task", "csv-summary-insight", "--no-llm")
        self.assertTrue(all(llm(r)["status"] == "not_run" for r in recs))

    def test_rate_limit_retry_timeout_and_budget(self):
        t = self.ex / "tasks"
        (t / "rl.yaml").write_text("id: rl\ntargets: [claude_code]\nprompt: FAKE_RATELIMIT\nallow_tools: [Read]\n"
                                   "graders: [{type: regex, pattern: ok}, {type: tool_used, tool: Bash, max: 0}]\n", encoding="utf-8")
        (t / "to.yaml").write_text("id: to\ntargets: [claude_code]\nprompt: FAKE_TIMEOUT\nlimits: {timeout_s: 2}\nallow_tools: [Read]\n"
                                   "graders: [{type: regex, pattern: ok}, {type: tool_used, tool: Bash, max: 0}]\n", encoding="utf-8")
        code, out = run_cli("run", "--tasks", str(t / "rl.yaml"), str(t / "to.yaml"), "--profiles", str(self.ex / "profiles" / "claude-baseline.yaml"),
                            "--out", str(self.out), "--cache-dir", str(self.cache), "--no-llm", "--retry-infra", "1", "--retry-wait-s", "0")
        self.assertEqual(code, 0, out)
        recs = {json.loads(l)["task_id"]: json.loads(l) for l in (self.runs()[-1] / "trials.jsonl").read_text().splitlines()}
        self.assertTrue(recs["rl"]["passed"], recs["rl"])  # 429 → 再試行で成功
        self.assertIn("再試行 1/1", out)
        self.assertEqual(recs["to"]["error"]["type"], "timeout")
        self.assertTrue(recs["to"]["valid"])  # タイムアウトは失敗として数える（除外しない）
        self.assertFalse(recs["to"]["passed"])

        # 予算上限: 0 ドルなら新規試行は実行されない（集計からも除外）
        code, out = run_cli("run", "--tasks", str(self.ex / "tasks" / "arithmetic-no-skill.yaml"), "--profiles",
                            str(self.ex / "profiles" / "claude-baseline.yaml"), "--out", str(self.out),
                            "--no-cache", "--max-cost-usd", "0", "--no-llm")
        last = self.runs()[-1]
        rec = json.loads((last / "trials.jsonl").read_text().splitlines()[0])
        self.assertEqual(rec["error"]["type"], "budget_stop")
        self.assertEqual(json.loads((last / "summary.json").read_text())["meta"]["not_run"], 1)

    def test_relative_out_dir(self):
        """--out を相対パスで指定しても（既定の results）、一時ホームと配布物が正しく渡る。"""
        cwd = os.getcwd()
        os.chdir(self.tmp)
        try:
            code, out = run_cli("run", "--tasks", str(self.ex / "tasks" / "csv-summary-format.yaml"), "--profiles",
                                str(self.ex / "profiles" / "claude-ds-lite.yaml"), "--no-llm", "--out", "results",
                                "--cache-dir", "cache")
        finally:
            os.chdir(cwd)
        self.assertEqual(code, 0, out)
        run = sorted(d for d in (self.tmp / "results").iterdir() if d.is_dir())[-1]
        rec = json.loads((run / "trials.jsonl").read_text(encoding="utf-8").splitlines()[0])
        self.assertTrue(rec["passed"], rec["grader_results"])
        self.assertTrue(Path(json.loads(self.argv_log.read_text().splitlines()[0])["cfg"]).is_absolute())

    def test_judge_uses_profile_env_and_failure_is_excluded(self):
        prof = self.ex / "profiles" / "claude-ds-lite.yaml"
        prof.write_text(prof.read_text(encoding="utf-8") + "\nenv: {FAKE_MARKER: from-profile}\nextra_args: [--add-dir, '{workdir}']\n",
                        encoding="utf-8")
        args = ["--trials", "1", "--task", "csv-summary-insight", "--judge-executable", sys.executable, str(FAKE / "fake_claude.py"),
                "--judge-profile", "claude-ds-lite"]
        code, out, s, recs = self.run_eval(*args)
        self.assertEqual(code, 0, out)
        logs = [json.loads(l) for l in self.argv_log.read_text().splitlines()]
        judge_logs = [l for l in logs if "json" in l["argv"]]
        self.assertTrue(judge_logs and all(l["marker"] == "from-profile" for l in judge_logs))
        # extra_args の {workdir} は試行ごとの作業ディレクトリに置換される
        trial = next(l for l in logs if "stream-json" in l["argv"] and l["marker"] == "from-profile")
        self.assertEqual(Path(trial["argv"][trial["argv"].index("--add-dir") + 1]), Path(trial["cwd"]))
        # 判定が認証エラー → その試行は除外（失敗に数えない）、キャッシュもしない
        os.environ["FAKE_JUDGE_FAIL"] = "1"
        code, out, s, recs = self.run_eval(*args, "--no-cache")
        r = next(r for r in recs if r["profile_id"] == "claude-ds-lite")
        self.assertEqual(r["error"]["type"], "judge_error", r)
        self.assertFalse(r["valid"])
        self.assertEqual(s["profiles"]["claude-ds-lite"]["excluded_reasons"], {"judge_error": 1})

    def test_llm_only_task_without_llm_is_not_graded(self):
        t = self.ex / "tasks" / "llm-only.yaml"
        t.write_text("id: llm-only\ntargets: [claude_code]\nprompt: 17 × 23 は？\nallow_tools: [Read]\n"
                     "graders: [{type: llm, criteria: 391 と答えている}]\n", encoding="utf-8")
        code, out = run_cli("run", "--tasks", str(t), "--profiles", str(self.ex / "profiles" / "claude-baseline.yaml"),
                            "--out", str(self.out), "--cache-dir", str(self.cache), "--no-llm")
        rec = json.loads((self.runs()[-1] / "trials.jsonl").read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(rec["error"]["type"], "not_graded")
        self.assertFalse(rec["valid"])

    @unittest.skipIf(os.name == "nt", "SIGINT の送り方が異なるため POSIX のみ")
    def test_ctrl_c_stops_quickly_and_cleans_up(self):
        """Ctrl-C で未開始の試行は始めず、実行中のエージェントは止め、一時ホームを残さない。"""
        import signal
        import subprocess
        import time
        t = self.ex / "tasks" / "slow.yaml"
        t.write_text("id: slow\ntargets: [claude_code]\nprompt: FAKE_TIMEOUT\nlimits: {timeout_s: 120}\nallow_tools: [Read]\n"
                     "graders: [{type: regex, pattern: ok}, {type: tool_used, tool: Bash, max: 0}]\n", encoding="utf-8")
        cmd = [sys.executable, "-m", "harness_eval", "run", "--tasks", str(t), "--profiles",
               str(self.ex / "profiles" / "claude-baseline.yaml"), "--trials", "3", "--no-llm",
               "--out", str(self.out), "--cache-dir", str(self.cache)]
        proc = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        deadline = time.monotonic() + 20
        while not (self.argv_log.exists() and self.argv_log.read_text().strip()) and time.monotonic() < deadline:
            time.sleep(0.1)
        time.sleep(0.5)
        t0 = time.monotonic()
        proc.send_signal(signal.SIGINT)
        out, _ = proc.communicate(timeout=30)
        self.assertLess(time.monotonic() - t0, 15, out)
        self.assertEqual(proc.returncode, 130, out)
        self.assertEqual(len(self.argv_log.read_text().splitlines()), 1, out)  # 2・3 試行目は始まらない
        self.assertEqual(list(self.out.rglob("home")), [])
        self.assertEqual(list(self.cache.glob("*.json")) if self.cache.exists() else [], [])  # 中断した試行はキャッシュしない

    def test_hook_used_end_to_end(self):
        (self.ex / "harnesses" / "hooked").mkdir()
        (self.ex / "harnesses" / "hooked" / "settings.json").write_text(
            '{"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "echo hi"}]}]}}', encoding="utf-8")
        for name, agent in (("claude-hooked", "claude_code"), ("codex-hooked", "codex")):
            fake = FAKE / ("fake_claude.py" if agent == "claude_code" else "fake_codex.py")
            (self.ex / "profiles" / f"{name}.yaml").write_text(
                f"id: {name}\nagent: {agent}\ninstall: [{{copy: ../harnesses/hooked/settings.json, to: '{{config_dir}}/settings.json'}}]\n"
                f"executable: [{json.dumps(sys.executable)}, {json.dumps(str(fake))}]\n", encoding="utf-8")
        t = self.ex / "tasks" / "hook.yaml"
        t.write_text("id: hook\ntargets: [claude_code, codex]\nprompt: 17 × 23 は？\nallow_tools: [Read]\n"
                     "graders: [{type: regex, pattern: '391'}, {type: hook_used, hook: 'SessionStart:.*', outcome: success}]\n",
                     encoding="utf-8")
        code, out = run_cli("run", "--tasks", str(t), "--profiles", str(self.ex / "profiles" / "claude-hooked.yaml"),
                            str(self.ex / "profiles" / "codex-hooked.yaml"), str(self.ex / "profiles" / "claude-baseline.yaml"),
                            "--out", str(self.out), "--cache-dir", str(self.cache), "--no-llm")
        self.assertEqual(code, 0, out)
        recs = {json.loads(l)["profile_id"]: json.loads(l) for l in
                (self.runs()[-1] / "trials.jsonl").read_text(encoding="utf-8").splitlines()}
        status = lambda p: [g["status"] for g in recs[p]["grader_results"]]
        self.assertEqual(status("claude-hooked"), ["pass", "pass"])
        self.assertEqual(status("claude-baseline"), ["pass", "fail"])  # 配布物なし → hook は動かない
        self.assertEqual(status("codex-hooked"), ["pass", "not_run"])   # Codex は記録がないので判定しない
        self.assertTrue(recs["codex-hooked"]["passed"])
        self.assertEqual(recs["codex-hooked"]["warnings"], [])  # 信頼確認省略の既知の通知は警告にしない

    def test_monthly_budget(self):
        """--monthly-budget-usd: 今月の実績（キャッシュ再利用分を除く）を引いた残りが上限になる。"""
        args = ["--tasks", str(self.ex / "tasks" / "arithmetic-no-skill.yaml"), "--profiles", str(self.ex / "profiles" / "claude-baseline.yaml"),
                "--out", str(self.out), "--cache-dir", str(self.cache), "--no-llm"]
        self.assertEqual(run_cli("run", *args)[0], 0)  # 偽 claude はこのタスクで $0.002
        code, out = run_cli("plan", *args, "--no-cache", "--monthly-budget-usd", "1")
        self.assertIn("今月の実績（claude_code）: $0.0020", out)
        led = self.out / "spend.jsonl"
        ledger = [json.loads(l) for l in led.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([(x["usd"], x["agent"], x["billing"]) for x in ledger], [(0.002, "claude_code", "api")])  # 発生のたびに台帳へ
        # Codex の口座の支出は Claude の予算を減らさない（別々の月額上限）
        with led.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"at": ledger[0]["at"], "usd": 5.0, "run_id": "x", "agent": "codex", "billing": "api"}) + "\n")
        self.assertIn("今月の実績（claude_code）: $0.0020", run_cli("plan", *args, "--no-cache", "--monthly-budget-usd", "1")[1])
        code, out = run_cli("run", *args, "--no-cache", "--monthly-budget-usd", "0.002")
        rec = json.loads((self.runs()[-1] / "trials.jsonl").read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(rec["error"]["type"], "budget_stop")
        # サブスクリプションは呼び出しごとの請求がないので月の予算では止めない（API 換算の推定額は台帳に残す）
        prof = self.ex / "profiles" / "claude-baseline.yaml"
        prof.write_text(prof.read_text(encoding="utf-8") + "billing: subscription\n", encoding="utf-8")
        code, out = run_cli("run", *args, "--no-cache", "--monthly-budget-usd", "0.002")
        rec = json.loads((self.runs()[-1] / "trials.jsonl").read_text(encoding="utf-8").splitlines()[0])
        self.assertTrue(rec["passed"], rec["error"])
        self.assertEqual(json.loads(led.read_text(encoding="utf-8").splitlines()[-1])["billing"], "subscription")

    def test_doctor_reports_inherited_route_variables(self):
        """親の環境から経路を変える変数（API キーなど）を引き継ぐと NG。値は表示しない。"""
        from harness_eval.isolation import ROUTE_VARS
        clean = {k: v for k, v in os.environ.items() if k not in {x for xs in ROUTE_VARS.values() for x in xs}}
        with mock.patch.dict(os.environ, clean, clear=True):
            code, out = run_cli("doctor", "--profiles", str(self.ex / "profiles"))
            self.assertEqual(code, 0, out)
            os.environ["ANTHROPIC_API_KEY"] = "sk-secret-value"
            code, out = run_cli("doctor", "--profiles", str(self.ex / "profiles"))
        self.assertEqual(code, 1, out)
        self.assertIn("ANTHROPIC_API_KEY", out)
        self.assertNotIn("sk-secret-value", out)

    def test_interrupted_before_retry_does_not_call_agent(self):
        """再試行待ちの間に Ctrl-C された試行は、新しくエージェントを呼ばずに interrupted で終わる。"""
        from harness_eval.runner import RunOptions, Runner
        from harness_eval.spec import load_profiles, load_tasks
        r = Runner(load_tasks([str(self.ex / "tasks" / "arithmetic-no-skill.yaml")]),
                   load_profiles([str(self.ex / "profiles" / "claude-baseline.yaml")]),
                   RunOptions(llm=False, use_cache=False, log=lambda s: None), self.out)
        r.interrupted.set()
        t, p = r.pairs[0]
        rec = r._one(t, p, 0, lambda x: None)
        self.assertEqual(rec.error["type"], "interrupted")
        self.assertFalse(self.argv_log.exists())

    def test_harvest(self):
        """履歴 → 架空化した下書き。元の依頼文は書き出し先に残らず、元の識別子が残った案は作り直す。"""
        hist = self.tmp / "hist" / ".claude"
        secret = "営業部の依頼: acme_orders_2026 から先月分の注文合計を出してほしい"
        rows = [{"display": secret, "pastedContents": {}, "timestamp": 1792671000000, "sessionId": "s1"},
                {"display": "雑談です。今日は天気がいいですね、そう思いませんか", "pastedContents": {}, "timestamp": 1792671100000, "sessionId": "s2"},
                {"display": "倉庫ごとの在庫数を集計して一覧にしてください（2026年版）", "pastedContents": {}, "timestamp": 1792671200000, "sessionId": "s3"}]
        hist.mkdir(parents=True)
        (hist / "history.jsonl").write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")
        out = self.tmp / "harvested"
        exe = ["--executable", sys.executable, str(FAKE / "fake_claude.py")]
        code, log = run_cli("harvest", "--out", str(out), "--source", str(hist), "--dry-run", *exe)
        self.assertEqual(code, 0, log)
        self.assertIn("候補 3 件", log)
        self.assertFalse(out.exists())  # dry-run は何も書かない
        ledger = self.tmp / "results" / "spend.jsonl"
        cwd = os.getcwd()
        os.chdir(self.tmp)  # README の例と同じ相対パスの --out（採点スクリプトは別の cwd で動くので、解決しないと全件落ちる）
        try:
            code, log = run_cli("harvest", "--out", "harvested", "--source", str(hist), "--ledger", str(ledger), *exe)
        finally:
            os.chdir(cwd)
        self.assertEqual(code, 0, log)
        self.assertNotIn(secret, log)  # 依頼の本文は表示しない
        written = sorted(p.name for p in (out / "tasks").glob("*.yaml"))
        self.assertEqual(written, ["h-monthly-orders-2.yaml", "h-monthly-orders.yaml"], log)
        self.assertIn("評価に向かない依頼 1 件", log)
        everything = "".join(f.read_text(encoding="utf-8", errors="replace") for f in out.rglob("*") if f.is_file())
        self.assertNotIn("acme_orders_2026", everything)  # 元の識別子が残った案は作り直された
        self.assertNotIn("営業部の依頼", everything)
        self.assertEqual(len(ledger.read_text().splitlines()), 4)  # 書き換え 4 回分（うち 1 回は作り直し）の費用
        code, log = run_cli("validate", "--tasks", str(out / "tasks"), "--profiles", str(self.ex / "profiles"))
        self.assertEqual(code, 0, log)
        self.assertNotIn("過程を見るグレーダー", log)  # 「本物の環境に接続しようとしていない」を必ず付ける
        # 人が確認したあと: 採点スクリプトを実行して、正解例で通り何もしなければ落ちることを確かめる
        code, log = run_cli("validate", "--tasks", str(out / "tasks"), "--profiles", str(self.ex / "profiles"), "--selftest")
        self.assertEqual(code, 0, log)
        self.assertEqual(log.count(": OK"), 2, log)
        # 未ログインなら最初の 1 件で止める（全件で失敗し続けない）
        os.environ["FAKE_JUDGE_FAIL"] = "1"
        code, log = run_cli("harvest", "--out", str(self.tmp / "noauth"), "--source", str(hist), "--ledger", str(ledger), *exe)
        os.environ.pop("FAKE_JUDGE_FAIL")
        self.assertIn("中止: 認証エラー", log)
        self.assertEqual(log.count("中止"), 1)
        # 履歴がなければ同梱の標準タスクを書き出す
        empty = self.tmp / "empty"
        empty.mkdir()
        code, log = run_cli("harvest", "--out", str(self.tmp / "fallback"), "--source", str(empty), *exe)
        self.assertIn("同梱の標準タスク", log)
        code, log = run_cli("validate", "--tasks", str(self.tmp / "fallback" / "tasks"), "--profiles", str(self.ex / "profiles"))
        self.assertEqual(code, 0, log)
        self.assertIn("タスク 6 件", log)

    def test_plan_and_validate(self):
        code, out = run_cli("validate", "--tasks", str(self.ex / "tasks"), "--profiles", str(self.ex / "profiles"))
        self.assertEqual(code, 0)
        code, out = run_cli("plan", "--tasks", str(self.ex / "tasks"), "--profiles", str(self.ex / "profiles"),
                            "--trials", "3", "--cache-dir", str(self.cache))
        self.assertEqual(code, 0)
        self.assertIn("組み合わせ 24 × 試行 3 = 72 試行", out)
        self.assertIn("CLI 9.9.9", out)


if __name__ == "__main__":
    unittest.main()
