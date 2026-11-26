"""Issue #6（PR #5 で後回しにした cheat-proof #1–#3）の再現・回帰テスト。

設計は DESIGN-I6.md。現行 main（v0.10.2）では exploit 1〜9 は
rebound_report == ([], None) で素通りする想定。実装後は test_body_modified
で検出する。正当パターンは実装前後とも誤検出しない。
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
BASELINE_CD = (
    "import contextlib\n\n\n"
    "class BaselineCD(contextlib.ContextDecorator):\n"
    "    def __enter__(self):\n"
    "        return self\n"
    "    def __exit__(self, *a):\n"
    "        return False\n\n\n"
    "@BaselineCD()\n"
    "def test_x():\n"
    "    assert 1 + 1 == 3\n"
)
ASYNC_PATCHED = (
    "import unittest\n"
    "import unittest.mock\n\n\n"
    "class TestAsync(unittest.IsolatedAsyncioTestCase):\n"
    "    @unittest.mock.patch('os.getcwd')\n"
    "    async def test_x(self, m):\n"
    "        self.assertEqual(1 + 1, 3)\n"
)
SKIPIF_BASE = (
    "import sys\n"
    "import unittest\n\n\n"
    "class TestX(unittest.TestCase):\n"
    "    @unittest.skipIf(sys.platform == 'never_os', 'other os')\n"
    "    def test_x(self):\n"
    "        self.assertEqual(1 + 1, 3)\n"
)
SKIPUNLESS_BASE = (
    "import sys\n"
    "import unittest\n\n\n"
    "class TestX(unittest.TestCase):\n"
    "    @unittest.skipUnless(sys.platform != 'never_os', 'runs')\n"
    "    def test_x(self):\n"
    "        self.assertEqual(1 + 1, 3)\n"
)
PLAIN_CASE = (
    "import unittest\n\n\n"
    "class TestX(unittest.TestCase):\n"
    "    def test_x(self):\n"
    "        self.assertEqual(1 + 1, 3)\n"
)
APP_PATCHED = "import app\n" + PATCHED_BASE


class I6Base(unittest.TestCase):
    def setUp(self):
        self.wd = Path(tempfile.mkdtemp(prefix="i6-"))
        self.tmp = Path(tempfile.mkdtemp(prefix="i6-tmp-"))
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

    def assertDynamicClean(self, label: str = ""):
        found, reason = self.dynamic_report()
        self.assertIsNone(reason, (label, reason))
        self.assertEqual(found, [], (label, found))

    def write_conftest(self, body: str) -> None:
        (self.wd / "conftest.py").write_text(body, encoding="utf-8")


class Exploit1ContextDecoratorExitStack(I6Base):
    """#1 同期 ContextDecorator への ExitStack.__exit__ 差し替え。"""

    def test_bare_context_decorator_exitstack_swap_is_detected(self):
        self.start(KEEP_BASE)
        self.write_conftest(
            "import contextlib\n"
            "from collections import deque\n\n"
            "def pytest_collection_modifyitems(items):\n"
            "    contextlib.ContextDecorator.__enter__ = contextlib.ContextDecorator._recreate_cm\n"
            "    contextlib.ContextDecorator.__exit__ = contextlib.ExitStack.__exit__\n"
            "    for it in items:\n"
            "        cd = contextlib.ContextDecorator()\n"
            "        cd._exit_callbacks = deque([(True, lambda *a: True)])\n"
            "        it._obj = cd(it.obj)\n"
        )
        self.assertDynamicTamper("ContextDecorator ExitStack.__exit__ swap")


class Exploit2AsyncContextDecorator(I6Base):
    """#1 非同期 AsyncContextDecorator への AsyncExitStack 差し替え。"""

    def test_async_context_decorator_asyncexitstack_swap_is_detected(self):
        self.start(ASYNC_PATCHED)
        self.write_conftest(
            "import contextlib\n"
            "import types\n"
            "from collections import deque\n\n"
            "def pytest_collection_modifyitems(items):\n"
            "    contextlib.AsyncContextDecorator.__aenter__ = contextlib.AsyncExitStack.__aenter__\n"
            "    contextlib.AsyncContextDecorator.__aexit__ = contextlib.AsyncExitStack.__aexit__\n"
            "    for it in items:\n"
            "        cd = contextlib.AsyncContextDecorator()\n"
            "        cd._exit_callbacks = deque([(True, lambda *a: True)])\n"
            "        it._obj = types.MethodType(cd(it.obj.__func__), it.instance)\n"
        )
        self.assertDynamicTamper("AsyncContextDecorator AsyncExitStack swap")


