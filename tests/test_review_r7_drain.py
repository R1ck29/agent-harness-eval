"""PR #5 r7 #7: ログが上限を超えたあとの使用量の取りこぼし（費用上限が効かなくなる）。超過のあるなしで、費用・トークンの計上が一致する。"""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness_eval import isolation  # noqa: E402
from harness_eval.adapters import ClaudeAdapter, CodexAdapter  # noqa: E402
from harness_eval.isolation import run_process  # noqa: E402
from harness_eval.runner import _solver_accounting_lines  # noqa: E402

RESULT = {"type": "result", "subtype": "success", "is_error": False, "duration_ms": 10, "num_turns": 3, "result": "answer",
          "total_cost_usd": 1.25, "terminal_reason": "completed",
          "usage": {"input_tokens": 10, "output_tokens": 20, "cache_read_input_tokens": 5, "cache_creation_input_tokens": 7},
          "modelUsage": {"m": {"inputTokens": 10, "costUSD": 1.25}}}
TURN = {"type": "turn.completed", "usage": {"input_tokens": 1000, "cached_input_tokens": 100, "output_tokens": 30, "reasoning_output_tokens": 4}}
# ツール出力などに入る、本物でない「result」の文字列（行頭の type は別・入れ子の中）
DECOY = json.dumps({"type": "user", "tool_use_result": {"type": "result", "total_cost_usd": 99}, "pad": "x" * 8000})


