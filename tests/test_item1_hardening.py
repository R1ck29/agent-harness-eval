"""Item 1 gate hardening: killpg EPERM, clarification done-check, stderr harness_error, #9 regression, error-rate guard."""
from __future__ import annotations

import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness_eval import isolation
from harness_eval.adapters import classify_stderr
from harness_eval.cheatproof import leak_check
from harness_eval.graders import is_clarification_question
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
                mock.patch.object(isolation.os, "killpg", side_effect=PermissionError(1, "EPERM")):
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
                mock.patch.object(isolation.os, "killpg", side_effect=PermissionError(1, "EPERM")):
            isolation._kill_tree(p)
        p.kill.assert_called_once_with()

    def test_process_lookup_error_is_still_swallowed(self):
        p = FakeProc(True)
        with mock.patch.object(isolation, "_HAS_WAITID", False), mock.patch.object(isolation.os, "name", "posix"), \
                mock.patch.object(isolation.os, "killpg", side_effect=ProcessLookupError()):
            isolation._kill_tree(p)
        p.kill.assert_not_called()


class ClarificationDoneCheck(unittest.TestCase):
    def test_question_then_later_completion_is_not_clarification(self):
        self.assertFalse(is_clarification_question("対象期間はいつですか？ 集計を実行しました。"))
        self.assertFalse(is_clarification_question("どちらが良いですか。とりあえずファイルを作成しました。"))

    def test_prep_done_then_question_is_clarification(self):
        for prep in ("確認が完了しました", "読み込みが完了しました", "現状をまとめました"):
            with self.subTest(prep=prep):
                self.assertTrue(is_clarification_question(f"{prep}。対象期間はいつですか。"))
                self.assertTrue(is_clarification_question(f"{prep}。対象期間はいつですか？"))

    def test_prep_done_alone_is_not_clarification(self):
        self.assertFalse(is_clarification_question("確認が完了しました。"))

    def test_bare_aggregation_done_stays_not_clarification(self):
        self.assertFalse(is_clarification_question("データの期間を集計しました。対象期間はいつですか。"))

    def test_url_query_question_mark_is_still_ignored(self):
        self.assertFalse(is_clarification_question("参考: https://example.com/search?q=1 を確認しました。"))

    # Boundary tests: lock the current (known trade-off) behaviour of issue #8 cases 2-4.
    def test_case2_please_confirm_conditions_list_is_not_clarification(self):
        self.assertFalse(is_clarification_question("以下の条件をご確認ください。\n- 期間\n- 対象"))

    def test_case3_followup_offer_without_completion_verb_still_passes(self):
        self.assertTrue(is_clarification_question("詳細な内訳を提示しましょうか。"))

    def test_case4_breakdown_offer_still_passes(self):
        self.assertTrue(is_clarification_question("内訳も出しましょうか。"))


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
