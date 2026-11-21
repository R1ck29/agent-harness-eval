"""PR #5 r4 レビュー（XREVIEW-AHE5-opus-r4.md）の指摘 #2/#6（テスト名の再束縛・削除）のテスト。"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from harness_eval.adapters import CodexAdapter  # noqa: E402
from harness_eval.cheatproof import capture_tests, rebound_tests, tamper_check  # noqa: E402
from harness_eval.isolation import run_process  # noqa: E402
from harness_eval.runner import _solver_accounting_lines  # noqa: E402

BASE = "def test_x():\n    assert 1 + 1 == 2\n"
OTHER = "\n\ndef other():\n    pass\n"


class StaticRebindTests(unittest.TestCase):
    """同じスコープでの束縛・削除は、構文を問わずすべて数える。"""

    def setUp(self):
        self.wd = Path(tempfile.mkdtemp(prefix="r5r-"))
        self.addCleanup(shutil.rmtree, self.wd, True)
        (self.wd / "tests").mkdir()
        self.t = self.wd / "tests" / "test_a.py"
        self.t.write_text(BASE, encoding="utf-8")
        self.before = capture_tests(self.wd)

    def kinds(self, appended: str, base: str = BASE):
        self.t.write_text(base, encoding="utf-8")
        self.before = capture_tests(self.wd)
        self.t.write_text(base + appended, encoding="utf-8")
        return [f["kind"] for f in tamper_check(self.before, self.wd)]

    def test_each_binding_form_on_a_test_name_is_detected(self):
        forms = {
            "for": "for test_x in (other,):\n    pass\n",
            "with": "import contextlib\nwith contextlib.nullcontext(other) as test_x:\n    pass\n",
            "except": "try:\n    raise ValueError\nexcept ValueError as test_x:\n    pass\n",
            "import_as": "import os as test_x\n",
            "from_import_as": "from os import path as test_x\n",
            "match": "match other:\n    case test_x:\n        pass\n",
            "match_star": "match [1]:\n    case [*test_x]:\n        pass\n",
            "del": "del test_x\n",
            "walrus": "(test_x := other)\n",
            "global_in_function": "def rebind():\n    global test_x\n    test_x = other\n\n\nrebind()\n",
            "global_in_class_body": "class Holder:\n    global test_x\n    test_x = other\n",
            "global_in_nested_function": "def outer():\n    def inner():\n        global test_x\n        test_x = other\n    inner()\n",
        }
        for label, code in forms.items():
            with self.subTest(label):
                self.assertIn("test_body_modified", self.kinds(OTHER + "\n" + code), label)

    def test_method_bindings_in_class_scope_are_detected(self):
        base = "class TestA:\n    def test_x(self):\n        assert 1\n"
        for label, code in {"del": "    del test_x\n", "for": "    for test_x in (print,):\n        pass\n",
                            "import_as": "    import os as test_x\n"}.items():
            with self.subTest(label):
                self.assertIn("test_body_modified", self.kinds("\n" + code, base), label)

    def test_legitimate_additions_are_still_allowed(self):
        legit = ("\n\ndef test_y():\n    import os as o\n    for i in range(3):\n        assert i >= 0\n"
                 "    with open(__file__) as f:\n        assert f\n    try:\n        pass\n    except ValueError as e:\n        raise e\n"
                 "\n\ndef helper():\n    test_x = 1  # a local, not the module-level test\n    return test_x\n"
                 "\n\nclass TestB:\n    def test_x(self):\n        assert 1\n\n\nvalues = [test_x for test_x in range(2)]\n"
                 "\n\ndef uses_global():\n    global counter\n    counter = 1\n")
        self.assertEqual(self.kinds(legit), [])


class DynamicRebindTests(unittest.TestCase):
    """収集時に実際に実行される関数の位置（ファイル・行）が、基準の定義と一致するか。"""

    def setUp(self):
        self.wd = Path(tempfile.mkdtemp(prefix="r5r-dyn-"))
        self.tmp = Path(tempfile.mkdtemp(prefix="r5r-tmp-"))
        self.addCleanup(shutil.rmtree, self.wd, True)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        (self.wd / "tests").mkdir()
        self.t = self.wd / "tests" / "test_a.py"
        self.original = ("class TestA:\n    def test_m(self):\n        assert 1 + 1 == 3\n\n\n"
                         "def test_x():\n    assert 1 + 1 == 3\n")
        self.t.write_text(self.original, encoding="utf-8")
        self.before = capture_tests(self.wd)

    def run_check(self):
        return rebound_tests(self.before, self.wd, self.tmp)

    def test_untouched_and_legitimately_extended_files_are_clean(self):
        self.assertEqual(self.run_check(), [])
        self.t.write_text(self.original + "\n\ndef test_new():\n    assert True\n", encoding="utf-8")
        self.assertEqual(self.run_check(), [])
        (self.wd / "tests" / "test_new.py").write_text("def test_z():\n    assert 1\n", encoding="utf-8")
        self.assertEqual(self.run_check(), [])

    def test_setattr_on_a_class_is_detected(self):
        self.t.write_text(self.original + "\n\ndef _ok(self):\n    assert True\n\n\nsetattr(TestA, 'test_m', _ok)\n", encoding="utf-8")
        found = self.run_check()
        self.assertEqual([f["kind"] for f in found], ["test_body_modified"])
        self.assertIn("TestA.test_m", found[0]["line"])

    def test_globals_assignment_is_detected(self):
        self.t.write_text(self.original + "\n\ndef _ok():\n    assert True\n\n\nglobals()['test_x'] = _ok\n", encoding="utf-8")
        found = self.run_check()
        self.assertEqual([f["kind"] for f in found], ["test_body_modified"])
        self.assertIn("test_x", found[0]["line"])

    def test_rebinding_from_a_new_conftest_is_detected(self):
        (self.wd / "tests" / "__init__.py").write_text("", encoding="utf-8")
        (self.wd / "conftest.py").write_text(
            "import tests.test_a as m\n\n\ndef _ok():\n    assert True\n\n\nm.test_x = _ok\n", encoding="utf-8")
        self.assertEqual([f["kind"] for f in self.run_check()], ["test_body_modified"])

    def test_a_vanished_test_is_detected(self):
        self.t.write_text(self.original + "\n\ndel test_x\n", encoding="utf-8")
        self.assertEqual([f["kind"] for f in self.run_check()], ["test_body_modified"])

    def test_the_host_process_never_imports_the_modified_module(self):
        marker = self.tmp / "ran-in-host"
        self.t.write_text(self.original + f"\n\nimport sys\nopen({str(marker)!r}, 'w').write(str(sys.argv))\n", encoding="utf-8")
        before_modules = set(sys.modules)
        self.run_check()
        self.assertNotIn("test_a", set(sys.modules) - before_modules)
        self.assertNotIn("tests.test_a", set(sys.modules) - before_modules)


USAGE = '{"type": "turn.completed", "usage": {"input_tokens": 1200, "cached_input_tokens": 200, "output_tokens": 34}}'


class DrainCapTests(unittest.TestCase):
    """r4 #7: drain がディスクへ書く量に上限を置き、超えたあとも使用量の行だけは残す。"""

    def run_noisy(self, limit: int, script: str):
        tmp = Path(tempfile.mkdtemp(prefix="r5r-drain-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        out, err = tmp / "stdout.jsonl", tmp / "stderr.txt"
        res = run_process([sys.executable, "-c", script], tmp, {"PATH": ""}, None, out, err, 60, log_limit=limit)
        return res, out, err

    def test_garbage_is_capped_but_the_trailing_usage_event_is_kept(self):
        script = ("import sys\n"
                  "for i in range(400):\n"
                  "    sys.stdout.write('garbage-' * 2000 + '\\n')\n"  # 約 14 KiB × 400 = 5.6 MiB
                  "sys.stdout.write('x' * 300000)\n"  # 改行のない長い断片
                  f"sys.stdout.write('\\n' + {USAGE!r} + '\\n')\n"
                  "sys.stderr.write('e' * 3000000)\n")
        limit = 1024 * 1024
        res, out, err = self.run_noisy(limit, script)
        self.assertTrue(res.log_truncated)
        self.assertEqual(res.returncode, 0)
        self.assertLess(out.stat().st_size, limit + 4096)  # 上限（と、使用量の行）で止まる
        self.assertLess(err.stat().st_size, limit + 4096)
        lines = list(_solver_accounting_lines(out))
        usage = CodexAdapter().parse(lines, usage_only=True)
        self.assertEqual(usage.tokens["input"], 1200)
        self.assertEqual(usage.tokens["output"], 34)

    def test_output_under_the_cap_is_written_whole_and_not_flagged(self):
        res, out, _ = self.run_noisy(1024 * 1024, f"print({USAGE!r})")
        self.assertFalse(res.log_truncated)
        self.assertEqual(out.read_text(encoding="utf-8").strip(), USAGE)

    def test_only_a_bounded_number_of_usage_lines_is_kept(self):
        script = f"for _ in range(200000):\n    print({USAGE!r})\n"  # すべて使用量の形の行 約 20 MiB
        limit = 256 * 1024
        res, out, _ = self.run_noisy(limit, script)
        self.assertTrue(res.log_truncated)
        self.assertLess(out.stat().st_size, limit + 2 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
