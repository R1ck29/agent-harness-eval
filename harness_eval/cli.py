"""コマンドライン入口: python -m harness_eval <command> ..."""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

import yaml

from . import __version__
from .isolation import ancestor_instructions, route_variables
from .metrics import HARD_CATEGORIES, gate
from .report import render
from .runner import RunOptions, Runner, load_run
from .spec import Grader, SpecError, Task, lint, load_profiles, load_tasks


def _opts(a: argparse.Namespace) -> RunOptions:
    return RunOptions(
        trials=a.trials, k=a.k, tags=a.tag or [], task_ids=a.task or [], jobs=a.jobs, llm=not a.no_llm,
        judge_agent=a.judge_agent, judge_model=a.judge_model or None, judge_executable=a.judge_executable,
        max_cost_usd=a.max_cost_usd, monthly_budget_usd=a.monthly_budget_usd,
        retry_infra=a.retry_infra, retry_wait_s=a.retry_wait_s, judge_profile=a.judge_profile, use_cache=not a.no_cache, cache_dir=Path(a.cache_dir), keep=a.keep,
        baseline=a.baseline, seed=a.seed)


def _load(a: argparse.Namespace):
    tasks = load_tasks(a.tasks)
    profiles = load_profiles(a.profiles)
    return tasks, profiles


def cmd_validate(a) -> int:
    tasks, profiles = _load(a)
    print(f"OK: タスク {len(tasks)} 件、プロファイル {len(profiles)} 件")
    for w in lint(tasks, profiles):
        print(f"  警告: {w}")
    if not a.selftest:
        return 0
    from .harvest import selftest
    bad = 0
    for t in tasks:  # 採点スクリプトを実行するので、harvest の下書きは内容を確認してから使う
        notes: list[str] = []
        r = selftest(t, notes)
        if r is not None:
            print(f"  selftest {t.id}: {'OK' if not r else r}")
            bad += bool(r)
            if notes:
                print(f"    注意: 何もしなくても通る判定 {notes}（入力の有無など情報のない判定なら外す。壊してはいけないものを守る判定なら意図どおり）")
    return 1 if bad else 0


def cmd_plan(a) -> int:
    tasks, profiles = _load(a)
    runner = Runner(tasks, profiles, _opts(a), Path(a.out))
    p = runner.plan()
    leaks = ancestor_instructions(Path(a.out))
    if leaks:
        print(f"  注意: {_ancestor_message(leaks)}（このままでは run が止まります）")
    for w in _budget_warnings(a, profiles):
        print(f"  注意: {w}")
    print(f"組み合わせ {p['pairs']} × 試行 {a.trials} = {p['trials_total']} 試行")
    print(f"  キャッシュで省略: {p['cache_hits']} 試行 ／ 実際にエージェントを呼ぶ: {p['agent_calls']} 回")
    print(f"  概算費用: ${p['estimated_cost_usd']:.4f}（履歴がなく見積もれない呼び出し {p['unknown_cost_calls']} 回）")
    print(f"  LLM 判定: {'あり（コード判定が全合格の試行のみ。配布物がないと通らない判定の不合格は除く）' if p['llm_judge'] else 'なし'}")
    for agent, spent in (runner.month_spent or {}).items():
        print(f"  今月の実績（{agent}）: ${spent:.4f} ／ 月の予算 ${a.monthly_budget_usd:.2f}（残り ${runner.month_remaining(agent):.4f}）")
    for pid, v in p["cli_versions"].items():
        print(f"  {pid}: CLI {v or '見つかりません'}")
    return 0


