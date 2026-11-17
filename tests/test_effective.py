"""0.9.0: 作業ディレクトリの信頼・有効設定の記録・利用上限・発火と反復の指標のテスト。

偽の CLI（tests/fake_agents）は、Claude Code 2.1.325・codex-cli 0.169.0 で実測した挙動（docs/design/REDESIGN.md §2）を再現する:
- Claude Code: init にユーザー・プロジェクトのスキル・サブエージェント・MCP を並べ、未信頼ならプロジェクトの permissions.allow を捨てて警告
- Codex: `debug prompt-input`・`mcp list --json` で読み込みを出し、未信頼なら作業ディレクトリの .codex/config.toml を読まない
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness_eval import effective  # noqa: E402
from harness_eval.adapters import ClaudeAdapter, CodexAdapter, _classify_message, reset_hint  # noqa: E402
from harness_eval.metrics import harness_load, pass_at_k, summarize, trigger_stats, wilson  # noqa: E402
from harness_eval.record import EXCLUDED_ERRORS, TrialRecord  # noqa: E402
from harness_eval.spec import Grader  # noqa: E402
from tests import test_e2e  # noqa: E402
from tests.test_e2e import FAKE, run_cli  # noqa: E402

USAGE_MSG = ("You’ve hit your usage limit. Upgrade to Pro (https://chatgpt.com/explore/pro), visit https://chatgpt.com/settings/usage "
             "to purchase more credits or try again at Nov 19th, 2026 9:03 AM.")


def _skill(base: Path, name: str, fm_name: str | None = None) -> None:
    d = base / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\nname: {fm_name or name}\ndescription: d\n---\nbody\n", encoding="utf-8")


class TrustTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = self.tmp / "home" / ".claude"
        self.wd = self.tmp / "workdir"
        self.cfg.mkdir(parents=True)
        self.wd.mkdir()

    def test_claude_merges_and_trusts_workdir_and_ancestors(self):
        (self.cfg / ".claude.json").write_text(json.dumps({"mcpServers": {"m": {"command": "x"}},
                                                          "projects": {"/other": {"allowedTools": ["Read"]}}}), encoding="utf-8")
        paths = effective.seed_trust("claude_code", self.cfg, self.wd)
        data = json.loads((self.cfg / ".claude.json").read_text(encoding="utf-8"))
        self.assertEqual(data["mcpServers"], {"m": {"command": "x"}})  # install が置いた設定は残る
        self.assertEqual(data["projects"]["/other"], {"allowedTools": ["Read"]})
        wd = self.wd.resolve()
        # 信頼は git のルート単位なので、作業ディレクトリが親のリポジトリの中でも効くよう祖先にも付ける（実測）
        for p in (wd, wd.parent, Path(wd.anchor)):
            self.assertTrue(data["projects"][p.as_posix()]["hasTrustDialogAccepted"], p)
            self.assertIn(p.as_posix(), paths)
        self.assertEqual(effective.seed_trust("claude_code", self.cfg, self.wd), paths)  # 繰り返しても同じ

    def test_claude_invalid_json_is_a_harness_error(self):
        (self.cfg / ".claude.json").write_text("{broken", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            effective.seed_trust("claude_code", self.cfg, self.wd)

    def test_codex_appends_project_table_once(self):
        (self.cfg / "config.toml").write_text('model = "x"', encoding="utf-8")  # 末尾に改行がない
        self.assertEqual(effective.seed_trust("codex", self.cfg, self.wd), [str(self.wd.resolve())])
        effective.seed_trust("codex", self.cfg, self.wd)
        text = (self.cfg / "config.toml").read_text(encoding="utf-8")
        header = f"[projects.{json.dumps(str(self.wd.resolve()))}]"
        self.assertEqual(text.count(header), 1)
        self.assertTrue(text.startswith('model = "x"\n'))
        self.assertIn('trust_level = "trusted"', text)


class ExpectedObservedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.home = self.tmp / "home"
        self.wd = self.tmp / "workdir"
        self.wd.mkdir()

    def test_claude_expected_parts(self):
        cfg = self.home / ".claude"
        _skill(cfg / "skills", "dir-name", "fm-name")
        _skill(self.wd / ".claude" / "skills", "proj")
        (cfg / "agents").mkdir()
        (cfg / "agents" / "rev.md").write_text("---\nname: reviewer\n---\n", encoding="utf-8")
        (cfg / ".claude.json").write_text(json.dumps({"mcpServers": {"user-mcp": {}}}), encoding="utf-8")
        (self.wd / ".mcp.json").write_text(json.dumps({"mcpServers": {"proj-mcp": {}}}), encoding="utf-8")
        (cfg / "CLAUDE.md").write_text("\n\n  Always answer in Japanese.\nmore\n", encoding="utf-8")
        exp = effective.expected_parts("claude_code", self.home, cfg, self.wd, trusted=True)
        self.assertEqual(exp["skills"], [("fm-name", "dir-name"), ("proj", "proj")])
        self.assertEqual(exp["agents"], [("reviewer", "rev")])
        self.assertEqual([n for n, _ in exp["mcp_servers"]], ["user-mcp", "proj-mcp"])
        self.assertEqual(exp["instructions"], [("home/.claude/CLAUDE.md", "Always answer in Japanese.")])
        obs = effective.observed_from_init({"loaded_skills": ["dir-name", "plugin:proj"], "loaded_agents": ["reviewer"],
                                            "mcp_server_names": ["user-mcp"]})
        miss = effective.missing_parts(exp, obs)
        # フォルダ名でも、プラグイン形式（plugin:skill）でも読み込み済みとみなす。CLAUDE.md は観測手段がないので照合しない
        self.assertEqual(miss, {"skills": [], "agents": [], "mcp_servers": ["proj-mcp"], "instructions": None})
        self.assertIsNone(effective.missing_parts(exp, None))
        self.assertIsNone(effective.observed_from_init({}))

    def test_codex_project_config_only_when_trusted(self):
        cfg = self.home / ".codex"
        cfg.mkdir(parents=True)
        (cfg / "config.toml").write_text('[mcp_servers.user_mcp]\ncommand = "x"\n[mcp_servers."quoted-name"]\n', encoding="utf-8")
        (self.wd / ".codex").mkdir()
        (self.wd / ".codex" / "config.toml").write_text("[mcp_servers.proj_mcp]\n", encoding="utf-8")
        _skill(self.home / ".agents" / "skills", "a")
        _skill(self.wd / ".agents" / "skills", "b")
        (cfg / "AGENTS.md").write_text("USER RULE\n", encoding="utf-8")
        untrusted = effective.expected_parts("codex", self.home, cfg, self.wd, trusted=False)
        trusted = effective.expected_parts("codex", self.home, cfg, self.wd, trusted=True)
        self.assertEqual([n for n, _ in untrusted["mcp_servers"]], ["user_mcp", "quoted-name"])
        self.assertEqual([n for n, _ in trusted["mcp_servers"]], ["user_mcp", "quoted-name", "proj_mcp"])
        self.assertEqual([n for n, _ in trusted["skills"]], ["a", "b"])
        self.assertEqual(trusted["instructions"], [("home/.codex/AGENTS.md", "USER RULE")])

    def test_parse_prompt_input_real_shape(self):
        items = [
            {"type": "additional_tools", "tools": []},
            {"type": "message", "role": "developer", "content": [{"type": "input_text", "text":
             "<skills_instructions>\n## Skills\n### Available skills\n- skill-creator: Create a skill. (file: r2/skill-creator/SKILL.md)\n"
             "- user-skill: probe skill user-skill (file: r1/user-skill/SKILL.md)\n</skills_instructions>"}]},
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text":
             "# AGENTS.md instructions for /w\n\n<INSTRUCTIONS>\nUSER RULE\n\n--- project-doc ---\n\nPROJECT RULE\n</INSTRUCTIONS>"}]},
        ]
        got = effective.parse_prompt_input(json.dumps(items))
        self.assertEqual(got["skills"], ["skill-creator", "user-skill"])
        self.assertIn("PROJECT RULE", got["instructions_text"])
        self.assertIsNone(effective.parse_prompt_input("not json"))
        exp = {"skills": [("user-skill", "user-skill"), ("gone", "gone")], "instructions": [("home/.codex/AGENTS.md", "USER RULE"),
                                                                                           ("workdir/AGENTS.md", "MISSING RULE")]}
        miss = effective.missing_parts(exp, {"skills": got["skills"], "agents": None, "mcp_servers": None,
                                             "instructions_text": got["instructions_text"]})
        self.assertEqual(miss, {"skills": ["gone"], "agents": None, "mcp_servers": None, "instructions": ["workdir/AGENTS.md"]})

    def test_probe_args_keep_only_config_flags(self):
        self.assertEqual(effective._probe_args(["-c", "model=x", "--json", "--enable", "f", "--config=a=b", "-m", "gpt"]),
                         ["-c", "model=x", "--enable", "f", "--config=a=b"])

    def test_misplaced_home_claude_json_and_redaction(self):
        (self.home / ".claude").mkdir(parents=True)
        (self.home / ".claude.json").write_text("{}", encoding="utf-8")
        self.assertEqual(len(effective.misplaced("claude_code", self.home, self.home / ".claude", True)), 1)
        self.assertEqual(effective.misplaced("codex", self.home, self.home / ".codex", True), [])
        self.assertEqual(effective.redact_argv(["-c", "api_key=sk-123", "--model", "x"]), ["-c", "api_key=***", "--model", "x"])

    def test_installed_excludes_credentials_and_fixture(self):
        cfg = self.home / ".claude"
        cfg.mkdir(parents=True)
        (cfg / ".credentials.json").write_text("secret", encoding="utf-8")
        (cfg / "CLAUDE.md").write_text("x", encoding="utf-8")
        files = effective.installed_files(self.home, self.wd, {"data.csv": "h1"}, {"data.csv": "h1", ".mcp.json": "h2"})
        self.assertEqual(sorted(files), ["home/.claude/CLAUDE.md", "workdir/.mcp.json"])


class ClassifyTest(unittest.TestCase):
    def test_usage_limit_is_excluded_and_not_rate_limit(self):
        self.assertEqual(_classify_message(USAGE_MSG), "usage_limit")
        self.assertEqual(_classify_message("Your credit balance is too low to access the Anthropic API"), "usage_limit")
        self.assertEqual(_classify_message("API Error: 429 rate limited"), "rate_limit")
        self.assertIn("usage_limit", EXCLUDED_ERRORS)
        self.assertEqual(reset_hint(USAGE_MSG), "Nov 19th, 2026 9:03 AM")
        self.assertEqual(reset_hint("You've hit your limit · resets 3pm (Asia/Tokyo)"), "3pm (Asia/Tokyo)")
        self.assertIsNone(reset_hint("boom"))

    def test_codex_turn_failed_usage_limit(self):
        lines = [json.dumps({"type": "thread.started", "thread_id": "t"}), json.dumps({"type": "turn.started"}),
                 json.dumps({"type": "error", "message": USAGE_MSG}), json.dumps({"type": "turn.failed", "error": {"message": USAGE_MSG}})]
        r = CodexAdapter(executable=["codex"]).parse(lines)
        self.assertEqual(r.error_type, "usage_limit")

    def test_claude_429_with_usage_text_prefers_usage_limit(self):
        lines = [json.dumps({"type": "result", "subtype": "success", "is_error": True, "api_error_status": 429,
                             "result": "You've hit your limit · resets 3pm"})]
        self.assertEqual(ClaudeAdapter(executable=["claude"]).parse(lines).error_type, "usage_limit")
        lines = [json.dumps({"type": "result", "subtype": "success", "is_error": True, "api_error_status": 429, "result": "x"})]
        self.assertEqual(ClaudeAdapter(executable=["claude"]).parse(lines).error_type, "rate_limit")

    def test_claude_init_records_mcp_names(self):
        lines = [json.dumps({"type": "system", "subtype": "init", "skills": [], "mcp_servers": [{"name": "a:b", "status": "pending"}]})]
        self.assertEqual(ClaudeAdapter(executable=["claude"]).parse(lines).meta["mcp_server_names"], ["a:b"])


def _rec(pid: str, task: str, passed: bool, skills: list[str], graders: list[dict], **kw) -> TrialRecord:
    return TrialRecord(run_id="r", task_id=task, profile_id=pid, agent="claude_code", trial=0, cache_key="k", task_hash="t",
                       profile_hash="p", cli_version=None, skill_calls=skills, grader_results=graders, passed=passed,
                       score=1.0 if passed else 0.0, **kw)


class MetricsTest(unittest.TestCase):
    def test_pass_at_k_unbiased(self):
        self.assertEqual(pass_at_k(5, 0, 3), 0.0)
        self.assertEqual(pass_at_k(5, 3, 3), 1.0)  # n − c < k なら必ず 1 回は合格
        self.assertAlmostEqual(pass_at_k(5, 1, 3), 1 - 4 / 10)  # 1 − C(4,3)/C(5,3)
        self.assertAlmostEqual(pass_at_k(4, 2, 1), 0.5)  # k = 1 は pass@1 と同じ
        self.assertIsNone(pass_at_k(2, 1, 3))

    def test_wilson(self):
        lo, hi = wilson(5, 10)
        self.assertAlmostEqual(lo, 0.2366, places=3)
        self.assertAlmostEqual(hi, 0.7634, places=3)
        lo, hi = wilson(0, 10)
        self.assertEqual(lo, 0.0)
        self.assertAlmostEqual(hi, 0.2775, places=3)
        self.assertIsNone(wilson(0, 0))

    def test_trigger_direction(self):
        g = lambda **p: Grader("skill_used", {"skill": "s", **{k: v for k, v in p.items() if k != "negate"}}, negate=p.get("negate", False))
        self.assertEqual(g().trigger_direction, "positive")
        self.assertEqual(g(min=2).trigger_direction, "positive")
        self.assertEqual(g(max=0).trigger_direction, "negative")
        self.assertEqual(g(negate=True).trigger_direction, "negative")
        self.assertEqual(g(max=0, negate=True).trigger_direction, "positive")
        self.assertIsNone(g(max=3).trigger_direction)
        self.assertIsNone(Grader("regex", {"pattern": "x"}).trigger_direction)

    def test_trigger_stats_decoupled_from_pass(self):
        pos = {"type": "skill_used", "target": "s", "trigger": "positive"}
        neg = {"type": "skill_used", "target": "s", "trigger": "negative"}
        recs = [
            _rec("p", "should", False, ["s"], [pos]),       # 呼んだが不合格 → 呼び出しとして数える
            _rec("p", "should", True, [], [pos]),           # 呼ばずに合格 → 呼んでいない
            _rec("p", "should", False, ["plugin:s"], [pos]),
            _rec("p", "anti", True, [], [neg]),
            _rec("p", "anti", False, ["s"], [neg]),
            _rec("p", "should", False, ["s"], [pos], valid=False),  # 無効な試行は数えない
            _rec("p", "old", True, ["s"], [{"type": "skill_used"}]),  # 0.8.0 以前の結果（向きなし）は数えない
        ]
        t = trigger_stats(recs)
        self.assertEqual((t["positive"], t["invoked"], t["negative"], t["false_triggers"]), (3, 2, 2, 1))
        self.assertAlmostEqual(t["invocation_rate"], 2 / 3)
        self.assertEqual(t["false_trigger_rate"], 0.5)
        self.assertEqual(t["detection"], "exact")
        self.assertEqual(len(t["invocation_rate_ci"]), 2)
        empty = trigger_stats([])
        self.assertIsNone(empty["invocation_rate"])
        self.assertIsNone(empty["detection"])

    def test_harness_load_and_summary(self):
        miss = {"skills": ["x"], "agents": [], "mcp_servers": None, "instructions": None}
        ok = {"skills": [], "agents": [], "mcp_servers": [], "instructions": None}
        recs = [_rec("p", "a", True, [], [], effective={"missing": miss}), _rec("p", "a", False, [], [], effective={"missing": ok}),
                _rec("p", "b", True, [], [], effective={"missing": None}), _rec("p", "b", True, [], [])]
        hl = harness_load(recs)
        self.assertEqual(hl, {"checked": 2, "with_missing": 1, "missing": {"skills": {"x": 1}}})
        self.assertTrue(effective.has_missing({"missing": miss}))
        self.assertFalse(effective.has_missing({"missing": ok}))
        s = summarize(recs, 2)
        p = s["profiles"]["p"]
        self.assertEqual(p["harness_load"]["with_missing"], 1)
        self.assertEqual(s["tasks"]["p"]["a"]["pass_at_k"], 1.0)
        self.assertEqual(p["pass_at_k"], 1.0)
        self.assertIsNotNone(p["pass_at_1_ci"])
        self.assertIn("trigger", p)


class EffectiveE2E(unittest.TestCase):
    """偽の CLI で run を通し、信頼の付与・有効設定の記録・読み込みの照合・利用上限の除外を確かめる。
    準備は test_e2e.E2E と同じ（継承すると E2E のテストまで二重に集められるので、関数だけ借りる）。"""
    setUp = test_e2e.E2E.setUp
    tearDown = test_e2e.E2E.tearDown
    runs = test_e2e.E2E.runs

    def _profile(self, name: str, agent: str, body: str = "") -> Path:
        fake = FAKE / ("fake_claude.py" if agent == "claude_code" else "fake_codex.py")
        f = self.ex / "profiles" / f"{name}.yaml"
        f.write_text(f"id: {name}\nagent: {agent}\n{body}executable: [{json.dumps(sys.executable)}, {json.dumps(str(fake))}]\n",
                     encoding="utf-8")
        return f

    def _run(self, task: str, *profiles: Path, env: dict[str, str] | None = None) -> tuple[int, str, dict, dict]:
        old = {k: os.environ.get(k) for k in (env or {})}
        os.environ.update(env or {})
        try:
            code, out = run_cli("run", "--tasks", str(self.ex / "tasks" / f"{task}.yaml"), "--profiles", *map(str, profiles),
                                "--out", str(self.out), "--cache-dir", str(self.cache), "--no-llm", "--no-cache")
        finally:
            for k, v in old.items():
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        run = self.runs()[-1]
        summary = json.loads((run / "summary.json").read_text(encoding="utf-8"))
        recs = {json.loads(l)["profile_id"]: json.loads(l) for l in (run / "trials.jsonl").read_text(encoding="utf-8").splitlines()}
        return code, out, summary, recs

    def test_examples_load_and_record(self):
        code, out, summary, recs = self._run("csv-summary-skill", self.ex / "profiles" / "claude-ds-lite.yaml",
                                             self.ex / "profiles" / "codex-ds-lite.yaml", self.ex / "profiles" / "claude-baseline.yaml")
        self.assertEqual(code, 0, out)
        for pid, source in (("claude-ds-lite", "init"), ("codex-ds-lite", "prompt_input")):
            eff = recs[pid]["effective"]
            self.assertEqual(eff["schema"], 1)
            self.assertEqual(eff["expected"]["skills"], ["csv-monthly-summary"])
            self.assertEqual(eff["observed"]["source"], source)
            self.assertEqual(eff["missing"]["skills"], [], pid)
            self.assertTrue(eff["trust"]["enabled"])
            self.assertTrue(any(p.endswith("/workdir") or p.endswith("\\workdir") for p in eff["trust"]["paths"]))
            self.assertFalse(any("credentials" in k or k.endswith("auth.json") for k in eff["installed"]))
            on_disk = json.loads((Path(recs[pid]["trial_dir"]) / "effective_config.json").read_text(encoding="utf-8"))
            self.assertEqual(on_disk, eff)
        self.assertEqual(recs["codex-ds-lite"]["effective"]["missing"]["instructions"], [])  # AGENTS.md が prompt-input にある
        self.assertEqual(recs["claude-baseline"]["effective"]["expected"]["skills"], [])
        p = summary["profiles"]["claude-ds-lite"]
        self.assertEqual(p["trigger"]["positive"], 1)
        self.assertEqual(p["trigger"]["invocation_rate"], 1.0)
        self.assertEqual(p["harness_load"], {"checked": 1, "with_missing": 0, "missing": {}})
        self.assertEqual(summary["profiles"]["claude-baseline"]["trigger"]["invocation_rate"], 0.0)
        self.assertEqual(summary["meta"]["effective_config_schema"], 1)
        report = (self.runs()[-1] / "report.html").read_text(encoding="utf-8")
        self.assertIn("呼び出し率", report)

    def test_not_loaded_skill_is_reported(self):
        code, out, summary, recs = self._run("csv-summary-skill", self.ex / "profiles" / "claude-ds-lite.yaml",
                                             self.ex / "profiles" / "codex-ds-lite.yaml", env={"FAKE_DROP_SKILLS": "1"})
        for pid in ("claude-ds-lite", "codex-ds-lite"):
            self.assertEqual(recs[pid]["effective"]["missing"]["skills"], ["csv-monthly-summary"], pid)
            self.assertEqual(summary["profiles"][pid]["harness_load"]["with_missing"], 1)
            self.assertTrue(recs[pid]["valid"])  # 試行の有効・無効は変えない（配布物の失敗として測る）
        self.assertIn("置いた部品が読み込まれていません", out)
        self.assertIn("読み込まれなかった部品", (self.runs()[-1] / "report.html").read_text(encoding="utf-8"))

    def test_project_level_settings_need_trust(self):
        h = self.ex / "harnesses" / "proj"
        (h / ".claude").mkdir(parents=True)
        (h / ".claude" / "settings.json").write_text('{"permissions": {"allow": ["Bash(echo *)"]}}', encoding="utf-8")
        (h / ".codex").mkdir()
        (h / ".codex" / "config.toml").write_text('[mcp_servers.proj_mcp]\ncommand = "x"\n', encoding="utf-8")
        install = "install: [{copy: ../harnesses/proj, to: '{workdir}'}]\n"
        trusted_c = self._profile("claude-proj", "claude_code", install)
        untrusted_c = self._profile("claude-proj-untrusted", "claude_code", install + "trust_workdir: false\n")
        trusted_x = self._profile("codex-proj", "codex", install)
        untrusted_x = self._profile("codex-proj-untrusted", "codex", install + "trust_workdir: false\n")
        code, out, _, recs = self._run("arithmetic-no-skill", trusted_c, untrusted_c, trusted_x, untrusted_x)
        self.assertEqual(code, 0, out)
        self.assertEqual(recs["claude-proj"]["effective"]["warnings"], [])
        self.assertIn("not been trusted", recs["claude-proj-untrusted"]["effective"]["warnings"][0])
        self.assertEqual(recs["claude-proj-untrusted"]["effective"]["trust"], {"enabled": False, "paths": []})
        self.assertIn("workdir/.claude/settings.json", recs["claude-proj"]["effective"]["installed"])
        # Codex: 信頼すれば作業ディレクトリの .codex/config.toml の MCP が読み込まれる。未信頼なら期待にも観測にも入らない
        self.assertEqual(recs["codex-proj"]["effective"]["expected"]["mcp_servers"], ["proj_mcp"])
        self.assertEqual(recs["codex-proj"]["effective"]["observed"]["mcp_servers"], ["proj_mcp"])
        self.assertEqual(recs["codex-proj-untrusted"]["effective"]["observed"]["mcp_servers"], [])
        self.assertEqual(recs["codex-proj-untrusted"]["effective"]["expected"]["mcp_servers"], [])

    def test_usage_limit_is_excluded_without_retry(self):
        t = self.ex / "tasks" / "limit.yaml"
        t.write_text("id: limit\ntargets: [codex]\nprompt: FAKE_USAGE_LIMIT\ngraders: [{type: regex, pattern: x}]\n", encoding="utf-8")
        code, out, summary, recs = self._run("limit", self.ex / "profiles" / "codex-baseline.yaml")
        rec = recs["codex-baseline"]
        self.assertFalse(rec["valid"])
        self.assertEqual(rec["error"]["type"], "usage_limit")
        self.assertEqual(rec["error"]["reset_hint"], "Nov 19th, 2026 9:03 AM")
        self.assertNotIn("再試行", out)
        self.assertIn("利用上限", out)
        self.assertEqual(summary["profiles"]["codex-baseline"]["excluded_reasons"], {"usage_limit": 1})


if __name__ == "__main__":
    unittest.main()