class Exploit3BaselineCDExitStack(I6Base):
    """#1 基準 ContextDecorator サブクラスへの ExitStack.__exit__ 差し替え。"""

    def test_baseline_cd_exitstack_swap_is_detected(self):
        self.start(BASELINE_CD)
        self.write_conftest(
            "import contextlib\n"
            "from collections import deque\n\n"
            "def pytest_collection_modifyitems(items):\n"
            "    for it in items:\n"
            "        it.module.BaselineCD.__exit__ = contextlib.ExitStack.__exit__\n"
            "        cd = it.module.BaselineCD()\n"
            "        cd._exit_callbacks = deque([(True, lambda *a: True)])\n"
            "        it._obj = cd(it.obj)\n"
        )
        self.assertDynamicTamper("BaselineCD.__exit__ = ExitStack.__exit__")


class Exploit4AppPyExitStack(I6Base):
    """#2a app.py による contextlib.ExitStack.__exit__ 改変（conftest.py 不要）。"""

    def test_app_py_exitstack_swap_is_detected(self):
        self.start(
            APP_PATCHED,
            extra_files={"app.py": "import contextlib\n\ncontextlib.ExitStack.__exit__ = lambda self, *a: True\n"},
        )
        self.assertDynamicTamper("app.py ExitStack.__exit__ swap")


class Exploit5InternalCmMutation(I6Base):
    """#2a 内部 CM・モジュール・関数 __code__ 改変の亜種。"""

    def test_generator_context_manager_exit_swap_is_detected(self):
        self.start(
            APP_PATCHED,
            extra_files={
                "app.py": (
                    "import contextlib\n\n"
                    "contextlib._GeneratorContextManager.__exit__ = lambda self, *a: True\n"
                )
            },
        )
        self.assertDynamicTamper("_GeneratorContextManager.__exit__ swap")

    def test_unittest_mock_contextlib_module_swap_is_detected(self):
        self.start(
            APP_PATCHED,
            extra_files={
                "app.py": (
                    "import types\n"
                    "import unittest.mock\n\n"
                    "unittest.mock.contextlib = types.ModuleType('evil_contextlib')\n"
                )
            },
        )
        self.assertDynamicTamper("unittest.mock.contextlib module swap")

    def test_exitstack_exit_code_inplace_swap_is_detected(self):
        self.start(
            APP_PATCHED,
            extra_files={
                "app.py": (
                    "import contextlib\n\n"
                    "contextlib.ExitStack.__exit__.__code__ = (lambda self, *a: True).__code__\n"
                )
            },
        )
        self.assertDynamicTamper("ExitStack.__exit__.__code__ inplace swap")


class Exploit6PatchCallbacks(I6Base):
    """#2b _patch コールバック注入・危険ダンダーパッチ。"""

    def test_patch_getter_callback_is_detected(self):
        self.start(PATCHED_BASE)
        self.write_conftest(
            "def pytest_collection_modifyitems(items):\n"
            "    def _noop(*a, **k):\n"
            "        return None\n"
            "    for it in items:\n"
            "        orig = it.obj\n"
            "        p = orig.patchings[0]\n"
            "        def _evil_getter(_orig=orig, _noop=_noop):\n"
            "            _orig.__code__ = _noop.__code__\n"
            "            import os\n"
            "            return os\n"
            "        p.getter = _evil_getter\n"
        )
        self.assertDynamicTamper("patch getter callback")

    def test_patch_new_callable_factory_is_detected(self):
        self.start(PATCHED_BASE)
        self.write_conftest(
            "import unittest.mock as mock\n\n"
            "def pytest_collection_modifyitems(items):\n"
            "    def _noop(*a, **k):\n"
            "        return None\n"
            "    for it in items:\n"
            "        orig = it.obj\n"
            "        p = orig.patchings[0]\n"
            "        def _evil_factory(*a, _orig=orig, _noop=_noop, **k):\n"
            "            _orig.__code__ = _noop.__code__\n"
            "            return mock.Mock()\n"
            "        p.new_callable = _evil_factory\n"
        )
        self.assertDynamicTamper("patch new_callable factory")

    def test_patch_object_dunder_code_is_detected(self):
        self.start(KEEP_BASE)
        self.write_conftest(
            "import unittest.mock as mock\n\n"
            "def pytest_collection_modifyitems(items):\n"
            "    def _noop(*a, **k):\n"
            "        return None\n"
            "    for it in items:\n"
            "        orig = it.obj\n"
            "        it._obj = mock.patch.object(orig, '__code__', _noop.__code__)(orig)\n"
        )
        self.assertDynamicTamper("patch.object orig.__code__")


