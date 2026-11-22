"""PR #5 r7（XREVIEW-AHE5-gemini-r5.md）: tamper の残りの抜け道（#1）と、デコレータ付きテストの回帰（#6）。

#1 a) `__wrapped__` による収集時のすり替え　b) `__code__` の差し替え（関数・クラスの本体の中・setattr・CodeType.replace）
   c) 束縛の走査漏れ（デコレータ・既定引数・基底・アノテーション・lambda の既定の walrus、`import *`）
   d) 動的チェックができなかったことを黙って空にしない
#6 デコレータ付きの正当なテストは改ざんなしで通る（co_firstlineno は最初のデコレータの行）。--rootdir・`__test__ = False`。"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from harness_eval import cheatproof  # noqa: E402
from harness_eval.cheatproof import capture_tests, rebound_report, rebound_tests, tamper_check  # noqa: E402

BASE = "def test_x():\n    assert 1 + 1 == 3\n"
CLS = "class TestA:\n    def test_m(self):\n        assert 1 + 1 == 3\n"
OK = "\n\ndef _ok():\n    assert True\n"


class Base(unittest.TestCase):
    def setUp(self):
        self.wd = Path(tempfile.mkdtemp(prefix="r7-"))
        self.tmp = Path(tempfile.mkdtemp(prefix="r7-tmp-"))
        self.addCleanup(shutil.rmtree, self.wd, True)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        (self.wd / "tests").mkdir()
        self.t = self.wd / "tests" / "test_a.py"

    def start(self, base: str):
        self.t.write_text(base, encoding="utf-8")
        self.before = capture_tests(self.wd)

    def static(self, new: str):
        self.t.write_text(new, encoding="utf-8")
        return [f["kind"] for f in tamper_check(self.before, self.wd)]

    def dynamic(self, new: str):
        self.t.write_text(new, encoding="utf-8")
        return rebound_tests(self.before, self.wd, self.tmp)


class StaticDunderWritesAnywhere(Base):
    """#1 b) 既存のテスト名の dunder 属性への書き込みは、関数・クラスの本体の中でも数える。"""

    def test_dunder_write_inside_a_function_class_or_method(self):
        cases = {
            "function": OK + "\n\ndef _go():\n    test_x.__code__ = _ok.__code__\n",
            "defaults": OK + "\n\ndef _go():\n    test_x.__defaults__ = (1,)\n",
            "class body": OK + "\n\nclass Patch:\n    test_x.__code__ = _ok.__code__\n",
            "method": OK + "\n\nclass Patch:\n    def go(self):\n        test_x.__kwdefaults__ = {}\n",
            "setattr": OK + "\n\ndef _go():\n    setattr(test_x, '__code__', _ok.__code__)\n",
            "delete": "\n\ndef _go():\n    del test_x.__code__\n",
        }
        for label, add in cases.items():
            with self.subTest(label):
                self.start(BASE)
                self.assertIn("test_body_modified", self.static(BASE + add), label)

    def test_a_dunder_write_on_an_unrelated_name_is_fine(self):
        self.start(BASE)
        self.assertEqual(self.static(BASE + "\n\ndef _go(o):\n    o.__code__ = 1\n    other.__dict__ = {}\n"), [])

    def test_dunder_writes_already_in_the_baseline_are_not_new(self):
        base = BASE + "\n\ndef _go():\n    test_x.__doc__ = 'x'\n"
        self.start(base)
        self.assertEqual(self.static(base + "\n\ndef test_y():\n    assert 1\n"), [])


