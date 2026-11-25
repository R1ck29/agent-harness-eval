"""PR #7 のレビュー指摘（判定モデルの実 ID・系統の推定・判定待ちの合否・--no-llm の確定・価格表の前置き付き ID・予算超過の費用）。"""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness_eval.adapters import ClaudeAdapter, CodexAdapter  # noqa: E402
from harness_eval.costs import resolve  # noqa: E402
from harness_eval.judging import infer_family  # noqa: E402
from harness_eval.metrics import summarize  # noqa: E402
from harness_eval.record import TrialRecord  # noqa: E402
from harness_eval.runner import RunOptions, Runner  # noqa: E402
from harness_eval.spec import load_profile, load_profiles, load_tasks  # noqa: E402


def rec(**kw) -> TrialRecord:
    base = dict(run_id="r", task_id="t", profile_id="p", agent="claude_code", trial=0, cache_key="k",
                task_hash="th", profile_hash="ph", cli_version="v")
    base.update(kw)
    return TrialRecord(**base)


class JudgeModelTests(unittest.TestCase):
    def test_claude_judge_model_picks_the_main_model_not_the_first_key(self):
        """modelUsage に副次呼び出しの haiku が先に並んでも、出力トークンの最も多いモデルを判定モデルとする。"""
        usage = {"claude-haiku-5-5-20261001": {"inputTokens": 900, "outputTokens": 20, "costUSD": 0.001},
                 "claude-sonnet-5-5-20261101": {"inputTokens": 3000, "outputTokens": 400, "costUSD": 0.02}}
        out = json.dumps({"type": "result", "subtype": "success", "result": "PASS", "modelUsage": usage})
        self.assertEqual(ClaudeAdapter.judge_model(out), "claude-sonnet-5-5-20261101")
        # 出力トークンがなければ costUSD で決める
        usage2 = {"claude-haiku-5-5": {"costUSD": 0.001}, "claude-opus-5-5": {"costUSD": 0.05}}
        self.assertEqual(ClaudeAdapter.judge_model(json.dumps({"type": "result", "modelUsage": usage2})), "claude-opus-5-5")
        # 値が dict でない古い形でも、キーがあれば拾う
        self.assertEqual(ClaudeAdapter.judge_model(json.dumps({"type": "result", "modelUsage": {"claude-sonnet-5-5": {}}})),
                         "claude-sonnet-5-5")

    def test_codex_judge_model_reads_top_level_and_payload(self):
        lines = [json.dumps({"type": "thread.started"}),
                 json.dumps({"type": "event", "payload": {"model": "gpt-6.2-sol"}}),
                 json.dumps({"type": "event", "payload": "not-a-dict"})]
        self.assertEqual(CodexAdapter.judge_model("\n".join(lines)), "gpt-6.2-sol")
        self.assertEqual(CodexAdapter.judge_model(json.dumps({"type": "x", "model": "gpt-6.2-astra", "payload": {"model": "other"}})),
                         "gpt-6.2-astra")  # トップレベルの model を優先
        self.assertIsNone(CodexAdapter.judge_model(json.dumps({"type": "turn.completed"})))


class InferFamilyTests(unittest.TestCase):
    def test_tags_and_provider_prefixes(self):
        for m in ("openrouter/openai/gpt-5:free", "openai/gpt-6.2-sol", "gpt-6.2-sol", "o3-mini", "azure/o4-mini:2026"):
            self.assertEqual(infer_family(m), "codex", m)
        for m in ("anthropic/claude-sonnet-5-5", "claude-opus-5-5", "sonnet", "bedrock/claude-sonnet-5-5-v1:0",
                  "openrouter/anthropic/claude-opus-5-5:free"):
            self.assertEqual(infer_family(m), "claude_code", m)


class PendingPassedTests(unittest.TestCase):
    def test_summarize_tolerates_pending_trials(self):
        """判定中に中断した trials.jsonl は passed が None のまま残る。report で合計しても落ちない（未確定は合格に数えない）。"""
        rs = [rec(trial=0, passed=True, score=1.0), rec(trial=1, passed=None, score=None)]
        row = summarize(rs, 2)["tasks"]["p"]["t"]
        self.assertEqual((row["passed"], row["valid"]), (1, 2))


class NoLlmFinalizeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_no_llm_finalizes_cached_pending_trials(self):
        """--no-llm で、キャッシュの試行に判定待ちが残っていれば not_run として合否を確定し、進行行を出す。"""
        tasks = load_tasks([str(ROOT / "examples" / "tasks" / "csv-summary-insight.yaml")])
        profs = load_profiles([str(ROOT / "examples" / "profiles" / "claude-baseline.yaml")])
        logs: list[str] = []
        r = Runner(tasks, profs, RunOptions(llm=False, log=logs.append), self.tmp / "out")
        self.assertIsNone(r.judge)

        def pending(trial: int, det: list[dict]) -> TrialRecord:
            x = rec(trial=trial, passed=None, grader_results=det + [
                {"type": "llm", "status": "pending", "harness_only": False, "weight": 1.0, "category": "quality"}])
            x.judge_inputs = [{"kind": "rubric", "grader": len(det), "criteria": "c", "focus": "final", "content": "x"}]
            return x

        ok = pending(0, [{"type": "regex", "status": "pass", "harness_only": False, "weight": 1.0, "category": "format"}])
        llm_only = pending(1, [])
        r._judge_phase([ok, llm_only])

        self.assertEqual((ok.grader_results[1]["status"], ok.grader_results[1]["reason"]), ("not_run", "--no-llm"))
        self.assertIs(ok.passed, True)
        self.assertFalse(llm_only.valid)  # 採点できるグレーダーがない
        self.assertEqual((llm_only.error or {}).get("type"), "not_graded")
        self.assertIs(llm_only.passed, False)
        judge_lines = [line for line in logs if line.startswith("[judge ")]
        self.assertEqual(len(judge_lines), 2, logs)
        self.assertIn("PASS", judge_lines[0])
        self.assertIn("SKIP", judge_lines[1])
        self.assertTrue(all("judge=$0.0000" in line for line in judge_lines), judge_lines)


class PriceAndBudgetTests(unittest.TestCase):
    def test_resolve_provider_prefixed_ids(self):
        self.assertEqual(resolve("bedrock/claude-sonnet-5-5-20261101-v1:0")[0], "claude-sonnet-5-5")
        self.assertEqual(resolve("us.anthropic.claude-opus-5-5-20261101-v1:0")[0], "claude-opus-5-5")
        self.assertEqual(resolve("openai/gpt-6.2-sol")[0], "gpt-6.2-sol")
        self.assertEqual(resolve("openrouter/openai/gpt-6.2-sol:free", "codex")[0], "gpt-6.2-sol")
        self.assertEqual(resolve(" OpenAI/GPT-6.2-Astra ")[0], "gpt-6.2-astra")
        self.assertEqual(resolve("openai/gpt-6.2-sol", "claude_code"), (None, None))  # 系統が違えば当てない
        self.assertEqual(resolve("bedrock/claude-sonnet-9-9-v1:0"), (None, None))  # 知らないモデルを推測しない

    def test_budget_exceeded_without_task_cap_is_unknown(self):
        """予算超過で費用が 0 か欠落、トークン換算もできず、タスクに max_budget_usd もない → 0 ではなく不明（None）。"""
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        (tmp / "p.yaml").write_text("id: p\nagent: claude_code\nmodel: sonnet\n", encoding="utf-8")
        prof = load_profile(tmp / "p.yaml")
        line = json.dumps({"type": "result", "subtype": "error_max_budget_usd", "is_error": False, "num_turns": 1, "total_cost_usd": 0})
        p = ClaudeAdapter(executable=["x"]).parse([line])
        r = rec(profile_id="p", error={"type": p.error_type, "message": p.error_message}, tokens=p.tokens,
                cost_usd=p.cost_usd, cost_source=p.cost_source)
        self.assertEqual(Runner._settle_cost(r, prof, max_budget_usd=None), (None, None))
        self.assertEqual(Runner._settle_cost(r, prof, max_budget_usd=0), (None, None))


if __name__ == "__main__":
    unittest.main()
