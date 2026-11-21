"""examples/judge-profiles/ の例が、読めて、役割どおりの認証経路になっていること（認証の値は読まない・モデルは呼ばない）。"""
import contextlib
import io
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness_eval.cli import main  # noqa: E402
from harness_eval.spec import load_profiles  # noqa: E402

EXAMPLES = ROOT / "examples" / "judge-profiles"


class ExampleJudgeProfiles(unittest.TestCase):
    def profiles(self):
        return {p.id: p for p in load_profiles([str(EXAMPLES)])}

    def test_codex_judge_uses_the_real_codex_home_in_place(self):
        p = self.profiles()["codex-judge-inplace"]
        self.assertEqual((p.agent, p.credentials, p.copy_credentials), ("codex", "in_place", False))
        self.assertEqual(p.inherit_env, [])  # API キーも不要（auth.json をその場で使う）

    def test_claude_judge_borrows_only_the_oauth_token_by_name(self):
        p = self.profiles()["claude-judge-oauth"]
        self.assertEqual((p.agent, p.inherit_env, p.credentials, p.copy_credentials), ("claude_code", ["CLAUDE_CODE_OAUTH_TOKEN"], None, False))

    def test_solver_copies_for_d_pass_the_token_without_copying_credentials(self):
        ps = self.profiles()
        for pid in ("claude-baseline", "claude-ds-lite"):
            p = ps[pid]
            self.assertEqual((p.agent, p.inherit_env, p.billing, p.credentials, p.copy_credentials),
                             ("claude_code", ["CLAUDE_CODE_OAUTH_TOKEN"], "subscription", None, False), pid)
        self.assertTrue(ps["claude-ds-lite"].install)  # 配布物（ds-lite）は解く側だけ

    def test_example_files_never_contain_a_token_value(self):
        for f in EXAMPLES.glob("*.yaml"):
            text = f.read_text(encoding="utf-8")
            self.assertNotRegex(text, r"(sk-|eyJ)[A-Za-z0-9_\-]{10,}", f.name)

    def test_validate_accepts_the_directory(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            code = main(["validate", "--tasks", str(ROOT / "examples" / "tasks"), "--profiles", str(EXAMPLES)])
        self.assertEqual(code, 0, buf.getvalue())


if __name__ == "__main__":
    unittest.main()
