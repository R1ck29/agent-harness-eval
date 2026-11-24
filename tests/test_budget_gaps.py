"""費用上限の穴（レビューで出た 2 件）のテスト。偽の CLI だけを使う（モデルは呼ばない）。
1. 開始前の費用上限: 履歴のない解く側の呼び出しを $0 と数えない（タスクの max_budget_usd、なければ既定の見積もり）
2. 実行中に上限へ達したら、判定は呼ばずに budget_stop として除外する（JudgeBudgetStop の直接テスト）"""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from harness_eval.cli import main  # noqa: E402
from harness_eval.costs import DEFAULT_SOLVER_CALL_USD  # noqa: E402
from harness_eval.judge_phase import JudgeBudgetStop, JudgePhase  # noqa: E402
from harness_eval.runner import Runner  # noqa: E402
from fake_env import SUFFIX  # noqa: E402

FAKE = ROOT / "tests" / "fake_agents"
PY = sys.executable


def run_cli(*args: str) -> tuple[int, str]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        code = main(list(args))
    return code, buf.getvalue()


class StartBudget(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="he budget "))
        self.ex = self.tmp / "examples"
        shutil.copytree(ROOT / "examples", self.ex)
        for f in (self.ex / "profiles").glob("*.yaml"):
            fake = FAKE / ("fake_claude.py" if "claude" in f.name else "fake_codex.py")
            f.write_text(f.read_text(encoding="utf-8") + f"\nexecutable: [{json.dumps(PY)}, {json.dumps(str(fake))}]\n" + SUFFIX, encoding="utf-8")
        self.argv_log = self.tmp / "argv.jsonl"
        self.patch = mock.patch.dict(os.environ, {"FAKE_ARGV_LOG": str(self.argv_log), "FAKE_STATE_DIR": str(self.tmp)})
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def args(self, cmd: str, task: str, *extra: str):
        return [cmd, "--tasks", str(self.ex / "tasks" / f"{task}.yaml"), "--include-sample-harness",
                "--profiles", str(self.ex / "profiles" / "claude-baseline.yaml"), str(self.ex / "profiles" / "claude-ds-lite.yaml"),
                "--out", str(self.tmp / "out"), "--cache-dir", str(self.tmp / "cache"), "--baseline", "claude-baseline", "--no-llm", *extra]

    def test_call_without_history_uses_the_task_cap_not_zero(self):
        code, out = run_cli(*self.args("plan", "csv-summary-format"))  # limits.max_budget_usd: 0.5 × 2 プロファイル
        self.assertEqual(code, 0, out)
        self.assertIn("解く側（solver）の概算: $1.0000", out)
        self.assertIn("履歴がなく", out)

    def test_task_without_a_cap_uses_the_default_estimate(self):
        task = self.tmp / "nocap.yaml"
        task.write_text("id: nocap\nprompt: 17 × 23 は？\ngraders: [{type: regex, pattern: '391'}]\n", encoding="utf-8")
        code, out = run_cli("plan", "--tasks", str(task), "--profiles", str(self.ex / "profiles" / "claude-baseline.yaml"),
                            "--out", str(self.tmp / "out"), "--cache-dir", str(self.tmp / "cache"), "--no-llm")
        self.assertEqual(code, 0, out)
        self.assertIn(f"解く側（solver）の概算: ${DEFAULT_SOLVER_CALL_USD:.4f}", out)

    def test_run_stops_before_any_call_when_the_assumed_cost_exceeds_the_cap(self):
        code, out = run_cli(*self.args("run", "csv-summary-format", "--max-cost-usd", "0.1"))
        self.assertEqual(code, 2, out)
        self.assertIn("止めました", out)
        self.assertFalse(self.argv_log.exists())  # 解く側を 1 回も呼んでいない

    def test_history_replaces_the_assumption(self):
        code, out = run_cli(*self.args("run", "csv-summary-format", "--max-cost-usd", "5", "--trials", "1"))
        self.assertEqual(code, 0, out)  # 実測（偽の CLI は $0.05 前後）がキャッシュに残る
        code, out = run_cli(*self.args("plan", "csv-summary-format", "--trials", "2"))
        self.assertEqual(code, 0, out)
        self.assertNotIn("解く側（solver）の概算: $1.0000", out)  # 実測の平均に置き換わる（0.5 × 2 の仮定ではない）