def cmd_run(a) -> int:
    tasks, profiles = _load(a)
    for w in lint(tasks, profiles):
        print(f"警告: {w}", file=sys.stderr)
    opt = _opts(a)
    if opt.baseline and opt.baseline not in {p.id for p in profiles}:
        print(f"--baseline '{opt.baseline}' がプロファイルにありません", file=sys.stderr)
        return 2
    leaks = ancestor_instructions(Path(a.out))
    if leaks and not a.allow_ancestor_instructions:
        print(_ancestor_message(leaks) + "（承知の上で実行するなら --allow-ancestor-instructions。結果に記録されます）", file=sys.stderr)
        return 2
    runner = Runner(tasks, profiles, opt, Path(a.out))
    judge = [runner.judge_route_profile()] if runner.judge_profile is None and runner.judge_warning else []
    for w in _budget_warnings(a, profiles) + _route_warnings(profiles + judge):
        print(f"注意: {w}", file=sys.stderr)
    if not runner.pairs:
        print("実行対象がありません（targets / --tag / --task を確認）", file=sys.stderr)
        return 2
    missing = [pid for pid, v in runner.versions.items() if v is None]
    if missing:
        print(f"警告: CLI のバージョンを取得できません: {missing}（未インストール・PATH 未設定の可能性）", file=sys.stderr)
    plan = runner.plan()
    print(f"実行: {plan['trials_total']} 試行（キャッシュ {plan['cache_hits']}、新規 {plan['agent_calls']}）→ {runner.run_dir}")
    run_dir, records, summary = runner.run()
    (run_dir / "report.html").write_text(render(summary, records, opt.baseline), encoding="utf-8")
    _print_summary(summary)
    print(f"\nレポート: {run_dir / 'report.html'}")
    return 0


def _budget_warnings(a, profiles) -> list[str]:
    """--max-cost-usd が効かない／超えうる条件を事前に知らせる。"""
    if a.max_cost_usd is None and a.monthly_budget_usd is None:
        return []
    w = []
    unpriced = [p.id for p in profiles if p.agent == "codex" and not p.pricing]
    if unpriced:
        w.append(f"{unpriced} は費用が分からない（pricing 未設定）ため、費用の上限の計算に入りません")
    if not a.no_llm and a.judge_agent == "codex":
        w.append("Codex の判定は費用が分からないため、費用の上限の計算に入りません")
    if a.jobs > 1:
        w.append(f"--jobs {a.jobs} では、上限に達した時点で実行中の試行（最大 {a.jobs - 1} 件）の分だけ上限を超えることがあります")
    subs = [p.id for p in profiles if p.billing == "subscription"]
    if subs and a.monthly_budget_usd is not None:
        w.append(f"{subs} はサブスクリプション（billing: subscription）なので月の予算には数えません（--max-cost-usd には数えます）")
    return w


def _ancestor_message(leaks: list[str]) -> str:
    return (f"--out より上のフォルダに、試行に読み込まれる指示・スキルがあります: {leaks}。一時ホームにしても CLI は上位フォルダの "
            "指示・スキル・ルール・サブエージェント（CLAUDE.md・AGENTS.md・.mcp.json、.claude/・.codex/・.agents/）を読むため、"
            "配布物なしの基準にも入り比較が壊れます。--out をこれらのない場所"
            "（リポジトリやホームの外。ただし OS の一時フォルダは Codex が拒否するので不可）にしてください")


def _route_warnings(profiles) -> list[str]:
    """宣言なしに親の環境から経路を変える変数を引き継ぐプロファイル（LLM 判定を含む。値は表示しない）。"""
    w = []
    for p in profiles:
        inherited = route_variables(p, dict(os.environ))[1]
        if inherited:
            w.append(f"{p.id}: 親の環境から {inherited} を宣言なしに引き継ぎます。意図して親の値を使うならプロファイルの inherit_env に名前だけ書き（値は書かない）、不要なら env_unset に入れてください（doctor で確認できます）")
    return w


def _print_summary(summary) -> None:
    print("\nプロファイル            pass^k  pass@1  有効/全  総コスト")
    for pid, s in summary["profiles"].items():
        f = lambda x: "  —  " if x is None else f"{x * 100:4.0f}%"
        c = "不明" if s["total_cost_usd"] is None else f"${s['total_cost_usd']:.4f}"
        print(f"{pid:22} {f(s['pass_hat_k'])}  {f(s['pass_at_1'])}  {s['valid']:>3}/{s['trials']:<3}  {c}")


