"""suites/code-review-graph（cc-review-tool-eval@b8372b2 の公開評価資産の移植）の自動検査。モデルも CLI も呼ばない。

- verify_defects.py: 12 欠陥が FIRES 9 / LATENT 2 / STATIC 1 に分類され exit 0
- string_contracts.py: fixtures/repo で H クラス 3 件を検出（0 トークン・速い）
- scoring.py: 1 指摘内での照合・相手側アンカー必須・装飾行の許容・移植元と同じ判定（固定の表）
- tasks: FIRES 9 件だけ・lint 警告 0・selftest 通過、examples/tasks と gate-v1 は変わらない
- check_defect_review.py と run_context_recall.py の軽い動作確認（CLI はスタブ）
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness_eval.harvest import selftest  # noqa: E402
from harness_eval.spec import PROCESS_GRADERS, lint, load_profiles, load_tasks  # noqa: E402

SUITE = ROOT / "suites" / "code-review-graph"
CHECKS = SUITE / "checks"
REPO = SUITE / "fixtures" / "repo"
FIRES = ["D01", "D02", "D04", "D05", "D06", "D07", "D08", "D09", "D10"]
TASK_IDS = {f"crg-{d.lower()}" for d in FIRES}
EXAMPLE_IDS = {"csv-summary-format", "csv-summary-skill", "arithmetic-no-skill", "csv-summary-insight", "request-extract",
               "request-ambiguous"}


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"crg_{name}", CHECKS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


scoring = _load("scoring")
string_contracts = _load("string_contracts")
recall = _load("run_context_recall")
TASKS = load_tasks([str(SUITE / "tasks")])


def _run(script: str, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(CHECKS / script), *args], capture_output=True, text=True, encoding="utf-8",
                          env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8", **(env or {})},
                          timeout=60)


class TestDeterministicChecks(unittest.TestCase):
    def test_verify_defects_classification(self):
        r = _run("verify_defects.py")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        got = dict(re.findall(r"^(D\d\d)\s+(FIRES|LATENT|STATIC)\b", r.stdout, re.MULTILINE))
        self.assertEqual(sorted(d for d, v in got.items() if v == "FIRES"), FIRES)
        self.assertEqual(sorted(d for d, v in got.items() if v == "LATENT"), ["D11", "D12"])
        self.assertEqual([d for d, v in got.items() if v == "STATIC"], ["D03"])
        self.assertIn("FIRES 9, LATENT 2, STATIC 1", r.stdout)
        self.assertEqual(got, json.loads((SUITE / "verdicts.json").read_text(encoding="utf-8")))

    def test_string_contracts_finds_the_three_h_class_defects(self):
        found = string_contracts.main(REPO)
        self.assertEqual({w for w, _ in found}, {"app/db.py", "app/api.py", "app/handlers.py"})
        self.assertEqual(len(found), 3)
        text = " ".join(m for _, m in found)
        for token in ("discount_pct", "customerId", "order.created"):
            self.assertIn(token, text)
        r = _run("string_contracts.py", str(REPO))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("3 string-contract finding(s), 0 tokens spent.", r.stdout)

    def test_defect_manifest_matches_scoring_and_verdicts(self):
        defects = json.loads((SUITE / "defects.json").read_text(encoding="utf-8"))["defects"]
        self.assertEqual(len(defects), 12)
        self.assertEqual(set(scoring.MATCH), {d["id"] for d in defects} - {"D03"})
        for d in defects:
            self.assertTrue((REPO / d["changed"]).is_file(), d["changed"])
            if d["id"] in scoring.MATCH:  # ファイル跨ぎ（X/H）だけが相手側アンカーの第 3 グループを持つ
                self.assertEqual(len(scoring.MATCH[d["id"]]) == 3, d["class"] != "L", d["id"])


# 移植元 blind_review.scored と同じ判定になることを固定した表（移植時に元の関数で同じ結果を確認した）
SCORING_TABLE = [
    ("D01", "FINDING: app/pricing.py | bulk discount uses > instead of >= (off-by-one at the threshold)", True),
    ("D01", "FINDING: app/pricing.py | rounding uses int() and truncates", False),
    ("D02", "FINDING: app/utils.py | parse_money raises an unhandled ValueError on malformed input", True),
    ("D04", "FINDING: app/db.py | connection leaks when cursor() raises; close() is skipped", True),
    ("D05", "FINDING: app/pricing.py | compute_total has a new required parameter; create_order in orders.py passes two", True),
    ("D05", "FINDING: app/pricing.py | compute_total has a new required parameter tax_rate", False),
    ("D06", "FINDING: app/repository.py | get_order returns None and get_order_summary dereferences it", True),
    ("D06", "FINDING: app/repository.py | get_order has no Optional[Order] type hint and may return None", False),
    ("D07", "FINDING: app/models.py | discount renamed; order_to_dict still reads .discount (AttributeError)", True),
    ("D08", "FINDING: app/db.py | discount_pct column does not exist in schema.sql (discount_percent)", True),
    ("D08", "FINDING: app/db.py | discount_pct looks suspicious", False),
    ("D09", "FINDING: app/events.py | publishes order.created but handlers subscribe to 'order_created'; never delivered", True),
    ("D10", "FINDING: app/api.py | customerId is camelCase; serializer emits customer_id", True),
    ("D10", "FINDING: app/api.py | customerId is camelCase while the rest is snake_case", False),
    ("D11", "FINDING: app/handlers.py | on_order_created is reached only by getattr in tasks.py", True),
    ("D12", "FINDING: app/config.py | MAX_RETRIES=0 disables retry", True),
    ("D05", "compute_total argument missing in create_order (no FINDING label)", False),
    ("D05", "FINDING: none", False),
]


class TestScoring(unittest.TestCase):
    def test_fixed_table_matches_source_rule(self):
        for did, text, want in SCORING_TABLE:
            with self.subTest(defect=did, text=text):
                self.assertIs(scoring.scored(did, text), want)

    def test_match_must_happen_inside_one_finding(self):
        split = ("FINDING: app/pricing.py | compute_total gained a required parameter\n"
                 "FINDING: app/orders.py | create_order builds the dict by hand")
        self.assertFalse(scoring.scored("D05", split))
        self.assertTrue(scoring.scored("D05", split.replace("\nFINDING:", " and")))

    def test_counterpart_anchor_is_required_for_cross_file(self):
        base = "FINDING: app/pricing.py | compute_total now takes tax_rate, a missing argument for callers"
        self.assertFalse(scoring.scored("D05", base))
        self.assertTrue(scoring.scored("D05", base + " such as create_order"))

    def test_decorated_labels_still_score(self):
        body = "app/pricing.py | compute_total signature changed; orders.py create_order passes two arguments"
        for label in ("**FINDING:**", "- FINDING:", "1. FINDING:", "> finding:", "### FINDING"):
            with self.subTest(label=label):
                self.assertEqual(scoring.findings(f"{label} {body}"), [body])
                self.assertTrue(scoring.scored("D05", f"preamble\n{label} {body}\n"))
        self.assertEqual(scoring.findings("FINDING:\n\nno label here"), [])
        self.assertIsNone(scoring.matched_finding("D05", ""))

    def test_references_score_their_defect(self):
        for did in FIRES:
            with self.subTest(defect=did):
                text = (SUITE / "references" / f"crg-{did.lower()}.final_answer.txt").read_text(encoding="utf-8")
                self.assertTrue(scoring.scored(did, text))


class TestTasks(unittest.TestCase):
    def test_exactly_the_nine_fires_tasks(self):
        self.assertEqual({t.id for t in TASKS}, TASK_IDS)

    def test_task_shape(self):
        for t in TASKS:
            with self.subTest(task=t.id):
                self.assertEqual(t.targets, ["claude_code", "codex"])
                self.assertEqual({g.kind for g in t.graders}, {"outcome", "process"})
                self.assertTrue(any(g.type in PROCESS_GRADERS for g in t.graders))
                self.assertEqual(t.fixture.resolve(), REPO.resolve())
                cmd = next(g for g in t.graders if g.type == "command")
                self.assertTrue(cmd.params["run"].endswith(f"--defect {t.id[4:].upper()}"))
                self.assertTrue(any(g.type == "tool_used" and g.params.get("max") == 0 and "Write" in g.params["tool"]
                                    for g in t.graders))
                self.assertNotIn("llm", {g.type for g in t.graders})

    def test_lint_has_no_warnings(self):
        self.assertEqual(lint(TASKS, load_profiles([str(ROOT / "examples" / "profiles")])), [])

    def test_selftest_passes_for_all_tasks(self):
        for t in TASKS:
            with self.subTest(task=t.id):
                self.assertEqual(selftest(t), "")

    def test_other_trees_are_unchanged(self):
        self.assertEqual({t.id for t in load_tasks([str(ROOT / "examples" / "tasks")])}, EXAMPLE_IDS)
        self.assertEqual(len(load_tasks([str(ROOT / "suites" / "gate-v1" / "tasks")])), 12)


class TestCheckDefectReview(unittest.TestCase):
    def grade(self, final: str, defect: str = "D05", review_md: str | None = None) -> subprocess.CompletedProcess:
        with tempfile.TemporaryDirectory() as tmp:
            rec = Path(tmp) / "record.json"
            rec.write_text(json.dumps({"final_message": final}), encoding="utf-8")
            if review_md is not None:
                (Path(tmp) / "review.md").write_text(review_md, encoding="utf-8")
            return _run("check_defect_review.py", "--defect", defect, env={"TRIAL_RECORD": str(rec), "TRIAL_WORKDIR": tmp})

    def test_pass_fail_and_review_md_fallback(self):
        good = (SUITE / "references" / "crg-d05.final_answer.txt").read_text(encoding="utf-8")
        r = self.grade(good)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("一致", r.stdout)
        self.assertEqual(self.grade("FINDING: app/pricing.py | compute_total looks fine").returncode, 1)
        self.assertEqual(self.grade("", review_md=good).returncode, 0)
        self.assertEqual(self.grade("").returncode, 1)
        self.assertEqual(self.grade(good, defect="D99").returncode, 2)


class TestContextRecall(unittest.TestCase):
    def test_measure_with_stubbed_cli(self):
        """impact は変更ファイルだけを返し、callers_of は compute_total の呼び出し元 orders.py を返すスタブ。"""
        def stub(cmd):
            if cmd[1] == "impact":
                return 0, json.dumps({"impacted_files": [str(REPO / f) for f in cmd[cmd.index("--files") + 1:-2]]}), ""
            if cmd[-1] == "compute_total":
                return 0, json.dumps({"results": [{"file_path": str(REPO / "app" / "orders.py")}]}), ""
            return 0, json.dumps({"results": []}), ""

        defects = recall.load_defects(SUITE / "defects.json")
        result = recall.measure(REPO, defects, cli="stub", run=stub)
        rows = {r["id"]: r for r in result["rows"]}
        self.assertEqual(result["repo_files"], 15)
        self.assertEqual(result["repo"], "repo")  # 絶対パスは記録しない
        self.assertEqual(recall.recall(result["rows"], "full-repo"), 1.0)
        self.assertEqual(recall.recall(result["rows"], "diff-only"), 4 / 12)
        self.assertEqual(recall.recall(result["rows"], "diff-only", "L"), 1.0)
        self.assertFalse(rows["D05"]["graph_hit"])
        self.assertTrue(rows["D05"]["graph+callers_hit"])
        self.assertGreater(rows["D05"]["graph_payload_tokens"], 0)
        self.assertIn("recall by class", recall.report(result))
        self.assertEqual(len(recall.load_defects(SUITE / "defects.json", "FIRES")), 9)

    def test_cli_failure_is_an_error(self):
        with self.assertRaises(RuntimeError):
            recall.graph_context("stub", REPO, ["app/pricing.py"], 2, run=lambda cmd: (1, "", "boom"))


if __name__ == "__main__":
    unittest.main()
