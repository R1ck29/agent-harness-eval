# Harness Eval Work Plan

## Current: docs without redundancy, duplication or stale facts; mechanisms shown as diagrams — 2026-10-29

### Audit findings

- README §3 and §6–§9 repeat guide.html §1–§9 almost verbatim (README says so: "2 形式で載せています").
- Inside README: the LLM-judge exception footnote 3×, the ancestor-folder rule 4× (plus guide and design §8), the list of
  excluded end states 3×, verified CLI versions 3× (plus design §8), the Keychain note 2× (plus design §8).
- guide §2 "使う順番" omits `harvest`, although README's flow starts with it.
- README opens with history (an external design doc that is not in the repo, the earlier attempt) that belongs in design.html.
- Install only says "unzip"; the repo is public on GitHub (no `git clone` path). README §9 has an empty code block.
- GitHub Pages is off, so docs/*.html show as source on GitHub.

### Decision (user, 2026-10-29): one home per fact, HTML for mechanisms, GitHub Pages on

| File | Holds | Does not hold |
|---|---|---|
| README.md | what it is, overview image, install, 5-minute commands, map of docs, development | mechanisms, option and key lists, rationale |
| docs/guide.html | how it works, every mechanism as a diagram (order of use, run flow, one trial, isolation, grading, metrics and gate, cache, harvest, outputs, modules) | YAML keys, command options, rationale |
| docs/reference.html (new) | how to write tasks and profiles, graders, auth, Codex notes, commands and options, cost formula, labels format | mechanisms, rationale |
| docs/design.html | why: decisions, lineage, tool survey, method, rejected ideas, risks, CLI facts verified on real binaries, dev-time measurements | how-to |
| docs/img/overview.svg (new) | the one overview figure, shared by README and guide | |

### Steps

- [x] guide.html rewritten (new run-flow, budget, gate and module figures; harvest in the order-of-use figure)
- [x] reference.html written from README §4–§5, §9 and the CLI help
- [x] design.html: took README §10 (§8 CLI facts) and dev-time results (§9); cost section reduced to decision + reason
- [x] README shrunk to the entry page; docs/index.html redirects to guide.html for Pages
- [x] Verify: README zero-cost commands run (validate --selftest 4/4 OK, plan, doctor); 63 tests OK; no broken anchors;
      no 36-char passage shared by two documents; figures checked in light/dark and at desktop width
- [x] Independent review (fresh subagent, docs vs code); fixed every finding
- [x] Commit and push; enable GitHub Pages (user approved 2026-10-29)

### Review (2026-10-29)

Independent reviewer findings, all fixed:
- HIGH: harvest writes to `--ledger` (default results/spend.jsonl), not `--out/spend.jsonl`; README now passes `--ledger "$OUT/spend.jsonl"`.
- HIGH: gate checks `--min-tasks` before "upper < −m", so a small regression run exits 3; the gate table now lists the order.
- MEDIUM: cache key also holds the tool version, `--no-llm`, judge prompt and judge CLI version; skipped_prior_fail is
  left out of the score (it was called "counted as fail" — stale since before this change); 14 facts stated in two places
  reduced to one home plus links.
- LOW: sftp in the deny list, k only matters for pass^k, AWS redirect needs isolate_home, harvest --source default,
  results/<run-id>/ prefix, bootstrap seed is fixed (not --seed), trials.jsonl gets every record.

## Previous: merge into one repo (agent-harness-eval v0.4.0) — 2026-10-28

### Inputs (read in full, nothing inferred)

- This repo, Phase 0 (2026-08-15): BenchFlow 0.6.7 engine plan, `src/harness_eval` (profiles, Decimal budgets,
  trust levels T0–T3, BenchFlow / Apple Container / AgentCore doctor), `docs/harness-eval-design.html`.
  Tests here: 44/45 (the failing one needs `bench` on PATH).
- Session repo v0.3.0 (2026-10-22): `~/Downloads/agent-harness-eval.zip` (140,735 bytes, from chat
  "リポジトリ実装と社内ツール化"), its implementation note (pasted by the user), and its design doc
  "Agent Harness Eval 設計書（Claude Code / Codex 対応）" (Claude Doc, 2026-10-21). Tests here: 46/46 (Python 3.12).
- The iCloud `agent-harness-eval.zip` is v0.1.0 (superseded; not used).
- OSS / method survey 2026-10-28 (subagent, primary sources): no existing tool covers Claude + Codex CLI,
  CLAUDE.md / AGENTS.md harnesses, script graders, pass^k, a budget ledger and a statistical release gate under a
  PyYAML-only install. Closest: the official `claude plugin eval` (Claude-only, plugin-only, no code graders, no
  CLAUDE.md). BenchFlow is now 0.7.11 and needs Python ≥3.12, Docker and litellm.
- Local CLIs: Claude Code 2.1.295 and Codex 0.146.1, both older than the versions v0.3.0 was verified against.

### Decision

Base = v0.3.0 (newer; its decisions win on conflict: direct CLI calls, PyYAML only, Python ≥3.10, zip
distribution, Windows + macOS). Drop BenchFlow and its sandbox backends. Port only what v0.3.0 lacks and what
the survey shows is current best practice:

| # | Change | Origin | Why |
|---|--------|--------|-----|
| 1 | Paired, task-clustered bootstrap CI for Δ; three-valued decision (pass / fail / inconclusive) with a pre-declared non-inferiority margin and a minimum task count; used by `gate` and the report | Phase 0 design; Anthropic stats post; Bowyer et al. 2025 | v0.3.0 `gate` fails on any single-task pass^k drop (one unlucky trial at k=3 blocks a release) and never shows uncertainty |
| 2 | Comparable score for Δ: graders that can only pass with the harness (`skill_used` / `subagent_used` / `hook_used` requiring ≥1 call) are left out of Δ in both arms; the full score is still reported | `claude plugin eval` docs | Otherwise Δ against a no-harness baseline is inflated by construction |
| 3 | Adjacent, counterbalanced scheduling: for each (trial, task) the profiles run back to back, in an order alternated by a seeded RNG (`--seed`) | Phase 0 design; Anthropic infrastructure-noise post | v0.3.0 runs all of one profile before the next, so time drift (API load, model rollout) confounds Δ |
| 4 | Cost basis and per-agent monthly ledgers: `client_estimate` (Claude `total_cost_usd`), `token_estimate` (Codex pricing), null when unavailable; profile `billing: api / subscription`; ledger rows carry the agent; `--monthly-budget-usd` applies to the Claude and Codex accounts separately; subscription spend is shown but not charged | Phase 0 (confirmed: separate USD 1,500 Claude / Codex ledgers; no fictional per-run USD for subscriptions) | v0.3.0 has one shared ledger and labels client estimates as "reported" |
| 5 | `doctor` static checks: route-changing variables (names verified in the CLI binaries) inherited without declaration, with a name-only `inherit_env` opt-in; CLI older than the verified version (one per agent: a per-flag `--help` check is unreliable because `--max-turns` is hidden) | Phase 0 doctor; local CLI versions | Trials inherit the parent environment, so a stray key silently changes routing and billing |
| 6 | Reference solutions for the bundled example tasks, so `validate --selftest` proves "reference passes, empty fails" for them too | Phase 0 known-bad control | Today only harvested drafts have references |
| 7 | Lint warning for executable harness parts (install `run`, hooks, MCP): they run with the user's privileges | Phase 0 trust levels (T2/T3 cannot be enforced without a sandbox) | Keeps the safety rule without unenforceable machinery |

Not adopted (the reasons go in docs/design.html):
- BenchFlow / Inspect / Harbor: Docker and heavy dependencies.
- Promptfoo: Node, and it drives the SDK, not the CLI.
- `claude plugin eval` as the engine: Claude-only; no CLAUDE.md or code graders.
- Decimal ledger arithmetic: float is exact enough at cent scale, and it is one fewer concept.
- Codex `--ignore-user-config`: not verifiable on the local 0.146.1, and CODEX_HOME is already isolated.
- Plugin-eval case import/export: no user need yet.

### Repo layout after merge

- Flat `harness_eval/` package, so it runs as `python -m harness_eval` straight from the unzipped folder.
- `examples/`, `tests/`, `README.md`.
- `docs/guide.html`: how it works (updated).
- `docs/design.html`: merged decision record and research; replaces `docs/harness-eval-design.html`.
- `pyproject.toml` (PyYAML at runtime; pytest, pytest-cov and ruff for dev), `requirements.txt`, `uv.lock`, `tasks/`.
- Removed: `src/`, `tests/unit/`, the BenchFlow dependency.

### Steps

- [x] Baseline snapshot for rollback (user chose git init + commit: `d2ff1db`)
- [x] Import the v0.3.0 tree; remove Phase 0 code; merged pyproject and lock; tests green as-is
- [x] (1)(2) stats in `metrics.py` plus summarize / deltas / gate / report integration, tests first
- [x] (3) counterbalanced scheduler, tests first
- [x] (4) cost basis and per-agent ledger, tests first
- [x] (5) doctor route and version checks, tests first
- [x] (6) example references and selftest; (7) lint warning
- [x] Update README, guide.html and design.html; no stale statements (grep for BenchFlow, old versions, old flags)
- [x] Verification:
  - full test suite on Python 3.10–3.13, ruff, coverage ≥80%
  - build the zip and run it from the unzipped copy
  - `doctor` on this Mac
  - a capped real smoke run, only if authorized
- [x] Independent review (a fresh reviewer with no build context); fix every CRITICAL/HIGH finding; re-review

## Review (2026-10-28)

Each step was verified by an independent reviewer subagent in a separate git worktree (task-graph.json):
- import: PASS.
- gate-stats: PASS. Follow-ups fixed: per-task counterbalancing, Δ fallback for pre-0.4 summaries, `--min-tasks` validation.
- cost-doctor: FAIL on attempt 1 (HIGH: doctor told users to write secrets into profiles), fixed with the name-only
  `inherit_env`, extended route variables and the judge-route check; PASS on attempt 2. A MEDIUM finding (value-only
  changes of model aliases reused the cache) was fixed in the docs step: non-secret values are hashed into the key.
- controls-docs: FAIL on attempts 1 and 2 (docs/code mismatches, duplication), PASS on attempt 3 (granted by the user).
- verify (final whole-repo review): FAIL on attempt 1 (HIGH: instruction files in folders above --out leak into every
  trial, confirmed with a local capture server) and attempt 2 (the check missed .claude/rules, agents, commands); fixed by
  refusing runs when CLAUDE.md, CLAUDE.local.md, AGENTS.md, AGENTS.override.md, .mcp.json or any .claude/, .codex/,
  .agents/ folder is at or above --out; other API errors became the excluded api_error. PASS on attempt 3 (granted).
- graphctl completion-check: complete. Final state: 62 tests on Python 3.10-3.13, coverage 90.9%, ruff clean,
  dist/agent-harness-eval.zip runs from an unzipped copy.
- Not done: no real API run in this session (doctor --live and a capped smoke run need the user's go-ahead and an --out
  outside home); Windows and real Codex runs remain unverified.
- Residual risks are listed in docs/design.html §8.

## History

- 2026-08-15 Phase 0 (BenchFlow-based contracts and doctor), superseded by the merge above. Where its open items go:
  - Counterbalanced pairs: item 3.
  - Paired statistics and three-valued decisions: item 1.
  - Cost provenance and separate ledgers: item 4.
  - Route-override checks: item 5.
  - Known-bad / reference controls: item 6.
  - Trust levels: item 7.
  - Covered by v0.3.0 already: materializing harnesses without ambient config (temp HOME / CLAUDE_CONFIG_DIR /
    CODEX_HOME), the deterministic graders, the HTML report, and classifying infrastructure errors as invalid.
  - Dropped, with reasons recorded in docs/design.html:
    - BenchFlow producer-artifact contracts: no BenchFlow.
    - Provider-gateway hard stop: needs corporate gateway infrastructure; the client-side ledger cap is the
      available control.
    - Sealed holdouts with separate credentials: no infrastructure or user need yet; harvested tasks are
      fictionalized instead.
    - Enforced egress / credential non-visibility and malicious-exfiltration tests: impossible without a
      sandbox; scope stays "evaluate only reviewed internal harnesses", documented as a residual risk.
- 2026-10-21 → 10-22: the session repo went from v0.1.0 to v0.3.0 (run, report, gate, calibrate, hooks, ledger,
  harvest).