def cmd_report(a) -> int:
    recs, summary = load_run(Path(a.run))
    out = Path(a.run) / "report.html"
    out.write_text(render(summary, recs, a.baseline or summary.get("meta", {}).get("options", {}).get("baseline")), encoding="utf-8")
    _print_summary(summary)
    print(f"\nレポート: {out}")
    return 0


GATE_EXIT = {"pass": 0, "fail": 1, "inconclusive": 3}


def cmd_gate(a) -> int:
    """前回と今回を比べ、pass（0）/ fail（1）/ inconclusive（3）を終了コードで返す。"""
    if not 0 <= a.margin < 1:
        raise SpecError("--margin は 0 以上 1 未満（例: 0.05 = 5 ポイントまでの悪化を許容）")
    if a.min_tasks < 2:
        raise SpecError("--min-tasks は 2 以上（区間を出すには比較できるタスクが 2 件以上必要）")
    _, cur = load_run(Path(a.current))
    _, prev = load_run(Path(a.previous))
    r = gate(cur, prev, a.metric, a.margin, a.min_tasks, a.hard_category or ())
    label = {"pass": "通過", "fail": "不通過", "inconclusive": "判定不能（データ不足）"}[r["decision"]]
    print(f"ゲート{label}: {a.metric} の前回との差（95% 区間）を非劣性マージン {a.margin:g} で判定")
    for pid, v in r["profiles"].items():
        ci = "—" if v["ci"] is None else f"[{v['ci'][0]:+.3f}, {v['ci'][1]:+.3f}]"
        est = "—" if v["estimate"] is None else f"{v['estimate']:+.3f}"
        print(f"  {pid}: 差 {est} {ci}（タスク {v['n_tasks']} 件、最低 {a.min_tasks} 件）→ {v['decision']}")
    for p in r["problems"]:
        print(f"  - 止める理由: {p}")
    if r["regressions"]:
        print("  前回より下がったタスク（参考）:")
        for p in r["regressions"]:
            print(f"    - {p}")
    return GATE_EXIT[r["decision"]]


def cmd_doctor(a) -> int:
    """環境検証。CLI の版と経路の変数を確かめ（費用 0）、--live でプロファイルごとに最小の試行を 1 回だけ実行する。"""
    print(f"harness-eval {__version__} / Python {sys.version.split()[0]} / PyYAML {yaml.__version__}")
    profiles = load_profiles(a.profiles)
    from .adapters import VERIFIED_VERSIONS, get_adapter, older_than_verified
    from .runner import cli_version
    static_ok = True
    for p in profiles:
        v = cli_version(get_adapter(p.agent, p))
        print(f"[{p.id}] {p.agent}: {v or '見つかりません'}（billing: {p.billing}）")
        static_ok &= v is not None
        if older_than_verified(p.agent, v):
            verified = ".".join(map(str, VERIFIED_VERSIONS[p.agent]))
            print(f"  注意: 起動フラグと出力形式を確認した版（{verified}）より古い CLI です。--live で実際に動くか確かめてください")
        chosen, inherited = route_variables(p, dict(os.environ))
        print(f"  経路: プロファイルで決めた変数 {chosen if chosen else 'なし（CLI の既定: ログイン済みの認証）'}")
        if inherited:
            static_ok = False
            print(f"  NG: 親の環境から経路を変える変数 {inherited} を宣言なしに引き継ぎます（値は表示しません）。意図して親の値を使うならプロファイルの inherit_env に名前だけ書き（値は書かない）、不要なら env_unset に入れてください")
    leaks = ancestor_instructions(Path(a.out))
    if leaks:
        static_ok = False
        print(f"NG: {_ancestor_message(leaks)}")
    if not a.live:
        print("（--live を付けると、隔離環境で認証・配布物の読み込みを実際に確認します。1 プロファイルあたり 1 回の最小呼び出し）")
        return 0 if static_ok else 1
    probe = Task(id="doctor-probe", prompt="Reply with exactly: OK", path=Path("doctor.yaml").resolve(),
                 max_turns=1, timeout_s=180, graders=[Grader("regex", {"pattern": r"\bOK\b"})])
    opt = RunOptions(trials=1, llm=False, use_cache=False, keep="none", log=lambda s: None,
                     ledger=Path(a.out) / "spend.jsonl")  # doctor の費用も月の実績に入れる
    runner = Runner([probe], profiles, opt, Path(a.out) / "_doctor")
    run_dir, recs, _ = runner.run()
    ok = static_ok
    for r in recs:
        status = "OK" if r.passed else f"NG ({(r.error or {}).get('type', 'score=' + str(r.score))}: {(r.error or {}).get('message', '')[:200]})"
        ok &= r.passed
        print(f"\n[{r.profile_id}] 最小試行: {status} ／ モデル {r.meta.get('model')} ／ 費用 {r.cost_usd}")
        for key, label in (("loaded_skills", "スキル"), ("loaded_plugins", "プラグイン"), ("mcp_servers", "MCP")):
            items = r.meta.get(key)
            if items is not None:
                print(f"  読み込まれた{label} ({len(items)}): {', '.join(items)[:600]}")
    shutil.rmtree(run_dir, ignore_errors=True)
    return 0 if ok else 1