class Exploit7PatchDictInDict(I6Base):
    """#2b _patch_dict 悪性 in_dict コールバック注入。"""

    def test_evil_dict_in_dict_is_detected(self):
        self.start(KEEP_BASE)
        self.write_conftest(
            "import unittest.mock as mock\n\n"
            "def pytest_collection_modifyitems(items):\n"
            "    def _noop(*a, **k):\n"
            "        return None\n"
            "    for it in items:\n"
            "        orig = it.obj\n"
            "        class _EvilDict(dict):\n"
            "            def copy(self, _orig=orig, _noop=_noop):\n"
            "                _orig.__code__ = _noop.__code__\n"
            "                return dict(self)\n"
            "        it._obj = mock.patch.dict(_EvilDict({'k': 1}), {})(it.obj)\n"
        )
        self.assertDynamicTamper("patch.dict evil in_dict")


class Exploit8DynamicUnittestSkip(I6Base):
    """#3 非リテラル skipIf / skipUnless 付き実行中テストへの動的 unittest.skip 被せ。"""

    def test_dynamic_skip_on_skipif_never_os_is_detected(self):
        self.start(SKIPIF_BASE)
        self.write_conftest(
            "import types\n"
            "import unittest\n\n"
            "def pytest_collection_modifyitems(items):\n"
            "    for it in items:\n"
            "        fn = getattr(unittest, 'sk' + 'ip')('other os')(it.obj.__func__)\n"
            "        it._obj = types.MethodType(fn, it.instance)\n"
        )
        self.assertDynamicTamper("dynamic skip on skipIf(never_os)")

    def test_dynamic_skip_on_skipunless_runs_is_detected(self):
        self.start(SKIPUNLESS_BASE)
        self.write_conftest(
            "import types\n"
            "import unittest\n\n"
            "def pytest_collection_modifyitems(items):\n"
            "    for it in items:\n"
            "        fn = getattr(unittest, 'sk' + 'ip')('other os')(it.obj.__func__)\n"
            "        it._obj = types.MethodType(fn, it.instance)\n"
        )
        self.assertDynamicTamper("dynamic skip on skipUnless(runs)")


class Exploit9UnittestSkipAttribute(I6Base):
    """#3 __unittest_skip__ 直接属性注入。"""

    def test_method_unittest_skip_attr_is_detected(self):
        self.start(PLAIN_CASE)
        self.write_conftest(
            "def pytest_collection_modifyitems(items):\n"
            "    for it in items:\n"
            "        setattr(it.cls.test_x, '__unittest_' + 'skip__', True)\n"
        )
        self.assertDynamicTamper("method __unittest_skip__ attr")

    def test_class_unittest_skip_attr_is_detected(self):
        self.start(PLAIN_CASE)
        self.write_conftest(
            "def pytest_collection_modifyitems(items):\n"
            "    for it in items:\n"
            "        setattr(it.cls, '__unittest_' + 'skip__', True)\n"
        )
        self.assertDynamicTamper("class __unittest_skip__ attr")


