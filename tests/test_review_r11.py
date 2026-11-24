"""PR #5 r11（Gemini round 3 レビュー）の再現・回帰テスト。

ブロッキング 3 件と安価に直せる非ブロッキング 2 件:
1. _safe_trailing_expr の内包表記ターゲットによる range シャドウ迂回
2. types.MethodType アンラップの type(owner) is cls による同クラス別インスタンスすり替え
3. 装飾済みテスト（@pytest.mark.parametrize 等を含む）に対する _foreign_wrapper / _wraps_in_closure 迂回
   - クロージャなし stdlib 関数許可
   - "call" タグの lib_roots 素通り・新規クラス __call__ 偽装
   - known_digests での fn_unlinked 未拒否 + @pytest.mark.* の decorated 誤分類 + ContextDecorator 検証
N2: 末尾ヘルパー関数の既定引数・注釈によるインポート時副作用
N3: @functools.lru_cache 付き __call__ を持つ呼び出し可能クラスの誤検出
"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from harness_eval.cheatproof import capture_tests, rebound_report, tamper_check  # noqa: E402

BASE = "def test_x():\n    assert 1 + 1 == 3\n"
PARAM_BASE = (
    "import pytest\n\n\n"
    "@pytest.mark.parametrize('n', [1])\n"
    "def test_x(n):\n"
    "    assert 1 + 1 == 3\n"
)
PATCHED_BASE = (
    "import unittest.mock\n\n\n"
    "@unittest.mock.patch('os.getcwd')\n"
    "def test_x(m):\n"
    "    assert 1 + 1 == 3\n"
)
UNITTEST_BASE = (
    "import unittest\n\n\n"
    "class TestA(unittest.TestCase):\n"
    "    def test_x(self):\n"
    "        self.assertEqual(1 + 1, 3)\n"
)


class R11Base(unittest.TestCase):
    def setUp(self):
        self.wd = Path(tempfile.mkdtemp(prefix="r11-"))
        self.tmp = Path(tempfile.mkdtemp(prefix="r11-tmp-"))
        self.addCleanup(shutil.rmtree, self.wd, True)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        (self.wd / "tests").mkdir()
        self.t = self.wd / "tests" / "test_a.py"

    def start(self, base: str):
        conftest = self.wd / "conftest.py"
        if conftest.exists():
            conftest.unlink()
        self.t.write_text(base, encoding="utf-8")
        self.before = capture_tests(self.wd)

    def static_kinds(self, new: str) -> list[str]:
        self.t.write_text(new, encoding="utf-8")
        return [f["kind"] for f in tamper_check(self.before, self.wd)]

    def dynamic_report(self, new: str | None = None):
        if new is not None:
            self.t.write_text(new, encoding="utf-8")
        return rebound_report(self.before, self.wd, self.tmp)

    def assertDynamicTamper(self, label: str = ""):
        found, reason = self.dynamic_report()
        self.assertIsNone(reason, (label, reason))
        self.assertEqual([f["kind"] for f in found], ["test_body_modified"], (label, found))


class Blocking1ComprehensionShadowRange(R11Base):
    """指摘 1: 内包表記ターゲットによる range シャドウ迂回を遮断する。"""

    def test_comp_target_shadowing_range_in_elt_is_tamper(self):
        self.start(UNITTEST_BASE)
        exploit = (
            UNITTEST_BASE
            + "\nfrom builtins import setattr as _s\n"
            "def _noop(self, result=None):\n"
            "    return result\n"
            "_x = [range(TestA, 'run', _noop) for range in [_s]]\n"
        )
        self.assertIn("test_body_modified", self.static_kinds(exploit))

    def test_comp_target_shadowing_range_variants_are_tamper(self):
        header = (
            UNITTEST_BASE
            + "\nfrom builtins import setattr as _s\n"
            "def _noop(self, result=None):\n"
            "    return result\n"
        )
        variants = {
            "if clause": "_x = [1 for range in [_s] if range(TestA, 'run', _noop)]\n",
            "second generator iter": "_x = [1 for range in [_s] for _ in [range(TestA, 'run', _noop)]]\n",
            "tuple unpack target": "_x = [range(TestA, 'run', _noop) for _, (range,) in [(0, (_s,))]]\n",
            "nested comprehension": "_x = [[range(TestA, 'run', _noop) for _ in (1,)] for range in [_s]]\n",
            "set comp": "_x = {range(TestA, 'run', _noop) for range in [_s]}\n",
            "gen exp": "_x = (range(TestA, 'run', _noop) for range in [_s])\n",
            "dict comp key": "_x = {range(TestA, 'run', _noop): 1 for range in [_s]}\n",
            "dict comp value": "_x = {1: range(TestA, 'run', _noop) for range in [_s]}\n",
        }
        for label, line in variants.items():
            with self.subTest(label):
                self.start(UNITTEST_BASE)
                self.assertIn("test_body_modified", self.static_kinds(header + line), label)

    def test_legitimate_range_in_comprehension_remains_clean(self):
        self.start(BASE)
        clean = (
            BASE
            + "\nPAIRS = [(i, j) for i in range(3) for j in range(2) if i in range(3)]\n"
            "MAP = {i: i * 2 for i in range(3)}\n"
        )
        self.assertEqual(self.static_kinds(clean), [])


class Blocking2MethodTypeOtherInstance(R11Base):
    """指摘 2: 同クラス別インスタンスへの MethodType すり替えを遮断する。"""

    def test_method_type_bound_to_other_instance_of_same_class_is_detected(self):
        self.start(UNITTEST_BASE)
        (self.wd / "conftest.py").write_text(
            "import types\n\n"
            "def pytest_collection_modifyitems(items):\n"
            "    for it in items:\n"
            "        fake = it.cls('test_x')\n"
            "        fake.assertEqual = lambda *a, **k: None\n"
            "        it.cls.test_x = types.MethodType(it.cls.test_x, fake)\n"
            "        it._obj = it.cls.test_x\n",
            encoding="utf-8",
        )
        self.assertDynamicTamper("MethodType bound to another instance of same class")

    def test_method_type_bound_to_other_instance_inside_mock_patch_is_detected(self):
        base = (
            "import unittest\n"
            "import unittest.mock\n\n\n"
            "class TestA(unittest.TestCase):\n"
            "    @unittest.mock.patch('os.getcwd')\n"
            "    def test_x(self, m):\n"
            "        self.assertEqual(1 + 1, 3)\n"
        )
        self.start(base)
        (self.wd / "conftest.py").write_text(
            "import types\n"
            "from unittest import mock\n\n"
            "def pytest_collection_modifyitems(items):\n"
            "    for it in items:\n"
            "        fake = it.cls('test_x')\n"
            "        fake.assertEqual = lambda *a, **k: None\n"
            "        orig = it.cls.test_x.__wrapped__\n"
            "        it._obj = mock.patch('os.getcwd')(types.MethodType(orig, fake))\n",
            encoding="utf-8",
        )
        self.assertDynamicTamper("MethodType(orig, fake) wrapped in mock.patch")

    def test_method_type_bound_to_item_instance_stored_on_class_is_detected(self):
        self.start(UNITTEST_BASE)
        (self.wd / "conftest.py").write_text(
            "import types\n\n"
            "def pytest_collection_modifyitems(items):\n"
            "    for it in items:\n"
            "        it.cls.test_x = types.MethodType(it.cls.test_x, it.instance)\n"
            "        it._obj = it.cls.test_x\n",
            encoding="utf-8",
        )
        self.assertDynamicTamper("instance-bound MethodType stored on class attribute")


class Blocking3WrapperAndClosureBypasses(R11Base):
    """指摘 3: 装飾済みテストに対する _foreign_wrapper / _wraps_in_closure 複数迂回経路を遮断する。"""

    def test_no_closure_stdlib_fn_wrapper_is_detected(self):
        for label, base in {"parametrize": PARAM_BASE, "mock.patch": PATCHED_BASE}.items():
            with self.subTest(label):
                self.start(base)
                (self.wd / "conftest.py").write_text(
                    "import threading\n\n"
                    "def pytest_collection_modifyitems(items):\n"
                    "    for it in items:\n"
                    "        f = threading.RLock\n"
                    "        f.__wrapped__ = it.obj\n"
                    "        it._obj = f\n",
                    encoding="utf-8",
                )
                self.assertDynamicTamper(f"no-closure stdlib fn on {label}")

    def test_evil_callable_class_with_stdlib_dunder_call_is_detected(self):
        cases = {
            "mock.patch __call__ on parametrize": (
                PARAM_BASE,
                "from unittest import mock\n\n"
                "class _Evil:\n"
                "    __call__ = mock.patch('os.getcwd')(lambda self, *a, **k: None)\n\n"
                "def pytest_collection_modifyitems(items):\n"
                "    for it in items:\n"
                "        w = _Evil()\n"
                "        w.__wrapped__ = it.obj\n"
                "        it._obj = w\n",
            ),
            "mock.patch __call__ on mock.patch with decoy attr": (
                PATCHED_BASE,
                "from unittest import mock\n\n"
                "class _Evil:\n"
                "    __call__ = mock.patch('os.getcwd')(lambda self, *a, **k: None)\n\n"
                "def pytest_collection_modifyitems(items):\n"
                "    for it in items:\n"
                "        w = _Evil()\n"
                "        w.fn = it.obj\n"
                "        w.__wrapped__ = it.obj\n"
                "        it._obj = w\n",
            ),
            "spoofed __module__ __qualname__ on _Evil": (
                PATCHED_BASE,
                "import threading\n\n"
                "class _Evil:\n"
                "    __call__ = threading.RLock\n\n"
                "_Evil.__module__ = 'threading'\n"
                "_Evil.__qualname__ = 'RLock'\n\n"
                "def pytest_collection_modifyitems(items):\n"
                "    for it in items:\n"
                "        w = _Evil()\n"
                "        w.fn = it.obj\n"
                "        w.__wrapped__ = it.obj\n"
                "        it._obj = w\n",
            ),
        }
        for label, (base, conftest) in cases.items():
            with self.subTest(label):
                self.start(base)
                (self.wd / "conftest.py").write_text(conftest, encoding="utf-8")
                self.assertDynamicTamper(label)

    def test_baseline_nested_fn_unlinked_is_detected(self):
        for label, deco in {
            "parametrize": "@pytest.mark.parametrize('n', [1])\ndef test_x(n):\n",
            "mock.patch": "@unittest.mock.patch('os.getcwd')\ndef test_x(m):\n",
        }.items():
            with self.subTest(label):
                base = (
                    "import pytest\n"
                    "import unittest.mock\n\n\n"
                    "def helper(v):\n"
                    "    def inner(*a, **k):\n"
                    "        return v\n"
                    "    return inner\n\n\n"
                    + deco
                    + "    assert 1 + 1 == 3\n"
                )
                self.start(base)
                (self.wd / "conftest.py").write_text(
                    "def pytest_collection_modifyitems(items):\n"
                    "    for it in items:\n"
                    "        fn = it.module.helper(None)\n"
                    "        fn.__wrapped__ = it.obj\n"
                    "        it._obj = fn\n",
                    encoding="utf-8",
                )
                self.assertDynamicTamper(f"baseline nested fn_unlinked on {label}")

    def test_contextmanager_swallow_on_parametrized_and_decorated_test_is_detected(self):
        conftest = (
            "import contextlib\n\n"
            "@contextlib.contextmanager\n"
            "def _swallow():\n"
            "    try:\n"
            "        yield\n"
            "    except AssertionError:\n"
            "        pass\n\n"
            "def pytest_collection_modifyitems(items):\n"
            "    for it in items:\n"
            "        it._obj = _swallow()(it.obj)\n"
        )
        for label, base in {"parametrize": PARAM_BASE, "mock.patch": PATCHED_BASE}.items():
            with self.subTest(label):
                self.start(base)
                (self.wd / "conftest.py").write_text(conftest, encoding="utf-8")
                self.assertDynamicTamper(f"contextmanager swallow on {label}")

    def test_contextdecorator_subclass_swallow_on_decorated_test_is_detected(self):
        cases = {
            "custom ContextDecorator subclass": (
                "import contextlib\n\n"
                "class _SwallowCD(contextlib.ContextDecorator):\n"
                "    def __enter__(self):\n"
                "        return self\n"
                "    def __exit__(self, *a):\n"
                "        return True\n\n"
                "def pytest_collection_modifyitems(items):\n"
                "    for it in items:\n"
                "        it._obj = _SwallowCD()(it.obj)\n"
            ),
            "suppress + ContextDecorator mixin": (
                "import contextlib\n\n"
                "class _Evil(contextlib.suppress, contextlib.ContextDecorator):\n"
                "    pass\n\n"
                "def pytest_collection_modifyitems(items):\n"
                "    for it in items:\n"
                "        it._obj = _Evil(AssertionError)(it.obj)\n"
            ),
        }
        for label, conftest in cases.items():
            with self.subTest(label):
                self.start(PATCHED_BASE)
                (self.wd / "conftest.py").write_text(conftest, encoding="utf-8")
                self.assertDynamicTamper(label)


class NonBlockingFixesAndLegitimatePatterns(R11Base):
    """非ブロッキング 2（末尾ヘルパー既定引数・注釈）と 3（@lru_cache 付き __call__）および正当パターンの非誤検出。"""

    def test_trailing_helper_default_arg_and_annotation_side_effect_is_tamper(self):
        for label, suffix in {
            "default arg": "\ndef _h(x=setattr(TestA, 'run', lambda self, result=None: result)):\n    return x\n",
            "kwonly default": "\ndef _h(*, x=setattr(TestA, 'run', lambda self, result=None: result)):\n    return x\n",
            "param annotation": "\ndef _h(x: setattr(TestA, 'run', lambda self, result=None: result)):\n    return x\n",
            "return annotation": "\ndef _h() -> setattr(TestA, 'run', lambda self, result=None: result):\n    return None\n",
            "walrus range shadow in default": (
                "\nfrom builtins import setattr as _s\n"
                "def _noop(self, result=None):\n    return result\n"
                "def _h(a=(range := _s), b=range(TestA, 'run', _noop)):\n    return a\n"
            ),
            "walrus range shadow then trailing assign": (
                "\nfrom builtins import setattr as _s\n"
                "def _noop(self, result=None):\n    return result\n"
                "def _h(a=(range := _s)):\n    return a\n"
                "_x = [range(TestA, 'run', _noop)]\n"
            ),
        }.items():
            with self.subTest(label):
                self.start(UNITTEST_BASE)
                self.assertIn("test_body_modified", self.static_kinds(UNITTEST_BASE + suffix), label)

    def test_trailing_helper_with_safe_defaults_and_annotations_remains_clean(self):
        self.start(BASE)
        clean = (
            BASE
            + "\ndef _helper(x: int | None = None, items: list[tuple[str, int]] = ()) -> dict[str, int]:\n"
            "    return {}\n"
        )
        self.assertEqual(self.static_kinds(clean), [])

    def test_callable_class_with_lru_cache_dunder_call_is_not_flagged(self):
        base = (
            "import functools\n\n\n"
            "class Wrap:\n"
            "    def __init__(self, fn):\n"
            "        self.fn = fn\n"
            "        functools.update_wrapper(self, fn)\n\n"
            "    @functools.lru_cache(maxsize=None)\n"
            "    def __call__(self, *a, **k):\n"
            "        return self.fn(*a, **k)\n\n\n"
            "@Wrap\n"
            "def test_wrapped():\n"
            "    assert 1 == 1\n"
        )
        self.start(base)
        self.assertEqual(self.static_kinds(base), [])
        self.assertEqual(self.dynamic_report(base), ([], None))

    def test_legitimate_patterns_including_contextdecorator_and_marks_remain_clean(self):
        (self.wd / "pkg").mkdir()
        (self.wd / "pkg" / "__init__.py").write_text("", encoding="utf-8")
        (self.wd / "pkg" / "deco.py").write_text(
            "import contextlib\n"
            "import functools\n\n\n"
            "def keep(fn):\n"
            "    @functools.wraps(fn)\n"
            "    def inner(*a, **k):\n"
            "        return fn(*a, **k)\n"
            "    return inner\n\n\n"
            "class BaselineCD(contextlib.ContextDecorator):\n"
            "    def __enter__(self):\n"
            "        return self\n"
            "    def __exit__(self, *a):\n"
            "        return False\n",
            encoding="utf-8",
        )
        base = (
            "import contextlib\n"
            "import functools\n"
            "import unittest\n"
            "import unittest.mock\n"
            "import pytest\n"
            "from pytest import mark\n"
            "from pkg.deco import BaselineCD, keep\n\n\n"
            "@contextlib.contextmanager\n"
            "def noop_cm():\n"
            "    yield\n\n\n"
            "@mark.parametrize('n', [1, 2])\n"
            "def test_mark_alias(n):\n"
            "    assert n in (1, 2)\n\n\n"
            "@pytest.mark.parametrize('n', [1])\n"
            "@unittest.mock.patch('os.getcwd')\n"
            "@keep\n"
            "@noop_cm()\n"
            "@BaselineCD()\n"
            "def test_stacked(m, n):\n"
            "    assert m is not None and n == 1\n\n\n"
            "class TestClassMethods:\n"
            "    @staticmethod\n"
            "    def test_static():\n"
            "        assert True\n\n"
            "    @classmethod\n"
            "    def test_cls(cls):\n"
            "        assert cls is TestClassMethods\n\n\n"
            "class TestU(unittest.TestCase):\n"
            "    @unittest.skipIf(False, 'ok')\n"
            "    def test_u(self):\n"
            "        self.assertTrue(True)\n\n\n"
            "class TestAsync(unittest.IsolatedAsyncioTestCase):\n"
            "    @unittest.mock.patch('os.getcwd')\n"
            "    async def test_async(self, m):\n"
            "        self.assertIsNotNone(m)\n"
        )
        self.start(base)
        self.assertEqual(self.static_kinds(base), [])
        self.assertEqual(self.dynamic_report(base), ([], None))


if __name__ == "__main__":
    unittest.main()
