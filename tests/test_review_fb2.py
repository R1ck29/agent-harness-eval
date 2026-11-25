"""v0.10.2: harvest / 採点 / PATH 警告 / preflight / report_only などの内部実行フィードバック。"""
from __future__ import annotations

import stat
import tempfile
import unittest
from pathlib import Path

from harness_eval.cheatproof import host_path_tools, leak_check
from harness_eval.isolation import grader_isolation
from harness_eval.graders import grade_all, is_clarification_question, JudgeConfig, tool_input_haystack
from harness_eval.harvest import (CONVERT_PROMPT, Prompt, check_written, refers_to_earlier_context,
                                  select, selftest, write_draft)
from harness_eval.record import TrialRecord
from harness_eval.runner import CACHE_SCHEMA
from harness_eval.spec import SpecError, lint, load_profile, load_task, parse_grader


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def rec(**kw) -> TrialRecord:
    base = dict(run_id="t", task_id="t", profile_id="p", agent="claude_code", trial=0,
                cache_key="", task_hash="", profile_hash="", cli_version=None)
    base.update(kw)
    return TrialRecord(**base)


class SchemaAndPromptTests(unittest.TestCase):
    def test_cache_schema_is_11(self):
        self.assertEqual(CACHE_SCHEMA, "11")

    def test_convert_prompt_says_trial_record_is_a_path(self):
        self.assertIn("JSON ファイルへのパス", CONVERT_PROMPT)
        self.assertIn('json.load(open(os.environ["TRIAL_RECORD"]))', CONVERT_PROMPT)
        self.assertIn("final_message", CONVERT_PROMPT)
        self.assertNotIn("TRIAL_RECORD の JSON の final_message", CONVERT_PROMPT)

    def test_convert_prompt_asks_for_alternative_outputs(self):
        self.assertIn("reference_outputs_alt", CONVERT_PROMPT)


class ClarificationUrlTests(unittest.TestCase):
    def test_japanese_questions_without_question_mark(self):
        self.assertTrue(is_clarification_question("どのファイルのことですか。"))
        self.assertTrue(is_clarification_question("教えてください。"))

    def test_question_mark_inside_url_is_not_enough(self):
        self.assertFalse(is_clarification_question("手順は https://docs.example.com/guide?id=1 にあります。"))
        self.assertFalse(is_clarification_question("https://example.com/search?q=foo を参照しました。"))

    def test_question_after_url_still_counts(self):
        self.assertTrue(is_clarification_question("https://docs.example.com/guide?id=1 でよいですか。"))
        self.assertTrue(is_clarification_question("この URL でよいですか？ https://example.com/x?q=1"))
        self.assertTrue(is_clarification_question("Do you mean https://example.com/a?"))
        self.assertTrue(is_clarification_question("対象は https://example.com/a？"))
        self.assertTrue(is_clarification_question("URLはhttps://example.com/aで合ってる？"))


class ToolUsedInputMatchTests(unittest.TestCase):
    def test_bash_command_string_supports_multiline_anchors(self):
        from harness_eval.spec import Grader
        g = Grader(type="tool_used", params={"tool": "Bash", "input_match": r"^ls\s+-l", "min": 1})
        r = rec(tool_calls=[{"name": "Bash", "input": {"command": "ls -l /tmp"}}])
        grade_all([g], r, Path("."), JudgeConfig(enabled=False))
        self.assertEqual(r.grader_results[0]["status"], "pass")
        r2 = rec(tool_calls=[{"name": "Bash", "input": {"command": "echo hi\nls -l /tmp"}}])
        grade_all([g], r2, Path("."), JudgeConfig(enabled=False))
        self.assertEqual(r2.grader_results[0]["status"], "pass")  # MULTILINE: ^ が 2 行目に効く
        r3 = rec(tool_calls=[{"name": "Bash", "input": {"command": "echo ls -l"}}])
        grade_all([g], r3, Path("."), JudgeConfig(enabled=False))
        self.assertEqual(r3.grader_results[0]["status"], "fail")

    def test_json_form_still_matches_non_command_fields(self):
        from harness_eval.spec import Grader
        g = Grader(type="tool_used", params={"tool": "Write", "input_match": r'"path": "/tmp/out.txt"', "min": 1})
        r = rec(tool_calls=[{"name": "Write", "input": {"path": "/tmp/out.txt", "content": "x"}}])
        grade_all([g], r, Path("."), JudgeConfig(enabled=False))
        self.assertEqual(r.grader_results[0]["status"], "pass")

    def test_haystack_puts_command_first(self):
        h = tool_input_haystack("Bash", {"command": "ls -l", "timeout": 30})
        self.assertTrue(h.startswith("ls -l"))
        self.assertIn("timeout", h)  # JSON フォールバックもある
        # description の行頭が ^pytest に誤一致しない
        h2 = tool_input_haystack("Bash", {"command": "ls -la", "description": "pytest check"})
        self.assertFalse(__import__("re").search(r"^pytest\b", h2, __import__("re").MULTILINE))


