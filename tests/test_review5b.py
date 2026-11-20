"""PR #5 の独立レビュー（Codex）の指摘 #6〜#9 のテスト（判定の完全性）。指摘番号は XREVIEW-AHE5-astra.md のもの。"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from harness_eval.cheatproof import capture_tests, tamper_check  # noqa: E402
from harness_eval.record import TrialRecord  # noqa: E402
from harness_eval.runner import RunOptions, cache_key  # noqa: E402
from harness_eval.soundness import outcome, verdict  # noqa: E402
from test_soundness import rec, rules, runs, summary  # noqa: E402


class TestBodyTamper(unittest.TestCase):
    """#6: 既存テストの本体を、確認行の数を変えずに弱めても改ざんとして検出する（新規テストの追加は許可）。"""

    def setUp(self):
        self.wd = Path(tempfile.mkdtemp(prefix="r5b-"))
        (self.wd / "tests").mkdir()
        self.t = self.wd / "tests" / "test_a.py"
        self.t.write_text("def test_x():\n    assert add(2, 2) == 4\n", encoding="utf-8")
        self.before = capture_tests(self.wd)

    def kinds(self):
        return [f["kind"] for f in tamper_check(self.before, self.wd)]

    def test_weakened_assertion_with_same_count_is_detected(self):
        self.t.write_text("def test_x():\n    assert add(2, 2) == add(2, 2)\n", encoding="utf-8")
        self.assertIn("test_body_modified", self.kinds())
        self.t.write_text("def test_x():\n    assert True\n", encoding="utf-8")
        self.assertIn("test_body_modified", self.kinds())

    def test_insertion_only_weakening_is_detected(self):
        """行を消さない弱め方（先頭に return・assert の前に代入）も、追記ではない変更として検出する。"""
        self.t.write_text("def test_x():\n    return\n    assert add(2, 2) == 4\n", encoding="utf-8")
        self.assertIn("test_body_modified", self.kinds())
        self.t.write_text("def test_x():\n    add = lambda a, b: 4\n    assert add(2, 2) == 4\n", encoding="utf-8")
        self.assertIn("test_body_modified", self.kinds())

    def test_an_edited_file_that_is_no_longer_watched_is_flagged(self):
        """監視できなくなった（大きすぎる・UTF-8 でない）既存のテストファイルは、消えていなくても検出する。"""
        self.t.write_text(self.t.read_text(encoding="utf-8") + "# " + "x" * 600_000 + "\n", encoding="utf-8")
        self.assertIn("test_file_unwatched", self.kinds())

    def test_more_test_file_name_patterns_are_watched(self):
        for rel in ("tests.py", "pkg/foo_tests.py", "__tests__/a.js", "foo_test.go"):
            d = Path(tempfile.mkdtemp(prefix="r5b-pat-"))
            self.addCleanup(shutil.rmtree, d, True)
            f = d / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("assert 1 == 1\n", encoding="utf-8")
            before = capture_tests(d)
            self.assertIn(rel, before, rel)
            f.write_text("assert True\n", encoding="utf-8")
            self.assertTrue(tamper_check(before, d), rel)

    def test_appending_new_tests_and_new_files_is_allowed(self):
        self.t.write_text(self.t.read_text(encoding="utf-8") + "\n\ndef test_y():\n    assert add(1, 1) == 2\n", encoding="utf-8")
        (self.wd / "tests" / "test_new.py").write_text("def test_z():\n    assert 1\n", encoding="utf-8")
        self.assertEqual(self.kinds(), [])

    def test_appending_a_replacement_test_is_detected(self):
        self.t.write_text(self.before["tests/test_a.py"] + "\n\ndef test_x():\n    assert True\n", encoding="utf-8")
        self.assertIn("test_body_modified", self.kinds())

    def test_method_redefinition_is_checked_within_its_class(self):
        original = "class TestA:\n    def test_x(self):\n        assert False\n"
        self.t.write_text(original, encoding="utf-8")
        self.before = capture_tests(self.wd)
        self.t.write_text(original + "\n    def test_x(self):\n        assert True\n", encoding="utf-8")
        self.assertIn("test_body_modified", self.kinds())
        self.t.write_text(original + "\nclass TestB:\n    def test_x(self):\n        assert True\n", encoding="utf-8")
        self.assertEqual(self.kinds(), [])


