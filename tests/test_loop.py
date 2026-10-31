"""自己改善ループの評価側の土台（v0.5.0）: タスクの分割・holdout 利用台帳・符号検定・回帰予算・昇格判定・雑音幅。"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness_eval.metrics import (gate, holdout_comparisons, paired_outcomes, promote, sign_test, split_half_noise,  # noqa: E402
                                  summarize)
from harness_eval.record import TrialRecord  # noqa: E402
from harness_eval.runner import RunOptions, select_pairs  # noqa: E402
from harness_eval.spec import SpecError, lint, load_profile, load_task, split_mixing  # noqa: E402


def write(p: Path, text: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def rec(**kw) -> TrialRecord:
    base = dict(run_id="r", task_id="t", profile_id="p", agent="claude_code", trial=0, cache_key="k",
                task_hash="th", profile_hash="ph", cli_version="v")
    base.update(kw)
    return TrialRecord(**base)


def task_yaml(tid: str, extra: str = "") -> str:
    return f"id: {tid}\nprompt: x\ngraders: [{{type: regex, pattern: x}}]\n{extra}"


class SplitSpecTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_split_and_family_keys(self):
        t = load_task(write(self.tmp / "a.yaml", task_yaml("a", "split: holdout\nfamily: monthly-report\n")))
        self.assertEqual((t.split, t.family), ("holdout", "monthly-report"))
        plain = load_task(write(self.tmp / "b.yaml", task_yaml("b")))
        self.assertEqual((plain.split, plain.family), (None, "b"))  # family の既定はタスク id（1 タスク 1 グループ）
        with self.assertRaisesRegex(SpecError, "split"):
            load_task(write(self.tmp / "c.yaml", task_yaml("c", "split: test\n")))
        with self.assertRaisesRegex(SpecError, "family"):
            load_task(write(self.tmp / "d.yaml", task_yaml("d", "family: 'a b'\n")))

    def test_split_and_family_not_in_hash(self):
        """分割を付け替えても再実行しない（結果に影響しない）。タスクを holdout に移しても前回の結果と比べられる。"""
        p = write(self.tmp / "a.yaml", task_yaml("a"))
        h = load_task(p).content_hash()
        p.write_text(task_yaml("a", "split: dev\nfamily: g1\n"), encoding="utf-8")
        self.assertEqual(h, load_task(p).content_hash())

    def test_lint_family_across_splits_and_small_holdout(self):
        ts = [load_task(write(self.tmp / f"{i}.yaml", task_yaml(i, f"split: {s}\nfamily: {f}\n")))
              for i, s, f in (("a", "train", "g1"), ("b", "holdout", "g1"), ("c", "holdout", "g2"))]
        base = load_profile(write(self.tmp / "p.yaml", "id: base\nagent: claude_code\n"))
        w = "\n".join(lint(ts, [base]))
        self.assertIn("g1", w)  # 同じグループが train と holdout にまたがる → 似たタスクから答えが漏れる
        self.assertIn("holdout", w)
        self.assertIn("2 件", w)  # holdout が少なすぎる（昇格判定がほぼ判定不能になる）

    def test_harness_hash_ignores_id(self):
        """同じ配布物を別の id で評価しても、配布物のハッシュは同じ（台帳で候補を二重に数えない・同一を見分ける）。"""
        write(self.tmp / "h" / "CLAUDE.md", "rule")
        a = load_profile(write(self.tmp / "a.yaml", "id: a\nagent: claude_code\ninstall: [{copy: h, to: '{config_dir}'}]\n"))
        b = load_profile(write(self.tmp / "b.yaml", "id: b\nagent: claude_code\ninstall: [{copy: h, to: '{config_dir}'}]\n"))
        self.assertNotEqual(a.content_hash(), b.content_hash())  # キャッシュはプロファイルごと（v0.4.0 と同じ）
        self.assertEqual(a.harness_hash(), b.harness_hash())

    def test_split_mixing(self):
        a = load_task(write(self.tmp / "a.yaml", task_yaml("a", "split: holdout\n")))
        b = load_task(write(self.tmp / "b.yaml", task_yaml("b", "split: dev\n")))
        c = load_task(write(self.tmp / "c.yaml", task_yaml("c")))
        self.assertIsNone(split_mixing([a]))
        self.assertIsNone(split_mixing([b, c]))
        self.assertIn("holdout", split_mixing([a, b]) or "")
        self.assertIn("holdout", split_mixing([a, c]) or "")  # 分割のないタスクとも混ぜない

    def test_select_pairs_by_split(self):
        ts = [load_task(write(self.tmp / f"{i}.yaml", task_yaml(i, f"split: {s}\n" if s else "")))
              for i, s in (("a", "train"), ("b", "dev"), ("c", None))]
        p = load_profile(write(self.tmp / "p.yaml", "id: p\nagent: claude_code\n"))
        got = lambda splits: sorted(t.id for t, _ in select_pairs(ts, [p], RunOptions(splits=splits)))
        self.assertEqual(got([]), ["a", "b", "c"])
        self.assertEqual(got(["dev"]), ["b"])
        self.assertEqual(got(["train", "dev"]), ["a", "b"])
        self.assertEqual(got(["none"]), ["c"])  # 分割を付けていないタスク


class SignTestTests(unittest.TestCase):
    def test_exact_values(self):
        self.assertEqual(sign_test(0, 0), 1.0)
        self.assertAlmostEqual(sign_test(3, 0), 0.25)        # 2 × (1/2)^3
        self.assertAlmostEqual(sign_test(6, 0), 0.03125)     # 2 × (1/2)^6
        self.assertAlmostEqual(sign_test(5, 1), 0.21875)     # 2 × (1 + 6) / 64
        self.assertEqual(sign_test(2, 2), 1.0)
        self.assertAlmostEqual(sign_test(1, 5), sign_test(5, 1))

    def test_paired_outcomes(self):
        cur = {"a": {"m": 1.0}, "b": {"m": 0.0}, "c": {"m": 0.5}, "d": {"m": None}, "e": {"m": 1.0}}
        base = {"a": {"m": 0.0}, "b": {"m": 1.0}, "c": {"m": 0.5}, "d": {"m": 1.0}, "f": {"m": 1.0}}
        o = paired_outcomes(cur, base, "m")
        self.assertEqual((o["improved"], o["regressed"], o["tied"], o["n"]), (["a"], ["b"], 1, 3))
        self.assertEqual(o["sign_p"], 1.0)


def run_summary(rows, k=1, splits=None, install_empty=None):
    """rows: {(profile, task): [passed, ...]}"""
    rs = [rec(profile_id=p, task_id=t, trial=i, passed=ok, score=1.0 if ok else 0.0, cmp_passed=ok, cmp_score=1.0 if ok else 0.0,
              profile_hash=f"h-{p}", cost_usd=0.01, tokens={"input": 100, "output": 10, "cache_read": 0, "cache_write": 0})
          for (p, t), oks in rows.items() for i, ok in enumerate(oks)]
    s = summarize(rs, k)
    tids = sorted({t for _, t in rows})
    s["meta"] = {"options": {"llm": False}, "tasks": {t: {"hash": "x" + t, "llm": False, "split": (splits or {}).get(t, "holdout"),
                                                         "family": t} for t in tids},
                 "profiles": {p: {"hash": f"h-{p}", "harness_hash": f"hh-{p}", "install_empty": (install_empty or {}).get(p, False)}
                              for p, _ in rows}}
    return s, rs


class GateRegressionBudgetTests(unittest.TestCase):
    def test_max_regressions(self):
        prev, _ = run_summary({("h", f"t{i}"): [True] for i in range(12)})
        cur, _ = run_summary({("h", f"t{i}"): [i != 0] for i in range(12)})
        loose = gate(cur, prev, metric="pass_at_1", margin=0.3, min_tasks=10)
        self.assertEqual(loose["decision"], "pass")  # 1 タスクの悪化はマージン 0.3 の内（v0.4.0 と同じ判定）
        self.assertEqual((loose["profiles"]["h"]["improved"], loose["profiles"]["h"]["regressed"]), (0, 1))
        strict = gate(cur, prev, metric="pass_at_1", margin=0.3, min_tasks=10, max_regressions=0)
        self.assertEqual(strict["decision"], "fail")  # 回帰予算 0 を宣言していれば止める
        self.assertTrue(any("回帰" in p for p in strict["problems"]))
        self.assertEqual(gate(cur, prev, metric="pass_at_1", margin=0.3, min_tasks=10, max_regressions=1)["decision"], "pass")


class PromoteTests(unittest.TestCase):
    def setUp(self):
        self.p = dict(candidate="cand", incumbent="inc", metric="pass_at_1", alpha=0.05, min_improvement=0.0,
                      max_regressions=0, min_tasks=10, comparisons=1, require_split="holdout")

    def build(self, cand, inc, **kw):
        rows = {**{("cand", f"t{i}"): v for i, v in enumerate(cand)}, **{("inc", f"t{i}"): v for i, v in enumerate(inc)}}
        return run_summary(rows, **kw)

    def test_promote_when_clearly_better(self):
        s, rs = self.build([[True]] * 12, [[False]] * 8 + [[True]] * 4)
        r = promote(s, rs, **self.p)
        self.assertEqual(r["decision"], "promote", r)
        self.assertEqual((len(r["improved"]), len(r["regressed"])), (8, 0))
        self.assertLess(r["sign_p"], 0.05)

    def test_reject_on_regression_budget(self):
        s, rs = self.build([[True]] * 11 + [[False]], [[False]] * 8 + [[True]] * 4)
        r = promote(s, rs, **self.p)
        self.assertEqual(r["decision"], "reject")  # 平均は上がっても、宣言した回帰予算 0 を超えた
        self.assertTrue(any("回帰" in x for x in r["problems"]))
        self.assertEqual(promote(s, rs, **{**self.p, "max_regressions": 1})["decision"], "promote")

    def test_reject_when_not_better(self):
        s, rs = self.build([[True]] * 12, [[True]] * 12)
        self.assertEqual(promote(s, rs, **self.p)["decision"], "reject")  # 改善がない

    def test_inconclusive_small_or_corrected(self):
        s, rs = self.build([[True]] * 12, [[False]] * 3 + [[True]] * 9)
        r = promote(s, rs, **self.p)
        self.assertEqual(r["decision"], "inconclusive")  # 3 タスクだけの改善は p = 0.25
        s2, rs2 = self.build([[True]] * 12, [[False]] * 6 + [[True]] * 6)
        self.assertEqual(promote(s2, rs2, **self.p)["decision"], "promote")  # p = 1/32
        r2 = promote(s2, rs2, **{**self.p, "comparisons": 5})
        self.assertEqual(r2["decision"], "inconclusive")  # 5 候補を試した後なら α/5 = 0.01 を下回らない
        self.assertAlmostEqual(r2["alpha_adjusted"], 0.01)
        s3, rs3 = self.build([[True]] * 6, [[False]] * 6)
        self.assertEqual(promote(s3, rs3, **self.p)["decision"], "inconclusive")  # タスクが少ない

    def test_more_regressions_than_improvements_never_promotes(self):
        """符号検定は両側なので、悪化の方が多くても p が小さくなる。回帰予算が緩くても昇格させない。"""
        s, rs = self.build([[True]] * 10 + [[False]] * 20, [[False]] * 10 + [[True]] * 20)
        r = promote(s, rs, **{**self.p, "max_regressions": 1000})
        self.assertNotEqual(r["decision"], "promote")

    def test_cached_trials_are_not_promoted(self):
        s, rs = self.build([[True]] * 12, [[False]] * 8 + [[True]] * 4)
        for r in rs:
            if r.profile_id == "inc":
                r.from_cache = True  # 現行が別の時点の結果
        res = promote(s, rs, **self.p)
        self.assertEqual(res["decision"], "inconclusive")
        self.assertEqual(res["cached_trials"]["inc"], 12)

    def test_task_with_value_on_one_side_is_inconclusive(self):
        """片側だけ値のないタスク（試行がすべて除外された・結果にない）は、悪化を隠しうるので昇格させない。"""
        s, rs = self.build([[True]] * 12, [[False]] * 8 + [[True]] * 5)  # 現行だけが t12 に合格している
        r = promote(s, rs, **self.p)
        self.assertEqual(r["decision"], "inconclusive", r)
        self.assertEqual(r["one_sided_tasks"], ["t12"])
        s2, rs2 = self.build([[True]] * 12, [[False]] * 8 + [[True]] * 4)
        s2["tasks"]["cand"]["t11"]["pass_at_1"] = None  # 候補の試行がすべて環境起因で除外された
        self.assertEqual(promote(s2, rs2, **self.p)["one_sided_tasks"], ["t11"])
        self.assertEqual(promote(s2, rs2, **self.p)["decision"], "inconclusive")

    def test_requires_holdout_split(self):
        s, rs = self.build([[True]] * 12, [[False]] * 8 + [[True]] * 4, splits={"t0": "dev"})
        r = promote(s, rs, **self.p)
        self.assertEqual(r["decision"], "reject")
        self.assertTrue(any("holdout" in x for x in r["problems"]))
        self.assertEqual(promote(s, rs, **{**self.p, "require_split": None})["decision"], "promote")

    def test_missing_profile_and_same_harness(self):
        s, rs = self.build([[True]] * 12, [[False]] * 12)
        self.assertEqual(promote(s, rs, **{**self.p, "candidate": "nope"})["decision"], "reject")
        s["meta"]["profiles"]["cand"]["harness_hash"] = s["meta"]["profiles"]["inc"]["harness_hash"]
        r = promote(s, rs, **self.p)
        self.assertEqual(r["decision"], "reject")
        self.assertTrue(any("同じ" in x for x in r["problems"]))

    def test_hard_category_rejects(self):
        s, rs = self.build([[True]] * 12, [[False]] * 8 + [[True]] * 4)
        s["profiles"]["cand"]["failure_tags"] = {"unsafe_access": 1}
        self.assertEqual(promote(s, rs, **self.p)["decision"], "reject")

    def test_uses_comparable_metric_against_no_harness(self):
        """現行が配布物なし（install が空）なら、配布物がないと通らない判定を両側とも外した値で比べる（Δ と同じ）。"""
        s, rs = self.build([[True]] * 12, [[False]] * 12, install_empty={"inc": True})
        for t in s["tasks"]["inc"].values():
            t["pass_at_1_cmp"] = 1.0  # スキル判定を除けば現行も同じ成果
        self.assertEqual(promote(s, rs, **self.p)["decision"], "reject")
        self.assertTrue(promote(s, rs, **self.p)["comparable_metric"])


class NoiseTests(unittest.TestCase):
    def test_split_half_noise(self):
        stable = [rec(profile_id="p", task_id=f"t{t}", trial=i, passed=True, score=1.0) for t in range(5) for i in range(4)]
        n = split_half_noise(stable, "p")
        self.assertEqual((n["n_tasks"], n["low"], n["high"]), (5, 0.0, 0.0))
        flaky = [rec(profile_id="p", task_id=f"t{t}", trial=i, passed=i % 2 == 0, score=1.0) for t in range(5) for i in range(4)]
        f = split_half_noise(flaky, "p")
        self.assertLess(f["low"], 0.0)
        self.assertGreater(f["high"], 0.0)
        self.assertEqual(split_half_noise(flaky, "p"), f)  # 同じ seed なら同じ
        single = [rec(profile_id="p", task_id="t", trial=0, passed=True, score=1.0)]
        self.assertEqual(split_half_noise(single, "p")["n_tasks"], 0)  # 2 試行未満のタスクは使えない

    def test_noise_is_scaled_to_n_trials_per_side(self):
        """半分どうしの差（各側 h 試行）を、各側 n 試行の比較に換算する（√(h/n)。n が奇数でも過大にしない）。"""
        three = [rec(profile_id="p", task_id="t", trial=i, passed=i != 1, score=1.0) for i in range(3)]
        self.assertAlmostEqual(split_half_noise(three, "p")["high"], (1 / 3) ** 0.5)
        four = [rec(profile_id="p", task_id="t", trial=i, passed=i % 2 == 0, score=1.0) for i in range(4)]
        self.assertAlmostEqual(split_half_noise(four, "p")["high"], 0.5 ** 0.5)
        single = [rec(profile_id="p", task_id="t", trial=0, passed=True, score=1.0)]
        self.assertEqual(split_half_noise(single, "p")["n_tasks"], 0)  # 2 試行未満のタスクは使えない


class HoldoutLedgerTests(unittest.TestCase):
    def test_comparisons(self):
        prof = lambda h, empty=False: {"harness": h, "install_empty": empty}
        rows = [{"tasks": {"a": "1", "b": "2"}, "profiles": {"inc": prof("H0"), "c1": prof("H1"), "base": prof("B", True)}},
                {"tasks": {"a": "1", "b": "2"}, "profiles": {"inc": prof("H0"), "c2": prof("H2")}},
                {"tasks": {"a": "1", "b": "2"}, "profiles": {"inc": prof("H0"), "c1-renamed": prof("H1")}},  # 同じ配布物は 1 回
                {"tasks": {"a": "1"}, "profiles": {"c4": prof("H4")}},  # タスクを 1 つ外した実行も同じ holdout を見ている
                {"tasks": {"z": "9"}, "profiles": {"c3": prof("H3")}},  # 別の holdout
                {"tasks": ["a"], "profiles": {"old": "H5"}}, 1,  # 形式の違う行は読み飛ばす
                {"tasks": {"a": []}, "profiles": {"c5": prof("H5")}},  # JSON としては正しいが型が違う
                {"tasks": {"a": "1"}, "profiles": {"c6": prof(["H6"])}}]
        self.assertEqual(holdout_comparisons(rows, {"a": "1", "b": "2"}, incumbent_hash="H0"), 3)  # H1, H2, H4（基準 B は数えない）
        self.assertEqual(holdout_comparisons(rows, {"a": "changed"}, incumbent_hash="H0"), 0)  # 中身が変わったタスクは別物
        self.assertEqual(holdout_comparisons([], {"a": "1"}, incumbent_hash="H0"), 0)

    def test_ledger_rows_have_no_content(self):
        """台帳には id とハッシュだけを書く（プロンプト・応答を書かない社内規則）。"""
        from harness_eval.runner import holdout_row
        tmp = Path(tempfile.mkdtemp())
        t = load_task(write(tmp / "a.yaml", task_yaml("a", "split: holdout\n").replace("prompt: x", "prompt: SECRET-PROMPT")))
        p = load_profile(write(tmp / "p.yaml", "id: inc\nagent: claude_code\n"))
        row = holdout_row("run1", [t], [p])
        self.assertEqual(set(row), {"at", "run_id", "tasks", "profiles"})
        self.assertEqual(row["tasks"], {"a": t.content_hash()})
        self.assertEqual(row["profiles"], {"inc": {"harness": p.harness_hash(), "install_empty": True}})
        self.assertNotIn("SECRET", json.dumps(row))

    def test_unreadable_ledger_is_an_error(self):
        """読めない台帳を空として扱うと m が 1 になり、補正が効かなくなる。黙って続けず止める。"""
        from harness_eval.cli import _read_jsonl
        tmp = Path(tempfile.mkdtemp())
        self.assertEqual(_read_jsonl(tmp / "none.jsonl"), [])  # まだない台帳は空
        bad = tmp / "bad.jsonl"
        bad.write_bytes(b'{"run_id": "\xff\xfe"}\n')
        with self.assertRaises(SpecError):
            _read_jsonl(bad)


if __name__ == "__main__":
    unittest.main()