class StaticBindingGaps(Base):
    """#1 c) 走査漏れだった位置の walrus（モジュールのスコープを束縛する）と `import *`。"""

    def test_walrus_in_headers_rebinds_a_test(self):
        cases = {
            "default": "def _(_=(test_x := _ok)):\n    pass\n",
            "kw default": "def _(*, k=(test_x := _ok)):\n    pass\n",
            "annotation": "def _(a: (test_x := _ok)):\n    pass\n",
            "return annotation": "def _() -> (test_x := _ok):\n    pass\n",
            "decorator": "@((test_x := _ok) and (lambda f: f))\ndef _():\n    pass\n",
            "base class": "class _((test_x := _ok) and object):\n    pass\n",
            "class keyword": "class _(metaclass=type((test_x := _ok))):\n    pass\n",
            "bare annotation": "_: (test_x := _ok)\n",
            "lambda default": "_ = lambda _=(test_x := _ok): 0\n",
            "async default": "async def _(_=(test_x := _ok)):\n    pass\n",
        }
        for label, code in cases.items():
            with self.subTest(label):
                self.start(BASE)
                self.assertIn("test_body_modified", self.static(BASE + OK + "\n\n" + code), label)

    def test_header_walrus_that_binds_another_name_is_fine(self):
        self.start(BASE)
        self.assertEqual(self.static(BASE + "\n\ndef _(_=(other := 1)):\n    pass\n"), [])

    def test_a_new_star_import_is_tamper(self):
        self.start(BASE)
        self.assertIn("test_body_modified", self.static(BASE + "\nfrom evil import *\n"))
        self.start(CLS)
        self.assertIn("test_body_modified", self.static(CLS + "\nfrom evil import *\n"))

    def test_a_star_import_already_in_the_baseline_is_not_new(self):
        base = "from helper import *\n\n\n" + BASE
        self.start(base)
        self.assertEqual(self.static(base + "\n\ndef test_y():\n    assert 1\n"), [])


class DynamicWrapperAndCode(Base):
    """#1 a)/b) 収集された実体（ラッパー側）と `__code__` の中身も基準と照合する。"""

    def assertRebound(self, base: str, new: str, label: str = ""):
        self.start(base)
        found = self.dynamic(new)
        self.assertEqual([f["kind"] for f in found], ["test_body_modified"], (label, found))

    def test_wrapped_attribute_swap_is_detected(self):
        self.assertRebound(BASE, BASE + OK + "_ok.__wrapped__ = test_x\nglobals()['test_x'] = _ok\n", "attribute")
        self.assertRebound(BASE, "import functools\n" + BASE + "\n\n@functools.wraps(test_x)\ndef _ok():\n    assert True\n\n\n"
                           "globals()['test_x'] = _ok\n", "wraps")
        self.assertRebound(CLS, CLS + "\n\ndef _ok(self):\n    assert True\n\n\n_ok.__wrapped__ = TestA.test_m\nsetattr(TestA, 'test_m', _ok)\n",
                           "setattr on a class")

    def test_code_replace_with_the_same_filename_and_line_is_detected(self):
        """__code__ を getattr/setattr の文字列組み立てで書く（静的には見えない）。位置は基準と同じだが中身（co_code）が違う。"""
        self.assertRebound(
            BASE, BASE + OK + "\n\ndef _go():\n    name = '__co' + 'de__'\n    c = test_x.__code__\n"
            "    setattr(test_x, name, _ok.__code__.replace(co_filename=c.co_filename, co_firstlineno=c.co_firstlineno))\n\n\n_go()\n")

    def test_code_constants_replaced_with_the_same_position_is_detected(self):
        self.assertRebound(
            BASE, BASE + "\n\ndef _go():\n    f = globals()['test_x']\n    n = '__co' + 'de__'\n    c = getattr(f, n)\n"
            "    setattr(f, n, c.replace(co_consts=tuple(2 if k == 3 else k for k in c.co_consts)))\n\n\n_go()\n")

    def test_legitimate_decorators_are_not_flagged(self):
        """#6 co_firstlineno は最初のデコレータの行。デコレータ付きの正当なテストは、静的にも収集時にも改ざんなし。"""
        base = (
            "import functools\nimport unittest\nimport unittest.mock\nimport pytest\n\n\n"
            "def retry(f):\n    @functools.wraps(f)\n    def wrapper(*a, **k):\n        return f(*a, **k)\n    return wrapper\n\n\n"
            "@pytest.mark.parametrize('n', [1, 2])\ndef test_param(n):\n    assert n in (1, 2)\n\n\n"
            "@unittest.mock.patch('os.getcwd')\ndef test_patch(m):\n    assert m is not None\n\n\n"
            "@pytest.mark.skipif(False, reason='r')\n@pytest.mark.usefixtures('tmp_path')\n@retry\ndef test_multi():\n    assert 1\n\n\n"
            "@retry\ndef test_local_wraps():\n    assert 1\n\n\n"
            "class TestC:\n    @pytest.mark.parametrize('n', [1])\n    def test_m(self, n):\n        assert n\n\n"
            "    @unittest.mock.patch('os.getcwd')\n    @retry\n    def test_p(self, m):\n        assert m is not None\n\n\n"
            "class TestU(unittest.TestCase):\n    @unittest.skipIf(False, 'x')\n    def test_u(self):\n        self.assertTrue(1)\n"
        )
        self.start(base)
        self.assertEqual(self.static(base), [])
        self.assertEqual(self.dynamic(base), [])
        shifted = base.replace("class TestC:", "def test_new():\n    assert 1\n\n\nclass TestC:")
        self.assertEqual(self.static(shifted), [])
        self.assertEqual(self.dynamic(shifted), [])

    def test_a_decorator_that_replaces_the_function_is_detected(self):
        """デコレータ付きでも、ラッパー（モジュールで新しく定義）が実体を置き換えれば検出する。"""
        base = ("import functools\n\n\ndef keep(f):\n    @functools.wraps(f)\n    def w(*a, **k):\n        return f(*a, **k)\n    return w\n\n\n"
                "@keep\ndef test_x():\n    assert 1 + 1 == 3\n")
        self.start(base)
        self.assertEqual(self.dynamic(base), [])
        self.assertRebound(base, base + "\n\ndef _ok():\n    assert True\n\n\n_ok.__wrapped__ = test_x.__wrapped__\nglobals()['test_x'] = _ok\n")

    def test_assertion_rewriting_does_not_look_like_a_code_change(self):
        base = ("import pytest\n\n\n@pytest.mark.parametrize('a,b', [(1, 1), (2, 2)])\ndef test_eq(a, b):\n    assert a == b, 'msg'\n"
                "    assert [x for x in (a, b)] == [a, b]\n\n\nclass TestK:\n    def test_k(self):\n        v = {'a': 1}\n        assert v['a'] == 1\n")
        self.start(base)
        self.assertEqual(self.dynamic(base), [])