class ReportOnlyTests(unittest.TestCase):
    def test_weight_zero_and_report_only_parse(self):
        g0 = parse_grader(Path("t.yaml"), 0, {"type": "regex", "pattern": "x", "weight": 0})
        self.assertEqual(g0.weight, 0)
        self.assertFalse(g0.report_only)
        g1 = parse_grader(Path("t.yaml"), 0, {"type": "regex", "pattern": "x", "report_only": True})
        self.assertEqual(g1.weight, 0)
        self.assertTrue(g1.report_only)
        with self.assertRaisesRegex(SpecError, "0 以上"):
            parse_grader(Path("t.yaml"), 0, {"type": "regex", "pattern": "x", "weight": -1})

    def test_report_only_fail_does_not_block_score_or_code_ok(self):
        from harness_eval.spec import Grader
        blocking = Grader(type="regex", params={"pattern": "ok"}, weight=1)
        extra = Grader(type="regex", params={"pattern": "must-see"}, weight=0, report_only=True)
        r = rec(final_message="ok")
        grade_all([blocking, extra], r, Path("."), JudgeConfig(enabled=False))
        self.assertEqual(r.score, 1.0)
        self.assertEqual(r.grader_results[1]["status"], "fail")
        self.assertTrue(r.grader_results[1]["report_only"])
        # weight 0 だけでも同じ
        zero = Grader(type="regex", params={"pattern": "must-see"}, weight=0)
        r2 = rec(final_message="ok")
        grade_all([blocking, zero], r2, Path("."), JudgeConfig(enabled=False))
        self.assertEqual(r2.score, 1.0)
        self.assertEqual(r2.grader_results[1]["status"], "fail")
        # 止める判定が残らなければ score は None（誤って 1.0 にしない）
        r3 = rec(final_message="ok")
        grade_all([zero], r3, Path("."), JudgeConfig(enabled=False))
        self.assertIsNone(r3.score)


class CoverageAndPreflightSpecTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)

    def test_coverage_warning_when_component_has_no_task(self):
        t = write(self.tmp / "t.yaml",
                  "id: a\nprompt: x\ntargets: [claude_code]\n"
                  "graders: [{type: regex, pattern: x}, {type: tool_used, tool: Bash, max: 0}]\n"
                  "coverage: [hooks]\n")
        p = write(self.tmp / "p.yaml", "id: a\nagent: claude_code\ncomponents: [hooks, skills]\n")
        w = "\n".join(lint([load_task(t)], [load_profile(p)]))
        self.assertIn("skills", w)
        self.assertNotIn("'hooks'", w)

    def test_preflight_parses_strings_and_argv(self):
        t = write(self.tmp / "t.yaml",
                  "id: a\nprompt: x\ngraders: [{type: regex, pattern: x}]\n"
                  "preflight: ['true', [python3, -c, 'print(1)']]\n")
        task = load_task(t)
        self.assertEqual(task.preflight[0], ["true"])
        self.assertEqual(task.preflight[1][0], "python3")
        t2 = write(self.tmp / "t2.yaml",
                   "id: b\nprompt: x\ngraders: [{type: regex, pattern: x}]\n"
                   "preflight: [soffice, --version]\n")
        self.assertEqual(load_task(t2).preflight, [["soffice", "--version"]])
        from harness_eval.spec import _preflight
        with self.assertRaisesRegex(SpecError, "引用符"):
            _preflight(self.tmp / "t3.yaml", ['echo "unclosed'])

    def test_preflight_and_coverage_excluded_from_hash(self):
        a = write(self.tmp / "a.yaml", "id: a\nprompt: x\ngraders: [{type: regex, pattern: x}]\n")
        b = write(self.tmp / "b.yaml",
                  "id: a\nprompt: x\ngraders: [{type: regex, pattern: x}]\n"
                  "coverage: [hooks]\npreflight: ['true']\n")
        self.assertEqual(load_task(a).content_hash(), load_task(b).content_hash())


