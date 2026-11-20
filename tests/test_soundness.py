"""評価の結論（verdict）と妥当性ガードの単体テスト（モデルは呼ばない）。"""
from __future__ import annotations

import unittest

from harness_eval.judging import blind, majority, model_strength, plan_families, strength_ok
from harness_eval.record import TrialRecord
from harness_eval.soundness import (EXIT_NOT_ADOPTABLE, Rules, cluster_bootstrap, invalid_reason, rules_hash,
                                    verdict)
from harness_eval.spec import SpecError

FAST = {"resamples": 2000}


def rec(task: str, profile: str, passed: bool, trial: int = 0, agent: str = "claude_code", error: dict | None = None,
        valid: bool = True, llm: str | None = None) -> TrialRecord:
    graders = [{"type": "command", "status": "pass" if passed else "fail", "harness_only": False}]
    if llm:
        graders.append({"type": "llm", "status": llm, "harness_only": False})
    return TrialRecord(run_id="r", task_id=task, profile_id=profile, agent=agent, trial=trial, cache_key="k",
                       task_hash="h", profile_hash="p", cli_version=None, passed=passed and valid and error is None,
                       cmp_passed=(passed and error is None) if valid else None, valid=valid, error=error,
                       grader_results=graders, meta={"grader_isolation": "bwrap"})


def runs(spec: dict[str, tuple[int, int]], trials: int, **kw) -> list[TrialRecord]:
    """spec: task -> (ベースラインの合格数, 候補の合格数)。各条件 trials 回。"""
    out = []
    for t, (b, c) in spec.items():
        out += [rec(t, "base", i < b, i, **kw) for i in range(trials)]
        out += [rec(t, "cand", i < c, i, **kw) for i in range(trials)]
    return out


def summary(tasks: dict[str, list[str]] | None = None, judge: dict | None = None, llm: bool = False) -> dict:
    meta = {"tasks": {t: {"tags": tags} for t, tags in (tasks or {}).items()},
            "options": {"llm": llm, "judge_agent": "claude_code", "judge_model": "haiku"}}
    if judge is not None:
        meta["judge"] = judge
    return {"meta": meta}


def rules(**kw) -> Rules:
    return Rules(baseline="base", candidate="cand", **{**FAST, **kw})


STRONG_JUDGE = {"tier": "decisive", "llm": True, "models": {"codex": "gpt-6.2-astra", "claude_code": "opus"},
                "strength": {"codex": "strong", "claude_code": "strong"}, "blind": True, "votes": 3, "seed": 0,
                "plan": {"claude_code": {"families": ["codex", "claude_code"], "same_family": False, "both_families": True}},
                "calibration": {"items": 10, "agree": 10, "rate": 1.0, "pass_items": 5, "fail_items": 5, "agree_pass": 5, "agree_fail": 5}}


class TestConclusions(unittest.TestCase):
    def test_improved_is_adoptable(self):
        v = verdict(runs({f"t{i}": (2, 4) for i in range(8)}, 4), summary(), rules())
        self.assertEqual(v["conclusion"], "IMPROVED", v["reasons"])
        self.assertTrue(v["adoptable"])
        self.assertEqual(v["exit_code"], 0)
        self.assertAlmostEqual(v["baseline_rate"], 0.5)
        self.assertGreater(v["ci"][0], 0)
        self.assertIn("2 段ブートストラップ", v["ci_method"])

    def test_regressed(self):
        v = verdict(runs({f"t{i}": (3, 0) for i in range(8)}, 4), summary(), rules())
        self.assertEqual((v["conclusion"], v["exit_code"]), ("REGRESSED", 1), v["reasons"])

    def test_no_change_when_ci_tight_around_zero(self):
        v = verdict(runs({f"t{i}": (10, 10) for i in range(20)}, 20), summary(), rules())
        self.assertEqual((v["conclusion"], v["exit_code"]), ("NO CHANGE", 4), v["reasons"])

    def test_underpowered_is_inconclusive(self):
        v = verdict(runs({f"t{i}": (1, 1 + (i % 2)) for i in range(5)}, 2), summary(), rules())
        self.assertEqual(v["conclusion"], "INCONCLUSIVE")
        self.assertIn("検出力不足", v["reasons"][0])