class ProjectDecorators(Base):
    """独立レビューの指摘: 作業ツリー内の既存のデコレータ（テストのファイルの外）で飾られたテストは、改ざんなしなら誤検出しない。"""

    DECO = "import functools\n\n\ndef retry(fn):\n    return functools.wraps(fn)(lambda *a, **k: fn(*a, **k))\n"
    NESTED = "import functools\n\n\ndef retry(fn):\n    @functools.wraps(fn)\n    def wrapper(*a, **k):\n        return fn(*a, **k)\n    return wrapper\n"
    TEST = "from pkg.deco import retry\n\n\n@retry\ndef test_x():\n    assert 1 + 1 == 3\n"

    def setUp(self):
        super().setUp()
        (self.wd / "pkg").mkdir()
        (self.wd / "pkg" / "__init__.py").write_text("", encoding="utf-8")
        self.deco = self.wd / "pkg" / "deco.py"

    def begin(self, deco: str):
        self.deco.write_text(deco, encoding="utf-8")
        self.start(self.TEST)

    def test_an_untouched_decorator_with_a_lambda_wrapper_is_clean(self):
        self.begin(self.DECO)
        self.assertEqual(self.dynamic(self.TEST), [])

    def test_an_honest_fix_inside_the_decorator_wrapper_is_clean(self):
        self.begin(self.NESTED)
        self.deco.write_text(self.NESTED.replace("        return fn(*a, **k)", "        r = fn(*a, **k)\n        return r"), encoding="utf-8")
        self.assertEqual(self.dynamic(self.TEST), [])

    def test_a_new_module_level_function_or_a_reused_baseline_helper_as_the_wrapper_is_detected(self):
        helper = "def helper():\n    assert True\n"
        self.begin(self.NESTED + "\n\n" + helper)
        self.deco.write_text(self.NESTED + "\n\n" + helper, encoding="utf-8")
        reuse = self.TEST + "\nfrom pkg.deco import helper\nhelper.__wrapped__ = test_x.__wrapped__\nglobals()['test_x'] = helper\n"
        self.assertEqual([f["kind"] for f in self.dynamic(reuse)], ["test_body_modified"])
        (self.wd / "pkg" / "evil.py").write_text("def ok():\n    assert True\n", encoding="utf-8")  # 新しいファイルの関数
        evil = self.TEST + "\nfrom pkg.evil import ok\nok.__wrapped__ = test_x.__wrapped__\nglobals()['test_x'] = ok\n"
        self.assertEqual([f["kind"] for f in self.dynamic(evil)], ["test_body_modified"])

    def test_a_new_nested_function_in_the_decorator_is_detected(self):
        """既存のデコレータを書き換えて、新しい名前の内側の関数（テストの代わり）を返させる。"""
        self.begin(self.NESTED)
        self.deco.write_text(self.NESTED.replace("    return wrapper", "    def ok():\n        assert True\n    ok.__wrapped__ = fn\n    return ok"),
                             encoding="utf-8")
        self.assertEqual([f["kind"] for f in self.dynamic(self.TEST)], ["test_body_modified"])