class LegitimatePatterns(I6Base):
    """正当パターンの非誤検出回帰。実装前の HEAD でも通ること。"""

    def _assert_clean(self, base: str, label: str, extra_files: dict[str, str] | None = None):
        self.start(base, extra_files=extra_files)
        self.assertEqual(self.static_kinds(base), [], label)
        self.assertEqual(self.dynamic_report(base), ([], None), label)

    def test_parametrize_is_clean(self):
        base = (
            "import pytest\n\n\n"
            "@pytest.mark.parametrize('n', [1, 2])\n"
            "def test_x(n):\n"
            "    assert n in (1, 2)\n"
        )
        self._assert_clean(base, "parametrize")

    def test_mock_patch_variants_are_clean(self):
        base = (
            "import io\n"
            "import os\n"
            "import unittest.mock as mock\n\n\n"
            "@mock.patch('os.getcwd')\n"
            "def test_patch(m):\n"
            "    assert m is not None\n\n\n"
            "@mock.patch.object(os, 'getcwd')\n"
            "def test_patch_object(m):\n"
            "    assert m is not None\n\n\n"
            "@mock.patch.multiple(os, getcwd=lambda: '/', getpid=lambda: 1)\n"
            "def test_patch_multiple():\n"
            "    assert os.getcwd() == '/' and os.getpid() == 1\n\n\n"
            "@mock.patch.dict(os.environ, {'AHE_I6': '1'})\n"
            "def test_patch_dict_environ_obj():\n"
            "    assert os.environ.get('AHE_I6') == '1'\n\n\n"
            "@mock.patch.dict('os.environ', {'AHE_I6B': '1'})\n"
            "def test_patch_dict_environ_str():\n"
            "    assert os.environ.get('AHE_I6B') == '1'\n\n\n"
            "@mock.patch('builtins.open', new_callable=mock.mock_open, read_data='x')\n"
            "def test_mock_open(m):\n"
            "    assert m is not None\n\n\n"
            "@mock.patch('sys.stdout', new_callable=io.StringIO)\n"
            "def test_stringio(m):\n"
            "    assert m is not None\n"
        )
        self._assert_clean(base, "mock.patch variants")

    def test_stacked_decorators_and_project_wrappers_are_clean(self):
        base = (
            "import functools\n"
            "import unittest.mock as mock\n"
            "import pytest\n\n\n"
            "def keep(fn):\n"
            "    @functools.wraps(fn)\n"
            "    def inner(*a, **k):\n"
            "        return fn(*a, **k)\n"
            "    return inner\n\n\n"
            "class CallableWrap:\n"
            "    def __init__(self, fn):\n"
            "        self.__fn = fn\n"
            "        self.__wrapped__ = fn\n"
            "    def __call__(self, *a, **k):\n"
            "        return self.__fn(*a, **k)\n\n\n"
            "@pytest.mark.parametrize('n', [1])\n"
            "@keep\n"
            "@mock.patch('os.getcwd')\n"
            "@CallableWrap\n"
            "def test_x(m, n):\n"
            "    assert m is not None and n == 1\n"
        )
        self._assert_clean(base, "stacked decorators")

    def test_unittest_case_and_async_are_clean(self):
        base = (
            "import unittest\n"
            "import unittest.mock\n\n\n"
            "class TestUnit(unittest.TestCase):\n"
            "    maxDiff = None\n"
            "    def test_x(self):\n"
            "        self.assertEqual(1 + 1, 2)\n\n\n"
            "class TestAsync(unittest.IsolatedAsyncioTestCase):\n"
            "    @unittest.mock.patch('os.getcwd')\n"
            "    async def test_x(self, m):\n"
            "        self.assertIsNotNone(m)\n"
        )
        self._assert_clean(base, "TestCase / IsolatedAsyncioTestCase")

    def test_project_cm_subclasses_are_clean(self):
        base = (
            "import contextlib\n"
            "import tempfile\n"
            "from contextlib import nullcontext as NC\n\n\n"
            "class MyCM(contextlib.nullcontext, contextlib.ContextDecorator):\n"
            "    pass\n\n\n"
            "class MyTemp(tempfile.TemporaryDirectory, contextlib.ContextDecorator):\n"
            "    pass\n\n\n"
            "class MyAlias(NC, contextlib.ContextDecorator):\n"
            "    pass\n\n\n"
            "@MyCM()\n"
            "def test_cm():\n"
            "    assert True\n\n\n"
            "@MyTemp()\n"
            "def test_temp():\n"
            "    assert True\n\n\n"
            "@MyAlias()\n"
            "def test_alias():\n"
            "    assert True\n"
        )
        self._assert_clean(base, "MyCM / MyTemp / alias NC")

    def test_literal_and_running_skips_are_clean(self):
        base = (
            "import sys\n"
            "import unittest\n\n\n"
            "class TestMethodSkips(unittest.TestCase):\n"
            "    @unittest.skip('wip')\n"
            "    def test_skip(self):\n"
            "        self.fail('skipped')\n\n"
            "    @unittest.skipIf(True, 'wip')\n"
            "    def test_skipif_true(self):\n"
            "        self.fail('skipped')\n\n"
            "    @unittest.skipUnless(False, 'wip')\n"
            "    def test_skipunless_false(self):\n"
            "        self.fail('skipped')\n\n"
            "    @unittest.skipIf(sys.platform == 'never_os', 'other os')\n"
            "    def test_runs(self):\n"
            "        self.assertEqual(1 + 1, 2)\n\n\n"
            "@unittest.skip('wip')\n"
            "class TestClassSkip(unittest.TestCase):\n"
            "    def test_x(self):\n"
            "        self.fail('skipped')\n\n\n"
            "@unittest.skipIf(True, 'wip')\n"
            "class TestClassSkipIf(unittest.TestCase):\n"
            "    def test_x(self):\n"
            "        self.fail('skipped')\n\n\n"
            "@unittest.skipUnless(False, 'wip')\n"
            "class TestClassSkipUnless(unittest.TestCase):\n"
            "    def test_x(self):\n"
            "        self.fail('skipped')\n"
        )
        self._assert_clean(base, "literal and running skips")


