"""PR #5 の独立レビュー（Codex）の指摘に対するテスト。指摘番号は XREVIEW-AHE5-astra.md のもの。"""
import json
import os
import shlex
import shutil
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness_eval import isolation  # noqa: E402
from harness_eval.isolation import build_env, bwrap_problem, make_trial_paths  # noqa: E402
from harness_eval.spec import load_profile  # noqa: E402
import tests.test_cheatproof as tc  # noqa: E402
from tests.test_cheatproof import write  # noqa: E402

BWRAP = sys.platform.startswith("linux") and bwrap_problem() is None


class GraderIsolationRecords(unittest.TestCase):
    def test_decisive_needs_isolation_for_executed_command_grades(self):
        from tests.test_soundness import rules, runs, summary
        from harness_eval.soundness import verdict

        recs = runs({f"t{i}": (2, 4) for i in range(8)}, 4)
        rec = recs[0]
        for status, mode, expected in [
            ("pass", None, "INCONCLUSIVE"),
            ("fail", "unknown", "INCONCLUSIVE"),
            ("pass", "none", "INCONCLUSIVE"),
            ("pass", "bwrap", "IMPROVED"),
            ("not_run", None, "IMPROVED"),
            (None, None, "IMPROVED"),
        ]:
            with self.subTest(status=status, mode=mode):
                rec.grader_results = [{"type": "command", "status": status}] if status else [{"type": "regex", "status": "pass"}]
                rec.meta = {} if mode is None else {"grader_isolation": mode}
                self.assertEqual(verdict(recs, summary(), rules())["conclusion"], expected)

    def test_commands_each_get_fresh_home_and_temp(self):
        from harness_eval.graders import JudgeConfig, grade_all
        from harness_eval.spec import Grader
        from tests.test_soundness import rec

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            workdir, scratch = root / "workdir", root / "grade_tmp"
            workdir.mkdir()
            scratch.mkdir()
            trial = rec("task", "candidate", True)
            homes = []

            def run(cmd, **kwargs):
                env = kwargs["env"]
                home = Path(env["HOME"])
                tmp = Path(env["TMPDIR"])
                self.assertTrue(home.is_dir())
                self.assertTrue(tmp.is_dir())
                self.assertFalse((home / "settings").exists())
                self.assertFalse((tmp / "cache").exists())
                (home / "settings").touch()
                (tmp / "cache").touch()
                homes.append(home)
                return isolation.subprocess.CompletedProcess(cmd, 0, "", "")

            graders = [Grader(type="command", params={"run": "true"}) for _ in range(2)]
            for mode in ("bwrap", "none"):
                with self.subTest(mode=mode), mock.patch("harness_eval.graders.grader_isolation", return_value=mode), \
                        mock.patch("harness_eval.graders.subprocess.run", side_effect=run):
                    homes.clear()
                    grade_all(graders, trial, workdir, JudgeConfig(enabled=False), tmp_dir=scratch)
                    self.assertEqual([g["status"] for g in trial.grader_results], ["pass", "pass"], trial.grader_results)
                    self.assertEqual(len(set(homes)), 2)
                    self.assertEqual(list(scratch.iterdir()), [])

    @unittest.skipIf(os.name == "nt", "シンボリックリンクの作成に権限が要る")
    def test_workdir_paths_do_not_add_host_bind_mounts(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d).resolve()
            workdir, scratch = root / "workdir", root / "grade_tmp"
            (workdir / "subdir").mkdir(parents=True)
            scratch.mkdir()
            private = write(root / "private" / "sentinel.txt", "fixture only")
            trusted = write(root / "trusted.txt", "task material")
            (workdir / "candidate").symlink_to(private)
            cmd = "cat " + shlex.quote(str(workdir / "candidate"))
            args = isolation.grader_sandbox_cmd(cmd, workdir, scratch, [str(workdir / "subdir" / ".." / "candidate"), str(trusted)])
            self.assertNotIn(str(private), args)
            self.assertIn(str(trusted), args)
            for candidate, target in [
                ("/" + str(workdir / "candidate"), private),
                (str(workdir / "subdir" / ".." / ".." / "workdir" / ".." / "trusted.txt"), trusted),
                (str(root / "private" / ".." / "workdir" / ".." / "trusted.txt"), trusted),
            ]:
                with self.subTest(candidate=candidate):
                    args = isolation.grader_sandbox_cmd("cat " + shlex.quote(candidate), workdir, scratch, [candidate])
                    self.assertNotIn(str(target), args)