def cmd_calibrate(a) -> int:
    """LLM 判定と人手ラベルの一致率。labels.yaml: [{task, profile, trial, grader: <番号>, label: PASS|FAIL}]"""
    recs, _ = load_run(Path(a.run))
    idx = {(r.task_id, r.profile_id, r.trial): r for r in recs}
    labels = yaml.safe_load(Path(a.labels).read_text(encoding="utf-8")) or []
    n = agree = 0
    conf: dict[str, int] = {}
    for i, lb in enumerate(labels):
        r = idx.get((lb["task"], lb["profile"], int(lb["trial"])))
        if r is None:
            print(f"  labels[{i}]: 該当する試行がありません")
            continue
        gi = int(lb["grader"])
        if not 0 <= gi < len(r.grader_results):
            print(f"  labels[{i}]: grader {gi} がありません（0〜{len(r.grader_results) - 1}）")
            continue
        g = r.grader_results[gi]
        if g["type"] != "llm" or g["status"] in ("not_run", "skipped_prior_fail"):
            print(f"  labels[{i}]: LLM 判定が実行されていません（{g['status']}）")
            continue
        human = str(lb["label"]).lower()
        n += 1
        agree += g["status"] == human
        key = f"人手={human} / LLM={g['status']}"
        conf[key] = conf.get(key, 0) + 1
    if not n:
        print("比較できるラベルがありません")
        return 1
    print(f"一致率: {agree}/{n} = {agree / n:.0%}（UNKNOWN は不一致として数える）")
    for k2, v in sorted(conf.items()):
        print(f"  {k2}: {v}")
    return 0


