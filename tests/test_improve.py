"""改善役（v0.6.0）: 読み取りの隔離の自己確認・候補の組み立てと事前検査・improve / skill-variants コマンド。"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness_eval.candidate import (apply_deltas, changed_skills, materialize, prechecks, target_problem,  # noqa: E402
                                    write_skill_variants)
from harness_eval.mockapi import (_rule_path, deny_rules, probe_verdict, read_isolation_check, special_chars,  # noqa: E402
                                  unusable_path)
from harness_eval.spec import SpecError, load_profile  # noqa: E402

FAKE = ROOT / "tests" / "fake_agents"
FAKE_CLAUDE = [sys.executable, str(FAKE / "fake_claude.py")]


class ProbeVerdictTests(unittest.TestCase):
    """模擬のモデルが出したツール呼び出しに、CLI が返した結果から判定する。"""
    STEPS = [("control", "CANARY-P"), ("control", "PUBLIC.txt"), ("secret", ""), ("secret", "")]
    INIT = {"permissionMode": "default", "tools": ["Read", "Glob", "Grep"]}

    def verdict(self, results: dict, init=None, bodies=()):
        return probe_verdict(self.STEPS, results, "CANARY-S", "S-1.txt", self.INIT if init is None else init, list(bodies))

    def ok_results(self):
        return {0: (False, "1\tCANARY-P"), 1: (False, "/a/PUBLIC.txt"), 2: (True, "denied /x/S-1.txt"), 3: (True, "Permission to read …")}

    def test_ok_when_secret_blocked_and_controls_work(self):
        self.assertEqual(self.verdict(self.ok_results())[0], "ok")  # エラーの文面にファイル名が入るのは漏れではない

    def test_leak_by_content_name_or_other_payload(self):
        for i, res in ((2, (False, "/x/S-1.txt:1:CANARY-S")), (3, (False, "/x/holdout/S-1.txt"))):  # 中身・一覧に出た名前
            state, why = self.verdict({**self.ok_results(), i: res})
            self.assertEqual(state, "leak")
            self.assertIn(str(i), why)
        self.assertEqual(self.verdict(self.ok_results(), bodies=['{"system": "… CANARY-S …"}'])[0], "leak")

    def test_unverified_when_control_fails_or_steps_missing(self):
        # 対照まで失敗する = deny 以外の理由で止まっている → deny の効果は確かめられていない
        self.assertEqual(self.verdict({**self.ok_results(), 0: (True, "denied")})[0], "unverified")
        self.assertEqual(self.verdict({**self.ok_results(), 1: (False, "No files found")})[0], "unverified")
        r = self.ok_results()
        del r[3]
        self.assertEqual(self.verdict(r)[0], "unverified")
        self.assertEqual(self.verdict({})[0], "unverified")

    def test_unverified_when_mode_or_tools_differ(self):
        self.assertEqual(self.verdict(self.ok_results(), init={"permissionMode": "auto", "tools": ["Read"]})[0], "unverified")
        self.assertEqual(self.verdict(self.ok_results(), init={"permissionMode": "default", "tools": ["Read", "Bash"]})[0], "unverified")
        self.assertEqual(self.verdict(self.ok_results(), init={})[0], "unverified")


class DenyRuleTests(unittest.TestCase):
    def test_absolute_rule_form(self):
        p = Path(tempfile.mkdtemp()).resolve()
        rules = deny_rules([p])
        posix = p.as_posix()
        self.assertEqual(rules, [f"Read(/{posix}/**)", f"Read(/{posix})"])
        self.assertTrue(rules[0].startswith("Read(//"))  # 先頭の // が「ファイルシステムの根からの絶対パス」の印

    def test_file_rule_and_duplicates(self):
        d = Path(tempfile.mkdtemp()).resolve()
        f = d / "a.txt"
        f.write_text("x")
        rules = deny_rules([f, d, d / "a.txt"])
        self.assertEqual(rules.count(f"Read(/{f.as_posix()})"), 1)

    @unittest.skipIf(os.name == "nt", "POSIX only")
    def test_backslash_path_is_unusable(self):
        self.assertTrue(unusable_path(Path("/data/hold\\out/run")))  # 2.1.295 では打ち消しても止まらない → 改善役を起動しない
        self.assertFalse(unusable_path(Path("/data/hold out [1]/run")))

    def test_brackets_escaped_and_windows_forms(self):
        self.assertEqual(_rule_path("/o/run[1]/h"), ["/o/run\\[1\\]/h"])  # [ ] はパターン記号
        self.assertEqual(_rule_path("C:/Users/a b/h"), ["/C:/Users/a b/h", "/c/Users/a b/h"])  # Read(//C:/…) と Read(//c/…)
        self.assertEqual(_rule_path("/o/x"), ["/o/x"])
        self.assertEqual(special_chars([Path("/o/run[1] (x)/評価")]), "".join(sorted(" ()[]評価")))


@unittest.skipIf(os.name == "nt", "fake CLI uses POSIX paths in this test")
class ReadIsolationCheckTests(unittest.TestCase):
    """偽 CLI が偽の Messages API と通信し、--settings の deny を守る（守らない）ときの判定。"""

    def test_ok_with_compliant_cli(self):
        state, why = read_isolation_check(FAKE_CLAUDE, model=None, env=dict(os.environ), special=" (1)[]評価")
        self.assertEqual(state, "ok", why)

    def test_leak_detected(self):
        state, why = read_isolation_check(FAKE_CLAUDE, model=None, env={**os.environ, "FAKE_IGNORE_DENY": "1"})
        self.assertEqual(state, "leak", why)

    def test_unverified_when_cli_missing(self):
        state, why = read_isolation_check([str(Path(tempfile.mkdtemp()) / "no-such-claude")], model=None, env=dict(os.environ))
        self.assertEqual(state, "unverified")
        self.assertTrue(why)

    @unittest.skipUnless(os.environ.get("HARNESS_EVAL_REAL_CLI"), "set HARNESS_EVAL_REAL_CLI=1 to probe the real claude (no cost)")
    def test_real_cli(self):
        for special in ("", " ()[]{a,b}*?!#$&'+;=@~評価"):
            state, why = read_isolation_check(["claude"], model=None, env=dict(os.environ), special=special)
            self.assertEqual(state, "ok", (special, why))
            state, why = read_isolation_check(["claude"], model=None, env=dict(os.environ), deny=False, special=special)
            self.assertEqual(state, "leak", (special, why))  # deny なしでは漏れる = 判定が deny の効果を見ている


def write(p: Path, text: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


SKILL = "---\nname: monthly\ndescription: 月別の集計に使う\n---\n\n# 手順\n\n1. scripts/m.py を実行する\n"


def make_profile(tmp: Path, install: str | None = None, extra: str = ""):
    write(tmp / "h" / "CLAUDE.md", "# ルール\n\n- 表は Markdown。\n")
    write(tmp / "h" / "skills" / "monthly" / "SKILL.md", SKILL)
    write(tmp / "h" / "skills" / "monthly" / "scripts" / "m.py", "print(1)\n")
    write(tmp / "h" / "rules" / "style.md", "- 丁寧に\n")
    install = install or ("install:\n  - copy: h/CLAUDE.md\n    to: \"{config_dir}/CLAUDE.md\"\n"
                          "  - copy: h/skills\n    to: \"{config_dir}/skills\"\n  - copy: h/rules/\n    to: \"{config_dir}/rules\"\n")
    return load_profile(write(tmp / "cur.yaml", f"id: cur\nagent: claude_code\nmodel: sonnet\n{install}{extra}"))


class MaterializeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_layout_follows_config_dir(self):
        h = self.tmp / "out"
        materialize(make_profile(self.tmp), h)
        self.assertEqual(sorted(p.relative_to(h).as_posix() for p in h.rglob("*") if p.is_file()),
                         ["CLAUDE.md", "rules/style.md", "skills/monthly/SKILL.md", "skills/monthly/scripts/m.py"])

    def test_run_step_and_outside_target_refused(self):
        with self.assertRaisesRegex(SpecError, "run"):
            materialize(make_profile(self.tmp, "install:\n  - run: echo hi\n"), self.tmp / "o1")
        with self.assertRaisesRegex(SpecError, "config_dir"):
            materialize(make_profile(self.tmp, "install:\n  - copy: h/CLAUDE.md\n    to: \"{workdir}/CLAUDE.md\"\n"), self.tmp / "o2")

    def test_empty_install_is_empty_harness(self):
        h = self.tmp / "o"
        materialize(make_profile(self.tmp, "install: []\n"), h)
        self.assertEqual(list(h.rglob("*")), [])


class ApplyDeltaTests(unittest.TestCase):
    def setUp(self):
        self.h = Path(tempfile.mkdtemp())
        write(self.h / "CLAUDE.md", "# ルール\n\n- 表は Markdown。\n")

    def d(self, **kw):
        base = {"id": "d1", "kind": "INV", "file": "CLAUDE.md", "op": "add", "old": "", "new": "", "reason": "r", "fixes": []}
        return {**base, **kw}

    def test_add_replace_remove(self):
        out = apply_deltas(self.h, [self.d(new="- 確認する。"),
                                    self.d(id="d2", op="replace", old="- 表は Markdown。", new="- 表は Markdown の表。"),
                                    self.d(id="d3", op="add", file="skills/x/SKILL.md", kind="DOM", new="---\nname: x\n---\n"),
                                    self.d(id="d4", op="remove", old="# ルール\n")])
        self.assertTrue(all(r["applied"] for r in out), out)
        self.assertEqual((self.h / "CLAUDE.md").read_text(encoding="utf-8"), "\n- 表は Markdown の表。\n- 確認する。\n")
        self.assertTrue((self.h / "skills" / "x" / "SKILL.md").is_file())

    def test_not_applied_reasons(self):
        out = apply_deltas(self.h, [self.d(op="replace", old="ない文", new="x"),
                                    self.d(id="d2", op="replace", old="\n", new="x"),  # 2 回以上出る
                                    self.d(id="d3", file="../evil.md", new="x"),
                                    self.d(id="d4", file="/abs.md", new="x"),
                                    self.d(id="d5", file="settings.json", new="{}"),
                                    self.d(id="d6", op="move", new="x")])
        self.assertEqual([r["applied"] for r in out], [False] * 6)
        self.assertTrue(all(r["why"] for r in out))
        self.assertFalse((self.h.parent / "evil.md").exists())
        self.assertFalse((self.h / "settings.json").exists())


class ApplyDeltaEdgeTests(unittest.TestCase):
    """レビューで見つかった境界: OS によらないパスの判定・リンク・書き込みの失敗・CRLF。"""

    def setUp(self):
        self.h = Path(tempfile.mkdtemp())
        write(self.h / "CLAUDE.md", "# ルール\n")

    def one(self, **kw):
        base = {"id": "d1", "kind": "INV", "file": "CLAUDE.md", "op": "add", "old": "", "new": "x", "reason": "", "fixes": []}
        return apply_deltas(self.h, [{**base, **kw}])[0]

    def test_windows_style_paths_refused_everywhere(self):
        for rel in ("/abs.md", "\\abs.md", "skills\\x\\SKILL.md", "C:/x.md", "a\x00.md", ".claude/x.md"):
            r = self.one(file=rel)
            self.assertFalse(r["applied"], rel)
        self.assertEqual(target_problem("skills/x/SKILL.md"), "")

    def test_link_out_of_harness_refused(self):
        outside = Path(tempfile.mkdtemp())
        try:
            os.symlink(outside, self.h / "linked", target_is_directory=True)
        except OSError:
            self.skipTest("symlink not permitted")
        self.assertFalse(self.one(file="linked/x.md")["applied"])
        self.assertEqual(list(outside.iterdir()), [])

    def test_write_errors_are_not_applied(self):
        r = self.one(file="CLAUDE.md/x.md")  # 同名のファイルがあるのでフォルダを作れない
        self.assertFalse(r["applied"])
        self.assertIn("書き込めません", r["why"])

    def test_crlf_file_kept_crlf(self):
        (self.h / "w.md").write_bytes(b"# A\r\n- one\r\n")
        self.assertTrue(self.one(file="w.md", op="replace", old="- one\n", new="- two\n")["applied"])
        self.assertTrue(self.one(file="w.md", new="- three")["applied"])
        self.assertEqual((self.h / "w.md").read_bytes(), b"# A\r\n- two\r\n- three\r\n")


class PrecheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cur = self.tmp / "cur"
        write(self.cur / "CLAUDE.md", "# ルール\n- 表は Markdown。\n")
        write(self.cur / "skills" / "monthly" / "SKILL.md", SKILL)
        self.train = "acme_orders.csv の 2026年9月 の売上を store_code ごとに集計して summary.md に書いてください。"

    def check(self, deltas, max_growth=0, graders=("check_monthly_totals.py",)):
        cand = self.tmp / "cand"
        if cand.exists():
            import shutil
            shutil.rmtree(cand)
        import shutil
        shutil.copytree(self.cur, cand)
        results = apply_deltas(cand, deltas)
        applied = [d for d, r in zip(deltas, results, strict=True) if r["applied"]]
        return prechecks(self.cur, cand, applied, [d for d, r in zip(deltas, results, strict=True) if not r["applied"]],
                         self.train, ["csv-summary-train"], set(graders), max_growth)

    def d(self, **kw):
        base = {"id": "d1", "kind": "INV", "file": "CLAUDE.md", "op": "replace", "old": "- 表は Markdown。", "new": "- 表はMarkdown。",
                "reason": "r", "fixes": []}
        return {**base, **kw}

    def test_clean_candidate_passes(self):
        self.assertEqual(self.check([self.d()]), [])

    def test_train_specific_strings(self):
        for new in ("- acme_orders.csv は列が多い。", "- store_code で集計する。", "- csv-summary-train では表にする。",
                    "- 売上を store_code ごとに集計して summary.md に書いてください"):
            problems = self.check([self.d(new=new)], max_growth=1000)
            self.assertTrue(any("train" in p for p in problems), (new, problems))

    def test_string_already_in_harness_is_fine(self):
        write(self.cur / "CLAUDE.md", "# ルール\n- 表は Markdown。\n- summary.md は上書きしない。\n")
        self.assertEqual(self.check([self.d(new="- 表は Markdown。summary.md も同じ。")], max_growth=1000), [])

    def test_grader_files(self):
        self.assertTrue(any("採点" in p for p in self.check([self.d(new="- 最後に check_monthly_totals.py で確かめる。")], max_growth=1000)))
        self.assertTrue(any("Markdown" in p for p in self.check([self.d(file="skills/monthly/scripts/m.py", op="add", new="x")])))
        self.assertTrue(any("外" in p for p in self.check([self.d(file="../outside.md", op="add", new="x")])))

    def test_always_loaded_growth(self):
        grow = self.d(op="add", old="", new="- 依頼が曖昧なら確認する。")
        self.assertTrue(any("常時" in p for p in self.check([grow])))
        self.assertEqual(self.check([grow], max_growth=100), [])
        rule = self.d(op="add", old="", file="rules/ask.md", new="- 確認する。")
        self.assertTrue(any("常時" in p for p in self.check([rule])))
        skill = self.d(op="add", old="", kind="DOM", file="skills/ask/SKILL.md", new="---\nname: ask\ndescription: 曖昧な依頼\n---\n確認する")
        self.assertEqual(self.check([skill]), [])  # スキルは 3 つのプロファイルで比べるので、ここでは数えない

    def test_copy_detected_even_if_harness_shares_a_phrase(self):
        shared = "結果は必ず日本語で、表は Markdown の表として出力してください。"
        self.train += shared
        write(self.cur / "CLAUDE.md", "# ルール\n- 表は Markdown。\n- " + shared + "\n")
        copy = "売上を store_code ごとに集計して summary.md に書いてください"
        self.assertTrue(any("train" in p for p in self.check([self.d(new="- " + copy.replace("store_code", "店"))], max_growth=1000)))

    def test_token_split_across_deltas(self):
        d1 = self.d(new="- acme_ordZZQ は列が多い。")
        d2 = self.d(id="d2", old="ZZQ", new="ers.csv")
        problems = self.check([d1, d2], max_growth=1000)
        self.assertTrue(any("train" in p for p in problems), problems)

    def test_imports_and_agents_count_as_always_loaded(self):
        imp = self.d(new="- 表は Markdown。@docs/more.md")
        more = self.d(id="d2", op="add", old="", file="docs/more.md", new="長い指示。" * 200)
        self.assertTrue(any("常時" in p for p in self.check([imp, more], max_growth=100)))
        self.assertEqual(self.check([self.d(new="- 表は Markdown。連絡先 a@example.com")], max_growth=100), [])  # メールは読み込みではない
        agent = self.d(op="add", old="", file="agents/a.md", new="---\nname: a\ndescription: " + "説明" * 300 + "\n---\n本文")
        self.assertTrue(any("常時" in p for p in self.check([agent])))
        outside = self.d(new="- 表は Markdown。@~/notes.md")
        self.assertTrue(any("@" in p for p in self.check([outside], max_growth=1000)))

    def test_at_words_that_are_not_imports(self):
        """デコレータ・npm のパッケージ・コードの中の @ は読み込みではない。読み込みの直後の句読点はパスに含めない。"""
        for new in ("- 型は `@dataclass(frozen=True)` で書く。", "- @pytest.fixture を使う。", "- 型定義は @types/node を入れる。"):
            self.assertEqual(self.check([self.d(new=new)], max_growth=1000), [], new)
        imp = self.d(new="- 詳細は @docs/more.md。")
        more = self.d(id="d2", op="add", old="", file="docs/more.md", new="長い指示。" * 200)
        problems = self.check([imp, more], max_growth=100)
        self.assertTrue(any("常時" in p for p in problems) and not any("外のファイル" in p for p in problems), problems)

    def test_unreadable_bytes_do_not_crash(self):
        (self.cur / "rules").mkdir()
        (self.cur / "rules" / "x.md").write_bytes(b"\xff\xfe bad")
        self.assertEqual(self.check([self.d()]), [])

    def test_grader_stem_is_a_word(self):
        self.assertEqual(self.check([self.d(new="- 入力を invalidated として扱わない。")], max_growth=1000, graders=("validate.py",)), [])
        self.assertTrue(any("採点" in p for p in self.check([self.d(new="- validate の結果に合わせる。")], max_growth=1000,
                                                             graders=("validate.py",))))

    def test_grader_name_without_extension(self):
        problems = self.check([self.d(new="- check_monthly_totals が見る合計行を入れる。")], max_growth=1000)
        self.assertTrue(any("採点" in p for p in problems), problems)

    def test_nothing_applied(self):
        self.assertTrue(any("適用" in p for p in self.check([self.d(old="ない文")])))
        self.assertTrue(any("適用" in p for p in self.check([])))


class SkillVariantTests(unittest.TestCase):
    def test_three_variants(self):
        tmp = Path(tempfile.mkdtemp())
        prof = make_profile(tmp, extra="inherit_env: [CLAUDE_CODE_OAUTH_TOKEN]\n")
        h = tmp / "cand" / "harness"
        materialize(prof, h)
        paths = write_skill_variants(h, "monthly", prof, "cand", tmp / "cand" / "skills")
        self.assertEqual(sorted(paths), ["description", "full", "none"])
        loaded = {k: load_profile(v) for k, v in paths.items()}
        self.assertEqual({k: p.id for k, p in loaded.items()},
                         {"none": "cand-monthly-none", "description": "cand-monthly-description", "full": "cand-monthly-full"})
        self.assertEqual(loaded["full"].inherit_env, ["CLAUDE_CODE_OAUTH_TOKEN"])  # 認証などの設定は引き継ぐ
        trees = {}
        for k, p in loaded.items():
            d = tmp / f"inst-{k}"
            materialize(p, d)
            trees[k] = {f.relative_to(d).as_posix(): f.read_text(encoding="utf-8") for f in d.rglob("*") if f.is_file()}
        self.assertNotIn("skills/monthly/SKILL.md", trees["none"])
        self.assertEqual(trees["description"]["skills/monthly/SKILL.md"], "---\nname: monthly\ndescription: 月別の集計に使う\n---\n")
        self.assertNotIn("skills/monthly/scripts/m.py", trees["description"])
        self.assertIn("# 手順", trees["full"]["skills/monthly/SKILL.md"])
        for k in trees:  # スキル以外は同じ
            self.assertEqual({f: t for f, t in trees[k].items() if not f.startswith("skills/monthly/")},
                             {f: t for f, t in trees["full"].items() if not f.startswith("skills/monthly/")})
        self.assertEqual(len({p.harness_hash() for p in loaded.values()}), 3)

    def test_profile_dir_resolved_against_base(self):
        tmp = Path(tempfile.mkdtemp())
        prof = make_profile(tmp, extra="env: {HOOKS: '{profile_dir}/bin'}\nexecutable: ['{profile_dir}/bin/wrap.sh']\nhash_paths: [h]\n")
        h = tmp / "cand" / "harness"
        materialize(prof, h)
        raw = yaml.safe_load(write_skill_variants(h, "monthly", prof, "c", tmp / "cand" / "skills")["full"].read_text(encoding="utf-8"))
        base = str(tmp.resolve())
        self.assertEqual((raw["env"]["HOOKS"], raw["executable"]), (base + "/bin", [base + "/bin/wrap.sh"]))
        self.assertNotIn("hash_paths", raw)

    def test_changed_skills(self):
        a, b = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp())
        for d in (a, b):
            write(d / "skills" / "same" / "SKILL.md", SKILL)
            write(d / "skills" / "edit" / "SKILL.md", SKILL)
        write(b / "skills" / "edit" / "SKILL.md", SKILL + "2. 確認する\n")
        write(b / "skills" / "new" / "SKILL.md", SKILL)
        self.assertEqual(changed_skills(a, b), ["edit", "new"])


def run_cli(*args: str) -> tuple[int, str]:
    import contextlib
    import io
    from harness_eval.cli import main
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        code = main(list(args))
    return code, buf.getvalue()


@unittest.skipIf(os.name == "nt", "fake CLI uses POSIX paths in this test")
class ImproveE2E(unittest.TestCase):
    """偽 CLI で train の実行 → improve → 生成されたプロファイルで dev を実行 → 次の回で予想を照合。"""
    TRAIN = ("csv-summary-format", "csv-summary-skill", "arithmetic-no-skill")
    HOLDOUT = ("request-extract", "request-ambiguous")

    def setUp(self):
        import shutil
        self.tmp = Path(tempfile.mkdtemp(prefix="he improve "))
        ex = self.ex = self.tmp / "examples"
        shutil.copytree(ROOT / "examples", ex)
        exe = f"\nexecutable: [{json.dumps(sys.executable)}, {json.dumps(str(FAKE / 'fake_claude.py'))}]\n"
        for f in (ex / "profiles").glob("claude*.yaml"):
            f.write_text(f.read_text(encoding="utf-8") + exe, encoding="utf-8")
        write(ex / "harnesses" / "thin" / "CLAUDE.md", "# ルール\n\n- 丁寧に答える。ファイルを消す必要があるときは理由を書いて止まる。\n")
        self.thin = write(ex / "profiles" / "claude-thin.yaml", "id: claude-thin\nagent: claude_code\nmodel: sonnet\ninstall:\n"
                          "  - copy: ../harnesses/thin/CLAUDE.md\n    to: \"{config_dir}/CLAUDE.md\"\n" + exe)
        for name, split in [(t, "train") for t in self.TRAIN] + [(t, "holdout") for t in self.HOLDOUT]:
            f = ex / "tasks" / f"{name}.yaml"
            f.write_text(f.read_text(encoding="utf-8") + f"\nsplit: {split}\n", encoding="utf-8")
        self.res, self.hold = self.tmp / "res", self.tmp / "holdout-res"
        self.manifest = self.tmp / "manifest.json"
        os.environ["FAKE_IMPROVE_MANIFEST"] = str(self.manifest)

    def tearDown(self):
        for k in ("FAKE_IMPROVE_MANIFEST", "FAKE_IMPROVE", "FAKE_IGNORE_DENY"):
            os.environ.pop(k, None)

    def train_run(self, profile: Path, split="train") -> Path:
        before = set(self.res.iterdir()) if self.res.exists() else set()
        code, out = run_cli("run", "--tasks", str(self.ex / "tasks"), "--split", split, "--profiles", str(profile),
                            "--out", str(self.res), "--no-cache", "--no-llm")
        self.assertEqual(code, 0, out)
        return [d for d in self.res.iterdir() if d.is_dir() and d not in before][0]

    def improve(self, run: Path, out: Path, profile: Path | None = None, *extra) -> tuple[int, str]:
        return run_cli("improve", "--run", str(run), "--profile", str(profile or self.thin), "--tasks", str(self.ex / "tasks"),
                       "--holdout-out", str(self.hold), "--out", str(out), *extra)

    def test_improve_flow(self):
        run = self.train_run(self.thin)
        code, out = self.improve(run, self.tmp / "cand1")
        self.assertEqual(code, 0, out)
        self.assertIn("隔離の自己確認: ok", out)
        r = json.loads((self.tmp / "cand1" / "improve.json").read_text(encoding="utf-8"))
        self.assertEqual(r["isolation_check"]["state"], "ok")
        self.assertTrue(r["precheck"]["passed"], r["precheck"])
        self.assertEqual({d["kind"] for d in r["deltas"]}, {"INV", "DOM"})
        self.assertEqual(r["dropped_fixes"], 1)  # 失敗タスクにない予想（no-such-task）は外す
        self.assertTrue(set(r["predictions"]) <= set(r["failed_tasks"]) and r["predictions"])
        # 改善役に見えたのは材料の写しだけ（タスク定義・採点スクリプト・holdout は作業フォルダにない）。deny に holdout とタスク定義
        m = json.loads(self.manifest.read_text(encoding="utf-8"))
        self.assertTrue(all(f == "harness/CLAUDE.md" or f.startswith("failures/") for f in m["files"]), m["files"])
        self.assertTrue(any(f.endswith("/task.md") for f in m["files"]) and any(f.endswith("/result.json") for f in m["files"]))
        self.assertTrue(any(f.endswith("/transcript.jsonl") for f in m["files"]))
        self.assertFalse(any(t in f for f in m["files"] for t in self.HOLDOUT))
        deny = " ".join(m["deny"])
        for place in (self.hold, self.ex / "tasks", run):
            self.assertIn(f"Read(/{place.resolve().as_posix()}/**)", deny)
        self.assertNotEqual(Path(m["home"]).resolve(), Path.home().resolve())  # 一時ホームで起動
        self.assertFalse(Path(m["home"]).exists())  # 材料と一時ホームは消える
        text = (self.tmp / "cand1" / "improve.json").read_text(encoding="utf-8")
        self.assertNotIn("sales.csv の売上を月ごと", text)  # 依頼文は記録に書かない
        spend = (self.res / "spend.jsonl").read_text(encoding="utf-8")
        self.assertIn("improve-", spend)
        # 生成されたプロファイル: dev は現行と同じ id、holdout は別の id。配布物のハッシュは同じ
        dev, hold = load_profile(Path(r["profiles"]["dev"])), load_profile(Path(r["profiles"]["holdout"]))
        self.assertEqual((dev.id, hold.id), ("claude-thin", "candidate"))
        self.assertEqual(dev.harness_hash(), hold.harness_hash())
        self.assertEqual(r["candidate"]["harness_hash"], hold.harness_hash())
        self.assertEqual(sorted(r["skills"]["table-format"]), ["description", "full", "none"])
        # 次の回: 候補（dev 用 = 現行と同じ id）で train を実行し、前回の予想を照合する
        run2 = self.train_run(Path(r["profiles"]["dev"]))
        code, out = self.improve(run2, self.tmp / "cand2", Path(r["profiles"]["dev"]), "--previous", str(self.tmp / "cand1" / "improve.json"))
        self.assertIn(code, (0, 3), out)
        r2 = json.loads((self.tmp / "cand2" / "improve.json").read_text(encoding="utf-8"))
        self.assertEqual(sorted(r2["previous"]["predicted"]), sorted(r["predictions"]))
        self.assertTrue(r2["previous"]["same_harness"])
        self.assertIn("前回の予想", out)
        # 3 つのスキル比較のプロファイルは同じ実行で動く
        code, out = run_cli("run", "--tasks", str(self.ex / "tasks"), "--split", "train", "--profiles",
                            *[r["skills"]["table-format"][v] for v in ("none", "description", "full")],
                            "--out", str(self.tmp / "skill-res"), "--no-cache", "--no-llm")
        self.assertEqual(code, 0, out)

    def test_refusals(self):
        run = self.train_run(self.thin)
        os.environ["FAKE_IMPROVE"] = "overfit"
        code, out = self.improve(run, self.tmp / "c-over")
        self.assertEqual(code, 1, out)  # train のタスク id を書いた候補は評価の前に見送り
        self.assertIn("train のタスク固有", out)
        self.assertFalse((self.tmp / "c-over" / "holdout").exists())  # 見送った候補のプロファイルは作らない
        os.environ["FAKE_IMPROVE"] = "bash"
        code, out = self.improve(run, self.tmp / "c-bash")
        self.assertEqual(code, 2, out)  # 前提と違う起動（Bash が有効）なら結果を捨てる
        self.assertIn("結果を捨てました", out)
        os.environ["FAKE_IMPROVE"] = "error"
        self.assertEqual(self.improve(run, self.tmp / "c-err")[0], 2)
        os.environ.pop("FAKE_IMPROVE")
        os.environ["FAKE_IGNORE_DENY"] = "1"
        self.manifest.unlink(missing_ok=True)
        code, out = self.improve(run, self.tmp / "c-leak")
        self.assertEqual(code, 2, out)  # 自己確認で漏れ → 改善役は起動しない
        self.assertIn("leak", out)
        self.assertFalse(self.manifest.exists())
        os.environ.pop("FAKE_IGNORE_DENY")
        code, out = self.improve(run, self.tmp / "c-leak")
        self.assertEqual(code, 2, out)  # 空でない書き出し先
        hold_run = self.train_run(self.thin, split="holdout")
        code, out = self.improve(hold_run, self.tmp / "c-hold")
        self.assertEqual(code, 2, out)  # holdout を含む実行は渡せない
        self.assertIn("train 以外", out)
        code, out = self.improve(run, self.tmp / "c-id", None, "--candidate-id", "claude-thin")
        self.assertEqual(code, 2, out)

    def test_run_installer_and_nothing_to_improve(self):
        runner = write(self.ex / "profiles" / "claude-run.yaml", self.thin.read_text(encoding="utf-8").replace("id: claude-thin", "id: claude-run")
                       .replace('  - copy: ../harnesses/thin/CLAUDE.md\n    to: "{config_dir}/CLAUDE.md"\n', "  - run: echo hi\n"))
        run = self.train_run(runner)
        code, out = self.improve(run, self.tmp / "c-run", runner)
        self.assertEqual(code, 2, out)
        self.assertIn("run", out)
        lite = self.ex / "profiles" / "claude-ds-lite.yaml"
        run = self.train_run(lite)
        code, out = self.improve(run, self.tmp / "c-none", lite)
        self.assertEqual(code, 3, out)  # 失敗がなければ改善役を呼ばない

    def test_skill_variants_command(self):
        code, out = run_cli("skill-variants", "--profile", str(self.ex / "profiles" / "claude-ds-lite.yaml"),
                            "--skill", "csv-monthly-summary", "--out", str(self.tmp / "sv"))
        self.assertEqual(code, 0, out)
        ids = sorted(load_profile(p).id for p in (self.tmp / "sv" / "skills" / "csv-monthly-summary").glob("*.yaml"))
        self.assertEqual(ids, [f"claude-ds-lite-csv-monthly-summary-{v}" for v in ("description", "full", "none")])
        code, out = run_cli("run", "--tasks", str(self.ex / "tasks"), "--task", "csv-summary-skill", "--profiles",
                            str(self.tmp / "sv" / "skills"), "--out", str(self.tmp / "sv-res"), "--no-cache", "--no-llm")
        self.assertEqual(code, 0, out)
        self.assertEqual(run_cli("skill-variants", "--profile", str(self.ex / "profiles" / "claude-ds-lite.yaml"),
                                 "--skill", "nope", "--out", str(self.tmp / "sv2"))[0], 2)


if __name__ == "__main__":
    unittest.main()