class AwsFiles(unittest.TestCase):
    """#4: AWS の認証ファイルは、プロファイルが Bedrock（または AWS のファイル）を明示したときだけ渡す。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="r5-aws-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "realhome"
        write(self.home / ".aws" / "credentials", "[default]\n")
        write(self.home / ".aws" / "config", "[default]\n")

    def env_for(self, yaml_text, agent="claude_code", parent_env=None):
        prof = load_profile(write(self.tmp / "p.yaml", f"id: p\nagent: {agent}\n{yaml_text}"))
        tp = make_trial_paths(self.tmp / "trial", ".claude" if agent == "claude_code" else ".codex")
        with mock.patch.object(Path, "home", return_value=self.home), \
                mock.patch.dict(os.environ, {"PATH": "/usr/bin", **(parent_env or {})}, clear=True):
            return build_env(prof, tp, "CLAUDE_CONFIG_DIR" if agent == "claude_code" else "CODEX_HOME")

    def test_undeclared_profiles_get_no_aws_files(self):
        for agent, extra in (("codex", ""), ("claude_code", ""), ("claude_code", "env: {ANTHROPIC_MODEL: x}\n")):
            env = self.env_for(extra, agent)
            self.assertNotIn("AWS_SHARED_CREDENTIALS_FILE", env, (agent, extra))
            self.assertNotIn("AWS_CONFIG_FILE", env, (agent, extra))

    def test_bedrock_profile_gets_them(self):
        env = self.env_for("env: {CLAUDE_CODE_USE_BEDROCK: '1'}\n")
        self.assertEqual(env["AWS_SHARED_CREDENTIALS_FILE"], str(self.home / ".aws" / "credentials"))
        self.assertEqual(env["AWS_CONFIG_FILE"], str(self.home / ".aws" / "config"))

    def test_env_unset_is_not_resurrected(self):
        env = self.env_for("env: {CLAUDE_CODE_USE_BEDROCK: '1'}\nenv_unset: [AWS_CONFIG_FILE]\n")
        self.assertIn("AWS_SHARED_CREDENTIALS_FILE", env)
        self.assertNotIn("AWS_CONFIG_FILE", env)

    def test_codex_never_gets_them_even_with_bedrock_env(self):
        env = self.env_for("env: {CLAUDE_CODE_USE_BEDROCK: '1'}\n", agent="codex")
        self.assertNotIn("AWS_SHARED_CREDENTIALS_FILE", env)

    def test_unset_bedrock_flag_does_not_supply_aws_files(self):
        env = self.env_for("inherit_env: [CLAUDE_CODE_USE_BEDROCK]\nenv_unset: [CLAUDE_CODE_USE_BEDROCK]\n",
                           parent_env={"CLAUDE_CODE_USE_BEDROCK": "1"})
        self.assertNotIn("CLAUDE_CODE_USE_BEDROCK", env)
        self.assertNotIn("AWS_SHARED_CREDENTIALS_FILE", env)
        self.assertNotIn("AWS_CONFIG_FILE", env)


@unittest.skipUnless(BWRAP, "bwrap が使えない")
class SolverWriteScope(unittest.TestCase):
    """#2: solver が書けるのは作業フォルダ・一時ホーム・TMPDIR だけ。ログ・採点記録のある試行ルートは見えない・書けない。"""

    def test_solver_cannot_touch_trial_root(self):
        t = tc.CheatingAgents("test_peeking_agent_is_invalid_and_never_counts")
        t.setUp()
        self.addCleanup(t.tearDown)
        _, _, _, recs = t.run_eval("peek", t.profile("attacker", "ATTACK_ROOT", sandbox="bwrap"))
        rec = recs[0]
        trial = Path(rec["trial_dir"])
        a = json.loads((trial / "workdir" / "attack.json").read_text(encoding="utf-8"))
        # 見えない・書けない（隔離の中の見かけ上の書き込みがホストに届かないこと）
        self.assertNotIn("tampered", (trial / "stdout.jsonl").read_text(encoding="utf-8"))
        self.assertNotIn("tampered", (trial / "stderr.txt").read_text(encoding="utf-8"))
        self.assertFalse((trial / "evil.txt").exists())
        self.assertNotIn("stdout.jsonl", a["listing"] or [])  # 試行ルートの本物の内容（ログ）は隔離の中に見えない
        self.assertEqual(a["listing"], ["home", "workdir"])  # 見えるのは書ける 2 つ（マウント点）だけ。ログ・採点記録は見えない


