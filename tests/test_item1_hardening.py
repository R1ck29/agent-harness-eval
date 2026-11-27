"""Item 1 gate hardening: killpg EPERM, clarification done-check, stderr harness_error, #9 regression, error-rate guard."""
from __future__ import annotations

import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from harness_eval import isolation
from harness_eval.adapters import classify_stderr
from harness_eval.cheatproof import leak_check
from harness_eval.graders import _split_sentences, is_clarification_question
from harness_eval.record import EXCLUDED_ERRORS, TrialRecord
from harness_eval.soundness import INVALIDATING, Rules, verdict


def _rec(task: str, profile: str, passed: bool, trial: int) -> TrialRecord:
    return TrialRecord(run_id="r", task_id=task, profile_id=profile, agent="claude_code", trial=trial, cache_key="k",
                       task_hash="h", profile_hash="p", cli_version=None, passed=passed, cmp_passed=passed, valid=True,
                       error=None, grader_results=[{"type": "command", "status": "pass" if passed else "fail",
                                                    "harness_only": False}],
                       meta={"grader_isolation": "bwrap"})


def runs(spec: dict[str, tuple[int, int]], trials: int) -> list[TrialRecord]:
    out: list[TrialRecord] = []
    for t, (b, c) in spec.items():
        out += [_rec(t, "base", i < b, i) for i in range(trials)]
        out += [_rec(t, "cand", i < c, i) for i in range(trials)]
    return out


def summary() -> dict:
    return {"meta": {"tasks": {}, "options": {"llm": False, "judge_agent": "claude_code", "judge_model": "haiku"}}}


class FakeProc:
    pid = 4242
    returncode = None

    def __init__(self, running: bool):
        self.running = running
        self.kill = mock.Mock()

    def poll(self):
        return None if self.running else 0

    def wait(self, timeout=None):
        return 0


class KillTreePermissionError(unittest.TestCase):
    def _run(self, running: bool) -> FakeProc:
        p = FakeProc(running)
        # Force the non-waitid path (macOS-like): poll() is not None -> killpg SIGKILL -> EPERM.
        with mock.patch.object(isolation, "_HAS_WAITID", False), mock.patch.object(isolation.os, "name", "posix"), \
                mock.patch.object(isolation.signal, "SIGKILL", 9, create=True), \
                mock.patch.object(isolation.signal, "SIGTERM", 15, create=True), \
                mock.patch.object(isolation.os, "killpg", create=True, side_effect=PermissionError(1, "EPERM")):
            isolation._kill_tree(p)  # must not raise
        return p

    def test_eperm_with_live_process_falls_back_to_kill(self):
        p = self._run(running=True)
        p.kill.assert_called_once_with()

    def test_eperm_with_dead_process_does_not_kill(self):
        p = self._run(running=False)
        p.kill.assert_not_called()

    def test_eperm_on_waitid_path_falls_back_to_kill(self):
        p = FakeProc(True)
        with mock.patch.object(isolation, "_HAS_WAITID", True), mock.patch.object(isolation.os, "name", "posix"), \
                mock.patch.object(isolation, "_exited_unreaped", return_value=False), \
                mock.patch.object(isolation.signal, "SIGKILL", 9, create=True), \
                mock.patch.object(isolation.signal, "SIGTERM", 15, create=True), \
                mock.patch.object(isolation.os, "killpg", create=True, side_effect=PermissionError(1, "EPERM")):
            isolation._kill_tree(p)
        p.kill.assert_called_once_with()

    def test_process_lookup_error_is_still_swallowed(self):
        p = FakeProc(True)
        with mock.patch.object(isolation, "_HAS_WAITID", False), mock.patch.object(isolation.os, "name", "posix"), \
                mock.patch.object(isolation.signal, "SIGKILL", 9, create=True), \
                mock.patch.object(isolation.signal, "SIGTERM", 15, create=True), \
                mock.patch.object(isolation.os, "killpg", create=True, side_effect=ProcessLookupError()):
            isolation._kill_tree(p)
        p.kill.assert_not_called()