class DrainUsageTests(unittest.TestCase):
    def run_script(self, limit: int, body: str, timeout_s: int = 60):
        tmp = Path(tempfile.mkdtemp(prefix="r7-drain-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        out = tmp / "stdout.jsonl"
        script = tmp / "noisy.py"  # 長い行を含むので、-c（引数の長さに上限がある）ではなくファイルで渡す
        script.write_text(body, encoding="utf-8")
        res = run_process([sys.executable, str(script)], tmp, {"PATH": ""}, None, out, tmp / "stderr.txt", timeout_s, log_limit=limit)
        return res, out

    def accounting(self, adapter, out):
        r = adapter.parse(list(_solver_accounting_lines(out)), usage_only=True)
        return r.cost_usd, r.tokens, r.cost_source, r.error_type, r.num_turns

    def assertSameAsUncut(self, adapter, body: str, limit: int = 256 * 1024):
        res_full, out_full = self.run_script(64 * 1024 * 1024, body)
        res_cut, out_cut = self.run_script(limit, body)
        self.assertFalse(res_full.log_truncated)
        self.assertTrue(res_cut.log_truncated)
        full = self.accounting(adapter, out_full)
        self.assertIsNotNone(full[1] and full[1].get("output"), full)  # 比べる基準が空でない
        self.assertEqual(self.accounting(adapter, out_cut), full)
        return out_cut

    def test_lookalike_lines_do_not_use_up_the_tail_budget(self):
        """(i) 入れ子に "type": "result" を含む行が 1 MiB 分あっても、本物の result を残す。"""
        body = (f"import sys\nfor _ in range(400): print({'g' * 1000!r})\n"
                f"for _ in range(300): print({DECOY!r})\n"  # 約 2.4 MiB
                f"print({json.dumps(RESULT)!r})\n")
        out = self.assertSameAsUncut(ClaudeAdapter(), body)
        self.assertLess(out.stat().st_size, 1024 * 1024)

    def test_a_large_result_line_is_kept_without_its_body(self):
        """(ii) 64 KiB を超える最終応答つきの result 行も、使用量だけ残す。"""
        big = dict(RESULT, result="A" * 300_000)
        body = f"for _ in range(400): print({'g' * 1000!r})\nprint({json.dumps(big)!r})\n"
        out = self.assertSameAsUncut(ClaudeAdapter(), body)
        self.assertLess(out.stat().st_size, 600 * 1024)  # 本文（300 KB）は残さない

    def test_a_usage_line_across_the_cap_is_not_split(self):
        """(iii) ちょうど上限をまたぐ使用量の行（前半は上限の手前・後半は上限のあと）。"""
        pad = "g" * 40_000
        body = f"print({pad!r})\nprint({json.dumps(RESULT)!r})\nprint({pad!r})\n"
        for limit in (40_001 + 10, 40_001 + 100, 40_001 + 300):
            with self.subTest(limit=limit):
                res_full, out_full = self.run_script(64 * 1024 * 1024, body)
                res, out = self.run_script(limit, body)
                self.assertTrue(res.log_truncated)
                self.assertEqual(self.accounting(ClaudeAdapter(), out), self.accounting(ClaudeAdapter(), out_full))

    def test_codex_turns_are_summed_across_the_cap(self):
        body = ("import sys\nfor _ in range(300): print('g' * 1000)\n"
                f"for _ in range(5): print({json.dumps(TURN)!r})\n"
                f"for _ in range(20): print({DECOY!r})\n"
                f"print({json.dumps(TURN)!r})\n")
        out = self.assertSameAsUncut(CodexAdapter(), body)
        usage = CodexAdapter().parse(list(_solver_accounting_lines(out)), usage_only=True)
        self.assertEqual(usage.tokens["input"], 6000)

    def test_a_turn_line_across_the_cap_is_counted_once(self):
        pad = "g" * 20_000
        body = f"print({pad!r})\nprint({json.dumps(TURN)!r})\nprint({json.dumps(TURN)!r})\n"
        res, out = self.run_script(20_001 + 20, body)
        self.assertEqual(CodexAdapter().parse(list(_solver_accounting_lines(out)), usage_only=True).tokens["input"], 2000)

    def test_a_cap_at_the_very_end_of_a_turn_line_does_not_count_it_twice(self):
        """上限がちょうど使用量の行の末尾（改行の直前）に当たる。ログに残った前半を完全な行として数えず、続きの改行で読み切った行だけを数える。"""
        pad, line = "g" * 5000, json.dumps(TURN)
        same_chunk = f"print({pad!r})\nprint({line!r})\nprint({line!r})\n"
        split_chunks = ("import sys, time\n"  # 行末で別のチャンクにする（ログに完全な行が改行なしで残る）
                        f"sys.stdout.write({pad!r} + '\\n' + {line!r}); sys.stdout.flush(); time.sleep(0.4)\n"
                        f"sys.stdout.write('\\n' + {line!r} + '\\n')\n")
        limit = len(pad) + 1 + len(line)
        for label, body in (("same chunk", same_chunk), ("split chunks", split_chunks)):
            for lim in (limit - 1, limit, limit + 1):
                with self.subTest(label, limit=lim):
                    res, out = self.run_script(lim, body)
                    self.assertTrue(res.log_truncated)
                    self.assertEqual(CodexAdapter().parse(list(_solver_accounting_lines(out)), usage_only=True).tokens["input"], 2000)

    def test_the_latest_result_wins_and_lookalikes_are_not_counted(self):
        first, last = dict(RESULT, total_cost_usd=0.5), dict(RESULT, total_cost_usd=2.5)
        body = (f"for _ in range(300): print({'g' * 1000!r})\nprint({json.dumps(first)!r})\nprint({DECOY!r})\nprint({json.dumps(last)!r})\n"
                f"print({DECOY!r})\n")
        res, out = self.run_script(64 * 1024, body)
        self.assertEqual(self.accounting(ClaudeAdapter(), out)[0], 2.5)

    def test_a_huge_line_is_dropped_but_later_usage_is_kept(self):
        body = ("import sys\nsys.stdout.write('x' * 100000 + '\\n')\n"
                f"print({json.dumps(RESULT)!r})\n")
        with mock.patch.object(isolation, "_USAGE_LINE_MAX", 64 * 1024):
            res, out = self.run_script(1024, body)
        self.assertTrue(res.log_truncated)
        self.assertEqual(self.accounting(ClaudeAdapter(), out)[0], 1.25)

    def test_usage_is_kept_when_a_detached_descendant_holds_the_pipe(self):
        """パイプを握ったまま別セッションに残る子孫がいて、タイムアウトで打ち切られても、集めた使用量はログに残る。"""
        body = ("import subprocess, sys\n"
                "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(9)'], start_new_session=True)\n"
                "for _ in range(300): print('g' * 1000)\n"
                f"print({json.dumps(RESULT)!r}, flush=True)\n")
        res, out = self.run_script(64 * 1024, body, timeout_s=1)
        self.assertTrue(res.timed_out and res.log_truncated)
        self.assertEqual(self.accounting(ClaudeAdapter(), out)[0], 1.25)


if __name__ == "__main__":
    unittest.main()
