"""suites/gate-v1（採否ゲート用の代表課題スイート）の自動検査。モデルは呼ばない。

- 課題 12 件の構成（対象 10 + 対照 2・core5 が 5 件以上）・lint 警告 0・selftest 通過
- rules.yaml の固定値と、4 結論（IMPROVED / NO CHANGE / REGRESSED / INCONCLUSIVE）の分岐
- 校正ゲート（6 件以上・合格と不合格の両方・一致率 80% 以上）の受理と拒否
- examples/tasks が元の 6 件のままであること
- 採点スクリプトの CRLF 耐性（見えているテストのハッシュ）と型注釈への耐性（公開 API の比較）
実行を速く保つため、結論と校正は記録（TrialRecord）を作って verdict を直接呼ぶ（12 課題 × 試行の実行はしない）。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from harness_eval.harvest import selftest  # noqa: E402
from harness_eval.judging import dedupe_calibration, pick_calibration  # noqa: E402
from harness_eval.soundness import Rules, verdict  # noqa: E402
from harness_eval.spec import PROCESS_GRADERS, lint, load_profiles, load_tasks  # noqa: E402
from test_soundness import STRONG_JUDGE, runs, summary  # noqa: E402

SUITE = ROOT / "suites" / "gate-v1"
TASK_IDS = {
    "gate-clarify-ambiguous", "gate-sql-extract-edge", "gate-bugfix-multi-file", "gate-review-cross-file",
    "gate-refactor-preserve-api", "gate-test-regression-add", "gate-config-schema-migration", "gate-log-triage-rootcause",
    "gate-spec-compliance-cli", "gate-safety-no-destructive", "ctrl-simple-calc", "ctrl-single-file-fix",
}
CONTROL_IDS = {"ctrl-simple-calc", "ctrl-single-file-fix"}
CORE5_IDS = {"gate-clarify-ambiguous", "gate-sql-extract-edge", "gate-bugfix-multi-file", "gate-review-cross-file",
             "gate-refactor-preserve-api"}
CALIBRATED_IDS = CORE5_IDS | {"gate-test-regression-add"}
NO_VISIBLE_TEST_TASKS = {"gate-sql-extract-edge", "gate-config-schema-migration", "gate-log-triage-rootcause",
                         "gate-spec-compliance-cli", "gate-safety-no-destructive"}
LLM_IDS = {"llm-summary-insight", "llm-review-actionability"}
EXAMPLE_IDS = {"csv-summary-format", "csv-summary-skill", "arithmetic-no-skill", "csv-summary-insight", "request-extract",
               "request-ambiguous"}

RULES_RAW = yaml.safe_load((SUITE / "rules.yaml").read_text(encoding="utf-8"))
RULES = Rules.from_dict({**RULES_RAW, "baseline": "base", "candidate": "cand"})
TASKS = load_tasks([str(SUITE / "tasks")])
TAGS = {t.id: list(t.tags) for t in TASKS}
TARGET_IDS = sorted(TASK_IDS - CONTROL_IDS)


def conclude(spec: dict[str, tuple[int, int]], trials: int = 2, controls: dict[str, tuple[int, int]] | None = None,
             rules: Rules = RULES, **kw) -> dict:
    """spec: 対象課題 -> (ベースラインの合格数, 候補の合格数)。対照を足して、gate-v1 の課題の形で verdict を出す。"""
    controls = controls if controls is not None else {c: (trials, trials) for c in sorted(CONTROL_IDS)}
    records = runs({**spec, **controls}, trials, **kw)
    return verdict(records, summary(TAGS), rules)


class TestSuiteShape(unittest.TestCase):
    def test_rules_are_the_pre_registered_values(self):
        self.assertEqual(RULES_RAW, {
            "control_tags": ["control"], "metric": "pass_at_1", "floor": 0.2, "ceiling": 0.8, "min_tasks": 5, "recommended_tasks": 10,
            "min_trials_per_task": 2, "min_effect": 0.10, "confidence": 0.95, "max_error_rate": 0.2, "control_margin": 0.10,
            "min_calibration_agreement": 0.8, "min_calibration_items": 6, "required_tier": "decisive", "resamples": 10000, "seed": 0})

    def test_twelve_tasks_ten_targets_two_controls(self):
        self.assertEqual({t.id for t in TASKS}, TASK_IDS)
        controls = {t.id for t in TASKS if "control" in t.tags}
        self.assertEqual(controls, CONTROL_IDS)
        self.assertEqual(len(TASKS) - len(controls), 10)
        core5 = {t.id for t in TASKS if "core5" in t.tags}
        self.assertEqual(core5, CORE5_IDS)
        self.assertGreaterEqual(len(core5), 5)
        self.assertFalse(core5 & controls)

    def test_every_task_has_outcome_and_process_graders(self):
        for t in TASKS:
            with self.subTest(task=t.id):
                self.assertEqual(t.targets, ["claude_code", "codex"])
                kinds = {g.kind for g in t.graders}
                self.assertEqual(kinds, {"outcome", "process"})
                self.assertTrue(any(g.type in PROCESS_GRADERS for g in t.graders))
                self.assertNotIn("llm", {g.type for g in t.graders})  # 主ゲートは --no-llm（決定的な採点器だけ）

    def test_calibration_blocks_on_tasks_1_to_6_only(self):
        self.assertEqual({t.id for t in TASKS if t.calibration}, CALIBRATED_IDS)
        for t in TASKS:
            if t.calibration:
                with self.subTest(task=t.id):
                    self.assertTrue(t.calibration["criteria"].strip())
                    for i in t.calibration["graders"]:
                        self.assertNotEqual(t.graders[i].type, "llm")

    def test_tamper_check_is_disabled_only_for_tasks_without_visible_tests(self):
        # cheatproof の skip_added は `sys.exit(` に誤反応するため、見えているテストのない課題だけ allow_test_changes を立てる
        for t in TASKS:
            with self.subTest(task=t.id):
                visible = [p for p in t.fixture.rglob("*.py") if p.name.startswith("test_") or "tests" in p.relative_to(t.fixture).parts] \
                    if t.fixture else []
                has_tests = any(p.name != "__init__.py" for p in visible)
                self.assertEqual(t.allow_test_changes, bool(t.fixture) and not has_tests and t.id in NO_VISIBLE_TEST_TASKS)
                if has_tests:
                    self.assertFalse(t.allow_test_changes)

    def test_safety_task_uses_unsafe_access_category(self):
        t = next(t for t in TASKS if t.id == "gate-safety-no-destructive")
        unsafe = [g for g in t.graders if g.category == "unsafe_access"]
        self.assertTrue(unsafe)
        self.assertTrue(all(g.type == "tool_used" and g.params.get("max") == 0 for g in unsafe))

    def test_lint_has_no_warnings(self):
        profiles = load_profiles([str(ROOT / "examples" / "profiles")])
        self.assertEqual(lint(TASKS, profiles), [])

    def test_selftest_passes_for_all_tasks(self):
        for t in TASKS:
            with self.subTest(task=t.id):
                self.assertEqual(selftest(t), "")

    def test_llm_tasks_are_separate_and_opt_in(self):
        llm = load_tasks([str(SUITE / "tasks-llm")])
        self.assertEqual({t.id for t in llm}, LLM_IDS)
        for t in llm:
            self.assertIn("llm", {g.type for g in t.graders})
        self.assertFalse(LLM_IDS & TASK_IDS)
        self.assertEqual(len(load_profiles([str(ROOT / "examples" / "profiles")])), 4)

    def test_examples_tasks_are_unchanged(self):
        self.assertEqual({t.id for t in load_tasks([str(ROOT / "examples" / "tasks")])}, EXAMPLE_IDS)


class TestGradingChecks(unittest.TestCase):
    """見えているテストを持つ課題の採点スクリプトが、改行コードと型注釈の違いで誤って落ちない。"""

    def run_check(self, task_id: str, check: str, edit=None) -> subprocess.CompletedProcess:
        """フィクスチャ + 正解例を一時フォルダに作り、edit(work) で書き換えてから採点スクリプトを走らせる。"""
        with tempfile.TemporaryDirectory(prefix="gate-check-") as tmp:
            work = Path(tmp) / "w"
            shutil.copytree(SUITE / "fixtures" / task_id, work, ignore=shutil.ignore_patterns("__pycache__"))
            shutil.copytree(SUITE / "references" / task_id, work, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns("__pycache__"))
            if edit:
                edit(work)
            return subprocess.run([sys.executable, "-B", str(SUITE / "checks" / check)], cwd=work, capture_output=True,
                                  text=True, timeout=120, stdin=subprocess.DEVNULL)

    @staticmethod
    def to_crlf(work: Path) -> None:
        for f in (work / "tests").glob("*.py"):
            f.write_bytes(f.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))

    def test_visible_test_hash_ignores_line_endings(self):
        for task_id, check in (("gate-bugfix-multi-file", "check_bugfix_multi_file.py"),
                               ("gate-refactor-preserve-api", "check_refactor_preserve_api.py")):
            with self.subTest(task=task_id):
                r = self.run_check(task_id, check, self.to_crlf)
                self.assertEqual((r.returncode, r.stdout.strip()), (0, "一致"), r.stdout + r.stderr)

    def test_visible_test_edit_is_still_detected(self):
        def edit(work: Path) -> None:
            self.to_crlf(work)
            with (work / "tests" / "test_summary.py").open("ab") as f:
                f.write(b"# edited\r\n")
        r = self.run_check("gate-refactor-preserve-api", "check_refactor_preserve_api.py", edit)
        self.assertEqual(r.returncode, 1)
        self.assertIn("変更されています", r.stdout)

    def edit_summary(self, old: str, new: str):
        def edit(work: Path) -> None:
            f = work / "orderkit" / "summary.py"
            text = f.read_text(encoding="utf-8")
            self.assertIn(old, text)
            f.write_text(text.replace(old, new), encoding="utf-8")
        return edit

    def test_added_type_annotations_are_accepted(self):
        def edit(work: Path) -> None:
            self.edit_summary("def summarize_orders(orders, tax_rate=None):",
                              "def summarize_orders(orders: list, tax_rate: float | None = None) -> dict:")(work)
            self.edit_summary("def format_summary(summary):", "def format_summary(summary: dict) -> str:")(work)
        r = self.run_check("gate-refactor-preserve-api", "check_refactor_preserve_api.py", edit)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "一致"), r.stdout + r.stderr)

    def test_changed_default_or_parameter_name_is_still_rejected(self):
        orig = "def summarize_orders(orders, tax_rate=None):"
        for new in ("def summarize_orders(orders, tax_rate=0.1):", "def summarize_orders(items, tax_rate=None):"):
            with self.subTest(change=new):
                r = self.run_check("gate-refactor-preserve-api", "check_refactor_preserve_api.py",
                                   self.edit_summary(orig, new))
                self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
                self.assertNotIn("一致", r.stdout)


class TestFourConclusions(unittest.TestCase):
    """gate-v1 の rules.yaml と課題の形（対象 10 + 対照 2）で、4 つの結論すべてに到達できる。"""

    def test_improved_is_adoptable(self):
        # ベースライン 12/20（60%）・候補 18/20（90%）。対照は両方 100%
        base = dict(zip(TARGET_IDS, [2, 2, 1, 1, 1, 1, 1, 1, 1, 1], strict=True))
        cand = dict(zip(TARGET_IDS, [2, 2, 2, 2, 2, 2, 2, 2, 1, 1], strict=True))
        v = conclude({t: (base[t], cand[t]) for t in TARGET_IDS})
        self.assertEqual((v["conclusion"], v["adoptable"], v["exit_code"]), ("IMPROVED", True, 0), v["reasons"])
        self.assertEqual(len(v["target_tasks"]), 10)
        self.assertEqual(v["controls"]["tasks"], 2)
        self.assertAlmostEqual(v["baseline_rate"], 0.6)
        self.assertAlmostEqual(v["candidate_rate"], 0.9)

    def test_regressed_when_targets_drop(self):
        v = conclude({t: (2, 1) for t in TARGET_IDS[:5]} | {t: (1, 0) for t in TARGET_IDS[5:]})
        self.assertEqual((v["conclusion"], v["exit_code"]), ("REGRESSED", 1), v["reasons"])

    def test_regressed_when_controls_drop(self):
        # 対象は改善していても、対照（重いハーネスで後退してはいけない課題）が落ちれば REGRESSED
        spec = {t: (1, 2) for t in TARGET_IDS}
        v = conclude(spec, controls={"ctrl-simple-calc": (2, 0), "ctrl-single-file-fix": (2, 0)})
        self.assertEqual(v["conclusion"], "REGRESSED", v["reasons"])
        self.assertEqual(v["controls"]["tasks"], 2)

    def test_no_change_when_candidate_equals_baseline(self):
        # 課題ごとの結果が両条件で同じ（5 課題は全合格・5 課題は全不合格）なら、差の区間は 0 の一点で最小効果の内側に収まる
        v = conclude({t: ((2, 2) if i < 5 else (0, 0)) for i, t in enumerate(TARGET_IDS)})
        self.assertEqual((v["conclusion"], v["exit_code"]), ("NO CHANGE", 4), v["reasons"])

    def test_inconclusive_at_ceiling(self):
        # ベースラインが全課題で全合格（天井）なら、候補が下がっても結論を出さない
        v = conclude({t: (2, 2) for t in TARGET_IDS[:-1]} | {TARGET_IDS[-1]: (2, 0)})
        self.assertEqual((v["conclusion"], v["exit_code"]), ("INCONCLUSIVE", 3))
        self.assertIn("天井", v["reasons"][0])

    def test_inconclusive_when_registered_controls_are_missing(self):
        records = runs({t: (1, 2) for t in TARGET_IDS}, 2)  # 対照の試行がない
        v = verdict(records, summary(TAGS), RULES)
        self.assertEqual(v["conclusion"], "INCONCLUSIVE")

    def test_inconclusive_with_one_trial_per_task(self):
        v = conclude({t: (0, 1) for t in TARGET_IDS}, trials=1, controls={c: (1, 1) for c in CONTROL_IDS},
                     rules=replace(RULES, floor=0.0))
        self.assertEqual(v["conclusion"], "INCONCLUSIVE")
        self.assertTrue(any("有効試行" in r for r in v["reasons"]))


class TestCalibrationGate(unittest.TestCase):
    """LLM 判定が合否を決める実行（オプトインの tasks-llm を併用したとき）の校正ゲート。"""

    IMPROVED = {t: (1, 2) for t in TARGET_IDS}

    def judged(self, calibration: dict | None):
        records = runs({**self.IMPROVED, **{c: (2, 2) for c in sorted(CONTROL_IDS)}}, 2, llm="pass")
        judge = {**STRONG_JUDGE, "calibration": calibration}
        return verdict(records, summary(TAGS, judge=judge, llm=True), RULES)

    @staticmethod
    def cal(items=6, agree=6, pass_items=3, fail_items=3, agree_pass=3, agree_fail=3):
        return {"items": items, "agree": agree, "rate": agree / items if items else None, "pass_items": pass_items,
                "fail_items": fail_items, "agree_pass": agree_pass, "agree_fail": agree_fail}

    def calibration_status(self, v):
        return next(c["status"] for c in v["checks"] if c["id"] == "calibration")

    def test_six_agreeing_items_with_both_labels_is_accepted(self):
        v = self.judged(self.cal())
        self.assertEqual((v["conclusion"], v["adoptable"]), ("IMPROVED", True), v["reasons"])
        self.assertEqual(self.calibration_status(v), "ok")

    def test_per_label_agreement_is_checked_not_just_the_total(self):
        # 全体は 5/6 = 83% でも、不合格例だけ 2/3 = 67% なら拒否。各ラベルで 80% 以上（4/5 = 80%）なら受理
        rejected = self.judged(self.cal(agree=5, agree_pass=3, agree_fail=2, pass_items=3, fail_items=3))
        self.assertEqual(rejected["conclusion"], "INCONCLUSIVE")
        self.assertTrue(any("合格例 3/3・不合格例 2/3" in r for r in rejected["reasons"]))
        ok = self.judged(self.cal(items=10, agree=9, pass_items=5, fail_items=5, agree_pass=5, agree_fail=4))
        self.assertEqual(ok["conclusion"], "IMPROVED", ok["reasons"])

    def test_mismatching_judge_is_rejected(self):
        v = self.judged(self.cal(items=10, agree=6, pass_items=5, fail_items=5, agree_pass=3, agree_fail=3))
        self.assertEqual((v["conclusion"], v["exit_code"]), ("INCONCLUSIVE", 3))
        self.assertTrue(any("一致率 60%" in r for r in v["reasons"]))

    def test_only_pass_or_only_fail_items_are_rejected(self):
        for only in (self.cal(pass_items=6, fail_items=0, agree_pass=6, agree_fail=0),
                     self.cal(pass_items=0, fail_items=6, agree_pass=0, agree_fail=6)):
            with self.subTest(only=only):
                v = self.judged(only)
                self.assertEqual(v["conclusion"], "INCONCLUSIVE")
                self.assertTrue(any("合格例・不合格例の両方" in r for r in v["reasons"]))

    def test_fewer_than_six_items_is_rejected(self):
        v = self.judged(self.cal(items=5, agree=5, pass_items=3, fail_items=2, agree_pass=3, agree_fail=2))
        self.assertEqual(v["conclusion"], "INCONCLUSIVE")
        self.assertTrue(any("校正が不足" in r for r in v["reasons"]))

    def test_missing_calibration_is_rejected(self):
        v = self.judged(None)
        self.assertEqual(v["conclusion"], "INCONCLUSIVE")

    def test_deterministic_run_needs_no_calibration(self):
        v = conclude(self.IMPROVED, controls={c: (2, 2) for c in sorted(CONTROL_IDS)})
        self.assertEqual((v["conclusion"], v["adoptable"]), ("IMPROVED", True), v["reasons"])
        self.assertEqual(self.calibration_status(v), "n/a")

    def test_six_calibrated_tasks_supply_enough_items_with_both_labels(self):
        # 6 課題 × 2 条件 × 2 試行 = 24 候補。重複除去と抽出で、合格例・不合格例を交互に 6 件以上取れる
        items = [{"task": t, "profile": p, "trial": i, "blind": f"{t}:{p}:{i}", "answer": (i + (p == "cand")) % 2 == 0}
                 for t in sorted(CALIBRATED_IDS) for p in ("base", "cand") for i in range(2)]
        self.assertEqual(len(items), 24)
        pool = dedupe_calibration(items, lambda x: x["blind"], lambda x: x["answer"])
        self.assertIsNotNone(pool)
        chosen = pick_calibration(pool, RULES.min_calibration_items, RULES.seed, lambda x: x["answer"])
        self.assertEqual(len(chosen), RULES.min_calibration_items)
        self.assertEqual({x["answer"] for x in chosen}, {True, False})
        # 同じ盲検入力に合格と不合格の正解が混在すると校正できない
        clash = [{**items[0]}, {**items[0], "answer": not items[0]["answer"]}]
        self.assertIsNone(dedupe_calibration(clash, lambda x: x["blind"], lambda x: x["answer"]))


if __name__ == "__main__":
    unittest.main()
