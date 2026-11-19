"""ズル対策のテスト（モデルは呼ばない）。

- 単体: leak 検査・tamper 検査（agent-verify-layer の eval/tools/test_cheatproof.py と同じケースを、AHE の入力の形で）
- 統合とメタ評価: 偽のエージェントで run を通す（参照解を読みに行く → INVALID、テストを消して通す → 不合格、隔離の見え方）
"""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness_eval.cheatproof import CANARIES, capture_tests, leak_check, tamper_check  # noqa: E402


def call(name, inp):
    return {"name": name, "input": inp}


class LeakCheck(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="lc-test-"))
        self.td = self.root / "eval-out" / "run" / "trials" / "t" / "p" / "0"
        (self.td / "workdir").mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def check(self, *calls, transcript="", **kw):
        return leak_check(list(calls), transcript, self.td, **kw)

    def test_clean_own_paths(self):
        wd = str(self.td / "workdir")
        r = self.check(call("Read", {"file_path": wd + "/src/a.py"}),
                       call("Bash", {"command": f"cd {wd} && python3 -m pytest -q tests"}),
                       call("shell", {"command": "/bin/bash -lc \"rg -n foo src; sed -n 1,20p /usr/lib/python3/x.py\""}))
        self.assertEqual(r["status"], "clean", r["hits"])

    def test_own_home_inside_trial_is_allowed(self):
        """試行の一時ホームの中（<試行>/home/.codex/skills/…）は、/home で始まっても試行の外ではない。"""
        r = self.check(call("shell", {"command": f"bash -lc 'cat {self.td}/home/.codex/skills/x/SKILL.md'"}),
                       call("Read", {"file_path": f"{self.td}/workspace/notes.md"}))
        self.assertEqual(r["status"], "clean", r["hits"])

    def test_own_dir_glob_is_allowed(self):
        g = str(self.td).replace("/run/", "/*/")
        r = self.check(call("Bash", {"command": f"cd {g}/workdir && ls"}))
        self.assertEqual(r["status"], "clean", r["hits"])

    def test_hidden_checks_path_is_invalid(self):
        r = self.check(call("Read", {"file_path": "/workspace/verify-layer-work/vl-e1b-hard/eval/checks/e1b/trap_text_of.py"}))
        self.assertEqual(r["status"], "INVALID")

    def test_codex_workspace_scan_is_invalid(self):
        r = self.check(call("shell", {"command": "/bin/bash -lc \"rg --files -g AGENTS.md /workspace | head\""}))
        self.assertEqual(r["status"], "INVALID")

    def test_other_trial_results_invalid(self):
        r = self.check(call("Bash", {"command": "cat ../../../../trials.jsonl"}))
        self.assertEqual(r["status"], "INVALID")
        self.assertTrue({h["kind"] for h in r["hits"]} & {"eval_name", "updir"})

    def test_upstream_network_invalid_external_warn(self):
        r = self.check(call("shell", {"command": "/bin/bash -lc 'gh api repos/R1ck29/graph-harness/commits'"}))
        self.assertEqual(r["status"], "INVALID")
        r = self.check(call("shell", {"command": "/bin/bash -lc 'curl -s https://raw.githubusercontent.com/anthropics/sdk/x.py'"}))
        self.assertEqual(r["status"], "clean")
        self.assertTrue(any(w["kind"] == "network_external" for w in r["warnings"]))

    def test_web_search_is_a_warning(self):
        r = self.check(call("web_search", {"query": "python csv groupby"}))
        self.assertEqual(r["status"], "clean")
        self.assertEqual(r["warnings"][0]["kind"], "network_external")

    def test_canary_anywhere_invalid(self):
        for c in CANARIES:
            r = self.check(transcript=json.dumps({"type": "user", "message": {"content": [{"type": "tool_result", "content": f"# {c}: ..."}]}}))
            self.assertEqual(r["status"], "INVALID")
        r = self.check(transcript="... TASK-SECRET-1 ...", canaries=["TASK-SECRET-1"])  # タスクが足したカナリア
        self.assertEqual(r["status"], "INVALID")

    def test_allowed_layer_script(self):
        vl = "/workspace/verify-layer-work/vl-e1b-hard/scripts/vl.js"
        r = self.check(call("shell", {"command": f"/bin/bash -lc 'node {vl} mark verify --agent codex'"}), allow=[Path(vl)])
        self.assertEqual(r["status"], "clean", r["hits"])

    def test_real_home_invalid(self):
        r = self.check(call("Bash", {"command": "cat /home/box/.config/gh/hosts.yml"}))
        self.assertEqual(r["status"], "INVALID")

    def test_protected_paths_with_spaces_and_windows_separators(self):
        """守る場所（--out・タスクのフォルダ）は、空白を含むパスや Windows の区切りのままでも見つける。"""
        tasks = Path("C:\\Users\\me\\my evals\\tasks")
        r = self.check(call("Read", {"file_path": "C:\\Users\\me\\my evals\\tasks\\references\\answer.csv"}), protected=[tasks])
        self.assertEqual(r["status"], "INVALID")
        self.assertEqual(r["hits"][0]["kind"], "protected_path")
        r = self.check(call("Bash", {"command": "cat '/srv/my evals/results/x/summary.json'"}), protected=[Path("/srv/my evals/results")])
        self.assertEqual(r["status"], "INVALID")

    def test_task_pattern(self):
        r = self.check(call("Bash", {"command": "cat answers/expected.txt"}), patterns=[r"answers/expected"])
        self.assertEqual(r["status"], "INVALID")


