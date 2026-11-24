"""PR #5 r9（Gemini PR 全体レビュー round 1、対象 3479e11）の再現・回帰テスト。

各ブロッキング指摘 B1〜B6 と非ブロッキング N2 の再現テスト（exploit / 不具合）と、
正当なデコレータ・unittest テストに対する非誤検出テスト。
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from harness_eval import cheatproof  # noqa: E402
from harness_eval.cheatproof import capture_tests, rebound_report, tamper_check  # noqa: E402
from harness_eval.isolation import ProcResult, build_env, make_trial_paths  # noqa: E402
from harness_eval.judge_phase import JudgePhase  # noqa: E402
from harness_eval.runner import RunOptions  # noqa: E402
from harness_eval.spec import Profile  # noqa: E402

BASE = "def test_x():\n    assert 1 + 1 == 3\n"
UNITTEST_BASE = (
    "import unittest\n\n\n"
    "class TestA(unittest.TestCase):\n"
    "    def test_x(self):\n"
    "        self.assertEqual(1 + 1, 3)\n"
)


class R9Base(unittest.TestCase):
    def setUp(self):
        self.wd = Path(tempfile.mkdtemp(prefix="r9-"))
        self.tmp = Path(tempfile.mkdtemp(prefix="r9-tmp-"))
        self.addCleanup(shutil.rmtree, self.wd, True)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        (self.wd / "tests").mkdir()
        self.t = self.wd / "tests" / "test_a.py"

    def start(self, base: str):
        self.t.write_text(base, encoding="utf-8")
        self.before = capture_tests(self.wd)

    def static_findings(self, new: str) -> list[dict[str, str]]:
        self.t.write_text(new, encoding="utf-8")
        return tamper_check(self.before, self.wd)

    def static_kinds(self, new: str) -> list[str]:
        return [f["kind"] for f in self.static_findings(new)]

    def dynamic_report(self, new: str):
        self.t.write_text(new, encoding="utf-8")
        return rebound_report(self.before, self.wd, self.tmp)

    def assertDynamicTamper(self, base: str, new: str, label: str = ""):
        self.start(base)
        found, reason = self.dynamic_report(new)
        self.assertIsNone(reason, (label, reason))
        self.assertEqual([f["kind"] for f in found], ["test_body_modified"], (label, found))


class B1WrapperDigestAndFuncUnwrap(R9Base):
    """B1: 実在 stdlib パス偽装による _foreign_wrapper 迂回と、非 MethodType の __func__ 無条件アンラップ。"""

    def test_real_stdlib_path_spoofing_via_exec_compile_is_detected(self):
        exploit = (
            "import os\n"
            + BASE
            + "\n_ns = {}\n"
            "exec(compile('def _ok():\\n    assert True\\n', os.__file__, 'exec'), _ns)\n"
            "_ns['_ok'].__wrapped__ = test_x\n"
            "globals()['test_x'] = _ns['_ok']\n"
        )
        self.assertDynamicTamper(BASE, exploit, "spoofed os.__file__ via exec")

    def test_real_stdlib_path_spoofing_via_code_replace_is_detected(self):
        exploit = (
            "import os\n"
            + BASE
            + "\ndef _ok():\n    assert True\n"
            "_ok.__code__ = _ok.__code__.replace(co_filename=os.__file__, co_firstlineno=1)\n"
            "_ok.__wrapped__ = test_x\n"
            "globals()['test_x'] = _ok\n"
        )
        self.assertDynamicTamper(BASE, exploit, "spoofed os.__file__ via code.replace")

    def test_callable_instance_with_dunder_func_attribute_is_detected(self):
        exploit = (
            UNITTEST_BASE
            + "\nclass _Wrap:\n"
            "    __func__ = TestA.test_x\n"
            "    def __call__(self, *a, **k):\n"
            "        assert True\n"
            "setattr(TestA, 'test_x', _Wrap())\n"
        )
        self.assertDynamicTamper(UNITTEST_BASE, exploit, "callable with __func__ attribute")


class B2CollectorIsolation(R9Base):
    """B2: _COLLECT_SCRIPT が workdir の同名モジュールでシャドーイングされず、__main__ 経由でも改ざんされない。"""

    def test_shadowed_sysconfig_in_workdir_cannot_bypass_collector(self):
        self.start(BASE)
        # 本物の sysconfig に委譲しつつ purelib を workdir に偽装するシャドーモジュール
        (self.wd / "sysconfig.py").write_text(
            "import importlib.util, os, sys\n"
            "_real = next(p for p in sys.path if p not in ('', '.', os.getcwd()) and os.path.isfile(os.path.join(p, 'sysconfig.py')))\n"
            "_spec = importlib.util.spec_from_file_location('_real_sc', os.path.join(_real, 'sysconfig.py'))\n"
            "_mod = importlib.util.module_from_spec(_spec)\n"
            "_spec.loader.exec_module(_mod)\n"
            "def get_paths(*a, **k):\n"
            "    p = dict(_mod.get_paths(*a, **k))\n"
            "    p['purelib'] = os.getcwd()\n"
            "    return p\n"
            "def __getattr__(name):\n"
            "    return getattr(_mod, name)\n",
            encoding="utf-8",
        )
        exploit = (
            BASE
            + "\ndef _ok():\n    assert True\n"
            "_ok.__wrapped__ = test_x\n"
            "globals()['test_x'] = _ok\n"
        )
        found, reason = self.dynamic_report(exploit)
        self.assertIsNone(reason, reason)
        self.assertEqual([f["kind"] for f in found], ["test_body_modified"], found)

    def test_shadowed_json_in_workdir_cannot_break_or_forge_collector(self):
        self.start(BASE)
        fake_payload = json.dumps({
            "rc": 0,
            "items": [{"id": "tests/test_a.py::test_x", "file": str(self.t), "line": 1, "chain": [], "code_changed": False}],
            "lib_roots": [],
        })
        (self.wd / "json.py").write_text(
            "def load(f):\n    return {}\n"
            f"def dump(obj, f):\n    f.write({fake_payload!r})\n"
            f"def dumps(obj):\n    return {fake_payload!r}\n",
            encoding="utf-8",
        )
        exploit = (
            BASE
            + "\ndef _ok():\n    assert True\n"
            "globals()['test_x'] = _ok\n"
        )
        found, reason = self.dynamic_report(exploit)
        self.assertIsNone(reason, reason)
        self.assertEqual([f["kind"] for f in found], ["test_body_modified"], found)

    def test_mutating_main_globals_cannot_poison_lib_roots_or_collector(self):
        exploit = (
            "import __main__, os\n"
            + BASE
            + "\nif hasattr(__main__, 'trusted_roots'):\n"
            "    __main__.trusted_roots.append(os.getcwd())\n"
            "def _ok():\n    assert True\n"
            "_ok.__wrapped__ = test_x\n"
            "globals()['test_x'] = _ok\n"
        )
        self.assertDynamicTamper(BASE, exploit, "mutate __main__.trusted_roots")


class B3HostFileSafety(R9Base):
    """B3: ホスト側の cheatproof.py がシンボリックリンク追従による秘密漏洩や FIFO ブロックを起こさない。"""

    @unittest.skipIf(os.name == "nt", "symlink requires privileges on Windows")
    def test_symlinked_test_file_does_not_leak_host_secret_and_is_detected(self):
        secret = self.tmp / "host_secret.json"
        token = "SUPER-SECRET-HOST-TOKEN-12345"
        secret.write_text(json.dumps({"refresh_token": token}) + "\n", encoding="utf-8")
        init_py = self.wd / "tests" / "__init__.py"
        init_py.write_text("# pkg\n", encoding="utf-8")
        self.start(BASE)
        init_py.unlink()
        init_py.symlink_to(secret)
        findings = tamper_check(self.before, self.wd)
        self.assertTrue(findings, "symlinked baseline test file must be flagged")
        blob = json.dumps(findings, ensure_ascii=False)
        self.assertNotIn(token, blob, "host secret must never be read or leaked via symlink")
        self.assertTrue(any(f["path"] == "tests/__init__.py" for f in findings), findings)

    @unittest.skipIf(os.name == "nt" or not hasattr(os, "mkfifo"), "mkfifo is POSIX only")
    def test_fifo_in_workdir_does_not_hang_known_wrappers_or_tamper_check(self):
        helper = self.wd / "helper.py"
        helper.write_text("def h():\n    return 1\n", encoding="utf-8")
        self.start(BASE)
        helper.unlink()
        os.mkfifo(helper)

        err: list[BaseException] = []

        def run():
            try:
                cheatproof._known_wrappers(self.wd, self.before)
                capture_tests(self.wd)
            except BaseException as e:  # pragma: no cover - diagnostic
                err.append(e)

        th = threading.Thread(target=run, daemon=True)
        th.start()
        th.join(timeout=2.0)
        hung = th.is_alive()
        if hung:
            # テスト終了時にスレッドを詰まらせないよう、書き込み側を開いてすぐ閉じる
            try:
                fd = os.open(helper, os.O_WRONLY | os.O_NONBLOCK)
                os.close(fd)
            except OSError:
                pass
            th.join(timeout=1.0)
        self.assertFalse(hung, "_known_wrappers / capture_tests hung on FIFO")
        self.assertEqual(err, [])

    @unittest.skipIf(os.name == "nt" or not hasattr(os, "mkfifo"), "mkfifo is POSIX only")
    def test_fifo_replacing_baseline_test_file_is_detected_without_hanging(self):
        self.start(BASE)
        self.t.unlink()
        os.mkfifo(self.t)
        res: list = []

        def run():
            res.append(tamper_check(self.before, self.wd))
            cheatproof._current_positions(self.wd, "tests/test_a.py", BASE)

        th = threading.Thread(target=run, daemon=True)
        th.start()
        th.join(timeout=2.0)
        hung = th.is_alive()
        if hung:
            try:
                fd = os.open(self.t, os.O_WRONLY | os.O_NONBLOCK)
                os.close(fd)
            except OSError:
                pass
            th.join(timeout=1.0)
        self.assertFalse(hung, "tamper_check / _current_positions hung on FIFO")
        self.assertTrue(res and any(f["path"] == "tests/test_a.py" for f in res[0]), res)


class B4TrailingAppendGuards(R9Base):
    """B4: .py テストファイル末尾追記でも既存クラスへの非テストメンバー・fixture・予約フック・shadow を拒否する。"""

    def test_trailing_non_test_methods_appended_to_existing_class_are_tamper(self):
        for label, extra in {
            "assertEqual": "\n    def assertEqual(self, a, b):\n        pass\n",
            "run": "\n    def run(self, result=None):\n        pass\n",
            "setUp": "\n    def setUp(self):\n        pass\n",
            "attribute": "\n    flag = True\n",
        }.items():
            with self.subTest(label):
                self.start(UNITTEST_BASE)
                self.assertIn("test_body_modified", self.static_kinds(UNITTEST_BASE + extra), label)

    def test_trailing_fixture_and_reserved_hooks_and_shadow_imports_are_tamper(self):
        cases = {
            "autouse fixture": (
                BASE,
                BASE + "\nimport pytest\n\n@pytest.fixture(autouse=True)\ndef _bypass(request):\n    request.node.obj = lambda: None\n",
            ),
            "fixture alias": (
                BASE,
                BASE + "\nfrom pytest import fixture as fx\n\n@fx(autouse=True)\ndef _bypass(request):\n    request.node.obj = lambda: None\n",
            ),
            "pytest hook": (
                BASE,
                BASE + "\ndef pytest_collection_modifyitems(items):\n    items.clear()\n",
            ),
            "load_tests": (
                BASE,
                BASE + "\ndef load_tests(loader, tests, pattern):\n    return tests\n",
            ),
            "setup_module": (
                BASE,
                BASE + "\ndef setup_module(m):\n    pass\n",
            ),
            "trailing import shadowing builtin": (
                "def test_len():\n    assert len([1]) == 1\n",
                "def test_len():\n    assert len([1]) == 1\n\nfrom other import len\n",
            ),
        }
        for label, (base, new) in cases.items():
            with self.subTest(label):
                self.start(base)
                self.assertIn("test_body_modified", self.static_kinds(new), label)


class _DummyJudgeHost(JudgePhase):
    def __init__(self, tmp: Path, judge_profiles: dict[str, Profile | None], *,
                 max_cost_usd: float | None = None, monthly_budget_usd: float | None = None,
                 month_spent: dict[str, float] | None = None):
        self.run_dir = tmp / "run"
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.opt = RunOptions(max_cost_usd=max_cost_usd, monthly_budget_usd=monthly_budget_usd,
                              cache_dir=tmp / "cache", use_cache=False)
        self.judge_profiles = judge_profiles
        self._judge_adapters: dict = {}
        self.judge_versions: dict = {}
        self._identities: dict = {}
        self._judge_seq = 0
        self.lock = threading.Lock()
        self._inplace_locks = {"claude_code": threading.Lock(), "codex": threading.Lock()}
        self.spent = 0.0
        self.spent_api: dict[str, float] = {"claude_code": 0.0, "codex": 0.0}
        self._month_spent = month_spent
        self.judge_usage: dict = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "calls": 0,
                                  "cost_usd": 0.0, "cost_unknown": 0, "cache_hits": 0, "by_model": {}}

    def month_remaining(self, agent: str) -> float | None:
        if self._month_spent is None or self.opt.monthly_budget_usd is None:
            return None
        return max(self.opt.monthly_budget_usd - self._month_spent.get(agent, 0.0), 0.0)

    def _judge_billing(self, family: str) -> str:
        jp = self.judge_profiles.get(family)
        return jp.billing if jp else "api"

    def _spend(self, usd: float | None, agent: str, billing: str) -> None:
        if not usd:
            return
        with self.lock:
            self.spent += usd
            if billing == "api":
                self.spent_api[agent] += usd


class B5JudgeEnvAndIsolation(unittest.TestCase):
    """B5: 判定役の既定フォールバック環境変数の絞り込みと、非 in_place 判定の bwrap 隔離。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="r9-b5-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_fallback_judge_profile_does_not_inherit_unrelated_parent_secrets(self):
        host = _DummyJudgeHost(self.tmp, {"codex": None})
        fake_env = {
            "PATH": "/usr/bin",
            "GH_TOKEN": "ghp_secret",
            "ANTHROPIC_API_KEY": "sk-ant-secret",
            "CLAUDE_CODE_OAUTH_TOKEN": "oauth-secret",
            "AWS_SECRET_ACCESS_KEY": "aws-secret",
            "OPENAI_API_KEY": "sk-openai-ok",
        }
        with mock.patch.dict(os.environ, fake_env, clear=True):
            prof = host._judge_profile("codex")
            tp = make_trial_paths(self.tmp / "j", ".codex")
            env = build_env(prof, tp, "CODEX_HOME")
        for leaked in ("GH_TOKEN", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "AWS_SECRET_ACCESS_KEY"):
            self.assertNotIn(leaked, prof.inherit_env, leaked)
            self.assertNotIn(leaked, env, leaked)
        self.assertEqual(env.get("OPENAI_API_KEY"), "sk-openai-ok")

    def test_non_in_place_judge_call_uses_bwrap_when_available(self):
        from harness_eval.adapters import CodexAdapter

        host = _DummyJudgeHost(self.tmp, {"codex": Profile(id="j", agent="codex", path=self.tmp / "j.yaml")})
        host._judge_adapters["codex"] = CodexAdapter(executable=[sys.executable, "-c", "pass"])
        seen_cmds: list[list[str]] = []

        def fake_run(cmd, cwd, env, prompt, stdout, stderr, timeout_s):
            seen_cmds.append(list(cmd))
            stdout.write_text(
                json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "PASS\nok"}}) + "\n"
                + json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 5}}) + "\n",
                encoding="utf-8",
            )
            return ProcResult(0, False, 1)

        with mock.patch("harness_eval.judge_phase.sandbox_mode", return_value="bwrap"), \
                mock.patch("harness_eval.judge_phase.bwrap_problem", return_value=None), \
                mock.patch("harness_eval.judge_phase.run_process", side_effect=fake_run):
            text, _, _ = host._judge_call("codex", "gpt-6.2-sol", "grade this")
        self.assertIn("PASS", text)
        self.assertTrue(seen_cmds and seen_cmds[0][0] == "bwrap", seen_cmds)


