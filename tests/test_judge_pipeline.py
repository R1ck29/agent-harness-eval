"""判定役の接続（段階・別系統・盲検・最小の入力・順序・多数決・判定キャッシュ・校正・見積もり・予算・費用の分離・事前登録・verdict）の
E2E テスト。偽の claude / codex CLI を使う（モデルは呼ばない）。"""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from harness_eval.cli import main  # noqa: E402
from harness_eval.costs import resolve, token_cost  # noqa: E402
from harness_eval.record import TrialRecord  # noqa: E402

FAKE = ROOT / "tests" / "fake_agents"
PY = sys.executable
CLAUDE_J = ["--judge-executable", PY, str(FAKE / "fake_claude.py")]
CODEX_J = ["--judge-codex-executable", PY, str(FAKE / "fake_codex.py")]


def run_cli(*args: str) -> tuple[int, str]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        code = main(list(args))
    return code, buf.getvalue()


class JudgePipeline(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="he judge "))
        ex = self.tmp / "examples"
        shutil.copytree(ROOT / "examples", ex)
        for f in (ex / "profiles").glob("*.yaml"):
            fake = FAKE / ("fake_claude.py" if "claude" in f.name else "fake_codex.py")
            f.write_text(f.read_text(encoding="utf-8") + f"\nexecutable: [{json.dumps(PY)}, {json.dumps(str(fake))}]\n", encoding="utf-8")
        self.ex, self.out, self.cache = ex, self.tmp / "results", self.tmp / "cache"
        self.argv_log, self.prompt_log = self.tmp / "argv.jsonl", self.tmp / "judge_prompts.jsonl"
        self.env = {"FAKE_ARGV_LOG": str(self.argv_log), "FAKE_STATE_DIR": str(self.tmp), "FAKE_JUDGE_PROMPT_LOG": str(self.prompt_log)}
        os.environ.update(self.env)

    def tearDown(self):
        for k in [*self.env, "FAKE_JUDGE_USAGE", "FAKE_JUDGE_VERDICT", "FAKE_CODEX_JUDGE_VERDICT"]:
            os.environ.pop(k, None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def profiles(self, *names):
        return [str(self.ex / "profiles" / f"{n}.yaml") for n in names]

    def go(self, *extra, profiles=("claude-baseline", "claude-ds-lite"), task="csv-summary-insight"):
        args = ["run", "--tasks", str(self.ex / "tasks"), "--profiles", *self.profiles(*profiles), "--task", task,
                "--out", str(self.out), "--cache-dir", str(self.cache), "--baseline", "claude-baseline", *extra]
        code, out = run_cli(*args)
        runs = sorted(d for d in self.out.iterdir() if d.is_dir()) if self.out.is_dir() else []
        if not runs or not (runs[-1] / "summary.json").is_file():
            return code, out, None, []
        s = json.loads((runs[-1] / "summary.json").read_text(encoding="utf-8"))
        recs = [json.loads(l) for l in (runs[-1] / "trials.jsonl").read_text(encoding="utf-8").splitlines()]
        return code, out, s, recs

    def prompts(self):
        return [json.loads(l) for l in self.prompt_log.read_text(encoding="utf-8").splitlines()] if self.prompt_log.exists() else []

    @staticmethod
    def llm(rec):
        return [g for g in rec["grader_results"] if g["type"] == "llm"][0]

    # ---------------------------------------------------------------- 段階と系統
    def test_dev_default_cross_family_single_mid_vote(self):
        code, out, s, recs = self.go(*CLAUDE_J, *CODEX_J)
        self.assertEqual(code, 0, out)
        j = s["meta"]["judge"]
        self.assertEqual((j["tier"], j["votes"], j["blind"]), ("dev", 1, True))
        self.assertEqual(j["plan"]["claude_code"]["families"], ["codex"])  # claude の評価は別系統（codex）が判定する
        calls = self.prompts()
        self.assertEqual([(c["family"], c["model"]) for c in calls], [("codex", "gpt-6.2-sol")] * 2)  # 合格した 2 出力 × 1 回

    def test_decisive_both_families_three_votes_each(self):
        code, out, s, recs = self.go("--tier", "decisive", *CLAUDE_J, *CODEX_J)
        self.assertEqual(code, 0, out)
        calls = self.prompts()
        self.assertEqual(sorted({(c["family"], c["model"]) for c in calls}), [("claude_code", "opus"), ("codex", "gpt-6.2-astra")])
        self.assertEqual(len(calls), 12)  # 2 出力 × 2 系統 × 3 回
        by = {r["profile_id"]: r for r in recs}
        self.assertEqual(self.llm(by["claude-ds-lite"])["status"], "pass")
        self.assertTrue(s["meta"]["judge"]["plan"]["claude_code"]["both_families"])

    def test_both_families_disagree_is_not_a_pass(self):
        os.environ["FAKE_CODEX_JUDGE_VERDICT"] = "FAIL"  # codex は不合格、claude は合格 → 判定不一致
        code, out, s, recs = self.go("--tier", "decisive", *CLAUDE_J, *CODEX_J)
        r = {r["profile_id"]: r for r in recs}["claude-ds-lite"]
        self.assertEqual(self.llm(r)["status"], "judge_disagreement")
        self.assertFalse(r["passed"])

    def test_only_same_family_available_warns(self):
        code, out, s, recs = self.go("--judge-family", "claude_code", *CLAUDE_J)
        self.assertEqual(code, 0, out)
        self.assertTrue(s["meta"]["judge"]["plan"]["claude_code"]["same_family"])
        self.assertIn("同系統の判定役", out)
        self.assertIn("judge_family", {c["id"] for c in s["verdicts"][0]["checks"]})

    def test_weak_judge_requires_explicit_override_and_is_marked(self):
        code, out, s, _ = self.go("--judge-agent", "claude_code", "--judge-model", "haiku", *CLAUDE_J)
        self.assertEqual(code, 2, out)
        self.assertIn("--allow-weak-judge", out)
        self.assertFalse(self.argv_log.exists())  # 何も実行していない
        code, out, s, _ = self.go("--judge-agent", "claude_code", "--judge-model", "haiku", "--allow-weak-judge", *CLAUDE_J)
        self.assertEqual(code, 0, out)
        self.assertEqual(s["meta"]["judge"]["weak"], ["claude_code: haiku"])
        self.assertIn("弱い判定役", out)

    # ---------------------------------------------------------------- 決定的な検査が先・最小の入力・盲検
    def test_failed_output_is_not_judged(self):
        t = self.ex / "tasks" / "csv-summary-insight.yaml"
        t.write_text(t.read_text(encoding="utf-8").replace("graders:\n", "graders:\n  - type: file_exists\n    path: never.txt\n", 1),
                     encoding="utf-8")
        code, out, s, recs = self.go(*CLAUDE_J, *CODEX_J)
        self.assertTrue(all(self.llm(r)["status"] == "skipped_prior_fail" for r in recs))  # 決定的な検査に不合格 → 判定しない
        self.assertEqual(self.prompts(), [])
        self.assertEqual(s["usage"]["judge"]["calls"], 0)

    def test_judge_input_is_minimal_and_blinded(self):
        code, out, s, recs = self.go(*CLAUDE_J, *CODEX_J)
        calls = self.prompts()
        self.assertEqual(len(calls), 2)
        p = calls[0]["prompt"]
        for leak in ("claude-ds-lite", "claude-baseline", str(self.tmp), "sales.csv を月別に集計", "Claude", "claude"):
            self.assertNotIn(leak, p)  # プロファイル名・パス・依頼文（会話）・系統名は判定入力に残らない
        self.assertIn("評価観点", p)
        self.assertNotIn("tool_use", p)

    def test_blind_masks_profile_names_paths_and_vendor_terms(self):
        from harness_eval.judging import blind
        text = "claude-ds-lite で /tmp/x/y/z を見た。CLAUDE.md と Codex の .claude/skills/ds-lite を使った（opus）"
        b = blind(text, terms=["claude-ds-lite", "ds-lite"], paths=[])
        for t in ("claude-ds-lite", "/tmp/x", "CLAUDE.md", "Codex", "opus", "ds-lite"):
            self.assertNotIn(t, b)

    def test_blind_terms_include_installed_skill_and_file_names(self):
        """配布物の install（copy / to）から、複製したファイル名とスキル名を伏せる語に入れる。"""
        from harness_eval.runner import RunOptions, Runner
        from harness_eval.spec import load_profiles, load_tasks
        tasks = load_tasks([str(self.ex / "tasks" / "csv-summary-insight.yaml")])
        r = Runner(tasks, load_profiles(self.profiles("claude-ds-lite")), RunOptions(log=lambda s: None), self.out)
        terms, _ = r._blind_terms()
        for t in ("csv-monthly-summary", "CLAUDE.md", "skills"):
            self.assertIn(t, terms)
        self.assertFalse([t for t in terms if "{" in t])  # {config_dir} などの置換前の文字列は入れない

    def test_judge_order_is_shuffled_by_seed(self):
        from harness_eval.runner import RunOptions, Runner
        from harness_eval.spec import load_profiles, load_tasks
        tasks = load_tasks([str(self.ex / "tasks" / "csv-summary-insight.yaml")])
        profs = load_profiles(self.profiles("claude-ds-lite"))

        def order(seed):
            r = Runner(tasks, profs, RunOptions(seed=seed, judge_agent="claude_code", judge_executable=[PY, str(FAKE / "fake_claude.py")],
                                                log=lambda s: None), self.out)
            seen = []
            r.judge_one = lambda rec, it: seen.append(rec.trial) or "pass"
            recs = []
            for i in range(12):
                rec = TrialRecord(run_id="x", task_id="t", profile_id="p", agent="claude_code", trial=i, cache_key="k", task_hash="h",
                                  profile_hash="p", cli_version=None,
                                  grader_results=[{"type": "llm", "status": "pending", "harness_only": False, "weight": 1.0, "category": ""}])
                rec.judge_inputs = [{"kind": "rubric", "grader": 0, "criteria": "c", "focus": "final", "content": "x"}]
                recs.append(rec)
            r._judge_phase(recs)
            return seen
        self.assertEqual(order(1), order(1))
        self.assertNotEqual(order(1), order(2))
        self.assertNotEqual(order(1), sorted(order(1)))

    # ---------------------------------------------------------------- キャッシュ
    def test_judge_cache_hit_and_tier_switch_reuses_solver_trials(self):
        self.go(*CLAUDE_J, *CODEX_J)
        n_judge, n_all = len(self.prompts()), len(self.argv_log.read_text().splitlines())
        code, out, s, recs = self.go(*CLAUDE_J, *CODEX_J)  # 同じ出力 → 判定もキャッシュ
        self.assertEqual(len(self.prompts()), n_judge)
        self.assertEqual(len(self.argv_log.read_text().splitlines()), n_all)
        self.assertEqual(s["meta"]["judge"]["usage"]["cache_hits"], 2)
        self.assertTrue(all(r["from_cache"] for r in recs))
        code, out, s, recs = self.go("--tier", "decisive", *CLAUDE_J, *CODEX_J)  # 段階を変える → 解く側は再実行しない、判定だけ
        self.assertTrue(all(r["from_cache"] for r in recs))
        self.assertEqual(len(self.prompts()), n_judge + 12)
        self.assertEqual(s["meta"]["judge"]["tier"], "decisive")

    # ---------------------------------------------------------------- 見積もり・予算・費用
    def test_estimate_is_shown_per_tier_and_role(self):
        code, out = run_cli("plan", "--tasks", str(self.ex / "tasks"), "--profiles", *self.profiles("claude-ds-lite"),
                            "--task", "csv-summary-insight", "--out", str(self.out), "--cache-dir", str(self.cache), *CLAUDE_J, *CODEX_J)
        self.assertEqual(code, 0, out)
        for s in ("解く側（solver）の概算", "判定側（judge）dev", "判定側（judge）decisive", "← この実行"):
            self.assertIn(s, out)

    def test_budget_stops_before_running(self):
        code, out, s, _ = self.go("--tier", "decisive", "--max-cost-usd", "0.0001", *CLAUDE_J, *CODEX_J)
        self.assertEqual(code, 2, out)
        self.assertIn("止めました", out)
        self.assertFalse(self.argv_log.exists())  # 解く側も判定側も呼んでいない

    def test_solver_and_judge_usage_are_separate_and_priced(self):
        os.environ["FAKE_JUDGE_USAGE"] = "1"
        code, out, s, recs = self.go("--judge-agent", "claude_code", *CLAUDE_J)
        u = s["usage"]
        self.assertEqual(u["judge"]["calls"], 2)
        self.assertEqual((u["judge"]["input"], u["judge"]["cache_read"], u["judge"]["cache_write"], u["judge"]["output"]),
                         (2000, 1000, 200, 40))
        # dev の claude 判定は sonnet: 2 回 ×（1000×2 + 500×0.10 + 100×2.50 + 20×10）/ 1e6
        self.assertAlmostEqual(u["judge"]["cost_usd"], 2 * (1000 * 2 + 500 * 0.10 + 100 * 2.50 + 20 * 10) / 1e6)
        self.assertEqual(u["solver"]["calls"], 2)
        self.assertAlmostEqual(u["total_cost_usd"], u["solver"]["cost_usd"] + u["judge"]["cost_usd"])
        self.assertAlmostEqual(u["judge_share"], u["judge"]["cost_usd"] / u["total_cost_usd"])
        self.assertIn("判定側（judge）", out)

    def test_report_and_cli_lead_with_the_verdict(self):
        code, out, s, recs = self.go("--no-llm", "--trials", "2", task="arithmetic-no-skill")
        run_dir = sorted(d for d in self.out.iterdir() if d.is_dir())[-1]
        html = (run_dir / "report.html").read_text(encoding="utf-8")
        self.assertLess(html.index("<h2>結論</h2>"), html.index("<h2>サマリー</h2>"))
        self.assertIn(s["verdicts"][0]["label"], html)
        self.assertIn("ベースライン合格率", html)
        self.assertIn("解く側（solver）", html)
        self.assertLess(out.index("結論 "), out.index("プロファイル "))
        self.assertIn(s["verdicts"][0]["conclusion"], ("IMPROVED", "NO CHANGE", "REGRESSED", "INCONCLUSIVE"))

    def test_codex_judge_cost_from_price_table(self):
        code, out, s, recs = self.go(*CLAUDE_J, *CODEX_J)
        # OpenAI の定義: input 2000 のうち 1500 がキャッシュ読み。dev の codex 判定は gpt-6.2-sol
        self.assertAlmostEqual(s["usage"]["judge"]["cost_usd"], 2 * (500 * 2 + 1500 * 0.10 + 50 * 10) / 1e6)

    def test_price_table_has_sources_and_no_guess(self):
        self.assertEqual(resolve("opus")[0], "claude-opus-5-5")
        self.assertEqual(resolve("claude-sonnet-5-5-20261101")[0], "claude-sonnet-5-5")
        self.assertIsNone(token_cost({"input": 10, "output": 1}, "some-unknown-model", "codex"))

    # ---------------------------------------------------------------- 校正
    def test_calibration_measures_agreement_with_deterministic_grader(self):
        t = self.ex / "tasks" / "csv-summary-insight.yaml"
        t.write_text(t.read_text(encoding="utf-8") + "calibration:\n  focus: summary.md\n  criteria: |\n"
                     "    PASS: 2026-03 の店舗別の考察がある。FAIL: ない。\n", encoding="utf-8")
        code, out, s, recs = self.go("--trials", "2", "--calibration-items", "4", *CLAUDE_J, *CODEX_J)
        cal = s["meta"]["judge"]["calibration"]
        self.assertTrue(cal["needed"])
        self.assertGreaterEqual(cal["items"], 2)
        self.assertIsNotNone(cal["rate"])
        self.assertEqual(cal["agree"], sum(v for k, v in cal["confusion"].items() if k.split(" / ")[0][4:] == k.split(" / ")[1][4:]))

    def test_calibration_takes_both_passing_and_failing_outputs(self):
        from harness_eval.judging import pick_calibration
        items = [{"answer": True}] * 10 + [{"answer": False}] * 3
        got = pick_calibration(items, 6, 0, lambda x: x["answer"])
        self.assertEqual((len(got), sum(not x["answer"] for x in got)), (6, 3))

    def test_no_llm_needs_no_calibration(self):
        code, out, s, recs = self.go("--no-llm", task="arithmetic-no-skill")
        self.assertFalse(s["meta"]["judge"]["llm"])

    # ---------------------------------------------------------------- 事前登録・verdict
    def test_prereg_saved_before_trials_and_post_hoc_is_marked(self):
        code, out, s, recs = self.go("--no-llm", "--trials", "2", task="arithmetic-no-skill")
        run_dir = sorted(d for d in self.out.iterdir() if d.is_dir())[-1]
        pre = json.loads((run_dir / "prereg.json").read_text(encoding="utf-8"))
        self.assertEqual(pre["rules"]["baseline"], "claude-baseline")
        self.assertLess(pre["registered_at"], s["meta"]["finished_at"])
        self.assertEqual(s["verdicts"][0]["prereg"]["status"], "registered")
        code, out = run_cli("verdict", str(run_dir), "--json")
        v = json.loads(out)
        self.assertEqual(v["prereg"]["status"], "registered")
        self.assertEqual(code, v["exit_code"])
        rules = self.tmp / "rules.yaml"
        rules.write_text("ceiling: 0.95\n", encoding="utf-8")
        code, out = run_cli("verdict", str(run_dir), "--rules", str(rules))
        self.assertIn("事後", out)

    def test_verdict_exit_codes_on_recorded_results(self):
        from test_soundness import runs as mk
        cases = {"IMPROVED": ({f"t{i}": (2, 4) for i in range(8)}, 4, 0), "INCONCLUSIVE": ({f"t{i}": (4, 4) for i in range(8)}, 4, 3),
                 "REGRESSED": ({f"t{i}": (2, 0) for i in range(8)}, 4, 1), "NO CHANGE": ({f"t{i}": (20, 20) for i in range(12)}, 40, 4)}
        for name, (spec, trials, want) in cases.items():
            with self.subTest(name=name):
                d = self.tmp / name.replace(" ", "_")
                d.mkdir()
                recs = mk(spec, trials)
                (d / "trials.jsonl").write_text("".join(json.dumps(r.to_dict(), ensure_ascii=False) + "\n" for r in recs), encoding="utf-8")
                (d / "summary.json").write_text(json.dumps({"meta": {"tasks": {}, "options": {}}}), encoding="utf-8")
                code, out = run_cli("verdict", str(d), "--baseline", "base", "--candidate", "cand", "--json")
                v = json.loads(out)
                self.assertEqual((v["conclusion"], code), (name, want), v["reasons"])


    def test_verdict_bad_rules_file_is_a_definition_error(self):
        """--rules の YAML が壊れている・ない → 終了コード 2（REGRESSED の 1 と区別する）。"""
        from test_soundness import runs as mk
        d = self.tmp / "bad-rules"
        d.mkdir()
        recs = mk({f"t{i}": (2, 4) for i in range(8)}, 4)
        (d / "trials.jsonl").write_text("".join(json.dumps(r.to_dict(), ensure_ascii=False) + "\n" for r in recs), encoding="utf-8")
        (d / "summary.json").write_text(json.dumps({"meta": {"tasks": {}, "options": {}}}), encoding="utf-8")
        (d / "rules.yaml").write_text("ceiling: [unclosed\n", encoding="utf-8")
        for rules in (d / "rules.yaml", d / "missing.yaml"):
            code, out = run_cli("verdict", str(d), "--baseline", "base", "--candidate", "cand", "--rules", str(rules))
            self.assertEqual(code, 2, out)
            self.assertIn("判定規則を読めません", out)

if __name__ == "__main__":
    unittest.main()
