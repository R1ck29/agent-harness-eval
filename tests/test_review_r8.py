"""PR #5 r8（Gemini r7 レビュー 0da41ad の指摘対応）の回帰テスト。

#1(a) _foreign_wrapper() の 3 経路: 偽パス exec / TMPDIR ファイル、__code__ なし（partial・__call__ インスタンス）、
      _LIB_DIRS 名偽装（.venv/ok.py・__pycache__/ok.py・site-packages/ok.py）
#1(b) _COLLECT_SCRIPT の feed() に co_flags & 0x3FF を含め、CO_GENERATOR 付与による本体未実行を検出
#6    _PureAddition の 3 点: ImportFrom 拡張での fixture 別名禁止、モジュール直下 FunctionDef での shadow チェック、
      同一ファイル内の多重継承 Mixin による MRO 上書き禁止
"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from harness_eval import cheatproof  # noqa: E402
from harness_eval.cheatproof import capture_tests, rebound_report, tamper_check  # noqa: E402

BASE = "def test_x():\n    assert 1 + 1 == 3\n"
UNITTEST_BASE = (
    "import unittest\n\n\n"
    "class TestU(unittest.TestCase):\n"
    "    def test_u(self):\n"
    "        self.assertEqual(1 + 1, 3)\n"
)


class R8Base(unittest.TestCase):
    def setUp(self):
        self.wd = Path(tempfile.mkdtemp(prefix="r8-"))
        self.tmp = Path(tempfile.mkdtemp(prefix="r8-tmp-"))
        self.addCleanup(shutil.rmtree, self.wd, True)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        (self.wd / "tests").mkdir()
        self.t = self.wd / "tests" / "test_a.py"

    def start(self, base: str):
        self.t.write_text(base, encoding="utf-8")
        self.before = capture_tests(self.wd)

    def static_kinds(self, new: str) -> list[str]:
        self.t.write_text(new, encoding="utf-8")
        return [f["kind"] for f in tamper_check(self.before, self.wd)]

    def dynamic_report(self, new: str):
        self.t.write_text(new, encoding="utf-8")
        return rebound_report(self.before, self.wd, self.tmp)

    def assertDynamicTamper(self, base: str, new: str, label: str = ""):
        self.start(base)
        found, reason = self.dynamic_report(new)
        self.assertIsNone(reason, (label, reason))
        self.assertEqual([f["kind"] for f in found], ["test_body_modified"], (label, found))


class ForeignWrapperLoopholes(R8Base):
    """#1(a) _foreign_wrapper() の 3 経路の抜け道と、正当なデコレータの非誤検出。"""

    def test_fake_path_outside_workdir_via_exec_compile_is_detected(self):
        exploit_root = (
            BASE
            + "\n_ns = {}\n"
            "exec(compile('def _ok():\\n    assert True\\n', '/x.py', 'exec'), _ns)\n"
            "_ns['_ok'].__wrapped__ = test_x\n"
            "globals()['test_x'] = _ns['_ok']\n"
        )
        self.assertDynamicTamper(BASE, exploit_root, "fake /x.py")

        exploit_fake_stdlib = (
            "import os, sysconfig\n"
            + BASE
            + "\n_fake = os.path.join(sysconfig.get_path('stdlib'), '__no_such_r8_file__.py')\n"
            "_ns = {}\n"
            "exec(compile('def _ok():\\n    assert True\\n', _fake, 'exec'), _ns)\n"
            "_ns['_ok'].__wrapped__ = test_x\n"
            "globals()['test_x'] = _ns['_ok']\n"
        )
        self.assertDynamicTamper(BASE, exploit_fake_stdlib, "nonexistent file under stdlib")

    def test_file_in_tmpdir_as_wrapper_is_detected(self):
        exploit = (
            "import importlib.util, os, tempfile\n"
            + BASE
            + "\n_p = os.path.join(tempfile.gettempdir(), 'r8_evil_wrapper.py')\n"
            "open(_p, 'w', encoding='utf-8').write('def _ok():\\n    assert True\\n')\n"
            "_spec = importlib.util.spec_from_file_location('_r8_evil', _p)\n"
            "_mod = importlib.util.module_from_spec(_spec)\n"
            "_spec.loader.exec_module(_mod)\n"
            "_mod._ok.__wrapped__ = test_x\n"
            "globals()['test_x'] = _mod._ok\n"
        )
        self.assertDynamicTamper(BASE, exploit, "TMPDIR file wrapper")

    def test_functools_partial_wrapper_without_code_is_detected(self):
        exploit = (
            "import functools\n"
            + BASE
            + "\n_p = functools.partial(lambda: None)\n"
            "_p.__wrapped__ = test_x\n"
            "globals()['test_x'] = _p\n"
        )
        self.assertDynamicTamper(BASE, exploit, "functools.partial wrapper")

    def test_callable_instance_wrapper_without_code_is_detected(self):
        exploit = (
            BASE
            + "\nclass _Ok:\n"
            "    def __call__(self):\n"
            "        return None\n"
            "_ok = _Ok()\n"
            "_ok.__wrapped__ = test_x\n"
            "globals()['test_x'] = _ok\n"
        )
        self.assertDynamicTamper(BASE, exploit, "__call__ instance wrapper")

    def test_lib_dir_name_spoofing_in_workdir_is_detected(self):
        for rel in (".venv/ok.py", "__pycache__/ok.py", "site-packages/ok.py"):
            with self.subTest(rel):
                self.start(BASE)
                spoof = self.wd / rel
                spoof.parent.mkdir(parents=True, exist_ok=True)
                spoof.write_text("def _ok():\n    assert True\n", encoding="utf-8")
                exploit = (
                    "import importlib.util\n"
                    + BASE
                    + f"\n_spec = importlib.util.spec_from_file_location('_spoof', {str(spoof)!r})\n"
                    "_mod = importlib.util.module_from_spec(_spec)\n"
                    "_spec.loader.exec_module(_mod)\n"
                    "_mod._ok.__wrapped__ = test_x\n"
                    "globals()['test_x'] = _mod._ok\n"
                )
                found, reason = self.dynamic_report(exploit)
                self.assertIsNone(reason, rel)
                self.assertEqual([f["kind"] for f in found], ["test_body_modified"], (rel, found))

    def test_legitimate_decorators_are_not_flagged(self):
        (self.wd / "pkg").mkdir()
        (self.wd / "pkg" / "__init__.py").write_text("", encoding="utf-8")
        (self.wd / "pkg" / "deco.py").write_text(
            "import functools\n\n\ndef keep(fn):\n    @functools.wraps(fn)\n    def inner(*a, **k):\n        return fn(*a, **k)\n    return inner\n",
            encoding="utf-8",
        )
        base = (
            "import functools\nimport unittest\nimport unittest.mock\nimport pytest\n"
            "from pkg.deco import keep\n\n\n"
            "def local_deco(f):\n    @functools.wraps(f)\n    def wrapper(*a, **k):\n        return f(*a, **k)\n    return wrapper\n\n\n"
            "@pytest.mark.parametrize('n', [1, 2])\ndef test_param(n):\n    assert n in (1, 2)\n\n\n"
            "@unittest.mock.patch('os.getcwd')\ndef test_patch(m):\n    assert m is not None\n\n\n"
            "@pytest.mark.skipif(False, reason='r')\n@unittest.mock.patch('os.getcwd')\n@keep\n@local_deco\n"
            "def test_multi(m):\n    assert m is not None\n\n\n"
            "class TestC:\n    @unittest.mock.patch('os.getcwd')\n    @keep\n    def test_method(self, m):\n        assert m is not None\n\n\n"
            "class TestU(unittest.TestCase):\n    @unittest.skipIf(False, 'ok')\n    def test_u(self):\n        self.assertTrue(1)\n"
        )
        self.start(base)
        self.assertEqual(self.static_kinds(base), [])
        self.assertEqual(self.dynamic_report(base), ([], None))

    def test_real_site_packages_inside_workdir_is_allowed_by_lib_roots(self):
        real_sp = self.wd / ".venv" / "lib" / "python3.12" / "site-packages"
        real_sp.mkdir(parents=True)
        real_mod = real_sp / "lib_deco.py"
        real_mod.write_text("def wrap(f):\n    return f\n", encoding="utf-8")
        spoof = self.wd / ".venv" / "ok.py"
        spoof.write_text("def ok():\n    pass\n", encoding="utf-8")
        lib_roots = (real_sp.resolve(),)
        self.assertFalse(cheatproof._foreign_wrapper(self.wd, [str(real_mod), 1], set(), lib_roots))
        self.assertTrue(cheatproof._foreign_wrapper(self.wd, [str(spoof), 1], set(), lib_roots))
        self.assertTrue(cheatproof._foreign_wrapper(self.wd, None, set(), lib_roots))
        self.assertTrue(cheatproof._foreign_wrapper(self.wd, [str(real_sp / "missing.py"), 1], set(), lib_roots))