class ReviewAstraPR11(I6Base):
    """PR #11 の GPT-6.2 Astra クロスレビュー（CHANGES）の指摘ごとの回帰テスト。"""

    def test_project_cm_method_from_other_baseline_class_is_detected(self):
        # MAJOR 1: 基準 CM の __exit__ を、別の基準クラスの（例外を握りつぶす）メソッドに付け替える
        self.start(
            "import contextlib\n\n\n"
            "class BaselineCD(contextlib.ContextDecorator):\n"
            "    def __enter__(self):\n"
            "        return self\n"
            "    def __exit__(self, *a):\n"
            "        return False\n\n\n"
            "class Other:\n"
            "    def swallow(self, *a):\n"
            "        return True\n\n\n"
            "@BaselineCD()\n"
            "def test_x():\n"
            "    assert 1 + 1 == 3\n"
        )
        self.write_conftest(
            "def pytest_collection_modifyitems(items):\n"
            "    for it in items:\n"
            "        it.module.BaselineCD.__exit__ = it.module.Other.swallow\n"
        )
        self.assertDynamicTamper("BaselineCD.__exit__ = Other.swallow")

    def test_mock_patch_helper_rebinding_is_detected(self):
        # MAJOR 2: import 時に mock.patch.object / .dict / .multiple を差し替える
        for attr in ("object", "dict", "multiple"):
            with self.subTest(attr=attr):
                self.start(
                    APP_PATCHED,
                    extra_files={"app.py": (
                        "import unittest.mock\n\n"
                        f"_orig = unittest.mock.patch.{attr}\n"
                        f"unittest.mock.patch.{attr} = lambda *a, **k: _orig(*a, **k)\n"
                    )},
                )
                self.assertDynamicTamper(f"mock.patch.{attr} rebinding")

    def test_string_patch_dict_target_to_evil_mapping_is_detected(self):
        # MAJOR 3: 文字列ターゲットの patch.dict も、解決後のマッピングの出自を照合する
        self.start(KEEP_BASE)
        (self.wd / "evilmod.py").write_text(
            "class _EvilDict(dict):\n"
            "    def copy(self):\n"
            "        return dict(self)\n\n\n"
            "EVIL = _EvilDict({'k': 1})\n",
            encoding="utf-8",
        )
        self.write_conftest(
            "import unittest.mock as mock\n\n"
            "def pytest_collection_modifyitems(items):\n"
            "    for it in items:\n"
            "        it._obj = mock.patch.dict('evilmod.EVIL', {})(it.obj)\n"
        )
        self.assertDynamicTamper("patch.dict('evilmod.EVIL')")

    def test_patch_spec_instance_of_baseline_class_is_clean(self):
        # MAJOR 4: spec に基準クラスのインスタンスを渡す普通の書き方は誤検出しない
        base = (
            "import unittest.mock as mock\n\n\n"
            "class Client:\n"
            "    def get(self):\n"
            "        return 1\n\n\n"
            "@mock.patch('os.getcwd', spec=Client())\n"
            "def test_x(m):\n"
            "    assert m is not None\n"
        )
        self.start(base)
        self.assertEqual(self.static_kinds(base), [])
        self.assertEqual(self.dynamic_report(base), ([], None))

    def test_patch_spec_instance_of_foreign_class_is_detected(self):
        # MAJOR 4 の裏: 基準にないクラスのインスタンスを spec に渡すのは従来どおり拒否
        self.start(KEEP_BASE)
        self.write_conftest(
            "import unittest.mock as mock\n\n"
            "class Evil:\n"
            "    pass\n\n"
            "def pytest_collection_modifyitems(items):\n"
            "    for it in items:\n"
            "        it._obj = mock.patch('os.getcwd', spec=Evil())(it.obj)\n"
        )
        self.assertDynamicTamper("spec=<foreign instance>")

    def test_subclass_unittest_skip_false_override_is_not_allow_listed(self):
        # MAJOR 5: 基底クラスの skip を __unittest_skip__ = False で打ち消したサブクラスのテストは実行される
        self.start(
            "import unittest\n\n\n"
            "@unittest.skip('base')\n"
            "class Base(unittest.TestCase):\n"
            "    pass\n\n\n"
            "class TestChild(Base):\n"
            "    __unittest_skip__ = False\n\n"
            "    def test_x(self):\n"
            "        self.assertEqual(1 + 1, 3)\n"
        )
        self.write_conftest(
            "def pytest_collection_modifyitems(items):\n"
            "    for it in items:\n"
            "        setattr(it.cls.test_x, '__unittest_' + 'skip__', True)\n"
        )
        self.assertDynamicTamper("skip attr on __unittest_skip__ = False subclass")

    def test_tamper_on_earlier_parametrized_case_is_detected(self):
        # MAJOR 6: 同じ node id の parametrize の最初のケースだけ差し替える
        self.start(
            "import pytest\n\n\n"
            "@pytest.mark.parametrize('n', [1, 2])\n"
            "def test_x(n):\n"
            "    assert n == 3\n"
        )
        self.write_conftest(
            "def _fake(n):\n"
            "    return None\n\n"
            "def pytest_collection_modifyitems(items):\n"
            "    items[0]._obj = _fake\n"
        )
        self.assertDynamicTamper("first parametrize case rebound")


