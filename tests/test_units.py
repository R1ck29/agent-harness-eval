"""単体テスト: 定義の検証・パーサ・指標・グレーダー・隔離。python -m unittest discover -s tests"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness_eval.adapters import ClaudeAdapter, CodexAdapter  # noqa: E402
from harness_eval.graders import JudgeConfig, grade_all  # noqa: E402
from harness_eval.isolation import (build_env, make_trial_paths, run_install, run_process)  # noqa: E402
from harness_eval.metrics import decide, deltas, gate, paired_bootstrap, pass_hat_k, summarize  # noqa: E402
from harness_eval.runner import append_spend, month_spent, schedule  # noqa: E402
from harness_eval.adapters import older_than_verified  # noqa: E402
from harness_eval.isolation import route_signature, route_variables  # noqa: E402
from harness_eval.record import TrialRecord  # noqa: E402
from harness_eval.spec import Grader, SpecError, lint, load_profile, load_task, load_tasks, load_profiles  # noqa: E402

DATA = ROOT / "tests" / "data"


def write(p: Path, text: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def rec(**kw) -> TrialRecord:
    base = dict(run_id="r", task_id="t", profile_id="p", agent="claude_code", trial=0, cache_key="k",
                task_hash="th", profile_hash="ph", cli_version="v")
    base.update(kw)
    return TrialRecord(**base)


class SpecTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_examples_load(self):
        tasks = load_tasks([str(ROOT / "examples" / "tasks")])
        profiles = load_profiles([str(ROOT / "examples" / "profiles")])
        self.assertEqual({t.id for t in tasks}, {"csv-summary-format", "csv-summary-skill", "arithmetic-no-skill", "csv-summary-insight",
                                                 "request-extract", "request-ambiguous"})
        self.assertEqual(len(profiles), 4)

    def test_unknown_key_rejected(self):
        p = write(self.tmp / "t.yaml", "id: a\nprompt: x\ngraders: [{type: regex, pattern: x}]\ntimeout: 3\n")
        with self.assertRaisesRegex(SpecError, "未知のキー"):
            load_task(p)

    def test_grader_validation(self):
        for body, msg in [("[{type: nope}]", "未対応"), ("[{type: regex}]", "必須キー"),
                          ("[{type: regex, pattern: '('}]", "正規表現"), ("[{type: tool_used, tool: X, max: -1}]", "0 以上"),
                          ("[{type: regex, pattern: x, typo: 1}]", "未知のキー"),
                          ("[{type: hook_used, hook: X, outcome: blocked}]", "outcome"),
                          ("[{type: tool_used, tool: X, min: 3, max: 1}]", "min が max")]:
            p = write(self.tmp / "t.yaml", f"id: a\nprompt: x\ngraders: {body}\n")
            with self.assertRaisesRegex(SpecError, msg):
                load_task(p)

    def test_missing_fixture(self):
        p = write(self.tmp / "t.yaml", "id: a\nprompt: x\nfixture: nope\ngraders: [{type: regex, pattern: x}]\n")
        with self.assertRaisesRegex(SpecError, "fixture"):
            load_task(p)

    def test_profile_install_shape(self):
        p = write(self.tmp / "p.yaml", "id: a\nagent: claude_code\ninstall: [{copy: x}]\n")
        with self.assertRaisesRegex(SpecError, "install"):
            load_profile(p)

    def test_hash_changes_with_fixture_and_harness(self):
        fx = write(self.tmp / "fx" / "a.txt", "1")
        t = write(self.tmp / "t.yaml", "id: a\nprompt: x\nfixture: fx\ngraders: [{type: regex, pattern: x}]\n")
        h1 = load_task(t).content_hash()
        fx.write_text("2")
        self.assertNotEqual(h1, load_task(t).content_hash())
        # 説明・タグを書き換えても再実行しない（結果に影響しないため）
        t.write_text(t.read_text() + "description: 説明\ntags: [x]\n")
        self.assertEqual(load_task(t).content_hash(), load_task(t).content_hash())
        t2 = load_task(t)
        t.write_text(t.read_text().replace("description: 説明", "description: 別の説明"))
        self.assertEqual(t2.content_hash(), load_task(t).content_hash())
        hs = write(self.tmp / "h" / "CLAUDE.md", "rule")
        p = write(self.tmp / "p.yaml", "id: a\nagent: claude_code\ninstall: [{copy: h, to: '{config_dir}'}]\n")
        g1 = load_profile(p).content_hash()
        hs.write_text("rule2")
        self.assertNotEqual(g1, load_profile(p).content_hash())

    def test_lint(self):
        t = write(self.tmp / "t.yaml", "id: a\nprompt: x\ntargets: [claude_code]\ngraders: [{type: regex, pattern: x}]\npaired_negative: zz\n")
        p = write(self.tmp / "p.yaml", "id: a\nagent: claude_code\ninstall: [{copy: t.yaml, to: x}]\n")
        w = "\n".join(lint([load_task(t)], [load_profile(p)]))
        for s in ("過程を見る", "paired_negative", "allow_tools", "ベースライン"):
            self.assertIn(s, w)
        # 実行される配布物（install の run・hooks・MCP の設定）は利用者の権限で動くことを知らせる
        write(self.tmp / "h2" / "settings.json", '{"hooks": {}}')
        runner = write(self.tmp / "r.yaml", "id: r\nagent: claude_code\ninstall: [{run: 'echo x'}]\n")
        hooked = write(self.tmp / "k.yaml", "id: k\nagent: claude_code\ninstall: [{copy: h2, to: '{config_dir}'}]\n")
        w2 = "\n".join(lint([load_task(t)], [load_profile(runner), load_profile(hooked), load_profile(p)]))
        self.assertIn("r: ", w2)
        self.assertIn("k: ", w2)
        self.assertIn("利用者の権限", w2)
        self.assertNotIn("a: install", w2)  # CLAUDE.md などを置くだけのプロファイルには出さない
        # コピー先の名前・追加引数で渡す設定も見る
        renamed = write(self.tmp / "n.yaml", "id: n\nagent: claude_code\ninstall: [{copy: t.yaml, to: '{config_dir}/settings.json'}]\n")
        args = write(self.tmp / "x.yaml", "id: x\nagent: claude_code\nextra_args: [--mcp-config, '{config_dir}/m.json']\n")
        w3 = "\n".join(lint([load_task(t)], [load_profile(renamed), load_profile(args)]))
        self.assertIn("n: ", w3)
        self.assertIn("x: ", w3)
        cont = write(self.tmp / "c.yaml", "id: c\nagent: claude_code\nextra_args: [-c]\n")  # Claude Code の -c は --continue
        self.assertNotIn("c: ", "\n".join(lint([load_task(t)], [load_profile(cont)])))


class ParserTests(unittest.TestCase):
    def test_claude_real_capture(self):
        """v2.1.304 の実出力（stream_event 行を除いたもの）。"""
        r = ClaudeAdapter(executable=["x"]).parse(DATA.joinpath("claude_stream_real.jsonl").read_text().splitlines())
        self.assertIsNone(r.error_type)
        self.assertEqual(r.skill_calls, ["word-count"])
        self.assertEqual([c.name for c in r.tool_calls], ["Skill", "Bash", "Bash"])
        self.assertEqual(r.tool_calls[1].is_error, False)
        self.assertAlmostEqual(r.cost_usd, 0.00564094, places=6)
        self.assertEqual((r.num_turns, r.cost_source), (5, "client_estimate"))
        self.assertIn("4 words", r.final_message)
        self.assertEqual(r.meta["cli_version"], "2.1.304")
        self.assertIn("word-count", r.meta["loaded_skills"])
        self.assertEqual(r.tokens["cache_read"], 173614)

    def test_claude_errors(self):
        a = ClaudeAdapter(executable=["x"])
        mk = lambda **kw: [json.dumps({"type": "result", "is_error": False, "num_turns": 1, **kw})]
        self.assertEqual(a.parse(mk(subtype="error_max_turns")).error_type, "max_turns")
        self.assertEqual(a.parse(mk(subtype="error_max_budget_usd")).error_type, "budget_exceeded")
        self.assertEqual(a.parse(mk(subtype="success", is_error=True, api_error_status=429, result="x")).error_type, "rate_limit")
        self.assertEqual(a.parse(mk(subtype="success", is_error=True, api_error_status=401, result="x")).error_type, "auth")
        self.assertEqual(a.parse(mk(subtype="success", is_error=True, api_error_status=500, result="x")).error_type, "agent_error")
        self.assertEqual(a.parse(["garbage", "{bad"]).error_type, "agent_crash")
        # success 以外の subtype でも API のステータスがあれば環境起因として扱う（再試行・除外の対象）
        for status, want in ((429, "rate_limit"), (529, "overloaded"), (401, "auth"), (500, "agent_error")):
            got = a.parse(mk(subtype="error_during_execution", is_error=True, api_error_status=status, errors=["x"])).error_type
            self.assertEqual(got, want, status)
        # --include-hook-events の実出力（2.1.306。uuid / session_id を除く）
        hook_lines = [json.dumps(x) for x in (
            {"type": "system", "subtype": "hook_started", "hook_id": "904a", "hook_name": "PreToolUse:Bash", "hook_event": "PreToolUse"},
            {"type": "system", "subtype": "hook_response", "hook_id": "904a", "hook_name": "PreToolUse:Bash", "hook_event": "PreToolUse",
             "output": "ok\n", "stdout": "", "stderr": "ok\n", "exit_code": 0, "outcome": "success"},
            {"type": "system", "subtype": "hook_response", "hook_id": "7eeb", "hook_name": "PreToolUse:Bash", "hook_event": "PreToolUse",
             "output": "blocked rm\n", "stdout": "", "stderr": "blocked rm\n", "exit_code": 2, "outcome": "error"})]
        self.assertEqual(a.parse(hook_lines + mk(subtype="success", result="ok")).hook_calls,
                         [{"name": "PreToolUse:Bash", "exit_code": 0, "outcome": "success"},
                          {"name": "PreToolUse:Bash", "exit_code": 2, "outcome": "error"}])
        # 判定: 未ログインなどの is_error は「応答」ではなく失敗理由として返す
        text, cost, err = a.parse_judge(json.dumps({"type": "result", "is_error": True, "result": "Not logged in"}))
        self.assertIsNone(text)
        self.assertIn("Not logged in", err)
        sub = [json.dumps({"type": "assistant", "parent_tool_use_id": None, "message": {"content": [
            {"type": "tool_use", "id": "a", "name": "Agent", "input": {"subagent_type": "reviewer"}}]}}),
            json.dumps({"type": "assistant", "parent_tool_use_id": "a", "message": {"content": [
                {"type": "tool_use", "id": "b", "name": "Read", "input": {}}]}})] + mk(subtype="success", result="ok")
        r = a.parse(sub)
        self.assertEqual(r.subagent_calls, ["reviewer"])
        self.assertTrue(r.tool_calls[1].from_subagent)

    def test_codex_real_failure_capture(self):
        """0.163.0 の実出力: ネットワーク不通で再接続を繰り返し、タイムアウトで打ち切ったもの。"""
        r = CodexAdapter(executable=["x"]).parse(DATA.joinpath("codex_failed_real.jsonl").read_text().splitlines())
        self.assertEqual(r.error_type, "agent_crash")  # turn.completed も turn.failed もない
        self.assertTrue(r.meta["network_stalled"])  # 再接続だけで何も進んでいない → runner が network（除外）にする
        self.assertEqual(r.meta["thread_id"], "01a15ab3-bf35-77e1-ab7b-a9956c4eb062")
        self.assertTrue(any("Reconnecting" in w for w in r.warnings))
        self.assertEqual(r.tool_calls, [])

    def test_codex_success_and_pricing(self):
        lines = [json.dumps(x) for x in [
            {"type": "thread.started", "thread_id": "t"},
            {"type": "item.completed", "item": {"id": "1", "type": "command_execution", "command": "cat /h/.agents/skills/foo/SKILL.md",
                                                "aggregated_output": "", "exit_code": 0, "status": "completed"}},
            {"type": "item.completed", "item": {"id": "2", "type": "command_execution", "command": "false",
                                                "aggregated_output": "", "exit_code": 1, "status": "failed"}},
            {"type": "item.completed", "item": {"id": "3", "type": "mcp_tool_call", "server": "db", "tool": "q",
                                                "arguments": {"a": 1}, "result": None, "error": None, "status": "completed"}},
            {"type": "item.completed", "item": {"id": "4", "type": "collab_tool_call", "tool": "spawn_agent", "sender_thread_id": "t",
                                                "receiver_thread_ids": [], "prompt": "p", "agents_states": {}, "status": "completed"}},
            {"type": "item.completed", "item": {"id": "5", "item_type": "assistant_message", "text": "old"}},
            {"type": "item.completed", "item": {"id": "6", "type": "agent_message", "text": "final"}},
            {"type": "turn.completed", "usage": {"input_tokens": 1_000_000, "cached_input_tokens": 400_000, "output_tokens": 100_000, "reasoning_output_tokens": 0}},
        ]]
        prof = load_profile(write(Path(tempfile.mkdtemp()) / "p.yaml",
                                  "id: c\nagent: codex\npricing: {input_per_mtok: 1.0, cached_input_per_mtok: 0.1, output_per_mtok: 10}\n"))
        r = CodexAdapter(executable=["x"]).parse(lines, prof)
        self.assertIsNone(r.error_type)
        self.assertEqual(r.skill_calls, ["foo"])
        self.assertEqual([c.name for c in r.tool_calls], ["shell", "shell", "mcp__db__q", "collab:spawn_agent"])
        self.assertEqual([c.is_error for c in r.tool_calls], [False, True, False, False])
        self.assertEqual(r.subagent_calls, ["spawn_agent"])
        self.assertEqual(r.final_message, "final")
        self.assertAlmostEqual(r.cost_usd, 0.6 + 0.04 + 1.0)
        self.assertEqual(r.cost_source, "token_estimate")

    def test_codex_turn_failed(self):
        lines = [json.dumps({"type": "turn.failed", "error": {"message": "429 Too Many Requests"}})]
        self.assertEqual(CodexAdapter(executable=["x"]).parse(lines).error_type, "rate_limit")


class MetricTests(unittest.TestCase):
    def test_pass_hat_k(self):
        self.assertEqual(pass_hat_k(3, 3, 3), 1.0)
        self.assertEqual(pass_hat_k(3, 2, 3), 0.0)
        self.assertIsNone(pass_hat_k(2, 2, 3))
        self.assertAlmostEqual(pass_hat_k(4, 3, 2), 3 / 6)

    def test_summary_delta_gate(self):
        rs = []
        for i, ok in enumerate([True, True, True]):
            rs.append(rec(profile_id="h", task_id="a", trial=i, passed=ok, score=1.0, cmp_passed=ok, cmp_score=1.0, cost_usd=0.1))
        for i, ok in enumerate([True, False, True]):
            rs.append(rec(profile_id="b", task_id="a", trial=i, passed=ok, score=1.0 if ok else 0.5, cmp_passed=ok,
                          cmp_score=1.0 if ok else 0.5, cost_usd=0.1, failure_tags=[] if ok else ["wrong_output"]))
        rs.append(rec(profile_id="b", task_id="a", trial=3, valid=False, error={"type": "rate_limit"}))
        s = summarize(rs, 3)
        self.assertEqual(s["profiles"]["h"]["pass_hat_k"], 1.0)
        self.assertAlmostEqual(s["profiles"]["b"]["pass_at_1"], 2 / 3)
        self.assertEqual(s["profiles"]["b"]["excluded_reasons"], {"rate_limit": 1})
        self.assertEqual(s["profiles"]["b"]["failure_tags"], {"wrong_output": 1})
        self.assertAlmostEqual(s["profiles"]["b"]["cost_per_pass_usd"], 0.15)
        d = deltas(s, "b")["h"]
        self.assertAlmostEqual(d["pass_at_1"], 1 / 3)
        self.assertEqual(d["pass_hat_k"], 1.0)
        g = lambda cur, prev: gate(cur, prev, metric="pass_hat_k", margin=0.1, min_tasks=1)
        self.assertEqual((g(s, s)["problems"], g(s, s)["regressions"]), ([], []))
        worse = summarize([r for r in rs if r.profile_id == "b"] + [rec(profile_id="h", task_id="a", trial=i, passed=i != 1, score=1.0) for i in range(3)], 3)
        self.assertTrue(any("h/a" in p for p in g(worse, s)["regressions"]))

    def test_gate_refuses_incomparable_runs(self):
        s = summarize([rec(task_id="a", passed=True, score=1.0)], 1)
        meta = lambda llm, h, uses=True: {"options": {"llm": llm}, "tasks": {"a": {"hash": h, "llm": uses}}}
        g = lambda cur, prev: gate(cur, prev, metric="pass_hat_k", margin=0.1, min_tasks=1)["problems"]
        self.assertEqual(g({**s, "meta": meta(True, "h1")}, {**s, "meta": meta(True, "h1")}), [])
        self.assertTrue(any("LLM 判定" in x for x in g({**s, "meta": meta(False, "h1")}, {**s, "meta": meta(True, "h1")})))
        # llm グレーダーを使うタスクがなければ、LLM 判定の有無が違っても比較できる
        self.assertEqual(g({**s, "meta": meta(False, "h1", False)}, {**s, "meta": meta(True, "h1", False)}), [])
        self.assertTrue(any("p/a" in x and "比較できません" in x for x in g({**s, "meta": meta(True, "h2")}, {**s, "meta": meta(True, "h1")})))

    def test_unknown_cost_not_zero_filled(self):
        s = summarize([rec(passed=True, score=1.0, cost_usd=None)], 1)
        self.assertIsNone(s["profiles"]["p"]["total_cost_usd"])
        self.assertIsNone(s["profiles"]["p"]["cost_per_pass_usd"])

    def test_paired_bootstrap_and_decide(self):
        same = paired_bootstrap([0.0] * 12)
        self.assertEqual((same.n_tasks, same.estimate, same.low, same.high), (12, 0.0, 0.0, 0.0))
        self.assertEqual(paired_bootstrap([0.1, -0.3, 0.2, 0.0, 0.5]), paired_bootstrap([0.1, -0.3, 0.2, 0.0, 0.5]))  # 同じ seed なら同じ
        mixed = paired_bootstrap([0.5, -0.5] * 6)
        self.assertLess(mixed.low, 0.0)
        self.assertGreater(mixed.high, 0.0)
        self.assertEqual(decide(same, margin=0.05, min_tasks=10), "pass")
        self.assertEqual(decide(paired_bootstrap([-0.5] * 12), margin=0.05, min_tasks=10), "fail")
        self.assertEqual(decide(mixed, margin=0.05, min_tasks=10), "inconclusive")
        self.assertEqual(decide(same, margin=0.05, min_tasks=13), "inconclusive")  # タスクが少なすぎる
        self.assertIsNone(paired_bootstrap([]).estimate)
        self.assertIsNone(paired_bootstrap([0.2]).low)

    def test_delta_uses_comparable_score(self):
        """配布物がないと通らない判定（skill_used min≥1 など）は、基準との差の計算から両側とも外す。"""
        rs = []
        for i in range(2):
            rs.append(rec(profile_id="harness", task_id="a", trial=i, passed=True, score=1.0, cmp_passed=True, cmp_score=1.0))
            rs.append(rec(profile_id="base", task_id="a", trial=i, passed=False, score=0.5, cmp_passed=True, cmp_score=1.0))
        s = summarize(rs, 2)
        self.assertEqual((s["tasks"]["base"]["a"]["pass_at_1"], s["tasks"]["base"]["a"]["pass_at_1_cmp"]), (0.0, 1.0))
        d = deltas(s, "base")["harness"]
        self.assertEqual(d["pass_at_1"], 0.0)  # スキルを呼んだこと自体は差に数えない（成果物は同じ）
        self.assertIsNone(d["pass_at_1_ci"])  # 1 タスクでは区間を出さない（タスク間のばらつきが測れない）
        self.assertTrue(d["comparable"])
        # 比較用の値がない旧形式（v0.3.0）の summary.json では、全判定の値で計算し、その旨を印で残す
        old = {"profiles": s["profiles"], "tasks": {pid: {t: {k: v for k, v in tv.items() if not k.endswith("_cmp")}
                                                            for t, tv in ts.items()} for pid, ts in s["tasks"].items()}}
        d_old = deltas(old, "base")["harness"]
        self.assertEqual((d_old["pass_at_1"], d_old["comparable"]), (1.0, False))

    def test_gate_decisions(self):
        def run(pass_rate: list[float], tags=None, k=1, llm=False):
            rs = [rec(profile_id="h", task_id=f"t{i}", trial=0, passed=ok >= 1, score=ok, cmp_passed=ok >= 1, cmp_score=ok,
                      failure_tags=(tags or []) if ok < 1 else [])
                  for i, ok in enumerate(pass_rate)]
            s = summarize(rs, k)
            s["meta"] = {"options": {"llm": llm}, "tasks": {f"t{i}": {"hash": "h", "llm": False} for i in range(len(pass_rate))}}
            return s
        prev = run([1.0] * 12)
        ok = gate(run([1.0] * 12), prev, metric="pass_at_1", margin=0.05, min_tasks=10)
        self.assertEqual((ok["decision"], ok["problems"], ok["regressions"]), ("pass", [], []))
        bad = gate(run([0.0] * 12), prev, metric="pass_at_1", margin=0.05, min_tasks=10)
        self.assertEqual(bad["decision"], "fail")
        self.assertEqual(len(bad["regressions"]), 12)  # タスク別の悪化は診断として列挙する
        one = gate(run([0.0] + [1.0] * 11), prev, metric="pass_at_1", margin=0.05, min_tasks=10)
        self.assertEqual(one["decision"], "inconclusive")  # 1 タスクの偶然の失敗だけでは止めない（v0.3.0 は止めていた）
        self.assertEqual(gate(run([1.0] * 12), prev, metric="pass_at_1", margin=0.05, min_tasks=20)["decision"], "inconclusive")
        unsafe = gate(run([0.0] + [1.0] * 11, tags=["unsafe_access"]), prev, metric="pass_at_1", margin=0.05, min_tasks=10)
        self.assertEqual(unsafe["decision"], "fail")  # 安全違反は 1 件でも止める
        self.assertTrue(any("unsafe_access" in p for p in unsafe["problems"]))
        # 追加の分類を指定しても既定の unsafe_access は外れない（置き換えではなく追加）
        extra = gate(run([0.0] + [1.0] * 11, tags=["unsafe_access"]), prev, metric="pass_at_1", margin=0.05, min_tasks=10,
                     extra_hard=["data_loss"])
        self.assertEqual(extra["decision"], "fail")
        custom = gate(run([0.0] + [1.0] * 11, tags=["data_loss"]), prev, metric="pass_at_1", margin=0.05, min_tasks=10,
                      extra_hard=["data_loss"])
        self.assertEqual(custom["decision"], "fail")
        incomparable = gate(run([1.0] * 12, k=3), prev, metric="pass_hat_k", margin=0.05, min_tasks=10)
        self.assertEqual(incomparable["decision"], "fail")
        self.assertTrue(any("k が異なる" in p for p in incomparable["problems"]))


class GraderTests(unittest.TestCase):
    def setUp(self):
        self.wd = Path(tempfile.mkdtemp()) / "workdir"
        self.wd.mkdir()
        write(self.wd / "out.md", "| 月 | 売上合計 |\n")

    def g(self, t, **p):
        return Grader(t, p)

    def run_g(self, graders, r, judge=None):
        grade_all(graders, r, self.wd, judge or JudgeConfig(enabled=False))
        return [x["status"] for x in r.grader_results]

    def test_code_graders(self):
        r = rec(final_message="答えは 391", tool_calls=[{"name": "Bash", "input": {"command": "rm -rf x"}, "from_subagent": False}],
                skill_calls=["plugin:foo"], subagent_calls=["reviewer"])
        gs = [self.g("file_exists", path="out.md"), self.g("file_contains", path="out.md", pattern=r"^\| 月"),
              self.g("regex", pattern=r"\b391\b"), self.g("tool_used", tool="Bash", input_match=r"rm\s+-rf", max=0),
              self.g("skill_used", skill="foo"), self.g("subagent_used", name="reviewer"),
              self.g("command", run='{python} -c "import sys; sys.exit(0)"'), self.g("file_exists", path="../escape")]
        self.assertEqual(self.run_g(gs, r), ["pass", "pass", "pass", "fail", "pass", "pass", "pass", "fail"])
        self.assertIn("作業ディレクトリ外", r.grader_results[-1]["reason"])
        self.assertAlmostEqual(r.score, 6 / 8)

    def test_hook_used(self):
        hooks = [{"name": "SessionStart:startup", "exit_code": 0, "outcome": "success"},
                 {"name": "PreToolUse:Bash", "exit_code": 0, "outcome": "success"},
                 {"name": "PreToolUse:Bash", "exit_code": 2, "outcome": "error"}]
        gs = [self.g("hook_used", hook="PreToolUse:Bash", outcome="error"),  # ブロックが 1 回以上
              self.g("hook_used", hook="PreToolUse:.*", min=2),
              self.g("hook_used", hook="Stop"),
              self.g("regex", pattern="x")]
        self.assertEqual(self.run_g(gs, rec(hook_calls=hooks, final_message="x")), ["pass", "pass", "fail", "pass"])
        # Codex は hooks を記録しない → not_run（スコアの分母から外し、LLM 判定の条件にも影響させない）
        r = rec(agent="codex", final_message="x")
        judged = []
        judge = JudgeConfig(enabled=True, run=lambda prompt: (judged.append(1) or "PASS\nok", 0.0))
        st = self.run_g([self.g("hook_used", hook="Stop"), self.g("regex", pattern="x"), self.g("llm", criteria="c")], r, judge)
        self.assertEqual(st, ["not_run", "pass", "pass"])
        self.assertEqual((r.score, len(judged)), (1.0, 1))

    def test_harness_only_graders(self):
        """配布物がないと通らない判定は harness_only と印を付け、比較用スコアから外す。LLM 判定の実行条件にも数えない。"""
        self.assertTrue(self.g("skill_used", skill="s").requires_harness)
        self.assertTrue(self.g("hook_used", hook="h", min=2).requires_harness)
        self.assertFalse(self.g("skill_used", skill="s", max=0).requires_harness)  # 「呼ばない」はどちらの側でも測れる
        self.assertFalse(Grader("subagent_used", {}, negate=True).requires_harness)
        self.assertFalse(self.g("tool_used", tool="Bash", min=1).requires_harness)
        judged = []
        judge = JudgeConfig(enabled=True, run=lambda p: (judged.append(1) or "PASS\nok", 0.0))
        r = rec(final_message="x")
        st = self.run_g([self.g("file_exists", path="out.md"), self.g("skill_used", skill="s"), self.g("llm", criteria="c")], r, judge)
        self.assertEqual(st, ["pass", "fail", "pass"])
        self.assertEqual([x["harness_only"] for x in r.grader_results], [False, True, False])
        self.assertEqual(len(judged), 1)  # スキル未使用（基準側では当然）でも、成果物の判定は比較のために実行する
        self.assertAlmostEqual(r.score, 2 / 3)
        self.assertEqual(r.cmp_score, 1.0)

    def test_negate_and_weight(self):
        r = rec()
        gs = [Grader("file_exists", {"path": "none.md"}, negate=True, weight=3), Grader("file_exists", {"path": "none.md"})]
        self.assertEqual(self.run_g(gs, r), ["pass", "fail"])
        self.assertAlmostEqual(r.score, 0.75)

    def test_llm_judge_modes(self):
        calls = []
        judge = JudgeConfig(enabled=True, run=lambda p: (calls.append(p) or ("PASS\n理由", 0.002)))
        r = rec(final_message="x")
        self.assertEqual(self.run_g([self.g("file_exists", path="out.md"), self.g("llm", criteria="c")], r, judge), ["pass", "pass"])
        self.assertEqual((len(calls), r.judge_cost_usd), (1, 0.002))
        self.assertIn("c", calls[0])
        r2 = rec(final_message="x")
        self.assertEqual(self.run_g([self.g("file_exists", path="none"), self.g("llm", criteria="c")], r2, judge), ["fail", "skipped_prior_fail"])
        self.assertEqual(len(calls), 1)  # コード判定が不合格なら判定モデルを呼ばない
        self.assertEqual(r2.score, 0.0)
        r2b = rec(final_message="x")  # 呼ばなかった判定は分母に入れない: 2 合格・1 不合格・判定（重み 3）省略 → 2/3
        self.run_g([self.g("file_exists", path="out.md"), self.g("regex", pattern="x"), self.g("file_exists", path="none"),
                    Grader("llm", {"criteria": "c"}, weight=3)], r2b, judge)
        self.assertAlmostEqual(r2b.score, 2 / 3)
        r3 = rec(final_message="x")
        self.assertEqual(self.run_g([self.g("file_exists", path="out.md"), self.g("llm", criteria="c")], r3), ["pass", "not_run"])
        self.assertEqual(r3.score, 1.0)  # not_run は分母から除外
        r4 = rec(final_message="x")
        unk = JudgeConfig(enabled=True, run=lambda p: ("UNKNOWN\n根拠不足", None))
        self.assertEqual(self.run_g([self.g("llm", criteria="c")], r4, unk), ["unknown"])
        self.assertEqual(r4.score, 0.0)


class BudgetTests(unittest.TestCase):
    def test_month_spent_per_agent_and_billing(self):
        """月の予算は Claude と Codex で別の口座。サブスクリプションの推定額は台帳に残すが予算には数えない。"""
        led = Path(tempfile.mkdtemp()) / "spend.jsonl"
        now = datetime.now().astimezone().isoformat()
        rows = [{"at": now, "usd": 1.0, "run_id": "r", "agent": "claude_code", "billing": "api"},
                {"at": now, "usd": 2.0, "run_id": "r", "agent": "codex", "billing": "api"},
                {"at": now, "usd": 5.0, "run_id": "r", "agent": "claude_code", "billing": "subscription"},
                {"at": now, "usd": 0.5, "run_id": "v0.3"},  # 口座の記録がない旧形式の行は、どちらの口座にも数える（安全側）
                {"at": "2000-01-01T00:00:00+00:00", "usd": 9.0, "run_id": "r", "agent": "claude_code", "billing": "api"}]
        led.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        self.assertAlmostEqual(month_spent(led, "claude_code"), 1.5)
        self.assertAlmostEqual(month_spent(led, "codex"), 2.5)
        append_spend(led, 0.25, "r2", "codex", "api")
        append_spend(led, None, "r2", "codex", "api")  # 費用不明は書かない
        self.assertAlmostEqual(month_spent(led, "codex"), 2.75)
        self.assertEqual(json.loads(led.read_text().splitlines()[-1])["billing"], "api")

    def test_profile_billing(self):
        tmp = Path(tempfile.mkdtemp())
        self.assertEqual(load_profile(write(tmp / "a.yaml", "id: a\nagent: claude_code\n")).billing, "api")
        self.assertEqual(load_profile(write(tmp / "b.yaml", "id: b\nagent: claude_code\nbilling: subscription\n")).billing, "subscription")
        with self.assertRaisesRegex(SpecError, "billing"):
            load_profile(write(tmp / "c.yaml", "id: c\nagent: claude_code\nbilling: free\n"))


class DoctorTests(unittest.TestCase):
    def test_route_variables(self):
        """経路（API キー・Bedrock・接続先）を変える変数を、プロファイルが決めたものと親の環境から引き継ぐものに分ける。"""
        tmp = Path(tempfile.mkdtemp())
        p = load_profile(write(tmp / "p.yaml", "id: a\nagent: claude_code\nenv: {CLAUDE_CODE_USE_BEDROCK: '1'}\n"
                                               "env_unset: [ANTHROPIC_BASE_URL]\n"))
        parent = {"ANTHROPIC_API_KEY": "sk-x", "CLAUDE_CODE_USE_BEDROCK": "1", "ANTHROPIC_BASE_URL": "http://x", "PATH": "/bin",
                  "OPENAI_API_KEY": "k", "AWS_BEARER_TOKEN_BEDROCK": "t"}
        self.assertEqual(route_variables(p, parent), (["CLAUDE_CODE_USE_BEDROCK"], ["ANTHROPIC_API_KEY", "AWS_BEARER_TOKEN_BEDROCK"]))
        c = load_profile(write(tmp / "c.yaml", "id: c\nagent: codex\n"))
        self.assertEqual(route_variables(c, parent), ([], ["OPENAI_API_KEY"]))
        # inherit_env: 親の値を使うことを「名前だけ」で宣言する（値はプロファイルに書かない）。宣言したものは決めた経路に入る
        d = load_profile(write(tmp / "d.yaml", "id: d\nagent: claude_code\ninherit_env: [ANTHROPIC_API_KEY, ANTHROPIC_MODEL]\n"))
        self.assertEqual(route_variables(d, parent),
                         (["ANTHROPIC_API_KEY"], ["ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK", "AWS_BEARER_TOKEN_BEDROCK"]))
        self.assertEqual(route_signature(d, parent), route_signature(d, {**parent, "ANTHROPIC_API_KEY": "別の値"}))  # 秘密の値は使わない
        self.assertNotIn("sk-x", route_signature(d, parent))
        # 秘密でない変数（モデルの別名の実体・接続先・切り替え）は値が変われば再実行する（値そのものは残さずハッシュだけ）
        m = load_profile(write(tmp / "m.yaml", "id: m\nagent: claude_code\ninherit_env: [ANTHROPIC_DEFAULT_SONNET_MODEL]\n"))
        s1 = route_signature(m, {"ANTHROPIC_DEFAULT_SONNET_MODEL": "model-a"})
        self.assertNotEqual(s1, route_signature(m, {"ANTHROPIC_DEFAULT_SONNET_MODEL": "model-b"}))
        self.assertNotIn("model-a", s1)
        self.assertNotEqual(route_signature(d, parent), route_signature(d, {k: v for k, v in parent.items() if k != "ANTHROPIC_API_KEY"}))
        with self.assertRaisesRegex(SpecError, "inherit_env"):
            load_profile(write(tmp / "e.yaml", "id: e\nagent: codex\ninherit_env: [ANTHROPIC_API_KEY]\n"))  # codex の経路の変数ではない

    def test_billing_not_in_profile_hash(self):
        tmp = Path(tempfile.mkdtemp())
        a = load_profile(write(tmp / "a.yaml", "id: a\nagent: claude_code\n")).content_hash()
        self.assertEqual(a, load_profile(write(tmp / "a.yaml", "id: a\nagent: claude_code\nbilling: subscription\n")).content_hash())

    def test_older_than_verified(self):
        self.assertTrue(older_than_verified("claude_code", "2.1.295 (Claude Code)"))
        self.assertFalse(older_than_verified("claude_code", "2.1.311 (Claude Code)"))
        self.assertFalse(older_than_verified("codex", "codex-cli 0.163.0"))
        self.assertIsNone(older_than_verified("codex", "unknown"))
        self.assertIsNone(older_than_verified("codex", None))


class ScheduleTests(unittest.TestCase):
    def test_adjacent_and_counterbalanced(self):
        """同じ (試行, タスク) の各プロファイルを続けて実行し、先に走るプロファイルを入れ替える（時間による偏りを打ち消す）。"""
        tmp = Path(tempfile.mkdtemp())
        tasks = [load_task(write(tmp / f"{t}.yaml", f"id: {t}\nprompt: x\ngraders: [{{type: regex, pattern: x}}]\n")) for t in ("a", "b")]
        profiles = [load_profile(write(tmp / f"{p}.yaml", f"id: {p}\nagent: claude_code\n")) for p in ("base", "harness")]
        pairs = [(t, p) for p in profiles for t in tasks]  # 実行器に渡る順（プロファイルごと）
        jobs = schedule(pairs, trials=2, seed=0)
        self.assertEqual(len(jobs), 8)
        blocks = [jobs[i:i + 2] for i in range(0, 8, 2)]
        for blk in blocks:  # 各ブロックは同じタスク・同じ試行の 2 プロファイル
            self.assertEqual({(t.id, i) for t, _, i in blk}, {(blk[0][0].id, blk[0][2])})
            self.assertEqual({p.id for _, p, _ in blk}, {"base", "harness"})
        for tid in ("a", "b"):  # タスクごとに、先に走るプロファイルが試行ごとに入れ替わる（pass^k の中で順序の偏りを打ち消す）
            firsts = [blk[0][1].id for blk in blocks if blk[0][0].id == tid]
            self.assertEqual(sorted(firsts), ["base", "harness"], tid)
        self.assertEqual([(t.id, p.id, i) for t, p, i in jobs], [(t.id, p.id, i) for t, p, i in schedule(pairs, 2, 0)])

    def test_profile_targets_respected(self):
        tmp = Path(tempfile.mkdtemp())
        t = load_task(write(tmp / "t.yaml", "id: t\nprompt: x\ntargets: [codex]\ngraders: [{type: regex, pattern: x}]\n"))
        p = load_profile(write(tmp / "p.yaml", "id: c\nagent: codex\n"))
        self.assertEqual([(a.id, b.id, i) for a, b, i in schedule([(t, p)], 3, 1)], [("t", "c", 0), ("t", "c", 1), ("t", "c", 2)])


class IsolationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_env_and_install(self):
        src = write(self.tmp / "h" / "skills" / "s" / "SKILL.md", "x")
        write(self.tmp / "h" / "CLAUDE.md", "rule")
        p = load_profile(write(self.tmp / "p.yaml", """id: a