class CodeFlagsLoopholes(R8Base):
    """#1(b) feed() が co_flags & 0x3FF を含めることで CO_GENERATOR / CO_COROUTINE 差し替えを検出する。"""

    def test_co_flags_co_generator_on_unittest_method_is_detected(self):
        exploit = (
            UNITTEST_BASE
            + "\n_n = '__co' + 'de__'\n"
            "_c = getattr(TestU.test_u, _n)\n"
            "setattr(TestU.test_u, _n, _c.replace(co_flags=_c.co_flags | 0x20))\n"
        )
        self.assertDynamicTamper(UNITTEST_BASE, exploit, "unittest CO_GENERATOR")

    def test_co_flags_co_coroutine_on_function_is_detected(self):
        exploit = (
            BASE
            + "\n_n = '__co' + 'de__'\n"
            "_c = getattr(test_x, _n)\n"
            "setattr(test_x, _n, _c.replace(co_flags=_c.co_flags | 0x80))\n"
        )
        self.assertDynamicTamper(BASE, exploit, "function CO_COROUTINE")

    def test_legitimate_future_annotations_and_normal_functions_stay_clean(self):
        base = (
            "from __future__ import annotations\nimport unittest\n\n\n"
            "def test_ann(x: int = 1) -> None:\n    assert x == 1\n\n\n"
            "class TestU(unittest.TestCase):\n    def test_u(self) -> None:\n        self.assertEqual(1, 1)\n"
        )
        self.start(base)
        self.assertEqual(self.dynamic_report(base), ([], None))