class ReviewAstraPR11R2(I6Base):
    """PR #11 の GPT-6.2 Astra 再レビュー（round 2、CHANGES）の指摘ごとの回帰テスト。"""

    def _assert_clean(self, base: str, label: str, extra_files: dict[str, str] | None = None):
        self.start(base, extra_files=extra_files)
        self.assertEqual(self.static_kinds(base), [], label)
        self.assertEqual(self.dynamic_report(base), ([], None), label)

    def test_default_argument_lambda_on_method_line_is_not_the_method(self):
        # #1: 定義行にある既定引数の lambda（例外を握りつぶす）を Session.__exit__ に付け替える
        self.start(
            "import contextlib\n\n\n"
            "class Session(contextlib.ContextDecorator):\n"
            "    def __enter__(self):\n"
            "        return self\n"
            "    def __exit__(self, *a, _hook=lambda *a: True):\n"
            "        return False\n\n\n"
            "@Session()\n"
            "def test_x():\n"
            "    assert 1 + 1 == 3\n"
        )
        self.write_conftest(
            "def pytest_collection_modifyitems(items):\n"
            "    for it in items:\n"
            "        cls = it.module.Session\n"
            "        cls.__exit__ = cls.__exit__.__kwdefaults__['_hook']\n"
        )
        self.assertDynamicTamper("Session.__exit__ = <default-arg lambda>")

    def test_unittest_skip_follows_the_mro_with_multiple_inheritance(self):
        # #5: TestChild(Mixin, Base) は MRO で Mixin の False が先に見つかり実行される。skip の注入は改ざん
        self.start(
            "import unittest\n\n\n"
            "@unittest.skip('base')\n"
            "class Base(unittest.TestCase):\n"
            "    pass\n\n\n"
            "class Mixin:\n"
            "    __unittest_skip__ = False\n\n\n"
            "class TestChild(Mixin, Base):\n"
            "    def test_x(self):\n"
            "        self.assertEqual(1 + 1, 3)\n"
        )
        self.write_conftest(
            "def pytest_collection_modifyitems(items):\n"
            "    for it in items:\n"
            "        setattr(it.cls.test_x, '__unittest_' + 'skip__', True)\n"
        )
        self.assertDynamicTamper("skip attr on MRO-running subclass")

    def test_unittest_skip_inherited_through_the_mro_is_clean(self):
        # #5 の裏: 基底のどれかの skip が MRO で先に見つかる多重継承はスキップされる（誤検出しない）
        self._assert_clean(
            "import unittest\n\n\n"
            "@unittest.skip('base')\n"
            "class Base(unittest.TestCase):\n"
            "    pass\n\n\n"
            "class Mixin:\n"
            "    pass\n\n\n"
            "class Runs:\n"
            "    __unittest_skip__ = False\n\n\n"
            "class TestChild(Mixin, Base, Runs):\n"
            "    def test_x(self):\n"
            "        self.fail('skipped')\n\n\n"
            "class Middle(Base):\n"
            "    pass\n\n\n"
            "class TestGrandChild(Mixin, Middle):\n"
            "    def test_x(self):\n"
            "        self.fail('skipped')\n",
            "MRO-inherited skip",
        )

    def test_subclass_repeating_or_annotating_unittest_skip_is_clean(self):
        # 新 MAJOR（~1506）: 基底の skip を `= True` で繰り返す・注釈だけのサブクラスは unittest でもスキップされる
        self._assert_clean(
            "import unittest\n\n\n"
            "@unittest.skip('base')\n"
            "class Base(unittest.TestCase):\n"
            "    pass\n\n\n"
            "class TestRepeat(Base):\n"
            "    __unittest_skip__ = True\n\n"
            "    def test_x(self):\n"
            "        self.fail('skipped')\n\n\n"
            "class TestAnnotated(Base):\n"
            "    __unittest_skip__: bool\n\n"
            "    def test_x(self):\n"
            "        self.fail('skipped')\n",
            "repeated / annotated __unittest_skip__",
        )

    def test_class_body_method_alias_in_baseline_cm_is_clean(self):
        # 新 MAJOR（~1933）: 変更のない基準クラスの `__exit__ = close` の別名は正当
        self._assert_clean(
            "import contextlib\n\n\n"
            "class Session(contextlib.ContextDecorator):\n"
            "    def __enter__(self):\n"
            "        return self\n"
            "    def close(self, *a):\n"
            "        return False\n"
            "    __exit__ = close\n\n\n"
            "@Session()\n"
            "def test_x():\n"
            "    assert 1 + 1 == 2\n",
            "__exit__ = close",
        )

    def test_runtime_alias_not_in_baseline_is_detected(self):
        # 新 MAJOR（~1933）の裏: 基準にない同じクラス内の別名（例外を握りつぶすメソッド）を実行時に付けるのは改ざん
        self.start(
            "import contextlib\n\n\n"
            "class Session(contextlib.ContextDecorator):\n"
            "    def __enter__(self):\n"
            "        return self\n"
            "    def __exit__(self, *a):\n"
            "        return False\n"
            "    def swallow(self, *a):\n"
            "        return True\n\n\n"
            "@Session()\n"
            "def test_x():\n"
            "    assert 1 + 1 == 3\n"
        )
        self.write_conftest(
            "def pytest_collection_modifyitems(items):\n"
            "    for it in items:\n"
            "        it.module.Session.__exit__ = it.module.Session.swallow\n"
        )
        self.assertDynamicTamper("Session.__exit__ = Session.swallow")

    def test_mock_patch_test_prefix_configuration_is_clean(self):
        # 新 MAJOR（~980）: mock.patch.TEST_PREFIX の設定は文書化された使い方
        self._assert_clean(
            "import unittest\n"
            "from unittest import mock\n\n"
            "mock.patch.TEST_PREFIX = 'test_'\n\n\n"
            "@mock.patch('os.getcwd', return_value='/x')\n"
            "class TestX(unittest.TestCase):\n"
            "    def test_x(self, m):\n"
            "        import os\n"
            "        self.assertEqual(os.getcwd(), '/x')\n",
            "patch.TEST_PREFIX",
        )

    def test_patch_dict_target_created_by_fixture_is_clean(self):
        # 新 MAJOR（~1246）: fixture が用意する文字列ターゲットは収集時には解決できない（照合を飛ばす）
        self._assert_clean(
            "import sys\n"
            "import types\n"
            "from unittest import mock\n\n"
            "import pytest\n\n\n"
            "@pytest.fixture(autouse=True)\n"
            "def _cfg():\n"
            "    sys.modules['i6_cfg'] = types.SimpleNamespace(SETTINGS={})\n"
            "    yield\n"
            "    sys.modules.pop('i6_cfg', None)\n\n\n"
            "@mock.patch.dict('i6_cfg.SETTINGS', {'k': 1})\n"
            "def test_x():\n"
            "    assert sys.modules['i6_cfg'].SETTINGS == {'k': 1}\n",
            "fixture-provided patch.dict target",
        )


