"""PR #5 r12（Gemini round 4 レビュー）の再現・回帰テスト。

ブロッキング 4 件:
1. ContextDecorator / AsyncContextDecorator / _GeneratorContextManager の _recreate_cm 差し替え・素の ContextDecorator 許可
2. "call" ラッパー経路での lib_roots 任意呼び出し可能クラス（unittest.TestSuite 等）素通り
3. 非テスト .py ファイルの既存関数・既存クラス改変による known_fn_digests / known_classes / known_callable_digests 迂回
4. unittest.mock.patch ラッパーの patched.patchings / additional_patchers / decoration_helper 差し替え

非ブロッキング 4 件:
N1. 未変更の基準 @unittest.skip / @unittest.skipIf(True, ...) / @unittest.skipUnless(False, ...) の誤検出
N2. _extra_for_fn の sync/async 両対応コンテキストマネージャでの __aenter__/__aexit__ 未検証
N3. _wraps_in_obj の __slots__ のみクラスでの判定ずれ
N4. 末尾補助関数の型注釈での負数・正数リテラル（Literal[-1] 等）拒否
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
PATCHED_BASE = (
    "import unittest.mock\n\n\n"
    "@unittest.mock.patch('os.getcwd')\n"
    "def test_x(m):\n"
    "    assert 1 + 1 == 3\n"
)
KEEP_BASE = (
    "import functools\n\n\n"
    "def keep(fn):\n"
    "    @functools.wraps(fn)\n"
    "    def inner(*a, **k):\n"
    "        return fn(*a, **k)\n"
    "    return inner\n\n\n"
    "@keep\n"
    "def test_x():\n"
    "    assert 1 + 1 == 3\n"
)


class R12Base(unittest.TestCase):
    def setUp(self):
        self.wd = Path(tempfile.mkdtemp(prefix="r12-"))
        self.tmp = Path(tempfile.mkdtemp(prefix="r12-tmp-"))
        self.addCleanup(shutil.rmtree, self.wd, True)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        (self.wd / "tests").mkdir()
        self.t = self.wd / "tests" / "test_a.py"

    def start(self, base: str, extra_files: dict[str, str] | None = None):
        conftest = self.wd / "conftest.py"
        if conftest.exists():
            conftest.unlink()
        for rel, content in (extra_files or {}).items():
            p = self.wd / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
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


class Blocking1ContextDecoratorRecreateCm(R12Base):
    """指摘 1: ContextDecorator / AsyncContextDecorator / _GeneratorContextManager の _recreate_cm 差し替え迂回。"""

    def test_bare_context_decorator_recreate_cm_swap_is_detected(self):
        self.start(PATCHED_BASE)
        (self.wd / "conftest.py").write_text(
            "import contextlib\n\n"
            "def pytest_collection_modifyitems(items):\n"
            "    for it in items:\n"
            "        cd = contextlib.ContextDecorator()\n"
            "        cd._recreate_cm = lambda: contextlib.suppress(AssertionError)\n"
            "        it._obj = cd(it.obj)\n",
            encoding="utf-8",
        )
        self.assertDynamicTamper("bare ContextDecorator._recreate_cm swap")

    def test_generator_context_manager_recreate_cm_swap_is_detected(self):
        self.start(PATCHED_BASE)
        (self.wd / "conftest.py").write_text(
            "import contextlib\n"
            "import threading\n\n"
            "def pytest_collection_modifyitems(items):\n"
            "    for it in items:\n"
            "        cm = contextlib._GeneratorContextManager(threading.RLock, (), {})\n"
            "        cm._recreate_cm = lambda: contextlib.suppress(AssertionError)\n"
            "        it._obj = cm(it.obj)\n",
            encoding="utf-8",
        )
        self.assertDynamicTamper("_GeneratorContextManager._recreate_cm swap")

    def test_async_context_decorator_recreate_cm_swap_is_detected(self):
        base = (
            "import unittest\n"
            "import unittest.mock\n\n\n"
            "class TestAsync(unittest.IsolatedAsyncioTestCase):\n"
            "    @unittest.mock.patch('os.getcwd')\n"
            "    async def test_x(self, m):\n"
            "        self.assertEqual(1 + 1, 3)\n"
        )
        self.start(base)
        (self.wd / "conftest.py").write_text(
            "import contextlib\n"
            "import types\n\n"
            "class _AsyncSwallow:\n"
            "    async def __aenter__(self):\n"
            "        return self\n"
            "    async def __aexit__(self, *a):\n"
            "        return True\n\n"
            "def pytest_collection_modifyitems(items):\n"
            "    for it in items:\n"
            "        cd = contextlib.AsyncContextDecorator()\n"
            "        cd._recreate_cm = lambda: _AsyncSwallow()\n"
            "        it._obj = types.MethodType(cd(it.obj.__func__), it.instance)\n",
            encoding="utf-8",
        )
        self.assertDynamicTamper("AsyncContextDecorator._recreate_cm swap")


class Blocking2LibRootsCallableClassBypass(R12Base):
    """指摘 2: 'call' 経路での lib_roots 任意呼び出し可能クラス（unittest.TestSuite 等）素通り迂回。"""

    def test_unittest_testsuite_wrapper_on_decorated_function_is_detected(self):
        conftest = (
            "import unittest\n\n"
            "def pytest_collection_modifyitems(items):\n"
            "    for it in items:\n"
            "        target = it.obj\n"
            "        s = unittest.TestSuite()\n"
            "        s.run = lambda *a, **k: None\n"
            "        s._ref = target\n"
            "        s.__wrapped__ = target\n"
            "        it._obj = s\n"
        )
        for label, base in {"mock.patch": PATCHED_BASE, "keep": KEEP_BASE}.items():
            with self.subTest(label):
                self.start(base)
                (self.wd / "conftest.py").write_text(conftest, encoding="utf-8")
                self.assertDynamicTamper(f"TestSuite on {label}")

    def test_unittest_testsuite_wrapper_on_testcase_method_is_detected(self):
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
            "import unittest\n\n"
            "def pytest_collection_modifyitems(items):\n"
            "    for it in items:\n"
            "        target = it.cls.test_x\n"
            "        s = unittest.TestSuite()\n"
            "        s.run = lambda *a, **k: None\n"
            "        s._ref = target\n"
            "        s.__wrapped__ = target\n"
            "        it.cls.test_x = s\n"
            "        it._obj = types.MethodType(s, it.instance)\n",
            encoding="utf-8",
        )
        self.assertDynamicTamper("TestSuite on TestCase method")


class Blocking3NonTestFileModificationBypass(R12Base):
    """指摘 3: 非テスト .py ファイルの既存関数・既存クラス改変による迂回。"""

    def test_modified_non_test_function_to_contextmanager_is_detected(self):
        self.start(PATCHED_BASE, extra_files={"app.py": "def helper():\n    return 1\n"})
        (self.wd / "app.py").write_text(
            "import contextlib\n\n"
            "@contextlib.contextmanager\n"
            "def helper():\n"
            "    try:\n"
            "        yield\n"
            "    except AssertionError:\n"
            "        pass\n",
            encoding="utf-8",
        )
        (self.wd / "conftest.py").write_text(
            "import app\n\n"
            "def pytest_collection_modifyitems(items):\n"
            "    for it in items:\n"
            "        it._obj = app.helper()(it.obj)\n",
            encoding="utf-8",
        )
        self.assertDynamicTamper("modified app.py helper -> contextmanager")

    def test_modified_non_test_class_with_suppress_contextdecorator_is_detected(self):
        self.start(PATCHED_BASE, extra_files={"app.py": "class App:\n    pass\n"})
        (self.wd / "app.py").write_text(
            "import contextlib\n\n"
            "class App(contextlib.ContextDecorator):\n"
            "    __enter__ = contextlib.suppress.__enter__\n"
            "    __exit__ = contextlib.suppress.__exit__\n",
            encoding="utf-8",
        )
        (self.wd / "conftest.py").write_text(
            "import app\n\n"
            "def pytest_collection_modifyitems(items):\n"
            "    for it in items:\n"
            "        cm = app.App.__new__(app.App)\n"
            "        cm._exceptions = (AssertionError,)\n"
            "        it._obj = cm(it.obj)\n",
            encoding="utf-8",
        )
        self.assertDynamicTamper("modified app.py class -> ContextDecorator + suppress")

    def test_modified_non_test_callable_class_dunder_call_is_detected(self):
        self.start(
            PATCHED_BASE,
            extra_files={"app.py": "class Runner:\n    def __call__(self, *a, **k):\n        return 1\n"},
        )
        (self.wd / "app.py").write_text(
            "class Runner:\n"
            "    def __call__(self, *a, **k):\n"
            "        return None\n",
            encoding="utf-8",
        )
        (self.wd / "conftest.py").write_text(
            "import app\n\n"
            "def pytest_collection_modifyitems(items):\n"
            "    for it in items:\n"
            "        r = app.Runner()\n"
            "        r._ref = it.obj\n"
            "        r.__wrapped__ = it.obj\n"
            "        it._obj = r\n",
            encoding="utf-8",
        )
        self.assertDynamicTamper("modified app.py Runner.__call__")


class Blocking4MockPatchPatchingsBypass(R12Base):
    """指摘 4: unittest.mock.patch ラッパーの patched.patchings 差し替え迂回。"""

    def test_mock_patch_patchings_swallow_is_detected(self):
        self.start(KEEP_BASE)
        (self.wd / "conftest.py").write_text(
            "from unittest import mock\n\n"
            "class _Swallow:\n"
            "    attribute_name = None\n"
            "    new = object()\n"
            "    def __enter__(self):\n"
            "        return self\n"
            "    def __exit__(self, *a):\n"
            "        return True\n\n"
            "def pytest_collection_modifyitems(items):\n"
            "    for it in items:\n"
            "        p = mock.patch('os.getcwd', new=lambda: '/')(it.obj)\n"
            "        p.patchings = [_Swallow()]\n"
            "        it._obj = p\n",
            encoding="utf-8",
        )
        self.assertDynamicTamper("mock.patch patched.patchings swap")

    def test_mock_patch_additional_patchers_and_decoration_helper_are_detected(self):
        cases = {
            "additional_patchers": (
                "from unittest import mock\n\n"
                "class _Swallow:\n"
                "    def __enter__(self):\n"
                "        return self\n"
                "    def __exit__(self, *a):\n"
                "        return True\n\n"
                "def pytest_collection_modifyitems(items):\n"
                "    for it in items:\n"
                "        patcher = mock.patch('os.getcwd', new=lambda: '/')\n"
                "        patcher.additional_patchers = [_Swallow()]\n"
                "        it._obj = patcher(it.obj)\n"
            ),
            "decoration_helper override": (
                "import contextlib\n"
                "from unittest import mock\n\n"
                "@contextlib.contextmanager\n"
                "def _fake_helper(patched, args, kwargs):\n"
                "    try:\n"
                "        yield (args, kwargs)\n"
                "    except AssertionError:\n"
                "        pass\n\n"
                "def pytest_collection_modifyitems(items):\n"
                "    for it in items:\n"
                "        patcher = mock.patch('os.getcwd', new=lambda: '/')\n"
                "        patcher.decoration_helper = _fake_helper\n"
                "        it._obj = patcher(it.obj)\n"
            ),
        }
        for label, conftest in cases.items():
            with self.subTest(label):
                self.start(KEEP_BASE)
                (self.wd / "conftest.py").write_text(conftest, encoding="utf-8")
                self.assertDynamicTamper(label)


class NonBlockingReproductions(R12Base):
    """非ブロッキング 1〜4 の再現・回帰テスト。"""

    def test_n1_baseline_unittest_skip_and_skipif_true_are_not_flagged(self):
        base = (
            "import unittest\n\n\n"
            "class TestSkip(unittest.TestCase):\n"
            "    @unittest.skip('wip')\n"
            "    def test_skip(self):\n"
            "        self.assertEqual(1, 2)\n\n"
            "    @unittest.skipIf(True, 'wip')\n"
            "    def test_skipif(self):\n"
            "        self.assertEqual(1, 2)\n\n"
            "    @unittest.skipUnless(False, 'wip')\n"
            "    def test_skipunless(self):\n"
            "        self.assertEqual(1, 2)\n"
        )
        self.start(base)
        self.assertEqual(self.static_kinds(base), [])
        self.assertEqual(self.dynamic_report(base), ([], None))

    def test_n2_dual_sync_async_contextdecorator_aexit_tamper_is_detected(self):
        base = (
            "import contextlib\n"
            "import unittest\n"
            "import unittest.mock\n\n\n"
            "class DualCD(contextlib.ContextDecorator, contextlib.AsyncContextDecorator):\n"
            "    def __enter__(self):\n"
            "        return self\n"
            "    def __exit__(self, *a):\n"
            "        return False\n"
            "    async def __aenter__(self):\n"
            "        return self\n"
            "    async def __aexit__(self, *a):\n"
            "        return False\n\n\n"
            "class TestAsync(unittest.IsolatedAsyncioTestCase):\n"
            "    @unittest.mock.patch('os.getcwd')\n"
            "    async def test_x(self, m):\n"
            "        self.assertEqual(1 + 1, 3)\n"
        )
        self.start(base)
        (self.wd / "conftest.py").write_text(
            "import contextlib\n"
            "import types\n\n"
            "async def _swallow_aexit(self, *a):\n"
            "    return True\n\n"
            "def pytest_collection_modifyitems(items):\n"
            "    for it in items:\n"
            "        DualCD = it.module.DualCD\n"
            "        DualCD.__aexit__ = _swallow_aexit\n"
            "        cd = DualCD()\n"
            "        wrapped = contextlib.AsyncContextDecorator.__call__(cd, it.obj.__func__)\n"
            "        it._obj = types.MethodType(wrapped, it.instance)\n",
            encoding="utf-8",
        )
        self.assertDynamicTamper("dual ContextDecorator+AsyncContextDecorator __aexit__ swap")

    def test_n3_slots_only_callable_without_real_ref_is_detected(self):
        base = (
            "import functools\n\n\n"
            "class SlotsWrap:\n"
            "    __slots__ = ('__wrapped__',)\n"
            "    def __call__(self, *a, **k):\n"
            "        return None\n\n\n"
            "def keep(fn):\n"
            "    @functools.wraps(fn)\n"
            "    def inner(*a, **k):\n"
            "        return fn(*a, **k)\n"
            "    return inner\n\n\n"
            "@keep\n"
            "def test_x():\n"
            "    assert 1 + 1 == 3\n"
        )
        self.start(base)
        (self.wd / "conftest.py").write_text(
            "def pytest_collection_modifyitems(items):\n"
            "    for it in items:\n"
            "        w = it.module.SlotsWrap()\n"
            "        w.__wrapped__ = it.obj\n"
            "        it._obj = w\n",
            encoding="utf-8",
        )
        self.assertDynamicTamper("slots-only callable class with only __wrapped__")

    def test_n4_trailing_helper_negative_literal_annotation_is_clean(self):
        self.start(BASE)
        clean = (
            BASE
            + "\nfrom typing import Literal\n\n"
            "def _h(a: Literal[-1, +2] = -1) -> int:\n"
            "    return a\n"
        )
        self.assertEqual(self.static_kinds(clean), [])


class SameClassLoopholeAndLegitimateRegressionTests(R12Base):
    """同系統の抜け道（クロージャ差し替え・クラスモンキーパッチ・動的 skip 等）の遮断と、正当パターンの非誤検出。"""

    def test_same_class_closure_cell_and_class_monkeypatch_bypasses_are_detected(self):
        cases = {
            "ContextDecorator self closure cell swap": (
                PATCHED_BASE,
                "import contextlib\n\n"
                "class _FakeCD:\n"
                "    def _recreate_cm(self):\n"
                "        return contextlib.suppress(AssertionError)\n\n"
                "def pytest_collection_modifyitems(items):\n"
                "    for it in items:\n"
                "        w = contextlib.ContextDecorator()(it.obj)\n"
                "        for cell in w.__closure__:\n"
                "            if isinstance(cell.cell_contents, contextlib.ContextDecorator):\n"
                "                cell.cell_contents = _FakeCD()\n"
                "        it._obj = w\n",
            ),
            "mock.patch self closure cell swap + del patchings": (
                KEEP_BASE,
                "import contextlib\n"
                "from unittest import mock\n\n"
                "class _FakePatch:\n"
                "    @contextlib.contextmanager\n"
                "    def decoration_helper(self, patched, args, kwargs):\n"
                "        try:\n"
                "            yield (args, kwargs)\n"
                "        except AssertionError:\n"
                "            pass\n\n"
                "def pytest_collection_modifyitems(items):\n"
                "    for it in items:\n"
                "        w = mock.patch('os.getcwd', new=lambda: '/')(it.obj)\n"
                "        del w.patchings\n"
                "        for cell in w.__closure__:\n"
                "            if isinstance(cell.cell_contents, mock._patch):\n"
                "                cell.cell_contents = _FakePatch()\n"
                "        it._obj = w\n",
            ),
            "mock._patch subclass in patchings": (
                KEEP_BASE,
                "from unittest import mock\n\n"
                "class _SubPatch(mock._patch):\n"
                "    def __enter__(self):\n"
                "        return None\n"
                "    def __exit__(self, *a):\n"
                "        return True\n\n"
                "def pytest_collection_modifyitems(items):\n"
                "    for it in items:\n"
                "        p = mock.patch('os.getcwd', new=lambda: '/')(it.obj)\n"
                "        sub = _SubPatch.__new__(_SubPatch)\n"
                "        sub.attribute_name = None\n"
                "        sub.new = object()\n"
                "        p.patchings = [sub]\n"
                "        it._obj = p\n",
            ),
            "mock._patch.__exit__ class monkeypatch to suppress.__exit__": (
                KEEP_BASE,
                "import contextlib\n"
                "from unittest import mock\n\n"
                "def pytest_collection_modifyitems(items):\n"
                "    mock._patch.__exit__ = contextlib.suppress.__exit__\n"
                "    for it in items:\n"
                "        p = mock.patch('os.getcwd', new=lambda: '/')(it.obj)\n"
                "        p.patchings[0]._exceptions = (AssertionError,)\n"
                "        it._obj = p\n",
            ),
            "mock.patch.dict instance override": (
                KEEP_BASE,
                "from unittest import mock\n\n"
                "def pytest_collection_modifyitems(items):\n"
                "    for it in items:\n"
                "        pd = mock.patch.dict('os.environ', {'K': '1'})\n"
                "        pd._unpatch_dict = lambda: None\n"
                "        it._obj = pd(it.obj)\n",
            ),
            "dynamic unittest.skip on unskipped decorated test": (
                KEEP_BASE,
                "import unittest\n\n"
                "def pytest_collection_modifyitems(items):\n"
                "    for it in items:\n"
                "        it._obj = unittest.skip('bypass')(it.obj)\n",
            ),
            "dynamic unittest.skip on skipIf(False) test": (
                "import unittest\n\n\n"
                "class TestA(unittest.TestCase):\n"
                "    @unittest.skipIf(False, 'runs normally')\n"
                "    def test_x(self):\n"
                "        self.assertEqual(1 + 1, 3)\n",
                "import types\n"
                "import unittest\n\n"
                "def pytest_collection_modifyitems(items):\n"
                "    for it in items:\n"
                "        it._obj = types.MethodType(unittest.skip('bypass')(it.obj.__func__), it.instance)\n",
            ),
        }
        for label, (base, conftest) in cases.items():
            with self.subTest(label):
                self.start(base)
                (self.wd / "conftest.py").write_text(conftest, encoding="utf-8")
                self.assertDynamicTamper(label)

    def test_known_class_stealing_stdlib_call_and_modified_nontest_nested_decorator_are_detected(self):
        # 1. Existing class stealing unittest.TestSuite.__call__
        self.start(PATCHED_BASE, extra_files={"app.py": "class App:\n    pass\n"})
        (self.wd / "conftest.py").write_text(
            "import unittest\n"
            "import app\n\n"
            "def pytest_collection_modifyitems(items):\n"
            "    app.App.__call__ = unittest.TestSuite.__call__\n"
            "    for it in items:\n"
            "        a = app.App()\n"
            "        a.run = lambda *x, **k: None\n"
            "        a._ref = it.obj\n"
            "        a.__wrapped__ = it.obj\n"
            "        it._obj = a\n",
            encoding="utf-8",
        )
        self.assertDynamicTamper("known class stealing TestSuite.__call__")

        # 2. Existing non-decorator nested function in app.py rewritten into @functools.wraps noop wrapper
        helper_base = (
            "def compute(xs):\n"
            "    def inner(x):\n"
            "        return x + 1\n"
            "    return [inner(x) for x in xs]\n"
        )
        self.start(PATCHED_BASE, extra_files={"app.py": helper_base})
        (self.wd / "app.py").write_text(
            "import functools\n\n"
            "def compute(fn):\n"
            "    @functools.wraps(fn)\n"
            "    def inner(*a, **k):\n"
            "        return None\n"
            "    return inner\n",
            encoding="utf-8",
        )
        (self.wd / "conftest.py").write_text(
            "import app\n\n"
            "def pytest_collection_modifyitems(items):\n"
            "    for it in items:\n"
            "        it._obj = app.compute(it.obj)\n",
            encoding="utf-8",
        )
        self.assertDynamicTamper("modified non-decorator nested fn in app.py")

    def test_legitimate_patterns_remain_clean(self):
        pkg_deco = (
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
            "        return False\n\n\n"
            "class SlotsCallable:\n"
            "    __slots__ = ('__wrapped__', '__fn')\n"
            "    def __init__(self, fn):\n"
            "        self.__fn = fn\n"
            "        self.__wrapped__ = fn\n"
            "    def __call__(self, *a, **k):\n"
            "        return self.__fn(*a, **k)\n"
        )
        base = (
            "import contextlib\n"
            "import os\n"
            "import unittest\n"
            "import unittest.mock\n"
            "import pytest\n"
            "from pkg.deco import BaselineCD, SlotsCallable, keep\n\n\n"
            "@contextlib.contextmanager\n"
            "def noop_cm():\n"
            "    yield\n\n\n"
            "@pytest.mark.parametrize('n', [1, 2])\n"
            "@keep\n"
            "@unittest.mock.patch('os.getcwd')\n"
            "@unittest.mock.patch('os.getpid')\n"
            "@unittest.mock.patch.dict('os.environ', {'AHE_R12': '1'})\n"
            "@unittest.mock.patch.multiple(os, getcwd=lambda: '/', getpid=lambda: 1)\n"
            "@keep\n"
            "@noop_cm()\n"
            "@BaselineCD()\n"
            "@SlotsCallable\n"
            "def test_all_stacked(mpid, mcwd, n):\n"
            "    assert mpid is not None and mcwd is not None and n in (1, 2)\n\n\n"
            "class TestStaticAndCls:\n"
            "    @staticmethod\n"
            "    def test_static():\n"
            "        assert True\n\n"
            "    @classmethod\n"
            "    def test_cls(cls):\n"
            "        assert cls is TestStaticAndCls\n\n\n"
            "class TestUnit(unittest.TestCase):\n"
            "    @unittest.skip('wip')\n"
            "    def test_skipped(self):\n"
            "        self.fail('skipped')\n\n"
            "    @unittest.skipIf(False, 'runs')\n"
            "    @unittest.mock.patch('os.getcwd')\n"
            "    def test_runs(self, m):\n"
            "        self.assertIsNotNone(m)\n\n\n"
            "class TestAsync(unittest.IsolatedAsyncioTestCase):\n"
            "    @unittest.mock.patch('os.getcwd')\n"
            "    async def test_async(self, m):\n"
            "        self.assertIsNotNone(m)\n"
        )
        self.start(base, extra_files={"pkg/__init__.py": "", "pkg/deco.py": pkg_deco})
        # Non-test file edited elsewhere (shifting line numbers of untouched decorators) must still pass!
        (self.wd / "pkg" / "deco.py").write_text("# comment shifting lines\n\n" + pkg_deco, encoding="utf-8")
        self.assertEqual(self.static_kinds(base), [])
        self.assertEqual(self.dynamic_report(base), ([], None))

    def test_legitimate_cm_subclass_inheriting_stdlib_cm_is_clean(self):
        """r12 回帰: stdlib CM を多重継承した正当な ContextDecorator を誤検出しない。"""
        base = (
            "import contextlib\n\n\n"
            "class MyCM(contextlib.nullcontext, contextlib.ContextDecorator):\n"
            "    pass\n\n\n"
            "@MyCM()\n"
            "def test_x():\n"
            "    assert 1 + 1 == 3\n"
        )
        self.start(base)
        self.assertEqual(self.static_kinds(base), [])
        self.assertEqual(self.dynamic_report(base), ([], None))

    def test_legitimate_inherited_private_slots_wrapper_is_clean(self):
        """継承先が同名のプライベート __slots__ を持っても、正当なラッパーを誤検出しない。"""
        base = (
            "import functools\n\n\n"
            "class BaseWrap:\n"
            "    __slots__ = ('__wrapped__', '__fn')\n"
            "    def __init__(self, fn):\n"
            "        self.__fn = fn\n"
            "        self.__wrapped__ = fn\n"
            "    def __call__(self, *a, **k):\n"
            "        return self.__fn(*a, **k)\n\n\n"
            "class SubWrap(BaseWrap):\n"
            "    __slots__ = ('__fn',)\n\n\n"
            "def keep(fn):\n"
            "    @functools.wraps(fn)\n"
            "    def inner(*a, **k):\n"
            "        return fn(*a, **k)\n"
            "    return inner\n\n\n"
            "@keep\n"
            "@SubWrap\n"
            "def test_x():\n"
            "    assert 1 + 1 == 3\n"
        )
        self.start(base)
        self.assertEqual(self.static_kinds(base), [])
        self.assertEqual(self.dynamic_report(base), ([], None))


if __name__ == "__main__":
    unittest.main()