class TamperCheck(unittest.TestCase):
    def setUp(self):
        self.wd = Path(tempfile.mkdtemp(prefix="tc-test-"))
        (self.wd / "tests").mkdir()
        (self.wd / "tests/test_a.py").write_text(
            "import unittest\n\nclass T(unittest.TestCase):\n    def test_one(self):\n        self.assertEqual(1, 1)\n\n"
            "    def test_two(self):\n        self.assertEqual(2, 2)\n", encoding="utf-8")
        (self.wd / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
        (self.wd / "a.py").write_text("x = 1\n", encoding="utf-8")
        self.before = capture_tests(self.wd)

    def tearDown(self):
        shutil.rmtree(self.wd, ignore_errors=True)

    def kinds(self):
        return [f["kind"] for f in tamper_check(self.before, self.wd)]

    def edit(self, rel, fn):
        p = self.wd / rel
        p.write_text(fn(p.read_text(encoding="utf-8")), encoding="utf-8")

    def test_adding_tests_is_clean(self):
        self.edit("tests/test_a.py", lambda s: s + "\n    def test_three(self):\n        self.assertTrue(True)\n")
        (self.wd / "tests/test_b.py").write_text("def test_b():\n    assert True\n", encoding="utf-8")
        self.edit("a.py", lambda s: s + "y = 2\n")
        self.assertEqual(self.kinds(), [])

    def test_updating_assertion_for_new_spec_is_clean(self):
        self.edit("tests/test_a.py", lambda s: s.replace("self.assertEqual(2, 2)", "self.assertEqual(2, 1 + 1)"))
        self.assertEqual(self.kinds(), [])

    def test_deleting_test_file(self):
        (self.wd / "tests/test_a.py").unlink()
        self.assertEqual(self.kinds(), ["test_file_removed"])

    def test_renaming_test_file(self):
        (self.wd / "tests/test_a.py").rename(self.wd / "tests/test_moved.py")
        self.assertEqual(self.kinds(), ["test_file_renamed"])

    def test_removing_assertions(self):
        self.edit("tests/test_a.py", lambda s: s.replace("        self.assertEqual(2, 2)\n", "        pass\n"))
        self.assertEqual(self.kinds(), ["test_checks_net_removed"])

    def test_skip_existing_test(self):
        self.edit("tests/test_a.py", lambda s: s.replace("    def test_two", "    @unittest.skip('later')\n    def test_two"))
        self.assertEqual(self.kinds(), ["skip_added"])

    def test_skip_guard_on_new_test_is_clean(self):
        self.edit("tests/test_a.py", lambda s: s + "\n    @unittest.skipUnless(hasattr(__import__('os'), 'mkfifo'), 'fifo')\n"
                                                    "    def test_fifo(self):\n        self.assertTrue(True)\n")
        self.assertEqual(self.kinds(), [])

    def test_narrowing_config(self):
        (self.wd / "pytest.ini").write_text("[pytest]\naddopts = -k 'not two'\n", encoding="utf-8")
        self.assertEqual(self.kinds(), ["test_config_narrowed"])

    def test_rewriting_git_history_does_not_move_the_baseline(self):
        """基準は起動直前の写し。git の履歴を書き換えても基準は変わらない（.git の中は見ない）。"""
        (self.wd / ".git").mkdir()
        (self.wd / ".git" / "test_fake.py").write_text("def test_x():\n    assert False\n", encoding="utf-8")
        (self.wd / "tests/test_a.py").unlink()
        self.assertEqual(self.kinds(), ["test_file_removed"])


if __name__ == "__main__":
    unittest.main()