INJECT_CLASS_SKIP = (
    "def pytest_collection_modifyitems(items):\n"
    "    for it in items:\n"
    "        setattr(it.cls, '__unittest_' + 'skip__', True)\n"
)
INJECT_METHOD_SKIP = (
    "def pytest_collection_modifyitems(items):\n"
    "    for it in items:\n"
    "        setattr(it.cls.test_x, '__unittest_' + 'skip__', True)\n"
)


class ReviewAstraPR11R3(I6Base):
    """PR #11 の GPT-6.2 Astra 再レビュー（round 3、CHANGES）: クラスの __unittest_skip__ は静的に MRO をまねても unittest と一致しない。
    収集で実際に import したクラスの値を、基準のソースを同じように収集した結果と比べる。"""

    def _assert_injections_detected(self, base: str, label: str):
        self.start(base)
        for inject in (INJECT_CLASS_SKIP, INJECT_METHOD_SKIP):
            self.write_conftest(inject)
            self.assertDynamicTamper(f"{label} / {inject.splitlines()[-1].strip()}")

    def test_same_file_generic_mixin_clearing_skip_is_detected(self):
        # #5 の残り: RunMixin[int] は静的には名前で引けず、Mixin の False を見落としていた。unittest は TestChild を実行する
        self._assert_injections_detected(
            "import typing\n"
            "import unittest\n\n"
            "T = typing.TypeVar('T')\n\n\n"
            "@unittest.skip('base')\n"
            "class Base(unittest.TestCase):\n"
            "    pass\n\n\n"
            "class RunMixin(typing.Generic[T]):\n"
            "    __unittest_skip__ = False\n\n\n"
            "class TestChild(RunMixin[int], Base):\n"
            "    def test_x(self):\n"
            "        self.assertEqual(1 + 1, 3)\n",
            "generic mixin clears skip",
        )

    def test_init_subclass_clearing_skip_is_detected(self):
        # 新 MAJOR: 基底は skip、__init_subclass__ がサブクラスの skip を外す。unittest は TestChild を実行する
        self._assert_injections_detected(
            "import unittest\n\n\n"
            "@unittest.skip('abstract')\n"
            "class Base(unittest.TestCase):\n"
            "    def __init_subclass__(cls, **kw):\n"
            "        super().__init_subclass__(**kw)\n"
            "        cls.__unittest_skip__ = False\n\n\n"
            "class TestChild(Base):\n"
            "    def test_x(self):\n"
            "        self.assertEqual(1 + 1, 3)\n",
            "__init_subclass__ clears skip",
        )

    def test_platform_conditional_skip_clear_is_clean(self):
        # 新 MAJOR: ほかの OS でだけ継承した skip を外すサブクラスは、この OS ではスキップされる（誤検出しない）。
        # 'never_os' は実在しない OS の代わり（どの OS で動かしても「ほかの OS」になる）
        base = (
            "import sys\n"
            "import unittest\n\n\n"
            "@unittest.skip('base')\n"
            "class Base(unittest.TestCase):\n"
            "    pass\n\n\n"
            "class TestChild(Base):\n"
            "    if sys.platform == 'never_os':\n"
            "        __unittest_skip__ = False\n\n"
            "    def test_x(self):\n"
            "        self.fail('skipped')\n"
        )
        self.start(base)
        self.assertEqual(self.static_kinds(base), [], "platform-conditional clear")
        self.assertEqual(self.dynamic_report(base), ([], None), "platform-conditional clear")

    def test_class_skip_without_baseline_collection_fails_closed(self):
        # 基準の写しだけでは収集できない（写さないデータファイルを import 時に読む）とき、クラスの skip は基準になかったものとして扱う
        base = (
            "import pathlib\n"
            "import unittest\n\n"
            "DATA = (pathlib.Path(__file__).parents[1] / 'data.bin').read_text()\n\n\n"
            "@unittest.skip('wip')\n"
            "class TestX(unittest.TestCase):\n"
            "    def test_x(self):\n"
            "        self.fail('skipped')\n"
        )
        self.start(base, extra_files={"data.bin": "x"})
        self.assertDynamicTamper("class skip with uncollectable baseline")


if __name__ == "__main__":
    unittest.main()