class HarvestSelectTests(unittest.TestCase):
    def test_per_session_and_skip_context_first(self):
        s1 = [
            Prompt("さっきの続きで集計してください。もっと詳しく", "claude_code", "s1", 100),
            Prompt("3月の店舗別売上を CSV から集計してください", "claude_code", "s1", 200),
            Prompt("結果を箇条書きにもしてください", "claude_code", "s1", 300),
        ]
        s2 = [Prompt("SQL が遅い理由を調べて改善案を出してください", "codex", "s2", 150)]
        prompts = s1 + s2
        # 既定: 各セッションの最初だけ
        picked = select(prompts, 10)
        self.assertEqual({p.session: p.text[:6] for p in picked},
                         {"s1": "さっきの続き", "s2": "SQL が遅"})
        # 文脈参照の先頭を飛ばす
        skipped = select(prompts, 10, skip_context_first=True)
        self.assertTrue(any(p.text.startswith("3月の店舗") for p in skipped))
        self.assertFalse(any(p.text.startswith("さっき") for p in skipped))
        # 1 セッションから 2 件（文脈先頭は残る）
        two = select(prompts, 10, per_session=2)
        self.assertEqual(sum(1 for p in two if p.session == "s1"), 2)
        # 同一文面の重複は 1 件にまとめる（history と transcript の二重収集）
        dup = [
            Prompt("3月の店舗別売上を CSV から集計してください", "claude_code", "s9", 1),
            Prompt("3月の店舗別売上を CSV から集計してください", "claude_code", "s9", 2),
            Prompt("その結果を箇条書きにもしてください。詳細も残す", "claude_code", "s9", 3),
        ]
        got = select(dup, 10, per_session=2)
        self.assertEqual([p.text[:4] for p in sorted(got, key=lambda x: x.ts)], ["3月の店", "その結果"])

    def test_refers_to_earlier_context(self):
        self.assertTrue(refers_to_earlier_context("さっきのファイルを直してください"))
        self.assertTrue(refers_to_earlier_context("continue with the previous file please"))
        self.assertTrue(refers_to_earlier_context("じゃあ集計してください。もっと詳しくお願いします"))
        self.assertTrue(refers_to_earlier_context("それでは売上を出してください"))
        self.assertFalse(refers_to_earlier_context("3月の店舗別売上を CSV から集計してください"))

    def test_check_written_runs_selftest_and_keeps_alt(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, tmp, True)
        origin = Prompt("x", "claude_code", "s", 0)
        check = ("import sys\nfrom pathlib import Path\n"
                 "p = Path('total.txt')\n"
                 "sys.exit(0 if p.is_file() and p.read_text().strip() in {'4600', '4,600'} else 1)\n")
        draft = {"id": "h-alt", "prompt": "orders.csv の合計を total.txt に",
                 "files": [{"path": "orders.csv", "content": "a\n1"}],
                 "reference_outputs": [{"path": "total.txt", "content": "4600"}],
                 "reference_outputs_alt": [{"path": "total.txt", "content": "4,600"}],
                 "check_script": check, "final_answer_example": "4600", "graders": []}
        path = write_draft(draft, tmp, origin)
        self.assertEqual(check_written(path), "")
        self.assertTrue((tmp / "references-alt" / "h-alt" / "total.txt").is_file())
        self.assertEqual(selftest(load_task(path)), "")
        # 別解が間違っていれば落とす（隔離できない環境では command を走らせない）
        bad = {**draft, "id": "h-alt-bad", "reference_outputs_alt": [{"path": "total.txt", "content": "0"}]}
        bad_path = write_draft(bad, tmp, origin)
        if grader_isolation() == "bwrap":
            self.assertIn("別解で採点が通らない", check_written(bad_path))
        else:
            self.assertEqual(check_written(bad_path), "")
            self.assertIn("別解で採点が通らない", selftest(load_task(bad_path)))
        empty_ref = {**draft, "id": "h-empty-ref", "reference_outputs": [], "reference_outputs_alt": [],
                     "final_answer_example": ""}
        self.assertIn("正解例がない", check_written(write_draft(empty_ref, tmp, origin)))