class TestGuards(unittest.TestCase):
    def test_ceiling(self):
        # 11/18 の天井の実行と同じ形: ベースラインは全合格、候補は 1 タスクで 0/2
        recs = runs({"a": (2, 2), "b": (2, 2), "c": (2, 0), "d": (2, 2), "e": (2, 2)}, 2)
        v = verdict(recs, summary(), rules())
        self.assertEqual(v["conclusion"], "INCONCLUSIVE")
        self.assertIn("天井", v["reasons"][0])
        self.assertEqual(v["exit_code"], 3)

    def test_floor(self):
        v = verdict(runs({f"t{i}": (0, 4) for i in range(8)}, 4), summary(), rules())
        self.assertEqual(v["conclusion"], "INCONCLUSIVE")
        self.assertIn("床", v["reasons"][0])

    def test_thresholds_are_configurable(self):
        v = verdict(runs({f"t{i}": (0, 4) for i in range(8)}, 4), summary(), rules(floor=0.0))
        self.assertEqual(v["conclusion"], "IMPROVED")

    def test_too_few_tasks(self):
        v = verdict(runs({f"t{i}": (2, 4) for i in range(3)}, 4), summary(), rules())
        self.assertEqual(v["conclusion"], "INCONCLUSIVE")
        self.assertTrue(any("タスク 3 件" in r for r in v["reasons"]))

    def test_too_few_trials(self):
        v = verdict(runs({f"t{i}": (0, 1) for i in range(8)}, 1), summary(), rules(floor=0.0))
        self.assertEqual(v["conclusion"], "INCONCLUSIVE")
        self.assertTrue(any("有効試行" in r for r in v["reasons"]))

    def test_recommended_tasks_warning(self):
        v = verdict(runs({f"t{i}": (2, 4) for i in range(8)}, 4), summary(), rules())
        self.assertEqual({c["id"]: c["status"] for c in v["checks"]}["sample_size"], "warn")

    def test_usage_limit_recorded_as_agent_error_is_invalid(self):
        # 11/19 の Codex 実行: 旧版は利用上限を agent_error（有効な失敗）として数えていた
        msg = "You've hit your usage limit. Upgrade to Pro ... or try again at 3:00 PM."
        r = rec("a", "base", False, error={"type": "agent_error", "message": msg})
        self.assertEqual(invalid_reason(r), "usage_limit")
        recs = runs({f"t{i}": (1, 2) for i in range(8)}, 2)
        for r in recs[: len(recs) // 2]:
            r.error, r.passed, r.cmp_passed = {"type": "agent_error", "message": msg}, False, False
        v = verdict(recs, summary(), rules())
        self.assertEqual(v["conclusion"], "INCONCLUSIVE")
        self.assertIn("usage_limit", v["errors"]["by_type"])
        self.assertIn("エラーで無効", v["reasons"][0])

    def test_one_sided_task(self):
        recs = runs({f"t{i}": (2, 4) for i in range(8)}, 4)
        for r in recs:
            if r.task_id == "t0" and r.profile_id == "cand":
                r.valid, r.error = False, {"type": "rate_limit", "message": "429"}
        v = verdict(recs, summary(), rules(max_error_rate=0.5))
        self.assertEqual(v["conclusion"], "INCONCLUSIVE")
        self.assertTrue(any("片側" in r for r in v["reasons"]))

    def test_control_regression(self):
        recs = runs({f"t{i}": (2, 4) for i in range(8)}, 4) + runs({f"c{i}": (4, 0) for i in range(5)}, 4)
        tags = {f"c{i}": ["control"] for i in range(5)}
        v = verdict(recs, summary(tags), rules(control_tags=("control",)))
        self.assertEqual(v["conclusion"], "REGRESSED")
        self.assertEqual(v["controls"]["tasks"], 5)
        self.assertNotIn("c0", v["target_tasks"])

    def test_control_suspected_regression_is_inconclusive(self):
        recs = runs({f"t{i}": (2, 4) for i in range(8)}, 4) + runs({"c0": (2, 0), "c1": (2, 2)}, 2)
        v = verdict(recs, summary({"c0": ["control"], "c1": ["control"]}), rules(control_tags=("control",)))
        self.assertEqual(v["conclusion"], "INCONCLUSIVE")
        self.assertTrue(any("対照" in r for r in v["reasons"]))

    def test_controls_at_ceiling_do_not_trigger_ceiling(self):
        recs = runs({f"t{i}": (2, 4) for i in range(8)}, 4) + runs({f"c{i}": (4, 4) for i in range(4)}, 4)
        v = verdict(recs, summary({f"c{i}": ["control"] for i in range(4)}), rules(control_tags=("control",)))
        self.assertEqual(v["conclusion"], "IMPROVED", v["reasons"])

    def test_registered_controls_must_be_present_and_usable(self):
        recs = runs({f"t{i}": (2, 4) for i in range(8)}, 4)
        registered = rules(control_tags=("control",))
        missing = verdict(recs, summary(), registered)
        self.assertEqual(missing["conclusion"], "INCONCLUSIVE")
        controls = runs({"c": (4, 4)}, 4, valid=False, error={"type": "agent_error"})
        invalid = verdict(recs + controls, summary({"c": ["control"]}), registered)
        self.assertEqual(invalid["conclusion"], "INCONCLUSIVE")
        self.assertEqual(invalid["controls"]["tasks"], 0)


class TestJudgeGuards(unittest.TestCase):
    def llm_records(self, status="pass", agent="claude_code"):
        return runs({f"t{i}": (2, 4) for i in range(8)}, 4, llm=status, agent=agent)

    def check(self, v, cid):
        return {c["id"]: c["status"] for c in v["checks"]}[cid]

    def test_strong_cross_family_calibrated_is_adoptable(self):
        v = verdict(self.llm_records(), summary(judge=STRONG_JUDGE), rules())
        self.assertEqual((v["conclusion"], v["adoptable"]), ("IMPROVED", True), v["checks"])
        self.assertEqual(self.check(v, "judge_family"), "ok")

    def test_same_family_judge_is_inconclusive(self):
        j = {**STRONG_JUDGE, "plan": {"claude_code": {"families": ["claude_code"], "same_family": True, "both_families": False}}}
        v = verdict(self.llm_records(), summary(judge=j), rules())
        self.assertEqual(v["conclusion"], "INCONCLUSIVE")
        self.assertTrue(any("同系統" in r for r in v["reasons"]))

    def test_low_calibration_agreement_is_inconclusive(self):
        j = {**STRONG_JUDGE, "calibration": {"items": 10, "agree": 6, "rate": 0.6, "pass_items": 5, "fail_items": 5, "agree_pass": 3, "agree_fail": 3}}
        v = verdict(self.llm_records(), summary(judge=j), rules())
        self.assertEqual(v["conclusion"], "INCONCLUSIVE")
        self.assertTrue(any("一致率 60%" in r for r in v["reasons"]))
        v2 = verdict(self.llm_records(), summary(judge=j), rules(min_calibration_agreement=0.5))
        self.assertEqual(v2["conclusion"], "IMPROVED")  # しきい値は事前登録で変えられる

    def test_missing_calibration_is_inconclusive(self):
        j = {**STRONG_JUDGE, "calibration": None}
        v = verdict(self.llm_records(), summary(judge=j), rules())
        self.assertEqual(v["conclusion"], "INCONCLUSIVE")
        self.assertTrue(any("校正" in r for r in v["reasons"]))

    def test_no_llm_needs_no_calibration(self):
        v = verdict(runs({f"t{i}": (2, 4) for i in range(8)}, 4), summary(judge={**STRONG_JUDGE, "calibration": None}), rules())
        self.assertEqual(v["conclusion"], "IMPROVED")
        self.assertIn("校正は不要", {c["id"]: c["message"] for c in v["checks"]}["calibration"])

    def test_weak_judge_is_marked_and_not_adoptable(self):
        j = {**STRONG_JUDGE, "models": {**STRONG_JUDGE["models"], "codex": "gpt-6.2-mini"}, "weak_allowed": True}
        v = verdict(self.llm_records(), summary(judge=j), rules())
        self.assertEqual(v["conclusion"], "IMPROVED")
        self.assertFalse(v["adoptable"])
        self.assertEqual(v["exit_code"], EXIT_NOT_ADOPTABLE)
        self.assertIn("暫定", v["label"])
        self.assertTrue(any("弱い判定役" in r for r in v["not_adoptable_reasons"]))

    def test_dev_tier_is_provisional(self):
        j = {**STRONG_JUDGE, "tier": "dev", "models": {"codex": "gpt-6.2-sol"}, "votes": 1,
             "plan": {"claude_code": {"families": ["codex"], "same_family": False, "both_families": False}}}
        v = verdict(self.llm_records(), summary(judge=j), rules())
        self.assertEqual((v["conclusion"], v["adoptable"], v["exit_code"]), ("IMPROVED", False, EXIT_NOT_ADOPTABLE))
        self.assertIn("暫定", v["label"])
        self.assertTrue(any("dev 段階" in r for r in v["not_adoptable_reasons"]))
        self.assertEqual(verdict(self.llm_records(), summary(judge=j), rules(required_tier="dev"))["exit_code"], EXIT_NOT_ADOPTABLE)

    def test_dev_tier_without_llm_decisions_does_not_matter(self):
        # LLM 判定が合否を決めていなければ（決定的な採点器だけ）、判定役の段階は結論に効かない
        j = {**STRONG_JUDGE, "tier": "dev", "models": {"codex": "gpt-6.2-sol"}, "votes": 1}
        v = verdict(runs({f"t{i}": (2, 4) for i in range(8)}, 4), summary(judge=j), rules())
        self.assertEqual((v["conclusion"], v["adoptable"], v["exit_code"]), ("IMPROVED", True, 0))

    def test_decisive_with_mid_model_or_single_vote_cannot_be_adopted(self):
        for overrides in ({"models": {**STRONG_JUDGE["models"], "codex": "gpt-6.2-sol"}}, {"votes": 1},
                          {"tier": None}, {"tier": "bogus"}):
            with self.subTest(overrides=overrides):
                v = verdict(self.llm_records(), summary(judge={**STRONG_JUDGE, **overrides}), rules())
                self.assertEqual(v["exit_code"], EXIT_NOT_ADOPTABLE)

    def test_missing_family_route_is_inconclusive(self):
        v = verdict(self.llm_records(), summary(judge={**STRONG_JUDGE, "plan": {}}), rules())
        self.assertEqual(v["conclusion"], "INCONCLUSIVE")
        self.assertEqual(self.check(v, "judge_route"), "fail")

    def test_legacy_haiku_unblinded_same_family(self):
        # 旧版: 判定役 haiku（claude_code）・盲検化なし。評価対象も claude_code
        v = verdict(self.llm_records(), summary(llm=True), rules())
        self.assertEqual(v["conclusion"], "INCONCLUSIVE")
        self.assertEqual(self.check(v, "blind"), "provisional")
        self.assertEqual(self.check(v, "judge_strength"), "provisional")

    def test_disagreement_counts_as_not_passed(self):
        # 記録の合否が合格のままでも（旧い記録・集計の誤り）、判定不一致の試行は合格として数えない
        recs = self.llm_records(status="judge_disagreement")
        self.assertTrue(all(r.passed for r in recs if r.profile_id == "cand" and r.cmp_passed))
        v = verdict(recs, summary(judge=STRONG_JUDGE), rules())
        self.assertEqual(v["candidate_rate"], 0.0)
        self.assertNotEqual(v["conclusion"], "IMPROVED")
        self.assertEqual(v["judge_disagreements"], len(recs))
        self.assertEqual(self.check(v, "judge_disagreement"), "warn")


class TestPrereg(unittest.TestCase):
    def test_registered_and_post_hoc(self):
        recs = runs({f"t{i}": (2, 4) for i in range(8)}, 4)
        reg_rules = Rules(baseline="base", **FAST)
        prereg = {"rules": reg_rules.to_dict(), "rules_hash": reg_rules.hash(), "registered_at": "t0"}
        ok = verdict(recs, summary(), rules(), prereg)
        self.assertEqual(ok["prereg"]["status"], "registered")
        self.assertTrue(ok["adoptable"])
        changed = verdict(recs, summary(), rules(ceiling=0.9), prereg)
        self.assertEqual(changed["prereg"]["status"], "post_hoc")
        self.assertEqual(changed["prereg"]["changed"], ["ceiling"])
        self.assertFalse(changed["adoptable"])
        self.assertEqual(changed["exit_code"], EXIT_NOT_ADOPTABLE)
        tampered = verdict(recs, summary(), rules(), {**prereg, "rules_hash": "invalid"})
        self.assertEqual(tampered["prereg"]["status"], "invalid")
        self.assertEqual(tampered["conclusion"], "INCONCLUSIVE")

    def test_no_prereg_is_reported(self):
        v = verdict(runs({f"t{i}": (2, 4) for i in range(8)}, 4), summary(), rules())
        self.assertEqual(v["prereg"]["status"], "none")

    def test_hash_is_stable_and_rules_validated(self):
        self.assertEqual(Rules(baseline="b").hash(), Rules(baseline="b").hash())
        self.assertNotEqual(Rules(baseline="b").hash(), Rules(baseline="b", floor=0.1).hash())
        self.assertEqual(rules_hash(Rules().to_dict()), Rules().hash())
        with self.assertRaises(SpecError):
            Rules(floor=0.9, ceiling=0.5)
        with self.assertRaises(SpecError):
            Rules.from_dict({"bogus": 1})
        self.assertEqual(Rules.from_dict(Rules(control_tags=("c",)).to_dict()).control_tags, ("c",))

    def test_missing_profile(self):
        with self.assertRaises(SpecError):
            verdict(runs({"a": (1, 1)}, 2), summary(), Rules(baseline="base", candidate="nope"))


class TestJudging(unittest.TestCase):
    def test_strength(self):
        self.assertEqual([model_strength(m) for m in ("opus", "claude-opus-5-5", "gpt-6.2-astra", "sonnet", "gpt-6.2-sol", "haiku", None)],
                         ["strong", "strong", "strong", "mid", "mid", "weak", "weak"])
        self.assertTrue(strength_ok("sonnet", "dev"))
        self.assertFalse(strength_ok("sonnet", "decisive"))

    def test_cross_family_selection(self):
        both = plan_families(["claude_code", "codex"], ["claude_code", "codex"], "decisive")
        self.assertEqual(both["claude_code"]["families"], ["codex", "claude_code"])
        self.assertEqual(both["codex"]["families"], ["claude_code", "codex"])
        self.assertTrue(both["codex"]["both_families"])
        dev = plan_families(["claude_code"], ["claude_code", "codex"], "dev")
        self.assertEqual(dev["claude_code"]["families"], ["codex"])
        only = plan_families(["claude_code"], ["claude_code"], "decisive")
        self.assertEqual((only["claude_code"]["families"], only["claude_code"]["same_family"]), (["claude_code"], True))
        forced = plan_families(["codex"], ["claude_code", "codex"], "decisive", forced="codex")
        self.assertTrue(forced["codex"]["same_family"])

    def test_blind_masks_identity(self):
        text = ("claude-verify-layer の結果。/tmp/run/abc/workdir/out.md に保存。CLAUDE.md のルールと skills/verify/SKILL.md に従い、"
                "Claude Code (opus) で実行。codex-baseline と比較。gpt-6.2-astra も Anthropic も OpenAI も。")
        out = blind(text, terms=["claude-verify-layer", "codex-baseline", "verify"], paths=["/tmp/run/abc/workdir"])
        for leak in ("claude-verify-layer", "codex-baseline", "/tmp/run", "CLAUDE.md", "SKILL.md", "Claude", "opus",
                     "gpt-6.2-astra", "Anthropic", "OpenAI", "verify"):
            self.assertNotIn(leak.lower(), out.lower(), out)
        self.assertIn("結果", out)

    def test_majority(self):
        self.assertEqual(majority(["pass", "pass", "fail"]), "pass")
        self.assertEqual(majority(["pass", "fail"]), "unknown")
        self.assertEqual(majority(["fail"]), "fail")

    def test_cluster_bootstrap_deterministic(self):
        pairs = [([True, False], [True, True])] * 5
        self.assertEqual(cluster_bootstrap(pairs, 0.95, 500, 1), cluster_bootstrap(pairs, 0.95, 500, 1))
        self.assertEqual(cluster_bootstrap([], 0.95, 10, 0), (None, None, None))


if __name__ == "__main__":
    unittest.main()
