"""判定役専用の「認証はその場で使う（複製しない）」経路（credentials: in_place）と、--judge-profile に実行のプロファイルにない YAML を
渡す経路のテスト。偽の CLI と偽の CODEX_HOME だけを使う（本物の ~/.codex・~/.claude には触れない。モデルも呼ばない）。"""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from harness_eval.cli import main  # noqa: E402
from harness_eval.spec import SpecError, load_profile  # noqa: E402
from fake_env import SUFFIX  # noqa: E402
from test_cheatproof import jwt  # noqa: E402

FAKE = ROOT / "tests" / "fake_agents"
PY = sys.executable
SECRET = "SECRET-REFRESH-TOKEN-DO-NOT-PRINT"
JUDGE_FLAGS = ["--ephemeral", "-s", "read-only", "--skip-git-repo-check", "--ignore-user-config", "--ignore-rules", "--disable", "hooks"]


def write(p: Path, text: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def run_cli(*args: str) -> tuple[int, str]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        code = main(list(args))
    return code, buf.getvalue()


def snapshot(d: Path) -> dict[str, bytes]:
    return {str(p.relative_to(d)): p.read_bytes() for p in sorted(d.rglob("*")) if p.is_file()}


class InPlaceSpec(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="he inplace spec "))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def load(self, body: str):
        return load_profile(write(self.tmp / "p.yaml", body))

    def test_in_place_is_read_for_codex(self):
        self.assertEqual(self.load("id: j\nagent: codex\ncredentials: in_place\n").credentials, "in_place")
        self.assertIsNone(self.load("id: j\nagent: codex\n").credentials)

    def test_in_place_rejects_claude_code_copy_and_unknown_values(self):
        for body in ("id: j\nagent: claude_code\ncredentials: in_place\n",  # Claude は CLAUDE_CODE_OAUTH_TOKEN を inherit_env で渡す
                     "id: j\nagent: codex\ncredentials: in_place\ncopy_credentials: true\n",
                     "id: j\nagent: codex\ncredentials: in_place\nisolate_home: false\n",  # 本物の HOME を見せない
                     "id: j\nagent: codex\ncredentials: copy\n"):
            with self.assertRaises(SpecError, msg=body):
                self.load(body)