class PathBinaryLeakTests(unittest.TestCase):
    def test_path_binary_is_warning_not_leak(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, tmp, True)
        bindir = tmp / "user" / ".local" / "bin"
        bindir.mkdir(parents=True)
        tool = bindir / "mytool"
        tool.write_text("#!/bin/sh\n", encoding="utf-8")
        tool.chmod(tool.stat().st_mode | stat.S_IEXEC)
        secret = tmp / "user" / "secret.txt"
        secret.write_text("no", encoding="utf-8")
        trial = tmp / "trial"
        trial.mkdir()
        path_env = str(bindir)
        r = leak_check([{"name": "Bash", "input": {"command": f"ls {tool}"}}], "", trial,
                       protected=[tmp / "user"], path_env=path_env)
        self.assertEqual(r["status"], "clean")
        self.assertTrue(any(w["kind"] == "path_binary" for w in r["warnings"]))
        # PATH ディレクトリそのものの列挙も警告
        r2 = leak_check([{"name": "Bash", "input": {"command": f"ls {bindir}"}}], "", trial,
                        protected=[tmp / "user"], path_env=path_env)
        self.assertEqual(r2["status"], "clean")
        # PATH にないホーム配下はこれまでどおり leak
        r3 = leak_check([{"name": "Bash", "input": {"command": f"cat {secret}"}}], "", trial,
                        protected=[tmp / "user"], path_env=path_env)
        self.assertEqual(r3["status"], "INVALID")
        self.assertTrue(any(h["kind"] in ("protected_path", "outside_path") for h in r3["hits"]))
        # PATH 上のリンクが秘密を指していても、リンク先そのものは leak のまま
        secret_link = bindir / "link-to-secret"
        secret_link.symlink_to(secret)
        r4 = leak_check([{"name": "Bash", "input": {"command": f"cat {secret}"}}], "", trial,
                        protected=[tmp / "user"], path_env=path_env)
        self.assertEqual(r4["status"], "INVALID")
        r5 = leak_check([{"name": "Bash", "input": {"command": f"ls {secret_link}"}}], "", trial,
                        protected=[tmp / "user"], path_env=path_env)
        self.assertEqual(r5["status"], "clean")  # PATH ディレクトリ直下の名前そのものは警告

    def test_host_path_tools_skips_shallow_roots(self):
        bins, dirs = host_path_tools("/home:/usr/bin")
        self.assertFalse(any(p.rstrip("/") == "/home" or p == "/home" for p in dirs))

    def test_path_tool_does_not_mask_other_protected_paths(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, tmp, True)
        bindir = tmp / "user" / ".local" / "bin"
        bindir.mkdir(parents=True)
        tool = bindir / "mytool"
        tool.write_text("#!/bin/sh\n", encoding="utf-8")
        tool.chmod(tool.stat().st_mode | stat.S_IEXEC)
        other = tmp / "eval-out" / "ans.txt"
        other.parent.mkdir(parents=True)
        other.write_text("secret", encoding="utf-8")
        trial = tmp / "trial"
        trial.mkdir()
        r = leak_check([{"name": "Bash", "input": {"command": f"ls {tool} && cat {other}"}}], "", trial,
                       protected=[tmp / "user", tmp / "eval-out"], path_env=str(bindir))
        self.assertEqual(r["status"], "INVALID")

    def test_home_on_path_does_not_allow_dotfiles(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, tmp, True)
        home = tmp / "user"
        home.mkdir()
        netrc = home / ".netrc"
        netrc.write_text("machine x", encoding="utf-8")
        netrc.chmod(netrc.stat().st_mode | stat.S_IEXEC)
        trial = tmp / "trial"
        trial.mkdir()
        r = leak_check([{"name": "Bash", "input": {"command": f"cat {netrc}"}}], "", trial,
                       protected=[home], path_env=str(home))
        self.assertEqual(r["status"], "INVALID")
        bins, dirs = host_path_tools(".")
        self.assertEqual(dirs, set())


class PreflightRunnerTests(unittest.TestCase):
    def test_declared_preflight_runs_and_fails(self):
        from harness_eval.runner import Runner
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, tmp, True)
        tpath = write(tmp / "tasks" / "a.yaml",
                      "id: a\nprompt: x\ngraders: [{type: regex, pattern: x}]\n"
                      "preflight: [['false']]\n")
        ppath = write(tmp / "profiles" / "p.yaml", "id: p\nagent: claude_code\nsandbox: off\n")
        task, profile = load_task(tpath), load_profile(ppath)
        class Dummy:
            pairs = [(task, profile)]
            def _run_declared_preflight(self):
                return Runner._run_declared_preflight(self)
        with self.assertRaisesRegex(SpecError, "事前確認に失敗"):
            Dummy()._run_declared_preflight()

    def test_declared_preflight_success(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, tmp, True)
        tpath = write(tmp / "tasks" / "a.yaml",
                      "id: a\nprompt: x\ngraders: [{type: regex, pattern: x}]\n"
                      "preflight: [['true']]\n")
        ppath = write(tmp / "profiles" / "p.yaml", "id: p\nagent: claude_code\nsandbox: off\n")
        task, profile = load_task(tpath), load_profile(ppath)
        from harness_eval.runner import Runner
        class Dummy:
            pairs = [(task, profile)]
            def _run_declared_preflight(self):
                return Runner._run_declared_preflight(self)
        Dummy()._run_declared_preflight()  # 例外にならない


class CeilingMessageTests(unittest.TestCase):
    def test_ceiling_message_says_what_to_do(self):
        # 既存の test_soundness.test_ceiling と同じ形 + 次の一手
        from tests.test_soundness import rules, runs, summary, verdict
        recs = runs({"a": (2, 2), "b": (2, 2), "c": (2, 0), "d": (2, 2), "e": (2, 2)}, 2)
        v = verdict(recs, summary(), rules())
        msg = v["reasons"][0]
        self.assertIn("天井", msg)
        self.assertIn("採点を厳しく", msg)
        self.assertIn("天井を上げて", msg)


if __name__ == "__main__":
    unittest.main()