class JudgeBudgetStopDirect(unittest.TestCase):
    """_judge_vote は上限に達していたら判定 CLI を呼ばずに JudgeBudgetStop を投げる。"""

    def stub(self, spent: float, cap: float | None, month_left: float | None = None, spent_api: float = 0.0):
        phase = type("P", (JudgePhase,), {})()
        phase.opt = SimpleNamespace(max_cost_usd=cap, use_cache=False, cache_dir=Path(tempfile.gettempdir()) / "he-no-cache")
        phase.judge = SimpleNamespace(tier="dev")
        phase.lock = threading.Lock()
        phase.spent = spent
        phase.spent_api = {"codex": spent_api}
        phase.month_remaining = lambda family: month_left
        phase._identity = lambda family, model: "id"
        phase._judge_profile = lambda family: SimpleNamespace(credentials=None)
        phase._judge_call = mock.Mock(side_effect=AssertionError("上限に達しているのに判定 CLI を呼んだ"))
        return phase

    def test_stops_when_the_run_cap_is_reached(self):
        for spent in (3.0, 3.5):
            p = self.stub(spent, 3.0)
            with self.assertRaises(JudgeBudgetStop):
                p._judge_vote("codex", "gpt-6.2-sol", "prompt", "rubric", 0)
            p._judge_call.assert_not_called()

    def test_stops_when_the_monthly_budget_is_used_up(self):
        p = self.stub(0.0, None, month_left=1.0, spent_api=1.0)
        with self.assertRaises(JudgeBudgetStop):
            p._judge_vote("codex", "gpt-6.2-sol", "prompt", "rubric", 0)
        p._judge_call.assert_not_called()

    def test_calls_when_under_the_cap(self):
        p = self.stub(0.5, 3.0)
        p._judge_call = mock.Mock(return_value=("PASS\nok", {"input": 1}, 0.01))
        row = p._judge_vote("codex", "gpt-6.2-sol", "prompt", "rubric", 0)
        self.assertEqual(row["status"], "pass")
        p._judge_call.assert_called_once()

    def test_run_marks_trials_budget_stop_and_never_calls_the_judge(self):
        """実行中に上限へ達した（開始前の見積もりは通した）→ 判定待ちの試行は budget_stop で除外され、判定 CLI は起動しない。"""
        tmp = Path(tempfile.mkdtemp(prefix="he budget run "))
        try:
            ex = tmp / "examples"
            shutil.copytree(ROOT / "examples", ex)
            for f in (ex / "profiles").glob("*.yaml"):
                fake = FAKE / ("fake_claude.py" if "claude" in f.name else "fake_codex.py")
                f.write_text(f.read_text(encoding="utf-8") + f"\nexecutable: [{json.dumps(PY)}, {json.dumps(str(fake))}]\n" + SUFFIX, encoding="utf-8")
            log = tmp / "argv.jsonl"
            with mock.patch.dict(os.environ, {"FAKE_ARGV_LOG": str(log), "FAKE_STATE_DIR": str(tmp)}), \
                    mock.patch.object(Runner, "estimated_total", return_value=0.0):  # 開始前の見積もりの門は通す
                code, out = run_cli("run", "--tasks", str(ex / "tasks" / "csv-summary-insight.yaml"), "--profiles",
                                    str(ex / "profiles" / "claude-baseline.yaml"), str(ex / "profiles" / "claude-ds-lite.yaml"),
                                    "--out", str(tmp / "out"), "--cache-dir", str(tmp / "cache"), "--baseline", "claude-baseline",
                                    "--max-cost-usd", "0.001", "--jobs", "1", "--no-cache",
                                    "--judge-codex-executable", PY, str(FAKE / "fake_codex.py"))
            self.assertEqual(code, 0, out)
            run = sorted(d for d in (tmp / "out").iterdir() if d.is_dir())[-1]
            recs = [json.loads(line) for line in (run / "trials.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertTrue(recs)
            self.assertTrue(all((r.get("error") or {}).get("type") == "budget_stop" and not r["valid"] for r in recs), recs)
            rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []
            self.assertFalse([r for r in rows if "read-only" in r["argv"] and "-C" not in r["argv"]], "上限に達したのに判定 CLI を呼んだ")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