class InPlaceJudge(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="he inplace "))
        ex = self.tmp / "examples"
        shutil.copytree(ROOT / "examples", ex)
        for f in (ex / "profiles").glob("*.yaml"):
            fake = FAKE / ("fake_claude.py" if "claude" in f.name else "fake_codex.py")
            f.write_text(f.read_text(encoding="utf-8") + f"\nexecutable: [{json.dumps(PY)}, {json.dumps(str(fake))}]\n" + SUFFIX, encoding="utf-8")
        self.ex, self.out, self.cache = ex, self.tmp / "results", self.tmp / "cache"
        # 偽の「本物の」CODEX_HOME（本物の ~/.codex ではない）。利用者の AGENTS.md・config.toml・認証を模した中身
        self.real = self.tmp / "real-codex"
        self.auth(10 * 3600)
        write(self.real / "AGENTS.md", "user rules\n")
        write(self.real / "config.toml", "model = 'x'\n")
        self.argv_log, self.overlap = self.tmp / "argv.jsonl", self.tmp / "overlap"
        self.overlap.mkdir()
        self.env = {"FAKE_ARGV_LOG": str(self.argv_log), "FAKE_STATE_DIR": str(self.tmp), "FAKE_JUDGE_OVERLAP_DIR": str(self.overlap)}
        self.patch = mock.patch.dict(os.environ, {**self.env, "CODEX_HOME": str(self.real), "CLAUDE_CONFIG_DIR": str(self.tmp / "real-claude")})
        self.patch.start()
        self.judge = self.judge_profile()

    def tearDown(self):
        self.patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def auth(self, exp_in_s: float):
        last = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        write(self.real / "auth.json", json.dumps({"tokens": {"access_token": jwt(time.time() + exp_in_s), "refresh_token": SECRET},
                                                   "last_refresh": last}))

    def judge_profile(self, extra: str = "credentials: in_place\n", name: str = "codex-judge-inplace") -> Path:
        # 実行のプロファイルの外（--profiles に渡さない）に置く
        return write(self.tmp / "judge" / f"{name}.yaml", f"id: {name}\nagent: codex\nbilling: subscription\n{extra}"
                     f"executable: [{json.dumps(PY)}, {json.dumps(str(FAKE / 'fake_codex.py'))}]\n" + SUFFIX)

    def go(self, *extra, profiles=("claude-baseline", "claude-ds-lite"), task="csv-summary-insight", trusted=True):
        if trusted and any("in_place" in str(x) or "inplace" in str(x) for x in extra):
            extra = (*extra, "--trust-judge-input")  # in_place は判定入力が信頼できるときだけ（指摘 5）。既定のテストは宣言する
        args = ["run", "--tasks", str(self.ex / "tasks"), "--profiles", *[str(self.ex / "profiles" / f"{n}.yaml") for n in profiles],
                "--task", task, "--out", str(self.out), "--cache-dir", str(self.cache), "--baseline", "claude-baseline", *extra]
        code, out = run_cli(*args)
        runs = sorted(d for d in self.out.iterdir() if d.is_dir()) if self.out.is_dir() else []
        s = json.loads((runs[-1] / "summary.json").read_text(encoding="utf-8")) if runs and (runs[-1] / "summary.json").is_file() else None
        return code, out, s

    def argv_rows(self):
        return [json.loads(line) for line in self.argv_log.read_text(encoding="utf-8").splitlines()] if self.argv_log.exists() else []

    def judge_calls(self):
        return [r for r in self.argv_rows() if "read-only" in r["argv"] and "-C" not in r["argv"]]

    # ---------------------------------------------------------------- その場で使う
    def test_in_place_needs_the_judge_input_to_be_declared_trusted(self):
        """#5: 実認証の CODEX_HOME を使う判定は、候補の出力を渡す限りツール経由の読み取りを CLI 側で止められない。
        判定入力が信頼できると宣言（--trust-judge-input）しない限り、起動前に止める。"""
        code, out, s = self.go("--judge-profile", f"codex={self.judge}", trusted=False)
        self.assertEqual(code, 2, out)
        self.assertIn("--trust-judge-input", out)
        self.assertEqual(self.judge_calls(), [])
        code, out, s = self.go("--judge-profile", f"codex={self.judge}")
        self.assertEqual(code, 0, out)
        self.assertTrue(s["meta"]["options"]["judge_input_trusted"])

    def test_in_place_judge_uses_real_codex_home_without_copying_or_writing(self):
        before = snapshot(self.real)
        code, out, s = self.go("--judge-profile", f"codex={self.judge}")
        self.assertEqual(code, 0, out)
        calls = self.judge_calls()
        self.assertGreaterEqual(len(calls), 2, out)
        for c in calls:
            self.assertEqual(c["cfg"], str(self.real))  # CODEX_HOME だけ本物
            self.assertNotEqual(c["home"], str(Path.home()))  # HOME は一時ホームのまま
            self.assertNotIn(str(self.real), c["home"] or "")
            for f in JUDGE_FLAGS:
                self.assertIn(f, c["argv"])
            self.assertEqual(c["argv"][:2], ["exec", "--json"])
        self.assertEqual(snapshot(self.real), before)  # 複製も書き戻しも、ロックファイルなどの追加もない
        self.assertNotIn(SECRET, out + json.dumps(s))  # 認証の中身はどこにも出ない
        self.assertEqual(s["meta"]["judge"]["plan"]["claude_code"]["families"], ["codex"])

    def test_copy_mode_is_unchanged_and_does_not_use_the_real_dir(self):
        """比較: in_place でないプロファイルの判定は、これまでどおり一時の CODEX_HOME を使う。"""
        plain = self.judge_profile("", "codex-judge-plain")
        code, out, _ = self.go("--judge-profile", f"codex={plain}")
        self.assertEqual(code, 0, out)
        self.assertTrue(self.judge_calls())
        for c in self.judge_calls():
            self.assertNotEqual(c["cfg"], str(self.real))
            self.assertNotIn("--ignore-user-config", c["argv"])

    def test_in_place_judge_calls_never_overlap(self):
        code, out, _ = self.go("--judge-profile", f"codex={self.judge}", "--trials", "4", "--jobs", "4", "--no-cache")
        self.assertEqual(code, 0, out)
        # 解く側も --jobs 4 で同じ argv ログに追記するので、判定の回数は偽 codex が数えたファイルで見る
        self.assertGreaterEqual(len(list(self.overlap.glob("call-*"))), 4, out)
        self.assertFalse((self.overlap / "OVERLAP").exists(), "同じ系統の in_place の判定が同時に走った")

    def test_expired_or_missing_auth_stops_before_any_trial(self):
        for setup in (lambda: self.auth(-60), lambda: self.auth(30 * 60), lambda: (self.real / "auth.json").unlink()):
            setup()
            code, out, _ = self.go("--judge-profile", f"codex={self.judge}", "--no-cache")
            self.assertEqual(code, 2, out)
            self.assertIn("codex", out)
            self.assertNotIn(SECRET, out)
            self.assertEqual(self.argv_rows(), [], "認証を確かめる前に CLI が起動した")

    def test_in_place_profile_as_a_solver_is_rejected(self):
        body = self.judge.read_text(encoding="utf-8").replace("codex-judge-inplace", "codex-solver-inplace")
        solver = write(self.ex / "profiles" / "codex-solver-inplace.yaml", body)
        code, out = run_cli("run", "--tasks", str(self.ex / "tasks" / "arithmetic-no-skill.yaml"), "--profiles", str(solver),
                            "--out", str(self.out), "--no-cache", "--no-llm")
        self.assertEqual(code, 2, out)
        self.assertIn("in_place", out)
        self.assertEqual(self.argv_rows(), [])  # 解く側として起動していない

    # ---------------------------------------------------------------- --judge-profile の形
    def test_judge_profile_forms(self):
        for ref in (str(self.judge), f"codex={self.judge}"):
            self.argv_log.unlink(missing_ok=True)
            code, out, _ = self.go("--judge-profile", ref, "--no-cache")
            self.assertEqual(code, 0, out)
            self.assertTrue(self.judge_calls(), ref)
            self.assertTrue(all(c["cfg"] == str(self.real) for c in self.judge_calls()), ref)

    def test_judge_profile_errors(self):
        for ref, word in ((f"claude_code={self.judge}", "codex"),  # 系統の不一致
                          (str(self.tmp / "nothing.yaml"), "nothing.yaml"),  # ないパス
                          ("no-such-id", "no-such-id")):
            code, out, _ = self.go("--judge-profile", ref, "--no-cache")
            self.assertEqual(code, 2, (ref, out))
            self.assertIn(word, out)

    def test_judge_profile_by_id_still_works(self):
        code, out, s = self.go("--judge-profile", "claude-baseline", "--judge-agent", "claude_code", "--allow-weak-judge",
                               "--judge-executable", PY, str(FAKE / "fake_claude.py"))
        self.assertEqual(code, 0, out)
        self.assertEqual(s["meta"]["judge"]["plan"]["claude_code"]["families"], ["claude_code"])


if __name__ == "__main__":
    unittest.main()