class DunderOwners(Base):
    def test_a_dunder_write_on_a_helper_is_fine_but_on_a_test_bearing_class_is_not(self):
        base = "class Foo:\n    pass\n\n\ndef helper():\n    pass\n\n\nclass TestA:\n    def test_m(self):\n        assert 1\n"
        self.start(base)
        ok = base + "\n\ndef test_new():\n    Foo.__doc__ = 'd'\n    setattr(Foo, '__repr__', lambda s: 'x')\n    helper.__doc__ = 'h'\n"
        self.assertEqual(self.static(ok), [])
        for bad in ("TestA.__init__ = lambda s: None", "setattr(TestA, '__test__', False)", "TestA.test_m.__code__ = test_new.__code__"):
            with self.subTest(bad):
                self.assertIn("test_body_modified", self.static(base + f"\n\ndef test_new():\n    pass\n\n\ndef _go():\n    {bad}\n"))


class DynamicCheckUnavailable(Base):
    """#1 d) 動的チェックができなかったときは、黙って空にせず理由つきで返す。"""

    def test_a_collection_failure_is_reported_as_unavailable(self):
        self.start(BASE)
        (self.wd / "tests" / "conftest.py").write_text("raise RuntimeError('boom')\n", encoding="utf-8")
        found, reason = rebound_report(self.before, self.wd, self.tmp)
        self.assertEqual(found, [])
        self.assertIn("rc=", reason or "")

    def test_a_launch_failure_is_reported_as_unavailable(self):
        self.start(BASE)
        with mock.patch.object(cheatproof.sys, "executable", str(self.tmp / "no-such-python")):
            found, reason = rebound_report(self.before, self.wd, self.tmp)
        self.assertEqual(found, [])
        self.assertTrue(reason)

    def test_a_normal_run_and_a_file_without_tests_are_available(self):
        self.start(BASE)
        self.assertEqual(rebound_report(self.before, self.wd, self.tmp), ([], None))
        self.t.write_text("x = 1\n", encoding="utf-8")
        before = capture_tests(self.wd)
        self.assertEqual(rebound_report(before, self.wd, self.tmp), ([], None))

    def test_findings_are_still_returned_with_no_reason(self):
        self.start(BASE)
        self.t.write_text(BASE + OK + "\nglobals()['test_x'] = _ok\n", encoding="utf-8")
        found, reason = rebound_report(self.before, self.wd, self.tmp)
        self.assertEqual((len(found), reason), (1, None))


class CollectionEnvironment(Base):
    """#6 の副次: --rootdir を明示する。`__test__ = False` は pytest が集めない。"""

    def test_a_parent_directory_config_does_not_shift_node_ids(self):
        outer = Path(tempfile.mkdtemp(prefix="r7-outer-"))
        self.addCleanup(shutil.rmtree, outer, True)
        (outer / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
        wd = outer / "work"
        (wd / "tests").mkdir(parents=True)
        (wd / "tests" / "test_a.py").write_text(BASE, encoding="utf-8")
        before = capture_tests(wd)
        with mock.patch.object(cheatproof, "grader_isolation", return_value="none"):  # bwrap では親が見えない。隔離なしの環境
            self.assertEqual(rebound_tests(before, wd, self.tmp), [])

    def test_classes_and_modules_with_test_false_are_not_expected(self):
        text = "class TestOff:\n    __test__ = False\n\n    def test_m(self):\n        assert 1\n\n\ndef test_x():\n    assert 1\n"
        self.start(text)
        self.assertEqual(self.dynamic(text), [])
        mod = "__test__ = False\n\n\ndef test_x():\n    assert 1\n"
        self.start(mod)
        self.assertEqual(self.dynamic(mod), [])


if __name__ == "__main__":
    unittest.main()