def cmd_harvest(a) -> int:
    """セッション履歴から実際の依頼を集め、架空の内容に置き換えたタスクの下書きを作る。"""
    from .adapters import ClaudeAdapter
    from .harvest import default_sources, harvest
    from .runner import append_spend
    profile = None
    if a.profile:
        profile = load_profiles([a.profile])[0]
        if profile.agent != "claude_code":
            raise SpecError("--profile は claude_code のプロファイル（書き換えは Claude Code で行う）")
    sources = [Path(s) for s in a.source] if a.source else default_sources()
    run_id = "harvest-" + datetime.now().strftime("%Y%m%d-%H%M%S")
    s = harvest(sources, Path(a.out), a.limit, ClaudeAdapter(profile, a.executable).exe, a.model or None, profile, a.dry_run,
                print, lambda usd: append_spend(Path(a.ledger), usd, run_id, "claude_code", profile.billing if profile else "api"))
    if a.dry_run:
        print("（--dry-run: 書き換え・書き出しはしていません。依頼の本文は表示しません）")
        return 0
    if s["written"]:
        print(f"\n下書き {len(s['written'])} 件: {Path(a.out) / 'tasks'}（使う前に内容を確認してください）")
    if s["skipped"]:
        print("見送り: " + "、".join(f"{k} {v} 件" for k, v in s["skipped"].items()))
    print(f"次: python -m harness_eval validate --tasks {Path(a.out) / 'tasks'} --profiles <プロファイル>")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="harness-eval", description="Claude Code / Codex ハーネスの評価")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(sp, need_profiles=True):
        sp.add_argument("--tasks", nargs="+", required=True, help="タスク YAML（ファイルまたはディレクトリ）")
        sp.add_argument("--profiles", nargs="+", required=need_profiles, default=[], help="プロファイル YAML")

    def runopts(sp):
        sp.add_argument("--trials", type=int, default=1, help="1 組あたりの試行回数（スモーク 1、フル 3 を推奨）")
        sp.add_argument("--k", type=int, help="pass^k の k（既定: --trials と同じ）")
        sp.add_argument("--tag", action="append", help="このタグを持つタスクだけ（複数可）")
        sp.add_argument("--task", action="append", help="このタスク id だけ（複数可）")
        sp.add_argument("--jobs", type=int, default=1, help="並列数（既定 1。レート制限に注意）")
        sp.add_argument("--no-llm", action="store_true", help="LLM 判定を実行しない（スモーク用）")
        sp.add_argument("--judge-agent", default="claude_code", choices=["claude_code", "codex"])
        sp.add_argument("--judge-model", default="haiku", help="LLM 判定のモデル（既定 haiku。空文字で CLI 既定）")
        sp.add_argument("--judge-executable", nargs="+", help="判定に使う CLI の実行ファイル（既定: PATH 上の claude / codex）")
        sp.add_argument("--max-cost-usd", type=float, help="この実行全体の費用上限（到達後の試行は未実行）")
        sp.add_argument("--monthly-budget-usd", type=float, help="月の予算。--out 配下の今月の実績を引いた残りをこの実行の上限にする")
        sp.add_argument("--retry-infra", type=int, default=1, help="レート制限・過負荷で落ちた試行の再試行回数")
        sp.add_argument("--retry-wait-s", type=float, default=20.0, help="再試行までの待ち秒数（回数に比例して延びる）")
        sp.add_argument("--judge-profile", help="LLM 判定の認証・環境変数（env / copy_credentials / executable）を借りるプロファイル id。"
                                                 "既定: 判定エージェントと同じ種類の --baseline、なければ最初の同種プロファイル")
        sp.add_argument("--no-cache", action="store_true", help="キャッシュを使わず全試行を実行")
        sp.add_argument("--cache-dir", default=".harness-eval-cache")
        sp.add_argument("--keep", choices=["all", "failed", "none"], default="failed", help="作業ディレクトリを残す範囲（none は生ログも消す）")
        sp.add_argument("--baseline", help="Δ の基準にするプロファイル id")
        sp.add_argument("--seed", type=int, default=0, help="プロファイルの実行順を入れ替える乱数の種（同じ値なら同じ順）")
        sp.add_argument("--allow-ancestor-instructions", action="store_true",
                        help="--out より上に CLAUDE.md・AGENTS.md・.claude/ などがあっても実行する（結果に記録。比較は汚れうる）")
        sp.add_argument("--out", default="results", help="結果の出力先")

    sp = sub.add_parser("validate", help="YAML の検証と設計ルールの警告")
    common(sp)
    sp.add_argument("--selftest", action="store_true",
                    help="正解例（references/）があるタスクで、採点スクリプトを含む判定が正解例で通り何もしなければ落ちるかを実行して確かめる"
                         "（harvest の下書きを人が確認したあとに使う）")
    sp.set_defaults(fn=cmd_validate)
    sp = sub.add_parser("plan", help="実行前の見積もり（呼び出し回数・キャッシュ・概算費用）")
    common(sp)
    runopts(sp)
    sp.set_defaults(fn=cmd_plan)
    sp = sub.add_parser("run", help="評価を実行してレポートを出力")
    common(sp)
    runopts(sp)
    sp.set_defaults(fn=cmd_run)
    sp = sub.add_parser("report", help="既存の結果から HTML レポートを作り直す")
    sp.add_argument("run")
    sp.add_argument("--baseline")
    sp.set_defaults(fn=cmd_report)
    sp = sub.add_parser("gate", help="前回結果と比べたリリース判定（終了コード 0 通過 / 1 不通過 / 3 判定不能）")
    sp.add_argument("--current", required=True)
    sp.add_argument("--previous", required=True)
    sp.add_argument("--metric", default="pass_hat_k", choices=["pass_hat_k", "pass_at_1", "mean_score"])
    sp.add_argument("--margin", type=float, required=True,
                    help="非劣性マージン: 許容する悪化の大きさ（例 0.05）。結果を見る前に、業務への影響から決めておく")
    sp.add_argument("--min-tasks", type=int, default=10,
                    help="判定に必要な比較できたタスク数（既定 10。少ないと区間が狭く出すぎるため判定不能にする）")
    sp.add_argument("--hard-category", action="append",
                    help=f"1 件でもあれば不通過にする失敗分類を追加（複数可。{list(HARD_CATEGORIES)} は常に含む）")
    sp.set_defaults(fn=cmd_gate)
    sp = sub.add_parser("doctor", help="環境検証（CLI・認証・配布物の読み込み）")
    sp.add_argument("--profiles", nargs="+", required=True)
    sp.add_argument("--live", action="store_true", help="最小の試行を実際に 1 回ずつ実行する")
    sp.add_argument("--out", default="results")
    sp.set_defaults(fn=cmd_doctor)
    sp = sub.add_parser("harvest", help="セッション履歴の実際の依頼から、架空化したタスクの下書きを作る")
    sp.add_argument("--out", required=True, help="書き出し先（tasks/・fixtures/・checks/・references/ を作る）")
    sp.add_argument("--source", nargs="+", help="履歴の場所（既定: ~/.claude と ~/.codex。フォルダか履歴ファイル）")
    sp.add_argument("--limit", type=int, default=10, help="作る下書きの最大数（新しいセッションから。既定 10）")
    sp.add_argument("--model", default="sonnet", help="書き換えに使うモデル（既定 sonnet。空文字で CLI 既定）")
    sp.add_argument("--profile", help="認証（env・copy_credentials・executable）を借りる claude_code プロファイル YAML")
    sp.add_argument("--executable", nargs="+", help="書き換えに使う claude の実行ファイル")
    sp.add_argument("--ledger", default="results/spend.jsonl", help="費用台帳（run の --out と同じ場所を推奨）")
    sp.add_argument("--dry-run", action="store_true", help="件数だけ数える（書き換えない・費用 0）")
    sp.set_defaults(fn=cmd_harvest)
    sp = sub.add_parser("calibrate", help="LLM 判定と人手ラベルの一致率")
    sp.add_argument("run")
    sp.add_argument("--labels", required=True)
    sp.set_defaults(fn=cmd_calibrate)

    a = ap.parse_args(argv)
    try:
        return a.fn(a)
    except KeyboardInterrupt:
        print("中断しました（この実行の集計・レポートは作られていません。trials.jsonl に途中までの記録があります）", file=sys.stderr)
        return 130
    except SpecError as e:
        print(f"定義エラー: {e}", file=sys.stderr)
        return 2
