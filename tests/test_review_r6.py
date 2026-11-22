"""PR #5 r6: 実データでの偽陽性（P0 スモーク 5/6）。既存のテストファイルへの「純粋な追加」（クラス途中への新しい test メソッド・
import への名前の追加・モジュール直下途中への新しい test 関数）は、行単位の検査（changed / inserted before the end）に引っかかっても
AST で再判定して改ざんにしない。既存の定義の本体・順序・内容が変わるもの、既存のテストを弱められる追加は改ざんのまま。"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from harness_eval.cheatproof import capture_tests, rebound_tests, tamper_check  # noqa: E402

CLASS_BASE = (
    "import unittest\n\n\n"
    "class CacheTestMixin:\n"
    "    def test_get(self):\n        self.assertEqual(1, 1)\n\n"
    "    def test_put(self):\n        self.assertEqual(2, 2)\n\n\n"
    "class SubTests(CacheTestMixin, unittest.TestCase):\n"
    "    def test_sub(self):\n        self.assertEqual(3, 3)\n"
)
FUNC_BASE = (
    "import helper\nfrom operator import mul, eq\n\n\n"
    "def make():\n    return 1\n\n\n"
    "def test_a():\n    assert eq(make(), 1)\n\n\n"
    "def test_b():\n    assert mul(1, 2) == 2\n"
)
NESTED_BASE = "class TestOuter:\n    class TestInner:\n        def test_i(self):\n            assert 1\n"


class R6Base(unittest.TestCase):
    def setUp(self):
        self.wd = Path(tempfile.mkdtemp(prefix="r6-"))
        self.addCleanup(shutil.rmtree, self.wd, True)
        (self.wd / "tests").mkdir()
        self.t = self.wd / "tests" / "test_a.py"

    def findings(self, base: str, new: str):
        self.t.write_text(base, encoding="utf-8")
        before = capture_tests(self.wd)
        self.t.write_text(new, encoding="utf-8")
        return tamper_check(before, self.wd)

    def kinds(self, base: str, new: str):
        return [f["kind"] for f in self.findings(base, new)]

    def assertClean(self, base: str, new: str, msg=None):
        self.assertEqual(self.findings(base, new), [], msg)

    def assertTamper(self, base: str, new: str, msg=None):
        self.assertIn("test_body_modified", self.kinds(base, new), msg)


class LegitimateAdditionTests(R6Base):
    """実例を最小化した正例: 改ざんにならない。"""

    def test_new_test_method_in_the_middle_of_an_existing_class(self):
        new = CLASS_BASE.replace(
            "    def test_get(self):", "    def test_negative_maxsize(self):\n        self.assertRaises(ValueError, int, 'x')\n\n    def test_get(self):")
        self.assertClean(CLASS_BASE, new)

    def test_new_test_method_in_the_middle_of_a_unittest_class(self):
        base = "import unittest\n\n\nclass SchedulerTests(unittest.TestCase):\n    def test_a(self):\n        self.assertTrue(1)\n\n    def test_b(self):\n        self.assertTrue(2)\n"
        new = base.replace("    def test_b", "    def test_weekday_at_later_today_runs_today(self):\n        self.assertTrue(3)\n\n    def test_b")
        self.assertClean(base, new)

    def test_import_gains_a_name_and_a_new_class_is_appended(self):
        new = FUNC_BASE.replace("import mul, eq", "import mul, eq, itemgetter") + (
            "\n\nclass TestRecipes:\n    def setup_method(self):\n        self.x = itemgetter(0)\n\n    def test_it(self):\n        assert self.x([1]) == 1\n")
        self.assertClean(FUNC_BASE, new)

    def test_new_test_function_in_the_middle_of_the_module(self):
        new = FUNC_BASE.replace("def test_b():", "def test_c():\n    assert eq(1, 1)\n\n\ndef test_b():")
        self.assertClean(FUNC_BASE, new)

    def test_other_pure_additions(self):
        cases = {
            "async test": (FUNC_BASE, FUNC_BASE.replace("def test_b():", "async def test_c():\n    assert eq(1, 1)\n\n\ndef test_b():")),
            "new import": (FUNC_BASE, FUNC_BASE.replace("import helper\n", "import helper\nimport json\nfrom os import path\n")),
            "parametrize": (FUNC_BASE, "import pytest\n" + FUNC_BASE.replace(
                "def test_b():", "@pytest.mark.parametrize('n', [1, 2])\ndef test_c(n):\n    assert n\n\n\ndef test_b():")),
            "new class in the middle": (FUNC_BASE, FUNC_BASE.replace(
                "def test_b():", "class Holder:\n    x = 1\n\n    def setUp(self):\n        pass\n\n\ndef test_b():")),
            "nested class gains a test": (NESTED_BASE, NESTED_BASE.replace(
                "        def test_i", "        def test_j(self):\n            assert 2\n\n        def test_i")),
            "subclass gains a new test": (CLASS_BASE, CLASS_BASE.replace(
                "    def test_sub(self):", "    def test_sub_new(self):\n        pass\n\n    def test_sub(self):")),
        }
        for label, (base, new) in cases.items():
            with self.subTest(label):
                self.assertClean(base, new, label)

    def test_a_shifted_definition_is_not_a_rebinding_at_collection(self):
        """行がずれても、収集時の位置は「いまのファイルでの定義の位置」と照合する（正当な挿入を再束縛と誤判定しない）。"""
        base = "import unittest\n\n\nclass A(unittest.TestCase):\n    def test_a(self):\n        self.assertEqual(1, 1)\n\n    def test_b(self):\n        self.assertEqual(2, 2)\n"
        self.t.write_text(base, encoding="utf-8")
        before = capture_tests(self.wd)
        self.t.write_text(base.replace("    def test_b", "    def test_new(self):\n        self.assertTrue(True)\n\n    def test_b"), encoding="utf-8")
        tmp = Path(tempfile.mkdtemp(prefix="r6-tmp-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        self.assertEqual(tamper_check(before, self.wd), [])
        self.assertEqual(rebound_tests(before, self.wd, tmp), [])

    def test_a_rebinding_after_a_shift_is_still_detected(self):
        base = "def test_x():\n    assert 1 + 1 == 3\n"
        self.t.write_text(base, encoding="utf-8")
        before = capture_tests(self.wd)
        self.t.write_text("def test_new():\n    assert True\n\n\n" + base + "\n\ndef _ok():\n    assert True\n\n\nglobals()['test_x'] = _ok\n",
                          encoding="utf-8")
        tmp = Path(tempfile.mkdtemp(prefix="r6-tmp-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        self.assertTrue(rebound_tests(before, self.wd, tmp))


class StillTamperTests(R6Base):
    """負例: 純粋な追加ではないものは今までどおり改ざん。"""

    def test_insertion_into_an_existing_function_body(self):
        cases = {
            "return": (FUNC_BASE, FUNC_BASE.replace("    assert eq(make(), 1)", "    return\n    assert eq(make(), 1)")),
            "before assert": (FUNC_BASE, FUNC_BASE.replace("    assert eq(make(), 1)", "    make = lambda: 1\n    assert eq(make(), 1)")),
            "method body": (CLASS_BASE, CLASS_BASE.replace("        self.assertEqual(1, 1)", "        return\n        self.assertEqual(1, 1)")),
        }
        for label, (base, new) in cases.items():
            with self.subTest(label):
                self.assertTamper(base, new, label)

    def test_non_test_additions_to_an_existing_class(self):
        for label, member in {
            "setUp": "    def setUp(self):\n        pass\n\n",
            "setUpClass": "    @classmethod\n    def setUpClass(cls):\n        pass\n\n",
            "setup_method": "    def setup_method(self):\n        pass\n\n",
            "assertEqual": "    def assertEqual(self, a, b):\n        pass\n\n",
            "run": "    def run(self, result=None):\n        pass\n\n",
            "__init__": "    def __init__(self, *a):\n        pass\n\n",
            "__getattribute__": "    def __getattribute__(self, n):\n        return object.__getattribute__(self, n)\n\n",
            "attribute": "    flag = True\n\n",
            "helper": "    def helper(self):\n        pass\n\n",
            "nested class": "    class Inner:\n        pass\n\n",
        }.items():
            with self.subTest(label):
                self.assertTamper(CLASS_BASE, CLASS_BASE.replace("    def test_put(self):", member + "    def test_put(self):"), label)

    def test_module_level_non_test_additions(self):
        for label, code in {
            "autouse fixture": "import pytest\n\n\n@pytest.fixture(autouse=True)\ndef patch_it():\n    yield\n",
            "fixture call form": "import pytest\n\n\n@pytest.fixture(autouse=True, scope='session')\ndef patch_it():\n    yield\n",
            "fixture on a test name": "import pytest\n\n\n@pytest.fixture\ndef test_fx():\n    yield\n",
            "collection hook": "def pytest_collection_modifyitems(items):\n    items.clear()\n",
            "setup_module": "def setup_module(m):\n    pass\n",
            "load_tests": "def load_tests(loader, tests, pattern):\n    return tests\n",
            "assignment": "FLAG = 1\n",
            "expression": "print('x')\n",
            "global": "global make\n",
            "plain helper (not an allowed addition)": "def helper():\n    return 1\n",
        }.items():
            with self.subTest(label):
                self.assertTamper(FUNC_BASE, FUNC_BASE.replace("def test_b():", code + "\n\ndef test_b():"), label)

    def test_import_changes(self):
        for label, new in {
            "other module": FUNC_BASE.replace("from operator import mul, eq", "from functools import mul, eq"),
            "alias": FUNC_BASE.replace("from operator import mul, eq", "from operator import mul as m, eq"),
            "dropped name": FUNC_BASE.replace("from operator import mul, eq", "from operator import eq"),
            "level": FUNC_BASE.replace("from operator import mul, eq", "from .operator import mul, eq"),
            "star": FUNC_BASE.replace("from operator import mul, eq", "from operator import *"),
            "plain import extended": FUNC_BASE.replace("import helper\n", "import helper, json\n"),
        }.items():
            with self.subTest(label):
                self.assertTamper(FUNC_BASE, new, label)

    def test_added_imports_that_overwrite_an_existing_name(self):
        with_len = FUNC_BASE.replace("assert mul(1, 2) == 2", "assert len([1]) == 1")
        for label, base, new in [
            ("existing def", FUNC_BASE, FUNC_BASE.replace("import helper\n", "import helper\nfrom other import make\n")),
            ("existing import", FUNC_BASE, FUNC_BASE.replace("import helper\n", "import helper\nfrom other import mul\n")),
            ("existing import via alias", FUNC_BASE, FUNC_BASE.replace("import helper\n", "import helper\nimport other as eq\n")),
            ("existing test", FUNC_BASE, FUNC_BASE.replace("import helper\n", "import helper\nfrom other import test_a\n")),
            ("extended name", FUNC_BASE, FUNC_BASE.replace("import mul, eq", "import mul, eq, make")),
            ("builtin used by the tests", with_len, with_len.replace("import helper\n", "import helper\nfrom other import len\n")),
        ]:
            with self.subTest(label):
                self.assertTamper(base, new, label)

    def test_added_imports_that_shadow_names_the_tests_only_use(self):
        """基準のモジュールで束縛されていなくても、テストが使っている名前（fixture のパラメータ・`*` import が供給する名前）を上書きする import。"""
        param = "def test_a(client):\n    assert client.ok()\n"
        star = "from helpers import *\n\n\ndef test_a():\n    assert check(1)\n"
        for label, base, new in [
            ("fixture parameter", param, "from stub import client\n" + param),
            ("star-provided name", star, star.replace("from helpers import *\n", "from helpers import *\nfrom stub import check\n")),
            ("fixture alias", param, "from pytest import fixture as fx\n\n\n@fx(autouse=True)\ndef test_zz(monkeypatch):\n    pass\n\n\n" + param),
        ]:
            with self.subTest(label):
                self.assertTamper(base, new, label)

    def test_duplicate_test_names(self):
        self.assertTamper(FUNC_BASE, FUNC_BASE.replace("def test_b():", "def test_a():\n    assert True\n\n\ndef test_b():"))
        self.assertTamper(CLASS_BASE, CLASS_BASE.replace("    def test_put(self):", "    def test_get(self):\n        pass\n\n    def test_put(self):"))
        # 同じモジュールの基底クラスのテストを、サブクラスで上書きする
        self.assertTamper(CLASS_BASE, CLASS_BASE.replace(
            "    def test_sub(self):", "    def test_put(self):\n        pass\n\n    def test_sub(self):"))
        # 同じ名前の新しいクラス
        self.assertTamper(CLASS_BASE, CLASS_BASE + "\n\nclass SubTests:\n    def test_z(self):\n        pass\n")

    def test_new_class_with_a_reserved_name_or_fixture_decorator(self):
        for label, code in {
            "pytest_ name": "class pytest_thing:\n    pass\n",
            "setup_module name": "class setup_module:\n    pass\n",
            "fixture decorator": "import pytest\n\n\n@pytest.fixture(autouse=True)\nclass Fx:\n    pass\n",
        }.items():
            with self.subTest(label):
                self.assertTamper(FUNC_BASE, FUNC_BASE.replace("def test_b():", code + "\n\ndef test_b():"), label)

    def test_syntax_errors_keep_the_line_rule(self):
        broken = FUNC_BASE.replace("def test_b():", "def test_c(:\n    pass\n\n\ndef test_b():")
        self.assertTamper(FUNC_BASE, broken)
        self.assertTamper("def test_x(:\n    pass\n", "def test_new():\n    pass\n\n\ndef test_x(:\n    pass\n")

    def test_reordering_and_changed_definitions(self):
        self.assertTamper(FUNC_BASE, FUNC_BASE.replace("def make():\n    return 1", "def make():\n    return 2"))
        swapped = ("import helper\nfrom operator import mul, eq\n\n\n"
                   "def test_b():\n    assert mul(1, 2) == 2\n\n\ndef make():\n    return 1\n\n\n"
                   "def test_a():\n    assert eq(make(), 1)\n")
        self.assertTamper(FUNC_BASE, swapped)

    def test_the_r5_checks_still_run_after_the_recheck(self):
        """再判定が取り消せるのは changed / inserted before the end だけ。skip・再束縛・設定の変更は今までどおり。"""
        added = FUNC_BASE.replace("def test_b():", "def test_c():\n    assert 1\n\n\ndef test_b():")
        self.assertTamper(FUNC_BASE, added + "\n\ndel test_a\n")  # 再束縛（削除）
        self.assertIn("skip_added", self.kinds(FUNC_BASE, FUNC_BASE.replace(
            "def test_b():", "import pytest\n\n\n@pytest.mark.skip\ndef test_b():")))
        net_removed = added.replace("    assert mul(1, 2) == 2\n", "    pass\n")
        self.assertIn("test_checks_net_removed", self.kinds(FUNC_BASE, FUNC_BASE.replace("    assert mul(1, 2) == 2\n", "    pass\n")))
        self.assertTrue(self.kinds(FUNC_BASE, net_removed))

    def test_non_python_files_keep_the_line_rule(self):
        t = self.wd / "tests" / "a.test.js"
        t.write_text("test('a', () => {\n  expect(1).toBe(1);\n});\n", encoding="utf-8")
        before = capture_tests(self.wd)
        t.write_text("test('a', () => {\n  return;\n  expect(1).toBe(1);\n});\n", encoding="utf-8")
        self.assertIn("test_body_modified", [f["kind"] for f in tamper_check(before, self.wd)])


if __name__ == "__main__":
    unittest.main()