class HostWritesDoNotFollowLinks(unittest.TestCase):
    """#2: ホストが書く先がリンクでも、その先は壊さない。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="r5-link-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.victim = write(self.tmp / "victim.txt", "precious")

    @unittest.skipIf(os.name == "nt", "シンボリックリンクの作成に権限が要る")
    def test_run_process_refuses_symlinked_log(self):
        out = self.tmp / "stdout.jsonl"
        out.symlink_to(self.victim)
        err = self.tmp / "stderr.txt"
        res = isolation.run_process([sys.executable, "-c", "print('x')"], self.tmp, dict(os.environ), None, out, err, 20)
        self.assertEqual(self.victim.read_text(encoding="utf-8"), "precious")
        self.assertIsNotNone(res.launch_error)

    @unittest.skipIf(os.name == "nt", "POSIX のパイプ記述子を確認する")
    def test_solver_output_is_piped_and_saved_on_timeout(self):
        out, err = self.tmp / "stdout.jsonl", self.tmp / "stderr.txt"
        probe = (
            "import os, stat, sys, time\n"
            "for fd in (1, 2):\n"
            "    print(stat.S_ISFIFO(os.fstat(fd).st_mode), file=sys.stderr if fd == 2 else sys.stdout, flush=True)\n"
            "time.sleep(60)\n"
        )
        res = isolation.run_process([sys.executable, "-c", probe], self.tmp, dict(os.environ), None, out, err, 1)
        self.assertTrue(res.timed_out)
        self.assertEqual(out.read_text(encoding="utf-8"), "True\n")
        self.assertEqual(err.read_text(encoding="utf-8"), "True\n")

    def test_large_solver_output_is_saved_while_running(self) -> None:
        out, err = self.tmp / "stdout.jsonl", self.tmp / "stderr.txt"
        size = 8 * 1024 * 1024
        probe = (
            "import pathlib, sys, time\n"
            "out, err, size = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]), int(sys.argv[3])\n"
            "for _ in range(size // 65536):\n"
            "    sys.stdout.buffer.write(b'o' * 65536)\n"
            "    sys.stderr.buffer.write(b'e' * 65536)\n"
            "sys.stdout.flush(); sys.stderr.flush()\n"
            "while min(out.stat().st_size, err.stat().st_size) < size:\n"
            "    time.sleep(0.01)\n"
            "print('usage record')\n"
        )
        res = isolation.run_process([sys.executable, "-c", probe, str(out), str(err), str(size)], self.tmp,
                                    dict(os.environ), None, out, err, 5)
        self.assertFalse(res.timed_out)
        self.assertEqual(res.returncode, 0)
        self.assertEqual(out.read_bytes(), b"o" * size + b"usage record\n")
        self.assertEqual(err.read_bytes(), b"e" * size)

    def test_timeout_preserves_usage_when_child_does_not_read_stdin(self) -> None:
        out, err = self.tmp / "stdout.jsonl", self.tmp / "stderr.txt"
        probe = "import time; print('usage record', flush=True); time.sleep(60)"
        res = isolation.run_process([sys.executable, "-c", probe], self.tmp, dict(os.environ), "x" * (1024 * 1024),
                                    out, err, 1)
        self.assertTrue(res.timed_out)
        self.assertEqual(out.read_bytes(), b"usage record\n")

    @unittest.skipIf(os.name == "nt", "シンボリックリンクの作成に権限が要る")
    def test_snapshot_skips_special_files_and_links(self):
        wd = self.tmp / "wd"
        write(wd / "a.txt", "a")
        (wd / "link").symlink_to(self.victim)
        os.mkfifo(wd / "pipe")
        snap = isolation.snapshot(wd)  # FIFO を読みに行って固まらない
        self.assertIn("a.txt", snap)
        self.assertNotIn("pipe", snap)
        self.assertIn("link", snap)  # リンクは中身ではなく「リンクであること・向き先」を記録する
        self.assertNotEqual(snap["link"], isolation.snapshot(self.victim.parent)["victim.txt"])

    @unittest.skipIf(os.name == "nt", "シンボリックリンクの作成に権限が要る")
    def test_host_places_files_without_following_planted_links(self):
        wd = self.tmp / "wd"
        wd.mkdir()
        (wd / "retry.json").symlink_to(self.victim)
        (wd / "grade_tmp").symlink_to(self.tmp)
        isolation.replace_text(wd / "retry.json", "{}")
        self.assertEqual(self.victim.read_text(encoding="utf-8"), "precious")  # リンクの先は書かない
        self.assertFalse((wd / "retry.json").is_symlink())
        d = isolation.fresh_dir(wd / "grade_tmp")
        self.assertFalse(d.is_symlink())
        self.assertTrue(self.tmp.exists())  # リンクの先のフォルダを消さない
        self.assertEqual(list(d.iterdir()), [])


@unittest.skipUnless(BWRAP, "bwrap が使えない")
class GraderIsolation(unittest.TestCase):
    """#1: command グレーダー（候補の実装を import / 実行しうる）は、本物のホーム・親の環境変数・他のパス・ネットワークから隔離する。"""

    def setUp(self):
        self.t = tc.CheatingAgents("test_peeking_agent_is_invalid_and_never_counts")
        self.t.setUp()
        self.addCleanup(self.t.tearDown)
        self.secret = write(Path(tempfile.mkdtemp(prefix="r5-secret-")) / "auth.json", "TOP-SECRET")
        self.addCleanup(shutil.rmtree, self.secret.parent, True)
        self.victim = write(self.secret.parent / "victim.txt", "precious")
        write(self.t.ev / "tasks" / "impl.yaml", "id: impl\ntargets: [claude_code]\nprompt: write impl\nfixture: ../fixtures/peek\n"
              "graders: [{type: command, run: '{python} impl.py'}]\n")

    def run_probe(self, env_extra=None):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        self.addCleanup(srv.close)
        env = {"AHE_TEST_SECRET_ENV": "sk-live-xyz", "AHE_TEST_SECRET_FILE": str(self.secret), "AHE_TEST_VICTIM": str(self.victim),
               "AHE_TEST_PORT": str(srv.getsockname()[1]), **(env_extra or {})}
        with mock.patch.dict(os.environ, env):
            _, _, _, recs = self.t.run_eval("impl", self.t.profile("implementer", "IMPL_PROBE", sandbox="bwrap"))
        rec = recs[0]
        return rec, json.loads(rec["grader_results"][0]["reason"].split(" ", 1)[1])

    def test_grader_runs_candidate_code_in_a_box(self):
        rec, seen = self.run_probe()
        self.assertEqual(rec["meta"]["grader_isolation"], "bwrap")
        self.assertIsNone(seen["env_secret"])  # 親の環境変数は許可リストだけ
        self.assertEqual(seen["env_keys"], [])
        self.assertFalse(seen["secret_file_visible"])  # 実のホーム・他のパスは見えない
        self.assertNotEqual(seen["net"], "connected")  # ネットワークなし
        self.assertEqual(self.victim.read_text(encoding="utf-8"), "precious")
        self.assertNotEqual(seen["workdir"], "writable")  # 作業フォルダは読み取り専用（採点が他の採点・判定の入力を書き換えない）
        self.assertTrue(seen["home"].startswith("ahe-grade-"))  # HOME はコマンドごとの一時フォルダ

    @unittest.skipIf(os.name == "nt", "シンボリックリンクの作成に権限が要る")
    def test_symlinked_python_prefix_is_bound_by_real_path_and_linked(self):
        """CI の uv は Python を別名のリンク（cpython-3.10-… → cpython-3.10.22-…）で参照する。リンク名で bind せず、実体を bind してリンクを張る。"""
        tmp = Path(tempfile.mkdtemp(prefix="r5-py-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        real = tmp / "uv" / "cpython-3.10.22"
        write(real / "bin" / "python3", "")
        link = tmp / "uv" / "cpython-3.10"
        link.symlink_to(real)
        (tmp / "wd").mkdir()
        (tmp / "g").mkdir()
        from harness_eval.isolation import grader_sandbox_cmd
        with mock.patch.object(sys, "base_prefix", str(link)), mock.patch.object(sys, "prefix", str(link)), \
                mock.patch.object(sys, "executable", str(link / "bin" / "python3")):
            args = grader_sandbox_cmd("true", tmp / "wd", tmp / "g")
        triples = [tuple(args[i:i + 3]) for i in range(len(args) - 2)]
        self.assertNotIn(("--ro-bind", str(link), str(link)), triples)  # リンク名では bind しない（bwrap が衝突する）
        self.assertIn(("--ro-bind", str(real.resolve()), str(real.resolve())), triples)
        self.assertIn(("--symlink", str(real.resolve()), str(link)), triples)

    def test_box_has_no_controlling_terminal(self):
        """採点・solver の隔離の中の候補のコードは、制御端末（/dev/tty）に TIOCSTI で文字を打ち込んで、操作者のシェルへ抜けられない。"""
        import pty
        probe = ("import fcntl, os, termios\n"
                 "try:\n    fd = os.open('/dev/tty', os.O_RDWR); fcntl.ioctl(fd, termios.TIOCSTI, b'X'); print('INJECTED')\n"
                 "except OSError as e:\n    print('blocked')\n")
        tmp = Path(tempfile.mkdtemp(prefix="r5-tty-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        (tmp / "wd").mkdir()
        (tmp / "g").mkdir()
        write(tmp / "probe.py", probe)
        child = ("import subprocess, sys\nfrom pathlib import Path\nfrom harness_eval.isolation import grader_sandbox_cmd\n"
                 f"tmp = Path({str(tmp)!r})\n"
                 f"c = grader_sandbox_cmd({sys.executable!r} + ' ' + str(tmp / 'probe.py'), tmp / 'wd', tmp / 'g')\n"
                 "r = subprocess.run(c, capture_output=True, text=True, cwd=tmp / 'wd')\n"
                 "print('RESULT', r.stdout.strip(), r.stderr.strip()[:200])\n")
        pid, fd = pty.fork()  # 制御端末を持つ子（ハーネスが端末から起動された状態）
        if pid == 0:
            os.chdir(ROOT)
            os.execv(sys.executable, [sys.executable, "-c", child])
        out = b""
        while True:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            out += chunk
        os.waitpid(pid, 0)
        text = out.decode("utf-8", "replace")
        self.assertIn("RESULT blocked", text)
        self.assertNotIn("INJECTED", text)
        from harness_eval.isolation import TrialPaths, sandbox_cmd
        from harness_eval.spec import load_profile
        prof = load_profile(write(tmp / "p.yaml", "id: p\nagent: codex\n"))
        tp = TrialPaths(tmp, tmp / "h", tmp / "h" / ".codex", tmp / "wd", tmp / "o", tmp / "e")
        self.assertIn("--new-session", sandbox_cmd([sys.executable, "-c", "pass"], tp, {"PATH": "/usr/bin"}, prof))

    def test_unavailable_box_is_recorded_and_blocks_a_decisive_verdict(self):
        """bwrap がない OS では「隔離なし」を記録する。decisive の verdict は結論を出さない。"""
        with mock.patch("harness_eval.graders.grader_isolation", return_value="none"):
            rec, seen = self.run_probe()
        self.assertEqual(rec["meta"]["grader_isolation"], "none")
        self.assertIsNone(seen["env_secret"])  # 隔離できなくても環境変数の許可リストは効く
        from harness_eval.soundness import Rules, _integrity
        inc: list = []
        chk = _integrity([_rec_of(rec)], "implementer", inc, Rules())
        self.assertEqual(chk["status"], "fail")
        self.assertTrue(any("採点" in m for m in inc), inc)
        inc2: list = []
        chk2 = _integrity([_rec_of(rec)], "implementer", inc2, Rules(required_tier="dev"))
        self.assertEqual((chk2["status"], inc2), ("warn", []))


class NegateAcrossFamilies(unittest.TestCase):
    """#10: 否定型の評価観点は、系統ごとの確定した多数決に適用してから合意を求める。UNKNOWN は票にしない。"""

    def test_negate_applies_per_family_then_combines(self):
        from harness_eval.judging import combine_rubric
        self.assertEqual(combine_rubric({"codex": "fail", "claude_code": "unknown"}, negate=True), "judge_disagreement")
        self.assertEqual(combine_rubric({"codex": "unknown", "claude_code": "unknown"}, negate=True), "unknown")
        self.assertEqual(combine_rubric({"codex": "fail", "claude_code": "fail"}, negate=True), "pass")
        self.assertEqual(combine_rubric({"codex": "pass", "claude_code": "fail"}, negate=True), "judge_disagreement")
        self.assertEqual(combine_rubric({"codex": "pass", "claude_code": "pass"}, negate=True), "fail")
        self.assertEqual(combine_rubric({"codex": "fail"}, negate=True), "pass")  # 1 系統（dev）
        self.assertEqual(combine_rubric({"codex": "pass", "claude_code": "unknown"}, negate=False), "judge_disagreement")


class JudgeIdentity(unittest.TestCase):
    """#11: 判定役の実行ファイル・版・実効モデル・経路が変われば、判定キャッシュのキーが変わる。"""

    def test_key_changes_with_identity(self):
        from harness_eval.judging import judge_cache_key, judge_identity
        a = judge_identity(["/usr/bin/python3", __file__], "codex 1.0", "gpt-x", "")
        b = judge_identity(["/usr/bin/python3", __file__], "codex 1.1", "gpt-x", "")
        c = judge_identity(["/usr/bin/python3", __file__], "codex 1.0", None, "")
        d = judge_identity(["/usr/bin/python3", __file__], "codex 1.0", "gpt-x", "OPENAI_BASE_URL#abc12345")
        e = judge_identity(["/usr/bin/python3", str(ROOT / "tests" / "fake_env.py")], "codex 1.0", "gpt-x", "")
        self.assertEqual(len({a, b, c, d, e}), 5)
        self.assertEqual(a, judge_identity(["/usr/bin/python3", __file__], "codex 1.0", "gpt-x", ""))
        keys = {judge_cache_key("p", "codex", "gpt-x", "r", "dev", 0, i) for i in (a, b, c, d, e)}
        self.assertEqual(len(keys), 5)

    @unittest.skipIf(os.name == "nt", "POSIX の実行ファイル検索を検証する")
    def test_path_executable_content_changes_even_with_same_size_and_mtime(self):
        from harness_eval.judging import judge_identity

        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"PATH": d}):
            exe = write(Path(d) / "fake-judge", "#!/bin/sh\necho pass\n")
            exe.chmod(0o755)
            before = judge_identity(["fake-judge"], "v1", None)
            self.assertEqual(before, judge_identity([str(exe)], "v1", None))
            st = exe.stat()
            write(exe, "#!/bin/sh\necho fail\n")
            os.utime(exe, ns=(st.st_atime_ns, st.st_mtime_ns))
            self.assertNotEqual(before, judge_identity(["fake-judge"], "v1", None))

    @unittest.skipIf(os.name == "nt", "POSIX の実行ファイル検索を検証する")
    def test_profile_path_placeholder_locates_the_judge_for_cache_identity(self):
        from harness_eval.judge_phase import JudgePhase
        from harness_eval.spec import Profile

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            exe = write(root / "bin" / "fake-judge", "#!/bin/sh\necho pass\n")
            exe.chmod(0o755)
            phase = JudgePhase()
            phase.judge_profiles = {"codex": Profile("j", "codex", root / "judge.yaml", env={"PATH": "{profile_dir}/bin"})}
            phase.judge_versions = {}
            phase._judge_exe = lambda _: [exe.name]
            phase._identities = {}
            before = phase._identity("codex", None)
            st = exe.stat()
            write(exe, "#!/bin/sh\necho fail\n")
            os.utime(exe, ns=(st.st_atime_ns, st.st_mtime_ns))
            phase._identities.clear()
            self.assertNotEqual(before, phase._identity("codex", None))

    def test_swapping_the_judge_cli_does_not_reuse_votes(self):
        from tests.test_judge_pipeline import CLAUDE_J, CODEX_J, JudgePipeline
        t = JudgePipeline("test_dev_default_cross_family_single_mid_vote")
        t.setUp()
        self.addCleanup(t.tearDown)
        code, out, s, _ = t.go(*CLAUDE_J, *CODEX_J)
        self.assertEqual(code, 0, out)
        self.assertEqual(s["usage"]["judge"]["cache_hits"], 0)
        code, out, s, _ = t.go(*CLAUDE_J, *CODEX_J)  # 同じ判定役 → キャッシュを使う
        self.assertGreater(s["usage"]["judge"]["cache_hits"], 0)
        other = t.tmp / "other_codex.py"  # 別の実行ファイル（中身は同じでも別物）
        shutil.copy(FAKE_DIR / "fake_codex.py", other)
        code, out, s, _ = t.go(*CLAUDE_J, "--judge-codex-executable", sys.executable, str(other))
        self.assertEqual(code, 0, out)
        self.assertEqual(s["usage"]["judge"]["cache_hits"], 0, "別の判定 CLI の票を再利用した")


FAKE_DIR = ROOT / "tests" / "fake_agents"


class CalibrationIndependence(unittest.TestCase):
    """#12: 校正は盲検化後の入力で重複を除き、合格例・不合格例の両方の実績を要求する。"""

    def test_dedupe_by_blinded_prompt(self):
        from harness_eval.judging import dedupe_calibration
        items = [("r1", {"p": "same", "answer": True}), ("r2", {"p": "same", "answer": True}), ("r3", {"p": "other", "answer": False})]
        got = dedupe_calibration(items, lambda x: x[1]["p"], lambda x: x[1]["answer"])
        self.assertEqual([x[0] for x in got], ["r1", "r3"])

    def test_verdict_needs_both_classes(self):
        from harness_eval.soundness import Rules, _judge_checks
        j = {"tier": "decisive", "llm": True, "models": {"codex": "gpt-6.2-astra", "claude_code": "opus"}, "blind": True, "votes": 3,
             "plan": {"claude_code": {"families": ["codex", "claude_code"]}}}
        rules = Rules(baseline="b", candidate="c")
        for cal, ok in (({"items": 6, "agree": 6, "rate": 1.0, "pass_items": 6, "fail_items": 0}, False),
                        ({"items": 6, "agree": 6, "rate": 1.0, "pass_items": 0, "fail_items": 6}, False),
                        ({"items": 6, "agree": 6, "rate": 1.0}, False),  # 内訳が記録されていない
                        ({"items": 6, "agree": 6, "rate": 1.0, "pass_items": 3, "fail_items": 3, "agree_pass": 3, "agree_fail": 3}, True),
                        ({"items": 6, "agree": 3, "rate": 0.5, "pass_items": 3, "fail_items": 3, "agree_pass": 3, "agree_fail": 0}, False)):
            fail, _ = _judge_checks({**j, "calibration": cal}, {"claude_code"}, 4, 0, rules, [])
            self.assertEqual(not fail, ok, (cal, fail))

    def test_conflicting_blinded_answers_invalidate_calibration_and_verdict(self):
        from types import SimpleNamespace
        from harness_eval.judge_phase import JudgePhase
        from harness_eval.soundness import verdict
        from tests.test_soundness import STRONG_JUDGE, rules, runs, summary

        recs = runs({f"t{i}": (2, 4) for i in range(8)}, 4, llm="pass")
        phase = JudgePhase()
        phase.judge = SimpleNamespace(calibration_items=10, seed=0)
        phase._blind_terms = lambda: (["solver-a", "solver-b"], [])
        phase.judge_one = mock.Mock(side_effect=lambda _, it: "pass" if it["answer"] else "fail")

        def item(content, answer):
            return {"kind": "calibration", "criteria": "Accuracy", "focus": "final", "content": content, "answer": answer}

        # 十分な一致例があっても、盲検化後に同一になる入力の正解が矛盾していたら校正できない。
        recs[0].judge_inputs = [item("solver-a sample", True), item("solver-b sample", False)]
        recs[0].judge_inputs += [item(f"distinct {i}", i % 2 == 0) for i in range(10)]
        phase._calibrate(recs)

        self.assertTrue(phase.calibration.get("conflicting_answers"))
        phase.judge_one.assert_not_called()
        v = verdict(recs, summary(judge={**STRONG_JUDGE, "calibration": phase.calibration}, llm=True), rules())
        self.assertEqual(v["conclusion"], "INCONCLUSIVE")
        self.assertTrue(any("矛盾" in reason for reason in v["reasons"]), v["reasons"])


def _rec_of(d):
    from harness_eval.record import TrialRecord
    return TrialRecord.from_dict(d) if hasattr(TrialRecord, "from_dict") else TrialRecord(**{k: v for k, v in d.items() if k in TrialRecord.__dataclass_fields__})


if __name__ == "__main__":
    unittest.main()