class TestNoLlmCacheAndVerdict(unittest.TestCase):
    """#7: --no-llm の結果を LLM 判定ありの実行で再利用しない。未実施の必須条件がある結果は採用不可。"""

    def test_cache_key_depends_on_llm_mode(self):
        from harness_eval.spec import load_profile, load_task
        ex = ROOT / "examples"
        task = load_task(ex / "tasks" / "csv-summary-insight.yaml")
        prof = load_profile(ex / "profiles" / "claude-baseline.yaml")
        a = cache_key(task, prof, "1", 0, RunOptions(llm=True))
        b = cache_key(task, prof, "1", 0, RunOptions(llm=False))
        self.assertNotEqual(a, b)
        self.assertEqual(a, cache_key(task, prof, "1", 0, RunOptions(llm=True, tier="decisive")))  # 段階では分けない

    def test_skipped_required_llm_condition_blocks_adoption(self):
        recs = runs({f"t{i}": (2, 4) for i in range(8)}, 4)
        for r in recs:
            r.grader_results.append({"type": "llm", "status": "not_run", "reason": "--no-llm", "harness_only": False})
        v = verdict(recs, summary(), rules())
        self.assertEqual(v["conclusion"], "INCONCLUSIVE", v["reasons"])
        self.assertTrue(any("未実施" in x for x in v["reasons"]), v["reasons"])
        # 既存の前段の失敗による省略（skipped_prior_fail）は未実施の必須条件ではない
        recs = runs({f"t{i}": (2, 4) for i in range(8)}, 4)
        for r in recs:
            r.grader_results.append({"type": "llm", "status": "skipped_prior_fail", "reason": "x", "harness_only": False})
        self.assertEqual(verdict(recs, summary(), rules())["conclusion"], "IMPROVED")


class TestCmpPassedNull(unittest.TestCase):
    """#8: 現行形式の明示的な cmp_passed: null（配布物専用の判定だけ）は比較から外す。フィールドがない旧記録だけ passed にフォールバック。"""

    def base(self, **kw):
        d = rec("t", "cand", True).to_dict()
        d.update(kw)
        return d

    def test_explicit_null_is_excluded_and_legacy_falls_back(self):
        explicit = TrialRecord.from_dict(self.base(cmp_passed=None, passed=True))
        self.assertIsNone(outcome(explicit))
        legacy = self.base(passed=True)
        del legacy["cmp_passed"]
        self.assertTrue(outcome(TrialRecord.from_dict(legacy)))

    def test_explicit_null_stays_excluded_with_judge_disagreement(self):
        d = self.base(cmp_passed=None, passed=True, grader_results=[{"type": "llm", "status": "judge_disagreement"}])
        self.assertIsNone(outcome(TrialRecord.from_dict(d)))
        d["cmp_passed"] = True
        self.assertFalse(outcome(TrialRecord.from_dict(d)))

    def test_skill_only_task_does_not_count_as_an_improvement(self):
        recs = runs({f"t{i}": (2, 2) for i in range(8)}, 4)
        for i in range(4):  # スキル使用だけを問うタスク: 候補だけ passed、比較できる判定はない
            recs += [rec("skill-only", "base", False, i), rec("skill-only", "cand", True, i)]
        for r in recs:
            if r.task_id == "skill-only":
                r.cmp_passed = None
        v = verdict(recs, summary(), rules())
        self.assertNotIn("skill-only", v.get("per_task", {}) or {})
        self.assertNotEqual(v["conclusion"], "IMPROVED", v["reasons"])