class PureAdditionGuards(R8Base):
    """#6 _PureAddition の 3 つのガード漏れ。"""

    def test_import_from_extension_with_fixture_alias_is_tamper(self):
        base = "from pytest import mark\n\n\n@mark.parametrize('x', [1])\ndef test_a(x):\n    assert x == 1\n"
        for alias in ("fixture as fx", "yield_fixture as yf"):
            with self.subTest(alias):
                self.start(base)
                short = alias.split()[-1]
                new = (
                    base.replace("from pytest import mark", f"from pytest import mark, {alias}")
                    .replace("@mark.parametrize", f"@{short}(autouse=True)\ndef test_zz():\n    yield\n\n\n@mark.parametrize")
                )
                self.assertIn("test_body_modified", self.static_kinds(new), alias)

        # 正当な別名でない追加（例: param）は許可のまま
        self.start(base)
        clean = base.replace("from pytest import mark", "from pytest import mark, param")
        self.assertEqual(self.static_kinds(clean), [])

    def test_module_level_test_function_shadowing_referenced_name_is_tamper(self):
        star_base = "from helpers import *\n\n\ndef test_a():\n    assert test_helper()\n"
        for label, insert in [
            ("sync def shadows star-imported name", "def test_helper():\n    return True\n\n\n"),
            ("async def shadows star-imported name", "async def test_helper():\n    return True\n\n\n"),
        ]:
            with self.subTest(label):
                self.start(star_base)
                new = star_base.replace("def test_a():", insert + "def test_a():")
                self.assertIn("test_body_modified", self.static_kinds(new), label)

        # fixture 引数名の shadow もモジュール直下の def test_* では不可
        fixture_base = "def test_a(test_client):\n    assert test_client\n"
        self.start(fixture_base)
        new_fx = "def test_client():\n    return True\n\n\n" + fixture_base
        self.assertIn("test_body_modified", self.static_kinds(new_fx))

        # 参照されていない新しい test_ 関数は従来どおり許可
        self.start(star_base)
        clean = star_base.replace("def test_a():", "def test_other():\n    assert True\n\n\ndef test_a():")
        self.assertEqual(self.static_kinds(clean), [])

    def test_mixin_multiple_inheritance_mro_override_is_tamper(self):
        base = (
            "import unittest\n\n\n"
            "class MixinA:\n"
            "    def test_a(self):\n"
            "        self.assertEqual(1, 1)\n\n\n"
            "class MixinB:\n"
            "    def test_b(self):\n"
            "        self.assertEqual(2, 2)\n\n\n"
            "class Sub(MixinA, MixinB, unittest.TestCase):\n"
            "    def test_sub(self):\n"
            "        self.assertEqual(3, 3)\n"
        )
        # MixinA に MixinB と同名の test_b を足すと Sub の MRO で MixinB.test_b が上書きされる
        self.start(base)
        exploit = base.replace("    def test_a(self):", "    def test_b(self):\n        pass\n\n    def test_a(self):")
        self.assertIn("test_body_modified", self.static_kinds(exploit))

        # 多段継承（BaseB -> MixinB、MixinA -> MidA、Sub(MidA, MixinB)）でも検出
        deep_base = (
            "import unittest\n\n\n"
            "class BaseB:\n"
            "    def test_b(self):\n"
            "        self.assertEqual(2, 2)\n\n\n"
            "class MixinA:\n"
            "    def test_a(self):\n"
            "        self.assertEqual(1, 1)\n\n\n"
            "class MidA(MixinA):\n"
            "    pass\n\n\n"
            "class MixinB(BaseB):\n"
            "    pass\n\n\n"
            "class Sub(MidA, MixinB, unittest.TestCase):\n"
            "    def test_sub(self):\n"
            "        self.assertEqual(3, 3)\n"
        )
        self.start(deep_base)
        deep_exploit = deep_base.replace("    def test_a(self):", "    def test_b(self):\n        pass\n\n    def test_a(self):")
        self.assertIn("test_body_modified", self.static_kinds(deep_exploit))

        # MixinA への同名でない新しい test_ 追加は正当（改ざんにならない）
        self.start(base)
        clean_mixin = base.replace("    def test_a(self):", "    def test_a_extra(self):\n        self.assertTrue(1)\n\n    def test_a(self):")
        self.assertEqual(self.static_kinds(clean_mixin), [])

        # 無関係な別クラスへの test_ 追加も正当
        unrelated = base + "\n\nclass TestOther(unittest.TestCase):\n    def test_other(self):\n        self.assertTrue(1)\n"
        self.start(unrelated)
        clean_other = unrelated.replace(
            "    def test_other(self):", "    def test_extra(self):\n        self.assertTrue(1)\n\n    def test_other(self):"
        )
        self.assertEqual(self.static_kinds(clean_other), [])


if __name__ == "__main__":
    unittest.main()