class B6SubscriptionJudgeBudget(unittest.TestCase):
    """B6: billing: subscription の判定役は --monthly-budget-usd で停止しない。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="r9-b6-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_subscription_judge_is_not_stopped_by_monthly_api_budget(self):
        sub_prof = Profile(id="codex-sub", agent="codex", path=self.tmp / "p.yaml", billing="subscription")
        host = _DummyJudgeHost(self.tmp, {"codex": sub_prof}, monthly_budget_usd=10.0, month_spent={"codex": 10.0})
        self.assertFalse(host._judge_over_budget("codex"))

    def test_api_judge_is_stopped_by_monthly_api_budget_and_max_cost_applies_to_subscription(self):
        api_prof = Profile(id="codex-api", agent="codex", path=self.tmp / "p.yaml", billing="api")
        host_api = _DummyJudgeHost(self.tmp, {"codex": api_prof}, monthly_budget_usd=10.0, month_spent={"codex": 10.0})
        self.assertTrue(host_api._judge_over_budget("codex"))

        sub_prof = Profile(id="codex-sub", agent="codex", path=self.tmp / "p.yaml", billing="subscription")
        host_sub = _DummyJudgeHost(self.tmp, {"codex": sub_prof}, max_cost_usd=1.0, monthly_budget_usd=10.0, month_spent={"codex": 10.0})
        host_sub.spent = 1.0
        self.assertTrue(host_sub._judge_over_budget("codex"))


class N2LegitimateCodelessAndStandardDecorators(R9Base):
    """N2 + 非誤検出: 正当なデコレータ・unittest テストを誤検出しないこと。"""

    def test_lru_cache_decorator_on_baseline_test_is_not_flagged(self):
        base = (
            "import functools\n\n\n"
            "@functools.lru_cache(maxsize=None)\n"
            "def test_cached():\n"
            "    assert 1 == 1\n"
        )
        self.start(base)
        self.assertEqual(self.dynamic_report(base), ([], None))

    def test_lru_cache_with_swapped_wrapped_attribute_is_detected(self):
        exploit = (
            "import functools\n"
            + BASE
            + "\n_ok = functools.lru_cache(maxsize=None)(lambda: None)\n"
            "_ok.__wrapped__ = test_x\n"
            "globals()['test_x'] = _ok\n"
        )
        self.assertDynamicTamper(BASE, exploit, "lru_cache with forged __wrapped__")

    def test_baseline_callable_class_decorator_with_update_wrapper_is_not_flagged(self):
        base = (
            "import functools\n\n\n"
            "class Wrap:\n"
            "    def __init__(self, fn):\n"
            "        self.fn = fn\n"
            "        functools.update_wrapper(self, fn)\n\n"
            "    def __call__(self, *a, **k):\n"
            "        return self.fn(*a, **k)\n\n\n"
            "@Wrap\n"
            "def test_wrapped():\n"
            "    assert 1 == 1\n"
        )
        self.start(base)
        self.assertEqual(self.dynamic_report(base), ([], None))

    def test_new_callable_class_decorator_added_by_solver_is_still_detected(self):
        exploit = (
            "import functools\n"
            + BASE
            + "\nclass _EvilWrap:\n"
            "    def __init__(self, fn):\n"
            "        functools.update_wrapper(self, fn)\n"
            "    def __call__(self, *a, **k):\n"
            "        return None\n"
            "globals()['test_x'] = _EvilWrap(test_x)\n"
        )
        self.assertDynamicTamper(BASE, exploit, "solver-added callable class wrapper")

    def test_legitimate_decorators_and_unittest_variants_are_not_flagged(self):
        (self.wd / "pkg").mkdir()
        (self.wd / "pkg" / "__init__.py").write_text("", encoding="utf-8")
        (self.wd / "pkg" / "deco.py").write_text(
            "import functools\n\n\n"
            "def keep(fn):\n"
            "    @functools.wraps(fn)\n"
            "    def inner(*a, **k):\n"
            "        return fn(*a, **k)\n"
            "    return inner\n",
            encoding="utf-8",
        )
        base = (
            "import functools\n"
            "import unittest\n"
            "import unittest.mock\n"
            "import pytest\n"
            "from pkg.deco import keep\n\n\n"
            "def local_deco(f):\n"
            "    @functools.wraps(f)\n"
            "    def wrapper(*a, **k):\n"
            "        return f(*a, **k)\n"
            "    return wrapper\n\n\n"
            "@pytest.mark.parametrize('n', [1, 2])\n"
            "def test_param(n):\n"
            "    assert n in (1, 2)\n\n\n"
            "@unittest.mock.patch('os.getcwd', autospec=True)\n"
            "def test_patch_autospec(m):\n"
            "    assert m is not None\n\n\n"
            "@pytest.mark.skipif(False, reason='r')\n"
            "@unittest.mock.patch('os.getcwd')\n"
            "@keep\n"
            "@local_deco\n"
            "def test_multi(m):\n"
            "    assert m is not None\n\n\n"
            "class TestStaticAndClass:\n"
            "    @staticmethod\n"
            "    def test_static():\n"
            "        assert True\n\n"
            "    @classmethod\n"
            "    def test_cls(cls):\n"
            "        assert cls is TestStaticAndClass\n\n\n"
            "class TestU(unittest.TestCase):\n"
            "    @unittest.mock.patch('os.getcwd', autospec=True)\n"
            "    def test_u(self, m):\n"
            "        self.assertIsNotNone(m)\n\n\n"
            "class TestAsync(unittest.IsolatedAsyncioTestCase):\n"
            "    async def test_async(self):\n"
            "        self.assertTrue(True)\n"
        )
        self.start(base)
        self.assertEqual(self.static_kinds(base), [])
        self.assertEqual(self.dynamic_report(base), ([], None))


class N5AndN6SmallFixes(unittest.TestCase):
    """N5/N6 の小修正の回帰テスト。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="r9-n6-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_verdict_corrupt_prereg_returns_spec_error_exit_2(self):
        import contextlib
        import io
        from harness_eval.cli import main

        d = self.tmp / "run"
        d.mkdir()
        (d / "summary.json").write_text(json.dumps({"meta": {"tasks": {}, "options": {}}}), encoding="utf-8")
        (d / "trials.jsonl").write_text("", encoding="utf-8")
        (d / "prereg.json").write_text("{corrupt", encoding="utf-8")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            code = main(["verdict", str(d), "--baseline", "a", "--candidate", "b"])
        self.assertEqual(code, 2, buf.getvalue())
        self.assertIn("prereg.json", buf.getvalue())

    def test_budget_warnings_skips_codex_model_in_price_table(self):
        from types import SimpleNamespace
        from harness_eval.cli import _budget_warnings

        args = SimpleNamespace(max_cost_usd=5.0, monthly_budget_usd=None, judge_model=None, judge_agent=None, no_llm=True, jobs=1)
        priced = Profile(id="c-known", agent="codex", path=self.tmp / "a.yaml", model="gpt-6.2-sol")
        unpriced = Profile(id="c-unknown", agent="codex", path=self.tmp / "b.yaml", model="unknown-x")
        self.assertEqual(_budget_warnings(args, [priced]), [])
        warns = _budget_warnings(args, [unpriced])
        self.assertTrue(any("c-unknown" in w for w in warns), warns)

    @unittest.skipIf(os.name == "nt", "POSIX mode bits")
    def test_credentials_lend_creates_file_with_0600_mode(self):
        import stat
        from harness_eval import credentials

        src_dir = self.tmp / "src"
        dst_dir = self.tmp / "dst"
        src_dir.mkdir()
        dst_dir.mkdir()
        (src_dir / "auth.json").write_text('{"tokens": {}}', encoding="utf-8")
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(src_dir)}):
            lent = credentials.lend("codex", dst_dir)
        self.assertIsNotNone(lent)
        assert lent is not None
        self.assertEqual(stat.S_IMODE(lent.dst.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