class TestAllInvalidTask(unittest.TestCase):
    """#9: 両条件とも全試行が無効になった対象タスクも、データ不足として数える。"""

    def test_a_wiped_out_task_blocks_an_improvement(self):
        recs = runs({f"t{i}": (1, 2) for i in range(5)}, 2)  # 5 タスク × 各 2 回で最低条件を満たす
        for i in range(2):
            for prof in ("base", "cand"):
                recs.append(rec("wiped", prof, False, i, valid=False, error={"type": "agent_crash", "message": "x"}))
        v = verdict(recs, summary(), rules(min_tasks=5, max_error_rate=0.5))
        self.assertEqual(v["conclusion"], "INCONCLUSIVE", v["reasons"])
        self.assertTrue(any("wiped" in x for x in v["reasons"]), v["reasons"])



class TestSolverCostSettledOnce(unittest.TestCase):
    """#13: pricing のない Codex 試行も、公開価格で呼び出しごとに費用を確定し、予算・台帳・集計に同じ額を渡す。"""

    def test_codex_without_pricing_is_costed_in_budget_ledger_and_summary(self):
        import json
        import shutil
        from test_cheatproof import run_cli, write
        tmp = Path(tempfile.mkdtemp(prefix="r5b-cost-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        ex = tmp / "examples"
        shutil.copytree(ROOT / "examples", ex)
        prof = write(ex / "profiles" / "codex-priced.yaml",
                     f"id: codex-priced\nagent: codex\nmodel: gpt-6.2-sol\nsandbox: off\nexecutable: [{json.dumps(sys.executable)}, "
                     f"{json.dumps(str(ROOT / 'tests' / 'fake_agents' / 'fake_codex.py'))}]\n")
        out = tmp / "out"
        code, text = run_cli("run", "--tasks", str(ex / "tasks" / "arithmetic-no-skill.yaml"), "--profiles", str(prof), "--out", str(out),
                             "--no-cache", "--no-llm", "--cache-dir", str(tmp / "cache"))
        self.assertEqual(code, 0, text)
        run = sorted(d for d in out.iterdir() if (d / "summary.json").is_file())[-1]
        rec = json.loads((run / "trials.jsonl").read_text(encoding="utf-8").splitlines()[0])
        s = json.loads((run / "summary.json").read_text(encoding="utf-8"))
        want = (500 * 2 + 1500 * 0.10 + 100 * 10) / 1e6  # 偽 codex の使用量（入力 2000・キャッシュ読み 1500・出力 100）を gpt-6.2-sol の公開価格で
        self.assertAlmostEqual(rec["cost_usd"], want, places=9)
        self.assertEqual(rec["cost_source"], "price_table")
        ledger = [json.loads(x) for x in (out / "spend.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertAlmostEqual(sum(x["usd"] for x in ledger), want, places=9)
        self.assertAlmostEqual(s["meta"]["spent_usd_this_run"], want, places=9)
        self.assertAlmostEqual(s["usage"]["solver"]["cost_usd"], want, places=9)  # 予算・台帳・集計が同じ額


class TestInPlaceBudgetInsideLock(unittest.TestCase):
    """#14: 直列に待つ in_place の判定は、ロックの中で起動の直前に残額を確認し、費用を計上してから次を許可する。"""

    def test_waiting_votes_do_not_launch_after_the_cap(self):
        self.check_waiting_votes(timed_out=False)

    def test_timed_out_votes_settle_usage_before_releasing_lock(self):
        self.check_waiting_votes(timed_out=True)

    def check_waiting_votes(self, timed_out: bool) -> None:
        import json
        import threading
        from collections import defaultdict
        from types import SimpleNamespace
        from unittest import mock
        from harness_eval.judge_phase import JudgeBudgetStop, JudgePhase
        from harness_eval.graders import JudgeUnavailable
        from harness_eval.runner import Runner
        from harness_eval.isolation import ProcResult
        from harness_eval.spec import Profile

        tmp = Path(tempfile.mkdtemp(prefix="r5b-lock-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        real = tmp / "real-codex"
        real.mkdir()
        (real / "auth.json").write_text("{}", encoding="utf-8")
        p = type("P", (JudgePhase,), {"_spend": Runner._spend})()
        p.opt = SimpleNamespace(max_cost_usd=0.002, use_cache=False, cache_dir=tmp / "cache", log=None)
        p.run_dir = tmp / "run"
        p.judge = SimpleNamespace(tier="dev")
        p.lock = threading.Lock()
        p.spent, p.spent_api = 0.0, defaultdict(float)
        p.ledger, p.run_id, p.month_spent = tmp / "spend.jsonl", "r", None
        p.month_remaining = lambda family: None
        p._judge_billing = lambda family: "subscription"
        p._judge_seq = 0
        p._identities = {}
        p._inplace_locks = {"codex": threading.Lock(), "claude_code": threading.Lock()}
        p.judge_versions = {"codex": "x"}
        p.judge_usage = {"input": 0, "cache_read": 0, "cache_write": 0, "output": 0, "cost_usd": 0.0, "cost_unknown": 0, "calls": 0,
                         "cache_hits": 0, "by_model": {}}
        p.judge_profiles = {"codex": Profile(id="j", agent="codex", path=tmp / "j.yaml", credentials="in_place", billing="subscription")}
        p._judge_exe = lambda family: [sys.executable, "-c", "pass"]
        launched = []

        def fake_run(cmd, cwd, env, stdin_text, stdout_path, stderr_path, timeout_s):
            import time
            launched.append(1)
            time.sleep(0.1)
            lines = [{"type": "item.completed", "item": {"type": "agent_message", "text": "PASS\nok"}},
                     {"type": "turn.completed", "usage": {"input_tokens": 2000, "cached_input_tokens": 1500, "output_tokens": 50}}]
            Path(stdout_path).write_text("\n".join(json.dumps(x) for x in lines), encoding="utf-8")
            Path(stderr_path).write_text("", encoding="utf-8")
            return ProcResult(0, timed_out, 100)

        results = []

        def vote(i):
            try:
                p._judge_vote("codex", "gpt-6.2-sol", f"prompt {i}", "rubric", 0)
                results.append("ran")
            except JudgeBudgetStop:
                results.append("stopped")
            except JudgeUnavailable:
                results.append("unavailable")

        with mock.patch("harness_eval.judge_phase.run_process", fake_run), \
                mock.patch("harness_eval.judge_phase.credentials.source_path", return_value=real / "auth.json"):
            threads = [threading.Thread(target=vote, args=(i,)) for i in range(4)]
            [t.start() for t in threads]
            [t.join() for t in threads]
        # 1 回 $0.00165 < 上限 $0.002、2 回で超える。待っていた残りは起動しない
        self.assertEqual(len(launched), 2, results)
        self.assertCountEqual(results, ["unavailable" if timed_out else "ran"] * 2 + ["stopped"] * 2)
        self.assertEqual(p.judge_usage["input"], 4000)
        self.assertEqual(p.judge_usage["calls"], 2)
        self.assertAlmostEqual(p.judge_usage["cost_usd"], p.spent)
        self.assertEqual(len(p.ledger.read_text(encoding="utf-8").splitlines()), 2)


class TestRetryUsageCounted(unittest.TestCase):
    """提案: 再試行で捨てた呼び出しの使量も solver の使用量に足す（judge との費用比較が正確になる）。"""

    def test_usage_summary_adds_retry_usage(self):
        from harness_eval.costs import add_usage, empty_usage, usage_summary
        retry = empty_usage()
        add_usage(retry, {"input": 100, "output": 10}, 0.05)
        u = usage_summary([], None, {}, "dev", retry)
        self.assertEqual((u["solver"]["calls"], u["solver"]["input"], u["solver"]["cost_usd"]), (1, 100, 0.05))
        self.assertAlmostEqual(u["total_cost_usd"], 0.05)


if __name__ == "__main__":
    unittest.main()