class ClarificationDoneCheck(unittest.TestCase):
    """Sentence-level done / clarify / neither classification (design-pr12-sentence.md, section 4)."""

    PREP = ("確認が完了しました", "読み込みが完了しました", "現状をまとめました")
    CASES: list[tuple[str, bool, str]] = [
        # T1-T3: preparatory "done" followed by a real question (#8 case 1), both 。 and ？ endings
        *[(f"{p}。対象期間はいつですか{end}", True, f"T{i + 1}") for i, p in enumerate(PREP) for end in ("。", "？")],
        # T4-T5: issue #8 cases 3 and 4 (locked trade-off)
        ("詳細な内訳を提示しましょうか。", True, "T4"),
        ("詳細な内訳を提示しましょうか！", True, "T4b"),
        ("詳細な内訳を提示しましょうか!", True, "T4c"),
        ("内訳も出しましょうか。", True, "T5"),
        ("元データはどの期間で集計しましたか？", True, "T6"),
        ("集計は完了しましたか？", True, "T7"),
        ("出力しましたか？", True, "T8"),
        ("保存済みですか？", True, "T9"),
        ("保存済みでしょうか？", True, "T10"),
        ("保存済みですか。", True, "T11"),
        ("作業を終えましたか？", True, "T12"),
        ("作業が終わりましたか？", True, "T13"),
        ("作業が完了しましたか？", True, "T14"),
        ("作業が完了していますか？", True, "T15"),
        ("保存済みのファイルはどれですか？", True, "T16"),
        ("保存済みファイルを使ってよいですか？", True, "T17"),
        ("確認が完了しました。対象期間はいつですか。", True, "T18"),
        ("元データは集計済みですか（はい／いいえ）？", True, "T19"),
        ("元データは集計済みですか(はい／いいえ)？", True, "T20"),
        ("対象は処理済みデータですか？", True, "T21"),
        ("集計済みかどうか教えていただけますか？", True, "T22"),
        ("変換済みレポートを使ってよいですか？", True, "T23"),
        ("データが集計済みでしたら、そのファイルを使ってよいですか？", True, "T24"),
        ("元データはどの期間で集計しました？", True, "T25"),
        # F1-F2: question first, completion later (#8 case 5)
        ("対象期間はいつですか？ 集計を実行しました。", False, "F1"),
        ("どちらが良いですか。とりあえずファイルを作成しました。", False, "F2"),
        ("確認が完了しました。", False, "F3"),
        ("データの期間を集計しました。対象期間はいつですか。", False, "F4"),
        ("参考: https://example.com/search?q=1 を確認しました。", False, "F5"),
        # F6: issue #8 case 2 (locked trade-off)
        ("以下の条件をご確認ください。\n- 期間\n- 対象", False, "F6"),
        ("集計しました", False, "F7"),
        ("保存済みです。対象期間はいつですか。", False, "F8"),
        ("保存済み。対象期間はいつですか。", False, "F9"),
        ("作業を終えました。", False, "F10"),
        ("作業が完了しました。期間はいつですか？", False, "F11"),
        ("作業が完了しています。", False, "F12"),
        ("集計しましたから、期間はいつですか。", False, "F13"),
        ("集計しましたが、期間はいつですか。", False, "F14"),
        ("期間はいつですか？ 集計しました。", False, "F15"),
        ("作業完了です。次はどの期間を集計しますか。", False, "F16"),
        ("作業を終えた。次はどの期間を集計しますか。", False, "F17"),
        ("作業終わった。次はどの期間を集計しますか。", False, "F18"),
        ("作業が完了した。次はどの期間を集計しますか。", False, "F19"),
        ("作業が完了したので、次の対象期間を教えてください。", False, "F20"),
        ("3月分は集計済みで、結果は添付のとおりです。次はどの期間を集計しますか。", False, "F21"),
    ]

    def test_regression_table(self):
        for text, expected, case_id in self.CASES:
            with self.subTest(case=case_id, text=text):
                self.assertIs(is_clarification_question(text), expected)

    def test_split_keeps_terminators_and_brackets(self):
        cases = [
            ("元データは集計済みですか（はい／いいえ）？", ["元データは集計済みですか（はい／いいえ）？"]),
            ("「対象期間はいつですか？」 集計を実行しました。", ["「対象期間はいつですか？」", "集計を実行しました。"]),
            ("抽出の前に確認させてください。「先月」は 2026年3月のことでしょうか？ 先月分を使います",
             ["抽出の前に確認させてください。", "「先月」は 2026年3月のことでしょうか？", "先月分を使います"]),
            ("集計しました。？", ["集計しました。", "？"]),
            ("作業が完了しました！？", ["作業が完了しました！？"]),
            ("確認が完了しました。\n？", ["確認が完了しました。", "？"]),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(_split_sentences(text), expected)

    def test_split_is_linear_on_repeated_bracket_groups(self):
        def best(groups: int) -> float:
            text = "(a)" * groups
            times = []
            for _ in range(3):
                t0 = time.perf_counter()
                _split_sentences(text)
                times.append(time.perf_counter() - t0)
            return min(times)

        small, large = best(2000), best(8000)  # 6k vs 24k characters
        self.assertLess(large, 1.0)
        self.assertLess(large, 12 * small + 0.05)

    def test_split_newline_resets_brackets_and_ascii_period_is_not_a_boundary(self):
        self.assertEqual(_split_sentences("（閉じ忘れ\n1) sales.csv を v0.10.3 で読む。\r\n2) 次"),
                         ["（閉じ忘れ", "1) sales.csv を v0.10.3 で読む。", "2) 次"])
        self.assertEqual(_split_sentences(" \n "), [])


class PathBinaryRegression(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)
        self.trial = self.tmp / "trial"
        self.trial.mkdir()

    def _tool(self, d: Path) -> Path:
        d.mkdir(parents=True, exist_ok=True)
        t = d / "mytool"
        t.write_text("#!/bin/sh\n", encoding="utf-8")
        t.chmod(t.stat().st_mode | stat.S_IEXEC)
        return t

    def _check(self, cmd: str, protected, path_env: str) -> dict:
        return leak_check([{"name": "Bash", "input": {"command": cmd}}], "", self.trial,
                          protected=protected, path_env=path_env)

    def test_binary_directly_in_protected_out_root_on_path_is_invalid(self):
        out = self.tmp / "out"
        tool = self._tool(out)
        # A protected root on PATH does not exempt what sits directly inside it (could be answers).
        self.assertEqual(self._check(f"ls {tool}", [out], str(out))["status"], "INVALID")

    def test_nested_path_tool_is_only_a_warning(self):
        home = self.tmp / "user"
        tool = self._tool(home / ".local" / "bin")
        r = self._check(f"ls {tool}", [home], str(tool.parent))
        self.assertEqual(r["status"], "clean")
        self.assertTrue(any(w["kind"] == "path_binary" for w in r["warnings"]))

    def test_dotdot_escape_past_path_tool_stays_invalid(self):
        home = self.tmp / "user"
        tool = self._tool(home / ".local" / "bin")
        (home / "secret.txt").write_text("s", encoding="utf-8")
        for cmd in (f"cat {tool}/../../../secret.txt", f"cat '{tool}'/../../../secret.txt"):
            with self.subTest(cmd=cmd.replace(str(self.tmp), "T")):
                self.assertEqual(self._check(cmd, [home], str(tool.parent))["status"], "INVALID")

    def test_symlink_to_secret_stays_invalid(self):
        home = self.tmp / "user"
        tool = self._tool(home / ".local" / "bin")
        secret = home / "secret.txt"
        secret.write_text("s", encoding="utf-8")
        (tool.parent / "lnk").symlink_to(secret)
        self.assertEqual(self._check(f"cat {secret}", [home], str(tool.parent))["status"], "INVALID")


class ClassifyStderr(unittest.TestCase):
    def test_bwrap_failure_is_harness_error(self):
        self.assertEqual(classify_stderr("bwrap: Can't create file at /x: Permission denied\n"), "harness_error")
        self.assertEqual(classify_stderr("bwrap: No permissions to create new namespace"), "harness_error")

    def test_disk_full_is_harness_error(self):
        self.assertEqual(classify_stderr("OSError: [Errno 28] No space left on device"), "harness_error")

    def test_normal_agent_traceback_is_not_harness_error(self):
        tb = "Traceback (most recent call last):\n  File \"a.py\", line 1\nValueError: boom\n"
        self.assertIsNone(classify_stderr(tb))

    def test_existing_classes_unchanged(self):
        self.assertEqual(classify_stderr("Error: 429 Too Many Requests"), "rate_limit")
        self.assertEqual(classify_stderr("API Error: overloaded"), "overloaded")
        self.assertEqual(classify_stderr("not logged in"), "auth")
        self.assertEqual(classify_stderr("You hit your usage limit"), "usage_limit")

    def test_harness_error_is_excluded_and_invalidating(self):
        self.assertIn("harness_error", EXCLUDED_ERRORS)
        self.assertIn("harness_error", INVALIDATING)


class ClassifyStderrEnvPrecedence(unittest.TestCase):
    def test_environment_diagnostics_beat_generic_auth_and_rate_limit_words(self):
        self.assertEqual(classify_stderr("OSError: [Errno 28] No space left on device: credentials.json"),
                         "harness_error")
        self.assertEqual(classify_stderr("bwrap: Can't bind /work/429: No such file or directory"),
                         "harness_error")


class ErrorRateGuard(unittest.TestCase):
    def _recs(self, n_bad: int):
        recs = runs({f"t{i}": (2, 4) for i in range(5)}, 4)  # 40 records
        bad = 0
        for r in recs:
            if bad < n_bad and r.profile_id == "cand":
                r.valid, r.passed, r.cmp_passed = False, False, None
                r.error = {"type": "harness_error", "message": "bwrap: failed"}
                bad += 1
        return recs

    def test_over_20_percent_harness_errors_is_inconclusive_and_not_adoptable(self):
        v = verdict(self._recs(9), summary(), Rules(baseline="base", candidate="cand", resamples=2000))  # 9/40 = 22.5%
        self.assertEqual(v["conclusion"], "INCONCLUSIVE")
        self.assertFalse(v["adoptable"])
        self.assertEqual(v["errors"]["by_type"].get("harness_error"), 9)

    def test_no_harness_errors_is_not_blocked_by_the_guard(self):
        v = verdict(self._recs(0), summary(), Rules(baseline="base", candidate="cand", resamples=2000))
        self.assertEqual(v["errors"]["invalid"], 0)
        self.assertNotEqual(v["conclusion"], "INCONCLUSIVE", v["reasons"])


if __name__ == "__main__":
    unittest.main()
