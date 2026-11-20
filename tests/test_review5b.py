"""PR #5 の独立レビュー（Codex）の指摘 #6〜#9 のテスト（判定の完全性）。指摘番号は XREVIEW-AHE5-astra.md のもの。"""
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

    def test_appending_new_tests_and_new_files_is_allowed(self):
        self.t.write_text(self.t.read_text(encoding="utf-8") + "\n\ndef test_y():\n    assert add(1, 1) == 2\n", encoding="utf-8")
        (self.wd / "tests" / "test_new.py").write_text("def test_z():\n    assert 1\n", encoding="utf-8")
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


if __name__ == "__main__":
    unittest.main()