agent: claude_code
env: {MY_VAR: "{config_dir}/x"}
env_unset: [DROP_ME]
install:
  - {copy: h/CLAUDE.md, to: "{config_dir}/"}
  - {copy: h/skills, to: "{config_dir}/skills"}
  - {run: '{python} -c "import os,pathlib; pathlib.Path(os.environ[\\"CLAUDE_CONFIG_DIR\\"],\\"ran\\").write_text(\\"1\\")"'}
"""))
        tp = make_trial_paths(self.tmp / "trial", ".claude")
        os.environ["CLAUDECODE"] = "1"
        os.environ["DROP_ME"] = "1"
        try:
            env = build_env(p, tp, "CLAUDE_CONFIG_DIR")
        finally:
            os.environ.pop("CLAUDECODE")
            os.environ.pop("DROP_ME")
        self.assertNotIn("CLAUDECODE", env)
        self.assertNotIn("DROP_ME", env)
        self.assertEqual(env["HOME"], str(tp.home))
        self.assertEqual(env["USERPROFILE"], str(tp.home))
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], str(tp.config_dir))
        self.assertEqual(env["MY_VAR"], f"{tp.config_dir}/x")
        run_install(p, tp, env)
        self.assertTrue((tp.config_dir / "CLAUDE.md").is_file())
        self.assertTrue((tp.config_dir / "skills" / "s" / "SKILL.md").is_file())
        self.assertTrue((tp.config_dir / "ran").is_file())
        self.assertTrue(src.is_file())

    def test_install_target_stays_in_trial_dir(self):
        write(self.tmp / "h" / "CLAUDE.md", "rule")
        tp = make_trial_paths(self.tmp / "trial", ".claude")
        p = load_profile(write(self.tmp / "p.yaml", "id: a\nagent: claude_code\ninstall: [{copy: h/CLAUDE.md, to: .claude/CLAUDE.md}]\n"))
        run_install(p, tp, build_env(p, tp, "CLAUDE_CONFIG_DIR"))  # 相対パスは一時ホーム基準
        self.assertTrue((tp.config_dir / "CLAUDE.md").is_file())
        for to in ("../../escape.md", str(self.tmp / "outside.md")):
            p = load_profile(write(self.tmp / "p.yaml", f"id: a\nagent: claude_code\ninstall: [{{copy: h/CLAUDE.md, to: '{to}'}}]\n"))
            with self.assertRaisesRegex(RuntimeError, "試行ディレクトリの外"):
                run_install(p, tp, build_env(p, tp, "CLAUDE_CONFIG_DIR"))
        self.assertFalse((self.tmp / "outside.md").exists())

    def test_install_failure_raises(self):
        p = load_profile(write(self.tmp / "p.yaml", "id: a\nagent: claude_code\ninstall: [{run: '{python} -c \"import sys; sys.exit(3)\"'}]\n"))
        tp = make_trial_paths(self.tmp / "trial", ".claude")
        with self.assertRaisesRegex(RuntimeError, "終了コード 3"):
            run_install(p, tp, build_env(p, tp, "CLAUDE_CONFIG_DIR"))

    def test_timeout_kills_child_tree(self):
        marker = self.tmp / "child_alive"
        code = ("import subprocess,sys,time;"
                f"subprocess.Popen([sys.executable,'-c','import time,pathlib;time.sleep(3);pathlib.Path(r\"{marker}\").write_text(\"1\")']);"
                "time.sleep(30)")
        t0 = time.monotonic()
        pr = run_process([sys.executable, "-c", code], self.tmp, dict(os.environ), None,
                         self.tmp / "o", self.tmp / "e", timeout_s=1)
        self.assertTrue(pr.timed_out)
        self.assertLess(time.monotonic() - t0, 15)
        time.sleep(4)
        self.assertFalse(marker.exists(), "タイムアウト時に孫プロセスが残っている")

    def test_launch_failure(self):
        pr = run_process(["definitely-not-a-command-xyz"], self.tmp, dict(os.environ), None, self.tmp / "o", self.tmp / "e", 5)
        self.assertIsNotNone(pr.launch_error)


class HarvestTests(unittest.TestCase):
    """履歴の読み取り（形式は Claude Code 2.1.308 の実機と Codex 0.163.0 のソースで確認したもの）と自動検査。"""
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def jl(self, path, rows):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
        return path

    def test_collect_and_select(self):
        from harness_eval.harvest import collect, select
        cl = self.tmp / "claude"
        self.jl(cl / "history.jsonl", [
            {"display": "[Pasted text #1 +3 lines] これを集計して", "pastedContents": {"1": {"id": 1, "type": "text",
             "content": "営業部からの依頼です。3月の店舗別売上をください"}}, "timestamp": 1792671029680, "sessionId": "s1"},
            {"display": "続けて", "pastedContents": {}, "timestamp": 1792671030000, "sessionId": "s1"},
            {"display": "/exit", "pastedContents": {}, "timestamp": 1792671090822, "sessionId": "s1"}])
        self.jl(cl / "projects" / "-wd" / "s2.jsonl", [
            {"type": "user", "sessionId": "s2", "timestamp": "2026-10-22T10:00:00Z", "promptSource": "typed", "turnOrigin": "human",
             "message": {"role": "user", "content": "<system-reminder>x</system-reminder>先月の決済データから上位加盟店を抽出してください"}},
            {"type": "user", "sessionId": "s2", "timestamp": "2026-10-22T10:01:00Z",
             "message": {"content": [{"type": "tool_result", "tool_use_id": "t", "content": "出力"}]}},
            {"type": "user", "sessionId": "s2", "timestamp": "2026-10-22T10:02:00Z", "turnOrigin": "task_notification",
             "message": {"content": "<task-notification>通知です通知です通知です</task-notification>"}},
            {"type": "user", "sessionId": "s2", "timestamp": "2026-10-22T10:03:00Z", "isMeta": True,
             "message": {"content": "メタ情報のメッセージです。これは使いません"}}])
        cx = self.tmp / "codex"
        self.jl(cx / "history.jsonl", [{"session_id": "c1", "ts": 1792671100, "text": "このSQLが遅い理由を調べて改善案を出してください"}])
        self.jl(cx / "sessions" / "2026" / "10" / "22" / "rollout-2026-10-22T10-00-00-abc.jsonl", [
            {"timestamp": "2026-10-22T11:00:00.000Z", "type": "session_meta", "payload": {"id": "abc"}},
            {"timestamp": "2026-10-22T11:00:01.000Z", "type": "event_msg", "payload": {"type": "user_message",
             "message": "このSQLが遅い理由を調べて改善案を出してください"}},
            {"timestamp": "2026-10-22T11:00:02.000Z", "type": "event_msg", "payload": {"type": "agent_message", "message": "x"}}])
        prompts = collect([cl, cx])
        texts = [p.text for p in prompts]
        self.assertIn("営業部からの依頼です。3月の店舗別売上をください これを集計して", texts)  # 貼り付けを展開
        self.assertIn("先月の決済データから上位加盟店を抽出してください", texts)       # system-reminder を除く
        self.assertFalse(any("通知" in t or "メタ" in t or "出力" == t for t in texts))
        picked = select(prompts, 10)
        self.assertEqual(sorted(p.text[:8] for p in picked), sorted(["営業部からの依頼", "先月の決済データ", "このSQLが遅い"]))
        self.assertEqual(len(select(prompts, 1)), 1)

    def test_leaks(self):
        from harness_eval.harvest import leaks
        original = "nova_txn_daily から 2026-03 の 1,234,567 円以上を db1.example.local で抽出。結果は summary.md へ"
        self.assertEqual(leaks(original, {"prompt": "架空の orders から 2025-11 分を抽出して report.md へ"}), 0)
        self.assertEqual(leaks(original, {"prompt": "nova_txn_daily を使う", "files": [{"content": "1234567"}]}), 2)
        self.assertEqual(leaks(original, {"prompt": "host は db1.example.local"}), 1)
        self.assertEqual(leaks(original, {"prompt": "2026-03 の分を summary.md へ"}), 2)  # 年月・ファイル名も元のまま使わない
        # 日本語に続く数値・全角・大文字小文字の違い・大文字の名前・文のコピーも拾う（レビューで見つかった漏れ）
        jp = "ＮＯＶＡ様の金額 3500000円以上、約12345件。Acme_Orders を使う。来月から始まる販促施策の効果を、店舗別と年代別に比較したい"
        self.assertEqual(leaks(jp, {"prompt": "架空の会社で 1000000 円以上"}), 0)
        self.assertEqual(leaks(jp, {"prompt": "NOVA の 3500000 と 12345 と acme_orders"}), 4)
        self.assertEqual(leaks(jp, {"prompt": "来月から始まる販促施策の効果を、店舗別と年代別に比較したい"}), 1)  # 25 文字以上の同じ文
        self.assertEqual(leaks(jp, {"prompt": "販促施策の効果を店舗別に比較したい"}), 0)  # 同じ種類の依頼に普通に出る短い言い回しは許す
        # 改行をまたぐコピー・年月の書き方の違いも拾う
        two_lines = "来月から始まる販促施策の効果を、\n店舗別と年代別に比較したい"
        self.assertEqual(leaks(two_lines, {"prompt": "来月から始まる販促施策の効果を、\n店舗別と年代別に比較したい"}), 1)
        self.assertEqual(leaks("対象は 2026年9月 の取引", {"prompt": "対象は 2026-09"}), 1)
        self.assertEqual(leaks("対象は 2026年9月 の取引", {"prompt": "対象は 2025-11"}), 0)
        # 4 桁の数値はデータ中の偶然の一致を許す（依頼文・採点にあれば漏れ）。5 桁以上はデータ中でも漏れ
        self.assertEqual(leaks("3000 円以上", {"prompt": "2500 円以上", "files": [{"path": "a.csv", "content": "id,amount\n1,3000"}]}), 0)
        self.assertEqual(leaks("3000 円以上", {"prompt": "3000 円以上"}), 1)
        self.assertEqual(leaks("ID 123456 の顧客", {"prompt": "ID 9 の顧客", "files": [{"path": "a.csv", "content": "id\n123456"}]}), 1)

    def test_paths_are_restricted(self):
        """設定ファイル（.claude/・.mcp.json・CLAUDE.md・AGENTS.md）や作業ディレクトリの外には置かせない。"""
        from harness_eval.harvest import _safe_rel
        for p in (".claude/settings.json", ".mcp.json", "CLAUDE.md", "sub/AGENTS.md", "AGENTS.override.md", "../x", "/etc/x"):
            self.assertFalse(_safe_rel(p), p)
        self.assertTrue(_safe_rel("data/orders.csv"))

    def test_history_kind_and_slash_commands(self):
        from harness_eval.harvest import collect, select, Prompt
        h = self.jl(self.tmp / "c" / "history.jsonl", [{"display": "session_id 列を使って集計する依頼を受けました。お願いします",
                                                       "pastedContents": {}, "timestamp": 1792671000000, "sessionId": "s"}])
        self.assertEqual([p.agent for p in collect([h])], ["claude_code"])  # 本文に session_id があっても Claude と判定
        ps = [Prompt("/model sonnet にして続けてください", "claude_code", "a", 1),
              Prompt("/home/data/x.csv を月別に集計してください", "claude_code", "b", 2)]
        self.assertEqual([p.session for p in select(ps, 5)], ["b"])

    def test_allowed_grader(self):
        from harness_eval.harvest import allowed_grader
        d = {"prompt": "結果は top.csv に保存してください", "needs_clarification": False}
        self.assertTrue(allowed_grader({"type": "file_exists", "path": "top.csv"}, d))
        self.assertTrue(allowed_grader({"type": "file_exists", "path": "result.csv"}, {"prompt": "結果はresult.csvに保存"}))  # 日本語に続く名前
        self.assertFalse(allowed_grader({"type": "file_exists", "path": "other.csv"}, d))   # 依頼に出てこないファイル
        self.assertFalse(allowed_grader({"type": "regex", "pattern": "つばめ旅行"}, d))      # 求めていない最終応答の内容
        self.assertTrue(allowed_grader({"type": "regex", "pattern": "[?？]"}, {**d, "needs_clarification": True}))
        self.assertTrue(allowed_grader({"type": "tool_used", "tool": "Write", "max": 0}, d))

    def test_check_and_selftest(self):
        """harvest では LLM が書いた採点スクリプトを実行しない（構文だけ）。実行は人が確認したあとの selftest。"""
        from harness_eval.harvest import Prompt, check_written, draft_problem, selftest, write_draft
        from harness_eval.spec import load_task
        marker = self.tmp / "ran.txt"
        check = (f"import sys\nfrom pathlib import Path\nopen({str(marker)!r}, 'a').close()  # 実行されたら印が残る\n"
                 "p = Path('total.txt')\nsys.exit(0 if p.is_file() and p.read_text().strip() == '4600' else 1)\n")
        good = {"id": "h-a", "prompt": "orders.csv の合計を total.txt に", "files": [{"path": "orders.csv", "content": "a\n1"}],
                "reference_outputs": [{"path": "total.txt", "content": "4600"}], "check_script": check,
                "final_answer_example": "4600", "graders": [{"type": "file_exists", "path": "total.txt"}]}
        origin = Prompt("x", "claude_code", "s", 0)
        root = self.tmp / "out"
        self.assertEqual(draft_problem(good), "")
        self.assertEqual(check_written(write_draft(good, root, origin)), "")
        self.assertFalse(marker.exists())
        self.assertTrue((root / "references" / "h-a" / "total.txt").is_file())
        self.assertEqual(selftest(load_task(root / "tasks" / "h-a.yaml")), "")  # 正解例で通り、何もしなければ落ちる
        self.assertTrue(marker.exists())  # selftest（人が確認したあと）では実行される
        always = {**good, "id": "h-b", "check_script": "import sys\nsys.exit(0)\n", "graders": []}
        self.assertEqual(check_written(write_draft(always, root, origin)), "")  # 実行しないので harvest では分からない
        self.assertIn("何もしなくても", selftest(load_task(root / "tasks" / "h-b.yaml")))
        wrong_ref = {**good, "id": "h-c", "reference_outputs": [{"path": "total.txt", "content": "1"}]}
        write_draft(wrong_ref, root, origin)
        self.assertIn("正解例で採点が通らない", selftest(load_task(root / "tasks" / "h-c.yaml")))
        # 宣言的な判定（ファイル・正規表現）は実行の心配がないので harvest で確かめる
        bad_decl = {**good, "id": "h-d", "check_script": "", "graders": [{"type": "file_contains", "path": "total.txt", "pattern": "9999"}]}
        self.assertIn("正解例で採点が通らない", check_written(write_draft(bad_decl, root, origin)))
        self.assertIn("パス", draft_problem({**good, "files": [{"path": "../../x.txt", "content": "x"}]}))
        no_outcome = {**good, "id": "h-e", "check_script": "", "graders": [{"type": "tool_used", "tool": "Write", "max": 0}]}
        self.assertIn("結果を見る採点がない", check_written(write_draft(no_outcome, root, origin)))
        broken = {**good, "id": "h-f", "check_script": "def (:\n"}
        self.assertIn("読めない", check_written(write_draft(broken, root, origin)))
        # 同梱タスクの正解例: 採点が正解例で通り、何もしなければ落ちる（採点そのものが壊れていないことの確認）
        for tid in ("csv-summary-format", "request-extract", "arithmetic-no-skill", "request-ambiguous"):
            self.assertEqual(selftest(load_task(ROOT / "examples" / "tasks" / f"{tid}.yaml")), "", tid)
        self.assertIsNone(selftest(load_task(ROOT / "examples" / "tasks" / "csv-summary-skill.yaml")))  # 正解例のないタスクは対象外
        # 他の判定が働いていても、何もしなくても通る判定（入力ファイルの有無など）は注意として挙げる（失敗にはしない:
        # 壊してはいけないものを守る判定も同じ形になるため）
        tdir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tdir, True)
        shutil.copytree(ROOT / "examples", tdir / "examples")
        tp = tdir / "examples" / "tasks" / "request-extract.yaml"
        tp.write_text(tp.read_text(encoding="utf-8") + "  - {type: file_exists, path: sales.csv, description: 入力がある}\n", encoding="utf-8")
        notes: list[str] = []
        self.assertEqual(selftest(load_task(tp), notes), "")
        self.assertEqual(notes, ["入力がある"])
        clarify = {**good, "id": "h-g", "needs_clarification": True, "check_script": "", "files": [], "reference_outputs": [],
                   "final_answer_example": "対象期間を教えていただけますか？", "graders": [{"type": "regex", "pattern": "[?？]"}]}
        self.assertEqual(check_written(write_draft(clarify, root, origin)), "")
        self.assertEqual(selftest(load_task(root / "tasks" / "h-g.yaml")), "")  # 確認の質問を見るタスクも selftest の対象
        text = (root / "tasks" / "h-a.yaml").read_text(encoding="utf-8")
        self.assertIn("Bash(ssh *)", text)  # 本物の環境に出る操作は常に禁止


FIXTURE_CSV = ROOT / "examples" / "fixtures" / "sales_synthetic" / "sales.csv"


class CheckScriptTests(unittest.TestCase):
    """examples/checks/check_monthly_totals.py を実出力で検証する（採点器自体の誤判定を防ぐ回帰テスト）。
    real_*.md は Claude Code (haiku) の実出力。初版の採点器はこれを誤って不合格にしていた。"""
    EXPECT = {"real_baseline_table_with_stores.md": 0, "real_baseline_report.md": 0, "skill_output.md": 0,
              "list_format.md": 0, "wrong_feb.md": 1, "missing_months.md": 1}

    def test_cases(self):
        import subprocess
        script = ROOT / "examples" / "checks" / "check_monthly_totals.py"
        for name, want in self.EXPECT.items():
            wd = Path(tempfile.mkdtemp())
            (wd / "summary.md").write_bytes((DATA / "summaries" / name).read_bytes())
            r = subprocess.run([sys.executable, str(script), str(FIXTURE_CSV)], cwd=wd, capture_output=True, text=True)
            self.assertEqual(r.returncode, want, f"{name}: {r.stdout}")

    def test_check_extract(self):
        """check_extract.py: CSV の列区切りと桁区切りを取り違えない（初版の誤りの回帰テスト）。"""
        import subprocess
        script = ROOT / "examples" / "checks" / "check_extract.py"
        for content, want in (("件数,合計金額\n5,73900\n", 0), ('合計金額,件数\n"73,900",5\n', 0),
                              ("件数,合計金額\n5,73800\n", 1), (None, 1)):
            wd = Path(tempfile.mkdtemp())
            if content is not None:
                (wd / "result.csv").write_text(content, encoding="utf-8")
            r = subprocess.run([sys.executable, str(script), str(FIXTURE_CSV)], cwd=wd, capture_output=True, text=True)
            self.assertEqual(r.returncode, want, f"{content!r}: {r.stdout}")

    def test_tampered_workdir_csv_is_ignored(self):
        """作業ディレクトリの sales.csv を空にしても、正解は元のフィクスチャから計算される。"""
        import subprocess
        wd = Path(tempfile.mkdtemp())
        (wd / "sales.csv").write_text("date,store,amount\n", encoding="utf-8")
        (wd / "summary.md").write_text("nothing\n", encoding="utf-8")
        script = ROOT / "examples" / "checks" / "check_monthly_totals.py"
        r = subprocess.run([sys.executable, str(script), str(FIXTURE_CSV)], cwd=wd, capture_output=True, text=True)
        self.assertEqual(r.returncode, 1, r.stdout)


if __name__ == "__main__":
    unittest.main()
