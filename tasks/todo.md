# Harness Eval Work Plan

## Current: self-improvement loop, evaluation side (v0.5.0) on top of the restructured docs — 2026-10-31

### Inputs (read in full, nothing inferred)

- Shared claude.ai session https://claude.ai/share/e1c96de7-ae94-4a70-b16a-3597830e6af8 (2026-10-18〜29): Harness²
  study, OSS survey "社内Claude Codeハーネスの自己改善：OSS調査と推奨設計", and a cloud Claude Code session that built
  v0.5.0 on 059e103 and handed it over as `0001-feat-v0.5.0-…patch` (119,904 bytes) + `agent-harness-eval-0.5.0.zip`.
  Neither file was on this Mac (not in ~/Downloads, not found by Spotlight).
- The patch was rebuilt from the share's snapshot API (`/api/chat_snapshots/<id>?render_all_tools=true`, every tool
  input of the session): the session's Edit/Write inputs were applied as data, its python heredocs were evaluated by
  an AST interpreter that only allows string replacement on repo files (no transcript code was executed), and its
  four `sed -i` lines were re-implemented by hand. Every replacement asserted its match. Result on 059e103: 87 tests
  OK, ruff clean, and a patch of 119,907 bytes — the 3-byte difference is the author line (`Claude <noreply@…>` in
  the original). Code and tests applied to main (edfd49e did not touch them); docs were merged by hand into the
  README / guide / reference / design split of 2026-10-29.

### Decision (from the session; user chose "design + evaluation-side foundation", 2026-10-29)

The improver stays outside this tool. This version gives the loop what the research says matters: data the improver
cannot see, a regression-aware paired test, a promotion decision corrected for how many candidates were already tried
on the holdout, and the noise floor of the current harness (design.html §10 has the table, sources and the
rejected alternatives: best-of-N control, built-in `improve`, credential-sealed holdout, rollout without approval).

### Port onto main — changes beyond the session's patch

- docs: README is an entry page now, so v0.5.0's README §4.2 / §9.1 went to guide.html §11 (mechanism, decision
  table) and reference.html (keys, lint rows, options); design.html §9 became §10 (risks §11, sources §12); the
  sealed-holdout rejection was merged into the existing "封印した holdout" row instead of a second row; the review
  note moved from "risks" to §10 (it is history, not a risk); a risk line records that no real-CLI holdout run has
  been done.
- cli.py: removed the "古い形式（v0.5.0 開発中）" ledger note (that row format existed only inside the dev session,
  so the note could never apply to a real ledger); `import json` at module level instead of two local imports;
  `--split` choices from `SPLITS` + `NO_SPLIT` and `promote --min-tasks` default from `MIN_HOLDOUT_TASKS`
  (one source for each); dropped the CLI's `max(1, m)` (promote() already clamps). metrics.py imports sorted.
- `tests/test_loop.py` stays a separate file: test_units.py is already 787 lines and the loop tests are one topic.

### Steps

- [x] Rebuild the session's patch and verify it (87 tests, ruff, byte size)
- [x] Apply code and tests to main; small cleanups above
- [x] Merge docs into guide / reference / design / README
- [x] Verify: 89 tests on Python 3.10 / 3.11 / 3.12 / 3.13, ruff clean, coverage 91%, no broken anchors, no new
      36-char passage shared by two documents, guide §11 figure rendered
- [x] Independent review (fresh reviewer, no build context); fixes; re-review PASS (see Review of the port)
- [ ] Commit and push (user approved 2026-10-31); verify GitHub's Download ZIP runs from an unzipped copy
- Distribution (user, 2026-10-31): the internal environment gets the code through GitHub's "Download ZIP", so the
  separate `git archive` zip (dist/agent-harness-eval.zip) and its README command were dropped.

### Review of the port (2026-10-31)

- Attempt 1: PASS (no CRITICAL/HIGH). Reviewer confirmed cache keys of all examples/ tasks and profiles are unchanged
  against HEAD. Fixed:
  - MEDIUM: promote silently dropped a task with a value on only one side (e.g. every candidate trial excluded by
    rate_limit), so a real regression never reached the budget or the sign test. `one_sided_tasks` now forces
    inconclusive and is listed in promote.json.
  - MEDIUM: a ledger row that is valid JSON with wrong value types raised TypeError, and an unreadable ledger was
    not handled. Non-string ids/hashes are skipped; an unreadable ledger is a SpecError (exit 2), never "empty"
    (that would make m = 1).
  - LOW: noise floor over-covered for odd trial counts (3 trials compared 1 vs 1). Scale is now √(h/n) per task
    (1/√2 for even n); supersedes "L1 noise scaled by 1/√2" below.
  - LOW: `holdout_signature` / ledger field `holdout` were written but never read — removed.
  - LOW: review history removed from design §10 (substance kept in table row 6); guide §11 no longer restates
    design's reasons; reference no longer repeats guide §8's hash-exclusion list.
- Attempt 2: PASS (89 tests, ruff clean; simulated n=3 A/A half-width 0.128, estimate now 0.115–0.135).

### Review of the session's v0.5.0 (2026-10-29, carried over)

- Attempt 1: PASS with MEDIUM fixes — M1 promote also requires improved > regressed (two-sided test); M2/M3 holdout
  runs never use the cache and promote is inconclusive with cached trials; M4 ledger rows carry {task id: hash} and
  per-profile {harness hash, install_empty}, m counts overlapping rows; `Profile.harness_hash()` (id excluded) so
  the same harness under two ids is one candidate; `content_hash()` unchanged, caches stay valid.
- Attempt 2: PASS.

### Handoff (for the next session)

State: v0.5.0 is in the working tree of main (not committed until the user says so). Not run against real CLIs
(no `doctor --live`, no real holdout run); Windows still unverified.

Decisions (user, 2026-10-31): collect as many content-free signals as possible, and fictionalized `harvest` tasks
from users' machines may be collected centrally. What the internal environment permits is unknown, so collection
must be switchable per signal type and narrowed once the policy is known (design.html §10, layer ①). Prompt text
is still never sent.

Still open:
- Whether one human approval before rollout to everyone is acceptable (recommended; not answered yet).
- What the internal environment permits (OTEL export destination, hook-based collection, central storage).

Next tasks (in order):
1. On the Mac: `doctor`, `doctor --live`, a capped smoke run with `--out` outside home (/Users/Shared/harness-eval).
2. First real task set: `harvest` → human review → `split` / `family` by request type (≥20 holdout) → `validate --selftest`.
3. A/A noise: `run --split holdout --trials 3` with the current harness under two profile ids, then
   `promote --candidate <copy> --incumbent <current> --max-regressions 0` (reject "同じ配布物"; read the estimate,
   counts and noise floor). Recorded in the ledger but does not raise m (same harness hash).
4. Improver (next version, design.html §10 残タスク): isolated `claude -p` reading only train failures and the
   harness diff; deny read access to the holdout folder (verify on the real CLI); INV/DOM itemized deltas;
   predicted fixed tasks checked next round.
5. Pre-checks on candidates; skill candidates as three profiles (no skill / description only / full).
6. Fleet layer: OTEL aggregation; staged rollout comparison and rollback.

## Previous: docs without redundancy, duplication or stale facts; mechanisms shown as diagrams — 2026-10-29

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
